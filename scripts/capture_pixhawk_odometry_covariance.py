#!/usr/bin/env python3
"""Capture source-bound MAVLink ODOMETRY attitude covariance evidence.

The tool may transmit only MAV_CMD_SET_MESSAGE_INTERVAL requests on the selected
companion link.  It never sends a flight-mode, mission, parameter-write, or
actuator command.  SIM/HIL/REAL use the same Pixhawk telemetry provider and the
same report contract; the selected runtime mode changes claims, not parsing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from multidetect.autonomy.serialization import canonical_sha256  # noqa: E402
from multidetect.mission_planning import load_executable_mission_document  # noqa: E402
from multidetect.pixhawk import (  # noqa: E402
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)
from multidetect.px4_mission_feedback_cli import (  # noqa: E402
    load_px4_mission_feedback_observation,
)

SCHEMA = "multidetect.pixhawk-odometry-covariance-observation/v2"
UNCERTAINTY_SOURCE = "mavlink_odometry_pose_covariance_local_ned_with_configured_floor"
_LOCAL_UDP = re.compile(r"udpin:(?:0\.0\.0\.0|127\.0\.0\.1):(\d{1,5})")
_UDP_ENDPOINT = re.compile(r"(?:udp|udpin|udpout):[^:]*:(\d{1,5})")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_HEARTBEAT_IDENTITY_FIELDS = (
    "source_system_id",
    "source_component_id",
    "autopilot_id",
    "vehicle_type_id",
    "mavlink_version",
)


class ObservationEvidenceError(ValueError):
    """Raised when stored covariance evidence cannot be reproduced exactly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_new(path: Path, document: Mapping[str, object]) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"refusing to overwrite evidence: {target}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _finite_nonnegative(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0.0
    )


def _endpoint_valid(runtime_mode: str, endpoint: str) -> bool:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return False
    local_match = _LOCAL_UDP.fullmatch(endpoint)
    if runtime_mode == "sim":
        return local_match is not None and int(local_match.group(1)) != 14_550
    if runtime_mode not in {"hil", "real"}:
        return False
    udp_match = _UDP_ENDPOINT.fullmatch(endpoint)
    return udp_match is None or int(udp_match.group(1)) != 14_550


def _mode_binding_paths(raw: object) -> tuple[Path, Path, Path]:
    if not isinstance(raw, Mapping):
        raise ObservationEvidenceError("HIL/REAL mode_binding must be an object")
    paths: list[Path] = []
    for field in ("mission", "mission_feedback", "runtime_profile"):
        source = raw.get(field)
        if not isinstance(source, Mapping):
            raise ObservationEvidenceError(f"mode_binding.{field} must be an object")
        value = source.get("path")
        if not isinstance(value, str) or not value.strip():
            raise ObservationEvidenceError(f"mode_binding.{field}.path is missing")
        paths.append(Path(value))
    return paths[0], paths[1], paths[2]


