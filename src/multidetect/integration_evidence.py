from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .compat import UTC

INTEGRATION_EVIDENCE_SCHEMA_VERSION = 1

INTEGRATION_PROFILES: dict[str, tuple[str, ...]] = {
    "software_hil": ("software_hil",),
    "vision_bench": ("software_hil", "rtsp_camera", "jetson"),
    "airframe_bench": (
        "software_hil",
        "rtsp_camera",
        "jetson",
        "pixhawk_v6x",
        "gr01",
    ),
    "inert_payload_bench": (
        "software_hil",
        "rtsp_camera",
        "jetson",
        "pixhawk_v6x",
        "gr01",
        "inert_payload",
    ),
}

_EXPECTED_EVENTS: dict[str, str | frozenset[str]] = {
    "software_hil": "combined_flight_stack_software_hil_passed",
    "rtsp_camera": "rtsp_camera_bench_passed",
    # Accept the original Nano-specific event for existing schema-v1 evidence bundles.
    "jetson": frozenset({"jetson_orin_bench_passed", "jetson_orin_nano_bench_passed"}),
    "pixhawk_v6x": "pixhawk_v6x_bench_passed",
    "gr01": "gr01_bench_passed",
    "inert_payload": "inert_payload_hardware_bench_passed",
}


def check_integration_evidence_bundle(
    bundle_path: str | Path,
    *,
    profile: str,
    now: datetime | None = None,
    maximum_hardware_age_hours: float = 168.0,
) -> dict[str, Any]:
    """Verify hashed integration artifacts without treating HIL as hardware evidence."""

    if profile not in INTEGRATION_PROFILES:
        raise ValueError(f"unknown integration evidence profile: {profile}")
    if not math.isfinite(maximum_hardware_age_hours) or maximum_hardware_age_hours <= 0:
        raise ValueError("maximum hardware evidence age must be finite and positive")
    checked_at = _utc(now or datetime.now(UTC))
    path = Path(bundle_path).resolve()
    bundle = _load_object(path, description="integration evidence bundle")
    _require_exact_int(bundle, "schema_version", INTEGRATION_EVIDENCE_SCHEMA_VERSION)
    bundle_id = _require_string(bundle, "bundle_id")
    aircraft_id = _require_string(bundle, "aircraft_id")
    records = bundle.get("records")
    if not isinstance(records, Mapping):
        raise ValueError("integration evidence bundle records must be an object")

    required_gates = INTEGRATION_PROFILES[profile]
    results: dict[str, dict[str, Any]] = {}
    for gate in required_gates:
        raw_record = records.get(gate)
        if not isinstance(raw_record, Mapping):
            results[gate] = _gate_result(False, ("required evidence record is missing",))
            continue
        results[gate] = _check_record(
            gate,
            raw_record,
            bundle_directory=path.parent,
            checked_at=checked_at,
            maximum_hardware_age_hours=maximum_hardware_age_hours,
        )
    passed = all(result["passed"] for result in results.values())
    return {
        "event": "integration_evidence_bundle_checked",
        "bundle_path": str(path),
        "bundle_id": bundle_id,
        "aircraft_id": aircraft_id,
        "profile": profile,
        "required_gates": list(required_gates),
        "gates": results,
        "passed": passed,
        "hardware_gate_count": sum(gate != "software_hil" for gate in required_gates),
        "software_hil_cannot_satisfy_hardware_gates": True,
        "production_approved": False,
        "physical_release_approved": False,
    }


def _check_record(
    gate: str,
    record: Mapping[str, Any],
    *,
    bundle_directory: Path,
    checked_at: datetime,
    maximum_hardware_age_hours: float,
) -> dict[str, Any]:
    reasons: list[str] = []
    artifact_value = record.get("artifact")
    expected_sha256 = record.get("sha256")
    if not isinstance(artifact_value, str) or not artifact_value.strip():
        return _gate_result(False, ("artifact path is missing",))
    if not _is_sha256(expected_sha256):
        return _gate_result(False, ("artifact SHA-256 is missing or invalid",))
    artifact_path = Path(artifact_value)
    if not artifact_path.is_absolute():
        artifact_path = bundle_directory / artifact_path
    artifact_path = artifact_path.resolve()
    if not artifact_path.is_file():
        return _gate_result(False, ("artifact file does not exist",), artifact_path)
    actual_sha256 = _sha256(artifact_path)
    if actual_sha256.lower() != expected_sha256.lower():
        reasons.append("artifact SHA-256 does not match")
    try:
        artifact = _load_object(artifact_path, description=f"{gate} artifact")
    except ValueError as exc:
        reasons.append(str(exc))
        return _gate_result(False, reasons, artifact_path, actual_sha256)
    expected_events = _EXPECTED_EVENTS[gate]
    event_matches = (
        artifact.get("event") == expected_events
        if isinstance(expected_events, str)
        else artifact.get("event") in expected_events
    )
    if not event_matches:
        reasons.append("artifact event does not match the requested gate")
    validator = _GATE_VALIDATORS[gate]
    reasons.extend(validator(artifact))
    if gate != "software_hil":
        reasons.extend(
            _hardware_freshness_reasons(
                artifact,
                checked_at=checked_at,
                maximum_age_hours=maximum_hardware_age_hours,
            )
        )
    return _gate_result(not reasons, reasons, artifact_path, actual_sha256)


