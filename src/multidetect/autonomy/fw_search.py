"""Target-blind area search for aircraft that cannot hover.

The multirotor search scans by turning in place and stepping ~15 m; a fixed-wing
(or VTOL in FW mode) turned into "reposition here" just circles where it is.  This
search flies lawnmower lanes with Dubins turns (``fw_geometry.lawnmower_search``)
and hands the loop one leg at a time, each at most ``max_leg_m`` ahead, so
consecutive repositions chain into continuous flight along the lanes.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from multidetect.sim_teacher.primitives import ActionPrimitive

from .fw_geometry import lawnmower_search


def camera_footprint_width_m(
    intrinsics: Mapping[str, float], altitude_m: float, pitch_down_rad: float
) -> float:
    """Approximate ground swath of a forward camera pitched down by ``pitch_down_rad``."""
    half_fov = math.atan2(float(intrinsics["cx"]), float(intrinsics["fx"]))
    look_down = max(float(pitch_down_rad), math.radians(10.0))
    slant = float(altitude_m) / math.sin(look_down)
    return 2.0 * slant * math.tan(half_fov)


class FixedWingSearch:
    """Lawnmower search over a square region around ``home_ned``; repeats when done."""

    def __init__(
        self,
        *,
        home_ned: Sequence[float],
        radius_m: float,
        footprint_width_m: float,
        turn_radius_m: float,
        max_leg_m: float,
        overlap: float = 0.2,
        accept_m: float = 10.0,
    ) -> None:
        if max_leg_m <= 0 or radius_m <= 0:
            raise ValueError("search radius and leg length must be positive")
        north, east = float(home_ned[0]), float(home_ned[1])
        width = min(max(float(footprint_width_m), 30.0), 2.0 * float(radius_m))
        path = lawnmower_search(
            (north - radius_m, north + radius_m, east - radius_m, east + radius_m),
            width, overlap, max(float(turn_radius_m), 1.0),
        )
        # One waypoint every ~0.9 leg along the sampled lanes and turns.
        spacing = 0.9 * float(max_leg_m)
        points = [p.position_ne for p in path.points]
        waypoints = [points[0]]
        travelled = 0.0
        for a, b in zip(points, points[1:]):
            travelled += math.hypot(b[0] - a[0], b[1] - a[1])
            if travelled >= spacing:
                waypoints.append(b)
                travelled = 0.0
        if waypoints[-1] != points[-1]:
            waypoints.append(points[-1])
        self.waypoints: tuple[tuple[float, float], ...] = tuple(waypoints)
        self.max_leg_m = float(max_leg_m)
        self.accept_m = float(accept_m)
        self.footprint_width_m = width
        self.passes = 0
        self._index = 0

    def next_action(self, *, pose_ned: Sequence[float], yaw_deg: float = 0.0) -> ActionPrimitive:
        north, east = float(pose_ned[0]), float(pose_ned[1])
        for _ in range(len(self.waypoints)):
            wn, we = self.waypoints[self._index]
            if math.hypot(wn - north, we - east) > self.accept_m:
                break
            self._index += 1
            if self._index >= len(self.waypoints):
                self._index, self.passes = 0, self.passes + 1
        wn, we = self.waypoints[self._index]
        dn, de = wn - north, we - east
        distance = math.hypot(dn, de)
        if distance > self.max_leg_m:
            scale = 0.95 * self.max_leg_m / distance
            dn, de = dn * scale, de * scale
        yaw = math.radians(float(yaw_deg))
        forward = dn * math.cos(yaw) + de * math.sin(yaw)
        right = -dn * math.sin(yaw) + de * math.cos(yaw)
        return ActionPrimitive.translate(forward, right, 0.0)
