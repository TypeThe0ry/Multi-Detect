"""Collect NEW current sources inside an already open owned V3 SIM session.

This callable does not launch Docker, request streams, restart RX, seal the
journals, approve a Mission, or send a flight command. Native/camera work has
one separate finite owner; the caller remains the only telemetry consumer and
dual-commit writer. A successful collection is not a successful qualifier.
"""

from __future__ import annotations

import copy
import math
import threading
from pathlib import Path

from multidetect.autonomy.serialization import strict_json_loads
from multidetect.autonomy.teacher_live_navigation_checkpoint import (
    capture_teacher_committed_prefix,
    navigation_parameter_baseline_argv,
    navigation_reference_phase_argv,
    qualify_teacher_live_navigation_checkpoint,
)
from scripts.collect_onboard_teacher_gazebo_origin_window import WORLD_ARGS
from scripts.onboard_teacher_gazebo_session import BoundedTeacherCamera, TeacherSessionPlan
from scripts.onboard_teacher_gazebo_session_v3 import (
    ConcurrentTelemetryReceiver,
    OwnedTeacherGazeboSessionV3,
)
from scripts.onboard_teacher_native_prefix import capture_native_prefix
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3
from scripts.run_onboard_teacher_gazebo_observation_v2 import (
    _json_new,
    _persist_camera,
    _profile,
    _runtime_source,
    _source,
    _verify_source,
    _write_new,
)

