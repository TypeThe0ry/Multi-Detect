"""Deterministic teacher policies for synthetic SIM episodes."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .primitives import ActionPrimitive


@dataclass(frozen=True, slots=True)
class ExploreBounds:
    min_altitude_m: float = 2.0
    max_altitude_m: float = 30.0
    box_min_north_m: float = -100.0
    box_max_north_m: float = 100.0
    box_min_east_m: float = -100.0
    box_max_east_m: float = 100.0


class ExplorePolicy:
    """Seeded random primitive generator that respects conservative bounds."""

    def __init__(self, seed: int = 0, bounds: ExploreBounds | None = None):
        self.rng = random.Random(seed)
        self.bounds = bounds or ExploreBounds()
        self.north = 0.0
        self.east = 0.0
        self.altitude = max(self.bounds.min_altitude_m, 5.0)
        self.yaw = 0.0

    def reset(
        self,
        *,
        north_m: float = 0.0,
        east_m: float = 0.0,
        altitude_m: float | None = None,
        yaw_deg: float = 0.0,
    ) -> None:
        self.north, self.east = north_m, east_m
        self.altitude = self.bounds.min_altitude_m if altitude_m is None else altitude_m
        self.yaw = yaw_deg

    def next_action(self) -> ActionPrimitive:
        options = ["translate", "turn", "hold"]
        kind = self.rng.choice(options)
        if kind == "turn":
            delta = self.rng.choice((-1, 1)) * self.rng.uniform(15.0, 120.0)
            self.yaw = (self.yaw + delta + 180) % 360 - 180
            return ActionPrimitive.turn(delta)
        if kind == "hold":
            return ActionPrimitive.hold(self.rng.uniform(0.5, 3.0))
        forward = self.rng.uniform(1.0, 15.0)
        right = self.rng.uniform(-15.0, 15.0)
        up = self.rng.uniform(-3.0, 3.0)
        # Clip vertical command and horizontal endpoint to the scenario box.
        n, e = (
            forward * math.cos(math.radians(self.yaw)) - right * math.sin(math.radians(self.yaw)),
            forward * math.sin(math.radians(self.yaw)) + right * math.cos(math.radians(self.yaw)),
        )
        if not (self.bounds.box_min_north_m <= self.north + n <= self.bounds.box_max_north_m):
            n = 0.0
        if not (self.bounds.box_min_east_m <= self.east + e <= self.bounds.box_max_east_m):
            e = 0.0
        target_alt = self.altitude + up
        if not self.bounds.min_altitude_m <= target_alt <= self.bounds.max_altitude_m:
            up = 0.0
        self.north += n
        self.east += e
        self.altitude += up
        yaw = math.radians(self.yaw)
        clipped_forward = n * math.cos(yaw) + e * math.sin(yaw)
        clipped_right = -n * math.sin(yaw) + e * math.cos(yaw)
        return ActionPrimitive.translate(clipped_forward, clipped_right, up)

    def action(self) -> ActionPrimitive:
        return self.next_action()


def target_in_fov(
    target_ned: tuple[float, float, float],
    pose_ned: tuple[float, float, float],
    yaw_deg: float,
    intrinsics: Mapping[str, float],
    image_size: tuple[int, int],
    max_range_m: float | None = None,
) -> tuple[bool, float, float]:
    """Return (in_fov, bearing_deg, range_m) for a nadir-ish forward camera."""
    dn, de, dd = (target_ned[i] - pose_ned[i] for i in range(3))
    rng = math.sqrt(dn * dn + de * de + dd * dd)
    bearing = math.degrees(math.atan2(de, dn) - math.radians(yaw_deg))
    bearing = (bearing + 180) % 360 - 180
    # Camera horizontal/vertical FOV from intrinsics.  Use optical axis +X,
    # with depth axis approximated as down for simulator geometry tests.
    fx, fy = float(intrinsics["fx"]), float(intrinsics["fy"])
    cx, cy = (
        float(intrinsics.get("cx", image_size[0] / 2)),
        float(intrinsics.get("cy", image_size[1] / 2)),
    )
    # rotate N/E into body frame and project as x forward, z down.
    yaw = math.radians(yaw_deg)
    x = dn * math.cos(yaw) + de * math.sin(yaw)
    y = -dn * math.sin(yaw) + de * math.cos(yaw)
    z = dd
    if x <= 0:
        return False, bearing, rng
    px, py = cx + fx * y / x, cy + fy * z / x
    inside = (
        0 <= px < image_size[0]
        and 0 <= py < image_size[1]
        and (max_range_m is None or rng <= max_range_m)
    )
    return inside, bearing, rng


@dataclass
class ExpertSearchPolicy:
    target_ned: tuple[float, float, float]
    intrinsics: Mapping[str, float]
    image_size: tuple[int, int] = (640, 480)
    standoff_m: float = 8.0
    hold_seconds: float = 2.0
    state: str = "search"

    @property
    def target_in_fov(self) -> bool:
        return self.state in {"approach", "hold", "done"}

    def next_action(
        self, *, pose_ned: tuple[float, float, float], yaw_deg: float = 0.0
    ) -> ActionPrimitive:
        inside, bearing, rng = target_in_fov(
            self.target_ned, pose_ned, yaw_deg, self.intrinsics, self.image_size
        )
        if self.state == "search":
            if inside:
                self.state = "approach"
            else:
                return ActionPrimitive.translate(8.0, 0.0, 0.0)
        if self.state == "approach":
            if rng > self.standoff_m:
                return ActionPrimitive.translate(min(8.0, rng - self.standoff_m), 0.0, 0.0)
            self.state = "hold"
            return ActionPrimitive.hold(self.hold_seconds)
        if self.state == "hold":
            self.state = "done"
            return ActionPrimitive.stop()
        return ActionPrimitive.stop()

    def labels(
        self, *, pose_ned: tuple[float, float, float], yaw_deg: float = 0.0
    ) -> dict[str, Any]:
        inside, bearing, rng = target_in_fov(
            self.target_ned, pose_ned, yaw_deg, self.intrinsics, self.image_size
        )
        return {"target_in_fov": inside, "target_bearing": bearing, "target_range": rng}


__all__ = ["ExploreBounds", "ExplorePolicy", "ExpertSearchPolicy", "target_in_fov"]
