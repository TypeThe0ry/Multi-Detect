"""Integrity-bound target event replay bundles.

The QGC target card intentionally stores small metadata snapshots.  This
module provides the next, heavier-weight evidence path: a local bundle can
bind a target video frame, a depth map, range/GPS evidence and the capture
time into one strict, hash-verified timeline.

The bundle is read-only evidence.  It has no mission, actuator, link or
flight-control fields and cannot be used as a control input.  Media files are
kept outside the JSONL stream and are re-hashed every time they are read so a
post-capture mutation fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .adaptive_ranging import AdaptiveRangingConfig
from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    optional_real,
    optional_text,
    require_aware_timestamp,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .evidence_session import normalize_evidence_session_id
from .mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_SATELLITES_MAX,
    MAVLINK_SATELLITES_UNKNOWN,
)

SCHEMA_VERSION = 1
TARGET_EVENT_SCHEMA = "multidetect.target-event.v1"
TARGET_EVENT_REPLAY_SCHEMA = "multidetect.target-event-replay.v1"
MAX_MEDIA_BYTES = 64 * 1024 * 1024
MAX_TOTAL_MEDIA_BYTES = 512 * 1024 * 1024
# Keep replay qualification on the same default contract as the live ranging
# policy.  The replay format deliberately has no policy object of its own;
# deriving these aliases from the canonical defaults prevents a silent drift
# when the live GPS thresholds change.  A custom live policy remains a caller
# concern, while serialized evidence always describes the product defaults.
_DEFAULT_ADAPTIVE_RANGING_CONFIG = AdaptiveRangingConfig()
GPS_QUALIFIED_FIX_TYPE = _DEFAULT_ADAPTIVE_RANGING_CONFIG.minimum_gps_fix_type
GPS_QUALIFIED_MIN_SATELLITES = _DEFAULT_ADAPTIVE_RANGING_CONFIG.minimum_gps_satellites
GPS_QUALIFIED_MAX_HORIZONTAL_ACCURACY_M = (
    _DEFAULT_ADAPTIVE_RANGING_CONFIG.maximum_gps_horizontal_accuracy_m
)
GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M = (
    _DEFAULT_ADAPTIVE_RANGING_CONFIG.maximum_gps_vertical_accuracy_m
)
# Keep this aligned with ``AdaptiveRangingConfig.maximum_gps_age_s``.  A
# replay bundle is evidence rather than a live navigation input, but a
# qualified fix still needs a bound observation-to-frame age so that a stale
# coordinate cannot be mistaken for a contemporaneous target position.
GPS_QUALIFIED_MAX_AGE_S = _DEFAULT_ADAPTIVE_RANGING_CONFIG.maximum_gps_age_s

_MEDIA_KINDS = frozenset({"video_frame", "depth_map"})
_MEDIA_ENCODINGS = frozenset({"png", "jpeg", "bmp", "npy", "raw"})
_MEDIA_EXTENSIONS = {
    "png": "png",
    "jpeg": "jpg",
    "bmp": "bmp",
    "npy": "npy",
    "raw": "bin",
}
_GPS_STATUSES = frozenset({"qualified", "unqualified", "unavailable"})
_RANGE_VALIDITIES = frozenset({"VALID", "DEGRADED", "UNAVAILABLE"})


def _strict_json_loads(payload: bytes, *, name: str) -> object:
    def reject_constant(value: str) -> None:
        raise AutonomyContractError(f"{name} contains non-finite JSON number {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AutonomyContractError(f"{name} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutonomyContractError(f"{name} is not valid UTF-8 JSON: {exc}") from exc


def _strict_relative_path(value: object, name: str) -> str:
    text = require_text(value, name)
    if "\\" in text or "\x00" in text:
        raise AutonomyContractError(f"{name} must use a safe POSIX relative path")
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or candidate.as_posix() in {"", "."}
        or ".." in candidate.parts
        or (candidate.parts and candidate.parts[0].endswith(":"))
    ):
        raise AutonomyContractError(f"{name} must be relative and contained")
    return candidate.as_posix()


def _resolve_contained_file(root: Path, relative_path: str, *, name: str) -> Path:
    base = root.resolve(strict=True)
    raw_candidate = base / Path(*PurePosixPath(relative_path).parts)
    if raw_candidate.is_symlink():
        raise AutonomyContractError(f"{name} path must not be a symlink")
    candidate = raw_candidate.resolve(strict=True)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} path escapes bundle directory") from exc
    if not candidate.is_file():
        raise AutonomyContractError(f"{name} path must name a regular file")
    return candidate


def _resolve_contained_directory(root: Path, relative_path: str, *, name: str) -> Path:
    relative = _strict_relative_path(relative_path, name)
    base = root.resolve(strict=True)
    raw_candidate = base / Path(*PurePosixPath(relative).parts)
    if raw_candidate.is_symlink():
        raise AutonomyContractError(f"{name} path must not be a symlink")
    candidate = raw_candidate.resolve(strict=True)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} path escapes bundle directory") from exc
    if not candidate.is_dir():
        raise AutonomyContractError(f"{name} path must name a directory")
    return candidate


def _read_verified_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_byte_count: int,
    name: str,
) -> bytes:
    expected_hash = require_sha256(expected_sha256, f"{name} sha256")
    expected_bytes = require_int(expected_byte_count, f"{name} bytes", minimum=1)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AutonomyContractError(f"unable to read {name}: {exc}") from exc
    if len(payload) != expected_bytes:
        raise AutonomyContractError(
            f"{name} byte count mismatch: expected {expected_bytes}, observed {len(payload)}"
        )
    observed_hash = hashlib.sha256(payload).hexdigest()
    if observed_hash != expected_hash:
        raise AutonomyContractError(
            f"{name} hash mismatch: expected {expected_hash}, observed {observed_hash}"
        )
    return payload


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _optional_real_mapping(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    return optional_real(value, name, minimum=minimum, maximum=maximum)


_GPS_FRESHNESS_VALUES = frozenset({"fresh", "stale", "unknown"})


def _validate_gps_event_timing(gps: TargetGpsEvidence, captured_at_s: float) -> float | None:
    """Bind an optional GPS observation timestamp to one target capture.

    The capture and observation clocks are monotonic seconds from the same
    session.  A future observation is impossible evidence and therefore fails
    closed.  A qualified fix older than the navigation freshness bound is
    rejected as well; an omitted timestamp remains ``unknown`` for backwards
    compatibility with v1 bundles that predate this field.
    """

    age = gps.age_at(captured_at_s)
    if gps.qualified and age is not None and age > GPS_QUALIFIED_MAX_AGE_S:
        raise AutonomyContractError(
            "qualified GPS evidence is stale at target capture "
            f"(age {age:.3f}s > {GPS_QUALIFIED_MAX_AGE_S:.3f}s)"
        )
    return age


def _validate_declared_gps_timing(
    *,
    declared_age: float | None,
    declared_age_present: bool,
    declared_freshness: str | None,
    declared_freshness_present: bool,
    event: TargetEvent,
) -> None:
    """Check optional serialized freshness fields against derived evidence."""

    if declared_age_present:
        actual_age = event.gps_age_s
        if actual_age is None:
            if declared_age is not None:
                raise AutonomyContractError(
                    "target event gps_age_s does not match GPS observation"
                )
        elif declared_age is None or not math.isclose(
            declared_age, actual_age, rel_tol=0.0, abs_tol=1.0e-9
        ):
            raise AutonomyContractError("target event gps_age_s does not match GPS observation")
    if declared_freshness_present and declared_freshness != event.gps_freshness:
        raise AutonomyContractError(
            "target event gps_freshness does not match GPS observation age"
        )


def _normalize_session_id(value: object) -> str:
    """Normalize the shared evidence-session UUID into this contract's error type."""

    try:
        return normalize_evidence_session_id(require_text(value, "session_id"))
    except (AutonomyContractError, ValueError) as exc:
        raise AutonomyContractError("session_id must be a valid UUID") from exc


