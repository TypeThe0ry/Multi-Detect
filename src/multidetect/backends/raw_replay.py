"""Deterministic pre-detection replay from integrity-bound raw sensor events.

The recording contains only raw image references and read-only telemetry
samples.  It deliberately has no detector output, evaluator truth, or expected
answer.  A caller binds :attr:`RawSensorReplayBackend.frame_source` and
:attr:`RawSensorReplayBackend.telemetry_provider` to the normal live pipeline
and supplies the same production detector used by SIM/HIL/REAL.

One strict manifest points at one JSONL event stream.  Every referenced file is
contained below the recording root and SHA-256/byte-count checked.  Reading a
frame verifies its image again so a post-load mutation fails closed rather than
silently changing a replay.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any

from ..autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    optional_text,
    require_bool,
    require_int,
    require_real,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from ..clock import ManualClock
from ..compat import UTC
from ..domain import VehicleTelemetry
from ..mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_SATELLITES_UNKNOWN,
    normalize_satellites_visible,
)
from ..runtime import RuntimeDescriptor, RuntimeMode, RuntimeProfile
from ..simulation.runtime_input import ScenarioRuntimeInput
from ..vision import CapturedFrame

ImageDecoder = Callable[[bytes, "RawReplayImageReference"], Any]

_UTC_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")
_DEFAULT_MAXIMUM_TELEMETRY_AGE_S = 1.0
_DEFAULT_MAXIMUM_CLOCK_SKEW_S = 1e-6

_REQUIRED_TELEMETRY_FIELDS = frozenset(
    {
        "altitude_agl_m",
        "roll_deg",
        "pitch_deg",
        "ground_speed_mps",
        "in_allowed_zone",
        "geofence_healthy",
        "position_healthy",
        "link_healthy",
        "flight_mode_allows_deploy",
        "release_zone_clear",
    }
)
_NULLABLE_BOOLEAN_FIELDS = frozenset(
    {
        "in_allowed_zone",
        "geofence_healthy",
        "position_healthy",
        "link_healthy",
        "flight_mode_allows_deploy",
        "release_zone_clear",
        "person_detector_healthy",
        "armed",
    }
)
_NULLABLE_INTEGER_FIELDS = frozenset(
    {
        "satellites_visible",
        "gps_fix_type",
        "mission_sequence",
        "mission_item_reached_sequence",
    }
)
_NULLABLE_TEXT_FIELDS = frozenset({"flight_mode", "altitude_reference"})
_NULLABLE_REAL_FIELDS = frozenset(
    {
        "altitude_agl_m",
        "roll_deg",
        "pitch_deg",
        "ground_speed_mps",
        "latitude_deg",
        "longitude_deg",
        "heading_deg",
        "battery_remaining_pct",
        "gps_horizontal_accuracy_m",
        "gps_vertical_accuracy_m",
        "gps_observed_at_s",
        "mission_sequence_observed_at_s",
        "mission_item_reached_observed_at_s",
        "attitude_observed_at_s",
        "position_observed_at_s",
        "velocity_north_mps",
        "velocity_east_mps",
        "acceleration_uncertainty_mps2",
        "airspeed_mps",
        "wind_north_mps",
        "wind_east_mps",
        "velocity_observed_at_s",
        "airspeed_observed_at_s",
        "wind_observed_at_s",
        "altitude_observed_at_s",
        "local_north_m",
        "local_east_m",
        "local_down_m",
        "local_position_observed_at_s",
        "gps_absolute_altitude_m",
    }
)
_ALL_TELEMETRY_FIELDS = frozenset(item.name for item in fields(VehicleTelemetry))
if (
    _NULLABLE_BOOLEAN_FIELDS
    | _NULLABLE_INTEGER_FIELDS
    | _NULLABLE_TEXT_FIELDS
    | _NULLABLE_REAL_FIELDS
) != _ALL_TELEMETRY_FIELDS:  # pragma: no cover - import-time schema drift guard
    raise RuntimeError("raw replay telemetry schema is out of sync with VehicleTelemetry")

_NON_NEGATIVE_REAL_FIELDS = frozenset(
    {
        "ground_speed_mps",
        "battery_remaining_pct",
        "gps_horizontal_accuracy_m",
        "gps_vertical_accuracy_m",
        "acceleration_uncertainty_mps2",
        "gps_observed_at_s",
        "mission_sequence_observed_at_s",
        "mission_item_reached_observed_at_s",
        "attitude_observed_at_s",
        "position_observed_at_s",
        "airspeed_mps",
        "velocity_observed_at_s",
        "airspeed_observed_at_s",
        "wind_observed_at_s",
        "altitude_observed_at_s",
        "local_position_observed_at_s",
    }
)


@dataclass(frozen=True, slots=True)
class RawReplayImageReference:
    """One verified encoded image below a recording root."""

    relative_path: str
    resolved_path: Path
    sha256: str
    byte_count: int
    width: int
    height: int
    encoding: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relative_path",
            _strict_relative_path(self.relative_path, "image.path"),
        )
        source = Path(self.resolved_path)
        if not source.is_absolute() or not source.is_file():
            raise AutonomyContractError("image resolved_path must be an existing absolute file")
        object.__setattr__(self, "resolved_path", source.resolve(strict=True))
        object.__setattr__(self, "sha256", require_sha256(self.sha256, "image.sha256"))
        object.__setattr__(
            self,
            "byte_count",
            require_int(self.byte_count, "image.bytes", minimum=1),
        )
        object.__setattr__(self, "width", require_int(self.width, "image.width", minimum=1))
        object.__setattr__(self, "height", require_int(self.height, "image.height", minimum=1))
        encoding = require_text(self.encoding, "image.encoding").lower()
        if encoding == "jpg":
            encoding = "jpeg"
        if encoding not in {"png", "jpeg", "bmp", "npy"}:
            raise AutonomyContractError("image.encoding must be one of: png, jpeg, bmp, npy")
        object.__setattr__(self, "encoding", encoding)
        self.read_verified_bytes()

    @classmethod
    def from_mapping(
        cls,
        raw: object,
        *,
        asset_root: Path,
    ) -> RawReplayImageReference:
        value = strict_object(
            raw,
            name="RawReplayImageReference",
            required=frozenset({"path", "sha256", "bytes", "width", "height", "encoding"}),
        )
        relative_path = _strict_relative_path(value["path"], "image.path")
        return cls(
            relative_path=relative_path,
            resolved_path=_resolve_contained_file(
                asset_root,
                relative_path,
                name="image",
            ),
            sha256=require_sha256(value["sha256"], "image.sha256"),
            byte_count=require_int(value["bytes"], "image.bytes", minimum=1),
            width=require_int(value["width"], "image.width", minimum=1),
            height=require_int(value["height"], "image.height", minimum=1),
            encoding=require_text(value["encoding"], "image.encoding"),
        )

    def read_verified_bytes(self) -> bytes:
        return _read_verified_file(
            self.resolved_path,
            expected_sha256=self.sha256,
            expected_byte_count=self.byte_count,
            name=f"image {self.relative_path}",
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "sha256": self.sha256,
            "bytes": self.byte_count,
            "width": self.width,
            "height": self.height,
            "encoding": self.encoding,
        }


@dataclass(frozen=True, slots=True)
class RawReplayFrame:
    frame_id: str
    captured_at_s: float
    image: RawReplayImageReference
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "frame_id", require_text(self.frame_id, "frame_id"))
        object.__setattr__(
            self,
            "captured_at_s",
            require_real(self.captured_at_s, "captured_at_s", minimum=0.0),
        )
        if not isinstance(self.image, RawReplayImageReference):
            raise AutonomyContractError("image must be a RawReplayImageReference")

    @classmethod
    def from_mapping(cls, raw: object, *, asset_root: Path) -> RawReplayFrame:
        value = strict_object(
            raw,
            name="RawReplayFrame",
            required=frozenset({"schema_version", "kind", "frame_id", "captured_at_s", "image"}),
        )
        if require_text(value["kind"], "kind") != "frame":
            raise AutonomyContractError("RawReplayFrame kind must be frame")
        return cls(
            frame_id=require_text(value["frame_id"], "frame_id"),
            captured_at_s=require_real(value["captured_at_s"], "captured_at_s", minimum=0.0),
            image=RawReplayImageReference.from_mapping(value["image"], asset_root=asset_root),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "frame",
            "frame_id": self.frame_id,
            "captured_at_s": self.captured_at_s,
            "image": self.image.to_mapping(),
        }


@dataclass(frozen=True, slots=True)
class RawReplayTelemetrySample:
    observed_at_s: float
    telemetry: VehicleTelemetry
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(
            self,
            "observed_at_s",
            require_real(self.observed_at_s, "observed_at_s", minimum=0.0),
        )
        if not isinstance(self.telemetry, VehicleTelemetry):
            raise AutonomyContractError("telemetry must be VehicleTelemetry")

    @classmethod
    def from_mapping(cls, raw: object) -> RawReplayTelemetrySample:
        value = strict_object(
            raw,
            name="RawReplayTelemetrySample",
            required=frozenset({"schema_version", "kind", "observed_at_s", "telemetry"}),
        )
        if require_text(value["kind"], "kind") != "telemetry":
            raise AutonomyContractError("RawReplayTelemetrySample kind must be telemetry")
        return cls(
            observed_at_s=require_real(value["observed_at_s"], "observed_at_s", minimum=0.0),
            telemetry=_vehicle_telemetry_from_mapping(value["telemetry"]),
            schema_version=require_schema_version(value["schema_version"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "telemetry",
            "observed_at_s": self.observed_at_s,
            "telemetry": _vehicle_telemetry_to_mapping(self.telemetry),
        }


@dataclass(frozen=True, slots=True)
class RawSensorReplayRecording:
    """Immutable, validation-complete raw frame and telemetry timeline."""

    recording_id: str
    airframe_profile_hash: str
    started_at_utc: datetime
    event_stream_sha256: str
    event_stream_bytes: int
    frames: tuple[RawReplayFrame, ...]
    telemetry_samples: tuple[RawReplayTelemetrySample, ...]
    scenario_id: str | None = None
    scenario_manifest_hash: str | None = None
    scenario_seed: int | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", require_schema_version(self.schema_version))
        object.__setattr__(self, "recording_id", require_text(self.recording_id, "recording_id"))
        object.__setattr__(
            self,
            "started_at_utc",
            _require_utc_timestamp(self.started_at_utc, "started_at_utc"),
        )
        object.__setattr__(
            self,
            "airframe_profile_hash",
            require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        object.__setattr__(
            self,
            "event_stream_sha256",
            require_sha256(self.event_stream_sha256, "events.sha256"),
        )
        object.__setattr__(
            self,
            "event_stream_bytes",
            require_int(self.event_stream_bytes, "events.bytes", minimum=1),
        )
        if not isinstance(self.frames, Sequence) or isinstance(
            self.frames, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("frames must be an array")
        frozen_frames = tuple(self.frames)
        if not frozen_frames or any(not isinstance(item, RawReplayFrame) for item in frozen_frames):
            raise AutonomyContractError("frames must contain RawReplayFrame values")
        object.__setattr__(self, "frames", frozen_frames)
        if not isinstance(self.telemetry_samples, Sequence) or isinstance(
            self.telemetry_samples, (str, bytes, bytearray)
        ):
            raise AutonomyContractError("telemetry_samples must be an array")
        frozen_telemetry = tuple(self.telemetry_samples)
        if not frozen_telemetry or any(
            not isinstance(item, RawReplayTelemetrySample) for item in frozen_telemetry
        ):
            raise AutonomyContractError(
                "telemetry_samples must contain RawReplayTelemetrySample values"
            )
        object.__setattr__(self, "telemetry_samples", frozen_telemetry)

        frame_ids = tuple(item.frame_id for item in frozen_frames)
        if len(frame_ids) != len(set(frame_ids)):
            raise AutonomyContractError("raw replay frame_id values must be unique")
        frame_times = tuple(item.captured_at_s for item in frozen_frames)
        if any(
            current <= previous
            for previous, current in zip(frame_times, frame_times[1:], strict=False)
        ):
            raise AutonomyContractError("raw replay frame timestamps must be strictly increasing")
        telemetry_times = tuple(item.observed_at_s for item in frozen_telemetry)
        if any(
            current <= previous
            for previous, current in zip(telemetry_times, telemetry_times[1:], strict=False)
        ):
            raise AutonomyContractError(
                "raw replay telemetry timestamps must be strictly increasing"
            )
        if telemetry_times[0] > frame_times[0]:
            raise AutonomyContractError(
                "raw replay requires telemetry at or before the first frame"
            )

        scenario_id, scenario_manifest_hash, scenario_seed = _scenario_identity(
            self.scenario_id,
            self.scenario_manifest_hash,
            self.scenario_seed,
        )
        object.__setattr__(self, "scenario_id", scenario_id)
        object.__setattr__(self, "scenario_manifest_hash", scenario_manifest_hash)
        object.__setattr__(self, "scenario_seed", scenario_seed)

    @classmethod
    def from_jsonl(
        cls,
        path: str | Path,
        *,
        recording_id: str,
        airframe_profile_hash: str,
        started_at_utc: datetime | str,
        expected_sha256: str | None = None,
        jsonl_sha256: str | None = None,
        expected_byte_count: int | None = None,
        scenario_id: str | None = None,
        scenario_manifest_hash: str | None = None,
        scenario_seed: int | None = None,
        scenario_input: ScenarioRuntimeInput | None = None,
    ) -> RawSensorReplayRecording:
        source = Path(path).resolve(strict=True)
        root = source.parent.resolve(strict=True)
        expected_hash = _one_expected_hash(expected_sha256, jsonl_sha256)
        return cls._load_verified_jsonl(
            source,
            asset_root=root,
            recording_id=recording_id,
            airframe_profile_hash=airframe_profile_hash,
            started_at_utc=_require_utc_timestamp(started_at_utc, "started_at_utc"),
            expected_sha256=expected_hash,
            expected_byte_count=expected_byte_count,
            scenario_id=scenario_id,
            scenario_manifest_hash=scenario_manifest_hash,
            scenario_seed=scenario_seed,
            scenario_input=scenario_input,
        )

    @classmethod
    def from_manifest(
        cls,
        path: str | Path,
        *,
        expected_manifest_sha256: str | None = None,
        scenario_input: ScenarioRuntimeInput | None = None,
    ) -> RawSensorReplayRecording:
        manifest_path = Path(path).resolve(strict=True)
        manifest_root = manifest_path.parent.resolve(strict=True)
        manifest_bytes = manifest_path.read_bytes()
        if expected_manifest_sha256 is not None:
            expected = require_sha256(expected_manifest_sha256, "expected_manifest_sha256")
            observed = hashlib.sha256(manifest_bytes).hexdigest()
            if observed != expected:
                raise AutonomyContractError(
                    f"raw replay manifest hash mismatch: expected {expected}, observed {observed}"
                )
        raw = _strict_json_loads(manifest_bytes, name="raw replay manifest")
        value = strict_object(
            raw,
            name="RawSensorReplayManifest",
            required=frozenset(
                {
                    "schema_version",
                    "recording_id",
                    "airframe_profile_hash",
                    "started_at_utc",
                    "recording_hash",
                    "events",
                }
            ),
            optional=frozenset({"scenario"}),
        )
        require_schema_version(value["schema_version"])
        events = strict_object(
            value["events"],
            name="RawSensorReplayEventStreamReference",
            required=frozenset({"path", "sha256", "bytes"}),
        )
        event_relative_path = _strict_relative_path(events["path"], "events.path")
        event_path = _resolve_contained_file(manifest_root, event_relative_path, name="events")
        scenario_id: str | None = None
        scenario_manifest_hash: str | None = None
        scenario_seed: int | None = None
        if "scenario" in value:
            scenario = strict_object(
                value["scenario"],
                name="RawSensorReplayScenarioBinding",
                required=frozenset({"scenario_id", "scenario_manifest_hash", "scenario_seed"}),
            )
            scenario_id = require_text(scenario["scenario_id"], "scenario_id")
            scenario_manifest_hash = require_sha256(
                scenario["scenario_manifest_hash"], "scenario_manifest_hash"
            )
            scenario_seed = require_int(scenario["scenario_seed"], "scenario_seed", minimum=0)
        recording = cls._load_verified_jsonl(
            event_path,
            asset_root=manifest_root,
            recording_id=require_text(value["recording_id"], "recording_id"),
            airframe_profile_hash=require_sha256(
                value["airframe_profile_hash"], "airframe_profile_hash"
            ),
            started_at_utc=_require_utc_timestamp(value["started_at_utc"], "started_at_utc"),
            expected_sha256=require_sha256(events["sha256"], "events.sha256"),
            expected_byte_count=require_int(events["bytes"], "events.bytes", minimum=1),
            scenario_id=scenario_id,
            scenario_manifest_hash=scenario_manifest_hash,
            scenario_seed=scenario_seed,
            scenario_input=scenario_input,
        )
        expected_recording_hash = require_sha256(value["recording_hash"], "recording_hash")
        if recording.recording_hash != expected_recording_hash:
            raise AutonomyContractError(
                "raw replay recording_hash does not match the verified event stream"
            )
        return recording

    @classmethod
    def _load_verified_jsonl(
        cls,
        path: Path,
        *,
        asset_root: Path,
        recording_id: str,
        airframe_profile_hash: str,
        started_at_utc: datetime,
        expected_sha256: str,
        expected_byte_count: int | None,
        scenario_id: str | None,
        scenario_manifest_hash: str | None,
        scenario_seed: int | None,
        scenario_input: ScenarioRuntimeInput | None,
    ) -> RawSensorReplayRecording:
        try:
            path.relative_to(asset_root)
        except ValueError as exc:
            raise AutonomyContractError("events path escapes recording directory") from exc
        event_bytes = _read_verified_file(
            path,
            expected_sha256=expected_sha256,
            expected_byte_count=expected_byte_count,
            name="raw replay event stream",
        )
        try:
            text = event_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AutonomyContractError("raw replay event stream must be UTF-8") from exc
        lines = text.splitlines()
        if not lines:
            raise AutonomyContractError("raw replay event stream cannot be empty")

        parsed_frames: list[RawReplayFrame] = []
        parsed_telemetry: list[RawReplayTelemetrySample] = []
        previous_event_at_s = -1.0
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                raise AutonomyContractError(
                    f"raw replay event stream line {line_number} cannot be blank"
                )
            raw = _strict_json_loads(
                line.encode("utf-8"),
                name=f"raw replay event stream line {line_number}",
            )
            if not isinstance(raw, Mapping):
                raise AutonomyContractError(
                    f"raw replay event stream line {line_number} must be an object"
                )
            kind = require_text(raw.get("kind"), f"line {line_number} kind")
            if kind == "frame":
                frame = RawReplayFrame.from_mapping(raw, asset_root=asset_root)
                event_at_s = frame.captured_at_s
                parsed_frames.append(frame)
            elif kind == "telemetry":
                sample = RawReplayTelemetrySample.from_mapping(raw)
                event_at_s = sample.observed_at_s
                parsed_telemetry.append(sample)
            else:
                raise AutonomyContractError(
                    f"raw replay event stream line {line_number} has unsupported kind {kind!r}"
                )
            if event_at_s < previous_event_at_s:
                raise AutonomyContractError(
                    "raw replay events must be ordered by their monotonic timestamp"
                )
            previous_event_at_s = event_at_s

        scenario_id, scenario_manifest_hash, scenario_seed = _scenario_identity(
            scenario_id, scenario_manifest_hash, scenario_seed
        )
        _validate_scenario_input(
            scenario_input,
            scenario_id=scenario_id,
            scenario_manifest_hash=scenario_manifest_hash,
            scenario_seed=scenario_seed,
        )
        return cls(
            recording_id=recording_id,
            airframe_profile_hash=airframe_profile_hash,
            started_at_utc=started_at_utc,
            event_stream_sha256=expected_sha256,
            event_stream_bytes=len(event_bytes),
            frames=tuple(parsed_frames),
            telemetry_samples=tuple(parsed_telemetry),
            scenario_id=scenario_id,
            scenario_manifest_hash=scenario_manifest_hash,
            scenario_seed=scenario_seed,
        )

    def identity_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "airframe_profile_hash": self.airframe_profile_hash,
            "started_at_utc": _format_utc_timestamp(self.started_at_utc),
            "events": {
                "sha256": self.event_stream_sha256,
                "bytes": self.event_stream_bytes,
            },
            "frames": [item.to_mapping() for item in self.frames],
            "telemetry": [item.to_mapping() for item in self.telemetry_samples],
        }
        if self.scenario_id is not None:
            result["scenario"] = {
                "scenario_id": self.scenario_id,
                "scenario_manifest_hash": self.scenario_manifest_hash,
                "scenario_seed": self.scenario_seed,
            }
        return result

    @property
    def recording_hash(self) -> str:
        """Content identity independent of the caller-provided recording label."""

        return canonical_sha256(self.identity_mapping())


class ReplayFrameSource:
    """Single-pass FrameSource which advances the shared replay clock."""

    def __init__(
        self,
        recording: RawSensorReplayRecording,
        clock: ManualClock,
        *,
        image_decoder: ImageDecoder | None = None,
    ) -> None:
        if not isinstance(recording, RawSensorReplayRecording):
            raise TypeError("ReplayFrameSource recording must be RawSensorReplayRecording")
        if not isinstance(clock, ManualClock):
            raise TypeError("ReplayFrameSource requires a ManualClock")
        if image_decoder is not None and not callable(image_decoder):
            raise TypeError("image_decoder must be callable or None")
        if clock.monotonic() > recording.frames[0].captured_at_s:
            raise ValueError("ManualClock starts after the first raw replay frame")
        self.recording = recording
        self.clock = clock
        self._image_decoder = image_decoder or _decode_image_bgr
        self._next_index = 0
        self._opened = False
        self._closed = False
        self._iterator_claimed = False
        self._lock = RLock()

    @property
    def frame_count(self) -> int:
        """Verified total available to launchers before the single pass starts."""

        return len(self.recording.frames)

    def open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("ReplayFrameSource is closed")
            self._opened = True

    def read(self) -> CapturedFrame:
        with self._lock:
            if self._closed:
                raise RuntimeError("ReplayFrameSource is closed")
            if not self._opened:
                raise RuntimeError("ReplayFrameSource is not open")
            if self._next_index >= len(self.recording.frames):
                raise RuntimeError("ReplayFrameSource is exhausted")
            entry = self.recording.frames[self._next_index]
            image_bytes = entry.image.read_verified_bytes()
            image_bgr = self._image_decoder(image_bytes, entry.image)
            _validate_decoded_image(image_bgr, entry.image)
            self.clock.advance_to(entry.captured_at_s)
            self._next_index += 1
            return CapturedFrame(
                frame_id=entry.frame_id,
                captured_at_s=entry.captured_at_s,
                image_bgr=image_bgr,
                width=entry.image.width,
                height=entry.image.height,
            )

    def frames(self) -> Iterator[CapturedFrame]:
        with self._lock:
            if self._closed:
                raise RuntimeError("ReplayFrameSource is closed")
            if self._iterator_claimed:
                raise RuntimeError("ReplayFrameSource iterators are single-pass")
            self._iterator_claimed = True
        self.open()
        while True:
            with self._lock:
                if self._closed:
                    raise RuntimeError("ReplayFrameSource is closed")
                if self._next_index >= len(self.recording.frames):
                    return
            yield self.read()

    def __iter__(self) -> Iterator[CapturedFrame]:
        return self.frames()

    def close(self) -> None:
        with self._lock:
            self._closed = True


class ReplayTelemetryProvider:
    """Return the newest recorded telemetry at the ManualClock replay time."""

    def __init__(
        self,
        recording: RawSensorReplayRecording,
        clock: ManualClock,
        *,
        maximum_age_s: float = _DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
        maximum_clock_skew_s: float = _DEFAULT_MAXIMUM_CLOCK_SKEW_S,
    ) -> None:
        if not isinstance(recording, RawSensorReplayRecording):
            raise TypeError("ReplayTelemetryProvider recording must be RawSensorReplayRecording")
        if not isinstance(clock, ManualClock):
            raise TypeError("ReplayTelemetryProvider requires a ManualClock")
        self.recording = recording
        self.clock = clock
        self.maximum_age_s = require_real(
            maximum_age_s,
            "maximum_age_s",
            minimum=0.0,
        )
        self.maximum_clock_skew_s = require_real(
            maximum_clock_skew_s,
            "maximum_clock_skew_s",
            minimum=0.0,
        )
        self._times = tuple(item.observed_at_s for item in recording.telemetry_samples)
        self._closed = False
        self._lock = RLock()

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        requested_at_s = require_real(now_s, "now_s", minimum=0.0)
        with self._lock:
            if self._closed:
                raise RuntimeError("ReplayTelemetryProvider is closed")
            replay_at_s = self.clock.monotonic()
            if abs(requested_at_s - replay_at_s) > self.maximum_clock_skew_s:
                raise RuntimeError(
                    "telemetry now_s is not synchronized with the shared replay clock"
                )
            index = bisect_right(self._times, replay_at_s) - 1
            if index < 0:
                raise RuntimeError("no replay telemetry is available at the current clock time")
            sample = self.recording.telemetry_samples[index]
            age_s = replay_at_s - sample.observed_at_s
            if age_s > self.maximum_age_s:
                raise RuntimeError(
                    f"replay telemetry is stale: age {age_s:.6f}s exceeds {self.maximum_age_s:.6f}s"
                )
            return sample.telemetry

    def close(self) -> None:
        with self._lock:
            self._closed = True


RawReplayFrameSource = ReplayFrameSource
RawReplayTelemetryProvider = ReplayTelemetryProvider


class RawSensorReplayBackend:
    """Concrete REPLAY binding for the pre-detection live pipeline."""

    def __init__(
        self,
        recording: RawSensorReplayRecording,
        *,
        descriptor: RuntimeDescriptor | None = None,
        clock: ManualClock | None = None,
        image_decoder: ImageDecoder | None = None,
        maximum_telemetry_age_s: float = _DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
        maximum_clock_skew_s: float = _DEFAULT_MAXIMUM_CLOCK_SKEW_S,
    ) -> None:
        if not isinstance(recording, RawSensorReplayRecording):
            raise TypeError("RawSensorReplayBackend recording must be RawSensorReplayRecording")
        selected_clock = clock or ManualClock(initial_utc=recording.started_at_utc)
        if not isinstance(selected_clock, ManualClock):
            raise TypeError("RawSensorReplayBackend requires a ManualClock")
        _validate_clock_anchor(selected_clock, recording)
        selected_descriptor = descriptor or RuntimeDescriptor(
            mode=RuntimeMode.REPLAY,
            profile=RuntimeProfile.replay(),
            backend_id=f"raw-replay:{recording.recording_id}",
            airframe_profile_hash=recording.airframe_profile_hash,
            scenario_id=recording.scenario_id,
            scenario_manifest_hash=recording.scenario_manifest_hash,
            scenario_seed=recording.scenario_seed,
            recording_hash=recording.recording_hash,
            deterministic=True,
            fault_injection_enabled=False,
        )
        _validate_descriptor(selected_descriptor, recording)
        self.recording = recording
        self.descriptor = selected_descriptor
        self.clock = selected_clock
        self.frame_source = ReplayFrameSource(
            recording,
            selected_clock,
            image_decoder=image_decoder,
        )
        self.telemetry_provider = ReplayTelemetryProvider(
            recording,
            selected_clock,
            maximum_age_s=maximum_telemetry_age_s,
            maximum_clock_skew_s=maximum_clock_skew_s,
        )
        self._closed = False

    @classmethod
    def from_manifest(
        cls,
        path: str | Path,
        *,
        expected_manifest_sha256: str | None = None,
        scenario_input: ScenarioRuntimeInput | None = None,
        clock: ManualClock | None = None,
        image_decoder: ImageDecoder | None = None,
        maximum_telemetry_age_s: float = _DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
        maximum_clock_skew_s: float = _DEFAULT_MAXIMUM_CLOCK_SKEW_S,
    ) -> RawSensorReplayBackend:
        return cls(
            RawSensorReplayRecording.from_manifest(
                path,
                expected_manifest_sha256=expected_manifest_sha256,
                scenario_input=scenario_input,
            ),
            clock=clock,
            image_decoder=image_decoder,
            maximum_telemetry_age_s=maximum_telemetry_age_s,
            maximum_clock_skew_s=maximum_clock_skew_s,
        )

    from_recording_manifest = from_manifest

    @classmethod
    def from_jsonl(
        cls,
        path: str | Path,
        *,
        recording_id: str,
        airframe_profile_hash: str,
        started_at_utc: datetime | str,
        expected_sha256: str | None = None,
        jsonl_sha256: str | None = None,
        expected_byte_count: int | None = None,
        scenario_id: str | None = None,
        scenario_manifest_hash: str | None = None,
        scenario_seed: int | None = None,
        scenario_input: ScenarioRuntimeInput | None = None,
        clock: ManualClock | None = None,
        image_decoder: ImageDecoder | None = None,
        maximum_telemetry_age_s: float = _DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
        maximum_clock_skew_s: float = _DEFAULT_MAXIMUM_CLOCK_SKEW_S,
    ) -> RawSensorReplayBackend:
        return cls(
            RawSensorReplayRecording.from_jsonl(
                path,
                recording_id=recording_id,
                airframe_profile_hash=airframe_profile_hash,
                started_at_utc=started_at_utc,
                expected_sha256=expected_sha256,
                jsonl_sha256=jsonl_sha256,
                expected_byte_count=expected_byte_count,
                scenario_id=scenario_id,
                scenario_manifest_hash=scenario_manifest_hash,
                scenario_seed=scenario_seed,
                scenario_input=scenario_input,
            ),
            clock=clock,
            image_decoder=image_decoder,
            maximum_telemetry_age_s=maximum_telemetry_age_s,
            maximum_clock_skew_s=maximum_clock_skew_s,
        )

    @property
    def frame_count(self) -> int:
        """Number of integrity-verified raw frames in this replay."""

        return self.frame_source.frame_count

    @property
    def input_hash(self) -> str:
        return self.recording.recording_hash

    def close(self) -> None:
        """Idempotent convenience; LiveMissionRunner remains resource owner."""

        if self._closed:
            return
        self._closed = True
        self.frame_source.close()
        self.telemetry_provider.close()


def _validate_descriptor(
    descriptor: RuntimeDescriptor,
    recording: RawSensorReplayRecording,
) -> None:
    if not isinstance(descriptor, RuntimeDescriptor):
        raise TypeError("RawSensorReplayBackend descriptor must be a RuntimeDescriptor")
    if descriptor.mode is not RuntimeMode.REPLAY:
        raise ValueError("RawSensorReplayBackend requires a replay RuntimeDescriptor")
    if descriptor.profile != RuntimeProfile.replay():
        raise ValueError("RawSensorReplayBackend requires the exact replay RuntimeProfile")
    if descriptor.backend_id != f"raw-replay:{recording.recording_id}":
        raise ValueError("RawSensorReplayBackend descriptor backend_id does not match recording")
    if not descriptor.deterministic:
        raise ValueError("RawSensorReplayBackend descriptor must be deterministic")
    if descriptor.fault_injection_enabled:
        raise ValueError("RawSensorReplayBackend cannot inject faults during playback")
    if descriptor.recording_hash != recording.recording_hash:
        raise ValueError(
            "RawSensorReplayBackend descriptor recording_hash does not match recording"
        )
    if descriptor.airframe_profile_hash != recording.airframe_profile_hash:
        raise ValueError(
            "RawSensorReplayBackend descriptor airframe_profile_hash does not match recording"
        )
    descriptor_scenario = (
        descriptor.scenario_id,
        descriptor.scenario_manifest_hash,
        descriptor.scenario_seed,
    )
    recording_scenario = (
        recording.scenario_id,
        recording.scenario_manifest_hash,
        recording.scenario_seed,
    )
    if descriptor_scenario != recording_scenario:
        raise ValueError("RawSensorReplayBackend descriptor scenario does not match recording")


def _require_utc_timestamp(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = require_text(value, name)
        if _UTC_TIMESTAMP_PATTERN.fullmatch(text) is None:
            raise AutonomyContractError(f"{name} must be an RFC3339 timestamp with Z or +00:00")
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise AutonomyContractError(f"{name} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomyContractError(f"{name} must be timezone-aware UTC")
    if parsed.utcoffset() != timedelta(0):
        raise AutonomyContractError(f"{name} must use the UTC offset")
    return parsed.astimezone(UTC)


def _format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_clock_anchor(clock: ManualClock, recording: RawSensorReplayRecording) -> None:
    expected_utc = recording.started_at_utc + timedelta(seconds=clock.monotonic())
    skew_s = abs((clock.utc_now() - expected_utc).total_seconds())
    if skew_s > _DEFAULT_MAXIMUM_CLOCK_SKEW_S:
        raise ValueError(
            "ManualClock UTC does not match the raw replay recording's started_at_utc anchor"
        )


def _scenario_identity(
    scenario_id: str | None,
    scenario_manifest_hash: str | None,
    scenario_seed: int | None,
) -> tuple[str | None, str | None, int | None]:
    values = (scenario_id, scenario_manifest_hash, scenario_seed)
    if any(value is None for value in values) != all(value is None for value in values):
        raise AutonomyContractError(
            "raw replay scenario binding requires id, manifest hash and seed together"
        )
    if scenario_id is None:
        return None, None, None
    return (
        require_text(scenario_id, "scenario_id"),
        require_sha256(scenario_manifest_hash, "scenario_manifest_hash"),
        require_int(scenario_seed, "scenario_seed", minimum=0),
    )


def _validate_scenario_input(
    scenario_input: ScenarioRuntimeInput | None,
    *,
    scenario_id: str | None,
    scenario_manifest_hash: str | None,
    scenario_seed: int | None,
) -> None:
    if scenario_input is None:
        return
    if not isinstance(scenario_input, ScenarioRuntimeInput):
        raise TypeError("scenario_input must be ScenarioRuntimeInput or None")
    expected = (
        scenario_input.scenario_id,
        scenario_input.scenario_manifest_hash,
        scenario_input.seed,
    )
    if expected != (scenario_id, scenario_manifest_hash, scenario_seed):
        raise AutonomyContractError(
            "raw replay scenario binding does not match the validated ScenarioRuntimeInput"
        )


def _one_expected_hash(
    expected_sha256: str | None,
    jsonl_sha256: str | None,
) -> str:
    if expected_sha256 is None and jsonl_sha256 is None:
        raise AutonomyContractError("raw replay JSONL requires expected_sha256 (or jsonl_sha256)")
    first = (
        require_sha256(expected_sha256, "expected_sha256") if expected_sha256 is not None else None
    )
    second = require_sha256(jsonl_sha256, "jsonl_sha256") if jsonl_sha256 is not None else None
    if first is not None and second is not None and first != second:
        raise AutonomyContractError("expected_sha256 and jsonl_sha256 disagree")
    return first or second or ""  # both-None is rejected above


def _strict_relative_path(value: object, name: str) -> str:
    text = require_text(value, name).replace("\\", "/")
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
    candidate = (base / Path(*PurePosixPath(relative_path).parts)).resolve(strict=True)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} path escapes recording directory") from exc
    if not candidate.is_file():
        raise AutonomyContractError(f"{name} path must name a regular file")
    return candidate


def _read_verified_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_byte_count: int | None,
    name: str,
) -> bytes:
    expected_hash = require_sha256(expected_sha256, f"{name} sha256")
    if expected_byte_count is not None:
        expected_bytes = require_int(expected_byte_count, f"{name} bytes", minimum=1)
    else:
        expected_bytes = None
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise AutonomyContractError(f"unable to read {name}: {exc}") from exc
    if expected_bytes is not None and len(payload) != expected_bytes:
        raise AutonomyContractError(
            f"{name} byte count mismatch: expected {expected_bytes}, observed {len(payload)}"
        )
    observed_hash = hashlib.sha256(payload).hexdigest()
    if observed_hash != expected_hash:
        raise AutonomyContractError(
            f"{name} hash mismatch: expected {expected_hash}, observed {observed_hash}"
        )
    return payload


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


def _nullable_bool(value: object, name: str) -> bool | None:
    if value is None:
        return None
    return require_bool(value, name)


def _nullable_real(value: object, name: str) -> float:
    if value is None:
        return float("nan")
    minimum = 0.0 if name in _NON_NEGATIVE_REAL_FIELDS else None
    maximum = 100.0 if name == "battery_remaining_pct" else None
    if name == "latitude_deg":
        minimum, maximum = -90.0, 90.0
    elif name == "longitude_deg":
        minimum, maximum = -180.0, 180.0
    elif name == "heading_deg":
        minimum, maximum = 0.0, 360.0
    return require_real(value, name, minimum=minimum, maximum=maximum)


def _nullable_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    if name == "satellites_visible":
        # MAVLink encodes an unknown satellite count as the wire sentinel 255;
        # never expose that sentinel as a real usable count to the positioning
        # and quality gates.
        parsed = require_int(value, name, minimum=0, maximum=MAVLINK_SATELLITES_UNKNOWN)
        return normalize_satellites_visible(parsed)
    maximum = MAVLINK_GPS_FIX_TYPE_MAX if name == "gps_fix_type" else None
    return require_int(value, name, minimum=0, maximum=maximum)


def _vehicle_telemetry_from_mapping(raw: object) -> VehicleTelemetry:
    value = strict_object(
        raw,
        name="VehicleTelemetry",
        required=_REQUIRED_TELEMETRY_FIELDS,
        optional=_ALL_TELEMETRY_FIELDS - _REQUIRED_TELEMETRY_FIELDS,
    )
    normalized: dict[str, object] = {}
    for name in _ALL_TELEMETRY_FIELDS:
        raw_value = value.get(name)
        if name in _NULLABLE_BOOLEAN_FIELDS:
            normalized[name] = _nullable_bool(raw_value, name)
        elif name in _NULLABLE_INTEGER_FIELDS:
            normalized[name] = _nullable_int(raw_value, name)
        elif name in _NULLABLE_TEXT_FIELDS:
            normalized[name] = optional_text(raw_value, name)
        else:
            normalized[name] = _nullable_real(raw_value, name)
    return VehicleTelemetry(**normalized)


def _vehicle_telemetry_to_mapping(telemetry: VehicleTelemetry) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in fields(VehicleTelemetry):
        value = getattr(telemetry, item.name)
        if isinstance(value, float) and not math.isfinite(value):
            result[item.name] = None
        else:
            result[item.name] = value
    # These fields were added after schema-v1 recordings already existed.
    # Omit an entirely absent wire-observation pair so opening and re-hashing
    # a legacy JSONL stream stays byte/identity compatible.  Once either side
    # of a pair was observed, retain both values (including a fail-closed null)
    # so a rejected/stale Mission frame cannot be disguised as legacy input.
    if not math.isfinite(telemetry.mission_sequence_observed_at_s):
        result.pop("mission_sequence_observed_at_s")
    if (
        telemetry.mission_item_reached_sequence is None
        and not math.isfinite(telemetry.mission_item_reached_observed_at_s)
    ):
        result.pop("mission_item_reached_sequence")
        result.pop("mission_item_reached_observed_at_s")
    if not math.isfinite(telemetry.gps_absolute_altitude_m):
        result.pop("gps_absolute_altitude_m")
    return result


def _decode_image_bgr(payload: bytes, reference: RawReplayImageReference) -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - depends on optional vision extra
        raise RuntimeError("raw image replay requires the vision extra (numpy)") from exc
    if reference.encoding == "npy":
        try:
            image = np.load(io.BytesIO(payload), allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise AutonomyContractError("raw replay NPY image cannot be decoded") from exc
    else:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - depends on optional vision extra
            raise RuntimeError("encoded raw image replay requires OpenCV") from exc
        image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise AutonomyContractError("raw replay image cannot be decoded")
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8:
        raise AutonomyContractError("raw replay image must decode to a uint8 ndarray")
    return np.ascontiguousarray(image)


def _validate_decoded_image(image: Any, reference: RawReplayImageReference) -> None:
    shape = getattr(image, "shape", None)
    if (
        not isinstance(shape, Sequence)
        or isinstance(shape, (str, bytes, bytearray))
        or len(shape) != 3
        or isinstance(shape[0], bool)
        or isinstance(shape[1], bool)
        or isinstance(shape[2], bool)
        or not all(isinstance(value, int) for value in shape)
        or tuple(shape) != (reference.height, reference.width, 3)
    ):
        raise AutonomyContractError(
            "raw replay image dimensions do not match manifest width/height/BGR channels"
        )


__all__ = [
    "RawReplayFrame",
    "RawReplayFrameSource",
    "RawReplayImageReference",
    "RawReplayTelemetryProvider",
    "RawReplayTelemetrySample",
    "RawSensorReplayBackend",
    "RawSensorReplayRecording",
    "ReplayFrameSource",
    "ReplayTelemetryProvider",
]
