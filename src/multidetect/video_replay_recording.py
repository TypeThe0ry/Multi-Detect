"""Convert a recorded video into an integrity-bound raw replay recording.

The detector replay contract consumes encoded image references and a monotonic
telemetry timeline.  Field bring-up and offline evaluation often start with a
video file, though, so this module provides one deterministic conversion path
instead of requiring operators to hand-author ``events.jsonl``.  The generated
telemetry is deliberately camera-only: link/position health is explicit and
GPS is unknown unless a separate raw-sensor recording is supplied.  This keeps
the output useful for detector/replay plumbing without inventing GNSS or
deployment evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .autonomy.serialization import canonical_json, canonical_sha256, require_int, require_sha256
from .backends import RawSensorReplayRecording
from .compat import UTC
from .deployment_recording_attestation import (
    PLACEHOLDER_START_TIME_UTC,
    derive_deployment_recording_time_window,
)
from .product_version import load_product_version

DEFAULT_AIRFRAME_PROFILE_HASH = "0" * 64
DEFAULT_START_TIME_UTC = "1970-01-01T00:00:00Z"


def create_video_replay_recording(
    video: str | Path,
    output: str | Path,
    *,
    recording_id: str | None = None,
    airframe_profile_hash: str = DEFAULT_AIRFRAME_PROFILE_HASH,
    started_at_utc: str | datetime = DEFAULT_START_TIME_UTC,
    fps: float | None = None,
    frame_stride: int = 1,
    frame_limit: int | None = None,
    expected_source_video_sha256: str | None = None,
    expected_source_video_bytes: int | None = None,
    source_evidence_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a hash-bound raw replay directory from ``video``.

    ``output`` must not already exist.  Frames are decoded and re-encoded as
    PNG below ``output/frames``; the event stream and manifest are then loaded
    again through :class:`RawSensorReplayRecording` before the directory is
    atomically moved into place.  A missing/invalid container FPS is a hard
    error unless the caller supplies ``fps`` explicitly.

    The result is camera-only evidence.  Every telemetry sample carries
    ``position_healthy=false`` and omits GPS fields, so downstream navigation
    and target-geolocation gates remain closed until an independent qualified
    MAVLink/raw-sensor recording is joined.
    """

    video_path = Path(video).expanduser().resolve(strict=True)
    if not video_path.is_file():
        raise ValueError(f"video must be a regular file: {video_path}")
    output_path = Path(output).expanduser().resolve()
    if output_path.exists():
        raise FileExistsError(f"output already exists; choose a new path: {output_path}")
    if output_path == video_path:
        raise ValueError("video and output must be different paths")

    if not isinstance(frame_stride, int) or isinstance(frame_stride, bool) or frame_stride <= 0:
        raise ValueError("frame_stride must be a positive integer")
    if frame_limit is not None and (
        not isinstance(frame_limit, int) or isinstance(frame_limit, bool) or frame_limit <= 0
    ):
        raise ValueError("frame_limit must be a positive integer when supplied")
    effective_fps_override = _positive_finite(fps, "fps") if fps is not None else None
    normalized_airframe = require_sha256(airframe_profile_hash, "airframe_profile_hash")
    expected_source_hash = (
        require_sha256(expected_source_video_sha256, "expected_source_video_sha256")
        if expected_source_video_sha256 is not None
        else None
    )
    normalized_expected_bytes = (
        require_int(expected_source_video_bytes, "expected_source_video_bytes", minimum=1)
        if expected_source_video_bytes is not None
        else None
    )
    if source_evidence_binding is not None and not isinstance(source_evidence_binding, Mapping):
        raise ValueError("source_evidence_binding must be an object")
    normalized_source_binding = (
        json.loads(canonical_json(source_evidence_binding))
        if source_evidence_binding is not None
        else None
    )
    source_video_sha256, source_video_bytes = _sha256_file(video_path)
    if expected_source_hash is not None and source_video_sha256 != expected_source_hash:
        raise ValueError("source video SHA-256 does not match its expected evidence binding")
    if normalized_expected_bytes is not None and source_video_bytes != normalized_expected_bytes:
        raise ValueError("source video byte count does not match its expected evidence binding")
    normalized_start = _format_utc(_parse_utc(started_at_utc))
    normalized_recording_id = video_path.stem if recording_id is None else recording_id
    if not isinstance(normalized_recording_id, str) or not normalized_recording_id.strip():
        raise ValueError("recording_id must be non-empty text")
    normalized_recording_id = normalized_recording_id.strip()

    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - optional vision dependency
        raise RuntimeError(
            "video replay recording requires OpenCV; install the repository vision extra"
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(
        tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=str(output_path.parent))
    )
    capture = None
    try:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise ValueError(f"OpenCV cannot open video: {video_path}")
        container_fps = float(capture.get(cv2.CAP_PROP_FPS))
        if effective_fps_override is None:
            if not math.isfinite(container_fps) or container_fps <= 0.0:
                raise ValueError(
                    "video container FPS is unavailable; supply an explicit positive --fps"
                )
            effective_fps = container_fps
            fps_source = "container"
        else:
            effective_fps = effective_fps_override
            fps_source = "override"

        frames_path = temporary_path / "frames"
        frames_path.mkdir()
        events: list[dict[str, object]] = []
        frame_count = 0
        decoded_frame_count = 0
        width: int | None = None
        height: int | None = None
        while frame_limit is None or frame_count < frame_limit:
            ok, image = capture.read()
            if not ok:
                break
            source_index = decoded_frame_count
            decoded_frame_count += 1
            if source_index % frame_stride:
                continue
            if image is None or getattr(image, "ndim", None) != 3:
                raise ValueError(f"video frame {source_index} is not a color image")
            observed_height = int(image.shape[0])
            observed_width = int(image.shape[1])
            if observed_width <= 0 or observed_height <= 0:
                raise ValueError(f"video frame {source_index} has invalid dimensions")
            if width is None:
                width, height = observed_width, observed_height
            elif (observed_width, observed_height) != (width, height):
                raise ValueError("video frames do not share one stable width/height")

            encoded_ok, encoded = cv2.imencode(".png", image)
            if not encoded_ok:
                raise RuntimeError(f"OpenCV failed to encode video frame {source_index}")
            payload = encoded.tobytes()
            frame_id = f"video-frame-{frame_count:06d}"
            image_name = f"frame-{frame_count:06d}.png"
            (frames_path / image_name).write_bytes(payload)
            captured_at_s = source_index / effective_fps
            # Keep telemetry and frame timestamps equal and ordered.  The
            # replay backend accepts equal event timestamps while requiring
            # each individual stream to increase strictly.
            events.append(
                {
                    "schema_version": 1,
                    "kind": "telemetry",
                    "observed_at_s": captured_at_s,
                    "telemetry": _camera_only_telemetry(),
                }
            )
            events.append(
                {
                    "schema_version": 1,
                    "kind": "frame",
                    "frame_id": frame_id,
                    "captured_at_s": captured_at_s,
                    "image": {
                        "path": f"frames/{image_name}",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "bytes": len(payload),
                        "width": width,
                        "height": height,
                        "encoding": "png",
                    },
                }
            )
            frame_count += 1
        if width is None or height is None or not events:
            raise ValueError("video did not yield any decodable frames")
        final_source_sha256, final_source_bytes = _sha256_file(video_path)
        if (final_source_sha256, final_source_bytes) != (
            source_video_sha256,
            source_video_bytes,
        ):
            raise ValueError("source video changed while replay frames were being decoded")
    except BaseException:
        _remove_staging_path(temporary_path)
        raise
    finally:
        if capture is not None:
            capture.release()

    event_payload = "".join(
        json.dumps(item, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        for item in events
    ).encode("utf-8")
    events_path = temporary_path / "events.jsonl"
    events_path.write_bytes(event_payload)
    event_sha256 = hashlib.sha256(event_payload).hexdigest()
    recording = RawSensorReplayRecording.from_jsonl(
        events_path,
        recording_id=normalized_recording_id,
        airframe_profile_hash=normalized_airframe,
        started_at_utc=normalized_start,
        expected_sha256=event_sha256,
        expected_byte_count=len(event_payload),
    )
    manifest_payload = json.dumps(
        {
            "schema_version": 1,
            "recording_id": recording.recording_id,
            "airframe_profile_hash": recording.airframe_profile_hash,
            "started_at_utc": normalized_start,
            "recording_hash": recording.recording_hash,
            "events": {
                "path": "events.jsonl",
                "sha256": event_sha256,
                "bytes": len(event_payload),
            },
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_path = temporary_path / "recording.manifest.json"
    manifest_path.write_bytes(manifest_payload)
    # Load the manifest a second time so the exact on-disk contract is what is
    # returned, not merely the in-memory event list.
    verified = RawSensorReplayRecording.from_manifest(manifest_path)
    if verified.recording_hash != recording.recording_hash:
        raise RuntimeError("video replay manifest changed the verified recording identity")
    attestation_window = derive_deployment_recording_time_window(temporary_path)

    product = load_product_version()
    final_manifest_path = output_path / "recording.manifest.json"
    summary = {
        "event": "video_replay_recording_created",
        "schema_version": 1,
        "product": product.product,
        "release_label": product.release_label,
        "python_version": product.python,
        "source_video": str(video_path),
        "source_video_sha256": source_video_sha256,
        "source_video_bytes": source_video_bytes,
        "recording_id": recording.recording_id,
        "airframe_profile_hash": recording.airframe_profile_hash,
        "started_at_utc": normalized_start,
        "recording_manifest": str(final_manifest_path),
        "recording_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "recording_hash": recording.recording_hash,
        "events_sha256": event_sha256,
        "events_bytes": len(event_payload),
        "frame_count": len(verified.frames),
        "decoded_frame_count": decoded_frame_count,
        "frame_stride": frame_stride,
        "frame_limit": frame_limit,
        "fps": effective_fps,
        "fps_source": fps_source,
        "width": width,
        "height": height,
        "telemetry_mode": "camera_only_unknown_navigation",
        "position_healthy": False,
        "gps_bound": False,
        "ground_truth_present": False,
        "predictions_present": False,
        "software_only": True,
        "deployment_domain_evidence_complete": False,
        "accuracy_validated": False,
        "gate_passed": False,
        "source_attestation_handoff": {
            "status": "operator_confirmation_required",
            "attestation_created": False,
            "recording_root": str(output_path),
            "started_at_utc": attestation_window["started_at_utc"],
            "minimum_ended_at_utc": attestation_window["minimum_ended_at_utc"],
            "latest_event_at_s": attestation_window["latest_event_at_s"],
            "time_anchor_is_placeholder": (
                attestation_window["started_at_utc"] == PLACEHOLDER_START_TIME_UTC
            ),
            "attestation_time_eligible": (
                attestation_window["started_at_utc"] != PLACEHOLDER_START_TIME_UTC
            ),
            "derive_times_flag": "--derive-times-from-recording",
            "required_operator_fields": [
                "stratum",
                "captured_by",
                "capture_device_id",
                "capture_site",
                "source_type",
            ],
        },
        "notes": [
            "Video pixels are hash-bound raw replay input; no detector output is embedded.",
            (
                "GPS/position is explicitly unknown and must be joined from an independent "
                "raw-sensor recording."
            ),
            (
                "This recording alone cannot close deployment, geolocation, HIL, REAL or "
                "final release gates."
            ),
        ],
    }
    if normalized_source_binding is not None:
        summary["source_evidence_binding"] = normalized_source_binding
        summary["summary_canonical_hash"] = canonical_sha256(summary)
    (temporary_path / "recording-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary_path, output_path)
    return summary


def _camera_only_telemetry() -> dict[str, object]:
    return {
        "altitude_agl_m": 0.0,
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "ground_speed_mps": 0.0,
        "in_allowed_zone": None,
        "geofence_healthy": None,
        "position_healthy": False,
        "link_healthy": True,
        "flight_mode_allows_deploy": False,
        "release_zone_clear": None,
        "flight_mode": "HOLD",
        "armed": False,
    }


def _positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("started_at_utc must be RFC3339 UTC") from exc
    else:
        raise ValueError("started_at_utc must be RFC3339 UTC")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("started_at_utc must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("started_at_utc must use UTC")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _remove_staging_path(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


__all__ = [
    "DEFAULT_AIRFRAME_PROFILE_HASH",
    "DEFAULT_START_TIME_UTC",
    "create_video_replay_recording",
]