@dataclass(frozen=True, slots=True)
class TargetMediaInput:
    """Raw media bytes supplied by the capture path."""

    kind: str
    payload: bytes
    encoding: str
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        kind = require_text(self.kind, "media.kind").lower()
        if kind not in _MEDIA_KINDS:
            raise AutonomyContractError("media.kind must be video_frame or depth_map")
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise AutonomyContractError("media.payload must be bytes")
        payload = bytes(self.payload)
        if not payload:
            raise AutonomyContractError("media.payload cannot be empty")
        if len(payload) > MAX_MEDIA_BYTES:
            raise AutonomyContractError("media.payload exceeds the 64 MiB per-file limit")
        object.__setattr__(self, "payload", payload)
        encoding = require_text(self.encoding, "media.encoding").lower()
        if encoding == "jpg":
            encoding = "jpeg"
        if encoding not in _MEDIA_ENCODINGS:
            raise AutonomyContractError("media.encoding is unsupported")
        object.__setattr__(self, "encoding", encoding)
        for name, value in (("media.width", self.width), ("media.height", self.height)):
            if value is not None:
                require_int(value, name, minimum=1, maximum=32_768)
        if (self.width is None) != (self.height is None):
            raise AutonomyContractError("media.width and media.height must be provided together")


@dataclass(frozen=True, slots=True)
class TargetMediaReference:
    """A hash-bound media file inside a target-event bundle."""

    kind: str
    relative_path: str
    resolved_path: Path
    sha256: str
    byte_count: int
    encoding: str
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        kind = require_text(self.kind, "media.kind").lower()
        if kind not in _MEDIA_KINDS:
            raise AutonomyContractError("media.kind must be video_frame or depth_map")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "relative_path",
            _strict_relative_path(self.relative_path, "media.path"),
        )
        source = Path(self.resolved_path)
        if not source.is_absolute() or not source.is_file():
            raise AutonomyContractError("media resolved_path must be an existing absolute file")
        object.__setattr__(self, "resolved_path", source.resolve(strict=True))
        object.__setattr__(self, "sha256", require_sha256(self.sha256, "media.sha256"))
        object.__setattr__(
            self,
            "byte_count",
            require_int(self.byte_count, "media.bytes", minimum=1, maximum=MAX_MEDIA_BYTES),
        )
        encoding = require_text(self.encoding, "media.encoding").lower()
        if encoding == "jpg":
            encoding = "jpeg"
        if encoding not in _MEDIA_ENCODINGS:
            raise AutonomyContractError("media.encoding is unsupported")
        object.__setattr__(self, "encoding", encoding)
        for name, value in (("media.width", self.width), ("media.height", self.height)):
            if value is not None:
                require_int(value, name, minimum=1, maximum=32_768)
        if (self.width is None) != (self.height is None):
            raise AutonomyContractError("media.width and media.height must be provided together")
        self.read_verified_bytes()

    def read_verified_bytes(self) -> bytes:
        return _read_verified_file(
            self.resolved_path,
            expected_sha256=self.sha256,
            expected_byte_count=self.byte_count,
            name=f"media {self.relative_path}",
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "path": self.relative_path,
            "sha256": self.sha256,
            "bytes": self.byte_count,
            "encoding": self.encoding,
        }
        if self.width is not None:
            result["width"] = self.width
            result["height"] = self.height
        return result

    @classmethod
    def from_mapping(
        cls,
        raw: object,
        *,
        asset_root: Path,
    ) -> TargetMediaReference:
        value = strict_object(
            raw,
            name="TargetMediaReference",
            required=frozenset({"kind", "path", "sha256", "bytes", "encoding"}),
            optional=frozenset({"width", "height"}),
        )
        relative_path = _strict_relative_path(value["path"], "media.path")
        return cls(
            kind=require_text(value["kind"], "media.kind"),
            relative_path=relative_path,
            resolved_path=_resolve_contained_file(asset_root, relative_path, name="media"),
            sha256=require_sha256(value["sha256"], "media.sha256"),
            byte_count=require_int(value["bytes"], "media.bytes", minimum=1),
            encoding=require_text(value["encoding"], "media.encoding"),
            width=(
                require_int(value["width"], "media.width", minimum=1, maximum=32_768)
                if "width" in value
                else None
            ),
            height=(
                require_int(value["height"], "media.height", minimum=1, maximum=32_768)
                if "height" in value
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetGpsEvidence:
    """GPS quality and target WGS-84 estimate for one event."""

    status: str = "unavailable"
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    horizontal_sigma_m: float | None = None
    fix_type: int | None = None
    satellites_visible: int | None = None
    horizontal_accuracy_m: float | None = None
    vertical_accuracy_m: float | None = None
    # Monotonic timestamp from the same capture clock as the enclosing target
    # event.  It is optional for legacy bundles; when present it is checked
    # against the event capture time and contributes an explicit freshness
    # result to the event mapping.
    observed_at_s: float | None = None

    def __post_init__(self) -> None:
        status = require_text(self.status, "gps.status").lower()
        if status not in _GPS_STATUSES:
            raise AutonomyContractError("gps.status must be qualified, unqualified, or unavailable")
        object.__setattr__(self, "status", status)
        latitude = _optional_real_mapping(
            self.latitude_deg, "gps.latitude_deg", minimum=-90.0, maximum=90.0
        )
        longitude = _optional_real_mapping(
            self.longitude_deg, "gps.longitude_deg", minimum=-180.0, maximum=180.0
        )
        sigma = _optional_real_mapping(
            self.horizontal_sigma_m, "gps.horizontal_sigma_m", minimum=0.0, maximum=100_000.0
        )
        object.__setattr__(self, "latitude_deg", latitude)
        object.__setattr__(self, "longitude_deg", longitude)
        object.__setattr__(self, "horizontal_sigma_m", sigma)
        if (latitude is None) != (longitude is None):
            raise AutonomyContractError("gps latitude and longitude must be provided together")
        if self.fix_type is not None:
            object.__setattr__(
                self,
                "fix_type",
                require_int(
                    self.fix_type,
                    "gps.fix_type",
                    minimum=0,
                    maximum=MAVLINK_GPS_FIX_TYPE_MAX,
                ),
            )
        if self.satellites_visible is not None:
            object.__setattr__(
                self,
                "satellites_visible",
                require_int(
                    self.satellites_visible,
                    "gps.satellites_visible",
                    minimum=0,
                    maximum=MAVLINK_SATELLITES_UNKNOWN,
                ),
            )
        for attribute, name in (
            ("horizontal_accuracy_m", "gps.horizontal_accuracy_m"),
            ("vertical_accuracy_m", "gps.vertical_accuracy_m"),
        ):
            value = getattr(self, attribute)
            if value is not None:
                object.__setattr__(
                    self,
                    attribute,
                    require_real(value, name, minimum=0.0, maximum=100_000.0),
                )
        observed_at = _optional_real_mapping(
            self.observed_at_s, "gps.observed_at_s", minimum=0.0
        )
        object.__setattr__(self, "observed_at_s", observed_at)
        if self.status == "unavailable" and any(
            value is not None
            for value in (
                latitude,
                longitude,
                sigma,
                self.fix_type,
                self.satellites_visible,
                self.horizontal_accuracy_m,
                self.vertical_accuracy_m,
            )
        ):
            raise AutonomyContractError("unavailable GPS evidence cannot contain measurements")
        if self.status == "qualified":
            # ``status=qualified`` is the replay representation of a healthy
            # global-position sample.  The live contract carries that bit as
            # ``VehicleTelemetry.position_healthy``; replay bundles predate a
            # separate field, so keeping the status as the single claim
            # preserves the v1 shape while enforcing every numeric quality
            # bound below.
            if (
                latitude is None
                or longitude is None
                or sigma is None
                # Match the coordinate domain enforced by
                # ``assess_gps_quality``; replay evidence must not widen the
                # live publisher's accepted latitude range.
                or not -90.0 < latitude < 90.0
                or not -180.0 <= longitude <= 180.0
                or self.fix_type is None
                or self.fix_type < GPS_QUALIFIED_FIX_TYPE
                or self.fix_type > MAVLINK_GPS_FIX_TYPE_MAX
                or self.satellites_visible is None
                or not GPS_QUALIFIED_MIN_SATELLITES
                <= self.satellites_visible
                <= MAVLINK_SATELLITES_MAX
                or self.horizontal_accuracy_m is None
                or self.horizontal_accuracy_m > GPS_QUALIFIED_MAX_HORIZONTAL_ACCURACY_M
                or self.vertical_accuracy_m is None
                or self.vertical_accuracy_m > GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M
            ):
                raise AutonomyContractError("qualified GPS evidence does not meet the quality gate")

    @property
    def qualified(self) -> bool:
        return self.status == "qualified"

    def age_at(self, captured_at_s: float) -> float | None:
        """Return GPS age at a target capture, rejecting future samples.

        ``captured_at_s`` and ``observed_at_s`` are deliberately monotonic
        values from one session clock.  Returning ``None`` for a missing
        timestamp keeps old v1 bundles readable, while a timestamp that would
        require time travel is a contract violation.
        """

        captured = require_real(captured_at_s, "captured_at_s", minimum=0.0)
        if self.observed_at_s is None:
            return None
        age = captured - self.observed_at_s
        if age < 0.0:
            raise AutonomyContractError("gps observation cannot be newer than target capture")
        return age

    def freshness_at(self, captured_at_s: float) -> str:
        """Classify the observation age as fresh, stale, or unknown."""

        age = self.age_at(captured_at_s)
        if age is None:
            return "unknown"
        return "fresh" if age <= GPS_QUALIFIED_MAX_AGE_S else "stale"

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "horizontal_sigma_m": self.horizontal_sigma_m,
            "fix_type": self.fix_type,
            "satellites_visible": self.satellites_visible,
            "horizontal_accuracy_m": self.horizontal_accuracy_m,
            "vertical_accuracy_m": self.vertical_accuracy_m,
            "observed_at_s": self.observed_at_s,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> TargetGpsEvidence:
        value = strict_object(
            raw,
            name="TargetGpsEvidence",
            required=frozenset(
                {
                    "status",
                    "latitude_deg",
                    "longitude_deg",
                    "horizontal_sigma_m",
                    "fix_type",
                    "satellites_visible",
                    "horizontal_accuracy_m",
                    "vertical_accuracy_m",
                }
            ),
            optional=frozenset({"observed_at_s"}),
        )
        return cls(
            status=require_text(value["status"], "gps.status"),
            latitude_deg=_optional_real_mapping(
                value["latitude_deg"], "gps.latitude_deg", minimum=-90.0, maximum=90.0
            ),
            longitude_deg=_optional_real_mapping(
                value["longitude_deg"], "gps.longitude_deg", minimum=-180.0, maximum=180.0
            ),
            horizontal_sigma_m=_optional_real_mapping(
                value["horizontal_sigma_m"],
                "gps.horizontal_sigma_m",
                minimum=0.0,
                maximum=100_000.0,
            ),
            fix_type=(
                require_int(
                    value["fix_type"],
                    "gps.fix_type",
                    minimum=0,
                    maximum=MAVLINK_GPS_FIX_TYPE_MAX,
                )
                if value["fix_type"] is not None
                else None
            ),
            satellites_visible=(
                require_int(
                    value["satellites_visible"],
                    "gps.satellites_visible",
                    minimum=0,
                    maximum=MAVLINK_SATELLITES_UNKNOWN,
                )
                if value["satellites_visible"] is not None
                else None
            ),
            horizontal_accuracy_m=_optional_real_mapping(
                value["horizontal_accuracy_m"],
                "gps.horizontal_accuracy_m",
                minimum=0.0,
                maximum=100_000.0,
            ),
            vertical_accuracy_m=_optional_real_mapping(
                value["vertical_accuracy_m"],
                "gps.vertical_accuracy_m",
                minimum=0.0,
                maximum=100_000.0,
            ),
            observed_at_s=_optional_real_mapping(
                value.get("observed_at_s"), "gps.observed_at_s", minimum=0.0
            ),
        )


@dataclass(frozen=True, slots=True)
class TargetRangeEvidence:
    """Actual target range and relative bearing bound to the same frame."""

    validity: str = "UNAVAILABLE"
    range_m: float | None = None
    relative_bearing_deg: float | None = None
    range_kind: str = "unknown"

    def __post_init__(self) -> None:
        validity = require_text(self.validity, "range.validity").upper()
        if validity not in _RANGE_VALIDITIES:
            raise AutonomyContractError("range.validity must be VALID, DEGRADED, or UNAVAILABLE")
        object.__setattr__(self, "validity", validity)
        range_m = _optional_real_mapping(
            self.range_m, "range.range_m", minimum=0.0, maximum=1_000_000.0
        )
        bearing = _optional_real_mapping(
            self.relative_bearing_deg, "range.relative_bearing_deg", minimum=-360.0, maximum=360.0
        )
        object.__setattr__(self, "range_m", range_m)
        object.__setattr__(self, "relative_bearing_deg", bearing)
        kind = require_text(self.range_kind, "range.range_kind").lower()
        if kind not in {"slant", "ground", "unknown"}:
            raise AutonomyContractError("range.range_kind must be slant, ground, or unknown")
        object.__setattr__(self, "range_kind", kind)
        if validity in {"VALID", "DEGRADED"} and range_m is None:
            raise AutonomyContractError("valid/degraded range evidence requires range_m")
        if validity == "UNAVAILABLE" and (range_m is not None or bearing is not None):
            raise AutonomyContractError(
                "unavailable range evidence cannot contain range_m or relative_bearing_deg"
            )

    def to_mapping(self) -> dict[str, object]:
        return {
            "validity": self.validity,
            "range_m": self.range_m,
            "relative_bearing_deg": self.relative_bearing_deg,
            "range_kind": self.range_kind,
        }

    @classmethod
    def from_mapping(cls, raw: object) -> TargetRangeEvidence:
        value = strict_object(
            raw,
            name="TargetRangeEvidence",
            required=frozenset({"validity", "range_m", "relative_bearing_deg", "range_kind"}),
        )
        return cls(
            validity=require_text(value["validity"], "range.validity"),
            range_m=_optional_real_mapping(
                value["range_m"], "range.range_m", minimum=0.0, maximum=1_000_000.0
            ),
            relative_bearing_deg=_optional_real_mapping(
                value["relative_bearing_deg"],
                "range.relative_bearing_deg",
                minimum=-360.0,
                maximum=360.0,
            ),
            range_kind=require_text(value["range_kind"], "range.range_kind"),
        )


@dataclass(frozen=True, slots=True)
class TargetEventInput:
    """One capture supplied to :class:`TargetEventReplayBuilder`."""

    event_id: str
    frame_id: str
    target_id: str
    target_label: str
    captured_at_s: float
    gps: TargetGpsEvidence
    ranging: TargetRangeEvidence
    video: TargetMediaInput | None = None
    depth: TargetMediaInput | None = None
    source_frame_id: str | None = None
    vehicle_altitude_agl_m: float | None = None
    vehicle_heading_deg: float | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.event_id, "event_id"),
            (self.frame_id, "frame_id"),
            (self.target_id, "target_id"),
            (self.target_label, "target_label"),
        ):
            require_text(value, name)
        require_real(self.captured_at_s, "captured_at_s", minimum=0.0)
        if not isinstance(self.gps, TargetGpsEvidence):
            raise AutonomyContractError("gps must be TargetGpsEvidence")
        if not isinstance(self.ranging, TargetRangeEvidence):
            raise AutonomyContractError("ranging must be TargetRangeEvidence")
        _validate_gps_event_timing(self.gps, self.captured_at_s)
        for media, kind in ((self.video, "video_frame"), (self.depth, "depth_map")):
            if media is not None:
                if not isinstance(media, TargetMediaInput):
                    raise AutonomyContractError(f"{kind} must be TargetMediaInput")
                if media.kind != kind:
                    raise AutonomyContractError(f"{kind} media kind mismatch")
        if self.video is None and self.depth is None:
            raise AutonomyContractError("target event requires a video frame or depth map")
        if self.source_frame_id is not None:
            require_text(self.source_frame_id, "source_frame_id")
        for value, name in (
            (self.vehicle_altitude_agl_m, "vehicle_altitude_agl_m"),
            (self.vehicle_heading_deg, "vehicle_heading_deg"),
        ):
            if value is not None:
                require_real(value, name, minimum=0.0 if name.endswith("altitude_agl_m") else 0.0)
        if self.vehicle_heading_deg is not None and self.vehicle_heading_deg > 360.0:
            raise AutonomyContractError("vehicle_heading_deg must be at most 360")


