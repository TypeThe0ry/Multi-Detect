"""Final motion primitive safety envelope.

The envelope is intentionally a small, policy independent arbiter.  It receives
the primitive after mission policy/supervisor decisions and returns the only
primitive that may be sent to the runtime.  It never mutates an executor or
supervisor; callers report :class:`SafetyDecision.reasons` to their supervisor.

Coordinates are local NED relative to the mission home.  ``min_altitude_m`` and
``max_altitude_m`` are therefore home-relative altitude limits.  AGL limits are
enforced only when a fresh ``current_agl_m`` (or ``ground_model`` + pose) is
available; otherwise home-relative limits remain authoritative.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned

from .obstacle_source import ObstacleSource

Point = tuple[float, float]
Pose = tuple[float, float, float]


def _point(value: Sequence[float]) -> Point | None:
    if isinstance(value, (str, bytes)):
        return None
    try:
        if len(value) != 2:
            return None
    except TypeError:
        return None
    try:
        p = (float(value[0]), float(value[1]))
    except (TypeError, ValueError):
        return None
    return p if all(math.isfinite(v) for v in p) else None


def _contains(point: Point, polygon: tuple[Point, ...]) -> bool:
    """Point in polygon, with boundary considered inside."""
    if len(polygon) < 3:
        return True
    x, y = point
    inside = False
    for a, b in zip(polygon, (*polygon[1:], polygon[0]), strict=True):
        ax, ay, bx, by = *a, *b
        cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
        if (
            abs(cross) <= 1e-9
            and min(ax, bx) - 1e-9 <= x <= max(ax, bx) + 1e-9
            and min(ay, by) - 1e-9 <= y <= max(ay, by) + 1e-9
        ):
            return True
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return inside


def _segment_inside(start: Point, end: Point, polygon: tuple[Point, ...]) -> bool:
    """Conservatively sample a segment; catches concave polygon exits."""
    if len(polygon) < 3:
        return True
    distance = math.dist(start, end)
    count = max(2, int(math.ceil(distance / 0.25)) + 1)
    return all(
        _contains(
            (
                start[0] + (end[0] - start[0]) * i / (count - 1),
                start[1] + (end[1] - start[1]) * i / (count - 1),
            ),
            polygon,
        )
        for i in range(count)
    )


def _segment_hits(start: Point, end: Point, polygon: tuple[Point, ...]) -> bool:
    """Return true when any part of a segment lies in a polygon."""
    if len(polygon) < 3:
        return False
    distance = math.dist(start, end)
    count = max(2, int(math.ceil(distance / 0.25)) + 1)
    return any(
        _contains(
            (
                start[0] + (end[0] - start[0]) * i / (count - 1),
                start[1] + (end[1] - start[1]) * i / (count - 1),
            ),
            polygon,
        )
        for i in range(count)
    )


@dataclass(frozen=True, slots=True)
class VehicleDynamics:
    """Small airframe-independent flight envelope supplied by the caller.

    Values are deliberately plain engineering parameters so the eventual
    AirframeProfile adapter can populate this object without coupling this
    arbiter to profile storage.
    """

    category: str = "multirotor"
    can_hover: bool = True
    min_airspeed_mps: float = 0.0
    cruise_airspeed_mps: float = 5.0
    max_airspeed_mps: float = 100.0
    min_turn_radius_m: float = 0.0
    max_bank_deg: float = 45.0
    climb_rate_mps: float = 2.0
    descent_rate_mps: float = 1.0

    def __post_init__(self) -> None:
        if self.category not in {"multirotor", "fixed_wing", "vtol"}:
            raise ValueError("category must be multirotor, fixed_wing or vtol")
        if not isinstance(self.can_hover, bool):
            raise ValueError("can_hover must be boolean")
        values = (
            self.min_airspeed_mps,
            self.cruise_airspeed_mps,
            self.max_airspeed_mps,
            self.min_turn_radius_m,
            self.max_bank_deg,
            self.climb_rate_mps,
            self.descent_rate_mps,
        )
        if any(not math.isfinite(float(v)) or float(v) < 0 for v in values):
            raise ValueError("vehicle dynamics must be finite and non-negative")
        if self.max_airspeed_mps < self.min_airspeed_mps:
            raise ValueError("max_airspeed_mps must be >= min_airspeed_mps")
        if self.category == "fixed_wing" and self.min_airspeed_mps <= 0:
            raise ValueError("fixed-wing min_airspeed_mps must be positive")


@dataclass(frozen=True, slots=True)
class SafetyEnvelopeConfig:
    operating_polygon: tuple[Point, ...] = ()
    no_fly_polygons: tuple[tuple[Point, ...], ...] = ()
    max_radius_from_home_m: float | None = None
    min_altitude_m: float = 0.0
    max_altitude_m: float | None = None
    min_agl_m: float | None = None
    max_translation_m: float = 15.0
    max_horizontal_step_m: float | None = None
    max_vertical_step_m: float = 15.0
    rth_after_rejections: int = 0
    hover_seconds: float = 1.0
    low_altitude_exception_m: float = 0.0
    vehicle: VehicleDynamics = field(default_factory=VehicleDynamics)

    def __post_init__(self) -> None:
        if self.operating_polygon and len(self.operating_polygon) < 3:
            raise ValueError("operating_polygon must have at least three points")
        if any(len(p) < 3 for p in self.no_fly_polygons):
            raise ValueError("no_fly_polygons must contain polygons")
        if not math.isfinite(self.min_altitude_m) or self.min_altitude_m < 0:
            raise ValueError("min_altitude_m must be finite and non-negative")
        for name, value in (("max_altitude_m", self.max_altitude_m), ("min_agl_m", self.min_agl_m)):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_altitude_m is not None and self.max_altitude_m < self.min_altitude_m:
            raise ValueError("max_altitude_m must be at least min_altitude_m")
        if not math.isfinite(self.max_translation_m) or self.max_translation_m <= 0:
            raise ValueError("max_translation_m must be positive")
        if self.max_horizontal_step_m is not None and (
            not math.isfinite(self.max_horizontal_step_m) or self.max_horizontal_step_m <= 0
        ):
            raise ValueError("max_horizontal_step_m must be positive")
        if not math.isfinite(self.max_vertical_step_m) or self.max_vertical_step_m <= 0:
            raise ValueError("max_vertical_step_m must be positive")
        if self.max_radius_from_home_m is not None and (
            not math.isfinite(self.max_radius_from_home_m) or self.max_radius_from_home_m <= 0
        ):
            raise ValueError("max_radius_from_home_m must be positive")
        if not math.isfinite(self.low_altitude_exception_m) or self.low_altitude_exception_m < 0:
            raise ValueError("low_altitude_exception_m must be finite and non-negative")
        if not isinstance(self.vehicle, VehicleDynamics):
            raise ValueError("vehicle must be VehicleDynamics")

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, Any] | None = None, *, default_max_translation_m: float = 15.0
    ) -> SafetyEnvelopeConfig:
        src: Mapping[str, Any] = values or {}
        nested = src.get("safety_envelope")
        if isinstance(nested, Mapping):
            src = {**src, **nested}

        def polygons(value: Any) -> tuple[tuple[Point, ...], ...]:
            if value is None:
                return ()
            if isinstance(value, Mapping):
                value = value.get("polygons", value.get("points", ()))
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                raise ValueError("polygon list must be a sequence")
            # Accept one polygon as [[n,e], ...], or a list of polygons.
            if value and _point(value[0]) is not None:
                value = (value,)
            result = []
            for poly in value:
                pts = tuple(_point(p) for p in poly) if isinstance(poly, Sequence) else ()
                if not pts or any(p is None for p in pts):
                    raise ValueError("polygon points must be finite [north,east] pairs")
                result.append(tuple(p for p in pts if p is not None))
            return tuple(result)

        vehicle_values = src.get("vehicle", src.get("vehicle_dynamics", {}))
        if not isinstance(vehicle_values, Mapping):
            vehicle_values = {}

        def vget(name: str, default: Any) -> Any:
            return vehicle_values.get(name, src.get(name, default))

        return cls(
            operating_polygon=(
                polygons(src.get("operating_polygon", src.get("geofence_polygon", ()))) or ((),)
            )[0],
            no_fly_polygons=polygons(src.get("no_fly_polygons", src.get("no_fly_zones", ()))),
            max_radius_from_home_m=(
                None
                if src.get("max_radius_from_home_m", src.get("home_radius_m")) is None
                else float(src.get("max_radius_from_home_m", src.get("home_radius_m")))
            ),
            min_altitude_m=float(src.get("min_altitude_m", 0.0)),
            max_altitude_m=(
                None if src.get("max_altitude_m") is None else float(src["max_altitude_m"])
            ),
            min_agl_m=(None if src.get("min_agl_m") is None else float(src["min_agl_m"])),
            max_translation_m=float(src.get("max_translation_m", default_max_translation_m)),
            max_horizontal_step_m=(
                None
                if src.get("max_horizontal_step_m") is None
                else float(src["max_horizontal_step_m"])
            ),
            max_vertical_step_m=float(
                src.get(
                    "max_vertical_step_m", src.get("max_translation_m", default_max_translation_m)
                )
            ),
            rth_after_rejections=int(src.get("rth_after_rejections", 0)),
            hover_seconds=float(src.get("safety_hover_seconds", 1.0)),
            low_altitude_exception_m=float(src.get("low_altitude_exception_m", 0.0)),
            vehicle=VehicleDynamics(
                category=str(vget("category", vget("vehicle_category", "multirotor"))),
                can_hover=bool(vget("can_hover", True)),
                min_airspeed_mps=float(vget("min_airspeed_mps", 0.0)),
                cruise_airspeed_mps=float(
                    vget("cruise_airspeed_mps", vget("cruise_speed_mps", 5.0))
                ),
                max_airspeed_mps=float(vget("max_airspeed_mps", 100.0)),
                min_turn_radius_m=float(vget("min_turn_radius_m", 0.0)),
                max_bank_deg=float(vget("max_bank_deg", 45.0)),
                climb_rate_mps=float(vget("climb_rate_mps", 2.0)),
                descent_rate_mps=float(vget("descent_rate_mps", 1.0)),
            ),
        )


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    action: ActionPrimitive
    status: str
    reasons: tuple[str, ...] = ()
    rejected: bool = False
    return_home: bool = False
    consecutive_rejections: int = 0
    # Kept for API compatibility; vertical motion is clipped per frame and
    # callers re-issue the remainder on the next frame.
    segments: tuple[ActionPrimitive, ...] = ()
    recovery_action: str = "hold"

    def info(self) -> dict[str, Any]:
        safety = {
            "status": self.status,
            "reasons": list(self.reasons),
            "rejected": self.rejected,
            "return_home": self.return_home,
            "consecutive_rejections": self.consecutive_rejections,
        }
        if self.recovery_action != "hold":
            safety["recovery_action"] = self.recovery_action
        return {"safety": safety}


@dataclass(slots=True)
class SafetyEnvelope:
    config: SafetyEnvelopeConfig = field(default_factory=SafetyEnvelopeConfig)
    obstacle_source: ObstacleSource | None = None
    home_ned: Pose | Point | None = None
    ground_model: Any | None = None
    # Kept as a read-only compatibility shim; all actual checks use source.
    obstacle_check: Any | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    _consecutive_rejections: int = 0
    vehicle: VehicleDynamics | None = None
    vehicle_dynamics: VehicleDynamics | None = None

    def __post_init__(self) -> None:
        if self.obstacle_source is None and self.obstacle_check is not None:
            raise TypeError("obstacle_check is unsupported; provide an ObstacleSource")
        if self.vehicle_dynamics is not None:
            self.vehicle = self.vehicle_dynamics
        if self.vehicle is None:
            self.vehicle = self.config.vehicle

    def set_home(self, home_ned: Sequence[float]) -> None:
        vals = tuple(float(v) for v in home_ned)
        if len(vals) == 2:
            self.home_ned = (vals[0], vals[1], 0.0)
        elif len(vals) == 3:
            self.home_ned = vals
        else:
            raise ValueError("home_ned must contain 2 or 3 values")

    @property
    def consecutive_rejections(self) -> int:
        return self._consecutive_rejections

    def reset(self) -> None:
        self._consecutive_rejections = 0

    def _reject(self, reasons: list[str], *, stop: bool = False) -> SafetyDecision:
        self._consecutive_rejections += 1
        threshold = self.config.rth_after_rejections
        action = ActionPrimitive.stop() if stop else ActionPrimitive.hold(self.config.hover_seconds)
        recovery = "loiter" if self.vehicle.category == "fixed_wing" else "hold"
        decision = SafetyDecision(
            action,
            "rejected",
            tuple(dict.fromkeys(reasons)),
            True,
            threshold > 0 and self._consecutive_rejections >= threshold,
            self._consecutive_rejections,
            recovery_action=recovery,
        )
        self._record(decision)
        return decision

    def _accept(
        self,
        action: ActionPrimitive,
        reasons: list[str],
        segments: tuple[ActionPrimitive, ...] = (),
    ) -> SafetyDecision:
        self._consecutive_rejections = 0
        decision = SafetyDecision(
            action,
            "clipped" if reasons else "accepted",
            tuple(dict.fromkeys(reasons)),
            False,
            False,
            0,
            segments,
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

    def _altitude(self, pose: Pose, current_agl_m: float | None) -> tuple[float, float | None]:
        home_down = (
            float(self.home_ned[2]) if self.home_ned is not None and len(self.home_ned) > 2 else 0.0
        )  # type: ignore[arg-type]
        if current_agl_m is None and self.ground_model is not None:
            # GroundModel implementations vary: FlatGround exposes a plane,
            # while telemetry adapters commonly expose ``agl_m``/``height_at``.
            model = self.ground_model
            try:
                if hasattr(model, "agl_m"):
                    current_agl_m = float(model.agl_m(pose))
                elif hasattr(model, "height_at"):
                    current_agl_m = (
                        home_down - float(pose[2]) - float(model.height_at(pose[0], pose[1]))
                    )
                elif hasattr(model, "ground_up_m"):
                    current_agl_m = home_down - float(pose[2]) - float(model.ground_up_m)
            except (TypeError, ValueError, AttributeError):
                current_agl_m = None
        return home_down - float(pose[2]), (None if current_agl_m is None else float(current_agl_m))

    def effective_radius_from_home(self) -> float | None:
        """Return configured radius, useful when composing package fences."""
        return self.config.max_radius_from_home_m

    def _spatial_reasons(self, start: Point, end: Point) -> list[str]:
        reasons: list[str] = []
        if self.config.operating_polygon and not _segment_inside(
            start, end, self.config.operating_polygon
        ):
            reasons.append("geofence")
        if self.config.max_radius_from_home_m is not None and self.home_ned is not None:
            home = (float(self.home_ned[0]), float(self.home_ned[1]))  # type: ignore[index]
            if math.dist(end, home) > self.config.max_radius_from_home_m + 1e-9:
                reasons.append("home_radius")
        for zone in self.config.no_fly_polygons:
            if _segment_hits(start, end, zone):
                reasons.append("no_fly")
        return reasons

    def _segment_clear(self, start: Pose, end: Pose) -> bool:
        if self.obstacle_source is None:
            return True
        return bool(self.obstacle_source.segment_clear(start, end))

    def _arc_clear(self, pose: Pose, yaw_deg: float, delta_deg: float) -> tuple[bool, list[str]]:
        """Sample a fixed-wing turn arc for fences and obstacles."""
        radius = self.vehicle.min_turn_radius_m
        if radius <= 0 or abs(delta_deg) <= 1e-9:
            return True, []
        count = max(3, int(math.ceil(abs(delta_deg) / 5.0)) + 1)
        heading = math.radians(yaw_deg)
        sign = 1.0 if delta_deg >= 0 else -1.0
        # The turn centre is to the left/right of the current forward vector.
        cx = pose[0] - sign * radius * math.sin(heading)
        cy = pose[1] + sign * radius * math.cos(heading)
        previous = (pose[0], pose[1], pose[2])
        reasons: list[str] = []
        for i in range(1, count):
            angle = (
                heading - sign * math.pi / 2 + sign * math.radians(abs(delta_deg)) * i / (count - 1)
            )
            point = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            reasons.extend(self._spatial_reasons(point, point))
            current = (point[0], point[1], pose[2])
            if not self._segment_clear(previous, current):
                reasons.append("obstacle")
            previous = current
        return not reasons, list(dict.fromkeys(reasons))

    def split_vertical(self, action: ActionPrimitive) -> tuple[ActionPrimitive, ...]:
        """Compatibility helper: return the single bounded action for this frame."""
        if action.kind != "translate" or abs(action.up_m) <= self.config.max_vertical_step_m + 1e-9:
            return (action,)
        return (
            ActionPrimitive.translate(
                action.forward_m,
                action.right_m,
                math.copysign(self.config.max_vertical_step_m, action.up_m),
            ),
        )

    def apply(
        self,
        action: ActionPrimitive,
        *,
        pose_ned: Pose,
        yaw_deg: float = 0.0,
        current_agl_m: float | None = None,
        agl_m: float | None = None,
        phase: str | None = None,
        skill: str | None = None,
        airspeed_mps: float | None = None,
        bank_deg: float | None = None,
        turn_radius_m: float | None = None,
        climb_rate_mps: float | None = None,
        descent_rate_mps: float | None = None,
        mode: str | None = None,
        duration_s: float | None = None,
    ) -> SafetyDecision:
        """Filter one primitive; long vertical moves are clipped for this frame."""
        pose = tuple(float(v) for v in pose_ned)
        if len(pose) != 3 or not all(math.isfinite(v) for v in pose):
            return self._reject(["non_finite_pose"], stop=True)
        if action.kind == "stop":
            return self._accept(action, [])
        vehicle = self.vehicle
        mode_name = str(mode or "").lower().replace("-", "_")
        if vehicle.category in {"fixed_wing", "vtol"}:
            if vehicle.category == "vtol" and mode_name in {"mc", "multirotor", "hover"}:
                active = "multirotor"
            else:
                active = mode_name or ("fw" if vehicle.category == "fixed_wing" else "transition")
                if vehicle.category == "vtol" and active not in {
                    "fw",
                    "fixed_wing",
                    "transition",
                    "trans",
                }:
                    active = "transition"
            if active in {"fw", "fixed_wing", "transition"}:
                if airspeed_mps is None and (
                    vehicle.category == "fixed_wing" or active == "transition"
                ):
                    return self._reject(["airspeed_unknown"])
                if airspeed_mps is not None and (
                    airspeed_mps < vehicle.min_airspeed_mps - 1e-9
                    or airspeed_mps > vehicle.max_airspeed_mps + 1e-9
                ):
                    return self._reject(["airspeed"])
                if bank_deg is not None:
                    limit = vehicle.max_bank_deg * (0.5 if active == "transition" else 1.0)
                    if abs(bank_deg) > limit + 1e-9:
                        return self._reject(["bank"])
                if turn_radius_m is not None and turn_radius_m < vehicle.min_turn_radius_m - 1e-9:
                    return self._reject(["turn_radius"])
        if current_agl_m is None:
            current_agl_m = agl_m
        altitude, agl = self._altitude(pose, current_agl_m)
        if vehicle.category == "vtol" and active == "transition":
            if altitude < max(self.config.min_altitude_m, 5.0) - 1e-9:
                return self._reject(["transition_altitude"])
        low_exception = str(skill or "").lower() in {"deliver", "land", "landing"} and str(
            phase or ""
        ).lower() in {
            "descent",
            "release",
            "land",
            "landing",
            "landing_commanded",
            "landing_pending",
            "low_altitude",
        }
        min_alt = (
            max(self.config.low_altitude_exception_m, 0.0)
            if low_exception
            else self.config.min_altitude_m
        )
        if action.kind in {"turn", "hold"}:
            if (
                altitude < min_alt - 1e-9
                or (
                    self.config.max_altitude_m is not None
                    and altitude > self.config.max_altitude_m + 1e-9
                )
                or (
                    self.config.min_agl_m is not None
                    and agl is not None
                    and agl < self.config.min_agl_m - 1e-9
                )
            ):
                return self._reject(["altitude"])
            if (
                vehicle.category in {"fixed_wing", "vtol"}
                and action.kind == "turn"
                and active != "multirotor"
            ):
                clear, reasons = self._arc_clear(pose, yaw_deg, action.yaw_delta_deg)
                if not clear:
                    return self._reject(reasons)
            return self._accept(action, [])
        if action.kind != "translate":
            return self._reject(["unsupported_primitive"], stop=True)
        forward, right, up = float(action.forward_m), float(action.right_m), float(action.up_m)
        reasons: list[str] = []
        target_alt = altitude + up
        # Clamp altitude while preserving the horizontal component.
        if target_alt < min_alt:
            up = min_alt - altitude
            target_alt = min_alt
            reasons.append("min_altitude")
        if self.config.max_altitude_m is not None and target_alt > self.config.max_altitude_m:
            up = self.config.max_altitude_m - altitude
            target_alt = self.config.max_altitude_m
            reasons.append("max_altitude")
        if (
            self.config.min_agl_m is not None
            and agl is not None
            and agl + up < self.config.min_agl_m
            and not low_exception
        ):
            up = self.config.min_agl_m - agl
            target_alt = altitude + up
            reasons.append("min_agl")
        horizontal = math.hypot(forward, right)
        hlimit = self.config.max_horizontal_step_m or self.config.max_translation_m
        if horizontal > hlimit + 1e-9:
            scale = hlimit / horizontal
            forward *= scale
            right *= scale
            reasons.append("max_horizontal_step")
        if abs(up) > self.config.max_vertical_step_m + 1e-9:
            up = math.copysign(self.config.max_vertical_step_m, up)
            reasons.append("max_vertical_step")
        if vehicle.category in {"fixed_wing", "vtol"} and duration_s and duration_s > 0:
            rate = abs(up) / duration_s
            limit = vehicle.climb_rate_mps if up >= 0 else vehicle.descent_rate_mps
            if limit > 0 and rate > limit + 1e-9:
                reasons.append("climb_rate" if up >= 0 else "descent_rate")
        if vehicle.category in {"fixed_wing", "vtol"}:
            measured_rate = climb_rate_mps if up >= 0 else descent_rate_mps
            limit = vehicle.climb_rate_mps if up >= 0 else vehicle.descent_rate_mps
            if measured_rate is not None and limit > 0 and abs(measured_rate) > limit + 1e-9:
                reasons.append("climb_rate" if up >= 0 else "descent_rate")
        north, east, _ = body_to_ned(forward, right, up, yaw_deg)
        end = (pose[0] + north, pose[1] + east)
        reasons.extend(self._spatial_reasons((pose[0], pose[1]), end))
        if not self._segment_clear(pose, (end[0], end[1], pose[2] - up)):
            reasons.append("obstacle")
        if (
            vehicle.category in {"fixed_wing", "vtol"}
            and active == "transition"
            and abs(action.yaw_delta_deg) > 0
        ):
            reasons.append("transition_bank")
        if any(
            r in reasons
            for r in (
                "geofence",
                "home_radius",
                "no_fly",
                "obstacle",
                "climb_rate",
                "descent_rate",
                "transition_bank",
            )
        ):
            return self._reject(reasons)
        # Preserve the original object/value representation when the default
        # envelope did not change the primitive.  This keeps serialized actions
        # byte-identical for existing valid policy output (including ints).
        if (
            not reasons
            and forward == action.forward_m
            and right == action.right_m
            and up == action.up_m
        ):
            filtered = action
        else:
            filtered = ActionPrimitive.translate(forward, right, up)
        return self._accept(filtered, reasons)

    # Runtime/policy integrations use ``arbitrate`` to make the single return
    # point explicit.  Keep ``apply`` as the implementation and compatibility
    # entry point for existing SIM callers.
    arbitrate = apply


__all__ = ["VehicleDynamics", "SafetyDecision", "SafetyEnvelope", "SafetyEnvelopeConfig"]
