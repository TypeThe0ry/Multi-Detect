"""Validate and bind persisted Gazebo wire-v3 sensor frames to replay evidence.

The Docker acceptance runner persists camera bytes together with NavSat,
odometry and covariance provenance.  This module keeps that transport record
out of the target estimator itself, but provides a strict identity bridge for
offline geolocation reports: a target event may reference only a frame that
was actually sealed by the sensor manifest.  No target coordinates or truth
are inferred from this manifest.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .autonomy.serialization import canonical_sha256, strict_json_loads

_SCHEMA = "multidetect.gazebo-bgr24-frame-set"
_WIRE_VERSION = 3
_PIXEL_FORMAT = "BGR24"
_SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class SensorTransportFrame:
    """A persisted frame identity and its sealed source timing."""

    frame_id: str
    sequence: int
    file: str
    bytes: int
    sha256: str
    captured_at_s: float
    captured_at_clock: str


@dataclass(frozen=True, slots=True)
class SensorTransportManifest:
    """Hash-bound, truth-free sensor transport manifest."""

    path: Path
    file_sha256: str
    file_bytes: int
    manifest_sha256: str
    schema_version: int
    wire_version: int
    pixel_format: str
    scenario_id: str | None
    scenario_manifest_hash: str | None
    sensor_binding_hash: str | None
    frames: tuple[SensorTransportFrame, ...]

    @property
    def frame_ids(self) -> tuple[str, ...]:
        return tuple(frame.frame_id for frame in self.frames)

    def frame_for_ids(self, *identifiers: str | None) -> SensorTransportFrame | None:
        """Return the unique frame matching one of the supplied IDs."""

        matches = {
            frame.frame_id: frame
            for identifier in identifiers
            if isinstance(identifier, str)
            for frame in self.frames
            if frame.frame_id == identifier
        }
        if len(matches) > 1:
            raise ValueError("sensor transport identifiers resolve to different frames")
        return next(iter(matches.values()), None)

    def selection_mapping(self, matched_frame_ids: list[str]) -> dict[str, object]:
        """Return stable provenance suitable for an accuracy report."""

        return {
            "manifest_file_sha256": self.file_sha256,
            "manifest_file_bytes": self.file_bytes,
            "manifest_sha256": self.manifest_sha256,
            "schema_version": self.schema_version,
            "wire_version": self.wire_version,
            "pixel_format": self.pixel_format,
            "scenario_id": self.scenario_id,
            "scenario_manifest_hash": self.scenario_manifest_hash,
            "sensor_binding_hash": self.sensor_binding_hash,
            "frame_count": len(self.frames),
            "matched_frame_count": len(matched_frame_ids),
            "matched_frame_ids": list(matched_frame_ids),
            "raw_bytes_sha256_verified": True,
            "truth_free": True,
        }


def load_sensor_transport_manifest(path: str | Path) -> SensorTransportManifest:
    """Read and verify one persisted Gazebo BGR24 frame-set manifest.

    Every referenced raw frame is reopened and hashed.  The manifest's own
    canonical hash is checked with its ``manifest_sha256`` field removed.
    """

    manifest_path = Path(path).expanduser().resolve(strict=True)
    payload = manifest_path.read_bytes()
    raw = strict_json_loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("sensor transport manifest must be an object")
    if raw.get("schema") != _SCHEMA:
        raise ValueError("sensor transport manifest schema is unsupported")
    schema_version = raw.get("schema_version")
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise ValueError("sensor transport manifest schema_version is unsupported")
    if raw.get("wire_version") != _WIRE_VERSION:
        raise ValueError("sensor transport manifest must bind wire_version=3")
    if raw.get("pixel_format") != _PIXEL_FORMAT:
        raise ValueError("sensor transport manifest must bind pixel_format=BGR24")
    declared_hash = _sha256(raw.get("manifest_sha256"), "manifest_sha256")
    without_hash = dict(raw)
    without_hash.pop("manifest_sha256", None)
    if canonical_sha256(without_hash) != declared_hash:
        raise ValueError("sensor transport manifest canonical hash mismatch")

    frames_raw = raw.get("frames")
    frame_count = raw.get("frame_count")
    if type(frame_count) is not int or frame_count < 1:
        raise ValueError("sensor transport frame_count must be positive")
    if not isinstance(frames_raw, list) or len(frames_raw) != frame_count:
        raise ValueError("sensor transport frames do not match frame_count")
    frames: list[SensorTransportFrame] = []
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    for index, item in enumerate(frames_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"sensor transport frame {index} must be an object")
        frame_id = _text(item.get("frame_id"), f"frames[{index}].frame_id")
        if frame_id in seen_ids:
            raise ValueError(f"sensor transport frame_id {frame_id!r} is duplicated")
        sequence = item.get("sequence")
        if type(sequence) is not int or sequence < 0 or sequence in seen_sequences:
            raise ValueError(f"sensor transport frame {frame_id!r} has invalid sequence")
        file_name = _relative_file(item.get("file"), f"frames[{index}].file")
        byte_count = item.get("bytes")
        if type(byte_count) is not int or byte_count < 1:
            raise ValueError(f"sensor transport frame {frame_id!r} has invalid bytes")
        frame_hash = _sha256(item.get("sha256"), f"frames[{index}].sha256")
        if item.get("raw_bytes_sha256_matches_wire_payload") is not True:
            raise ValueError(f"sensor transport frame {frame_id!r} is not wire-hash verified")
        if item.get("wire_payload_sha256") != frame_hash:
            raise ValueError(f"sensor transport frame {frame_id!r} payload hash mismatch")
        provenance = item.get("provenance")
        if not isinstance(provenance, Mapping):
            raise ValueError(f"sensor transport frame {frame_id!r} lacks provenance")
        captured_at_s = provenance.get("captured_at_s")
        if isinstance(captured_at_s, bool) or not isinstance(captured_at_s, (int, float)):
            raise ValueError(f"sensor transport frame {frame_id!r} has invalid capture time")
        if not math.isfinite(float(captured_at_s)) or float(captured_at_s) < 0:
            raise ValueError(f"sensor transport frame {frame_id!r} has invalid capture time")
        for key in ("frame_telemetry", "wire_telemetry"):
            if not isinstance(provenance.get(key), Mapping):
                raise ValueError(f"sensor transport frame {frame_id!r} lacks {key}")
        frame_path = _contained_file(manifest_path.parent, file_name)
        actual = frame_path.read_bytes()
        if len(actual) != byte_count or hashlib.sha256(actual).hexdigest() != frame_hash:
            raise ValueError(f"sensor transport frame {frame_id!r} raw bytes do not match manifest")
        frames.append(
            SensorTransportFrame(
                frame_id=frame_id,
                sequence=sequence,
                file=file_name,
                bytes=byte_count,
                sha256=frame_hash,
                captured_at_s=float(captured_at_s),
                captured_at_clock=_text(
                    provenance.get("captured_at_clock"),
                    f"frames[{index}].provenance.captured_at_clock",
                ),
            )
        )
        seen_ids.add(frame_id)
        seen_sequences.add(sequence)

    per_frame = raw.get("per_frame_sensor_evidence")
    if not isinstance(per_frame, Mapping):
        raise ValueError("sensor transport manifest lacks per_frame_sensor_evidence")
    if per_frame.get("record_count") != frame_count:
        raise ValueError("sensor transport evidence record_count mismatch")
    if per_frame.get("derived_ned_values_recomputed_before_seal") is not True:
        raise ValueError("sensor transport NED values are not sealed")
    if per_frame.get("gps_and_pose_values_covered_by_wire_record_sha256") is not True:
        raise ValueError("sensor transport GPS/pose values are not wire-bound")
    attribution = raw.get("source_attribution")
    if (
        not isinstance(attribution, Mapping)
        or attribution.get("truth_loaded_by_runtime") is not False
    ):
        raise ValueError("sensor transport manifest must be truth-free")
    claims = raw.get("claims")
    if (
        not isinstance(claims, Mapping)
        or claims.get("per_frame_sensor_geolocation_input") != "passed"
    ):
        raise ValueError("sensor transport geolocation input is not qualified")

    scenario_id = raw.get("scenario_id")
    if scenario_id is not None:
        scenario_id = _text(scenario_id, "scenario_id")
    scenario_manifest_hash = raw.get("scenario_manifest_hash")
    if scenario_manifest_hash is not None:
        scenario_manifest_hash = _sha256(
            scenario_manifest_hash,
            "scenario_manifest_hash",
        )
    sensor_binding_hash = raw.get("sensor_binding_hash")
    if sensor_binding_hash is not None:
        sensor_binding_hash = _sha256(sensor_binding_hash, "sensor_binding_hash")
    return SensorTransportManifest(
        path=manifest_path,
        file_sha256=hashlib.sha256(payload).hexdigest(),
        file_bytes=len(payload),
        manifest_sha256=declared_hash,
        schema_version=schema_version,
        wire_version=_WIRE_VERSION,
        pixel_format=_PIXEL_FORMAT,
        scenario_id=scenario_id,
        scenario_manifest_hash=scenario_manifest_hash,
        sensor_binding_hash=sensor_binding_hash,
        frames=tuple(sorted(frames, key=lambda frame: frame.sequence)),
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _relative_file(value: object, name: str) -> str:
    text = _text(value, name)
    if "\\" in text or "\x00" in text:
        raise ValueError(f"{name} must be a safe POSIX relative path")
    candidate = PurePosixPath(text)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() in {"", "."}:
        raise ValueError(f"{name} must be contained")
    return candidate.as_posix()


def _contained_file(root: Path, relative: str) -> Path:
    base = root.resolve(strict=True)
    raw = base.joinpath(*PurePosixPath(relative).parts)
    if raw.is_symlink():
        raise ValueError("sensor transport frame path must not be a symlink")
    candidate = raw.resolve(strict=True)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("sensor transport frame path escapes manifest directory") from exc
    if not candidate.is_file():
        raise ValueError("sensor transport frame path is not a file")
    return candidate


__all__ = [
    "SensorTransportFrame",
    "SensorTransportManifest",
    "load_sensor_transport_manifest",
]