def _software_hil_reasons(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if artifact.get("hardware_observed") is not False:
        reasons.append("software HIL must declare hardware_observed=false")
    if artifact.get("simulation_only") is not True:
        reasons.append("software HIL must declare simulation_only=true")
    if artifact.get("flight_control_enabled") is not False:
        reasons.append("software HIL enabled flight control")
    if artifact.get("physical_release_enabled") is not False:
        reasons.append("software HIL enabled physical release")
    if artifact.get("production_approved") is not False:
        reasons.append("software HIL cannot be production approved")
    pixhawk = artifact.get("pixhawk_path")
    if not isinstance(pixhawk, Mapping) or pixhawk.get("messages_transmitted_by_jetson") != 0:
        reasons.append("software HIL does not prove zero Jetson-to-Pixhawk messages")
    return tuple(reasons)


def _rtsp_reasons(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = _hardware_common_reasons(artifact)
    _require_metric(artifact, "processed_frames", minimum=300, reasons=reasons)
    _require_metric(artifact, "duration_seconds", minimum=60, reasons=reasons)
    if artifact.get("source_kind") != "rtsp":
        reasons.append("camera source is not RTSP")
    if artifact.get("resolution_stable") is not True:
        reasons.append("RTSP resolution stability is not verified")
    if artifact.get("credentials_recorded") is not False:
        reasons.append("RTSP artifact may contain credentials")
    return tuple(reasons)


def _jetson_reasons(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = _hardware_common_reasons(artifact)
    if artifact.get("device_model") not in {"Jetson Orin Nano", "Jetson Orin NX"}:
        reasons.append("device model is not a supported Jetson Orin NX/Nano")
    if artifact.get("active_inference_provider") not in {
        "TensorrtExecutionProvider",
        "CUDAExecutionProvider",
    }:
        reasons.append("Jetson GPU inference provider is not verified")
    _require_metric(artifact, "soak_duration_seconds", minimum=3600, reasons=reasons)
    _require_metric(artifact, "processed_frames", minimum=54_000, reasons=reasons)
    _require_metric(artifact, "processing_fps", minimum=15, reasons=reasons)
    _require_metric(artifact, "inference_latency_p95_ms", maximum=66.7, reasons=reasons)
    _require_metric(artifact, "capture_queue_high_watermark", maximum=1, reasons=reasons)
    if artifact.get("capture_queue_bounded") is not True:
        reasons.append("Jetson capture queue boundedness is not verified")
    _require_metric(artifact, "memory_sample_count", minimum=60, reasons=reasons)
    _require_metric(artifact, "process_rss_growth_mb", maximum=256, reasons=reasons)
    if artifact.get("memory_growth_bounded") is not True:
        reasons.append("Jetson process RSS growth boundedness is not verified")
    _require_metric(artifact, "maximum_temperature_c", maximum=95, reasons=reasons)
    return tuple(reasons)


def _pixhawk_reasons(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = _hardware_common_reasons(artifact)
    if artifact.get("hardware_model") != "Pixhawk V6X":
        reasons.append("flight-controller model is not Pixhawk V6X")
    if artifact.get("read_only") is not True:
        reasons.append("Pixhawk provider is not proven read-only")
    if artifact.get("messages_transmitted_by_jetson") != 0:
        reasons.append("Jetson transmitted messages to Pixhawk")
    if artifact.get("qgc_snapshot_fresh") is not True:
        reasons.append("QGroundControl comparison snapshot is not fresh")
    if artifact.get("qgc_field_match") is not True:
        reasons.append("QGroundControl field comparison is incomplete")
    if artifact.get("link_loss_fail_closed") is not True:
        reasons.append("Pixhawk link-loss fail-closed behavior is not verified")
    if artifact.get("link_loss_method") != "cached_staleness_without_receive":
        reasons.append("Pixhawk link-loss verification method is invalid")
    _require_metric(artifact, "sample_count", minimum=100, reasons=reasons)
    _require_metric(artifact, "fresh_sample_count", minimum=100, reasons=reasons)
    if not isinstance(artifact.get("firmware_version"), str):
        reasons.append("Pixhawk firmware version is missing")
    return tuple(reasons)


def _gr01_reasons(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = _hardware_common_reasons(artifact)
    if artifact.get("hardware_model") != "GR01":
        reasons.append("data-link model is not GR01")
    if not isinstance(artifact.get("hardware_id"), str) or not artifact["hardware_id"].strip():
        reasons.append("GR01 hardware ID is missing")
    if artifact.get("remote_is_loopback") is not False:
        reasons.append("GR01 hardware evidence used a loopback address")
    if artifact.get("bidirectional_ip_verified") is not True:
        reasons.append("GR01 bidirectional IP is not verified")
    if artifact.get("signed_operator_round_trip") is not True:
        reasons.append("signed G20 operator round-trip is not verified")
    if artifact.get("application_hmac_verified") is not True:
        reasons.append("GR01 application HMAC is not verified")
    if artifact.get("mavlink2_signature_verified") is not True:
        reasons.append("GR01 MAVLink2 signature is not verified")
    _require_metric(artifact, "requested_round_trips", minimum=100, reasons=reasons)
    _require_metric(artifact, "round_trip_samples", minimum=100, reasons=reasons)
    _require_metric(artifact, "packet_loss_rate", minimum=0, maximum=0.01, reasons=reasons)
    _require_metric(artifact, "ack_latency_p95_ms", minimum=0, maximum=500, reasons=reasons)
    return tuple(reasons)


def _payload_reasons(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    reasons = _hardware_common_reasons(artifact)
    if artifact.get("inert_load_only") is not True:
        reasons.append("payload bench did not use inert loads exclusively")
    if artifact.get("controller_and_sensor_id_separated") is not True:
        reasons.append("payload controller and confirmation sensor identities are not separated")
    if artifact.get("controller_and_sensor_key_separated") is not True:
        reasons.append("payload controller and confirmation sensor keys are not separated")
    if artifact.get("independent_confirmation_verified") is not True:
        reasons.append("independent payload confirmation is not verified")
    if artifact.get("uncertain_result_no_retry_verified") is not True:
        reasons.append("uncertain-result no-retry behavior is not verified")
    if artifact.get("people_excluded_from_test_area") is not True:
        reasons.append("payload bench test-area exclusion is not verified")
    if artifact.get("command_channel_present") is not False:
        reasons.append("payload evidence verifier exposed a command channel")
    if artifact.get("physical_release_approved") is not False:
        reasons.append("payload bench artifact cannot approve physical release")
    if artifact.get("production_approved") is not False:
        reasons.append("payload bench artifact cannot grant production approval")
    if not isinstance(artifact.get("controller_firmware_version"), str):
        reasons.append("payload controller firmware version is missing")
    _require_metric(artifact, "authenticated_controller_records", minimum=21, reasons=reasons)
    _require_metric(artifact, "authenticated_sensor_records", minimum=20, reasons=reasons)
    _require_metric(artifact, "uncertain_fault_injection_cycles", minimum=1, reasons=reasons)
    _require_metric(artifact, "confirmed_cycles", minimum=20, reasons=reasons)
    return tuple(reasons)


def _hardware_common_reasons(artifact: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    if artifact.get("hardware_observed") is not True:
        reasons.append("hardware gate requires hardware_observed=true")
    if artifact.get("simulation_only") is not False:
        reasons.append("hardware gate cannot use simulation-only evidence")
    if artifact.get("passed") is not True:
        reasons.append("hardware artifact does not declare passed=true")
    return reasons


def _hardware_freshness_reasons(
    artifact: Mapping[str, Any],
    *,
    checked_at: datetime,
    maximum_age_hours: float,
) -> tuple[str, ...]:
    raw = artifact.get("observed_at_utc")
    if not isinstance(raw, str):
        return ("hardware evidence timestamp is missing",)
    try:
        observed = _utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return ("hardware evidence timestamp is invalid",)
    age_hours = (checked_at - observed).total_seconds() / 3600.0
    if age_hours < 0:
        return ("hardware evidence timestamp is in the future",)
    if age_hours > maximum_age_hours:
        return ("hardware evidence is stale",)
    return ()


def _require_metric(
    artifact: Mapping[str, Any],
    name: str,
    *,
    reasons: list[str],
    minimum: float | None = None,
    maximum: float | None = None,
) -> None:
    value = artifact.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        reasons.append(f"metric {name} is missing or invalid")
        return
    if minimum is not None and value < minimum:
        reasons.append(f"metric {name} is below the required minimum")
    if maximum is not None and value > maximum:
        reasons.append(f"metric {name} exceeds the allowed maximum")


def _gate_result(
    passed: bool,
    reasons: tuple[str, ...] | list[str],
    artifact_path: Path | None = None,
    actual_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "passed": passed,
        "reasons": list(reasons),
        "artifact": str(artifact_path) if artifact_path is not None else None,
        "actual_sha256": actual_sha256,
    }


def _load_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} cannot be read as JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _require_exact_int(document: Mapping[str, Any], name: str, expected: int) -> None:
    value = document.get(name)
    if isinstance(value, bool) or value != expected:
        raise ValueError(f"integration evidence {name} must equal {expected}")


def _require_string(document: Mapping[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"integration evidence {name} must be a non-empty string")
    return value.strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("integration evidence time must include a timezone")
    return value.astimezone(UTC)


_GATE_VALIDATORS: dict[str, Callable[[Mapping[str, Any]], tuple[str, ...]]] = {
    "software_hil": _software_hil_reasons,
    "rtsp_camera": _rtsp_reasons,
    "jetson": _jetson_reasons,
    "pixhawk_v6x": _pixhawk_reasons,
    "gr01": _gr01_reasons,
    "inert_payload": _payload_reasons,
}


__all__ = [
    "INTEGRATION_EVIDENCE_SCHEMA_VERSION",
    "INTEGRATION_PROFILES",
    "check_integration_evidence_bundle",
]
