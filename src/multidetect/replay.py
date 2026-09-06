from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from math import isfinite, isnan
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .autonomy.serialization import canonical_json, canonical_sha256, require_sha256
from .deployment_planner import PrimaryRangeEvidence
from .domain import BoundingBox, Detection, FrameObservation, SensorKind, VehicleTelemetry
from .mavlink_gps import normalize_gps_fix_type, normalize_satellites_visible
from .multimodal_ranging import RangeSolution, RangeValidity

_LABEL_ALIASES = {"fire": "flame"}


@dataclass(frozen=True, slots=True)
class ReplayRecording:
    """Immutable post-detection observation recording with no evaluator truth.

    Every frame already carries detections and normalized vehicle telemetry.
    This format is not a raw image/video recording and does not by itself prove
    production-detector parity.
    """

    recording_id: str
    frames: tuple[FrameObservation, ...]
    airframe_profile_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.recording_id, str) or not self.recording_id.strip():
            raise ValueError("recording_id cannot be empty")
        object.__setattr__(self, "recording_id", self.recording_id.strip())
        object.__setattr__(
            self,
            "airframe_profile_hash",
            require_sha256(self.airframe_profile_hash, "airframe_profile_hash"),
        )
        if not isinstance(self.frames, Sequence) or isinstance(
            self.frames, (str, bytes, bytearray)
        ):
            raise ValueError("replay frames must be an array")
        normalized_frames = tuple(_freeze_frame(frame) for frame in self.frames)
        object.__setattr__(self, "frames", normalized_frames)
        previous_timestamp: float | None = None
        seen_ids: set[str] = set()
        for frame in self.frames:
            if frame.frame_id in seen_ids:
                raise ValueError(f"duplicate replay frame_id: {frame.frame_id}")
            if previous_timestamp is not None and frame.captured_at_s <= previous_timestamp:
                raise ValueError("replay timestamps must increase strictly")
            seen_ids.add(frame.frame_id)
            previous_timestamp = frame.captured_at_s

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "recording_id": self.recording_id,
            "airframe_profile_hash": self.airframe_profile_hash,
            "frames": [_frame_to_mapping(frame) for frame in self.frames],
        }

    @property
    def canonical_json(self) -> str:
        return canonical_json(self.to_mapping())

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def recording_hash(self) -> str:
        """Content digest independent of the caller-provided recording label."""

        return canonical_sha256(
            {
                "schema_version": 1,
                "frames": [_frame_to_mapping(frame) for frame in self.frames],
            }
        )

    @classmethod
    def from_jsonl(
        cls,
        path: str | Path,
        *,
        airframe_profile_hash: str,
        recording_id: str | None = None,
    ) -> ReplayRecording:
        source = Path(path)
        return cls(
            recording_id=recording_id or source.stem,
            frames=load_jsonl_replay(source),
            airframe_profile_hash=airframe_profile_hash,
        )


