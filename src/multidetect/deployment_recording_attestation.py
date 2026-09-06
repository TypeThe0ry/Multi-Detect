"""Integrity-bound human attestation for one physical field recording source."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .backends import RawSensorReplayRecording
from .compat import UTC
from .replay_set import REPLAY_SET_STRATA

ATTESTATION_NAME = "deployment-recording-source.json"
SCHEMA_VERSION = 1
SOURCE_TYPES = ("local_camera", "rtsp_camera")
PLACEHOLDER_START_TIME_UTC = "1970-01-01T00:00:00Z"


def create_deployment_recording_attestation(
    recording_root: str | Path,
    *,
    stratum: str,
    captured_by: str,
    capture_device_id: str,
    capture_site: str,
    source_type: str,
    started_at_utc: str,
    ended_at_utc: str,
) -> dict[str, Any]:
    """Create, but do not write, one physical-field source attestation."""

    root = _recording_root(recording_root)
    normalized_stratum = _stratum(stratum)
    normalized_source = require_text(source_type, "source_type").lower()
    if normalized_source not in SOURCE_TYPES:
        raise AutonomyContractError(
            f"source_type must be one of: {', '.join(SOURCE_TYPES)}"
        )
    started = _utc(started_at_utc, "started_at_utc")
    ended = _utc(ended_at_utc, "ended_at_utc")
    _require_non_placeholder_start(started)
    if ended <= started:
        raise AutonomyContractError("ended_at_utc must be later than started_at_utc")

    manifest_path = root / "recording.manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AutonomyContractError("recording.manifest.json must be a regular non-symlink file")
    payload = manifest_path.read_bytes()
    recording = RawSensorReplayRecording.from_manifest(manifest_path)
    if recording.started_at_utc != started:
        raise AutonomyContractError(
            "attested started_at_utc must match recording manifest started_at_utc"
        )
    latest_event_at_s = _latest_event_at_s(recording)
    if ended < started + timedelta(seconds=latest_event_at_s):
        raise AutonomyContractError(
            "ended_at_utc must cover the recording's latest event timestamp"
        )

    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event": "deployment_recording_source_attested",
        "stratum": normalized_stratum,
        "capture_origin": "physical_field",
        "source_type": normalized_source,
        "captured_by": require_text(captured_by, "captured_by"),
        "capture_device_id": require_text(capture_device_id, "capture_device_id"),
        "capture_site": require_text(capture_site, "capture_site"),
        "started_at_utc": _format_utc(started),
        "ended_at_utc": _format_utc(ended),
        "recording_manifest": {
            "path": "recording.manifest.json",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "recording_hash": recording.recording_hash,
        },
        "claims": {
            "hardware_observed": True,
            "synthetic": False,
            "simulation": False,
            "screen_replay": False,
            "deployment_source_attested": True,
            "release_ready": False,
        },
    }
    document["canonical_hash"] = canonical_sha256(document)
    return document


def write_deployment_recording_attestation(
    recording_root: str | Path,
    **values: Any,
) -> dict[str, Any]:
    """Atomically write a non-overwriting attestation beside its recording."""

    root = _recording_root(recording_root)
    output = root / ATTESTATION_NAME
    if output.exists() or output.is_symlink():
        raise AutonomyContractError(f"deployment recording attestation already exists: {output}")
    document = create_deployment_recording_attestation(root, **values)
    payload = (
        json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return verify_deployment_recording_attestation(root, expected_stratum=values["stratum"])


def derive_deployment_recording_time_window(
    recording_root: str | Path,
) -> dict[str, object]:
    """Derive the exact manifest start and minimum valid end without asserting provenance."""

    root = _recording_root(recording_root)
    manifest = root / "recording.manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise AutonomyContractError("recording.manifest.json must be a regular non-symlink file")
    recording = RawSensorReplayRecording.from_manifest(manifest)
    latest_event_at_s = _latest_event_at_s(recording)
    return {
        "event": "deployment_recording_time_window_derived",
        "started_at_utc": _format_utc(recording.started_at_utc),
        "minimum_ended_at_utc": _format_utc(
            recording.started_at_utc + timedelta(seconds=latest_event_at_s)
        ),
        "latest_event_at_s": latest_event_at_s,
        "provenance_attested": False,
        "operator_confirmation_required": True,
    }


def verify_deployment_recording_attestation(
    recording_root: str | Path,
    *,
    expected_stratum: str | None = None,
) -> dict[str, Any]:
    """Reopen an attestation and its exact recording manifest fail-closed."""

    root = _recording_root(recording_root)
    path = root / ATTESTATION_NAME
    if path.is_symlink() or not path.is_file():
        raise AutonomyContractError(
            f"deployment recording source requires {ATTESTATION_NAME}"
        )
    raw = strict_json_loads(path.read_bytes(), name="deployment recording attestation")
    value = strict_object(
        raw,
        name="DeploymentRecordingAttestation",
        required=frozenset(
            {
                "schema_version",
                "event",
                "stratum",
                "capture_origin",
                "source_type",
                "captured_by",
                "capture_device_id",
                "capture_site",
                "started_at_utc",
                "ended_at_utc",
                "recording_manifest",
                "claims",
                "canonical_hash",
            }
        ),
    )
    require_schema_version(value["schema_version"], expected=SCHEMA_VERSION)
    if value["event"] != "deployment_recording_source_attested":
        raise AutonomyContractError("deployment recording attestation event is invalid")
    stratum = _stratum(value["stratum"])
    if expected_stratum is not None and stratum != _stratum(expected_stratum):
        raise AutonomyContractError("deployment recording attestation stratum does not match")
    if value["capture_origin"] != "physical_field":
        raise AutonomyContractError("capture_origin must be physical_field")
    source_type = require_text(value["source_type"], "source_type").lower()
    if source_type not in SOURCE_TYPES:
        raise AutonomyContractError("deployment recording source_type is invalid")
    captured_by = require_text(value["captured_by"], "captured_by")
    capture_device_id = require_text(value["capture_device_id"], "capture_device_id")
    capture_site = require_text(value["capture_site"], "capture_site")
    started = _utc(value["started_at_utc"], "started_at_utc")
    ended = _utc(value["ended_at_utc"], "ended_at_utc")
    _require_non_placeholder_start(started)
    if ended <= started:
        raise AutonomyContractError("ended_at_utc must be later than started_at_utc")

    binding = strict_object(
        value["recording_manifest"],
        name="recording_manifest",
        required=frozenset({"path", "sha256", "bytes", "recording_hash"}),
    )
    if binding["path"] != "recording.manifest.json":
        raise AutonomyContractError("attested recording manifest path is invalid")
    manifest = root / "recording.manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise AutonomyContractError("attested recording manifest must be a regular file")
    manifest_payload = manifest.read_bytes()
    if len(manifest_payload) != require_int(
        binding["bytes"], "recording_manifest.bytes", minimum=1
    ):
        raise AutonomyContractError("attested recording manifest byte count changed")
    if hashlib.sha256(manifest_payload).hexdigest() != require_sha256(
        binding["sha256"], "recording_manifest.sha256"
    ):
        raise AutonomyContractError("attested recording manifest SHA-256 changed")
    recording = RawSensorReplayRecording.from_manifest(manifest)
    if recording.recording_hash != require_sha256(
        binding["recording_hash"], "recording_manifest.recording_hash"
    ):
        raise AutonomyContractError("attested recording hash changed")
    if recording.started_at_utc != started:
        raise AutonomyContractError(
            "attested started_at_utc differs from recording manifest"
        )
    latest_event_at_s = _latest_event_at_s(recording)
    if ended < started + timedelta(seconds=latest_event_at_s):
        raise AutonomyContractError(
            "attested ended_at_utc does not cover the recording's latest event"
        )

    claims = strict_object(
        value["claims"],
        name="claims",
        required=frozenset(
            {
                "hardware_observed",
                "synthetic",
                "simulation",
                "screen_replay",
                "deployment_source_attested",
                "release_ready",
            }
        ),
    )
    expected_claims = {
        "hardware_observed": True,
        "synthetic": False,
        "simulation": False,
        "screen_replay": False,
        "deployment_source_attested": True,
        "release_ready": False,
    }
    if dict(claims) != expected_claims:
        raise AutonomyContractError("deployment recording attestation claims are invalid")
    expected_hash = require_sha256(value["canonical_hash"], "canonical_hash")
    unhashed = dict(value)
    unhashed.pop("canonical_hash")
    if canonical_sha256(unhashed) != expected_hash:
        raise AutonomyContractError("deployment recording attestation canonical_hash mismatch")
    return {
        "event": "deployment_recording_source_verified",
        "stratum": stratum,
        "capture_origin": "physical_field",
        "source_type": source_type,
        "captured_by": captured_by,
        "capture_device_id": capture_device_id,
        "capture_site": capture_site,
        "started_at_utc": _format_utc(started),
        "ended_at_utc": _format_utc(ended),
        "recording_hash": recording.recording_hash,
        "canonical_hash": expected_hash,
        "release_ready": False,
    }


def _recording_root(value: str | Path) -> Path:
    requested = Path(value)
    if requested.is_symlink():
        raise AutonomyContractError("recording root must not be a symbolic link")
    root = requested.resolve(strict=True)
    if not root.is_dir():
        raise AutonomyContractError("recording root must be a directory")
    return root


def _latest_event_at_s(recording: RawSensorReplayRecording) -> float:
    return max(
        (
            *(frame.captured_at_s for frame in recording.frames),
            *(sample.observed_at_s for sample in recording.telemetry_samples),
        ),
        default=0.0,
    )


def _require_non_placeholder_start(value: datetime) -> None:
    if _format_utc(value) == PLACEHOLDER_START_TIME_UTC:
        raise AutonomyContractError(
            "deployment recording attestation rejects the placeholder 1970 UTC start; "
            "recreate the recording with its actual capture UTC"
        )


def _stratum(value: object) -> str:
    normalized = require_text(value, "stratum").lower()
    if normalized not in REPLAY_SET_STRATA:
        raise AutonomyContractError(
            f"stratum must be one of: {', '.join(REPLAY_SET_STRATA)}"
        )
    return normalized


def _utc(value: object, name: str) -> datetime:
    text = require_text(value, name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} must be RFC3339 UTC") from exc
    if result.tzinfo is None or result.utcoffset() != UTC.utcoffset(result):
        raise AutonomyContractError(f"{name} must use UTC")
    return result.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ATTESTATION_NAME",
    "PLACEHOLDER_START_TIME_UTC",
    "SCHEMA_VERSION",
    "SOURCE_TYPES",
    "create_deployment_recording_attestation",
    "derive_deployment_recording_time_window",
    "verify_deployment_recording_attestation",
    "write_deployment_recording_attestation",
]