def build_mode_binding(
    *,
    runtime_mode: str,
    endpoint: str,
    baud: int,
    diagnostics: Mapping[str, object],
    mission_path: Path,
    mission_feedback_path: Path,
    runtime_profile_path: Path,
) -> dict[str, object]:
    """Bind a HIL/REAL covariance observation to existing Mission evidence."""

    if runtime_mode not in {"hil", "real"}:
        raise ObservationEvidenceError("mode binding is reserved for HIL/REAL")
    if not _endpoint_valid(runtime_mode, endpoint):
        raise ObservationEvidenceError("endpoint is invalid for HIL/REAL mode binding")
    if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
        raise ObservationEvidenceError("baud must be a positive integer")
    resolved_mission = mission_path.expanduser().resolve(strict=True)
    resolved_feedback = mission_feedback_path.expanduser().resolve(strict=True)
    resolved_profile = runtime_profile_path.expanduser().resolve(strict=True)
    mission = load_executable_mission_document(resolved_mission)
    feedback = load_px4_mission_feedback_observation(
        resolved_feedback,
        expected_mission=mission,
        runtime_profile_path=resolved_profile,
    )
    runtime_binding = feedback.get("runtime_binding")
    feedback_transport = feedback.get("transport")
    heartbeat = diagnostics.get("heartbeat_identity")
    autopilot_version = diagnostics.get("autopilot_version_identity")
    qualification = diagnostics.get("qualification")
    if not isinstance(runtime_binding, Mapping):
        runtime_binding = {}
    if not isinstance(feedback_transport, Mapping):
        feedback_transport = {}
    if not isinstance(heartbeat, Mapping):
        heartbeat = {}
    if not isinstance(autopilot_version, Mapping):
        autopilot_version = {}
    if not isinstance(qualification, Mapping):
        qualification = {}
    expected_heartbeat = runtime_binding.get("heartbeat_identity")
    if not isinstance(expected_heartbeat, Mapping):
        expected_heartbeat = {}
    observed_identity = runtime_binding.get("observed_identity")
    if not isinstance(observed_identity, Mapping):
        observed_identity = {}

    heartbeat_matches = all(
        heartbeat.get(field) == expected_heartbeat.get(field) and heartbeat.get(field) is not None
        for field in _HEARTBEAT_IDENTITY_FIELDS
    )
    current_firmware_hash = autopilot_version.get("identity_hash")
    expected_firmware_hash = observed_identity.get("firmware_identity_hash")
    checks = {
        "feedback_schema_v2": feedback.get("schema_version") == 2,
        "feedback_runtime_mode_exact": feedback.get("runtime_mode") == runtime_mode,
        "feedback_gate_passed": feedback.get("feedback_gate_passed") is True,
        "runtime_binding_gate_passed": runtime_binding.get("gate_passed") is True,
        "runtime_output_locked": runtime_binding.get("output_locked") is True,
        "runtime_flight_control_disabled": (runtime_binding.get("flight_control_enabled") is False),
        "configured_endpoint_exact": (
            feedback_transport.get("configured_endpoint") == endpoint
            and diagnostics.get("configured_endpoint") == endpoint
        ),
        "resolved_endpoint_exact": (
            feedback_transport.get("resolved_endpoint") == diagnostics.get("resolved_endpoint")
            and diagnostics.get("resolved_endpoint") is not None
        ),
        "baud_exact": feedback_transport.get("baud") == baud,
        "heartbeat_identity_exact": heartbeat_matches,
        "current_firmware_identity_exact": (
            isinstance(current_firmware_hash, str)
            and current_firmware_hash == expected_firmware_hash
        ),
        "provider_read_only": diagnostics.get("read_only") is True,
        "provider_hardware_control_disabled": (
            diagnostics.get("hardware_control_enabled") is False
        ),
        "provider_transport_link_current": (diagnostics.get("transport_link_healthy") is True),
        "provider_qualified_link_current": (diagnostics.get("qualified_link_healthy") is True),
        "provider_operational_qualification": (
            qualification.get("required") is True
            and qualification.get("passed") is True
            and qualification.get("reasons") in ((), [])
        ),
    }
    binding: dict[str, object] = {
        "schema_version": 1,
        "runtime_mode": runtime_mode,
        "mission": {
            "path": str(resolved_mission),
            "file_sha256": _sha256(resolved_mission),
            "mission_id": mission.mission_id,
            "mission_sha256": mission.canonical_hash,
            "airframe_profile_hash": mission.airframe_profile_hash,
        },
        "mission_feedback": {
            "path": str(resolved_feedback),
            "file_sha256": _sha256(resolved_feedback),
            "evidence_sha256": feedback.get("evidence_sha256"),
            "runtime_binding_sha256": runtime_binding.get("binding_sha256"),
        },
        "runtime_profile": {
            "path": str(resolved_profile),
            "file_sha256": _sha256(resolved_profile),
            "profile_document_sha256": runtime_binding.get("profile_document_sha256"),
            "profile_sha256": runtime_binding.get("profile_sha256"),
            "profile_id": (
                runtime_binding.get("profile", {}).get("profile_id")
                if isinstance(runtime_binding.get("profile"), Mapping)
                else None
            ),
            "key_id": runtime_binding.get("key_id"),
        },
        "current_heartbeat_identity": {
            field: heartbeat.get(field) for field in _HEARTBEAT_IDENTITY_FIELDS
        },
        "current_firmware_identity_hash": current_firmware_hash,
        "checks": checks,
        "gate_passed": all(checks.values()),
    }
    binding["binding_sha256"] = canonical_sha256(binding)
    return binding