CONTRACT = "multidetect.teacher-live-navigation-collection-v1"
MAXIMUM_PHASE_S = 30.0
NATIVE_LIMIT_BYTES = 256 * 1024
CLAIMS = {
    "execution_authority": False,
    "Grant_created": False,
    "Mission_approved": False,
    "ARM_sent": False,
    "flight_executed": False,
    "physical_performance_claimed": False,
    "final_release_allowed": False,
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _error(error):
    return f"{type(error).__name__}: {error}"[:1024]


def _finite_timeout(value):
    _require(
        type(value) in (int, float) and math.isfinite(value) and 0 < value <= MAXIMUM_PHASE_S,
        "Explicit bounded collection timeout required",
    )
    return float(value)


def _check_open(session, receiver, capture, plan):
    _require(
        isinstance(session, OwnedTeacherGazeboSessionV3)
        and type(plan) is TeacherSessionPlan
        and session.plan is plan
        and session.execute is True
        and session._entered is True
        and session._closed is False
        and session._failure is None,
        "Exact currently open owned teacher session/plan required",
    )
    _require(
        isinstance(receiver, ConcurrentTelemetryReceiver)
        and session.receiver is receiver
        and receiver._owner == threading.get_ident() == session._owner
        and receiver.clock == session.clock.monotonic,
        "Original single RX and main consumer required",
    )
    receiver.raise_if_failed()
    state = receiver.to_mapping()
    _require(
        state["started"] is True
        and state["thread_alive"] is True
        and state["stopped"] is False
        and not receiver._stop.is_set(),
        "Original RX must remain live",
    )
    _require(
        isinstance(capture, TeacherTelemetryCaptureV3)
        and capture._owner == threading.get_ident()
        and capture._clock == session.clock.monotonic
        and capture.clock_id == session.clock.session_id
        and 16 <= capture.max_records <= 16384,
        "Same bounded V3 main writer/live clock required; V4 needs NEW adapter",
    )
    capture._open()
    _require(not capture._busy, "Idle dual-commit writer required")
    _require(plan.frame_count == 2, "This provider requires a declared two-frame camera plan")
    _require(
        session._active_camera is None
        and (session._camera_thread is None or not session._camera_thread.is_alive()),
        "Cannot overlap an existing camera owner",
    )
    session._remaining()


class _SourceOwner:
    """One bounded native/camera worker; never reads RX or calls writer APIs."""

    def __init__(self, session, directory, deadline_s, parameter_baseline):
        self.session, self.directory, self.deadline_s = session, directory, deadline_s
        self.baseline = copy.deepcopy(parameter_baseline)
        self.rows, self.camera_updates, self.camera_stats = {}, [], None
        self.failure = None
        self.done, self.cancelled = threading.Event(), threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="teacher-live-source-owner", daemon=True
        )

    def _now(self):
        now = self.session.clock.monotonic()
        _require(
            not self.cancelled.is_set()
            and not self.session._closed
            and self.session._failure is None
            and now < self.deadline_s,
            "Current source collection cancelled/closed/deadline exhausted",
        )
        self.session._remaining()
        return now

    def _command(self, name):
        session = self.session
        executable, cid = session.plan.docker_plan.docker_executable, session.container_id
        if name in {"owned-inspect-before", "owned-inspect-after"}:
            return [executable, "container", "inspect", cid]
        arguments = {
            "parameter-baseline": navigation_parameter_baseline_argv(),
            "navigation-before": navigation_reference_phase_argv(),
            "navigation-after": navigation_reference_phase_argv(),
            "loaded-world-sdf": WORLD_ARGS,
        }.get(name)
        _require(arguments is not None, "Only fixed readonly current source commands allowed")
        return [executable, "exec", cid, *arguments]

    def _native(self, name):
        started = self._now()
        row = {
            "name": name,
            "command": self._command(name),
            "container_id": self.session.container_id,
            "clock_id": self.session.clock.session_id,
            "started_at_s": started,
            "finished_at_s": None,
            "API_attempted": False,
            "API_returned": False,
            "native_exit_code": None,
            "streams": {},
            "error": None,
            "claims": dict(CLAIMS),
        }
        self.rows[name] = row
        primary = None
        try:
            _json_new(self.directory / f"{name}-attempt.json", row)
            row["API_called_at_s"] = self._now()
            row["API_attempted"] = True
            result = capture_native_prefix(
                row["command"],
                timeout_s=min(4.0, self.deadline_s - self._now(), self.session._remaining()),
                prefix_bytes=NATIVE_LIMIT_BYTES,
                output_limit_bytes=NATIVE_LIMIT_BYTES,
            )
            row["API_returned"] = True
            _require(
                type(result) is dict and type(result.get("streams")) is dict,
                "Original bounded native capture result required",
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
                    type(raw) is bytes and len(raw) <= NATIVE_LIMIT_BYTES,
                    "Bounded original native stream bytes required",
                )
                binding = _write_new(self.directory / f"{name}.{label}.bin", raw)
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
                and row["reader_threads_exited"] is True
                and row["complete_output_observed"] is True
                and row["timeout"] is False,
                "Native source failed/incomplete; originals retained without qualification",
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
                    "Complete stdout AND stderr byte identity required",
                )
            raw_stdout = result["streams"]["stdout"]["prefix"]
            _require(bool(raw_stdout), "Empty original native source refused")
            if name.startswith("owned-inspect"):
                inspected = strict_json_loads(raw_stdout)
                _require(
                    type(inspected) is list and len(inspected) == 1,
                    "Single exact original owned inspect required",
                )
                _require(
                    self.session._owned(inspected[0]) == self.session.container_id,
                    "Current owned container identity differs",
                )
            self._now()
            return row
        except BaseException as error:
            primary, row["error"] = error, _error(error)
            raise
        finally:
            row["finished_at_s"] = self.session.clock.monotonic()
            try:
                _require(row["finished_at_s"] >= started, "Native source clock regressed")
                _json_new(self.directory / f"{name}-result.json", row)
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"Native result persistence also failed: {_error(error)}")

    def _camera(self):
        session = self.session
        self._now()
        backend = session._camera(session.plan, session.container_id, session.clock)
        camera, primary = None, None
        try:
            camera = BoundedTeacherCamera(backend, session.plan, session.clock, self.deadline_s)
            with session._camera_lock:
                _require(session._active_camera is None, "Camera owner overlap refused")
                session._active_camera = camera
            for index in range(2):
                self._now()
                observation = camera.read(deadline_s=self.deadline_s)
                target = self.directory / f"camera-{index:03d}"
                target.mkdir()
                binding = _persist_camera(target, observation)
                self.camera_updates.append(
                    {
                        "window_index": 0,
                        "frame_index": index,
                        "camera": binding,
                        "host_return_at_s": observation.host_return_stamp.value_ns / 1e9,
                        "source_sensor_stamp_ns": observation.provenance.sensor_stamp_ns,
                    }
                )
            camera.close(require_complete=True)
            self.camera_stats = camera.source.stats.to_mapping()
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                if camera is not None:
                    camera.close(require_complete=primary is None)
                else:
                    backend.close()
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note(f"Scoped camera cleanup also failed: {_error(error)}")
            finally:
                with session._camera_lock:
                    if session._active_camera is camera:
                        session._active_camera = None

    def _run(self):
        try:
            if self.baseline is None:
                self.baseline = self._native("parameter-baseline")
            self._native("loaded-world-sdf")
            self._native("owned-inspect-before")
            self._native("navigation-before")
            self._camera()
            self._native("navigation-after")
            self._native("owned-inspect-after")
        except BaseException as error:
            self.failure = error
        finally:
            self.done.set()