@dataclass(frozen=True, slots=True)
class TargetEvent:
    """One fully materialized, hash-bound target event."""

    sequence: int
    event_id: str
    frame_id: str
    target_id: str
    target_label: str
    captured_at_s: float
    gps: TargetGpsEvidence
    ranging: TargetRangeEvidence
    video: TargetMediaReference | None = None
    depth: TargetMediaReference | None = None
    source_frame_id: str | None = None
    vehicle_altitude_agl_m: float | None = None
    vehicle_heading_deg: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sequence", require_int(self.sequence, "sequence", minimum=0))
        for value, name in (
            (self.event_id, "event_id"),
            (self.frame_id, "frame_id"),
            (self.target_id, "target_id"),
            (self.target_label, "target_label"),
        ):
            object.__setattr__(self, name, require_text(value, name))
        object.__setattr__(
            self,
            "captured_at_s",
            require_real(self.captured_at_s, "captured_at_s", minimum=0.0),
        )
        if not isinstance(self.gps, TargetGpsEvidence):
            raise AutonomyContractError("gps must be TargetGpsEvidence")
        if not isinstance(self.ranging, TargetRangeEvidence):
            raise AutonomyContractError("ranging must be TargetRangeEvidence")
        _validate_gps_event_timing(self.gps, self.captured_at_s)
        for media, kind in ((self.video, "video_frame"), (self.depth, "depth_map")):
            if media is not None:
                if not isinstance(media, TargetMediaReference) or media.kind != kind:
                    raise AutonomyContractError(f"{kind} media reference is invalid")
        if self.video is None and self.depth is None:
            raise AutonomyContractError("target event requires a video frame or depth map")
        if self.source_frame_id is not None:
            object.__setattr__(
                self, "source_frame_id", require_text(self.source_frame_id, "source_frame_id")
            )
        for value, name in (
            (self.vehicle_altitude_agl_m, "vehicle_altitude_agl_m"),
            (self.vehicle_heading_deg, "vehicle_heading_deg"),
        ):
            if value is not None:
                require_real(value, name, minimum=0.0)
        if self.vehicle_heading_deg is not None and self.vehicle_heading_deg > 360.0:
            raise AutonomyContractError("vehicle_heading_deg must be at most 360")

    @property
    def gps_age_s(self) -> float | None:
        """Age of the GPS observation at this event, or ``None`` for legacy data."""

        return self.gps.age_at(self.captured_at_s)

    @property
    def gps_freshness(self) -> str:
        """Derived ``fresh``/``stale``/``unknown`` GPS freshness label."""

        return self.gps.freshness_at(self.captured_at_s)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema": TARGET_EVENT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "target_event",
            "sequence": self.sequence,
            "event_id": self.event_id,
            "frame_id": self.frame_id,
            "target_id": self.target_id,
            "target_label": self.target_label,
            "captured_at_s": self.captured_at_s,
            "gps": self.gps.to_mapping(),
            "gps_age_s": self.gps_age_s,
            "gps_freshness": self.gps_freshness,
            "ranging": self.ranging.to_mapping(),
            "video": self.video.to_mapping() if self.video is not None else None,
            "depth": self.depth.to_mapping() if self.depth is not None else None,
            "source_frame_id": self.source_frame_id,
            "vehicle_altitude_agl_m": self.vehicle_altitude_agl_m,
            "vehicle_heading_deg": self.vehicle_heading_deg,
        }

    @classmethod
    def from_mapping(cls, raw: object, *, asset_root: Path) -> TargetEvent:
        value = strict_object(
            raw,
            name="TargetEvent",
            required=frozenset(
                {
                    "schema",
                    "schema_version",
                    "kind",
                    "sequence",
                    "event_id",
                    "frame_id",
                    "target_id",
                    "target_label",
                    "captured_at_s",
                    "gps",
                    "ranging",
                    "video",
                    "depth",
                    "source_frame_id",
                    "vehicle_altitude_agl_m",
                    "vehicle_heading_deg",
                }
            ),
            optional=frozenset({"gps_age_s", "gps_freshness"}),
        )
        if require_text(value["schema"], "schema") != TARGET_EVENT_SCHEMA:
            raise AutonomyContractError("target event schema identifier is invalid")
        if require_text(value["kind"], "kind") != "target_event":
            raise AutonomyContractError("target event kind is invalid")
        require_schema_version(value["schema_version"])
        video = (
            TargetMediaReference.from_mapping(value["video"], asset_root=asset_root)
            if value["video"] is not None
            else None
        )
        depth = (
            TargetMediaReference.from_mapping(value["depth"], asset_root=asset_root)
            if value["depth"] is not None
            else None
        )
        event = cls(
            sequence=require_int(value["sequence"], "sequence", minimum=0),
            event_id=require_text(value["event_id"], "event_id"),
            frame_id=require_text(value["frame_id"], "frame_id"),
            target_id=require_text(value["target_id"], "target_id"),
            target_label=require_text(value["target_label"], "target_label"),
            captured_at_s=require_real(value["captured_at_s"], "captured_at_s", minimum=0.0),
            gps=TargetGpsEvidence.from_mapping(value["gps"]),
            ranging=TargetRangeEvidence.from_mapping(value["ranging"]),
            video=video,
            depth=depth,
            source_frame_id=(
                optional_text(value["source_frame_id"], "source_frame_id")
                if value["source_frame_id"] is not None
                else None
            ),
            vehicle_altitude_agl_m=_optional_real_mapping(
                value["vehicle_altitude_agl_m"], "vehicle_altitude_agl_m", minimum=0.0
            ),
            vehicle_heading_deg=_optional_real_mapping(
                value["vehicle_heading_deg"], "vehicle_heading_deg", minimum=0.0, maximum=360.0
            ),
        )
        declared_age = (
            _optional_real_mapping(value["gps_age_s"], "gps_age_s", minimum=0.0)
            if "gps_age_s" in value
            else None
        )
        declared_freshness_present = "gps_freshness" in value
        declared_freshness = (
            require_text(value["gps_freshness"], "gps_freshness")
            if declared_freshness_present
            else None
        )
        if declared_freshness is not None and declared_freshness not in _GPS_FRESHNESS_VALUES:
            raise AutonomyContractError(
                "gps_freshness must be fresh, stale, or unknown"
            )
        _validate_declared_gps_timing(
            declared_age=declared_age,
            declared_age_present="gps_age_s" in value,
            declared_freshness=declared_freshness,
            declared_freshness_present=declared_freshness_present,
            event=event,
        )
        return event


