from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from .deployment_planner import PrimaryRangeEvidence
from .domain import BoundingBox, Detection, FrameObservation, SensorKind, VehicleTelemetry
from .multimodal_ranging import RangeSolution, RangeValidity

_LABEL_ALIASES = {"fire": "flame"}


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
        satellites_visible=_optional_int(telemetry_raw.get("satellites_visible")),
        armed=_optional_bool(telemetry_raw.get("armed")),
        flight_mode=_optional_string(telemetry_raw.get("flight_mode")),
        mission_sequence=_optional_int(telemetry_raw.get("mission_sequence")),
        velocity_north_mps=_optional_float(telemetry_raw.get("velocity_north_mps")),
        velocity_east_mps=_optional_float(telemetry_raw.get("velocity_east_mps")),
        airspeed_mps=_optional_float(telemetry_raw.get("airspeed_mps")),
        wind_north_mps=_optional_float(telemetry_raw.get("wind_north_mps")),
        wind_east_mps=_optional_float(telemetry_raw.get("wind_east_mps")),
        velocity_observed_at_s=_optional_float(telemetry_raw.get("velocity_observed_at_s")),
        airspeed_observed_at_s=_optional_float(telemetry_raw.get("airspeed_observed_at_s")),
        wind_observed_at_s=_optional_float(telemetry_raw.get("wind_observed_at_s")),
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
