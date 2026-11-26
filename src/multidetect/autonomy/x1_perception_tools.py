"""Read-only observations and bounded Altair tool turns for one X1 decision."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, wait
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock, Thread
from typing import TYPE_CHECKING

from multidetect.domain import Detection, SensorKind

from .motion_intent import camera_ray_from_profile
from .serialization import canonical_json, canonical_sha256, strict_json_loads
from .x1_contingency_executor import (
    DECISION_SCHEMA,
    MAX_DECISION_BYTES,
    REQUEST_SCHEMA,
    decode_decision,
)
from .x1_contingency_state import require_token

if TYPE_CHECKING:
    from multidetect.onboard_runtime.camera import CameraCalibration
    from multidetect.terrain_dem import LocalDemGrid
    from multidetect.unified_tracking import TargetPoolUpdate

    from .airframe_profile import AirframeProfile
    from .x1_mission import X1MissionStep

TOOL_SCHEMA = "x1.contingency.tool.v1"
TOOLS = ("look", "target_status", "gimbal_state", "terrain_ahead", "route_status", "battery_status")
_AXES = ("pitch", "roll", "yaw")
_BODY_JOINT_FRAME = "body_FRD_C11_ZXY"
_BODY_OR_HORIZON = {_BODY_JOINT_FRAME, "horizon_roll_pitch_vehicle_yaw_ZYX"}
_LABEL = re.compile(r"[\w .:/-]{1,64}")
_MAX_CALLS = 8
_MAX_ROUTE_STEPS = 32
_MAX_DETECTIONS = 12
_MAX_OBSERVATION_AGE_MS = 1_000
_MAX_ALIGNMENT_MS = 100


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class ToolProtocolError(ValueError):
    """An invalid or incomplete window; no action is returned to the executor."""


class _Unavailable(ValueError):
    """Stable tool failure code (never include exceptions or sensor payloads)."""


def _value(value, minimum=-1e12, maximum=1e12):
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError("invalid observation number")
    return value


def _vector(values, length, minimum=-1e12, maximum=1e12):
    if len(values) != length:
        raise ValueError("invalid observation vector")
    return [_value(v, minimum, maximum) for v in values]


def _stamp(value):
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise ValueError("invalid monotonic timestamp")
    return value


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool: str
    request_id: str
    call_index: int

    @classmethod
    def parse(cls, obj):
        if (
            not isinstance(obj, dict)
            or set(obj) != {"schema", "tool", "args", "request_id", "call_index"}
            or obj["schema"] != TOOL_SCHEMA
            or obj["args"] != {}
            or not isinstance(obj["tool"], str)
            or obj["tool"] not in TOOLS
            or type(obj["call_index"]) is not int
            or not 0 <= obj["call_index"] < _MAX_CALLS
        ):
            raise ToolProtocolError("invalid_tool_call")
        try:
            request_id = require_token(obj["request_id"])
        except ValueError as exc:
            raise ToolProtocolError("invalid_tool_call") from exc
        return cls(obj["tool"], request_id, obj["call_index"])


@dataclass(frozen=True, slots=True)
class GimbalReading:
    captured_ms: int
    # Project fields from attitude_readback(), not from a command or its ACK.
    readback: Mapping
    limits_deg: Mapping | None


@dataclass(frozen=True, slots=True)
class FrameSummary:
    captured_ms: int
    calibration: CameraCalibration
    detections: tuple[Detection, ...]
    gimbal: GimbalReading | None
    mode: str


@dataclass(frozen=True, slots=True)
class TargetReading:
    update: TargetPoolUpdate
    # A real geolocation producer must bind its estimate to the same track.
    position_track_id: str
    position_captured_ms: int
    position_wgs84_amsl: tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class RouteReading:
    captured_ms: int
    steps: tuple[X1MissionStep, ...]
    active_index: int
    position_ned_m: tuple[float, float, float]
    # DEM-local north/east and AMSL height of the compiled NED origin.
    origin_dem_ne_amsl: tuple[float, float, float]
    minimum_clearance_m: float


@dataclass(frozen=True, slots=True)
class BatteryReading:
    percent: float
    percent_captured_ms: int
    remaining_wh: float | None
    absolute_error_wh: float | None
    energy_captured_ms: int | None


@dataclass(frozen=True, slots=True)
class PerceptionSources:
    """Trusted producer-owned snapshot readers, not live controller/vehicle ports.

    Readers only read already-produced samples; they receive no model inputs and
    must not grab the actuator bus, run inference, or refresh sensor timestamps.
    Publish whole stable samples; do not mutate their members after publication.
    """

    frame: Callable[[], FrameSummary | None] | None = None
    target: Callable[[], TargetReading | None] | None = None
    gimbal: Callable[[], GimbalReading | None] | None = None
    route: Callable[[], RouteReading | None] | None = None
    battery: Callable[[], BatteryReading | None] | None = None
    dem: LocalDemGrid | None = None
    profile: AirframeProfile | None = None


def _read(reader, reason):
    reading = reader() if reader is not None else None
    if reading is None:
        raise _Unavailable(reason)
    return reading


def _angles(reading):
    raw = reading.readback
    if (
        raw.get("comparison_frame") not in _BODY_OR_HORIZON
        or type(raw.get("failure_flags")) is not int
    ):
        raise _Unavailable("gimbal_readback_unavailable")
    if raw["failure_flags"] != 0 or raw.get("actual_deg") is None or "readback_error" in raw:
        raise _Unavailable("gimbal_readback_unavailable")
    return {axis: _value(raw["actual_deg"][axis], -360, 360) for axis in _AXES}


def _look(sources):
    frame = _read(sources.frame, "frame_summary_unavailable")
    if (
        sources.profile is None
        or not sources.profile.camera.gimbal.get("enabled")
        or frame.mode not in {"mc", "fw"}
        or frame.calibration is None
    ):
        raise _Unavailable("camera_geometry_unavailable")
    cal = frame.calibration
    if any(cal.distortion):
        raise _Unavailable("rectified_camera_required")
    if frame.gimbal is None:
        raise _Unavailable("body_gimbal_pose_unavailable")
    captured, pose_time = _stamp(frame.captured_ms), _stamp(frame.gimbal.captured_ms)
    if abs(captured - pose_time) > _MAX_ALIGNMENT_MS:
        raise _Unavailable("frame_pose_not_aligned")
    if frame.gimbal.readback.get("comparison_frame") != _BODY_JOINT_FRAME:
        raise _Unavailable("body_gimbal_pose_unavailable")
    angles = _angles(frame.gimbal)
    if len(frame.detections) > 1_024:
        raise _Unavailable("detection_input_too_large")
    boxes = []
    for detection in frame.detections:
        if (
            not isinstance(detection, Detection)
            or detection.sensor != SensorKind.RGB
            or not _LABEL.fullmatch(detection.label)
        ):
            raise ValueError("invalid RGB detection")
        bbox = detection.bbox
        boxes.append(
            (
                detection.label,
                _value(detection.confidence, 0, 1),
                (bbox.x1, bbox.y1, bbox.x2, bbox.y2),
                bbox.center,
            )
        )
    boxes.sort(key=lambda item: (-item[1], item[0], item[2]))
    records = []
    for label, confidence, bbox, (u, v) in boxes[:_MAX_DETECTIONS]:
        x, y, z = camera_ray_from_profile(
            u * cal.width,
            v * cal.height,
            fx=cal.fx,
            fy=cal.fy,
            cx=cal.cx,
            cy=cal.cy,
            attitude_deg=(0, 0, 0),
            profile=sources.profile,
            mode=frame.mode,
            gimbal_angles_deg=angles,
        )
        records.append(
            {
                "class": label,
                "confidence": confidence,
                "bbox": list(bbox),
                "bearing_deg": round(math.degrees(math.atan2(y, x)), 4),
                "elevation_deg": round(math.degrees(math.atan2(-z, math.hypot(x, y))), 4),
            }
        )
    return {"detections": records, "truncated": len(boxes) > _MAX_DETECTIONS}, (captured, pose_time)


def _target(sources):
    reading = _read(sources.target, "tracked_position_unavailable")
    update = reading.update
    track = next((item for item in update.tracks if item.track_id == update.primary_track_id), None)
    if track is None or track.track_id != reading.position_track_id:
        raise _Unavailable("tracked_position_unavailable")
    captured = int(_value(update.captured_at_s, 0) * 1000)
    last_seen = int(_value(track.last_seen_at_s, 0, update.captured_at_s) * 1000)
    position_stamp = _stamp(reading.position_captured_ms)
    if position_stamp > captured or position_stamp > last_seen + _MAX_ALIGNMENT_MS:
        raise _Unavailable("tracked_position_not_aligned")
    lat, lon, alt = _vector(reading.position_wgs84_amsl, 3)
    return {
        "track_id": require_token(track.track_id),
        "visible": track.track_id in update.visual_confirmed_track_ids,
        "last_seen_age_ms_at_snapshot": captured - last_seen,
        "position_wgs84_amsl": [_value(lat, -90, 90), _value(lon, -180, 180), alt],
    }, (captured, position_stamp)


def _gimbal(sources):
    reading = _read(sources.gimbal, "gimbal_readback_unavailable")
    angles = _angles(reading)
    if reading.limits_deg is None or any(axis not in reading.limits_deg for axis in _AXES):
        raise _Unavailable("gimbal_limits_unavailable")
    limits = {axis: _vector(reading.limits_deg[axis], 2, -360, 360) for axis in _AXES}
    if any(lo > hi for lo, hi in limits.values()):
        raise ValueError("invalid gimbal limits")
    return {
        "angles_deg": angles,
        "limits_deg": limits,
        "frame": reading.readback["comparison_frame"],
        "device_id": _device_id(reading.readback.get("gimbal_device_id")),
    }, (_stamp(reading.captured_ms),)


def _device_id(value):
    if type(value) is not int or not 1 <= value <= 255:
        raise _Unavailable("gimbal_device_unavailable")
    return value


def _route_reading(sources):
    route = _read(sources.route, "active_route_unavailable")
    if (
        type(route.active_index) is not int
        or not 0 <= route.active_index <= len(route.steps)
        or len(route.steps) - route.active_index > _MAX_ROUTE_STEPS
    ):
        raise ValueError("invalid route index or size")
    return route


def _endpoint(step):
    if step.command is None or step.command.target_ned is None:
        return None
    return _vector(step.command.target_ned, 3)


def _route(sources):
    route = _route_reading(sources)
    point = _vector(route.position_ned_m, 3)
    rows = []
    for step in route.steps[route.active_index :]:
        end = _endpoint(step)
        rows.append(
            {
                "step_id": require_token(step.source_step_id),
                "primitive": require_token(step.primitive),
                "endpoint_distance_m": None
                if end is None
                else round(math.dist(point[:2], end[:2]), 2),
            }
        )
        if end is not None:
            point = end
    return {"steps": rows}, (_stamp(route.captured_ms),)


def _terrain(sources):
    if sources.dem is None:
        raise _Unavailable("dem_unavailable")
    route = _route_reading(sources)
    start = _vector(route.position_ned_m, 3)
    suffix = route.steps[route.active_index :]
    end = _endpoint(suffix[0]) if suffix and suffix[0].primitive == "fw_goto_waypoint" else None
    if end is None or math.dist(start[:2], end[:2]) < 1.0:
        raise _Unavailable("straight_active_segment_unavailable")
    north, east, alt = _vector(route.origin_dem_ne_amsl, 3)
    floor = _value(route.minimum_clearance_m, 0)
    points, clearances = [], []
    try:
        # Point samples of the nominal connector, not a proven swept corridor.
        for i in range(9):
            n, e, down = [a + (b - a) * i / 8 for a, b in zip(start, end, strict=True)]
            surface = sources.dem.sample_local(north + n, east + e).elevation_m
            clearances.append(alt - down - surface)
            points.append(
                {
                    "distance_m": round(math.dist(start[:2], (n, e)), 2),
                    "elevation_amsl_m": round(surface, 2),
                    "clearance_m": round(clearances[-1], 2),
                }
            )
    except ValueError as exc:
        raise _Unavailable("dem_segment_outside_coverage") from exc
    return {
        "profile": points,
        "min_sample_margin_m": round(min(clearances) - floor, 2),
        "minimum_clearance_m": floor,
    }, (_stamp(route.captured_ms),)


def _battery(sources):
    reading = _read(sources.battery, "bounded_energy_unavailable")
    if reading.remaining_wh is None or reading.absolute_error_wh is None:
        raise _Unavailable("bounded_energy_unavailable")
    return {
        "percent": _value(reading.percent, 0, 100),
        "remaining_wh": _value(reading.remaining_wh, 0),
        "absolute_error_wh": _value(reading.absolute_error_wh, 0),
    }, (_stamp(reading.percent_captured_ms), _stamp(reading.energy_captured_ms))


_READERS = {
    "look": (_look, "rgb_detector_body_gimbal"),
    "target_status": (_target, "tracker_geolocation"),
    "gimbal_state": (_gimbal, "gimbal_device_attitude"),
    "terrain_ahead": (_terrain, "local_dem_route"),
    "route_status": (_route, "compiled_x1_route"),
    "battery_status": (_battery, "battery_bounded_energy"),
}


@dataclass(frozen=True, slots=True)
class _Result:
    tool: str
    source: str
    data: dict | None = None
    stamps: tuple[int, ...] = ()
    error: str | None = None

    def status(self, now, max_age):
        age = None
        error = self.error
        if self.stamps:
            if max(self.stamps) > now:
                error = "future_observation"
            else:
                age = now - min(self.stamps)
                if age > max_age:
                    error = "stale_observation"
        return age, error

    def wire(self, now, max_age):
        age, error = self.status(now, max_age)
        return {
            "schema": TOOL_SCHEMA,
            "tool": self.tool,
            "ok": error is None,
            "data": deepcopy(self.data) if error is None else None,
            "source": self.source,
            "age_ms": age,
            "error": error,
        }


class _TimedSlot:
    """At most one outstanding call, including a non-cooperative timed-out reader."""

    def __init__(self):
        self.lock = Lock()
        self.future = None

    @property
    def busy(self):
        future = self.future
        return future is not None and not future.done()

    def call(self, fn, deadline, clock):
        with self.lock:
            timeout_ms = deadline - clock()
            if timeout_ms <= 0:
                raise ToolProtocolError("timeout")
            if self.future is not None and not self.future.done():
                raise ToolProtocolError("busy")
            future = self.future = Future()

            def work():
                try:
                    future.set_result(fn())
                except BaseException as exc:
                    future.set_exception(exc)

            Thread(target=work, name="x1-tool-turn", daemon=True).start()
        done, _ = wait((future,), timeout=timeout_ms / 1000)
        if not done or clock() >= deadline:
            raise ToolProtocolError("timeout")
        return future.result()


@dataclass(frozen=True, slots=True)
class ToolReply:
    wire: str | bytes
    deadline_ms: int
    observations_valid_through_ms: int | None

    def check_fresh(self, now_ms):
        if now_ms >= self.deadline_ms:
            raise ToolProtocolError("window_timeout")
        if self.observations_valid_through_ms is not None and now_ms > self.observations_valid_through_ms:
            raise ToolProtocolError("tool_result_expired")


class ToolRunner:
    """Await backend -> read one tool -> await backend, until action or rejection."""

    def __init__(
        self,
        sources: PerceptionSources | None = None,
        *,
        max_calls=3,
        per_call_ms=150,
        total_ms=2_500,
        max_age_ms=_MAX_OBSERVATION_AGE_MS,
        clock_ms=_now_ms,
    ):
        for value, lower, upper in (
            (max_calls, 0, _MAX_CALLS),
            (per_call_ms, 1, 3_000),
            (total_ms, 1, 3_000),
            (max_age_ms, 1, _MAX_OBSERVATION_AGE_MS),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("invalid tool runner limit")
        self._sources = sources if sources is not None else PerceptionSources()
        self._max_calls, self._per_call_ms, self._total_ms, self._max_age_ms = (
            max_calls,
            per_call_ms,
            total_ms,
            max_age_ms,
        )
        self._clock = clock_ms
        self._tool, self._backend = _TimedSlot(), _TimedSlot()
        self._window = Lock()

    def run(self, request: dict, backend: Callable, deadline: int) -> str | bytes:
        """Return the original final decision wire, never a tool call or fallback action."""
        return self.run_window(request, backend, deadline).wire

    @property
    def backend_busy(self):
        return self._backend.busy

    def run_window(self, request: dict, backend: Callable, deadline: int, *, cancelled=None) -> ToolReply:
        """Return the final action and its validity bounds to the collector."""
        if not self._window.acquire(blocking=False):
            raise ToolProtocolError("window_busy")
        try:
            return self._run(request, backend, deadline, cancelled)
        finally:
            self._window.release()

    def _run(self, request, backend, deadline, cancelled):
        def require_active():
            if cancelled is not None and cancelled.is_set():
                raise ToolProtocolError("window_cancelled")

        started = self._clock()
        deadline = min(_stamp(deadline), _stamp(request["deadline_ms"]), started + self._total_ms)
        if request.get("schema") != REQUEST_SCHEMA or "perception" in request:
            raise ToolProtocolError("invalid_base_request")
        frozen = canonical_json(request)
        state = request["state"]
        digest = canonical_sha256(state)
        identity = {
            "request_id": require_token(request["request_id"]),
            "event_id": request["event_id"],
            "mission_id": state["mission_id"],
            "revision": state["revision"],
        }
        capabilities = frozenset(request.get("capabilities", ()))
        results = []
        while True:
            require_active()
            now = self._clock()
            if now >= deadline:
                raise ToolProtocolError("window_timeout")
            index = len(results)
            message = strict_json_loads(frozen)
            message["perception"] = {
                "schema": TOOL_SCHEMA,
                "state_sha256": digest,
                "call_index": index,
                "calls_remaining": self._max_calls - index,
                "tools": list(TOOLS) if index < self._max_calls else [],
                "results": [result.wire(now, self._max_age_ms) for result in results],
            }
            sent_ok = tuple(row["ok"] for row in message["perception"]["results"])
            try:
                raw = self._backend.call(
                    lambda message=message: backend(message), deadline, self._clock
                )
            except ToolProtocolError as exc:
                raise ToolProtocolError(
                    "window_timeout" if str(exc) == "timeout" else "backend_busy"
                ) from exc
            require_active()
            try:
                wire = raw.encode("utf-8") if isinstance(raw, str) else raw
                if not isinstance(wire, bytes) or len(wire) > MAX_DECISION_BYTES:
                    raise ValueError("invalid wire size")
                reply = strict_json_loads(wire, name="Altair X1 tool turn")
                if not isinstance(reply, dict):
                    raise ValueError("invalid reply object")
                # Reject non-finite decoded exponents and unpaired surrogates.
                canonical_json(reply).encode("utf-8")
            except (ValueError, RecursionError) as exc:
                raise ToolProtocolError("invalid_backend_json") from exc
            if self._clock() >= deadline:
                raise ToolProtocolError("window_timeout")
            if reply.get("schema") == DECISION_SCHEMA:
                if set(reply) != {*identity, "schema", "action", "params"} or any(
                    type(reply[key]) is not type(value) or reply[key] != value
                    for key, value in identity.items()
                ):
                    raise ToolProtocolError("invalid_final_identity")
                try:
                    decode_decision(wire, capabilities=capabilities)
                except (ValueError, RecursionError) as exc:
                    raise ToolProtocolError("invalid_final_action") from exc
                # A final reply that relied on a result which expired mid-inference
                # must not reach the executor. The model still chooses all actions.
                valid_through = min(
                    (min(result.stamps) + self._max_age_ms
                    for result, was_ok in zip(results, sent_ok, strict=True)
                     if was_ok),
                    default=None,
                )
                final = ToolReply(raw, deadline, valid_through)
                final.check_fresh(self._clock())
                return final
            if reply.get("schema") != TOOL_SCHEMA:
                raise ToolProtocolError("unknown_reply_schema")
            if index == self._max_calls:
                raise ToolProtocolError("call_budget_exhausted")
            call = ToolCall.parse(reply)
            if call.request_id != identity["request_id"] or call.call_index != index:
                raise ToolProtocolError("unmatched_tool_call")
            require_active()
            results.append(self._read_tool(call.tool, deadline))

    def _read_tool(self, tool, deadline):
        read, source = _READERS[tool]
        try:
            data, stamps = self._tool.call(
                lambda: read(self._sources),
                min(deadline, self._clock() + self._per_call_ms),
                self._clock,
            )
        except _Unavailable as exc:
            return _Result(tool, source, error=str(exc))
        except ToolProtocolError as exc:
            return _Result(tool, source, error=f"tool_{exc}")
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            return _Result(tool, source, error="invalid_observation")
        except Exception:
            return _Result(tool, source, error="source_error")
        result = _Result(tool, source, data, stamps)
        # A future sample must not turn into a success as time advances.
        if result.status(self._clock(), self._max_age_ms)[1] == "future_observation":
            return _Result(tool, source, error="future_observation")
        return result