def collect_live_checkpoint(
    session,
    receiver,
    capture,
    plan,
    root,
    output,
    *,
    profile_source=None,
    runtime_source=None,
    world_source=None,
    model_sources,
    parameter_baseline=None,
    timeout_s=20.0,
):
    """Return the frozen typed CP only after actual open-source revalidation.

    ``root`` is the artifact/repository root. ``output`` must be fresh beneath
    it. ``model_sources`` contains original source-bound fixed-image x500 and
    x500_base assets plus the repository composite, not target/world truth.
    A previous CP's original ``inputs.parameter_baseline`` may be supplied;
    it is re-read by the qualifier and current parameter_update must match.
    Current loaded-world original is in ``output/collection.json`` for a
    separate route/environment validator; its mere presence grants nothing.
    """
    timeout = _finite_timeout(timeout_s)
    _check_open(session, receiver, capture, plan)
    root, directory = Path(root).resolve(), Path(output).resolve()
    _require(
        directory.is_relative_to(root) and directory != root and not directory.exists(),
        "Fresh contained collection output required",
    )
    _require(
        type(model_sources) is dict and bool(model_sources),
        "Original model source closure required",
    )
    _require(
        parameter_baseline is None or type(parameter_baseline) is dict,
        "Original source-bound parameter baseline row required",
    )
    directory.mkdir()
    sources_dir = directory / "sources"
    sources_dir.mkdir()
    started = session.clock.monotonic()
    deadline = min(session.deadline_s, started + timeout)
    checkpoint, worker, primary = None, None, None
    report = {
        "contract": CONTRACT,
        "clock_id": session.clock.session_id,
        "container_id": session.container_id,
        "owner_token": session.owner_token,
        "started_at_s": started,
        "finished_at_s": None,
        "deadline_s": deadline,
        "native_rows": {},
        "camera_updates": [],
        "parameter_baseline": parameter_baseline,
        "baseline_reused": parameter_baseline is not None,
        "collection_complete": False,
        "checkpoint_qualified": False,
        "writer_sealed": False,
        "RX_restarted": False,
        "worker_exited": False,
        "error": None,
        "claims": dict(CLAIMS),
    }
    try:
        if profile_source is None:
            path = directory / "profile.json"
            _json_new(path, _profile(plan))
            profile_source = _source(path, root)
        if runtime_source is None:
            _, runtime_source = _runtime_source(plan, root)
        if world_source is None:
            world_source = _source(plan.docker_plan.world_path, root)
        for source in (profile_source, runtime_source, world_source, *model_sources.values()):
            _verify_source(source, root)
        report["sources"] = {
            "profile": profile_source.to_mapping(),
            "runtime": runtime_source.to_mapping(),
            "world": world_source.to_mapping(),
            "models": {k: v.to_mapping() for k, v in model_sources.items()},
        }
        worker = _SourceOwner(session, sources_dir, deadline, parameter_baseline)
        worker.thread.start()
        while not worker.done.is_set():
            _check_open_current(session, receiver, capture, deadline)
            batch = receiver.read_batch(0.01)
            if batch:
                capture.append_batch(batch)
        worker.thread.join(timeout=0.1)
        _require(not worker.thread.is_alive(), "Scoped source worker exit unobserved")
        if worker.failure is not None:
            raise worker.failure
        _check_open_current(session, receiver, capture, deadline)
        # Drain only a bounded immediately available queue prefix, never wait
        # for a permanently empty RX stream. Do not append after CP export.
        for _ in range(receiver.capacity):
            batch = receiver.read_batch(0)
            if not batch:
                break
            capture.append_batch(batch)
        _check_open_current(session, receiver, capture, deadline)
        report["collection_complete"] = True
        prefix = capture_teacher_committed_prefix(
            capture,
            artifact_root=root,
            output_directory=directory / "prefix",
            clock=session.clock,
        )
        report["prefix_source"] = prefix.to_mapping()
        now = session.clock.monotonic()
        checkpoint = qualify_teacher_live_navigation_checkpoint(
            artifact_root=root,
            output_directory=directory / "checkpoint",
            prefix_source=prefix,
            profile_source=profile_source,
            runtime_source=runtime_source,
            world_source=world_source,
            model_sources=model_sources,
            camera_updates=worker.camera_updates,
            parameter_baseline=worker.baseline,
            navigation_before=worker.rows["navigation-before"],
            navigation_after=worker.rows["navigation-after"],
            owned_inspect_before=worker.rows["owned-inspect-before"],
            owned_inspect_after=worker.rows["owned-inspect-after"],
            plan=plan.to_mapping(),
            container_id=session.container_id,
            owner_token=session.owner_token,
            now_s=now,
            clock_id=session.clock.session_id,
            live_capture=capture,
            live_clock=session.clock,
        )
        report["checkpoint_source"] = checkpoint.checkpoint_source.to_mapping()
        report["checkpoint_qualified"] = True
        return checkpoint
    except BaseException as error:
        primary, report["error"] = error, _error(error)
        raise
    finally:
        if worker is not None:
            worker.cancelled.set()
            worker.thread.join(timeout=5)
            report["worker_exited"] = not worker.thread.is_alive()
            if report["worker_exited"]:
                report["native_rows"] = copy.deepcopy(worker.rows)
                report["camera_updates"] = copy.deepcopy(worker.camera_updates)
                report["camera_stats"] = copy.deepcopy(worker.camera_stats)
                report["parameter_baseline"] = copy.deepcopy(worker.baseline)
            else:
                report["cleanup_error"] = (
                    "Scoped source worker still live; outer supervisor required"
                )
        report["finished_at_s"] = session.clock.monotonic()
        report["writer_closed_at_return"] = capture._closed
        report["RX_at_return"] = receiver.to_mapping()
        try:
            _json_new(directory / "collection.json", report)
            if worker is not None and not report["worker_exited"] and primary is None:
                raise RuntimeError(report["cleanup_error"])
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note(f"Collection receipt persistence also failed: {_error(error)}")


def _check_open_current(session, receiver, capture, deadline):
    _require(
        not session._closed and session._failure is None,
        "Owned session closed/failed during collection",
    )
    _require(session.clock.monotonic() < deadline, "Live source collection deadline exhausted")
    session._remaining()
    _require(
        session.receiver is receiver
        and capture._owner == threading.get_ident() == session._owner
        and receiver._owner == session._owner
        and capture._clock == session.clock.monotonic == receiver.clock
        and capture.clock_id == session.clock.session_id,
        "Original RX/writer/live clock identity changed",
    )
    receiver.raise_if_failed()
    _require(
        receiver.to_mapping()["thread_alive"] is True and not receiver._stop.is_set(),
        "Original RX stopped during collection",
    )
    capture._open()
    _require(not capture._busy, "Dual-commit writer busy during source collection")


__all__ = ["CONTRACT", "collect_live_checkpoint"]
