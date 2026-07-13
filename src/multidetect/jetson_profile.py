from __future__ import annotations

import ipaddress
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import MissionConfig
from .model_manifest import ModelManifestError, verify_model_manifest

_REQUIRED_KEYS = frozenset(
    {
        "CAMERA_SOURCE",
        "ALERT_RECEIVER_HOST",
        "ALERT_RECEIVER_PORT",
        "ALERT_HMAC_KEY",
        "ALERT_SENDER_ID",
        "ALERT_RECEIVER_ID",
        "FIRE_MODEL_PATH",
        "FIRE_MODEL_MANIFEST",
        "FIRE_MODEL_CLASS_NAMES",
        "FIRE_MODEL_OUTPUT_COORDINATES",
        "FIRE_CONFIDENCE_THRESHOLD",
        "FIRE_FLAME_CONFIDENCE_THRESHOLD",
        "FIRE_SMOKE_CONFIDENCE_THRESHOLD",
        "FIRE_CANDIDATE_STABILITY_FRAMES",
        "PIXHAWK_ENDPOINT",
        "PIXHAWK_BAUD",
        "TASK_AREA_MISSION_SEQUENCE",
    }
)
_PLACEHOLDER_MARKERS = ("REPLACE_", "USER:PASSWORD", "CAMERA_HOST", "192.0.2.1")


def load_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"environment line {line_number} has no '=' separator")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"environment line {line_number} has an invalid key")
        if key in values:
            raise ValueError(f"environment key is duplicated: {key}")
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"environment value contains a control character: {key}")
        values[key] = value.strip()
    return values


