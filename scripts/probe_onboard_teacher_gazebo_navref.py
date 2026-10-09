"""Bounded late nav-reference diagnostics, not an origin/flight qualifier.

An explicit V5 subclass reads fixed native commands after the frozen observer
has sealed telemetry and before its real owned cleanup. No second receive loop,
ARM, Mission, pose mutation, model, or training is introduced. Default plan-only.
Gazebo model/link output is retained, not guessed to be a world-frame pose.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import math
import re
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import canonical_json, require_real  # noqa: E402
from scripts.onboard_teacher_native_prefix import capture_native_prefix  # noqa: E402
from scripts.onboard_teacher_sim_operator_v2 import (  # noqa: E402
    DisposableSimOperatorIntent,
    OwnedTeacherGazeboSessionV5,
)
from scripts.run_onboard_teacher_gazebo_observation_v2 import (  # noqa: E402
    DEFAULT_SCENARIO,
    FALSE_CLAIMS,
    _json_new,
    _write_new,
)
from scripts.run_onboard_teacher_sim_operator_v2 import observe_sim_operator  # noqa: E402

CONTRACT = "multidetect.teacher-SIM-navref-late-probe-v1"
EXPERIMENT_ID = "teacher-SIM-origin-probe-20261008-r1"
PARAMETERS = (
    "EKF2_IMU_POS_X",
    "EKF2_IMU_POS_Y",
    "EKF2_IMU_POS_Z",
    "SENS_GPS0_OFFX",
    "SENS_GPS0_OFFY",
    "SENS_GPS0_OFFZ",
    "SENS_GPS0_ID",
    "SENS_GPS1_OFFX",
    "SENS_GPS1_OFFY",
    "SENS_GPS1_OFFZ",
    "SENS_GPS1_ID",
    "EKF2_HGT_REF",
    "EKF2_EV_CTRL",
    "SIM_GZ_EN_GPS",
    "SIM_GZ_EN_ODOM",
)
SPECS = tuple(
    (name, ("/opt/px4-gazebo/bin/px4-param", "show", "-q", name)) for name in PARAMETERS
) + (
    ("gz-model-pose", ("gz", "model", "-m", "x500_mono_cam_0", "--pose")),
    ("gz-base-link", ("gz", "model", "-m", "x500_mono_cam_0", "--link", "base_link")),
    (
        "vehicle-local-position",
        (
            "/opt/px4-gazebo/bin/px4-listener",
            "vehicle_local_position",
            "-n",
            "1",
        ),
    ),
)
SOURCE_PATHS = (
    "scripts/probe_onboard_teacher_gazebo_navref.py",
    "tests/test_probe_onboard_teacher_gazebo_navref.py",
    "scripts/run_onboard_teacher_sim_operator_v2.py",
    "scripts/onboard_teacher_sim_operator.py",
    "scripts/onboard_teacher_sim_operator_v2.py",
    "scripts/onboard_teacher_gazebo_session.py",
    "scripts/onboard_teacher_gazebo_session_v3.py",
    "scripts/onboard_teacher_gazebo_session_v4.py",
    "scripts/onboard_teacher_native_prefix.py",
    "scripts/run_onboard_teacher_gazebo_observation_v2.py",
    "scripts/onboard_teacher_telemetry_capture.py",
    "scripts/onboard_teacher_telemetry_capture_v2.py",
    "scripts/onboard_teacher_telemetry_capture_v3.py",
    "scripts/onboard_teacher_observed_context.py",
    "scripts/onboard_teacher_observed_context_v2.py",
    "scripts/run_simulator_docker.py",
    "scripts/gazebo_sensor_stream.py",
    "scripts/gazebo_depth_wire.py",
    "src/multidetect/autonomy/contracts.py",
    "src/multidetect/autonomy/serialization.py",
    "src/multidetect/clock.py",
    "src/multidetect/simulator/stack.py",
)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _record(path, *, allow_empty=False):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    _require(
        path.is_file()
        and (before.st_size > 0 or allow_empty)
        and before.st_size <= 8 * 1024 * 1024,
        "Bounded nonempty evidence file required",
    )
    with path.open("rb") as stream:
        raw = stream.read(before.st_size + 1)
    after = path.stat()

    def identity(stat):
        return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

    _require(
        identity(before) == identity(after) and len(raw) == before.st_size,
        "Evidence changed during read",
    )
    return {"path": path.as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def source_inventory():
    """Listed entrypoints only, not a full transitive/runtime attestation."""
    return [_record(ROOT / path) for path in SOURCE_PATHS]


def parse_quiet_value(raw, name):
    _require(
        name in PARAMETERS and type(raw) is bytes and 0 < len(raw) <= 1024,
        "Fixed parameter and bounded original numeric bytes required",
    )
    text = raw.decode("ascii", errors="strict").strip()
    _require(
        re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text),
        "Quiet parameter output is not one actual finite number",
    )
    value = float(text)
    _require(math.isfinite(value), "Nonfinite parameter output refused")
    return value


class NavrefProbeSession(OwnedTeacherGazeboSessionV5):
    """Same V5 owner/clock; late read hook never bypasses real cleanup."""

    def __init__(self, *args, probe_directory, probe_runner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.probe_directory = Path(probe_directory).resolve()
        self.probe_runner = probe_runner
        self.navref_probe = {
            "status": "not-started",
            "commands": [],
            "error": None,
            "diagnostic_after_telemetry_seal": True,
            "coeval_with_camera_or_selected_telemetry": False,
            "origin_qualified": False,
            "flight_authority": False,
            "native_runner_injected": probe_runner is not None,
        }

    def _probe_time(self):
        value = require_real(self.clock.monotonic(), "probe time", minimum=0)
        rows = self.navref_probe["commands"]
        previous = rows[-1].get("finished_at_s") if rows else None
        if previous is not None:
            _require(value >= previous, "Probe clock regressed")
        return value

    def _probe_owner(self):
        _require(
            self.execute is True and self._entered and not self._closed,
            "Opened current owned session required",
        )
        _require(
            type(self.container_id) is str and re.fullmatch(r"[0-9a-f]{64}", self.container_id),
            "Exact actual container ID required",
        )
        identity = self._owned(self._inspect(self.container_id))
        _require(identity == self.container_id, "Exact owned probe container changed")
        return identity

    def _probe_one(self, index, name, arguments):
        _require((name, tuple(arguments)) in SPECS, "Only fixed readonly probes permitted")
        identity = self._probe_owner()
        command = [self.plan.docker_plan.docker_executable, "exec", identity, *arguments]
        row = {
            "index": index,
            "name": name,
            "command": command,
            "container_id": identity,
            "clock_id": self.clock.session_id,
            "started_at_s": self._probe_time(),
            "finished_at_s": None,
            "API_attempted": False,
            "API_returned": False,
            "native_exit_code": None,
            "streams": {},
            "error": None,
            "telemetry_already_sealed": True,
            "origin_qualified": False,
        }
        self.navref_probe["commands"].append(row)
        primary = None
        try:
            row["before_API_receipt"] = _json_new(
                self.probe_directory / f"attempt-{index:02d}.json", row
            )
            row["API_attempted"] = True
            runner = capture_native_prefix if self.probe_runner is None else self.probe_runner
            result = runner(
                command,
                timeout_s=min(10, self._remaining()),
                prefix_bytes=128 * 1024,
                output_limit_bytes=256 * 1024,
            )
            row["API_returned"] = True
            _require(
                type(result) is dict and type(result.get("streams")) is dict,
                "Original bounded native result required",
            )
            for key in (
                "native_exit_code",
                "owned_process_id",
                "owned_process_exited",
                "reader_threads_exited",
                "timeout",
                "owned_process_killed",
                "complete_output_observed",
                "output_limit_bytes",
                "prefix_limit_bytes_per_stream",
            ):
                row[key] = result.get(key)
            for label in ("stdout", "stderr"):
                item = result["streams"][label]
                raw = item["prefix"]
                _require(
                    type(raw) is bytes and len(raw) <= 128 * 1024,
                    "Original bounded stdout/stderr bytes required",
                )
                binding = _write_new(self.probe_directory / f"native-{index:02d}.{label}.bin", raw)
                row["streams"][label] = {
                    "original_bytes": binding,
                    "observed_bytes": item.get("observed_bytes"),
                    "all_bytes_retained": item.get("all_bytes_retained"),
                    "full_output_sha256": item.get("full_output_sha256"),
                    "EOF": item.get("EOF"),
                    "error": item.get("error"),
                }
            _require(
                type(row["native_exit_code"]) is int
                and row["native_exit_code"] == 0
                and row["owned_process_exited"] is True
                and row["reader_threads_exited"] is True
                and row["complete_output_observed"] is True
                and row["timeout"] is False,
                "Native probe failed/incomplete; original bytes retained",
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
                    "Complete original native stream identity required",
                )
            if name in PARAMETERS:
                row["actual_quiet_parameter_value"] = parse_quiet_value(
                    result["streams"]["stdout"]["prefix"], name
                )
            else:
                _require(
                    row["streams"]["stdout"]["original_bytes"]["bytes"] > 0,
                    "Empty pose/listener output is not diagnostic evidence",
                )
                row["pose_or_listener_output_parsed"] = False
            self._probe_owner()
        except BaseException as error:
            primary = error
            row["error"] = f"{type(error).__name__}: {error}"[:2048]
            raise
        finally:
            try:
                finished = self._probe_time()
                _require(finished >= row["started_at_s"], "Native probe clock regressed")
                row["finished_at_s"] = finished
            except BaseException as error:
                row["timing_error"] = f"{type(error).__name__}: {error}"[:2048]
                if primary is not None:
                    primary.add_note(row["timing_error"])
                else:
                    raise

    def _probe_all(self):
        _require(self.navref_probe["status"] == "not-started", "Probe is single-use")
        receiver = None if self.receiver is None else self.receiver.to_mapping()
        _require(
            receiver is not None
            and receiver.get("thread_alive") is False
            and receiver.get("queued_packets") == 0,
            "Frozen unique receiver must be stopped and drained before late probes",
        )
        camera = self.to_mapping().get("camera_thread_alive")
        _require(camera is False, "Camera owner must have exited before late probes")
        self.probe_directory.mkdir(exist_ok=False)
        self.navref_probe["status"] = "reading"
        for index, (name, args) in enumerate(SPECS):
            self._probe_one(index, name, args)
        for row in self.navref_probe["commands"]:
            for binding in [
                row["before_API_receipt"],
                *(item["original_bytes"] for item in row["streams"].values()),
            ]:
                _require(
                    _record(binding["path"], allow_empty=True) == binding,
                    "Persisted original probe output changed before completion",
                )
        self.navref_probe["status"] = "recorded"

    def __exit__(self, error_type, body_error, traceback):
        primary = body_error
        if primary is None:
            try:
                self._probe_all()
            except BaseException as error:
                primary = error
                self.navref_probe.update(status="failed", error=f"{type(error).__name__}: {error}")
        else:
            self.navref_probe.update(
                status="skipped-body-failure", error=f"{type(primary).__name__}: {primary}"[:2048]
            )
        try:
            super().__exit__(
                type(primary) if primary else None,
                primary,
                primary.__traceback__ if primary else traceback,
            )
        except BaseException as error:
            if primary is not None:
                primary.add_note(f"Owned cleanup also failed: {type(error).__name__}: {error}")
            else:
                raise
        if body_error is not None:
            return False  # Never replace/suppress the original body failure.
        if primary is not None:
            raise primary
        return False

    def to_mapping(self):
        return {**super().to_mapping(), "navref_probe": copy.deepcopy(self.navref_probe)}


def probe_sim_operator(
    plan,
    output_directory,
    *,
    execute=False,
    operator_intent=None,
    observation_s=20,
    artifact_root=ROOT,
    observer=None,
    session_type=None,
    native_runner=None,
    operator_intent_binding=None,
):
    """Explicit wrapper; injected legacy flags are never promoted to actual authority."""
    _require(type(execute) is bool, "Explicit execute boolean required")
    directory, root = Path(output_directory).resolve(), Path(artifact_root).resolve()
    _require(
        directory.is_relative_to(root) and directory != root and not directory.exists(),
        "Fresh contained output directory required",
    )
    require_real(observation_s, "observation_s", minimum=1, maximum=30)
    _require(
        type(operator_intent) is DisposableSimOperatorIntent,
        "Exact explicit original operator intent required",
    )
    intent = replace(operator_intent, experiment_id=EXPERIMENT_ID)
    if operator_intent_binding is not None:
        _require(
            _record(operator_intent_binding["path"]) == operator_intent_binding,
            "Original operator intent binding differs",
        )
    before = source_inventory()
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "error": None,
        "experiment_id": EXPERIMENT_ID,
        "execute_requested": execute,
        "actual_operator_intent": intent.to_mapping(),
        "plan": plan.to_mapping(),
        "input_operator_intent_binding": operator_intent_binding,
        "sources_before": before,
        "sources_after": None,
        "selected_sources_unchanged_during_run": False,
        "source_inventory_scope": "explicit listed repository entrypoints; not transitive",
        "diagnostic_after_telemetry_seal": True,
        "coeval_origin_proof": False,
        "origin_qualified": False,
        "native_probe_observed_here": False,
        "injected_probe_dependencies_used": any(
            value is not None for value in (observer, session_type, native_runner)
        ),
        "legacy_injected_flags_preserved": True,
        "claims": {**FALSE_CLAIMS, "origin_qualified": False},
        "legacy_observation": None,
        "probe": None,
    }
    if not execute:
        return report
    sessions = []

    def factory(selected, *, execute):
        session = (NavrefProbeSession if session_type is None else session_type)(
            selected,
            execute=execute,
            probe_directory=directory / "navref-native",
            probe_runner=native_runner,
        )
        sessions.append(session)
        return session

    try:
        legacy = (observe_sim_operator if observer is None else observer)(
            plan,
            directory,
            execute=True,
            operator_intent=intent,
            observation_s=observation_s,
            session_factory=factory,
            artifact_root=root,
        )
        report["legacy_observation"] = legacy
        _require(len(sessions) == 1, "Exactly one inherited owned session required")
        state = sessions[0].to_mapping()
        report["probe"] = state["navref_probe"]
        _require(
            legacy["status"] == "SIM-operator-observations-recorded"
            and legacy["injected_dependencies_used"] is True
            and legacy["external_SIM_observed_here"] is False
            and state.get("cleanup_verified") is True
            and report["probe"]["status"] == "recorded"
            and len(report["probe"]["commands"]) == len(SPECS),
            "Inherited observation/probe/owned cleanup did not complete",
        )
        report.update(
            status="navref-probe-recorded",
            native_probe_observed_here=not report["injected_probe_dependencies_used"],
        )
    except BaseException as error:
        report.update(status="diagnostics-failed", error=f"{type(error).__name__}: {error}"[:2048])
    finally:
        if sessions:
            try:
                report["session"] = sessions[0].to_mapping()
                report["probe"] = report["session"]["navref_probe"]
            except BaseException as error:
                report.update(status="diagnostics-failed", session_error=str(error)[:2048])
        try:
            after = source_inventory()
            report["sources_after"] = after
            _require(before == after, "Selected source bytes changed during probe")
            if operator_intent_binding is not None:
                _require(
                    _record(operator_intent_binding["path"]) == operator_intent_binding,
                    "Original operator intent changed during probe",
                )
            report["selected_sources_unchanged_during_run"] = True
        except BaseException as error:
            report.update(status="diagnostics-failed", source_error=str(error)[:2048])
        if report["status"] != "navref-probe-recorded":
            report["native_probe_observed_here"] = False
        if not directory.exists():
            directory.mkdir(parents=False, exist_ok=False)
        report["receipt"] = _json_new(directory / "origin-probe.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--operator-intent", type=Path, required=True)
    parser.add_argument("--observation-s", type=float, default=20)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    from scripts.diagnose_onboard_teacher_px4_events import strict_json
    from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan

    intent_binding = _record(args.operator_intent)
    intent_raw = args.operator_intent.read_bytes()
    _require(
        len(intent_raw) == intent_binding["bytes"]
        and hashlib.sha256(intent_raw).hexdigest() == intent_binding["sha256"],
        "Original intent byte binding differs",
    )
    intent = DisposableSimOperatorIntent.from_mapping(strict_json(intent_raw))
    _require(_record(args.operator_intent) == intent_binding, "Original intent bytes changed")
    output = args.output_directory.resolve()
    plan = build_teacher_session_plan(
        args.scenario, output.parent / f"{output.name}-job", frame_count=2
    )
    report = probe_sim_operator(
        plan,
        output,
        execute=args.execute,
        operator_intent=intent,
        observation_s=args.observation_s,
        operator_intent_binding=intent_binding,
    )
    print(
        canonical_json(
            {
                key: report.get(key)
                for key in (
                    "status",
                    "error",
                    "native_probe_observed_here",
                    "origin_qualified",
                    "receipt",
                )
            }
        )
    )
    return 0 if report["status"] in {"planned", "navref-probe-recorded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