_TELEMETRY_OPTIONAL_BOOLS = frozenset(
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
_TELEMETRY_OPTIONAL_INTS = frozenset(
    {
        "satellites_visible",
        "gps_fix_type",
        "mission_sequence",
        "mission_item_reached_sequence",
    }
)
_TELEMETRY_OPTIONAL_TEXT = frozenset(
    {"flight_mode", "altitude_reference", "attitude_uncertainty_source"}
)
_TELEMETRY_REQUIRED_FINITE_FLOATS = frozenset(
    {"altitude_agl_m", "roll_deg", "pitch_deg", "ground_speed_mps"}
)


def _freeze_frame(frame: object) -> FrameObservation:
    if not isinstance(frame, FrameObservation):
        raise ValueError("replay frames must contain FrameObservation values")
    if not isinstance(frame.frame_id, str) or not frame.frame_id.strip():
        raise ValueError("replay frame_id must be non-empty text")
    if isinstance(frame.captured_at_s, bool) or not isinstance(frame.captured_at_s, int | float):
        raise ValueError("replay captured_at_s must be numeric")
    if not isinstance(frame.detections, Sequence) or isinstance(
        frame.detections, (str, bytes, bytearray)
    ):
        raise ValueError("frame detections must be an array")
    detections = tuple(_freeze_detection(item) for item in frame.detections)
    return FrameObservation(
        frame_id=frame.frame_id.strip(),
        captured_at_s=float(frame.captured_at_s),
        detections=detections,
        telemetry=_freeze_telemetry(frame.telemetry),
    )


def _freeze_detection(value: object) -> Detection:
    if not isinstance(value, Detection):
        raise ValueError("frame detections must contain Detection values")
    if not isinstance(value.bbox, BoundingBox):
        raise ValueError("detection bbox must be BoundingBox")
    if not isinstance(value.label, str):
        raise ValueError("detection label must be text")
    if isinstance(value.confidence, bool) or not isinstance(value.confidence, int | float):
        raise ValueError("detection confidence must be numeric")
    for coordinate in (value.bbox.x1, value.bbox.y1, value.bbox.x2, value.bbox.y2):
        if isinstance(coordinate, bool) or not isinstance(coordinate, int | float):
            raise ValueError("detection bbox coordinates must be numeric")
    if not isinstance(value.sensor, SensorKind):
        raise ValueError("detection sensor must be SensorKind")
    if not isinstance(value.model_version, str):
        raise ValueError("detection model_version must be text")
    model_version = value.model_version.strip()
    if not model_version:
        raise ValueError("detection model_version cannot be empty")
    metadata = _freeze_json(value.metadata, "detection.metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("detection metadata must be an object")
    return Detection(
        label=value.label,
        confidence=float(value.confidence),
        bbox=BoundingBox(
            float(value.bbox.x1),
            float(value.bbox.y1),
            float(value.bbox.x2),
            float(value.bbox.y2),
        ),
        sensor=value.sensor,
        model_version=model_version,
        metadata=metadata,
    )


def _freeze_telemetry(value: object) -> VehicleTelemetry:
    if not isinstance(value, VehicleTelemetry):
        raise ValueError("frame telemetry must be VehicleTelemetry")
    normalized: dict[str, Any] = {}
    for item in fields(VehicleTelemetry):
        name = item.name
        raw = getattr(value, name)
        if name in _TELEMETRY_OPTIONAL_BOOLS:
            if raw is not None and type(raw) is not bool:
                raise ValueError(f"telemetry {name} must be boolean or null")
            normalized[name] = raw
        elif name in _TELEMETRY_OPTIONAL_INTS:
            if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int)):
                raise ValueError(f"telemetry {name} must be integer or null")
            normalized[name] = raw
        elif name in _TELEMETRY_OPTIONAL_TEXT:
            if raw is None:
                normalized[name] = None
            elif not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"telemetry {name} must be non-empty text or null")
            else:
                normalized[name] = raw.strip()
        else:
            if isinstance(raw, bool) or not isinstance(raw, int | float):
                raise ValueError(f"telemetry {name} must be numeric")
            number = float(raw)
            if name in _TELEMETRY_REQUIRED_FINITE_FLOATS:
                if not isfinite(number):
                    raise ValueError(f"telemetry {name} must be finite")
            elif not isfinite(number) and not isnan(number):
                raise ValueError(f"telemetry {name} cannot be infinite")
            normalized[name] = number
    return VehicleTelemetry(**normalized)


def _freeze_json(value: object, path: str) -> object:
    if value is None or type(value) is bool or isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return float(value)
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            frozen[key] = _freeze_json(nested, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(nested, f"{path}[{index}]") for index, nested in enumerate(value))
    raise ValueError(f"{path} contains a non-JSON value")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(nested) for nested in value]
    return value


def _frame_to_mapping(frame: FrameObservation) -> dict[str, Any]:
    telemetry: dict[str, Any] = {}
    for item in fields(VehicleTelemetry):
        value = getattr(frame.telemetry, item.name)
        telemetry[item.name] = None if isinstance(value, float) and isnan(value) else value
    # Preserve schema-v1 recording identities created before PX4 Mission wire
    # timestamps existed.  New observations remain explicit, including a null
    # sequence paired with a timestamp after a malformed frame was rejected.
    if not isfinite(frame.telemetry.mission_sequence_observed_at_s):
        telemetry.pop("mission_sequence_observed_at_s")
    if frame.telemetry.mission_item_reached_sequence is None and not isfinite(
        frame.telemetry.mission_item_reached_observed_at_s
    ):
        telemetry.pop("mission_item_reached_sequence")
        telemetry.pop("mission_item_reached_observed_at_s")
    if not isfinite(frame.telemetry.gps_absolute_altitude_m):
        telemetry.pop("gps_absolute_altitude_m")
    if (
        not isfinite(frame.telemetry.attitude_roll_sigma_deg)
        and not isfinite(frame.telemetry.attitude_pitch_sigma_deg)
        and not isfinite(frame.telemetry.attitude_heading_sigma_deg)
        and frame.telemetry.attitude_uncertainty_source is None
    ):
        telemetry.pop("attitude_roll_sigma_deg")
        telemetry.pop("attitude_pitch_sigma_deg")
        telemetry.pop("attitude_heading_sigma_deg")
        telemetry.pop("attitude_uncertainty_source")
    detections = [
        {
            "label": item.label,
            "confidence": float(item.confidence),
            "bbox": [item.bbox.x1, item.bbox.y1, item.bbox.x2, item.bbox.y2],
            "sensor": item.sensor.value,
            "model_version": item.model_version,
            "metadata": _thaw_json(item.metadata),
        }
        for item in frame.detections
    ]
    return {
        "frame_id": frame.frame_id,
        "captured_at_s": float(frame.captured_at_s),
        "telemetry": telemetry,
        "detections": detections,
    }