def _record_from_diagnostics(
    diagnostics: Mapping[str, object],
    *,
    sample_index: int,
) -> dict[str, object] | None:
    raw = diagnostics.get("attitude_uncertainty")
    if not isinstance(raw, Mapping) or raw.get("source") != UNCERTAINTY_SOURCE:
        return None
    covariance = raw.get("raw_angular_covariance_rad2")
    if not isinstance(covariance, Sequence) or isinstance(covariance, (str, bytes, bytearray)):
        return None
    return {
        "sample_index": sample_index,
        "accepted_count": raw.get("accepted_count"),
        "observed_at_s": raw.get("observed_at_s"),
        "age_s": raw.get("age_s"),
        "associated_with_attitude": raw.get("associated_with_attitude"),
        "source": raw.get("source"),
        "child_frame_id": raw.get("child_frame_id"),
        "reset_counter": raw.get("reset_counter"),
        "roll_sigma_deg": raw.get("roll_sigma_deg"),
        "pitch_sigma_deg": raw.get("pitch_sigma_deg"),
        "heading_sigma_deg": raw.get("heading_sigma_deg"),
        "raw_angular_covariance_rad2": list(covariance),
    }


def build_observation_report(
    *,
    runtime_mode: str,
    endpoint: str,
    started_at_utc: str,
    finished_at_utc: str,
    duration_s: float,
    samples_requested: int,
    records: Sequence[Mapping[str, object]],
    diagnostics: Mapping[str, object],
    minimum_odometry_messages: int,
    minimum_accepted_covariances: int,
    baud: int = 57_600,
    mode_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    message_counts = diagnostics.get("message_type_counts")
    uncertainty = diagnostics.get("attitude_uncertainty")
    requests = diagnostics.get("ranging_stream_requests")
    identity_requests = diagnostics.get("autopilot_version_requests")
    autopilot_version = diagnostics.get("autopilot_version_identity")
    qualification = diagnostics.get("qualification")
    if not isinstance(message_counts, Mapping):
        message_counts = {}
    if not isinstance(uncertainty, Mapping):
        uncertainty = {}
    if not isinstance(requests, Mapping):
        requests = {}
    if not isinstance(identity_requests, Mapping):
        identity_requests = {}
    if not isinstance(autopilot_version, Mapping):
        autopilot_version = {}
    if not isinstance(qualification, Mapping):
        qualification = {}
    requested_intervals = requests.get("requested_message_intervals_us")
    if not isinstance(requested_intervals, Mapping):
        requested_intervals = {}
    attempts = requests.get("attempts")
    attempts = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
    identity_attempts = identity_requests.get("attempts")
    identity_attempts = (
        identity_attempts
        if isinstance(identity_attempts, int) and not isinstance(identity_attempts, bool)
        else 0
    )
    transmitted = diagnostics.get("messages_transmitted")
    transmitted = (
        transmitted if isinstance(transmitted, int) and not isinstance(transmitted, bool) else -1
    )
    odometry_count = message_counts.get("ODOMETRY", 0)
    odometry_count = (
        odometry_count
        if isinstance(odometry_count, int) and not isinstance(odometry_count, bool)
        else 0
    )
    accepted_count = uncertainty.get("accepted_count", 0)
    accepted_count = (
        accepted_count
        if isinstance(accepted_count, int) and not isinstance(accepted_count, bool)
        else 0
    )

    records_valid = bool(records) and all(
        record.get("source") == UNCERTAINTY_SOURCE
        and record.get("associated_with_attitude") is True
        and record.get("child_frame_id") in {1, 12}
        and all(
            _finite_positive(record.get(field))
            for field in ("roll_sigma_deg", "pitch_sigma_deg", "heading_sigma_deg")
        )
        and isinstance(record.get("raw_angular_covariance_rad2"), Sequence)
        and len(record["raw_angular_covariance_rad2"]) == 9  # type: ignore[arg-type]
        and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in record["raw_angular_covariance_rad2"]  # type: ignore[union-attr]
        )
        and all(
            float(record["raw_angular_covariance_rad2"][index]) > 0.0  # type: ignore[index]
            for index in (0, 4, 8)
        )
        for record in records
    )
    checks = {
        "runtime_mode_valid": runtime_mode in {"sim", "hil", "real"},
        "endpoint_policy": _endpoint_valid(runtime_mode, endpoint),
        "transport_link_current": diagnostics.get("transport_link_healthy") is True,
        "qualified_link_current": (
            diagnostics.get("qualified_link_healthy") is True
            and qualification.get("required") is True
            and qualification.get("passed") is True
            and qualification.get("reasons") in ((), [])
        ),
        "odometry_message_count": odometry_count >= minimum_odometry_messages,
        "accepted_covariance_count": accepted_count >= minimum_accepted_covariances,
        "captured_records_valid": records_valid,
        "latest_covariance_attitude_associated": (
            uncertainty.get("associated_with_attitude") is True
        ),
        "latest_covariance_source_exact": uncertainty.get("source") == UNCERTAINTY_SOURCE,
        "odometry_interval_requested": requested_intervals.get("ODOMETRY") == 50_000,
        "autopilot_version_identity_observed": (
            identity_requests.get("enabled") is True
            and 1 <= identity_attempts <= 3
            and identity_requests.get("command_id") == 512
            and identity_requests.get("message_id") == 148
            and identity_requests.get("response_observed") is True
            and isinstance(autopilot_version.get("identity_hash"), str)
            and _SHA256.fullmatch(str(autopilot_version.get("identity_hash"))) is not None
        ),
        "autopilot_version_current_session": (
            _finite_nonnegative(diagnostics.get("autopilot_version_observed_at_s"))
            and _finite_nonnegative(diagnostics.get("autopilot_version_age_s"))
            and float(diagnostics["autopilot_version_age_s"]) <= duration_s + 1.0
        ),
        "telemetry_requests_only": (
            attempts > 0
            and transmitted == attempts * len(requested_intervals) + identity_attempts
            and diagnostics.get("hardware_control_enabled") is False
        ),
        "runtime_identity_binding": (
            mode_binding is None
            if runtime_mode == "sim"
            else (
                isinstance(mode_binding, Mapping)
                and mode_binding.get("runtime_mode") == runtime_mode
                and mode_binding.get("gate_passed") is True
            )
        ),
    }
    parity_path = ROOT / "configs" / "release-parity.json"
    source_path = Path(__file__).resolve()
    provider_path = ROOT / "src" / "multidetect" / "pixhawk.py"
    without_hash: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": 2,
        "release_status": "0.3.0-pre.8",
        "runtime_mode": runtime_mode,
        "endpoint": endpoint,
        "baud": baud,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "duration_s": duration_s,
        "samples_requested": samples_requested,
        "minimum_odometry_messages": minimum_odometry_messages,
        "minimum_accepted_covariances": minimum_accepted_covariances,
        "odometry_messages_observed": odometry_count,
        "accepted_covariances_observed": accepted_count,
        "records": list(records),
        "final_diagnostics": dict(diagnostics),
        "mode_binding": None if mode_binding is None else dict(mode_binding),
        "checks": checks,
        "source_binding": {
            "capture_script": str(source_path.relative_to(ROOT)).replace("\\", "/"),
            "capture_script_sha256": _sha256(source_path),
            "provider": str(provider_path.relative_to(ROOT)).replace("\\", "/"),
            "provider_sha256": _sha256(provider_path),
            "release_parity_path": str(parity_path.relative_to(ROOT)).replace("\\", "/"),
            "release_parity_sha256": _sha256(parity_path),
            "release_parity_hashes": json.loads(parity_path.read_text(encoding="utf-8"))["parity"],
        },
        "claims": {
            "estimator_attitude_covariance_observed": bool(checks["captured_records_valid"]),
            "sim_acceptance": (
                "passed" if runtime_mode == "sim" and all(checks.values()) else "not_claimed"
            ),
            "hil_covariance_observation": (
                "passed" if runtime_mode == "hil" and all(checks.values()) else "not_claimed"
            ),
            "real_covariance_observation": (
                "passed" if runtime_mode == "real" and all(checks.values()) else "not_claimed"
            ),
            "hil_acceptance": "not_claimed",
            "real_acceptance": "not_claimed",
            "hardware_acceptance": "not_claimed",
            "flight_acceptance": "not_claimed",
        },
        "gate_passed": all(checks.values()),
    }
    without_hash["evidence_sha256"] = canonical_sha256(without_hash)
    return without_hash


