from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from .domain import BoundingBox, Detection, FrameObservation, SensorKind, VehicleTelemetry

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
