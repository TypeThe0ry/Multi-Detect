#!/usr/bin/env python3
"""Concurrent 3-D teacher observation ONLY; default plan-only, never fly.

Original camera payloads and receive-timestamped MAVLink journals are retained
without training admission, model inference, ARM, Mission or actuator output.
CPU-injected dependencies cannot establish external-SIM execution evidence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_json,
    require_int,
    require_real,
)
from scripts.onboard_teacher_observed_context import (  # noqa: E402
    REQUIRED_MESSAGES,
    build_observed_teacher_context,
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
)

CONTRACT = "multidetect.onboard-teacher-gazebo-observation-v3"


def observe_teacher(
    plan,
    output_directory,
    *,
    execute=False,
    context_timeout_s=30.0,
    maximum_records=8192,
    queue_capacity=256,
    camera_windows=3,
    camera_interval_s=1,
    session_factory=None,
    capture_factory=None,
    context_builder=build_observed_teacher_context,
    artifact_root=ROOT,
):
    if type(execute) is not bool:
        raise TypeError("execute must be bool")
    timeout = require_real(context_timeout_s, "context_timeout_s", minimum=1, maximum=60)
    maximum_records = require_int(maximum_records, "maximum_records", minimum=16, maximum=8192)
    queue_capacity = require_int(queue_capacity, "queue_capacity", minimum=1, maximum=4096)
    camera_windows = require_int(camera_windows, "camera_windows", minimum=1, maximum=8)
    interval = require_real(camera_interval_s, "camera_interval_s", minimum=0, maximum=10)
    directory = Path(output_directory).resolve()
    directory.relative_to(Path(artifact_root).resolve())
    if directory.exists():
        raise ValueError("Fresh observation output required; never overwrite evidence")
    injected = (
        session_factory is not None
        or capture_factory is not None
        or context_builder is not build_observed_teacher_context
    )
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "execute_requested": execute,
        "injected_dependencies_used": injected,
        "external_SIM_observed_here": False,
        "context_qualified": False,
        "telemetry_stream_request_API_calls": 0,
        "camera_frames_recorded": 0,
        "camera_frames": [],
        "camera": [],
        "qualification_attempts": [],
        "raw_telemetry": None,
        "receiver": None,
        "context_error": None,
        "error": None,
        "session": None,
        "capture_close_error": None,
        "session_report_error": None,
        "closed_snapshot_is_live_context": False,
        "plan": plan.to_mapping(),
        "claims": dict(FALSE_CLAIMS),
        "receipt": None,
    }
    runtime, runtime_source = _runtime_source(plan, artifact_root)
    report["runtime_source"] = runtime_source.to_mapping()
    if not execute:
        return report  # No process/socket/journal or output directory in plan-only.
    directory.mkdir(exist_ok=False)
    _json_new(directory / "plan.json", report["plan"])
    if session_factory is None:
        from scripts.onboard_teacher_gazebo_session_v3 import OwnedTeacherGazeboSessionV3

        session_factory = OwnedTeacherGazeboSessionV3
    capture_factory = TeacherTelemetryCaptureV3 if capture_factory is None else capture_factory
    session = capture = receiver = None

    def collect(timeout_s=0.02):
        samples = receiver.read_batch(timeout_s)
        if samples:
            capture.append_batch(samples)
        receiver.raise_if_failed()

    def camera_updates():
        rows = session.camera_updates()
        for row in rows:
            report["camera_frames"].append(row)
            report["camera"].append(row["camera"])
            report["camera_frames_recorded"] += 1
        if rows and not injected:
            report["external_SIM_observed_here"] = True

    def stop_and_drain():
        # Native camera helper is not allowed to outlive container cleanup;
        # socket ownership is stopped before connection.close or final seal.
        stop_errors = []
        for stop in (session.stop_camera, session.stop_receiver):
            try:
                stop()
            except BaseException as error:
                stop_errors.append(error)
        # There can be at most queue_capacity rows after the RX thread joined.
        drained = 0
        while drained < queue_capacity:
            batch = receiver.read_batch(0)
            if not batch:
                break
            drained += len(batch)
            capture.append_batch(batch)
        report["receiver"] = receiver.to_mapping()
        if report["receiver"]["thread_alive"] or report["receiver"]["queued_packets"]:
            raise RuntimeError("RX producer/queue remains at telemetry finalization")
        if stop_errors:
            raise stop_errors[0]
        receiver.raise_if_failed()

    try:
        session = session_factory(plan, execute=True)
        with session:
            transaction_id = f"teacher-observe-{uuid4().hex}"
            local_origin_id = f"unresolved-ekf-origin-{uuid4().hex}"
            report.update(
                clock=session.clock.identity(),
                transaction_id=transaction_id,
                local_origin_id=local_origin_id,
            )
            profile = _profile(plan)
            _json_new(directory / "profile.json", profile)
            profile_source = _source(directory / "profile.json", artifact_root)
            capture = capture_factory(
                directory / "telemetry",
                clock=session.clock.monotonic,
                clock_id=session.clock.session_id,
                transaction_id=transaction_id,
                local_origin_id=local_origin_id,
                max_records=maximum_records,
            )
            receiver = session.start_receiver(queue_capacity=queue_capacity)
            try:
                deadline = session.clock.monotonic() + timeout
                while session.clock.monotonic() < deadline:
                    collect()
                    row = capture.latest_message(
                        "HEARTBEAT", now_s=session.clock.monotonic(), maximum_age_s=2
                    )
                    if row is not None:
                        report["initial_accepted_heartbeat"] = row
                        break
                else:
                    raise ValueError("No persisted source-admitted HEARTBEAT before deadline")
                requests = receiver.request_streams((*REQUIRED_MESSAGES, *EXTRA_MESSAGES))
                report["stream_requests"] = _json_new(
                    directory / "stream-requests.json",
                    {"requests": requests, "claims": FALSE_CLAIMS},
                )
                report["telemetry_stream_request_API_calls"] = len(requests)
                session.start_camera_windows(
                    directory, _persist_camera, windows=camera_windows, interval_s=interval
                )
                deadline = session.clock.monotonic() + timeout
                next_attempt = 0
                while session.clock.monotonic() < deadline:
                    collect()
                    camera_updates()
                    camera_done = session.camera_finished()
                    now = session.clock.monotonic()
                    present = {
                        name: capture.latest_message(name, now_s=now, maximum_age_s=2)
                        for name in REQUIRED_MESSAGES
                    }
                    report["latest_present_message_types"] = sorted(
                        name for name, row in present.items() if row is not None
                    )
                    if now < next_attempt:
                        continue
                    next_attempt = now + 0.5
                    attempt_index = len(report["qualification_attempts"])
                    camera_at = (
                        report["camera_frames"][-1]["host_return_at_s"]
                        if report["camera_frames"]
                        else None
                    )
                    attempt = {
                        "attempt_index": attempt_index,
                        "observed_at_s": now,
                        "present": report["latest_present_message_types"],
                        "camera_host_return_age_s": None if camera_at is None else now - camera_at,
                        "camera_age_is_sensor_clock_mapping": False,
                        "receive_ages_s": {
                            name: None if row is None else now - row["received_at_s"]
                            for name, row in present.items()
                        },
                        "receive_skew_s": None,
                        "qualified": False,
                        "error": None,
                    }
                    report["qualification_attempts"].append(attempt)
                    if any(row is None for row in present.values()) or camera_at is None:
                        attempt["error"] = "Fresh source-bound telemetry/camera not complete"
                        continue
                    # Missing/stale snapshot requests poison the writer: check
                    # all keys first, then export; never catch and resume a failed export.
                    times = [row["received_at_s"] for row in present.values()]
                    attempt["receive_skew_s"] = max(times) - min(times)
                    rows = capture.materialize_snapshot(
                        list(REQUIRED_MESSAGES),
                        directory / f"snapshot-{attempt_index:03d}",
                        now_s=now,
                        maximum_age_s=2,
                    )
                    _verify_source(runtime_source, artifact_root)
                    now = session.clock.monotonic()
                    resource_path = directory / f"resources-{attempt_index:03d}.json"
                    _json_new(
                        resource_path,
                        _resources(
                            plan,
                            profile,
                            session,
                            camera_at,
                            now_s=now,
                            transaction_id=transaction_id,
                            runtime=runtime,
                        ),
                    )
                    try:
                        context = context_builder(
                            rows,
                            artifact_root=artifact_root,
                            now_s=now,
                            captured_at_utc=session.clock.utc_now().isoformat(),
                            clock_id=session.clock.session_id,
                            transaction_id=transaction_id,
                            local_origin_id=local_origin_id,
                            profile_source=profile_source,
                            profile_id=profile["airframe_profile_id"],
                            resource_source=_source(resource_path, artifact_root),
                        )
                        after = session.clock.monotonic()
                        if not 0 <= after - context.context.captured_at_s <= 2:
                            raise ValueError("Context became stale during qualification")
                        binding = _json_new(
                            directory / f"observed-context-{attempt_index:03d}.json",
                            context.to_mapping(),
                        )
                        attestation = session.attest_qualified(
                            telemetry_qualified=True
                        ).to_mapping()
                        _verify_source(runtime_source, artifact_root)
                        final = session.clock.monotonic()
                        if not 0 <= final - context.context.captured_at_s <= 2:
                            raise ValueError("Context became stale during source/ownership checks")
                    except (ValueError, TypeError) as error:
                        attempt["error"] = f"{type(error).__name__}: {error}"[:2048]
                        report["context_error"] = attempt["error"]
                        report["context_qualified"] = False
                        continue
                    attempt.update(qualified=True, qualified_at_s=final)
                    report.update(
                        context=binding,
                        context_qualified=True,
                        context_qualified_at_observer_s=final,
                        context_error=None,
                        attestation=attestation,
                    )
                    if camera_done:
                        break
                camera_updates()
                if not session.camera_finished():
                    raise RuntimeError(
                        "Declared camera windows incomplete before observation deadline"
                    )
                report["diagnostic_rows"] = {
                    name: capture.latest_message(
                        name, now_s=session.clock.monotonic(), maximum_age_s=10
                    )
                    for name in (*REQUIRED_MESSAGES, *EXTRA_MESSAGES)
                }
                stop_and_drain()
                report["raw_telemetry"] = capture.seal()
                capture = None
                report["status"] = (
                    "observed-context-qualified"
                    if report["context_qualified"]
                    else "context-refused"
                )
                if not report["context_qualified"]:
                    report.pop("context", None)
                    report.pop("attestation", None)
                    if report["context_error"] is None:
                        report["context_error"] = "Required fresh telemetry missing before deadline"
            finally:
                if capture is not None:
                    try:
                        stop_and_drain()
                    except BaseException as close_error:
                        report["capture_close_error"] = (
                            f"{type(close_error).__name__}: {close_error}"[:2048]
                        )
                    try:
                        report["raw_telemetry"] = capture.abort(
                            "Observation refused/failed; no downstream flight"
                        )
                    except BaseException as close_error:
                        report["capture_close_error"] = (
                            f"{type(close_error).__name__}: {close_error}"[:2048]
                        )
                    finally:
                        capture = None
    except BaseException as error:
        report.update(
            status="observation-failed",
            context_qualified=False,
            error=f"{type(error).__name__}: {error}"[:2048],
        )
        report.pop("context", None)
        report.pop("attestation", None)
        if capture is not None:
            try:
                report["raw_telemetry"] = capture.abort("Session initialization failed; no flight")
            except BaseException as close_error:
                report["capture_close_error"] = f"{type(close_error).__name__}: {close_error}"[
                    :2048
                ]
    finally:
        if receiver is not None:
            report["receiver"] = receiver.to_mapping()
        if session is not None:
            try:
                report["session"] = session.to_mapping()
                attempts = report["session"].get("stream_request_API_attempts")
                if attempts is not None:
                    report["telemetry_stream_request_API_attempts"] = len(attempts)
                    report["telemetry_stream_request_API_calls"] = sum(
                        row["API_returned"] is True for row in attempts
                    )
            except BaseException as error:
                report.update(
                    status="observation-failed",
                    context_qualified=False,
                    session_report_error=f"{type(error).__name__}: {error}"[:2048],
                )
                report.pop("context", None)
                report.pop("attestation", None)
        report["receipt"] = _json_new(directory / "observation.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--context-timeout-s", type=float, default=30)
    parser.add_argument("--queue-capacity", type=int, default=256)
    parser.add_argument("--camera-windows", type=int, default=3)
    args = parser.parse_args(argv)
    from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan

    output = args.output_directory.resolve()
    plan = build_teacher_session_plan(
        args.scenario, output.parent / f"{output.name}-job", frame_count=2
    )
    report = observe_teacher(
        plan,
        output,
        execute=args.execute,
        context_timeout_s=args.context_timeout_s,
        queue_capacity=args.queue_capacity,
        camera_windows=args.camera_windows,
    )
    print(
        canonical_json(
            {
                key: report[key]
                for key in ("status", "context_qualified", "error", "context_error", "receipt")
            }
        )
    )
    return 0 if report["status"] in {"planned", "observed-context-qualified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
