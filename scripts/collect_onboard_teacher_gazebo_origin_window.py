"""Read-only co-window source collection; NEVER exact-sync/origin/flight admission.

One V5 RX owner and one V3 writer stay active while an independent bounded
native owner reads actual listener/parameter diagnostics. Four raw camera
frames lie between two requested listener snapshots. Source/sender times are
retained, not rewritten into a common epoch. Default is plan-only.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import queue
import sys
import threading
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

_PROBE = ROOT / "scripts/probe_onboard_teacher_gazebo_navref.py"
_raw = _PROBE.read_bytes()
if len(_raw) != 20972 or hashlib.sha256(_raw).hexdigest() != (
    "497ad75d63a6cde5fcd9edc33bec5eae921f01696dfb9313c2172c7acba21abf"
):
    raise RuntimeError("Frozen late navref probe source differs")

from multidetect.autonomy.serialization import (  # noqa:E402
    canonical_json,
    require_int,
    require_real,
)
from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan  # noqa:E402
from scripts.onboard_teacher_native_prefix import capture_native_prefix  # noqa:E402
from scripts.onboard_teacher_observed_context import REQUIRED_MESSAGES  # noqa:E402
from scripts.onboard_teacher_sim_operator_v2 import (  # noqa:E402
    DisposableSimOperatorIntent,
    OwnedTeacherGazeboSessionV5,
    SimOperator,
)
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3  # noqa:E402
from scripts.probe_onboard_teacher_gazebo_navref import (  # noqa:E402
    PARAMETERS,
    _record,
    _require,
    parse_quiet_value,
    source_inventory,
)
from scripts.probe_onboard_teacher_gazebo_navref import (  # noqa: E402
    SOURCE_PATHS as FROZEN_SOURCE_PATHS,
)
from scripts.run_onboard_teacher_gazebo_observation_v2 import (  # noqa:E402
    DEFAULT_SCENARIO,
    EXTRA_MESSAGES,
    FALSE_CLAIMS,
    _json_new,
    _persist_camera,
    _profile,
    _runtime_source,
    _verify_source,
    _write_new,
)

CONTRACT = "multidetect.teacher-SIM-origin-window-v1"
EXPERIMENT_ID = "teacher-SIM-origin-window-20261008-r1"
MAX_RECORDS = 16384
MAX_DATA_BYTES = 64 * 1024 * 1024
PHASE_TIMEOUT_S = 180
SOURCE_PATHS = (
    *FROZEN_SOURCE_PATHS,
    "scripts/collect_onboard_teacher_gazebo_origin_window.py",
    "tests/test_collect_onboard_teacher_gazebo_origin_window.py",
    "scripts/run_simulator_docker_acceptance.py",
    "src/multidetect/backends/gazebo_sim.py",
    "src/multidetect/vision.py",
    "src/multidetect/gazebo_depth.py",
    "configs/product-version.json",
    "configs/release-parity.json",
    "configs/training/onboard-policy-current-reference-v1.json",
)
SELECTED_KINDS = (
    "HOME_POSITION",
    "GPS_GLOBAL_ORIGIN",
    "LOCAL_POSITION_NED",
    "GLOBAL_POSITION_INT",
    "ATTITUDE",
    "ATTITUDE_QUATERNION",
)
PREFLIGHT_TOPICS = (
    "estimator_status_flags",
    "estimator_status",
    "vehicle_status",
    "failsafe_flags",
)
LISTENER_ARGS = ("/opt/px4-gazebo/bin/px4-listener", "vehicle_local_position", "-i", "0", "-n", "1")
WORLD_ARGS = (
    "gz",
    "service",
    "-s",
    "/world/default/generate_world_sdf",
    "--reqtype",
    "gz.msgs.SdfGeneratorConfig",
    "--reptype",
    "gz.msgs.StringMsg",
    "--timeout",
    "3000",
    "--req",
    "global_entity_gen_config { expand_include_tags { data: true } }",
)


class WindowTelemetryCapture(TeacherTelemetryCaptureV3):
    """Explicit NEW instance record limit; all frozen V3 byte/codec gates remain.

    No module-global monkeypatch. Frozen receipt still describes its V3 journal
    algorithm; the enclosing NEW contract declares actual max_records=16384.
    """

    def __init__(self, *args, **kwargs):
        _require("max_records" not in kwargs, "Window instance has a fixed record budget")
        super().__init__(*args, max_records=8192, **kwargs)
        self.max_records = MAX_RECORDS


def _sources(plan):
    rows = source_inventory()
    paths = [
        *(ROOT / path for path in SOURCE_PATHS),
        Path(plan.source_manifest_path),
        Path(plan.docker_plan.world_path),
    ]
    for path in paths:
        row = _record(path)
        if row not in rows:
            _require(
                not any(old["path"] == row["path"] for old in rows),
                "Source inventory path has conflicting identity",
            )
            rows.append(row)
    return rows


def _error(error):
    return f"{type(error).__name__}: {error}"[:2048]


class NativeReadOwner:
    """One non-RX worker; no writer/cache/control operations on this thread."""

    def __init__(self, session, directory, deadline_s, *, runner=None):
        self.session, self.directory = session, Path(directory)
        self.deadline_s, self.runner = deadline_s, runner
        self.directory.mkdir(exist_ok=False)
        self._owner = threading.get_ident()
        self._jobs, self._stop = queue.Queue(1), threading.Event()
        self._thread, self._failure = None, None
        self._used = set()
        self.rows, self.jobs = [], []

    def _check(self):
        now = require_real(self.session.clock.monotonic(), "native clock", minimum=0)
        _require(
            not self._stop.is_set() and now < self.deadline_s,
            "Native read owner cancelled or phase deadline reached",
        )
        return now

    def start(self):
        _require(
            threading.get_ident() == self._owner and self._thread is None,
            "Native owner is main-thread started once",
        )
        self._thread = threading.Thread(target=self._run, name="teacher-origin-native", daemon=True)
        self._thread.start()
        return self

    def submit(self, phase):
        _require(
            threading.get_ident() == self._owner
            and phase in {"before", "after"}
            and phase not in self._used
            and self._thread is not None,
            "Only one fixed before/after native job permitted",
        )
        self.raise_if_failed()
        self._used.add(phase)
        job = {
            "phase": phase,
            "done": threading.Event(),
            "rows": [],
            "error": None,
            "requested_at_s": self._check(),
        }
        self._jobs.put_nowait(job)
        self.jobs.append(job)
        return job

    @staticmethod
    def specs(phase):
        parameters = [
            (name, ("/opt/px4-gazebo/bin/px4-param", "show", "-q", name)) for name in PARAMETERS
        ]
        listener = [("vehicle-local-position", LISTENER_ARGS)]
        if phase == "before":
            return parameters + listener
        _require(phase == "after", "Unknown native phase")
        return (
            listener
            + [
                (name, ("/opt/px4-gazebo/bin/px4-listener", name, "-i", "0", "-n", "1"))
                for name in PREFLIGHT_TOPICS
            ]
            + [("loaded-world-sdf", WORLD_ARGS)]
            + parameters
        )

    def _native(self, phase, name, arguments):
        _require(
            (name, arguments) in self.specs(phase), "Only fixed readonly native argv permitted"
        )
        started = self._check()
        identity = self.session._owned(self.session._inspect(self.session.container_id))
        _require(identity == self.session.container_id, "Exact owned container changed")
        self._check()
        command = [self.session.plan.docker_plan.docker_executable, "exec", identity, *arguments]
        row = {
            "index": len(self.rows),
            "name": name,
            "role": phase,
            "command": command,
            "container_id": identity,
            "clock_id": self.session.clock.session_id,
            "started_at_s": started,
            "API_called_at_s": None,
            "finished_at_s": None,
            "API_attempted": False,
            "API_returned": False,
            "native_exit_code": None,
            "streams": {},
            "error": None,
            "parameter_or_health_qualified": False,
            "coeval_parameter_claimed": False,
            "origin_qualified": False,
        }
        self.rows.append(row)
        primary = None
        try:
            row["before_API_receipt"] = _json_new(
                self.directory / f"attempt-{row['index']:02d}.json", row
            )
            row["API_called_at_s"] = self._check()
            row["API_attempted"] = True
            limit = 1024 * 1024 if name == "loaded-world-sdf" else 256 * 1024
            runner = capture_native_prefix if self.runner is None else self.runner
            result = runner(
                command,
                timeout_s=min(10, self.deadline_s - self._check(), self.session._remaining()),
                prefix_bytes=256 * 1024,
                output_limit_bytes=limit,
            )
            row["API_returned"] = True
            _require(
                type(result) is dict and type(result.get("streams")) is dict,
                "Bounded original native result required",
            )
            for key in (
                "native_exit_code",
                "owned_process_id",
                "owned_process_exited",
                "reader_threads_exited",
                "complete_output_observed",
                "timeout",
                "output_limit_bytes",
                "prefix_limit_bytes_per_stream",
            ):
                row[key] = result.get(key)
            for label in ("stdout", "stderr"):
                item = result["streams"][label]
                raw = item["prefix"]
                _require(
                    type(raw) is bytes and len(raw) <= 256 * 1024,
                    "Bounded original native bytes required",
                )
                binding = _write_new(self.directory / f"native-{row['index']:02d}.{label}.bin", raw)
                row["streams"][label] = {
                    "original_bytes": binding,
                    **{
                        key: item.get(key)
                        for key in (
                            "observed_bytes",
                            "all_bytes_retained",
                            "full_output_sha256",
                            "EOF",
                            "error",
                        )
                    },
                }
            _require(
                type(row["native_exit_code"]) is int
                and row["native_exit_code"] == 0
                and row["owned_process_exited"] is True
                and row["timeout"] is False
                and row["reader_threads_exited"] is True
                and row["complete_output_observed"] is True,
                "Native read failed/incomplete; retained originals are diagnostic only",
            )
            for item in row["streams"].values():
                binding = item["original_bytes"]
                _require(
                    item["all_bytes_retained"] is True
                    and item["EOF"] is True
                    and item["error"] is None
                    and type(item["observed_bytes"]) is int
                    and item["observed_bytes"] == binding["bytes"]
                    and item["full_output_sha256"] == binding["sha256"],
                    "Complete native stdout/stderr byte identity required",
                )
            if name in PARAMETERS:
                row["actual_quiet_parameter_value"] = parse_quiet_value(
                    result["streams"]["stdout"]["prefix"], name
                )
            else:
                _require(
                    row["streams"]["stdout"]["original_bytes"]["bytes"] > 0,
                    "Empty native topic/world output refused",
                )
                row["native_output_semantics_parsed"] = False
            _require(
                self.session._owned(self.session._inspect(identity)) == identity,
                "Owned container changed after native read",
            )
            self._check()
        except BaseException as error:
            primary = error
            row["error"] = _error(error)
            raise
        finally:
            try:
                finished = require_real(self.session.clock.monotonic(), "native finish", minimum=0)
                _require(finished >= started, "Native observer clock regressed")
                row["finished_at_s"] = finished
                row["receipt"] = _json_new(self.directory / f"result-{row['index']:02d}.json", row)
            except BaseException as error:
                row["secondary_error"] = _error(error)
                if primary is None:
                    raise
                primary.add_note(row["secondary_error"])
        return row

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    job = self._jobs.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    for name, args in self.specs(job["phase"]):
                        job["rows"].append(self._native(job["phase"], name, args))
                except BaseException as error:
                    job["error"] = self._failure = error
                    raise
                finally:
                    job["done"].set()
        except BaseException as error:
            self._failure = error

    def raise_if_failed(self):
        if self._failure is not None:
            raise self._failure

    def close(self):
        _require(threading.get_ident() == self._owner, "Native owner closed by wrong thread")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(32)  # Frozen inspect <=30s; RX is stopped first on failure.
            _require(
                not self._thread.is_alive(), "Native owner did not exit before container cleanup"
            )

    def to_mapping(self):
        _require(
            self._thread is None or not self._thread.is_alive(),
            "Native report requires stopped worker; no unsynchronized live read",
        )
        return {
            "rows": copy.deepcopy(self.rows),
            "error": None if self._failure is None else _error(self._failure),
            "thread_alive": False,
            "jobs": [
                {
                    "phase": row["phase"],
                    "requested_at_s": row["requested_at_s"],
                    "complete": row["done"].is_set(),
                    "error": None if row["error"] is None else _error(row["error"]),
                }
                for row in self.jobs
            ],
            "owns_receiver": False,
            "owns_writer": False,
            "flight_authority": False,
        }


def collect_origin_window(
    plan,
    output_directory,
    *,
    execute=False,
    operator_intent=None,
    observation_s=30,
    queue_capacity=512,
    artifact_root=ROOT,
    session_factory=None,
    capture_factory=None,
    operator_factory=None,
    native_runner=None,
    operator_native_runner=None,
):
    _require(type(execute) is bool, "Explicit execute bool required")
    duration = require_real(observation_s, "observation_s", minimum=1, maximum=60)
    capacity = require_int(queue_capacity, "queue_capacity", minimum=16, maximum=4096)
    directory, root = Path(output_directory).resolve(), Path(artifact_root).resolve()
    _require(
        directory != root and directory.is_relative_to(root) and not directory.exists(),
        "Fresh contained observation directory required",
    )
    _require(
        type(operator_intent) is DisposableSimOperatorIntent,
        "Exact explicit disposable SIM operator intent required",
    )
    _require(plan.frame_count == 2, "Exactly two frames per each of two windows required")
    intent = replace(operator_intent, experiment_id=EXPERIMENT_ID)
    before = _sources(plan)
    _, runtime_binding = _runtime_source(plan, root)
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "error": None,
        "execute_requested": execute,
        "experiment_id": EXPERIMENT_ID,
        "actual_operator_intent": intent.to_mapping(),
        "plan": plan.to_mapping(),
        "runtime_source": runtime_binding.to_mapping(),
        "sources_before": before,
        "sources_after": None,
        "selected_sources_unchanged_during_run": False,
        "source_inventory_scope": "listed repository/scenario/world files; not transitive",
        "injected_dependencies_used": any(
            v is not None
            for v in (
                session_factory,
                capture_factory,
                operator_factory,
                native_runner,
                operator_native_runner,
            )
        ),
        "external_SIM_observed_here": False,
        "origin_qualified": False,
        "exact_synchronization_claimed": False,
        "frames_wire_epoch_mapping_verified": False,
        "capture_record_limit": MAX_RECORDS,
        "data_byte_limit": MAX_DATA_BYTES,
        "frozen_V3_constructor_record_limit": 8192,
        "new_instance_record_limit_explicit": True,
        "phase_timeout_s": PHASE_TIMEOUT_S,
        "observation_wait_budget_s": duration,
        "queue_capacity": capacity,
        "camera_frame_budget": 4,
        "listener_budget": 2,
        "parameters_before": {},
        "parameters_after": {},
        "parameters_unchanged": False,
        "listener_snapshots": [],
        "preflight_snapshots": [],
        "loaded_world_sdf": None,
        "camera_frames": [],
        "selected_telemetry": {kind: [] for kind in SELECTED_KINDS},
        "snapshot_rows": None,
        "snapshot_manifest": None,
        "raw_telemetry": None,
        "writer_stages": [],
        "close_errors": [],
        "claims": {**FALSE_CLAIMS, "origin_qualified": False},
    }
    if not execute:
        return report
    directory.mkdir(exist_ok=False)
    _json_new(directory / "plan.json", report["plan"])
    session = capture = receiver = operator = native = None
    primary = None
    deadline = None
    capture_closed = False

    def collect(timeout=0.01, *, cleanup=False):
        batch = receiver.read_batch(timeout)
        if batch:
            rows = capture.append_batch(batch)
            for row in rows:
                kind = row["message"]["message_type"]
                if kind in report["selected_telemetry"]:
                    report["selected_telemetry"][kind].append(row)
        report["camera_frames"].extend(session.camera_updates())
        if not cleanup:
            receiver.raise_if_failed()
            if native is not None:
                native.raise_if_failed()
            _require(
                session.clock.monotonic() < deadline, "Overall co-window phase deadline reached"
            )

    def wait_until(predicate, name):
        stage = min(deadline, session.clock.monotonic() + duration)
        row = {
            "name": name,
            "started_at_s": session.clock.monotonic(),
            "receiver_before": receiver.to_mapping(),
            "complete": False,
        }
        report["writer_stages"].append(row)
        try:
            while not predicate():
                collect()
                _require(session.clock.monotonic() < stage, f"Bounded {name} wait exhausted")
            collect(0)
            row["complete"] = True
        finally:
            row["finished_at_s"] = session.clock.monotonic()
            row["receiver_after"] = receiver.to_mapping()

    def stop_and_drain():
        failures = []
        if receiver is not None:
            try:
                session.stop_receiver()
            except BaseException as error:
                failures.append(error)
        if session is not None:
            try:
                session.stop_camera()
            except BaseException as error:
                failures.append(error)
        if native is not None:
            try:
                native.close()
            except BaseException as error:
                failures.append(error)
        if receiver is not None:
            state = receiver.to_mapping()
            _require(state["thread_alive"] is False, "RX did not exit before final drain")
            remaining = require_int(
                state["queued_packets"], "queued_packets", minimum=0, maximum=capacity
            )
            while remaining:
                count_before = receiver.to_mapping()["consumed_packets"]
                collect(0, cleanup=True)
                consumed = receiver.to_mapping()["consumed_packets"] - count_before
                _require(0 < consumed <= remaining, "Finite queue drain did not progress")
                remaining -= consumed
            receiver.raise_if_failed()
        if failures:
            for secondary in failures[1:]:
                failures[0].add_note(_error(secondary))
            raise failures[0]

    try:
        session = (OwnedTeacherGazeboSessionV5 if session_factory is None else session_factory)(
            plan, execute=True
        )
        with session:
            deadline = min(session.deadline_s, session.clock.monotonic() + PHASE_TIMEOUT_S)
            try:
                operator = (SimOperator if operator_factory is None else operator_factory)(
                    session,
                    intent,
                    directory / "sim-operator",
                    repository_root=root,
                    native_runner=operator_native_runner,
                )
                report["operator"] = operator.apply()
                report.update(
                    clock=session.clock.identity(),
                    transaction_id="origin-window-" + uuid4().hex,
                    local_origin_id="unresolved-origin-window-" + uuid4().hex,
                    profile=_profile(plan),
                )
                _json_new(directory / "profile.json", report["profile"])
                capture = (WindowTelemetryCapture if capture_factory is None else capture_factory)(
                    directory / "telemetry",
                    clock=session.clock.monotonic,
                    clock_id=session.clock.session_id,
                    transaction_id=report["transaction_id"],
                    local_origin_id=report["local_origin_id"],
                )
                receiver = session.start_receiver(queue_capacity=capacity)
                wait_until(
                    lambda: (
                        capture.latest_message(
                            "HEARTBEAT", now_s=session.clock.monotonic(), maximum_age_s=2
                        )
                        is not None
                    ),
                    "initial durable heartbeat",
                )
                report["request_rows"] = receiver.request_streams(
                    (*REQUIRED_MESSAGES, *EXTRA_MESSAGES)
                )
                collect(0)
                native = NativeReadOwner(
                    session, directory / "native", deadline, runner=native_runner
                ).start()
                first = native.submit("before")
                wait_until(first["done"].is_set, "native parameter-before/listener-before")
                native.raise_if_failed()
                listener_before = next(
                    r for r in first["rows"] if r["name"] == "vehicle-local-position"
                )
                report["listener_snapshots"].append(listener_before)
                report["camera_started_at_s"] = session.clock.monotonic()
                session.start_camera_windows(directory, _persist_camera, windows=2, interval_s=2)
                wait_until(session.camera_finished, "two late camera windows")
                _require(
                    len(report["camera_frames"]) == 4,
                    "Exactly four original camera frames required",
                )
                _require(
                    [(r["window_index"], r["frame_index"]) for r in report["camera_frames"]]
                    == [(0, 0), (0, 1), (1, 0), (1, 1)],
                    "Original camera window/frame ordering differs",
                )
                stamps = [
                    require_int(r["source_sensor_stamp_ns"], "camera source stamp", minimum=1)
                    for r in report["camera_frames"]
                ]
                _require(
                    all(a < b for a, b in zip(stamps, stamps[1:], strict=False)),
                    "Camera source frames repeat or source clock regressed between windows",
                )
                report["camera_completed_at_s"] = session.clock.monotonic()
                now = session.clock.monotonic()
                _require(
                    all(
                        capture.latest_message(k, now_s=now, maximum_age_s=2) is not None
                        for k in REQUIRED_MESSAGES
                    ),
                    "Fresh nine-message snapshot incomplete",
                )
                report["snapshot_rows"] = capture.materialize_snapshot(
                    list(REQUIRED_MESSAGES), directory / "snapshot", now_s=now, maximum_age_s=2
                )
                report["snapshot_manifest"] = _record(directory / "snapshot/snapshot-manifest.json")
                second = native.submit("after")
                wait_until(
                    second["done"].is_set, "native listener-after/preflight/world/parameter-after"
                )
                native.raise_if_failed()
                listener_after = next(
                    r for r in second["rows"] if r["name"] == "vehicle-local-position"
                )
                report["listener_snapshots"].append(listener_after)
                times = [row["host_return_at_s"] for row in report["camera_frames"]]
                _require(
                    listener_before["finished_at_s"] <= min(times)
                    and max(times) <= listener_after["started_at_s"],
                    "Listener host intervals do not bracket original camera host returns",
                )
                report["host_camera_bracket_observed"] = True
                for phase, job in (("before", first), ("after", second)):
                    report[f"parameters_{phase}"] = {
                        r["name"]: r for r in job["rows"] if r["name"] in PARAMETERS
                    }
                report["parameters_unchanged"] = all(
                    report["parameters_before"][name]["actual_quiet_parameter_value"]
                    == report["parameters_after"][name]["actual_quiet_parameter_value"]
                    for name in PARAMETERS
                )
                _require(report["parameters_unchanged"], "Actual nav-reference parameters changed")
                report["preflight_snapshots"] = [
                    r for r in second["rows"] if r["name"] in PREFLIGHT_TOPICS
                ]
                report["loaded_world_sdf"] = next(
                    r for r in second["rows"] if r["name"] == "loaded-world-sdf"
                )
                report["selected_telemetry_host_window"] = {
                    "start_s": listener_before["finished_at_s"],
                    "end_s": listener_after["started_at_s"],
                    "sender_epoch_equivalence_claimed": False,
                }
                stop_and_drain()
                receipt = capture.seal()
                capture_closed = True
                report["raw_telemetry"] = {
                    key: receipt[key]
                    for key in (
                        "status",
                        "receipt",
                        "record_count",
                        "packet_attempt_count",
                        "accepted_source_count",
                        "foreign_source_count",
                        "orphan_packet_count",
                        "data_budget_charged_bytes",
                        "journal_sources",
                        "commit_counts",
                    )
                }
                _require(receipt["status"] == "recorded", "V3 dual-journal close failed")
                _verify_source(runtime_binding, root)
                report["status"] = "origin-window-recorded"
            except BaseException as error:
                primary = error
                raise
            finally:
                if not capture_closed:
                    try:
                        stop_and_drain()
                    except BaseException as error:
                        report["close_errors"].append(_error(error))
                        if primary is not None:
                            primary.add_note(_error(error))
                        else:
                            primary = error
                    if capture is not None:
                        try:
                            receipt = capture.abort(
                                "Co-window failed; no origin/flight/training authority"
                            )
                            capture_closed = True
                            report["raw_telemetry"] = {
                                key: receipt[key]
                                for key in (
                                    "status",
                                    "receipt",
                                    "record_count",
                                    "packet_attempt_count",
                                    "orphan_packet_count",
                                    "journal_sources",
                                    "commit_counts",
                                )
                            }
                        except BaseException as error:
                            report["close_errors"].append(_error(error))
                            if primary is not None:
                                primary.add_note(_error(error))
                            else:
                                primary = error
                    if primary is not None and sys.exc_info()[0] is None:
                        raise primary
    except BaseException as error:
        report.update(status="diagnostics-failed", error=_error(error))
    finally:
        for key, value in (
            ("session", session),
            ("receiver", receiver),
            ("operator", operator),
            ("native_owner", native),
        ):
            if value is not None:
                try:
                    report[key] = value.to_mapping()
                except BaseException as error:
                    report["close_errors"].append(_error(error))
        try:
            after = _sources(plan)
            report["sources_after"] = after
            _require(before == after, "Selected sources changed during co-window")
            _verify_source(runtime_binding, root)
            report["selected_sources_unchanged_during_run"] = True
            _require(
                session is not None and report.get("session", {}).get("cleanup_verified") is True,
                "Real owned container cleanup did not verify",
            )
            for row in [] if native is None else native.rows:
                for binding in [
                    row["before_API_receipt"],
                    *(item["original_bytes"] for item in row["streams"].values()),
                ]:
                    _require(
                        _record(binding["path"], allow_empty=True) == binding,
                        "Native evidence changed before producer close",
                    )
        except BaseException as error:
            report["close_errors"].append(_error(error))
        if report["close_errors"]:
            report["status"] = "diagnostics-failed"
        report["external_SIM_observed_here"] = (
            report["status"] == "origin-window-recorded"
            and not report["injected_dependencies_used"]
        )
        report["receipt"] = _json_new(directory / "origin-window.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--operator-intent", type=Path, required=True)
    parser.add_argument("--observation-s", type=float, default=30)
    parser.add_argument("--queue-capacity", type=int, default=512)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    from scripts.diagnose_onboard_teacher_px4_events import strict_json

    output = args.output_directory.resolve()
    intent = DisposableSimOperatorIntent.from_mapping(
        strict_json(args.operator_intent.read_bytes())
    )
    plan = build_teacher_session_plan(
        args.scenario, output.parent / f"{output.name}-job", frame_count=2, session_timeout_s=240
    )
    report = collect_origin_window(
        plan,
        output,
        execute=args.execute,
        operator_intent=intent,
        observation_s=args.observation_s,
        queue_capacity=args.queue_capacity,
    )
    print(
        canonical_json(
            {
                key: report.get(key)
                for key in (
                    "status",
                    "error",
                    "external_SIM_observed_here",
                    "origin_qualified",
                    "receipt",
                )
            }
        )
    )
    return 0 if report["status"] in {"planned", "origin-window-recorded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