@dataclass(frozen=True, slots=True)
class TargetEventReplayBundle:
    """Verified target event timeline and its contained media assets."""

    bundle_id: str
    session_id: str
    started_at_utc: str
    event_stream_sha256: str
    event_stream_bytes: int
    events: tuple[TargetEvent, ...]
    root_path: Path
    manifest_path: Path
    bundle_hash: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "bundle_id", require_text(self.bundle_id, "bundle_id"))
        object.__setattr__(self, "session_id", _normalize_session_id(self.session_id))
        object.__setattr__(
            self, "started_at_utc", require_aware_timestamp(self.started_at_utc, "started_at_utc")
        )
        object.__setattr__(
            self, "event_stream_sha256", require_sha256(self.event_stream_sha256, "events.sha256")
        )
        object.__setattr__(
            self,
            "event_stream_bytes",
            require_int(self.event_stream_bytes, "events.bytes", minimum=1),
        )
        if not isinstance(self.events, Sequence) or isinstance(
            self.events, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("events must be an array")
        events = tuple(self.events)
        if not events or any(not isinstance(event, TargetEvent) for event in events):
            raise AutonomyContractError("events must contain TargetEvent values")
        if tuple(event.sequence for event in events) != tuple(range(len(events))):
            raise AutonomyContractError("target event sequences must be contiguous from zero")
        event_ids = tuple(event.event_id for event in events)
        if len(set(event_ids)) != len(event_ids):
            raise AutonomyContractError("target event event_id values must be unique")
        # GPS observations are part of the same monotonic capture timeline as
        # the events.  A source can be omitted for legacy records, but when two
        # observations are present they must never move backwards: otherwise a
        # replay could show a newer target frame backed by an older aircraft
        # fix (a subtle form of time-travel evidence).  Keep the last known
        # observation across records with an omitted timestamp so a later
        # regression is still rejected.
        previous_gps_observed_at_s: float | None = None
        if any(
            current.captured_at_s <= previous.captured_at_s
            for previous, current in zip(events, events[1:], strict=False)
        ):
            raise AutonomyContractError("target event timestamps must be strictly increasing")
        for event in events:
            observed_at_s = event.gps.observed_at_s
            if observed_at_s is None:
                continue
            if (
                previous_gps_observed_at_s is not None
                and observed_at_s < previous_gps_observed_at_s
            ):
                raise AutonomyContractError(
                    "target event GPS observation timestamps must be non-decreasing"
                )
            previous_gps_observed_at_s = observed_at_s
        object.__setattr__(self, "events", events)
        root = Path(self.root_path)
        manifest = Path(self.manifest_path)
        if not root.is_absolute() or not root.is_dir():
            raise AutonomyContractError("bundle root_path must be an existing absolute directory")
        if not manifest.is_absolute() or not manifest.is_file():
            raise AutonomyContractError("bundle manifest_path must be an existing absolute file")
        if manifest.parent.resolve(strict=True) != root.resolve(strict=True):
            raise AutonomyContractError("bundle manifest_path must be inside root_path")
        object.__setattr__(self, "root_path", root.resolve(strict=True))
        object.__setattr__(self, "manifest_path", manifest.resolve(strict=True))
        object.__setattr__(self, "bundle_hash", require_sha256(self.bundle_hash, "bundle_hash"))

    @property
    def event_count(self) -> int:
        return len(self.events)

    @property
    def media_count(self) -> int:
        return len(
            {
                reference.relative_path
                for event in self.events
                for reference in (event.video, event.depth)
                if reference is not None
            }
        )

    def iter_events(
        self,
        *,
        target_id: str | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> Iterator[TargetEvent]:
        normalized_target = require_text(target_id, "target_id") if target_id is not None else None
        start = require_real(start_s, "start_s", minimum=0.0) if start_s is not None else None
        end = require_real(end_s, "end_s", minimum=0.0) if end_s is not None else None
        if start is not None and end is not None and end < start:
            raise AutonomyContractError("event replay end_s must not be before start_s")
        for event in self.events:
            if normalized_target is not None and event.target_id != normalized_target:
                continue
            if start is not None and event.captured_at_s < start:
                continue
            if end is not None and event.captured_at_s > end:
                continue
            yield event

    def summary(self) -> dict[str, object]:
        return {
            "schema": TARGET_EVENT_REPLAY_SCHEMA,
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "started_at_utc": self.started_at_utc,
            "event_count": self.event_count,
            "media_count": self.media_count,
            "event_stream_sha256": self.event_stream_sha256,
            "event_stream_bytes": self.event_stream_bytes,
            "bundle_hash": self.bundle_hash,
            "claims": {
                "read_only_evidence": True,
                "contains_flight_control": False,
                "contains_truth": False,
            },
        }


class TargetEventReplayBuilder:
    """Create one immutable target event replay bundle."""

    def __init__(
        self,
        root: str | Path,
        *,
        session_id: str,
        bundle_id: str | None = None,
        started_at_utc: str | None = None,
        maximum_events: int = 10_000,
        maximum_media_bytes: int = MAX_TOTAL_MEDIA_BYTES,
        overwrite: bool = False,
    ) -> None:
        self.root = Path(root).resolve()
        self.session_id = _normalize_session_id(session_id)
        self.bundle_id = require_text(
            bundle_id if bundle_id is not None else str(uuid4()), "bundle_id"
        )
        selected_started_at = started_at_utc if started_at_utc is not None else _utc_now()
        self.started_at_utc = require_aware_timestamp(selected_started_at, "started_at_utc")
        self.maximum_events = require_int(
            maximum_events, "maximum_events", minimum=1, maximum=100_000
        )
        self.maximum_media_bytes = require_int(
            maximum_media_bytes,
            "maximum_media_bytes",
            minimum=1,
            maximum=4 * 1024 * 1024 * 1024,
        )
        self._closed = False
        self._events: list[TargetEvent] = []
        self._media_bytes = 0
        if self.root.exists():
            if not self.root.is_dir():
                raise AutonomyContractError("target event replay root must be a directory")
            # A replay bundle is an immutable evidence artifact.  Never delete
            # or replace an existing directory implicitly, even when callers
            # pass the legacy ``overwrite`` flag.  They must choose a fresh
            # empty root so an old event stream cannot be mixed into a new one.
            if any(self.root.iterdir()):
                raise AutonomyContractError(
                    "target event replay root is not empty; choose a new empty root"
                )
        else:
            self.root.mkdir(parents=True, exist_ok=False)
        self.media_root = self.root / "media"
        self.media_root.mkdir(parents=True, exist_ok=True)

    def append(self, event: TargetEventInput) -> TargetEvent:
        if self._closed:
            raise AutonomyContractError("target event replay builder is already finalized")
        if not isinstance(event, TargetEventInput):
            raise AutonomyContractError("append requires TargetEventInput")
        if len(self._events) >= self.maximum_events:
            raise AutonomyContractError("target event replay maximum_events reached")
        if any(previous.event_id == event.event_id for previous in self._events):
            raise AutonomyContractError("target event event_id values must be unique")
        if self._events and event.captured_at_s <= self._events[-1].captured_at_s:
            raise AutonomyContractError("target event timestamps must be strictly increasing")
        if event.gps.observed_at_s is not None:
            previous_gps_observed_at_s = next(
                (
                    previous.gps.observed_at_s
                    for previous in reversed(self._events)
                    if previous.gps.observed_at_s is not None
                ),
                None,
            )
            if (
                previous_gps_observed_at_s is not None
                and event.gps.observed_at_s < previous_gps_observed_at_s
            ):
                raise AutonomyContractError(
                    "target event GPS observation timestamps must be non-decreasing"
                )
        sequence = len(self._events)
        video = self._store_media(sequence, event.video)
        depth = self._store_media(sequence, event.depth)
        materialized = TargetEvent(
            sequence=sequence,
            event_id=event.event_id,
            frame_id=event.frame_id,
            target_id=event.target_id,
            target_label=event.target_label,
            captured_at_s=event.captured_at_s,
            gps=event.gps,
            ranging=event.ranging,
            video=video,
            depth=depth,
            source_frame_id=event.source_frame_id,
            vehicle_altitude_agl_m=event.vehicle_altitude_agl_m,
            vehicle_heading_deg=event.vehicle_heading_deg,
        )
        self._events.append(materialized)
        return materialized

    def _store_media(
        self,
        sequence: int,
        media: TargetMediaInput | None,
    ) -> TargetMediaReference | None:
        if media is None:
            return None
        next_total = self._media_bytes + len(media.payload)
        if next_total > self.maximum_media_bytes:
            raise AutonomyContractError("target event replay media byte budget exceeded")
        extension = _MEDIA_EXTENSIONS[media.encoding]
        path = self.media_root / f"{sequence:06d}-{media.kind}.{extension}"
        _atomic_write_bytes(path, media.payload)
        digest = hashlib.sha256(media.payload).hexdigest()
        self._media_bytes = next_total
        return TargetMediaReference(
            kind=media.kind,
            relative_path=path.relative_to(self.root).as_posix(),
            resolved_path=path,
            sha256=digest,
            byte_count=len(media.payload),
            encoding=media.encoding,
            width=media.width,
            height=media.height,
        )

    def finalize(self) -> TargetEventReplayBundle:
        if self._closed:
            raise AutonomyContractError("target event replay builder is already finalized")
        if not self._events:
            raise AutonomyContractError("target event replay requires at least one event")
        event_stream = b"".join(
            canonical_json(event.to_mapping()).encode("utf-8") + b"\n" for event in self._events
        )
        event_path = self.root / "events.jsonl"
        _atomic_write_bytes(event_path, event_stream)
        media_references = {
            reference.relative_path: reference
            for event in self._events
            for reference in (event.video, event.depth)
            if reference is not None
        }
        manifest_without_hash: dict[str, object] = {
            "schema": TARGET_EVENT_REPLAY_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "kind": "target_event_replay_manifest",
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "started_at_utc": self.started_at_utc,
            "events": {
                "path": "events.jsonl",
                "sha256": hashlib.sha256(event_stream).hexdigest(),
                "bytes": len(event_stream),
                "count": len(self._events),
            },
            "media": {
                "root": "media",
                "count": len(media_references),
                "bytes": sum(reference.byte_count for reference in media_references.values()),
            },
            "claims": {
                "read_only_evidence": True,
                "contains_flight_control": False,
                "contains_truth": False,
            },
        }
        manifest = dict(manifest_without_hash)
        manifest["bundle_hash"] = canonical_sha256(manifest_without_hash)
        manifest_path = self.root / "manifest.json"
        _atomic_write_bytes(
            manifest_path,
            (canonical_json(manifest) + "\n").encode("utf-8"),
        )
        self._closed = True
        return load_target_event_replay_manifest(manifest_path)


def load_target_event_replay_manifest(
    path: str | Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> TargetEventReplayBundle:
    """Load and verify one target event replay manifest and all referenced media."""

    requested_manifest_path = Path(path)
    if requested_manifest_path.is_symlink():
        raise AutonomyContractError("target event replay manifest must not be a symlink")
    manifest_path = requested_manifest_path.resolve(strict=True)
    if not manifest_path.is_file():
        raise AutonomyContractError("target event replay manifest must be a regular file")
    manifest_bytes = manifest_path.read_bytes()
    if expected_manifest_sha256 is not None:
        expected = require_sha256(expected_manifest_sha256, "expected_manifest_sha256")
        observed = hashlib.sha256(manifest_bytes).hexdigest()
        if observed != expected:
            raise AutonomyContractError(
                "target event replay manifest hash mismatch: "
                f"expected {expected}, observed {observed}"
            )
    raw = _strict_json_loads(manifest_bytes, name="target event replay manifest")
    value = strict_object(
        raw,
        name="TargetEventReplayManifest",
        required=frozenset(
            {
                "schema",
                "schema_version",
                "kind",
                "bundle_id",
                "session_id",
                "started_at_utc",
                "events",
                "media",
                "claims",
                "bundle_hash",
            }
        ),
    )
    if require_text(value["schema"], "schema") != TARGET_EVENT_REPLAY_SCHEMA:
        raise AutonomyContractError("target event replay manifest schema identifier is invalid")
    if require_text(value["kind"], "kind") != "target_event_replay_manifest":
        raise AutonomyContractError("target event replay manifest kind is invalid")
    require_schema_version(value["schema_version"])
    claims = strict_object(
        value["claims"],
        name="TargetEventReplayClaims",
        required=frozenset({"read_only_evidence", "contains_flight_control", "contains_truth"}),
    )
    if not require_bool(claims["read_only_evidence"], "claims.read_only_evidence"):
        raise AutonomyContractError("target event replay must be read-only evidence")
    if require_bool(claims["contains_flight_control"], "claims.contains_flight_control"):
        raise AutonomyContractError("target event replay cannot contain flight-control output")
    if require_bool(claims["contains_truth"], "claims.contains_truth"):
        raise AutonomyContractError("target event replay cannot contain simulator truth")

    manifest_without_hash = dict(value)
    del manifest_without_hash["bundle_hash"]
    expected_bundle_hash = require_sha256(value["bundle_hash"], "bundle_hash")
    observed_bundle_hash = canonical_sha256(manifest_without_hash)
    if observed_bundle_hash != expected_bundle_hash:
        raise AutonomyContractError(
            "target event replay bundle_hash does not match the canonical manifest"
        )

    root = manifest_path.parent.resolve(strict=True)
    events_value = strict_object(
        value["events"],
        name="TargetEventReplayEvents",
        required=frozenset({"path", "sha256", "bytes", "count"}),
    )
    event_relative_path = _strict_relative_path(events_value["path"], "events.path")
    if event_relative_path != "events.jsonl":
        raise AutonomyContractError("events.path must be events.jsonl")
    event_path = _resolve_contained_file(root, event_relative_path, name="events")
    event_bytes = _read_verified_file(
        event_path,
        expected_sha256=require_sha256(events_value["sha256"], "events.sha256"),
        expected_byte_count=require_int(events_value["bytes"], "events.bytes", minimum=1),
        name="target event stream",
    )
    event_count = require_int(events_value["count"], "events.count", minimum=1, maximum=100_000)
    media_value = strict_object(
        value["media"],
        name="TargetEventReplayMedia",
        required=frozenset({"root", "count", "bytes"}),
    )
    if _strict_relative_path(media_value["root"], "media.root") != "media":
        raise AutonomyContractError("media.root must be media")
    media_root = _resolve_contained_directory(root, media_value["root"], name="media.root")
    declared_media_count = require_int(
        media_value["count"], "media.count", minimum=0, maximum=200_000
    )
    declared_media_bytes = require_int(
        media_value["bytes"], "media.bytes", minimum=0, maximum=MAX_TOTAL_MEDIA_BYTES
    )
    try:
        text = event_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AutonomyContractError("target event stream must be UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != event_count:
        raise AutonomyContractError(
            f"target event count mismatch: expected {event_count}, observed {len(lines)}"
        )
    events: list[TargetEvent] = []
    referenced_paths: dict[str, TargetMediaReference] = {}
    previous_time = -1.0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise AutonomyContractError(f"target event stream line {line_number} cannot be blank")
        parsed = _strict_json_loads(
            line.encode("utf-8"), name=f"target event stream line {line_number}"
        )
        event = TargetEvent.from_mapping(parsed, asset_root=root)
        if event.sequence != len(events):
            raise AutonomyContractError(
                f"target event stream line {line_number} sequence is not contiguous"
            )
        if event.captured_at_s <= previous_time:
            raise AutonomyContractError("target event timestamps must be strictly increasing")
        previous_time = event.captured_at_s
        for reference in (event.video, event.depth):
            if reference is None:
                continue
            if not reference.resolved_path.is_relative_to(media_root):
                raise AutonomyContractError("target event media must stay below media.root")
            if reference.relative_path in referenced_paths:
                raise AutonomyContractError("target event media paths must be unique")
            referenced_paths[reference.relative_path] = reference
        events.append(event)
    if len(referenced_paths) != declared_media_count:
        raise AutonomyContractError(
            "target event media count mismatch: "
            f"expected {declared_media_count}, observed {len(referenced_paths)}"
        )
    observed_media_bytes = sum(reference.byte_count for reference in referenced_paths.values())
    if observed_media_bytes != declared_media_bytes:
        raise AutonomyContractError(
            "target event media byte mismatch: "
            f"expected {declared_media_bytes}, observed {observed_media_bytes}"
        )
    actual_media_paths: set[str] = set()
    try:
        media_candidates = tuple(media_root.rglob("*"))
    except OSError as exc:
        raise AutonomyContractError(f"unable to enumerate target event media: {exc}") from exc
    for candidate in media_candidates:
        if candidate.is_symlink():
            raise AutonomyContractError("target event media directory cannot contain symlinks")
        if candidate.is_file():
            actual_media_paths.add(candidate.relative_to(root).as_posix())
        elif not candidate.is_dir():
            raise AutonomyContractError("target event media directory contains a non-regular entry")
    if actual_media_paths != set(referenced_paths):
        raise AutonomyContractError("target event media directory contains unreferenced files")
    return TargetEventReplayBundle(
        bundle_id=require_text(value["bundle_id"], "bundle_id"),
        session_id=require_text(value["session_id"], "session_id"),
        started_at_utc=require_text(value["started_at_utc"], "started_at_utc"),
        event_stream_sha256=require_sha256(events_value["sha256"], "events.sha256"),
        event_stream_bytes=require_int(events_value["bytes"], "events.bytes", minimum=1),
        events=tuple(events),
        root_path=root,
        manifest_path=manifest_path,
        bundle_hash=expected_bundle_hash,
    )


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "GPS_QUALIFIED_FIX_TYPE",
    "GPS_QUALIFIED_MAX_AGE_S",
    "GPS_QUALIFIED_MAX_HORIZONTAL_ACCURACY_M",
    "GPS_QUALIFIED_MAX_VERTICAL_ACCURACY_M",
    "GPS_QUALIFIED_MIN_SATELLITES",
    "MAX_MEDIA_BYTES",
    "MAX_TOTAL_MEDIA_BYTES",
    "TARGET_EVENT_REPLAY_SCHEMA",
    "TARGET_EVENT_SCHEMA",
    "TargetEvent",
    "TargetEventInput",
    "TargetEventReplayBuilder",
    "TargetEventReplayBundle",
    "TargetGpsEvidence",
    "TargetMediaInput",
    "TargetMediaReference",
    "TargetRangeEvidence",
    "load_target_event_replay_manifest",
]
