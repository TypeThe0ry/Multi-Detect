"""Read-only owned PX4/Gazebo diagnostics; default plan-only, never fly.

One RX/send owner feeds the frozen dual journal. After RX stop/join and seal,
bounded native version/parameter/log reads use only the exact owned container.
This does not qualify a live context, change parameters, run AI or admit training.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import require_int, require_real  # noqa: E402
from scripts.onboard_teacher_observed_context import REQUIRED_MESSAGES  # noqa: E402
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3  # noqa: E402
from scripts.run_onboard_teacher_gazebo_observation_v2 import (  # noqa: E402
    DEFAULT_SCENARIO,
    EXTRA_MESSAGES,
    FALSE_CLAIMS,
    _json_new,
    _runtime_source,
    _verify_source,
    _write_new,
)

CONTRACT = "multidetect.teacher-owned-PX4-readonly-diagnostics-v1"
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
    """Only after RX is joined: diagnostic reads cannot masquerade as live context."""
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
        before = session.clock.monotonic()
        timeout = min(15, session._remaining())
        row = {
            "name": name,
            "command": command,
            "container_id": identity,
            "started_at_s": before,
            "finished_at_s": None,
            "clock_id": session.clock.session_id,
            "native_exit_code": None,
            "source": "injected_native_runner_not_external_SIM"
            if runner is not None
            else "actual_owned_container_read_not_metadata_default",
            "error": None,
            "telemetry_already_closed": True,
            "flight_authority": False,
        }
        result_rows.append(row)
        try:
            result = (
                subprocess.run(command, capture_output=True, timeout=timeout, check=False)
                if runner is None
                else runner(command, timeout_s=timeout)
            )
            if type(result.returncode) is not int:
                raise RuntimeError("Native readonly exit missing")
            row["native_exit_code"] = result.returncode
            if type(result.stdout) is not bytes or type(result.stderr) is not bytes:
                raise TypeError("Original native readonly output bytes required")
            if len(result.stdout) + len(result.stderr) > 256 * 1024:
                raise ValueError("Readonly native output budget exceeded")
            stem = f"native-{index:02d}-{name}"
            row["stdout"] = _write_new(directory / f"{stem}.stdout.bin", result.stdout)
            row["stderr"] = _write_new(directory / f"{stem}.stderr.bin", result.stderr)
        except BaseException as error:
            row["error"] = f"{type(error).__name__}: {error}"[:2048]
            raise
        finally:
            row["finished_at_s"] = session.clock.monotonic()
        # A missing parameter or nonzero native exit stays visible. It is not
        # silently translated to a metadata default, zero, or a preflight PASS.
    session._owned(session._inspect(session.container_id))
    return result_rows


def diagnose_px4(
    plan,
    output_directory,
    *,
    execute=False,
    observation_s=20,
    queue_capacity=256,
    session_factory=None,
    capture_factory=None,
    native_runner=None,
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
    _, runtime_binding = _runtime_source(plan, root)
    injected = any(item is not None for item in (session_factory, capture_factory, native_runner))
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
    from scripts.onboard_teacher_gazebo_session_v4 import (
        DiagnosticRequest,
        OwnedTeacherGazeboSessionV4,
    )

    factory = OwnedTeacherGazeboSessionV4 if session_factory is None else session_factory
    capture_type = TeacherTelemetryCaptureV3 if capture_factory is None else capture_factory
    directory.mkdir(exist_ok=False)
    _json_new(directory / "plan.json", report["plan"])
    session = receiver = capture = None
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

    def stop_and_drain():
        nonlocal stopped
        stop_error = None
        if not stopped:
            try:
                session.stop_receiver()
            except BaseException as error:
                stop_error = error
            stopped = True
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
            transaction = "teacher-diagnostics-" + uuid4().hex
            report.update(clock=session.clock.identity(), transaction_id=transaction)
            capture = capture_type(
                directory / "telemetry",
                clock=session.clock.monotonic,
                clock_id=session.clock.session_id,
                transaction_id=transaction,
                local_origin_id="unresolved-diagnostic-origin-" + uuid4().hex,
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
                report["status"] = "read-only-diagnostics-recorded"
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
        binding = _json_new(directory / "diagnostics.json", report)
        report["receipt"] = binding
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--observation-s", type=float, default=20)
    args = parser.parse_args(argv)
    from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan

    output = args.output_directory.resolve()
    plan = build_teacher_session_plan(
        args.scenario,
        output.parent / f"{output.name}-job",
        frame_count=2,
    )
    report = diagnose_px4(plan, output, execute=args.execute, observation_s=args.observation_s)
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
    return 0 if report["status"] in {"planned", "read-only-diagnostics-recorded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
