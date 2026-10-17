"""Pure PX4 fixed-wing/VTOL MAVLink command encoders.

The functions in this module only validate values and return a command id plus
the seven MAVLink ``COMMAND_LONG`` parameters.  They deliberately do not
import pymavlink, so command encoding can be tested on a CPU-only host.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MAV_CMD_NAV_LOITER_UNLIM = 17
MAV_CMD_NAV_LOITER_TIME = 19
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_TAKEOFF = 22
MAV_CMD_NAV_LAND = 21
MAV_CMD_NAV_LOITER_TO_ALT = 31
MAV_CMD_NAV_VTOL_TAKEOFF = 84
MAV_CMD_NAV_VTOL_LAND = 85
MAV_CMD_DO_ORBIT = 34
MAV_CMD_DO_CHANGE_SPEED = 178
MAV_CMD_DO_LAND_START = 189
MAV_CMD_DO_REPOSITION = 192
MAV_CMD_DO_VTOL_TRANSITION = 3000

MAV_VTOL_STATE_MC = 3
MAV_VTOL_STATE_FW = 4

PX4_CUSTOM_MAIN_MODE_AUTO = 4
PX4_CUSTOM_MAIN_MODE_POSCTL = 3
PX4_CUSTOM_MAIN_MODE_OFFBOARD = 6
PX4_CUSTOM_SUB_MODE_AUTO_TAKEOFF = 2
PX4_CUSTOM_SUB_MODE_AUTO_LOITER = 3
PX4_CUSTOM_SUB_MODE_AUTO_MISSION = 4
PX4_CUSTOM_SUB_MODE_AUTO_RTL = 5
PX4_CUSTOM_SUB_MODE_AUTO_LAND = 6

PX4_AUTO_TAKEOFF_CUSTOM_MODE = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_TAKEOFF << 24
)
PX4_AUTO_LOITER_CUSTOM_MODE = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_LOITER << 24
)
PX4_AUTO_MISSION_CUSTOM_MODE = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_MISSION << 24
)
PX4_AUTO_RTL_CUSTOM_MODE = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_RTL << 24
)
PX4_AUTO_LAND_CUSTOM_MODE = (PX4_CUSTOM_MAIN_MODE_AUTO << 16) | (
    PX4_CUSTOM_SUB_MODE_AUTO_LAND << 24
)
PX4_POSCTL_CUSTOM_MODE = PX4_CUSTOM_MAIN_MODE_POSCTL << 16
PX4_OFFBOARD_CUSTOM_MODE = PX4_CUSTOM_MAIN_MODE_OFFBOARD << 16


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A command-long wire value, with exactly seven float parameters."""

    command: int
    params: tuple[float, ...]
    name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.command, int) or self.command < 0:
            raise ValueError("command must be a non-negative integer")
        if len(self.params) != 7 or any(not math.isfinite(float(v)) for v in self.params):
            raise ValueError("command params must contain seven finite values")

    @property
    def args(self) -> tuple[float, ...]:
        return self.params

    @property
    def command_id(self) -> int:
        return self.command

    def __iter__(self):
        """Allow ``command, params = spec`` in small adapters/tests."""
        yield self.command
        yield self.params