def _required_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ObservationEvidenceError(f"{key} must be an integer")
    return value


def _required_float(document: Mapping[str, object], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationEvidenceError(f"{key} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ObservationEvidenceError(f"{key} must be finite")
    return converted


def verify_observation_report(document: Mapping[str, object]) -> dict[str, object]:
    """Rebuild and verify a stored observation against the current source tree.

    The verifier rejects a report whose canonical digest was merely recomputed
    after editing: every derived check, claim, count, and source binding is
    rebuilt from the stored raw records and diagnostics before exact comparison.
    """

    if document.get("schema") != SCHEMA or document.get("schema_version") != 2:
        raise ObservationEvidenceError("unsupported evidence schema")
    stored_hash = document.get("evidence_sha256")
    if not isinstance(stored_hash, str):
        raise ObservationEvidenceError("evidence_sha256 must be a string")
    unhashed = {key: value for key, value in document.items() if key != "evidence_sha256"}
    if stored_hash != canonical_sha256(unhashed):
        raise ObservationEvidenceError("evidence_sha256 mismatch")

    runtime_mode = document.get("runtime_mode")
    endpoint = document.get("endpoint")
    started_at_utc = document.get("started_at_utc")
    finished_at_utc = document.get("finished_at_utc")
    records = document.get("records")
    diagnostics = document.get("final_diagnostics")
    if runtime_mode not in {"sim", "hil", "real"}:
        raise ObservationEvidenceError("runtime_mode is invalid")
    if not isinstance(endpoint, str):
        raise ObservationEvidenceError("endpoint must be a string")
    if not _endpoint_valid(str(runtime_mode), endpoint):
        raise ObservationEvidenceError("endpoint is invalid for the runtime mode")
    if not isinstance(started_at_utc, str) or not isinstance(finished_at_utc, str):
        raise ObservationEvidenceError("observation timestamps must be strings")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes, bytearray)):
        raise ObservationEvidenceError("records must be an array")
    if not all(isinstance(record, Mapping) for record in records):
        raise ObservationEvidenceError("every record must be an object")
    if not isinstance(diagnostics, Mapping):
        raise ObservationEvidenceError("final_diagnostics must be an object")

    mode_binding = document.get("mode_binding")
    rebuilt_mode_binding: Mapping[str, object] | None = None
    if runtime_mode == "sim":
        if mode_binding is not None:
            raise ObservationEvidenceError("SIM evidence cannot carry a HIL/REAL mode binding")
    else:
        mission_path, feedback_path, profile_path = _mode_binding_paths(mode_binding)
        rebuilt_mode_binding = build_mode_binding(
            runtime_mode=runtime_mode,
            endpoint=endpoint,
            baud=_required_int(document, "baud"),
            diagnostics=diagnostics,
            mission_path=mission_path,
            mission_feedback_path=feedback_path,
            runtime_profile_path=profile_path,
        )

    rebuilt = build_observation_report(
        runtime_mode=runtime_mode,
        endpoint=endpoint,
        started_at_utc=started_at_utc,
        finished_at_utc=finished_at_utc,
        duration_s=_required_float(document, "duration_s"),
        samples_requested=_required_int(document, "samples_requested"),
        records=records,
        diagnostics=diagnostics,
        minimum_odometry_messages=_required_int(document, "minimum_odometry_messages"),
        minimum_accepted_covariances=_required_int(document, "minimum_accepted_covariances"),
        baud=_required_int(document, "baud"),
        mode_binding=rebuilt_mode_binding,
    )
    if dict(document) != rebuilt:
        differing = sorted(
            key for key in set(document) | set(rebuilt) if document.get(key) != rebuilt.get(key)
        )
        raise ObservationEvidenceError(
            "evidence does not reproduce from raw observations: " + ", ".join(differing)
        )
    return {
        "schema": SCHEMA,
        "verified": True,
        "gate_passed": rebuilt["gate_passed"],
        "runtime_mode": runtime_mode,
        "records_verified": len(records),
        "evidence_sha256": stored_hash,
    }