def frame_from_mapping(raw: Mapping[str, Any]) -> FrameObservation:
    telemetry_raw = raw["telemetry"]
    telemetry = VehicleTelemetry(
        altitude_agl_m=float(telemetry_raw["altitude_agl_m"]),
        roll_deg=float(telemetry_raw["roll_deg"]),
        pitch_deg=float(telemetry_raw["pitch_deg"]),
        ground_speed_mps=float(telemetry_raw["ground_speed_mps"]),
        in_allowed_zone=_optional_bool(telemetry_raw.get("in_allowed_zone")),
        geofence_healthy=_optional_bool(telemetry_raw.get("geofence_healthy")),
        position_healthy=_optional_bool(telemetry_raw.get("position_healthy")),
        link_healthy=_optional_bool(telemetry_raw.get("link_healthy")),
        flight_mode_allows_deploy=_optional_bool(telemetry_raw.get("flight_mode_allows_deploy")),
        release_zone_clear=_optional_bool(telemetry_raw.get("release_zone_clear")),
        person_detector_healthy=_optional_bool(telemetry_raw.get("person_detector_healthy")),
        latitude_deg=_optional_float(telemetry_raw.get("latitude_deg")),
        longitude_deg=_optional_float(telemetry_raw.get("longitude_deg")),
        heading_deg=_optional_float(telemetry_raw.get("heading_deg")),
        battery_remaining_pct=_optional_float(telemetry_raw.get("battery_remaining_pct")),
        satellites_visible=normalize_satellites_visible(
            _optional_int(telemetry_raw.get("satellites_visible"))
        ),
        gps_fix_type=normalize_gps_fix_type(_optional_int(telemetry_raw.get("gps_fix_type"))),
        gps_horizontal_accuracy_m=_optional_float(telemetry_raw.get("gps_horizontal_accuracy_m")),
        gps_vertical_accuracy_m=_optional_float(telemetry_raw.get("gps_vertical_accuracy_m")),
        gps_observed_at_s=_optional_float(telemetry_raw.get("gps_observed_at_s")),
        armed=_optional_bool(telemetry_raw.get("armed")),
        flight_mode=_optional_string(telemetry_raw.get("flight_mode")),
        mission_sequence=_optional_int(telemetry_raw.get("mission_sequence")),
        mission_sequence_observed_at_s=_optional_float(
            telemetry_raw.get("mission_sequence_observed_at_s")
        ),
        mission_item_reached_sequence=_optional_int(
            telemetry_raw.get("mission_item_reached_sequence")
        ),
        mission_item_reached_observed_at_s=_optional_float(
            telemetry_raw.get("mission_item_reached_observed_at_s")
        ),
        attitude_observed_at_s=_optional_float(telemetry_raw.get("attitude_observed_at_s")),
        attitude_roll_sigma_deg=_optional_float(telemetry_raw.get("attitude_roll_sigma_deg")),
        attitude_pitch_sigma_deg=_optional_float(telemetry_raw.get("attitude_pitch_sigma_deg")),
        attitude_heading_sigma_deg=_optional_float(telemetry_raw.get("attitude_heading_sigma_deg")),
        attitude_uncertainty_source=_optional_string(
            telemetry_raw.get("attitude_uncertainty_source")
        ),
        position_observed_at_s=_optional_float(telemetry_raw.get("position_observed_at_s")),
        velocity_north_mps=_optional_float(telemetry_raw.get("velocity_north_mps")),
        velocity_east_mps=_optional_float(telemetry_raw.get("velocity_east_mps")),
        acceleration_uncertainty_mps2=_optional_float(
            telemetry_raw.get("acceleration_uncertainty_mps2")
        ),
        airspeed_mps=_optional_float(telemetry_raw.get("airspeed_mps")),
        wind_north_mps=_optional_float(telemetry_raw.get("wind_north_mps")),
        wind_east_mps=_optional_float(telemetry_raw.get("wind_east_mps")),
        velocity_observed_at_s=_optional_float(telemetry_raw.get("velocity_observed_at_s")),
        airspeed_observed_at_s=_optional_float(telemetry_raw.get("airspeed_observed_at_s")),
        wind_observed_at_s=_optional_float(telemetry_raw.get("wind_observed_at_s")),
        altitude_observed_at_s=_optional_float(telemetry_raw.get("altitude_observed_at_s")),
        altitude_reference=_optional_string(telemetry_raw.get("altitude_reference")),
        local_north_m=_optional_float(telemetry_raw.get("local_north_m")),
        local_east_m=_optional_float(telemetry_raw.get("local_east_m")),
        local_down_m=_optional_float(telemetry_raw.get("local_down_m")),
        local_position_observed_at_s=_optional_float(
            telemetry_raw.get("local_position_observed_at_s")
        ),
        gps_absolute_altitude_m=_optional_float(telemetry_raw.get("gps_absolute_altitude_m")),
    )
    detections: list[Detection] = []
    for detection_raw in raw.get("detections", []):
        box = detection_raw["bbox"]
        if len(box) != 4:
            raise ValueError("detection bbox must contain four normalized XYXY values")
        raw_label = str(detection_raw["label"]).strip().lower()
        detections.append(
            Detection(
                label=_LABEL_ALIASES.get(raw_label, raw_label),
                confidence=float(detection_raw["confidence"]),
                bbox=BoundingBox(*(float(value) for value in box)),
                sensor=SensorKind(detection_raw.get("sensor", "rgb")),
                model_version=str(detection_raw.get("model_version", "replay")),
                metadata=dict(detection_raw.get("metadata", {})),
            )
        )
    return FrameObservation(
        frame_id=str(raw["frame_id"]),
        captured_at_s=float(raw["captured_at_s"]),
        detections=tuple(detections),
        telemetry=telemetry,
    )


