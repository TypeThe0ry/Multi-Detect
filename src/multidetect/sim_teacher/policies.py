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
    """Target-blind bounded search followed by geometric approach.

    The target is consulted only for the geometric FOV label and after a
    detection. Search waypoints are a fixed expanding-square prior. Occlusion
    and line-of-sight are intentionally not modelled yet.
    """

    target_ned: tuple[float, float, float]
    intrinsics: Mapping[str, float]
    image_size: tuple[int, int] = (1280, 960)
    standoff_m: float = 15.0
    hold_seconds: float = 2.0
    state: str = "search"
    search_radius_m: float = 150.0
    max_detection_range_m: float = 80.0
    max_translate_m: float = 15.0
    scan_increment_deg: float = 90.0
    home_ned: tuple[float, float, float] = (0.0, 0.0, 0.0)
    approach_bearing_threshold_deg: float = 10.0
    approach_turn_min_deg: float = 2.0
    approach_progress_m: float = 0.5

    def __post_init__(self) -> None:
        if self.standoff_m <= 0 or self.standoff_m >= self.max_detection_range_m:
            raise ValueError("standoff_m must be positive and below detection range")
        if self.search_radius_m <= 0 or self.max_translate_m <= 0:
            raise ValueError("search radius and translation bound must be positive")
        if not 60.0 <= self.scan_increment_deg <= 90.0:
            raise ValueError("scan increment must be between 60 and 90 degrees")
        self._commanded_yaw_deg: float | None = None
        self._initial_scan_remaining = int(round(360.0 / self.scan_increment_deg))
        self._scan_remaining = 0
        self._waypoint_index = 0
        self._waypoints = self._build_spiral()
        self._last_inside = False
        self._last_approach_range: float | None = None
        self._approach_stall_steps = 0
        self._approach_replan_pending = False
        self._approach_replanned = False

    def _build_spiral(self) -> list[tuple[float, float]]:
        spacing = min(70.0, self.search_radius_m / 2.0)
        home_n, home_e = self.home_ned[:2]
        points: list[tuple[float, float]] = []
        north, east = home_n, home_e
        length, direction_index = spacing, 0
        directions = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))
        while math.hypot(north - home_n, east - home_e) < self.search_radius_m:
            for _ in range(2):
                dn, de = directions[direction_index % 4]
                north += dn * length
                east += de * length
                rel_n, rel_e = north - home_n, east - home_e
                radius = math.hypot(rel_n, rel_e)
                if radius > self.search_radius_m:
                    scale = self.search_radius_m / radius
                    points.append((home_n + rel_n * scale, home_e + rel_e * scale))
                    return points
                points.append((north, east))
                direction_index += 1
            length += spacing
        return points

    def reset(self, *, state: str = "search", yaw_deg: float | None = None) -> None:
        self.state = state
        self._commanded_yaw_deg = yaw_deg
        self._initial_scan_remaining = int(round(360.0 / self.scan_increment_deg))
        self._scan_remaining = 0
        self._waypoint_index = 0
        self._last_inside = False
        self._last_approach_range = None
        self._approach_stall_steps = 0
        self._approach_replan_pending = False
        self._approach_replanned = False

    @property
    def target_in_fov(self) -> bool:
        return self.state in {"approach", "hold", "done"} or self._last_inside

    @staticmethod
    def _wrap(degrees: float) -> float:
        return (degrees + 180.0) % 360.0 - 180.0

    def _scan_action(self, yaw_deg: float) -> ActionPrimitive:
        if self._commanded_yaw_deg is None:
            self._commanded_yaw_deg = yaw_deg
        self._commanded_yaw_deg += self.scan_increment_deg
        self._scan_remaining -= 1
        return ActionPrimitive.turn(self.scan_increment_deg)

    def _bounded_translation(self, pose_ned, waypoint, yaw_deg) -> ActionPrimitive:
        dn, de = waypoint[0] - pose_ned[0], waypoint[1] - pose_ned[1]
        distance = math.hypot(dn, de)
        if distance <= 1e-6:
            return ActionPrimitive.hold(0.0)
        step = min(self.max_translate_m, distance)
        end_n, end_e = pose_ned[0] + dn * step / distance, pose_ned[1] + de * step / distance
        home_n, home_e = self.home_ned[:2]
        rel_n, rel_e = end_n - home_n, end_e - home_e
        radius = math.hypot(rel_n, rel_e)
        if radius > self.search_radius_m:
            scale = self.search_radius_m / radius
            end_n, end_e = home_n + rel_n * scale, home_e + rel_e * scale
        dn, de = end_n - pose_ned[0], end_e - pose_ned[1]
        yaw = math.radians(yaw_deg)
        return ActionPrimitive.translate(
            dn * math.cos(yaw) + de * math.sin(yaw), -dn * math.sin(yaw) + de * math.cos(yaw), 0.0
        )

    def next_action(self, *, pose_ned, yaw_deg: float = 0.0) -> ActionPrimitive:
        if self._commanded_yaw_deg is None:
            self._commanded_yaw_deg = yaw_deg
        inside, bearing, rng = target_in_fov(
            self.target_ned,
            pose_ned,
            yaw_deg,
            self.intrinsics,
            self.image_size,
            max_range_m=self.max_detection_range_m,
        )
        self._last_inside = inside
        if self.state == "done":
            return ActionPrimitive.stop()
        if self.state == "hold":
            self.state = "done"
            return ActionPrimitive.stop()
        if self.state not in {"approach", "hold", "done"} and inside:
            self.state = "approach"
        if self.state == "approach":
            dn = self.target_ned[0] - pose_ned[0]
            de = self.target_ned[1] - pose_ned[1]
            horizontal_range = math.hypot(dn, de)
            if self._last_approach_range is not None:
                if self._last_approach_range - horizontal_range > self.approach_progress_m:
                    self._approach_stall_steps = 0
                else:
                    self._approach_stall_steps += 1
            self._last_approach_range = horizontal_range
            if self._approach_stall_steps >= 3:
                self._approach_replan_pending = True
                self._approach_stall_steps = 0
            if horizontal_range <= self.standoff_m + 1e-6:
                self.state = "hold"
                return ActionPrimitive.hold(self.hold_seconds)
            desired = self._wrap(yaw_deg + bearing)
            delta = self._wrap(desired - self._commanded_yaw_deg)
            if self._approach_replan_pending:
                self._approach_replan_pending = False
                self._approach_replanned = True
                if (
                    abs(bearing) >= self.approach_turn_min_deg
                    and abs(delta) >= self.approach_turn_min_deg
                ):
                    self._commanded_yaw_deg = desired
                    return ActionPrimitive.turn(bearing)
            if abs(bearing) > self.approach_bearing_threshold_deg:
                self._commanded_yaw_deg = desired
                if abs(delta) >= self.approach_turn_min_deg:
                    return ActionPrimitive.turn(delta)
            return ActionPrimitive.translate(
                min(self.max_translate_m, max(0.0, horizontal_range - self.standoff_m)), 0.0, 0.0
            )
        if self._initial_scan_remaining > 0:
            self.state = "scan"
            self._initial_scan_remaining -= 1
            return self._scan_action(yaw_deg)
        if self._scan_remaining > 0:
            self.state = "scan"
            return self._scan_action(yaw_deg)
        self.state = "search"
        if self._waypoint_index >= len(self._waypoints):
            self._waypoint_index = 0
        waypoint = self._waypoints[self._waypoint_index]
        if math.hypot(waypoint[0] - pose_ned[0], waypoint[1] - pose_ned[1]) <= 0.5:
            self._waypoint_index += 1
            self._scan_remaining = int(round(360.0 / self.scan_increment_deg))
            self.state = "scan"
            return self._scan_action(yaw_deg)
        return self._bounded_translation(pose_ned, waypoint, yaw_deg)

    def labels(self, *, pose_ned, yaw_deg: float = 0.0) -> dict[str, Any]:
        inside, bearing, rng = target_in_fov(
            self.target_ned,
            pose_ned,
            yaw_deg,
            self.intrinsics,
            self.image_size,
            max_range_m=self.max_detection_range_m,
        )
        return {
            "phase": self.state,
            "target_in_fov": inside,
            "target_bearing_deg": bearing,
            "target_range_m": rng,
            "approach_replanned": self._approach_replanned,
        }


__all__ = ["ExploreBounds", "ExplorePolicy", "ExpertSearchPolicy", "target_in_fov"]
