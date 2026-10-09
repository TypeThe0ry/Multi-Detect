#!/usr/bin/env python3
"""V2 owned 3-D observation with lossless typed telemetry, NEVER a flight runner.

Default is plan-only. --execute owns an isolated software-SIM session, records
unaltered camera payloads and source-filtered original MAVLink packets, and
attempts the observed-context bridge. Missing endurance/origin/pose is evidence,
not a reason to synthesize a constant or produce an OperatorGrant. This program
cannot upload a Mission, ARM, set mode, start an Actor, or train a model.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding  # noqa: E402
from multidetect.autonomy.serialization import (  # noqa: E402
    canonical_json,
    require_int,
    require_real,
    strict_json_loads,
)
from multidetect.simulator.stack import airframe_profile_hash  # noqa: E402
from scripts.onboard_teacher_observed_context import (  # noqa: E402
    REQUIRED_MESSAGES,
    build_observed_teacher_context,
)
from scripts.onboard_teacher_telemetry_capture_v2 import (  # noqa: E402
    TeacherTelemetryCaptureV2 as TeacherTelemetryCapture,
)

CONTRACT = "multidetect.onboard-teacher-gazebo-observation-v2"
DEFAULT_SCENARIO = ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json"
FALSE_CLAIMS = {
    key: False
    for key in (
        "coordinate_origin_verified",
        "sensor_clock_mapping_verified",
        "source_authenticated",
        "route_clearance_verified",
        "mission_approved",
        "mission_uploaded",
        "flight_commands_sent",
        "flight_executed",
        "actual_teacher_trajectory_collected",
        "teacher_expert_verified",
        "train_admitted",
        "model_loaded",
        "model_inference_performed",
        "optimizer_performed",
        "candidate_promoted",
        "default_model_changed",
        "execution_authority",
        "physical_performance_claimed",
        "hil_real_evidence",
        "outdoor_gnss_evidence",
        "final_release_allowed",
    )
}
EXTRA_MESSAGES = ("GPS_GLOBAL_ORIGIN", "ATTITUDE_QUATERNION")


def _write_new(path, raw):
    path = Path(path).resolve()
    with path.open("xb") as stream:
        if stream.write(raw) != len(raw):
            raise OSError("Short observation artifact write")
        stream.flush()
        os.fsync(stream.fileno())
    after = path.read_bytes()
    if after != raw:
        raise ValueError("Observation artifact differs after persistence")
    return {
        "path": path.as_posix(),
        "bytes": len(after),
        "sha256": hashlib.sha256(after).hexdigest(),
    }


def _json_new(path, value):
    return _write_new(path, (canonical_json(value) + "\n").encode("utf-8"))


def _source(path, root):
    path = Path(path).resolve(strict=True)
    relative = path.relative_to(Path(root).resolve()).as_posix()
    if not path.is_file() or not 0 < path.stat().st_size <= 8 * 1024 * 1024:
        raise ValueError("Bounded nonempty source file required")
    raw = path.read_bytes()
    return EvidenceBinding(relative, len(raw), hashlib.sha256(raw).hexdigest(), relative, "")


def _verify_source(source, root):
    root = Path(root).resolve()
    path = root / source.path
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Runtime source escaped its original artifact root")
    before = path.stat()
    with path.open("rb") as stream:
        raw = stream.read(source.bytes + 1)
    after = path.stat()

    def identity(stat):
        return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino

    if (
        identity(before) != identity(after)
        or len(raw) != source.bytes
        or hashlib.sha256(raw).hexdigest() != source.sha256
    ):
        raise RuntimeError("Original runtime source drifted; no derived-resource renewal")
    return raw


def _runtime_source(plan, root):
    source = Path(plan.source_manifest_path)
    if not source.name.endswith(".scenario.json"):
        raise ValueError("Repository scenario filename must end in .scenario.json")
    path = source.with_name(source.name.removesuffix(".scenario.json") + ".runtime.json")
    binding = _source(path, root)
    return strict_json_loads(_verify_source(binding, root)), binding


def _profile(plan):
    docker = plan.docker_plan
    return {
        "schema_version": 1,
        "airframe_profile_id": docker.airframe_profile_id,
        "vehicle_model": docker.vehicle_model,
        "model_name": docker.model_name,
        "stack_plan_sha256": docker.stack_plan_sha256,
    }


def _persist_camera(directory, observation):
    """Retain original wire bytes, not thumbnails or synthetic training images."""
    payloads = {}
    for name, value in (
        ("rgb-bgr24", observation.rgb_bgr24),
        ("depth", getattr(observation.depth, "payload", None)),
    ):
        if value is None:
            raise ValueError("Paired original RGB and depth required for this capture")
        if type(value) is not bytes or not 0 < len(value) <= 64 * 1024 * 1024:
            raise ValueError("Original bounded camera wire bytes required")
        payloads[name] = _write_new(directory / f"{name}.bin", value)
    metadata = observation.to_mapping()
    if (
        metadata["rgb_bgr24_sha256"] != payloads["rgb-bgr24"]["sha256"]
        or metadata["depth"]["payload_sha256"] != payloads["depth"]["sha256"]
    ):
        raise ValueError("Original camera provenance does not bind persisted wire payloads")
    metadata["persisted_payloads"] = payloads
    metadata["claims"] = dict(FALSE_CLAIMS)
    return _json_new(directory / "camera.json", metadata)


def _request_observation_streams(session, directory):
    """Telemetry requests only. No Mission, arm, mode or actuator command."""
    from pymavlink import mavutil

    sent = []
    for name in (*REQUIRED_MESSAGES, *EXTRA_MESSAGES):
        message_id = getattr(mavutil.mavlink, f"MAVLINK_MSG_ID_{name}")
        now = session.clock.monotonic()
        args = (1, 1, 511, 0, message_id, 250000, 0, 0, 0, 0, 0)
        session.connection.mav.command_long_send(*args)
        sent.append(
            {
                "kind": "telemetry-stream-request",
                "message_type": name,
                "args": list(args),
                "API_called_at_s": now,
                "clock_id": session.clock.session_id,
                "acknowledged": False,
                "datagram_delivery_independently_verified": False,
            }
        )
    return _json_new(
        directory / "stream-requests.json", {"requests": sent, "claims": dict(FALSE_CLAIMS)}
    )


def _resources(plan, profile, session, camera_at_s, *, now_s, transaction_id, runtime):
    # A configured wind is not a measurement, and these flags are NOT a PASS.
    assets = plan.docker_plan.assets
    return {
        "clock_id": session.clock.session_id,
        "transaction_id": transaction_id,
        "observed_at_s": now_s,
        "airframe_profile_hash": airframe_profile_hash(profile),
        "geofence_valid": False,
        "camera_available": 0 <= now_s - camera_at_s <= 2.0,
        "perception_available": False,
        "map_cache_available": assets.map.path.is_file(),
        "dem_available": assets.dem.path.is_file(),
        "wind_north_mps": runtime["world"]["wind_north_mps"],
        "wind_east_mps": runtime["world"]["wind_east_mps"],
        "wind_source": "software-SIM-configured",
    }


def observe_teacher(
    plan,
    output_directory,
    *,
    execute=False,
    context_timeout_s=30.0,
    maximum_records=8192,
    session_factory=None,
    context_builder=build_observed_teacher_context,
    artifact_root=ROOT,
):
    """Return durable evidence even after refusal; CLI exit remains nonzero.

    Injected factories are CPU fixtures, never external SIM evidence. Runtime
    source IDs and the unresolved-origin label are NOT sender authentication or
    a coordinate mapping. Only the raw-capture object owns network receives.
    """
    if type(execute) is not bool:
        raise TypeError("execute must be bool")
    timeout = require_real(context_timeout_s, "context_timeout_s", minimum=1, maximum=60)
    maximum_records = require_int(maximum_records, "maximum_records", minimum=16, maximum=8192)
    directory = Path(output_directory).resolve()
    directory.relative_to(Path(artifact_root).resolve())
    directory.mkdir(exist_ok=False)
    injected = session_factory is not None or context_builder is not build_observed_teacher_context
    report = {
        "contract": CONTRACT,
        "status": "planned",
        "execute_requested": execute,
        "injected_dependencies_used": injected,
        "external_SIM_observed_here": False,
        "context_qualified": False,
        "telemetry_stream_request_API_calls": 0,
        "camera_frames_recorded": 0,
        "raw_telemetry": None,
        "context_error": None,
        "error": None,
        "session": None,
        "capture_close_error": None,
        "session_report_error": None,
        "closed_snapshot_is_live_context": False,
        "plan": plan.to_mapping(),
        "claims": dict(FALSE_CLAIMS),
    }
    runtime, runtime_source = _runtime_source(plan, artifact_root)
    report["runtime_source"] = runtime_source.to_mapping()
    _json_new(directory / "plan.json", report["plan"])
    if not execute:
        report["receipt"] = _json_new(directory / "observation.json", report)
        return report
    if session_factory is None:
        from scripts.onboard_teacher_gazebo_session import OwnedTeacherGazeboSession

        session_factory = OwnedTeacherGazeboSession
    capture = None
    session = None
    try:
        session = session_factory(plan, execute=True)
        with session:
            transaction_id = f"teacher-observe-{uuid4().hex}"
            local_origin_id = f"unresolved-ekf-origin-{uuid4().hex}"
            report["clock"] = session.clock.identity()
            report["transaction_id"] = transaction_id
            report["local_origin_id"] = local_origin_id
            profile = _profile(plan)
            _json_new(directory / "profile.json", profile)
            profile_source = _source(directory / "profile.json", artifact_root)
            capture = TeacherTelemetryCapture(
                directory / "telemetry",
                reader=session.raw_receive,
                clock=session.clock.monotonic,
                clock_id=session.clock.session_id,
                transaction_id=transaction_id,
                local_origin_id=local_origin_id,
                max_records=maximum_records,
            )
            initial_deadline = session.clock.monotonic() + timeout
            while session.clock.monotonic() < initial_deadline:
                capture.receive(min(0.2, max(0, initial_deadline - session.clock.monotonic())))
                row = capture.latest_message(
                    "HEARTBEAT", now_s=session.clock.monotonic(), maximum_age_s=2.0
                )
                if row is not None:
                    report["initial_accepted_heartbeat"] = row
                    break
            else:
                raise ValueError("No persisted source-admitted HEARTBEAT; UDP peer not established")
            report["stream_requests"] = _request_observation_streams(session, directory)
            report["telemetry_stream_request_API_calls"] = len(REQUIRED_MESSAGES) + len(
                EXTRA_MESSAGES
            )
            report["camera"] = []
            for index in range(plan.frame_count):
                camera_directory = directory / f"camera-{index:03d}"
                camera_directory.mkdir()
                observation = session.camera.read()
                report["camera"].append(_persist_camera(camera_directory, observation))
                report["camera_frames_recorded"] += 1
            report["external_SIM_observed_here"] = not injected
            camera_at_s = observation.host_return_stamp.value_ns / 1e9
            deadline = session.clock.monotonic() + timeout
            attempt = 0
            next_attempt = 0.0
            last_rows = {}
            while session.clock.monotonic() < deadline:
                capture.receive(min(0.2, max(0, deadline - session.clock.monotonic())))
                now = session.clock.monotonic()
                last_rows = {
                    name: capture.latest_message(name, now_s=now, maximum_age_s=2.0)
                    for name in REQUIRED_MESSAGES
                }
                report["latest_present_message_types"] = sorted(
                    name for name, row in last_rows.items() if row is not None
                )
                if any(row is None for row in last_rows.values()) or now < next_attempt:
                    continue
                _verify_source(runtime_source, artifact_root)
                resources = _resources(
                    plan,
                    profile,
                    session,
                    camera_at_s,
                    now_s=now,
                    transaction_id=transaction_id,
                    runtime=runtime,
                )
                resource_path = directory / f"resources-{attempt:03d}.json"
                _json_new(resource_path, resources)
                attempt += 1
                next_attempt = now + 0.5
                try:
                    context = context_builder(
                        last_rows,
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
                    if not 0 <= after - context.context.captured_at_s <= 2.0:
                        raise ValueError("Qualified context became stale during source reads")
                    binding = _json_new(
                        directory / f"observed-context-{attempt:03d}.json", context.to_mapping()
                    )
                    attestation = session.attest_qualified(telemetry_qualified=True).to_mapping()
                    _verify_source(runtime_source, artifact_root)
                    final = session.clock.monotonic()
                    if not 0 <= final - context.context.captured_at_s <= 2.0:
                        raise ValueError(
                            "Qualified context became stale during persistence/attestation"
                        )
                except (ValueError, TypeError) as error:
                    report["context_error"] = f"{type(error).__name__}: {error}"[:2048]
                    continue
                report["context"] = binding
                report["context_qualified"] = True
                report["context_qualified_at_observer_s"] = final
                report["context_error"] = None
                report["attestation"] = attestation
                break
            report["diagnostic_rows"] = {
                name: capture.latest_message(
                    name, now_s=session.clock.monotonic(), maximum_age_s=10.0
                )
                for name in (*REQUIRED_MESSAGES, *EXTRA_MESSAGES)
            }
            report["raw_telemetry"] = capture.seal()
            capture = None
            report["status"] = (
                "observed-context-qualified" if report["context_qualified"] else "context-refused"
            )
            if not report["context_qualified"] and report["context_error"] is None:
                report["context_error"] = (
                    "Required source-bound fresh telemetry missing before deadline"
                )
    except BaseException as error:
        report["status"] = "observation-failed"
        report["error"] = f"{type(error).__name__}: {error}"[:2048]
        if capture is not None:
            try:
                report["raw_telemetry"] = capture.abort("Observation failed; no downstream flight")
            except BaseException as close_error:
                report["capture_close_error"] = f"{type(close_error).__name__}: {close_error}"[
                    :2048
                ]
    finally:
        if session is not None:
            try:
                report["session"] = session.to_mapping()
            except BaseException as report_error:
                report["status"] = "observation-failed"
                report["session_report_error"] = f"{type(report_error).__name__}: {report_error}"[
                    :2048
                ]
        report["receipt"] = _json_new(directory / "observation.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--execute", action="store_true", help="observe isolated software SIM ONLY, never fly"
    )
    parser.add_argument("--context-timeout-s", type=float, default=30)
    args = parser.parse_args(argv)
    from scripts.onboard_teacher_gazebo_session import build_teacher_session_plan

    output = args.output_directory.resolve()
    plan = build_teacher_session_plan(
        args.scenario, output.parent / f"{output.name}-job", frame_count=2
    )
    report = observe_teacher(
        plan, output, execute=args.execute, context_timeout_s=args.context_timeout_s
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