def verify_observation_file(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationEvidenceError(f"cannot read evidence: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ObservationEvidenceError("evidence root must be an object")
    return verify_observation_report(document)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--runtime-mode", choices=("sim", "hil", "real"))
    parser.add_argument("--baud", type=int)
    parser.add_argument("--mission", type=Path)
    parser.add_argument("--mission-feedback-observation", type=Path)
    parser.add_argument("--runtime-profile", type=Path)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--interval-seconds", type=float, default=0.1)
    parser.add_argument("--minimum-odometry-messages", type=int, default=20)
    parser.add_argument("--minimum-accepted-covariances", type=int, default=10)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verify is not None:
        capture_only = (
            args.endpoint,
            args.runtime_mode,
            args.baud,
            args.mission,
            args.mission_feedback_observation,
            args.runtime_profile,
            args.out,
        )
        if any(item is not None for item in capture_only):
            raise ValueError("--verify cannot be combined with capture arguments")
        verification = verify_observation_file(args.verify)
        print(json.dumps(verification, ensure_ascii=False, allow_nan=False, indent=2))
        return 0 if verification["gate_passed"] else 1
    if args.endpoint is None or args.runtime_mode is None or args.out is None:
        raise ValueError("capture requires --endpoint, --runtime-mode, and --out")
    baud = 57_600 if args.baud is None else args.baud
    if not _endpoint_valid(args.runtime_mode, args.endpoint):
        raise ValueError("endpoint is invalid for the selected runtime mode")
    if isinstance(baud, bool) or not isinstance(baud, int) or baud <= 0:
        raise ValueError("baud must be a positive integer")
    mode_sources = (args.mission, args.mission_feedback_observation, args.runtime_profile)
    if args.runtime_mode == "sim" and any(item is not None for item in mode_sources):
        raise ValueError("SIM capture cannot carry HIL/REAL Mission identity sources")
    if args.runtime_mode in {"hil", "real"} and any(item is None for item in mode_sources):
        raise ValueError(
            "HIL/REAL capture requires --mission, --mission-feedback-observation, "
            "and --runtime-profile"
        )
    if args.samples <= 0:
        raise ValueError("samples must be positive")
    if not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0.0:
        raise ValueError("interval-seconds must be finite and positive")
    if args.minimum_odometry_messages <= 0 or args.minimum_accepted_covariances <= 0:
        raise ValueError("minimum counts must be positive")

    provider = PixhawkReadOnlyTelemetryProvider(
        PixhawkReadOnlyConfig(
            args.endpoint,
            baud=baud,
            expected_system_id=1,
            expected_autopilot_id=12,
            require_operational_state=args.runtime_mode in {"hil", "real"},
            request_ranging_streams=True,
            request_autopilot_version=True,
        )
    )
    records: list[dict[str, object]] = []
    last_accepted_count = 0
    started_at_utc = _utc_now()
    started = time.monotonic()
    try:
        for sample_index in range(args.samples):
            now_s = time.monotonic()
            provider.snapshot(now_s=now_s)
            diagnostics = provider.diagnostics(now_s=now_s)
            record = _record_from_diagnostics(diagnostics, sample_index=sample_index)
            if record is not None:
                accepted_count = record.get("accepted_count")
                if isinstance(accepted_count, int) and accepted_count > last_accepted_count:
                    records.append(record)
                    last_accepted_count = accepted_count
            if sample_index + 1 < args.samples:
                time.sleep(args.interval_seconds)
        finished = time.monotonic()
        final_diagnostics = provider.diagnostics(now_s=finished)
    finally:
        provider.close()
    mode_binding = None
    if args.runtime_mode in {"hil", "real"}:
        if any(item is None for item in mode_sources):  # Defensive guard for optimized Python.
            raise ValueError("HIL/REAL identity sources are unavailable")
        mode_binding = build_mode_binding(
            runtime_mode=args.runtime_mode,
            endpoint=args.endpoint,
            baud=baud,
            diagnostics=final_diagnostics,
            mission_path=args.mission,
            mission_feedback_path=args.mission_feedback_observation,
            runtime_profile_path=args.runtime_profile,
        )
    report = build_observation_report(
        runtime_mode=args.runtime_mode,
        endpoint=args.endpoint,
        started_at_utc=started_at_utc,
        finished_at_utc=_utc_now(),
        duration_s=finished - started,
        samples_requested=args.samples,
        records=records,
        diagnostics=final_diagnostics,
        minimum_odometry_messages=args.minimum_odometry_messages,
        minimum_accepted_covariances=args.minimum_accepted_covariances,
        baud=baud,
        mode_binding=mode_binding,
    )
    _atomic_write_new(args.out, report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