def _finite(value: Any, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _point(point: Any, *, alt: float | None = None) -> tuple[float, float, float]:
    if isinstance(point, dict):
        lat = point.get("lat", point.get("latitude"))
        lon = point.get("lon", point.get("longitude"))
        point_alt = point.get("alt", point.get("altitude", alt))
    else:
        try:
            values = tuple(point)
        except TypeError as exc:
            raise ValueError("point must be a two/three element sequence") from exc
        if len(values) not in (2, 3):
            raise ValueError("point must contain latitude, longitude and optional altitude")
        lat, lon = values[:2]
        point_alt = values[2] if len(values) == 3 else alt
    if point_alt is None:
        raise ValueError("altitude is required")
    return _finite(lat, "latitude"), _finite(lon, "longitude"), _finite(point_alt, "altitude")


def _spec(command: int, params: tuple[float, ...], name: str) -> CommandSpec:
    if len(params) > 7:
        raise ValueError("MAVLink command has at most seven parameters")
    return CommandSpec(command, tuple(params) + (0.0,) * (7 - len(params)), name)


def do_reposition(point: Any, *, alt: float | None = None, ground_speed: float = -1.0,
                  radius: float = 0.0, yaw_deg: float = 0.0,
                  change_mode: bool = True) -> CommandSpec:
    """PX4 guided fixed-wing reposition (lat/lon/alt in params 5..7)."""
    lat, lon, altitude = _point(point, alt=alt)
    speed = _finite(ground_speed, "ground_speed")
    radius = _finite(radius, "radius")
    if radius < 0:
        raise ValueError("radius must be non-negative")
    # PX4's COMMAND_LONG definition carries the plane yaw selector in radians;
    # the public API stays in degrees to match the rest of the runtime.
    yaw = math.radians(_finite(yaw_deg, "yaw_deg"))
    return _spec(MAV_CMD_DO_REPOSITION,
                 (speed, 1.0 if change_mode else 0.0, radius, yaw, lat, lon, altitude),
                 "MAV_CMD_DO_REPOSITION")


def do_orbit(center: Any, *, radius: float, speed: float, yaw_behavior: float = 0.0,
             orbit_count: float = 0.0, alt: float | None = None,
             yaw_deg: float | None = None) -> CommandSpec:
    """PX4 orbit command (radius, tangential speed and lat/lon/alt center)."""
    lat, lon, altitude = _point(center, alt=alt)
    radius = _finite(radius, "radius")
    speed = _finite(speed, "speed")
    if radius <= 0:
        raise ValueError("radius must be positive")
    return _spec(MAV_CMD_DO_ORBIT,
                 (radius, speed, _finite(yaw_behavior, "yaw_behavior"),
                  _finite(orbit_count, "orbit_count"), lat, lon, altitude),
                 "MAV_CMD_DO_ORBIT")


def do_change_speed(speed_mps: float, *, speed_type: float = 0.0,
                    throttle: float = -1.0, relative: float = 0.0) -> CommandSpec:
    return _spec(MAV_CMD_DO_CHANGE_SPEED,
                 (_finite(speed_type, "speed_type"), _finite(speed_mps, "speed_mps"),
                  _finite(throttle, "throttle"), _finite(relative, "relative")),
                 "MAV_CMD_DO_CHANGE_SPEED")


def nav_loiter_unlimited(center: Any, *, radius: float, alt: float | None = None,
                         yaw_deg: float = 0.0) -> CommandSpec:
    lat, lon, altitude = _point(center, alt=alt)
    return _spec(MAV_CMD_NAV_LOITER_UNLIM,
                 (0.0, 0.0, _finite(radius, "radius"), _finite(yaw_deg, "yaw_deg"),
                  lat, lon, altitude), "MAV_CMD_NAV_LOITER_UNLIM")


def nav_loiter_time(center: Any, *, seconds: float, radius: float,
                    alt: float | None = None, yaw_deg: float = 0.0) -> CommandSpec:
    lat, lon, altitude = _point(center, alt=alt)
    return _spec(MAV_CMD_NAV_LOITER_TIME,
                 (_finite(seconds, "seconds"), 0.0, _finite(radius, "radius"),
                  _finite(yaw_deg, "yaw_deg"), lat, lon, altitude), "MAV_CMD_NAV_LOITER_TIME")


def nav_loiter_to_alt(center: Any, *, radius: float, alt: float | None = None,
                      yaw_deg: float = 0.0) -> CommandSpec:
    lat, lon, altitude = _point(center, alt=alt)
    return _spec(MAV_CMD_NAV_LOITER_TO_ALT,
                 (0.0, _finite(radius, "radius"), 0.0, _finite(yaw_deg, "yaw_deg"),
                  lat, lon, altitude), "MAV_CMD_NAV_LOITER_TO_ALT")


def do_vtol_transition(to: str | int) -> CommandSpec:
    if isinstance(to, str):
        normalized = to.strip().upper()
        try:
            target = {"MC": MAV_VTOL_STATE_MC, "MULTICOPTER": MAV_VTOL_STATE_MC,
                      "FW": MAV_VTOL_STATE_FW, "FIXED_WING": MAV_VTOL_STATE_FW}[normalized]
        except KeyError as exc:
            raise ValueError("VTOL target must be MC or FW") from exc
    else:
        target = int(to)
        if target not in (MAV_VTOL_STATE_MC, MAV_VTOL_STATE_FW):
            raise ValueError("VTOL target must be MAV_VTOL_STATE_MC (3) or FW (4)")
    return _spec(MAV_CMD_DO_VTOL_TRANSITION, (float(target),), "MAV_CMD_DO_VTOL_TRANSITION")


def nav_takeoff(*, alt: float, pitch_deg: float = 0.0, yaw_deg: float = 0.0,
                point: Any = (0.0, 0.0)) -> CommandSpec:
    lat, lon, altitude = _point(point, alt=alt)
    return _spec(MAV_CMD_NAV_TAKEOFF,
                 (_finite(pitch_deg, "pitch_deg"), 0.0, 0.0, _finite(yaw_deg, "yaw_deg"),
                  lat, lon, altitude), "MAV_CMD_NAV_TAKEOFF")


def nav_vtol_takeoff(*, alt: float, pitch_deg: float = 0.0, yaw_deg: float = 0.0,
                     point: Any = (0.0, 0.0)) -> CommandSpec:
    spec = nav_takeoff(alt=alt, pitch_deg=pitch_deg, yaw_deg=yaw_deg, point=point)
    return CommandSpec(MAV_CMD_NAV_VTOL_TAKEOFF, spec.params, "MAV_CMD_NAV_VTOL_TAKEOFF")


def nav_vtol_land(*, point: Any = (0.0, 0.0), alt: float = 0.0,
                  yaw_deg: float = 0.0) -> CommandSpec:
    lat, lon, altitude = _point(point, alt=alt)
    return _spec(MAV_CMD_NAV_VTOL_LAND,
                 (0.0, 0.0, 0.0, _finite(yaw_deg, "yaw_deg"), lat, lon, altitude),
                 "MAV_CMD_NAV_VTOL_LAND")


def nav_land(*, point: Any = (0.0, 0.0), alt: float = 0.0,
             yaw_deg: float = 0.0) -> CommandSpec:
    lat, lon, altitude = _point(point, alt=alt)
    return _spec(MAV_CMD_NAV_LAND,
                 (0.0, 0.0, 0.0, _finite(yaw_deg, "yaw_deg"), lat, lon, altitude),
                 "MAV_CMD_NAV_LAND")


def do_land_start(point: Any | None = None, *, alt: float | None = None) -> CommandSpec:
    if point is None:
        return _spec(MAV_CMD_DO_LAND_START, (), "MAV_CMD_DO_LAND_START")
    lat, lon, altitude = _point(point, alt=alt)
    return _spec(MAV_CMD_DO_LAND_START, (0.0, 0.0, 0.0, 0.0, lat, lon, altitude),
                 "MAV_CMD_DO_LAND_START")


_CUSTOM_MODES = {
    "AUTO_TAKEOFF": (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_TAKEOFF),
    "AUTO_LOITER": (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_LOITER),
    "AUTO_MISSION": (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_MISSION),
    "AUTO_RTL": (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_RTL),
    "AUTO_LAND": (PX4_CUSTOM_MAIN_MODE_AUTO, PX4_CUSTOM_SUB_MODE_AUTO_LAND),
    "POSCTL": (PX4_CUSTOM_MAIN_MODE_POSCTL, 0),
    "OFFBOARD": (PX4_CUSTOM_MAIN_MODE_OFFBOARD, 0),
}


def custom_mode(name: str) -> int:
    try:
        main, sub = _CUSTOM_MODES[name.strip().upper()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"unsupported PX4 custom mode: {name}") from exc
    return (main << 16) | (sub << 24)


def parse_custom_mode(value: int | str) -> str:
    if isinstance(value, str):
        return value.strip().upper()
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("custom mode must be an integer or name")
    main, sub = (value >> 16) & 0xFF, (value >> 24) & 0xFF
    for name, pair in _CUSTOM_MODES.items():
        if pair == (main, sub):
            return name
    return f"PX4({main},{sub})"


# Verb-first aliases make the pure API convenient to discover without tying
# callers to pymavlink's generated naming style.
encode_reposition = do_reposition
encode_orbit = do_orbit
encode_change_speed = do_change_speed
encode_loiter_unlimited = nav_loiter_unlimited
encode_loiter_time = nav_loiter_time
encode_loiter_to_alt = nav_loiter_to_alt
encode_vtol_transition = do_vtol_transition
encode_takeoff = nav_takeoff
encode_vtol_takeoff = nav_vtol_takeoff
encode_vtol_land = nav_vtol_land
encode_land_start = do_land_start
encode_custom_mode = custom_mode


__all__ = [name for name in globals() if name.startswith(("MAV_", "PX4_"))] + [
    "CommandSpec", "do_reposition", "do_orbit", "do_change_speed", "nav_loiter_unlimited",
    "nav_loiter_time", "nav_loiter_to_alt", "do_vtol_transition", "nav_takeoff",
    "nav_vtol_takeoff", "nav_vtol_land", "nav_land", "do_land_start", "custom_mode",
    "parse_custom_mode",
    "encode_reposition", "encode_orbit", "encode_change_speed", "encode_loiter_unlimited",
    "encode_loiter_time", "encode_loiter_to_alt", "encode_vtol_transition", "encode_takeoff",
    "encode_vtol_takeoff", "encode_vtol_land", "encode_land_start", "encode_custom_mode",
]