def load_jsonl_replay(path: str | Path) -> tuple[FrameObservation, ...]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return tuple(iter_jsonl_replay(handle))


def primary_range_evidence_from_frame(
    frame: FrameObservation,
) -> PrimaryRangeEvidence | None:
    """Decode explicit synthetic HIL range evidence from one replay detection."""

    candidates = tuple(
        detection
        for detection in frame.detections
        if "primary_range_evidence" in detection.metadata
    )
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ValueError("replay frame must contain at most one primary range evidence record")
    detection = candidates[0]
    raw = detection.metadata["primary_range_evidence"]
    if not isinstance(raw, Mapping):
        raise ValueError("primary range evidence must be an object")
    validity = RangeValidity(str(raw["validity"]).strip().lower())
    ground_range = _optional_number(raw.get("ground_range_m"))
    ground_ci = _optional_interval(raw.get("ground_range_ci95_m"))
    slant_range = _optional_number(raw.get("slant_range_m"))
    slant_ci = _optional_interval(raw.get("slant_range_ci95_m"))
    source_target_id = str(raw["source_target_id"])
    solution = RangeSolution(
        target_id=source_target_id,
        frame_id=frame.frame_id,
        calibration_id=str(raw["calibration_id"]),
        evaluated_at_s=float(raw.get("evaluated_at_s", frame.captured_at_s)),
        validity=validity,
        reasons=_string_tuple(raw["reasons"], "range reasons"),
        sources=_string_tuple(raw.get("sources", ()), "range sources"),
        rejected_sources=_string_tuple(
            raw.get("rejected_sources", ()),
            "rejected range sources",
        ),
        slant_range_m=slant_range,
        ground_range_m=ground_range,
        slant_range_ci95_m=slant_ci,
        ground_range_ci95_m=ground_ci,
        relative_bearing_deg=_optional_number(raw.get("relative_bearing_deg")),
        absolute_bearing_deg=_optional_number(raw.get("absolute_bearing_deg")),
        bearing_sigma_deg=_optional_number(raw.get("bearing_sigma_deg")),
        north_offset_m=_optional_number(raw.get("north_offset_m")),
        east_offset_m=_optional_number(raw.get("east_offset_m")),
        data_freshness_s=_optional_number(raw.get("data_freshness_s")),
        sensor_consistency=float(raw.get("sensor_consistency", 0.0)),
    )
    return PrimaryRangeEvidence(
        source_target_id=source_target_id,
        source_frame_id=frame.frame_id,
        source_captured_at_s=frame.captured_at_s,
        source_label=detection.label,
        source_bbox=detection.bbox,
        solution=solution,
    )


def iter_jsonl_replay(lines: Iterable[str]) -> Iterator[FrameObservation]:
    previous_timestamp: float | None = None
    seen_frame_ids: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError("frame must be a JSON object")
            frame = frame_from_mapping(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid replay line {line_number}: {exc}") from exc
        if frame.frame_id in seen_frame_ids:
            raise ValueError(f"duplicate replay frame_id on line {line_number}: {frame.frame_id}")
        if previous_timestamp is not None and frame.captured_at_s <= previous_timestamp:
            raise ValueError(f"replay timestamps must increase strictly (line {line_number})")
        seen_frame_ids.add(frame.frame_id)
        previous_timestamp = frame.captured_at_s
        yield frame


def _optional_bool(value: Any) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("telemetry health values must be true, false, or null")


def _optional_float(value: Any) -> float:
    return float("nan") if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _optional_number(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_interval(value: Any) -> tuple[float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError("range confidence interval must contain two values")
    return float(value[0]), float(value[1])


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a string array")
    return tuple(value)
