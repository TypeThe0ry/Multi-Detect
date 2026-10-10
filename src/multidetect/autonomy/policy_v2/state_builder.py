"""Adapters that turn live or recorded observations into state-v2 values."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .contracts import PolicyContractError, StructuredSceneState, TaskSpecV1
from .search_memory import SearchMemoryGrid


def _number(value: object, name: str, default: float = 0.0) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PolicyContractError(f"{name} must be finite")
    return float(value)


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name)


def _get(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _point(value: object) -> tuple[float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        if "local_ned_m" not in value and "ned" not in value:
            return None
        value = value.get("local_ned_m", value.get("ned"))
        if isinstance(value, Mapping):
            return tuple(_number(value.get(name), name) for name in ("north_m", "east_m", "down_m"))
    if (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) >= 3
    ):
        return tuple(_number(item, "pose") for item in value[:3])  # type: ignore[return-value]
    return None


def _enum_value(value: object) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _match(value: object) -> float:
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    return 0.5


def _norm(value: float, scale: float, *, clamp: bool = True) -> float:
    result = value / scale
    return max(-1.0, min(1.0, result)) if clamp else result


@dataclass(frozen=True, slots=True)
class StateBuilderInput:
    captured_at_s: float
    frame_id: str
    task_spec: TaskSpecV1
    context: object | None = None
    pose: object | None = None
    tracks: Sequence[object] = ()
    ranges: Sequence[object] = ()
    event: object | None = None
    memory: SearchMemoryGrid | None = None
    dt_s: float | None = None


class StructuredSceneStateBuilder:
    """Build one canonical state from runtime objects or a SIM step record."""

    HISTORY_LENGTH = 16
    TRACK_SLOTS = 8

    def __init__(
        self, *, history_length: int = HISTORY_LENGTH, memory: SearchMemoryGrid | None = None
    ) -> None:
        if history_length != self.HISTORY_LENGTH:
            raise PolicyContractError("state history length is frozen at 16")
        self.memory = memory or SearchMemoryGrid()
        self._history: list[dict[str, Any]] = []
        self._last_captured_at: float | None = None

    def reset(self) -> None:
        self._history.clear()
        self._last_captured_at = None
        self.memory.reset()

    def build(self, inputs: StateBuilderInput | None = None, **kwargs: Any) -> StructuredSceneState:
        data = inputs or StateBuilderInput(**kwargs)
        captured = _number(
            data.captured_at_s,
            "captured_at_s",
        )
        if captured < 0.0:
            raise PolicyContractError("captured_at_s must be non-negative")
        if self._last_captured_at is not None and captured < self._last_captured_at:
            raise PolicyContractError("captured_at_s moved backwards")
        dt = (
            data.dt_s
            if data.dt_s is not None
            else (0.0 if self._last_captured_at is None else captured - self._last_captured_at)
        )
        dt = _number(
            dt,
            "dt_s",
        )
        if dt < 0.0:
            raise PolicyContractError("dt_s must be non-negative")
        frame_id = str(data.frame_id).strip()
        if not frame_id:
            raise PolicyContractError("frame_id must be non-empty")
        task = data.task_spec
        if not isinstance(task, TaskSpecV1):
            raise PolicyContractError("task_spec must be TaskSpecV1")
        self._validate_frame(frame_id, data.tracks, data.ranges)
        pose = data.pose
        context = data.context
        health, health_valid = self._health(context if context is not None else pose)
        pose_block, pose_valid = self._pose(pose, context)
        tracks, track_valid = self._tracks(data.tracks, data.ranges, captured)
        scene = self._scene(tracks, data.ranges, data.event)
        task_block = {
            "phase": _enum_value(_get(data.event, "task_phase", "search"))
            if data.event is not None
            else "search",
            "search_budget": 0.0,
            "return_required": 1.0 if task.return_home else 0.0,
            "requested_class": task.target_class or "",
            "requested_color": task.target_color or "",
            "confidence": task.confidence,
        }
        event_block = self._event(data.event)
        memory_channels = self._memory_channels(data, pose, captured)
        valid_mask: dict[str, bool] = {
            "health": health_valid,
            "pose": pose_valid,
            "gps": health_valid and bool(health.get("gps_fix", 0.0) > 0.0),
            "range": any(track.get("valid_mask", {}).get("range", False) for track in tracks),
            "tracks": bool(tracks),
            "memory": True,
        }
        reset_mask = {"history": self._last_captured_at is None, "memory": data.memory is not None}
        current = {
            "captured_at_s": captured,
            "dt_s": dt,
            "frame_id": frame_id,
            "task_spec_hash": task.canonical_hash,
            "valid_mask": valid_mask,
            "reset_mask": reset_mask,
            "truth_free": True,
            "health": health,
            "pose": pose_block,
            "task": task_block,
            "tracks": tracks,
            "scene": scene,
            "memory": [[list(cell) for cell in row] for row in memory_channels],
            "event": event_block,
        }
        self._history.append(current)
        self._history = self._history[-self.HISTORY_LENGTH :]
        history = tuple({} for _ in range(self.HISTORY_LENGTH - len(self._history))) + tuple(
            self._history
        )
        self._last_captured_at = captured
        return StructuredSceneState(
            captured_at_s=captured,
            dt_s=dt,
            frame_id=frame_id,
            task_spec_hash=task.canonical_hash,
            valid_mask=valid_mask,
            reset_mask=reset_mask,
            health=health,
            pose=pose_block,
            task=task_block,
            tracks=tuple(tracks),
            scene=scene,
            memory=memory_channels,
            event=event_block,
            history=history,
            truth_free=True,
        )

    def from_runtime(
        self,
        *,
        captured_at_s: float,
        frame_id: str,
        task_spec: TaskSpecV1,
        context: object | None = None,
        pose: object | None = None,
        tracks: Sequence[object] = (),
        ranges: Sequence[object] = (),
        event: object | None = None,
        memory: SearchMemoryGrid | None = None,
        dt_s: float | None = None,
    ) -> StructuredSceneState:
        return self.build(
            StateBuilderInput(
                captured_at_s,
                frame_id,
                task_spec,
                context,
                pose,
                tracks,
                ranges,
                event,
                memory,
                dt_s,
            )
        )

    def from_sim_step_record(
        self, record: Mapping[str, Any], task_spec: TaskSpecV1 | None = None
    ) -> StructuredSceneState:
        if not isinstance(record, Mapping):
            raise PolicyContractError("step record must be an object")
        observation = record.get("obs_before", record.get("observation", record))
        if not isinstance(observation, Mapping):
            raise PolicyContractError("step record has no observation")
        spec = task_spec or self._task_from_record(record)
        captured = observation.get("captured_at_s", observation.get("timestamp_s"))
        if captured is None and observation.get("frame_sensor_stamp_ns") is not None:
            captured = _number(observation["frame_sensor_stamp_ns"], "frame_sensor_stamp_ns") / 1e9
        if captured is None:
            raise PolicyContractError("sim observation is missing captured_at_s")
        frame = observation.get("frame_id", observation.get("frame_sensor_stamp_ns"))
        if frame is None:
            raise PolicyContractError("sim observation is missing frame_id")
        # Offline SIM perception is recorded beside the step stream.  Adapt
        # detector outputs into the same track/range shape used by runtime
        # callers before entering ``build``.  Privileged labels and outcomes
        # are deliberately ignored here.
        perception = observation.get("perception", record.get("perception"))
        tracks, ranges = self._sim_perception(perception, frame_id=str(frame))
        # labels/outcome/action are deliberately not passed to the state.
        return self.build(
            StateBuilderInput(
                captured_at_s=_number(captured, "captured_at_s"),
                frame_id=str(frame),
                task_spec=spec,
                pose=observation,
                tracks=tracks
                if perception is not None
                else tuple(observation.get("tracks", ())),
                ranges=ranges
                if perception is not None
                else tuple(observation.get("ranges", ())),
                event=observation.get("event"),
            )
        )

    @staticmethod
    def _sim_perception(
        perception: object, *, frame_id: str
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
        """Convert one ``perception.jsonl`` row into runtime-shaped inputs.

        The bridge intentionally has no tracker.  Each detector output is a
        one-frame track, with a stable row-local id, and its measured bearing
        and depth are represented by a matching range record.  This keeps the
        canonical state identical to a runtime frame containing equivalent
        detections while preserving missing depth as an invalid range.
        """
        if not isinstance(perception, Mapping):
            return (), ()
        raw = perception.get("detections", ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            return (), ()
        tracks: list[dict[str, Any]] = []
        ranges: list[dict[str, Any]] = []
        for index, detection in enumerate(raw):
            if not isinstance(detection, Mapping):
                continue
            track_id = str(
                detection.get("track_id", detection.get("detection_id", f"sim-det-{index}"))
            )
            box = detection.get("bbox", detection.get("bbox_xyxy_px"))
            if isinstance(box, Mapping):
                box = [box.get(key, 0.0) for key in ("x1", "y1", "x2", "y2")]
            if (
                not isinstance(box, Sequence)
                or isinstance(box, (str, bytes, bytearray))
                or len(box) < 4
            ):
                continue
            confidence = detection.get("confidence", detection.get("conf", 0.0))
            label = str(detection.get("label", detection.get("class", "")))
            tracks.append(
                {
                    "track_id": track_id,
                    "frame_id": frame_id,
                    "label": label,
                    "state": "TRACKING",
                    "bbox": list(box[:4]),
                    "confidence": confidence,
                    "tracking_quality": confidence,
                    "primary": bool(detection.get("primary", False)),
                    "actionable": bool(detection.get("actionable", False)),
                    "class_match": detection.get("class_match"),
                    "color_match": detection.get("color_match"),
                }
            )
            range_m = detection.get("range_m")
            bearing = detection.get("bearing_deg")
            absolute_bearing = detection.get("absolute_bearing_deg")
            range_row: dict[str, Any] = {
                "target_id": track_id,
                "frame_id": frame_id,
                "validity": "valid" if range_m is not None else "invalid",
            }
            if range_m is not None:
                range_row["slant_range_m"] = range_m
            if bearing is not None:
                range_row["relative_bearing_deg"] = bearing
            if absolute_bearing is not None:
                range_row["absolute_bearing_deg"] = absolute_bearing
            ranges.append(range_row)
        return tuple(tracks), tuple(ranges)

    @staticmethod
    def _task_from_record(record: Mapping[str, Any]) -> TaskSpecV1:
        import hashlib

        text = str(record.get("task_text", ""))
        return TaskSpecV1(
            prompt_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), resolver_id="sim-record"
        )

    @staticmethod
    def _validate_frame(frame_id: str, tracks: Sequence[object], ranges: Sequence[object]) -> None:
        for item in (*tracks, *ranges):
            item_frame = _get(item, "frame_id")
            if item_frame is not None and str(item_frame) != frame_id:
                raise PolicyContractError("source frame mismatch")

    def _health(self, context: object | None) -> tuple[dict[str, Any], bool]:
        if context is None:
            return {
                "battery": 0.0,
                "flight_time": 0.0,
                "ground_speed": 0.0,
                "airspeed": 0.0,
                "wind_north": 0.0,
                "wind_east": 0.0,
                "gps_fix": 0.0,
                "gps_accuracy": 0.0,
                "gps_age": 0.0,
            }, False
        battery = _get(context, "battery_remaining_pct")
        fix = _get(context, "gps_fix_type")
        accuracy = _get(context, "gps_horizontal_accuracy_m")
        values = {
            "battery": 0.0 if battery is None else _number(battery, "battery") / 100.0,
            "flight_time": 0.0
            if _get(context, "estimated_flight_time_s") is None
            else _number(_get(context, "estimated_flight_time_s"), "flight_time") / 3600.0,
            "ground_speed": _number(_get(context, "ground_speed_mps", 0.0), "ground_speed") / 50.0,
            "airspeed": 0.0
            if _get(context, "airspeed_mps") is None
            else _number(_get(context, "airspeed_mps"), "airspeed") / 50.0,
            "wind_north": 0.0
            if _get(context, "wind_north_mps") is None
            else _number(_get(context, "wind_north_mps"), "wind_north") / 20.0,
            "wind_east": 0.0
            if _get(context, "wind_east_mps") is None
            else _number(_get(context, "wind_east_mps"), "wind_east") / 20.0,
            "gps_fix": 0.0 if fix is None else max(0.0, min(1.0, _number(fix, "gps_fix") / 6.0)),
            "gps_accuracy": 0.0
            if accuracy is None
            else max(0.0, min(1.0, _number(accuracy, "gps_accuracy") / 100.0)),
            "gps_age": 0.0,
        }
        return values, True

    @staticmethod
    def _pose(pose: object | None, context: object | None) -> tuple[dict[str, Any], bool]:
        source = pose or context
        if isinstance(source, Mapping) and "pose" in source:
            source = source["pose"]
        point = _point(source)
        if point is None and context is not None:
            point = _point(_get(context, "local_ned_m"))
        attitude = _get(source, "attitude", {})
        velocity = _get(source, "velocity", (0.0, 0.0, 0.0))
        if isinstance(velocity, Mapping):
            velocity = tuple(
                velocity.get(key, 0.0) for key in ("north_mps", "east_mps", "down_mps")
            )
        vel = (
            tuple(_number(v, "velocity") for v in velocity)
            if isinstance(velocity, Sequence) and len(velocity) >= 3
            else (0.0, 0.0, 0.0)
        )
        roll, pitch, heading = (
            _number(_get(attitude, key, 0.0), key) for key in ("roll_deg", "pitch_deg", "yaw_deg")
        )
        block = {
            "north": 0.0,
            "east": 0.0,
            "down": 0.0,
            "altitude": 0.0,
            "velocity_n": _norm(vel[0], 20.0),
            "velocity_e": _norm(vel[1], 20.0),
            "velocity_d": _norm(vel[2], 20.0),
            "roll_sin": math.sin(math.radians(roll)),
            "roll_cos": math.cos(math.radians(roll)),
            "pitch_sin": math.sin(math.radians(pitch)),
            "pitch_cos": math.cos(math.radians(pitch)),
            "heading_sin": math.sin(math.radians(heading)),
            "heading_cos": math.cos(math.radians(heading)),
        }
        if point is None:
            return block, False
        block.update(
            {
                "north": _norm(point[0], 100.0),
                "east": _norm(point[1], 100.0),
                "down": _norm(point[2], 100.0),
                "altitude": _norm(-point[2], 30.0),
            }
        )
        return block, True

    def _tracks(
        self, tracks: Sequence[object], ranges: Sequence[object], captured: float
    ) -> tuple[list[dict[str, Any]], bool]:
        range_map = {str(_get(item, "target_id")): item for item in ranges}
        ordered = sorted(
            tracks,
            key=lambda item: (
                not bool(_get(item, "primary", False)),
                not bool(_get(item, "actionable", False)),
                -_number(_get(item, "confidence", 0.0), "confidence"),
                str(_get(item, "track_id", "")),
            ),
        )[: self.TRACK_SLOTS]
        output: list[dict[str, Any]] = []
        for item in ordered:
            box = _get(item, "bbox", (0.0, 0.0, 0.0, 0.0))
            values = (_get(box, key, 0.0) for key in ("x1", "y1", "x2", "y2"))
            x1, y1, x2, y2 = (_number(v, "bbox") for v in values)
            rid = str(_get(item, "track_id", ""))
            r = range_map.get(rid)
            valid_range = (
                r is not None
                and _enum_value(_get(r, "validity", "invalid")) != "invalid"
                and _get(r, "ground_range_m", _get(r, "slant_range_m")) is not None
            )
            distance = (
                _number(_get(r, "ground_range_m", _get(r, "slant_range_m", 0.0)), "range")
                if valid_range
                else 0.0
            )
            bearing = (
                _number(
                    _get(r, "absolute_bearing_deg", _get(r, "relative_bearing_deg", 0.0)), "bearing"
                )
                if valid_range
                else 0.0
            )
            output.append(
                {
                    "track_id": rid,
                    "state": _enum_value(_get(item, "state", "lost")),
                    "label": str(_get(item, "label", "")),
                    "bbox": [x1, y1, x2, y2],
                    "velocity": [
                        _number(_get(item, "velocity_x_s", 0.0), "velocity_x_s"),
                        _number(_get(item, "velocity_y_s", 0.0), "velocity_y_s"),
                    ],
                    "confidence": _number(_get(item, "confidence", 0.0), "confidence"),
                    "tracking_quality": _number(
                        _get(item, "tracking_quality", 0.0), "tracking_quality"
                    ),
                    "age_s": max(
                        0.0,
                        captured
                        - _number(_get(item, "last_seen_at_s", captured), "last_seen_at_s"),
                    ),
                    "missed": _number(_get(item, "missed_frame_count", 0), "missed_frame_count"),
                    "range": max(0.0, min(1.0, distance / 100.0)),
                    "bearing_sin": math.sin(math.radians(bearing)),
                    "bearing_cos": math.cos(math.radians(bearing)),
                    "class_match": _match(_get(item, "class_match")),
                    "color_match": _match(_get(item, "color_match")),
                    "valid_mask": {"range": bool(valid_range), "track": True},
                }
            )
        return output, bool(output)

    @staticmethod
    def _scene(
        tracks: Sequence[Mapping[str, Any]], ranges: Sequence[object], event: object | None
    ) -> dict[str, Any]:
        distances = [
            float(_get(item, "ground_range_m", _get(item, "slant_range_m")))
            for item in ranges
            if _get(item, "ground_range_m", _get(item, "slant_range_m")) is not None
        ]
        return {
            "primary": bool(
                any(
                    item.get("valid_mask", {}).get("track") and item.get("track_id")
                    for item in tracks
                )
            ),
            "nearest_obstacle": 0.0,
            "nearest_target": (min(distances) / 50.0 if distances else 0.0),
            "avoidance_risk": _norm(
                _number(_get(event, "severity", 0.0), "severity") if event else 0.0, 100.0
            ),
        }

    @staticmethod
    def _event(event: object | None) -> dict[str, Any]:
        return {
            "kind": _enum_value(_get(event, "kind", "NONE")) if event else "NONE",
            "severity": max(
                0.0, min(1.0, _number(_get(event, "severity", 0.0), "severity") / 100.0)
            )
            if event
            else 0.0,
        }

    def _memory_channels(
        self, data: StateBuilderInput, pose: object | None, captured: float
    ) -> tuple[tuple[tuple[float, ...], ...], ...]:
        if data.memory is not None:
            self.memory = data.memory
        point = _point(pose)
        if point is None and isinstance(pose, Mapping):
            point = _point(pose.get("pose"))
        if point is not None:
            self.memory.update(north_m=point[0], east_m=point[1], captured_at_s=captured)
        return self.memory.as_channels(captured_at_s=captured)


__all__ = ["StateBuilderInput", "StructuredSceneStateBuilder"]