def jetson_static_preflight(
    mission_path: Path,
    environment_path: Path,
    *,
    allow_placeholders: bool = False,
    verify_model_files: bool = False,
) -> dict[str, Any]:
    """Validate deployment wiring without opening camera, ONNX runtime or MAVLink."""

    mission = MissionConfig.from_json(mission_path)
    values = load_environment_file(environment_path)
    errors: list[str] = []
    missing = sorted(_REQUIRED_KEYS - values.keys())
    errors.extend(f"missing environment key: {key}" for key in missing)
    if mission.payload_installed:
        errors.append("patrol service mission must not declare payload slots")

    camera = values.get("CAMERA_SOURCE", "")
    parsed_camera = urlsplit(camera)
    camera_scheme = parsed_camera.scheme.lower()
    camera_source_kind = camera_scheme if camera_scheme in {"rtsp", "rtsps"} else "invalid"
    if camera_source_kind == "invalid" or not parsed_camera.hostname:
        errors.append("CAMERA_SOURCE must be an RTSP URI with a host")
    camera_credentials_present = parsed_camera.username is not None

    alert_host = values.get("ALERT_RECEIVER_HOST", "")
    if not _valid_host(alert_host, allow_placeholders=allow_placeholders):
        errors.append("ALERT_RECEIVER_HOST is invalid")
    alert_port = _integer(values.get("ALERT_RECEIVER_PORT"), "ALERT_RECEIVER_PORT", errors)
    if alert_port is not None and not 1 <= alert_port <= 65535:
        errors.append("ALERT_RECEIVER_PORT must be in [1, 65535]")
    sender_id = values.get("ALERT_SENDER_ID", "").strip()
    receiver_id = values.get("ALERT_RECEIVER_ID", "").strip()
    if not sender_id or not receiver_id or sender_id == receiver_id:
        errors.append("alert sender/receiver identities must be non-empty and distinct")
    hmac_key = values.get("ALERT_HMAC_KEY", "")
    hmac_placeholder = _contains_placeholder(hmac_key)
    if not allow_placeholders and (hmac_placeholder or len(hmac_key.encode("utf-8")) < 32):
        errors.append("ALERT_HMAC_KEY must be a non-placeholder secret of at least 32 bytes")

    class_names = tuple(
        item.strip().lower()
        for item in values.get("FIRE_MODEL_CLASS_NAMES", "").split(",")
        if item.strip()
    )
    if len(class_names) < 2 or len(class_names) != len(set(class_names)):
        errors.append("FIRE_MODEL_CLASS_NAMES must contain unique comma-separated labels")
    output_coordinates = values.get("FIRE_MODEL_OUTPUT_COORDINATES", "")
    if output_coordinates not in {"letterbox_xyxy_px", "normalized_xyxy"}:
        errors.append("FIRE_MODEL_OUTPUT_COORDINATES is unsupported")
    model_path = Path(values.get("FIRE_MODEL_PATH", ""))
    manifest_path = Path(values.get("FIRE_MODEL_MANIFEST", ""))
    if model_path.suffix.lower() != ".onnx":
        errors.append("FIRE_MODEL_PATH must name an ONNX file")
    if manifest_path.suffix.lower() != ".json":
        errors.append("FIRE_MODEL_MANIFEST must name a JSON file")
    confidence_floor = _real(
        values.get("FIRE_CONFIDENCE_THRESHOLD"),
        "FIRE_CONFIDENCE_THRESHOLD",
        errors,
    )
    flame_threshold = _real(
        values.get("FIRE_FLAME_CONFIDENCE_THRESHOLD"),
        "FIRE_FLAME_CONFIDENCE_THRESHOLD",
        errors,
    )
    smoke_threshold = _real(
        values.get("FIRE_SMOKE_CONFIDENCE_THRESHOLD"),
        "FIRE_SMOKE_CONFIDENCE_THRESHOLD",
        errors,
    )
    for name, threshold in (
        ("FIRE_CONFIDENCE_THRESHOLD", confidence_floor),
        ("FIRE_FLAME_CONFIDENCE_THRESHOLD", flame_threshold),
        ("FIRE_SMOKE_CONFIDENCE_THRESHOLD", smoke_threshold),
    ):
        if threshold is not None and not 0.0 <= threshold <= 1.0:
            errors.append(f"{name} must be in [0, 1]")
    for name, threshold in (
        ("FIRE_FLAME_CONFIDENCE_THRESHOLD", flame_threshold),
        ("FIRE_SMOKE_CONFIDENCE_THRESHOLD", smoke_threshold),
    ):
        if threshold is not None and confidence_floor is not None and threshold < confidence_floor:
            errors.append(f"{name} cannot be below FIRE_CONFIDENCE_THRESHOLD")
        if threshold is not None and threshold > mission.minimum_confidence:
            errors.append(f"{name} cannot exceed the mission minimum_confidence")
    stability_frames = _integer(
        values.get("FIRE_CANDIDATE_STABILITY_FRAMES"),
        "FIRE_CANDIDATE_STABILITY_FRAMES",
        errors,
    )
    if stability_frames is not None and stability_frames < mission.minimum_track_observations:
        errors.append(
            "FIRE_CANDIDATE_STABILITY_FRAMES cannot be below mission minimum_track_observations"
        )

    endpoint = values.get("PIXHAWK_ENDPOINT", "")
    endpoint_kind = _endpoint_kind(endpoint)
    if endpoint_kind == "invalid":
        errors.append("PIXHAWK_ENDPOINT must be a serial device or udp:/tcp: endpoint")
    baud = _integer(values.get("PIXHAWK_BAUD"), "PIXHAWK_BAUD", errors)
    if baud is not None and baud <= 0:
        errors.append("PIXHAWK_BAUD must be positive")
    sequence = _integer(
        values.get("TASK_AREA_MISSION_SEQUENCE"),
        "TASK_AREA_MISSION_SEQUENCE",
        errors,
    )
    if sequence is not None and sequence < 0:
        errors.append("TASK_AREA_MISSION_SEQUENCE cannot be negative")

    model_verified = False
    production_approved = False
    if verify_model_files and not errors:
        try:
            verified = verify_model_manifest(
                manifest_path,
                model_path,
                expected_class_names=class_names,
                expected_output_coordinates=output_coordinates,
                expected_model_role="fire_candidate",
                require_production_approved=True,
            )
        except (ModelManifestError, OSError, ValueError) as exc:
            errors.append(f"model manifest verification failed: {exc}")
        else:
            model_verified = True
            production_approved = verified.production_approved

    return {
        "event": "jetson_static_preflight",
        "valid": not errors,
        "errors": errors,
        "mission_id": mission.mission_id,
        "mission_capability": "patrol_only" if not mission.payload_installed else "payload",
        "camera_source_kind": camera_source_kind,
        "camera_credentials_present": camera_credentials_present,
        "camera_source_redacted": True,
        "alert_hmac_configured": bool(hmac_key) and not hmac_placeholder,
        "alert_secret_redacted": True,
        "model_class_names": class_names,
        "model_output_coordinates": output_coordinates,
        "candidate_confidence_floor": confidence_floor,
        "flame_candidate_threshold": flame_threshold,
        "smoke_candidate_threshold": smoke_threshold,
        "candidate_stability_frames": stability_frames,
        "mission_minimum_confidence": mission.minimum_confidence,
        "model_files_verified": model_verified,
        "model_production_approved": production_approved,
        "pixhawk_endpoint_kind": endpoint_kind,
        "pixhawk_read_only": True,
        "provider_fallback_order": [
            "TensorrtExecutionProvider",
            "CUDAExecutionProvider",
            "CPUExecutionProvider",
        ],
        "camera_opened": False,
        "model_loaded": False,
        "pixhawk_opened": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }


def _integer(value: str | None, name: str, errors: list[str]) -> int | None:
    try:
        return int(value or "")
    except ValueError:
        errors.append(f"{name} must be an integer")
        return None


def _real(value: str | None, name: str, errors: list[str]) -> float | None:
    try:
        parsed = float(value or "")
    except ValueError:
        errors.append(f"{name} must be a finite number")
        return None
    if not math.isfinite(parsed):
        errors.append(f"{name} must be a finite number")
        return None
    return parsed


def _valid_host(value: str, *, allow_placeholders: bool) -> bool:
    if allow_placeholders and _contains_placeholder(value):
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value))


def _endpoint_kind(value: str) -> str:
    if value.startswith("/dev/") or re.fullmatch(r"(?i)COM\d+", value):
        return "serial"
    if re.fullmatch(r"(?i)(?:udp|tcp):[^:]+:\d+", value):
        return "network"
    return "invalid"


def _contains_placeholder(value: str) -> bool:
    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


__all__ = ["jetson_static_preflight", "load_environment_file"]
