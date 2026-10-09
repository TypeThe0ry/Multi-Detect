"""Actual owned SIM operator plus source-bound v2 preflight observation.

One explicit parameter selection, honest GCS presence and original RGB/depth.
No ARM, Mission, teacher action, model or training. Uses the frozen lifecycle
and raw journal, and a byte-bounded original prefix for verbose native logs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import require_int, require_real  # noqa: E402
from scripts.onboard_teacher_native_prefix import capture_native_prefix  # noqa: E402
from scripts.onboard_teacher_observed_context import REQUIRED_MESSAGES  # noqa: E402
from scripts.onboard_teacher_observed_context_v2 import (  # noqa: E402
    build_observed_teacher_context_v2,
)
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import (  # noqa: E402
    DEFAULT_SCENARIO,
    EXTRA_MESSAGES,
    FALSE_CLAIMS,
    _json_new,
    _persist_camera,
    _profile,
    _resources,
    _runtime_source,
    _source,
    _verify_source,
    _write_new,
)

CONTRACT = "multidetect.teacher-owned-SIM-operator-observation-v1"
PARAMETERS = (
    "SYS_AUTOSTART",
    "MAV_TYPE",
    "BAT1_SOURCE",
    "BAT1_N_CELLS",
    "BAT1_CAPACITY",
    "BAT1_A_PER_V",
    "SIM_BAT_DRAIN",
    "SIM_BAT_MIN_PCT",
    "COM_ARM_WO_GPS",
    "COM_RC_IN_MODE",
    "EKF2_GPS_CTRL",
    "SYS_HAS_GPS",
    "COM_RCL_EXCEPT",
    "NAV_RCL_ACT",
    "COM_RC_LOSS_T",
    "NAV_DLL_ACT",
    "COM_DL_LOSS_T",
    "BAT_AVRG_CURRENT",
)
DIAGNOSTIC_KINDS = {
    "HEARTBEAT",
    "AUTOPILOT_VERSION",
    "PARAM_VALUE",
    "PARAM_ERROR",
    "STATUSTEXT",
    "COMMAND_ACK",
    "UNKNOWN_410",
    "UNKNOWN_411",
    "SYS_STATUS",
    "BATTERY_STATUS",
    "VFR_HUD",
}


def _native_reads(session, directory, *, runner=None, result_rows=None):
    result_rows = [] if result_rows is None else result_rows
    specs = [("version", ["/opt/px4-gazebo/bin/px4-ver", "all"])]
    specs += [(name, ["/opt/px4-gazebo/bin/px4-param", "show", name]) for name in PARAMETERS]
    specs += [("container-log", None)]
    for index, (name, args) in enumerate(specs):
        identity = session._owned(session._inspect(session.container_id))
        if identity != session.container_id:
            raise RuntimeError("Exact owned native-read identity changed")
        command = [session.plan.docker_plan.docker_executable]
        command += (
            ["logs", "--tail", "200", identity] if args is None else ["exec", identity, *args]
        )
        row = {
            "name": name,
            "command": command,
            "container_id": identity,
            "started_at_s": session.clock.monotonic(),
            "finished_at_s": None,
            "clock_id": session.clock.session_id,
            "error": None,
            "telemetry_already_closed": True,
            "flight_authority": False,
            "source": "actual_owned_native_read" if runner is None else "CPU_fixture_native",
        }
        primary = None
        result_rows.append(row)  # before native API
        try:
            result = (capture_native_prefix if runner is None else runner)(
                command, timeout_s=min(15, session._remaining())
            )
            streams = result.pop("streams")
            row.update(result)
            row["streams"] = {}
            for label in ("stdout", "stderr"):
                item = dict(streams[label])
                prefix = item.pop("prefix")
                binding = _write_new(
                    directory / f"native-{index:02d}-{name}.{label}.prefix.bin", prefix
                )
                row["streams"][label] = {**item, "retained_prefix": binding}
            if not result["complete_output_observed"] or not result["reader_threads_exited"]:
                raise RuntimeError("Native stream incomplete; original prefixes retained")
            if args is not None and not all(
                item["all_bytes_retained"] for item in streams.values()
            ):
                raise ValueError("Small version/parameter result not completely retained")
        except BaseException as error:
            primary = error
            row["error"] = f"{type(error).__name__}: {error}"[:2048]
            raise
        finally:
            try:
                row["finished_at_s"] = session.clock.monotonic()
            except BaseException as error:
                row["finished_at_s"] = None
                row["secondary_error"] = f"{type(error).__name__}: {error}"[:2048]
                if primary is not None:
                    primary.add_note(row["secondary_error"])
                else:
                    raise
    session._owned(session._inspect(session.container_id))
    return result_rows


def observe_sim_operator(
    plan,
    output_directory,
    *,
    execute=False,
    operator_intent=None,
    observation_s=20,
    queue_capacity=256,
    session_factory=None,
    capture_factory=None,
    native_runner=None,
    operator_native_runner=None,
    artifact_root=ROOT,
):
    if type(execute) is not bool:
        raise TypeError("Explicit boolean execute required")
    duration = require_real(observation_s, "observation_s", minimum=1, maximum=30)
    capacity = require_int(queue_capacity, "queue_capacity", minimum=16, maximum=4096)
    root, directory = Path(artifact_root).resolve(), Path(output_directory).resolve()
    directory.relative_to(root)
    if directory.exists():
        raise ValueError("Fresh diagnostic output required; originals never overwritten")
    runtime, runtime_binding = _runtime_source(plan, root)
    injected = any(
        item is not None
        for item in (
            session_factory,
            capture_factory,
            native_runner,
            operator_native_runner,
        )
    )
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "execute_requested": execute,
        "injected_dependencies_used": injected,
        "external_SIM_observed_here": False,
        "context_qualified": False,
        "closed_snapshot_is_live_context": False,
        "plan": plan.to_mapping(),
        "runtime_source": runtime_binding.to_mapping(),
        "observation_s": duration,
        "operator": None,
        "operator_intent": None if operator_intent is None else operator_intent.to_mapping(),
        "context_attempts": [],
        "camera_frames": [],
        "parameter_names": list(PARAMETERS),
        "request_rows": [],
        "diagnostic_rows": [],
        "native_reads": [],
        "raw_telemetry": None,
        "receiver": None,
        "session": None,
        "error": None,
        "close_errors": [],
        "claims": dict(FALSE_CLAIMS),
    }
    if not execute:
        return report
    from scripts.onboard_teacher_gazebo_session_v4 import DiagnosticRequest
    from scripts.onboard_teacher_sim_operator import (
        DisposableSimOperatorIntent,
        OwnedTeacherGazeboSessionV5,
        SimOperator,
    )

    if type(operator_intent) is not DisposableSimOperatorIntent:
        raise ValueError("Explicit source-bound disposable SIM operator intent required")
    factory = OwnedTeacherGazeboSessionV5 if session_factory is None else session_factory
    capture_type = TeacherTelemetryCaptureV3 if capture_factory is None else capture_factory
    directory.mkdir(exist_ok=False)
    _json_new(directory / "plan.json", report["plan"])
    session = receiver = capture = operator = None
    profile = profile_source = None
    next_attempt = 0
    stopped = False

    def collect(timeout_s=0.02):
        batch = receiver.read_batch(timeout_s)
        if batch:
            rows = capture.append_batch(batch)
            for row in rows:
                if row["message"]["message_type"] in DIAGNOSTIC_KINDS:
                    if len(report["diagnostic_rows"]) >= 2048:
                        raise ValueError("Diagnostic row budget exhausted; no silent truncation")
                    report["diagnostic_rows"].append(row)
        receiver.raise_if_failed()
        report["camera_frames"].extend(session.camera_updates())

    def attempt_context():
        nonlocal next_attempt
        now = session.clock.monotonic()
        if now < next_attempt:
            return
        next_attempt = now + 1.0
        index = len(report["context_attempts"])
        if index >= 30:
            raise ValueError("Bounded context attempt budget exhausted")
        present = {
            name: capture.latest_message(name, now_s=now, maximum_age_s=2)
            for name in REQUIRED_MESSAGES
        }
        attempt = {
            "attempt_index": index,
            "at_s": now,
            "present": sorted(name for name, row in present.items() if row is not None),
            "context": None,
            "qualified": False,
            "error": None,
        }
        report["context_attempts"].append(attempt)
        if any(row is None for row in present.values()):
            attempt["error"] = "Fresh nine-message observation incomplete"
            return
        rows = capture.materialize_snapshot(
            list(REQUIRED_MESSAGES),
            directory / f"snapshot-{index:03d}",
            now_s=now,
            maximum_age_s=2,
        )
        _verify_source(runtime_binding, root)
        now = session.clock.monotonic()
        camera_at = (
            report["camera_frames"][-1]["host_return_at_s"] if report["camera_frames"] else -1e30
        )
        resource_path = directory / f"resources-{index:03d}.json"
        _json_new(
            resource_path,
            _resources(
                plan,
                profile,
                session,
                camera_at,
                now_s=now,
                transaction_id=report["transaction_id"],
                runtime=runtime,
            ),
        )
        try:
            context = build_observed_teacher_context_v2(
                rows,
                artifact_root=root,
                now_s=now,
                captured_at_utc=session.clock.utc_now().isoformat(),
                clock_id=session.clock.session_id,
                transaction_id=report["transaction_id"],
                local_origin_id=report["local_origin_id"],
                profile_source=profile_source,
                profile_id=profile["airframe_profile_id"],
                resource_source=_source(resource_path, root),
            )
            if not 0 <= session.clock.monotonic() - context.context.captured_at_s <= 2:
                raise ValueError("Context stale during qualification")
            attempt["context"] = _json_new(
                directory / f"context-{index:03d}.json", context.to_mapping()
            )
            if not 0 <= session.clock.monotonic() - context.context.captured_at_s <= 2:
                raise ValueError("Context became stale during persistence; binding retained")
            attempt["qualified"] = True
            report["context_qualified"] = True
        except ValueError as error:
            attempt["error"] = f"{type(error).__name__}: {error}"[:2048]

    def stop_and_drain():
        nonlocal stopped
        stop_error = None
        if not stopped:
            for stop in (session.stop_camera, session.stop_receiver):
                try:
                    stop()
                except BaseException as error:
                    if stop_error is None:
                        stop_error = error
                    else:
                        stop_error.add_note(f"Additional stop failure: {error}")
            stopped = True
        report["camera_frames"].extend(session.camera_updates())
        before = receiver.to_mapping()
        if before["thread_alive"]:
            raise RuntimeError("RX remains alive; journals cannot finalize")
        remaining = before["queued_packets"]
        if type(remaining) is not int or not 0 <= remaining <= capacity:
            raise RuntimeError("Bounded queued telemetry required after RX join")
        while remaining:
            batch = receiver.read_batch(0)
            if not batch or len(batch) > remaining:
                raise RuntimeError("Finite RX queue drain differs")
            rows = capture.append_batch(batch)
            report["diagnostic_rows"].extend(
                row for row in rows if row["message"]["message_type"] in DIAGNOSTIC_KINDS
            )
            remaining -= len(batch)
        if len(report["diagnostic_rows"]) > 2048:
            raise ValueError("Diagnostic row budget exceeded during final drain")
        report["receiver"] = receiver.to_mapping()
        if report["receiver"]["queued_packets"]:
            raise RuntimeError("RX queue not empty after finite drain")
        if stop_error is not None:
            raise stop_error
        receiver.raise_if_failed()

    try:
        session = factory(plan, execute=True)
        with session:
            operator = SimOperator(
                session,
                operator_intent,
                directory / "sim-operator",
                repository_root=root,
                native_runner=operator_native_runner,
            )
            report["operator"] = operator.apply()
            transaction = "teacher-SIM-operator-" + uuid4().hex
            origin = "unresolved-operator-origin-" + uuid4().hex
            report.update(
                clock=session.clock.identity(), transaction_id=transaction, local_origin_id=origin
            )
            profile = _profile(plan)
            _json_new(directory / "profile.json", profile)
            profile_source = _source(directory / "profile.json", root)
            capture = capture_type(
                directory / "telemetry",
                clock=session.clock.monotonic,
                clock_id=session.clock.session_id,
                transaction_id=transaction,
                local_origin_id=origin,
                max_records=8192,
            )
            receiver = session.start_receiver(queue_capacity=capacity)
            try:
                deadline = session.clock.monotonic() + duration
                while session.clock.monotonic() < deadline:
                    collect()
                    heartbeat = capture.latest_message(
                        "HEARTBEAT",
                        now_s=session.clock.monotonic(),
                        maximum_age_s=2,
                    )
                    if heartbeat is not None:
                        report["initial_accepted_heartbeat"] = heartbeat
                        break
                else:
                    raise ValueError("No durable source-bound heartbeat before diagnostic deadline")
                report["request_rows"].extend(
                    receiver.request_streams((*REQUIRED_MESSAGES, *EXTRA_MESSAGES))
                )
                collect(0)
                session.start_camera_windows(directory, _persist_camera, windows=1, interval_s=0)
                specs = [DiagnosticRequest("autopilot-version")]
                specs += [DiagnosticRequest("parameter-read", name) for name in PARAMETERS]
                # Bound time away from the writer. No second receive loop while requesting.
                for offset in range(0, len(specs), 4):
                    report["request_rows"].extend(
                        receiver.request_diagnostics(tuple(specs[offset : offset + 4]))
                    )
                    collect(0)
                deadline = session.clock.monotonic() + duration
                while session.clock.monotonic() < deadline:
                    collect()
                    attempt_context()
                stop_and_drain()
                receipt = capture.seal()
                capture = None  # Closed original journal is not a fresh live context.
                report["raw_telemetry"] = {
                    key: receipt[key]
                    for key in (
                        "receipt",
                        "record_count",
                        "accepted_source_count",
                        "foreign_source_count",
                        "orphan_packet_count",
                        "journal_sources",
                        "commit_counts",
                        "status",
                    )
                }
                if receipt["status"] != "recorded":
                    raise RuntimeError("Original diagnostic capture did not seal successfully")
                _verify_source(runtime_binding, root)
                _native_reads(
                    session, directory, runner=native_runner, result_rows=report["native_reads"]
                )
                _verify_source(runtime_binding, root)
                report["status"] = "SIM-operator-observations-recorded"
                report["external_SIM_observed_here"] = not injected
            finally:
                if capture is not None:
                    try:
                        stop_and_drain()
                    except BaseException as error:
                        report["close_errors"].append(f"{type(error).__name__}: {error}"[:2048])
                    try:
                        receipt = capture.abort("Readonly diagnostics failed; no flight/training")
                        report["raw_telemetry"] = {
                            "receipt": receipt["receipt"],
                            "status": receipt["status"],
                        }
                    except BaseException as error:
                        report["close_errors"].append(f"{type(error).__name__}: {error}"[:2048])
                    capture = None
    except BaseException as error:
        report.update(status="diagnostics-failed", error=f"{type(error).__name__}: {error}"[:2048])
        if capture is not None:
            try:
                capture.abort("Session initialization failed; no flight/training")
            except BaseException as close_error:
                report["close_errors"].append(f"{type(close_error).__name__}: {close_error}"[:2048])
    finally:
        if operator is not None:
            report["operator"] = operator.to_mapping()
        if receiver is not None:
            try:
                report["receiver"] = receiver.to_mapping()
            except BaseException as error:
                report["close_errors"].append(f"{type(error).__name__}: {error}"[:2048])
                report["status"] = "diagnostics-failed"
        if session is not None:
            try:
                report["session"] = session.to_mapping()
            except BaseException as error:
                report["close_errors"].append(f"{type(error).__name__}: {error}"[:2048])
                report["status"] = "diagnostics-failed"
        if report["close_errors"]:
            report["status"] = "diagnostics-failed"
        binding = _json_new(directory / "operator-observation.json", report)
        report["receipt"] = binding
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--operator-intent", type=Path, required=True)
    parser.add_argument("--observation-s", type=float, default=20)
    args = parser.parse_args(argv)
    from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan

    output = args.output_directory.resolve()
    plan = build_teacher_session_plan(
        args.scenario,
        output.parent / f"{output.name}-job",
        frame_count=2,
    )
    from scripts.diagnose_onboard_teacher_px4_events import strict_json
    from scripts.onboard_teacher_sim_operator import DisposableSimOperatorIntent

    intent = DisposableSimOperatorIntent.from_mapping(
        strict_json(args.operator_intent.read_bytes())
    )
    report = observe_sim_operator(
        plan,
        output,
        execute=args.execute,
        operator_intent=intent,
        observation_s=args.observation_s,
    )
    from multidetect.autonomy.serialization import canonical_json

    print(
        canonical_json(
            {
                key: report.get(key)
                for key in (
                    "status",
                    "error",
                    "external_SIM_observed_here",
                    "receipt",
                )
            }
        )
    )
    return 0 if report["status"] in {"planned", "SIM-operator-observations-recorded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
