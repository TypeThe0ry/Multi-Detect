"""Host-side validation for every primitive sent to the vehicle.

The envelope deliberately works on the small ``ActionPrimitive`` contract rather
than on a particular planner.  That keeps the final safety decision independent
of which autonomy skill produced a command.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned

Point = tuple[float, float]
Pose = tuple[float, float, float]
ObstacleCheck = Callable[[Pose, float, float], bool]


def _finite_point(value: Sequence[float]) -> Point | None:
    if isinstance(value, (str, bytes)) or len(value) != 2:
        return None
    try:
        point = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return point if all(math.isfinite(item) for item in point) else None


def _polygon_contains(point: Point, polygon: tuple[Point, ...]) -> bool:
    """Return whether a point is inside a polygon, including its boundary."""
    if len(polygon) < 3:
        return True
    x, y = point
    inside = False
    for first, second in zip(polygon, (*polygon[1:], polygon[0]), strict=True):
        x1, y1 = first
        x2, y2 = second
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) <= 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 and min(
            y1, y2
        ) - 1e-9 <= y <= max(y1, y2) + 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            intersection_x = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < intersection_x:
                inside = not inside
    return inside


def _cross(first: Point, second: Point) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _polygon_segment_clear(start: Point, end: Point, polygon: tuple[Point, ...]) -> bool:
    """Return whether the complete segment stays inside a polygon."""
    if len(polygon) < 3:
        return True
    dx, dy = end[0] - start[0], end[1] - start[1]
    parameters = [0.0, 1.0]
    for edge_start, edge_end in zip(polygon, (*polygon[1:], polygon[0]), strict=True):
        ex, ey = edge_end[0] - edge_start[0], edge_end[1] - edge_start[1]
        denominator = _cross((dx, dy), (ex, ey))
        offset = (edge_start[0] - start[0], edge_start[1] - start[1])
        if abs(denominator) <= 1e-12:
            if abs(_cross(offset, (dx, dy))) <= 1e-9:
                if abs(dx) >= abs(dy) and abs(dx) > 1e-12:
                    parameters.extend(
                        ((edge_start[0] - start[0]) / dx, (edge_end[0] - start[0]) / dx)
                    )
                elif abs(dy) > 1e-12:
                    parameters.extend(
                        ((edge_start[1] - start[1]) / dy, (edge_end[1] - start[1]) / dy)
                    )
            continue
        segment_t = _cross(offset, (ex, ey)) / denominator
        edge_u = _cross(offset, (dx, dy)) / denominator
        if -1e-9 <= segment_t <= 1.0 + 1e-9 and -1e-9 <= edge_u <= 1.0 + 1e-9:
            parameters.append(max(0.0, min(1.0, segment_t)))
    if not _polygon_contains(start, polygon) or not _polygon_contains(end, polygon):
        return False
    parameters = sorted({max(0.0, min(1.0, value)) for value in parameters})
    for first, second in zip(parameters, parameters[1:], strict=False):
        middle = (first + second) / 2.0
        point = (start[0] + dx * middle, start[1] + dy * middle)
        if not _polygon_contains(point, polygon):
            return False
    return True


@dataclass(frozen=True, slots=True)
class SafetyEnvelopeConfig:
    """Limits applied by :class:`SafetyEnvelope`.

    ``None`` means that the corresponding optional limit is disabled.  The
    defaults match the existing host behavior: horizontal legs are already
    bounded to 15 m, while no common fence, radius, or altitude ceiling was
    previously applied at the primitive boundary.
    """

    operating_polygon: tuple[Point, ...] = ()
    max_radius_from_home_m: float | None = None
    min_altitude_m: float = 0.0
    max_altitude_m: float | None = None
    max_translation_m: float = 15.0
    rth_after_rejections: int = 0
    hover_seconds: float = 1.0

    def __post_init__(self) -> None:
        if len(self.operating_polygon) not in (0,) and len(self.operating_polygon) < 3:
            raise ValueError("operating_polygon must have at least three points")
        if not math.isfinite(self.min_altitude_m) or self.min_altitude_m < 0:
            raise ValueError("min_altitude_m must be finite and non-negative")
        if self.max_altitude_m is not None and (
            not math.isfinite(self.max_altitude_m) or self.max_altitude_m < self.min_altitude_m
        ):
            raise ValueError("max_altitude_m must be at least min_altitude_m")
        if not math.isfinite(self.max_translation_m) or self.max_translation_m <= 0:
            raise ValueError("max_translation_m must be finite and positive")
        if self.max_radius_from_home_m is not None and (
            not math.isfinite(self.max_radius_from_home_m)
            or self.max_radius_from_home_m <= 0
        ):
            raise ValueError("max_radius_from_home_m must be finite and positive")
        if self.rth_after_rejections < 0:
            raise ValueError("rth_after_rejections must be non-negative")
        if not math.isfinite(self.hover_seconds) or self.hover_seconds < 0:
            raise ValueError("hover_seconds must be finite and non-negative")

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any] | None = None,
        *,
        default_max_translation_m: float = 15.0,
    ) -> SafetyEnvelopeConfig:
        """Build a config from a flat or nested onboard policy mapping."""
        source: Mapping[str, Any] = values or {}
        nested = source.get("safety_envelope")
        if isinstance(nested, Mapping):
            source = {**source, **nested}

        polygon_value = source.get("operating_polygon", source.get("geofence_polygon", ()))
        polygon: list[Point] = []
        if polygon_value is not None:
            if isinstance(polygon_value, Mapping):
                polygon_value = polygon_value.get("points", ())
            if isinstance(polygon_value, Sequence) and not isinstance(
                polygon_value, (str, bytes)
            ):
                for item in polygon_value:
                    point = _finite_point(item) if isinstance(item, Sequence) else None
                    if point is None:
                        raise ValueError(
                            "operating_polygon points must be finite [north, east] pairs"
                        )
                    polygon.append(point)
            elif polygon_value:
                raise ValueError("operating_polygon must be a sequence of points")
        max_radius = source.get(
            "max_radius_from_home_m",
            source.get("max_home_radius_m", source.get("home_radius_m")),
        )
        max_altitude = source.get("max_altitude_m", source.get("max_agl_m"))
        min_altitude = source.get("min_altitude_m", source.get("min_agl_m", 0.0))
        max_translation = source.get("max_translation_m", default_max_translation_m)
        rth = source.get(
            "rth_after_rejections",
            source.get("return_home_after_rejections", 0),
        )
        return cls(
            operating_polygon=tuple(polygon),
            max_radius_from_home_m=None if max_radius is None else float(max_radius),
            min_altitude_m=float(min_altitude),
            max_altitude_m=None if max_altitude is None else float(max_altitude),
            max_translation_m=float(max_translation),
            rth_after_rejections=int(rth),
            hover_seconds=float(source.get("safety_hover_seconds", 1.0)),
        )


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """The filtered primitive and a label-ready audit record."""

    action: ActionPrimitive
    status: str
    reasons: tuple[str, ...] = ()
    rejected: bool = False
    return_home: bool = False
    consecutive_rejections: int = 0

    def info(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "status": self.status,
            "reasons": list(self.reasons),
            "rejected": self.rejected,
            "consecutive_rejections": self.consecutive_rejections,
        }
        if self.return_home:
            record["return_home"] = True
        return {"safety": record}


@dataclass(slots=True)
class SafetyEnvelope:
    """Validate, clip, or veto a primitive at the host/PX4 boundary."""

    config: SafetyEnvelopeConfig = field(default_factory=SafetyEnvelopeConfig)
    obstacle_check: ObstacleCheck | None = None
    home_ned: Point | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _consecutive_rejections: int = 0

    def set_home(self, home_ned: Point) -> None:
        self.home_ned = (float(home_ned[0]), float(home_ned[1]))

    @property
    def consecutive_rejections(self) -> int:
        return self._consecutive_rejections

    def reset(self) -> None:
        self._consecutive_rejections = 0

    def _reject(self, reasons: list[str], *, stop: bool = False) -> SafetyDecision:
        self._consecutive_rejections += 1
        return_home = (
            self.config.rth_after_rejections > 0
            and self._consecutive_rejections >= self.config.rth_after_rejections
        )
        action = ActionPrimitive.stop() if stop else ActionPrimitive.hold(self.config.hover_seconds)
        decision = SafetyDecision(
            action=action,
            status="rejected",
            reasons=tuple(dict.fromkeys(reasons)),
            rejected=True,
            return_home=return_home,
            consecutive_rejections=self._consecutive_rejections,
        )
        self._record(decision)
        return decision

    def _accept(self, action: ActionPrimitive, reasons: list[str]) -> SafetyDecision:
        clipped = bool(reasons)
        self._consecutive_rejections = 0
        decision = SafetyDecision(
            action=action,
            status="clipped" if clipped else "accepted",
            reasons=tuple(dict.fromkeys(reasons)),
            consecutive_rejections=0,
        )
        self._record(decision)
        return decision

    def _record(self, decision: SafetyDecision) -> None:
        self.events.append(
            {
                "status": decision.status,
                "reasons": list(decision.reasons),
                "rejected": decision.rejected,
                "return_home": decision.return_home,
                "consecutive_rejections": decision.consecutive_rejections,
            }
        )
        del self.events[:-128]

    def _inside_limits(self, point: Point) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        polygon = self.config.operating_polygon
        if polygon and not _polygon_contains(point, polygon):
            reasons.append("geofence")
        if self.config.max_radius_from_home_m is not None and self.home_ned is not None:
            if math.dist(point, self.home_ned) > self.config.max_radius_from_home_m + 1e-9:
                reasons.append("home_radius")
        return not reasons, reasons

    def _segment_safe(self, pose_ned: Pose, end: Point, target_altitude: float) -> bool:
        start = (pose_ned[0], pose_ned[1])
        polygon = self.config.operating_polygon
        if polygon and not _polygon_segment_clear(start, end, polygon):
            return False
        if not self._inside_limits(end)[0]:
            return False
        if self.obstacle_check is None:
            return True
        # Use the lower altitude for a conservative horizontal obstacle check.
        conservative_pose = (pose_ned[0], pose_ned[1], -min(-pose_ned[2], target_altitude))
        return bool(self.obstacle_check(conservative_pose, end[0], end[1]))

    def _max_safe_fraction(
        self, pose_ned: Pose, vector: Point, target_altitude: float
    ) -> float | None:
        start = (pose_ned[0], pose_ned[1])
        if not self._inside_limits(start)[0]:
            return None
        if not self._segment_safe(pose_ned, start, target_altitude):
            return None
        end = (start[0] + vector[0], start[1] + vector[1])
        if self._segment_safe(pose_ned, end, target_altitude):
            return 1.0
        # Find the first unsafe sample, then refine the boundary.  This handles
        # both convex fences and the circular home-radius limit without a GIS
        # dependency, and preserves a safe prefix for obstacle footprints.
        safe_t = 0.0
        unsafe_t = 1.0
        for index in range(1, 129):
            candidate_t = index / 128.0
            candidate = (start[0] + vector[0] * candidate_t, start[1] + vector[1] * candidate_t)
            if not self._segment_safe(pose_ned, candidate, target_altitude):
                unsafe_t = candidate_t
                break
            safe_t = candidate_t
        if unsafe_t == 1.0 and safe_t < 1.0:
            unsafe_t = 1.0
        for _ in range(32):
            middle = (safe_t + unsafe_t) / 2.0
            candidate = (start[0] + vector[0] * middle, start[1] + vector[1] * middle)
            if self._segment_safe(pose_ned, candidate, target_altitude):
                safe_t = middle
            else:
                unsafe_t = middle
        return max(0.0, safe_t - 1e-9) if safe_t > 1e-9 else None

    def apply(self, action: ActionPrimitive, *, pose_ned: Pose, yaw_deg: float) -> SafetyDecision:
        """Return the only version of ``action`` allowed to leave the host."""
        if len(pose_ned) != 3 or not all(math.isfinite(float(value)) for value in pose_ned):
            return self._reject(["non_finite_pose"], stop=True)
        if self.home_ned is None:
            self.set_home((float(pose_ned[0]), float(pose_ned[1])))
        if action.kind == "stop":
            return self._accept(action, [])

        altitude = -float(pose_ned[2])
        max_altitude = self.config.max_altitude_m
        altitude_valid = self.config.min_altitude_m - 1e-9 <= altitude and (
            max_altitude is None or altitude <= max_altitude + 1e-9
        )
        if action.kind in {"turn", "hold"}:
            if altitude_valid:
                return self._accept(action, [])
            return self._reject(["altitude"])
        if action.kind != "translate":
            return self._reject(["unsupported_primitive"], stop=True)

        reasons: list[str] = []
        forward, right, up = float(action.forward_m), float(action.right_m), float(action.up_m)
        if altitude < self.config.min_altitude_m - 1e-9:
            if up <= 0.0:
                return self._reject(["min_altitude"])
            forward = right = 0.0
            up = self.config.min_altitude_m - altitude
            reasons.append("min_altitude")
        elif max_altitude is not None and altitude > max_altitude + 1e-9:
            if up >= 0.0:
                return self._reject(["max_altitude"])
            forward = right = 0.0
            up = max_altitude - altitude
            reasons.append("max_altitude")
        vertical_required = abs(up)
        if vertical_required > self.config.max_translation_m + 1e-9:
            return self._reject(["max_translation", "altitude"])
        horizontal_limit = math.sqrt(
            max(0.0, self.config.max_translation_m**2 - vertical_required**2)
        )
        horizontal = math.hypot(forward, right)
        if horizontal > horizontal_limit + 1e-9:
            scale = horizontal_limit / horizontal
            forward, right = forward * scale, right * scale
            reasons.append("max_translation")

        target_altitude = altitude + up
        if target_altitude < self.config.min_altitude_m:
            up = self.config.min_altitude_m - altitude
            target_altitude = self.config.min_altitude_m
            reasons.append("min_altitude")
        if max_altitude is not None and target_altitude > max_altitude:
            up = max_altitude - altitude
            target_altitude = max_altitude
            reasons.append("max_altitude")

        north, east, _ = body_to_ned(forward, right, up, yaw_deg)
        full_end = (pose_ned[0] + north, pose_ned[1] + east)
        fraction = self._max_safe_fraction(pose_ned, (north, east), target_altitude)
        if fraction is None:
            unsafe_reasons = self._inside_limits((pose_ned[0], pose_ned[1]))[1]
            if self.config.operating_polygon and not _polygon_segment_clear(
                (pose_ned[0], pose_ned[1]), full_end, self.config.operating_polygon
            ):
                unsafe_reasons.append("geofence")
            if self.config.max_radius_from_home_m is not None and self.home_ned is not None:
                if math.dist(full_end, self.home_ned) > self.config.max_radius_from_home_m:
                    unsafe_reasons.append("home_radius")
            if self.obstacle_check is not None and not self._segment_safe(
                pose_ned, full_end, target_altitude
            ):
                unsafe_reasons.append("static_obstacle")
            return self._reject(reasons + unsafe_reasons or ["static_obstacle"])
        if fraction < 1.0 - 1e-9:
            north, east = north * fraction, east * fraction
            if self.config.operating_polygon and not _polygon_segment_clear(
                (pose_ned[0], pose_ned[1]), full_end, self.config.operating_polygon
            ):
                reasons.append("geofence")
            if self.config.max_radius_from_home_m is not None and self.home_ned is not None:
                if math.dist(full_end, self.home_ned) > self.config.max_radius_from_home_m:
                    reasons.append("home_radius")
            if self.obstacle_check is not None and not self._segment_safe(
                pose_ned, full_end, target_altitude
            ):
                reasons.append("static_obstacle")
        yaw = math.radians(yaw_deg)
        clipped_forward = north * math.cos(yaw) + east * math.sin(yaw)
        clipped_right = -north * math.sin(yaw) + east * math.cos(yaw)
        clipped = ActionPrimitive.translate(clipped_forward, clipped_right, up)
        return self._accept(clipped, reasons)


__all__ = ["SafetyDecision", "SafetyEnvelope", "SafetyEnvelopeConfig"]
