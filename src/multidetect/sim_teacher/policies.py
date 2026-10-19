"""Deterministic teacher policies for synthetic SIM episodes."""

from __future__ import annotations

import math
import random
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .primitives import ActionPrimitive


@dataclass(frozen=True, slots=True)
class SDFObstacle:
    """Horizontal obstacle footprint and top elevation in world ENU/NED coordinates."""

    north_m: float
    east_m: float
    height_m: float
    size_north_m: float
    size_east_m: float
    yaw_rad: float = 0.0

    @property
    def top_m(self) -> float:
        return self.height_m


def segment_clear_of_obstacles(
    start_ned: tuple[float, float, float],
    end_ned: tuple[float, float, float],
    altitude_m: float,
    obstacles: tuple[SDFObstacle, ...] | list[SDFObstacle],
    margin_m: float = 3.0,
) -> bool:
    """Return whether a horizontal segment clears tall obstacle footprints.

    A segment that starts inside an obstacle's clearance ring is allowed when no
    point of it is closer to that obstacle than the start (climbing in place or
    moving away): otherwise a vehicle that ended up inside the ring, e.g. after
    descending toward a candidate next to a house, could never leave it (a SITL
    count run hovered 120 steps 10.8 m from a 7.2 m house at 8 m altitude).
    """
    ax, ay = start_ned[:2]
    bx, by = end_ned[:2]
    vx, vy = bx - ax, by - ay
    denom = vx * vx + vy * vy
    # Descending inside a ring is never an escape: the start may be above a roof.
    not_descending = len(start_ned) < 3 or len(end_ned) < 3 or end_ned[2] <= start_ned[2] + 1e-6
    for obstacle in obstacles:
        if obstacle.top_m <= float(altitude_m) - 2.0:
            continue
        t = (
            0.0
            if denom <= 1e-12
            else max(
                0.0,
                min(
                    1.0,
                    ((obstacle.north_m - ax) * vx + (obstacle.east_m - ay) * vy) / denom,
                ),
            )
        )
        distance = math.hypot(obstacle.north_m - (ax + t * vx), obstacle.east_m - (ay + t * vy))
        footprint_radius = math.hypot(obstacle.size_north_m, obstacle.size_east_m) / 2.0
        if distance < footprint_radius + float(margin_m):
            start_distance = math.hypot(obstacle.north_m - ax, obstacle.east_m - ay)
            if not_descending and distance >= start_distance - 1e-6:
                continue  # leaving (or rising inside) the clearance ring
            return False
    return True


def load_sdf_obstacles(path: str | Path) -> tuple[SDFObstacle, ...]:
    """Load static ``obstacle_*`` model footprints from an SDF world.

    SDF poses are ENU (x=east, y=north, z=up).  The returned coordinates are
    the NED horizontal convention used by the teacher.  Unknown geometry is
    skipped so ordinary, non-generated worlds remain usable.
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ()
    world = root.find("world")
    if world is None:
        return ()
    result: list[SDFObstacle] = []
    for model in world.findall("model"):
        name = model.get("name", "")
        if not name.startswith("obstacle_"):
            continue
        pose = model.findtext("pose", "0 0 0 0 0 0").split()
        try:
            x, y, z = (float(pose[i]) for i in range(3))
            yaw = float(pose[5]) if len(pose) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        geometry = model.find(".//collision/geometry")
        if geometry is None:
            geometry = model.find(".//visual/geometry")
        if geometry is None:
            continue
        box = geometry.find("box/size")
        cylinder = geometry.find("cylinder")
        try:
            if box is not None and box.text:
                sx, sy, sz = (float(v) for v in box.text.split()[:3])
                sn, se = sy, sx
            elif cylinder is not None:
                radius = float(cylinder.findtext("radius", "0"))
                sz = float(cylinder.findtext("length", "0"))
                sn = se = 2.0 * radius
            else:
                continue
            if min(sn, se, sz) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        result.append(SDFObstacle(y, x, z + sz / 2.0, sn, se, yaw))
    return tuple(result)


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


class HoverPolicy:
    """Move to a trajectory standoff, turn toward its centroid, then hold."""

    def __init__(
        self,
        target_ned: tuple[float, float, float],
        hold_s: float = 2.0,
        *,
        trajectory: Mapping[str, Any] | None = None,
        standoff_distance_m: float | None = None,
    ):
        self.target_ned = target_ned
        self.hold_s = hold_s
        self._phase = 0
        self.centroid_ned = target_ned
        self.standoff_distance_m = standoff_distance_m
        if trajectory is not None:
            points = [tuple(float(v) for v in p[:2]) for p in trajectory.get("waypoints", ())]
            if len(points) >= 2:
                east = sum(point[0] for point in points) / len(points)
                north = sum(point[1] for point in points) / len(points)
                half_extent = (
                    max(
                        max(point[0] for point in points) - min(point[0] for point in points),
                        max(point[1] for point in points) - min(point[1] for point in points),
                    )
                    / 2.0
                )
                distance = max(15.0, 1.6 * half_extent + 6.0)
                self.centroid_ned = (north, east, target_ned[2])
                self.standoff_distance_m = distance

    @staticmethod
    def ned_to_body(delta_n: float, delta_e: float, yaw_deg: float) -> tuple[float, float]:
        """Convert a NED horizontal displacement to body forward/right."""
        yaw = math.radians(float(yaw_deg))
        return (
            delta_n * math.cos(yaw) + delta_e * math.sin(yaw),
            -delta_n * math.sin(yaw) + delta_e * math.cos(yaw),
        )

    def next_action(
        self,
        *,
        pose_ned: tuple[float, float, float] | None = None,
        yaw_deg: float = 90.0,
    ) -> ActionPrimitive:
        if self._phase == 0:
            pose = pose_ned or (0.0, 0.0, 0.0)
            distance = self.standoff_distance_m
            if distance is None:
                target = self.target_ned
                delta_n, delta_e = target[0] - pose[0], target[1] - pose[1]
            else:
                # South of the ENU trajectory centroid means lower northing.
                target_n = self.centroid_ned[0] - distance
                target_e = self.centroid_ned[1]
                delta_n, delta_e = target_n - pose[0], target_e - pose[1]
            forward, right = self.ned_to_body(delta_n, delta_e, yaw_deg)
            self._phase = 1
            return ActionPrimitive.translate(forward, right, 0.0)
        if self._phase == 1 and self.standoff_distance_m is not None:
            pose = pose_ned or (0.0, 0.0, 0.0)
            desired = math.degrees(
                math.atan2(self.centroid_ned[1] - pose[1], self.centroid_ned[0] - pose[0])
            )
            delta = (desired - float(yaw_deg) + 180.0) % 360.0 - 180.0
            self._phase = 2
            return ActionPrimitive.turn(delta)
        return ActionPrimitive.hold(self.hold_s)


def target_in_fov(
    target_ned: tuple[float, float, float],
    pose_ned: tuple[float, float, float],
    yaw_deg: float,
    intrinsics: Mapping[str, float],
    image_size: tuple[int, int],
    max_range_m: float | None = None,
    camera_pitch_rad: float = 0.0,
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
    # Camera +X is pitched down by ``camera_pitch_rad``; NED +Z points down.
    pitch = float(camera_pitch_rad)
    x, z = x * math.cos(pitch) + dd * math.sin(pitch), -x * math.sin(pitch) + dd * math.cos(pitch)
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
    altitude_m: float = 5.0
    obstacles: tuple[SDFObstacle, ...] = ()
    camera_pitch_rad: float = 0.18
    # Route used for target-blind bounded searches.  ``current`` preserves the
    # historical expanding-square behaviour; alternate routes are useful for
    # zone search/count experiments and remain target independent.
    search_pattern: str = "current"
    # ``two_altitude`` flies its configured route at ``altitude_m`` first, then
    # descends in place and repeats a close-in route.  Keeping the lower height
    # on the policy (rather than in the host) makes the transition one bounded
    # primitive and keeps the route target-blind.
    low_altitude_m: float | None = None
    max_altitude_step_m: float = 8.0
    max_blocked_attempts: int = 3
    max_blocked_consecutive: int = 10
    # Re-adopt the measured yaw when it differs from the last commanded heading by
    # more than this (small differences are tracking noise and are ignored).
    resync_yaw_deg: float = 5.0

    def __post_init__(self) -> None:
        if self.standoff_m <= 0 or self.standoff_m >= self.max_detection_range_m:
            raise ValueError("standoff_m must be positive and below detection range")
        if self.search_radius_m <= 0 or self.max_translate_m <= 0:
            raise ValueError("search radius and translation bound must be positive")
        if not 60.0 <= self.scan_increment_deg <= 90.0:
            raise ValueError("scan increment must be between 60 and 90 degrees")
        if self.search_pattern not in {"current", "lawnmower", "spiral", "two_altitude"}:
            raise ValueError(
                "search_pattern must be one of current, lawnmower, spiral, two_altitude"
            )
        if self.low_altitude_m is not None and (
            not math.isfinite(self.low_altitude_m)
            or self.low_altitude_m <= 0
            or self.low_altitude_m > self.altitude_m
        ):
            raise ValueError("low_altitude_m must be positive and no higher than altitude_m")
        if not math.isfinite(self.max_altitude_step_m) or self.max_altitude_step_m <= 0:
            raise ValueError("max_altitude_step_m must be finite and positive")
        self._commanded_yaw_deg: float | None = None
        self._initial_scan_remaining = int(round(360.0 / self.scan_increment_deg))
        self._scan_remaining = 0
        self._waypoint_index = 0
        self._waypoints = self._build_route()
        self._search_phase = "high"
        self._low_waypoints: list[tuple[float, float]] = []
        if self.search_pattern == "two_altitude":
            low = self.low_altitude_m
            if low is None:
                low = min(self.altitude_m, max(8.0, min(12.0, 0.5 * self.altitude_m)))
            self.low_altitude_m = low
            shared = self._build_two_altitude_route()
            self._waypoints = [(n, e) for n, e, z in shared if z == self.altitude_m]
            self._low_waypoints = [(n, e) for n, e, z in shared if z == self.low_altitude_m]
        self._last_inside = False
        self._last_approach_range: float | None = None
        self._approach_stall_steps = 0
        self._approach_replan_pending = False
        self._approach_replanned = False
        self._safety_replanned = False
        self._blocked_attempts = 0
        self._blocked_consecutive = 0
        self._blocked = False
        self._waypoint_attempts: dict[tuple[str, int], int] = {}
        self._waypoint_last_distance: dict[tuple[str, int], float] = {}
        self.skipped_waypoints: list[dict[str, Any]] = []
        self._search_outcome = "running"

    def _build_spiral(self) -> list[tuple[float, float]]:
        # Space legs from the pitched camera ground swath, with overlap.
        hfov = 2.0 * math.atan2(float(self.image_size[0]), 2.0 * float(self.intrinsics["fx"]))
        vfov = 2.0 * math.atan2(float(self.image_size[1]), 2.0 * float(self.intrinsics["fy"]))
        vertical_ratio = math.tan(vfov / 2.0)
        pitch = self.camera_pitch_rad
        altitude = max(0.0, self.altitude_m)
        onset = (
            altitude
            * (math.cos(pitch) - vertical_ratio * math.sin(pitch))
            / max(1e-6, math.sin(pitch) + vertical_ratio * math.cos(pitch))
        )
        camera_forward = onset * math.cos(pitch) + altitude * math.sin(pitch)
        half_swath = math.tan(hfov / 2.0) * max(0.0, camera_forward)
        spacing = min(70.0, self.search_radius_m / 2.0, max(8.0, 1.6 * half_swath))
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

    def _build_route(self) -> list[tuple[float, float]]:
        """Build the configured bounded route in absolute N/E coordinates.

        The coverage helpers intentionally share the same camera geometry as
        the offline evaluator.  ``two_altitude`` builds both high and low
        lawnmower legs; the state machine switches between them after the
        high pass has completed.
        """
        if self.search_pattern == "current":
            return self._build_spiral()
        return self._build_coverage_route(self.altitude_m)

    def _build_coverage_route(self, altitude_m: float) -> list[tuple[float, float]]:
        from multidetect.search_coverage import CoverageCamera, expanding_spiral, lawnmower

        camera = CoverageCamera(
            fx=float(self.intrinsics["fx"]),
            fy=float(self.intrinsics["fy"]),
            cx=float(self.intrinsics.get("cx", self.image_size[0] / 2)),
            cy=float(self.intrinsics.get("cy", self.image_size[1] / 2)),
            width_px=int(self.image_size[0]),
            height_px=int(self.image_size[1]),
            pitch_down_deg=math.degrees(self.camera_pitch_rad),
        )
        if self.search_pattern in {"lawnmower", "two_altitude"}:
            relative = lawnmower(self.search_radius_m, camera, max(altitude_m, 1e-3))
        else:
            relative = expanding_spiral(self.search_radius_m, camera, max(altitude_m, 1e-3))
        home_n, home_e = self.home_ned[:2]
        return [(home_n + n, home_e + e) for n, e in relative[1:]]

    def _build_two_altitude_route(self) -> list[tuple[float, float, float]]:
        from multidetect.search_coverage import CoverageCamera, two_altitude_route

        camera = CoverageCamera(
            fx=float(self.intrinsics["fx"]),
            fy=float(self.intrinsics["fy"]),
            cx=float(self.intrinsics.get("cx", self.image_size[0] / 2)),
            cy=float(self.intrinsics.get("cy", self.image_size[1] / 2)),
            width_px=int(self.image_size[0]),
            height_px=int(self.image_size[1]),
            pitch_down_deg=math.degrees(self.camera_pitch_rad),
        )
        route = two_altitude_route(
            self.search_radius_m,
            camera,
            self.altitude_m,
            float(self.low_altitude_m),
            max_altitude_step_m=self.max_altitude_step_m,
        )
        home_n, home_e = self.home_ned[:2]
        return [(home_n + n, home_e + e, z) for n, e, z in route]

    def reset(self, *, state: str = "search", yaw_deg: float | None = None) -> None:
        self.state = state
        self._commanded_yaw_deg = yaw_deg
        self._initial_scan_remaining = int(round(360.0 / self.scan_increment_deg))
        self._scan_remaining = 0
        self._waypoint_index = 0
        self._search_phase = "high"
        self._last_inside = False
        self._last_approach_range = None
        self._approach_stall_steps = 0
        self._approach_replan_pending = False
        self._approach_replanned = False
        self._safety_replanned = False
        self._blocked_attempts = 0
        self._blocked_consecutive = 0
        self._blocked = False

    def _search_leg_action(
        self, pose_ned, waypoint, yaw_deg: float | None = None
    ) -> ActionPrimitive:
        """Turn onto a spiral leg, then fly it body-forward."""
        dn = waypoint[0] - pose_ned[0]
        de = waypoint[1] - pose_ned[1]
        distance = math.hypot(dn, de)
        if distance <= 1e-6:
            return ActionPrimitive.hold(0.0)
        if (
            yaw_deg is not None
            and self._commanded_yaw_deg is not None
            and abs(self._wrap(yaw_deg - self._commanded_yaw_deg)) > self.resync_yaw_deg
        ):
            # Someone else turned the vehicle (e.g. a host approaching a candidate).
            # Legs are flown body-forward, so a stale heading flies every leg off
            # course and clips the boundary on the wrong line: a count run left
            # its 150 m zone and flew 395 m out.
            self._commanded_yaw_deg = yaw_deg
        desired = math.degrees(math.atan2(de, dn))
        delta = self._wrap(desired - float(self._commanded_yaw_deg or 0.0))
        if abs(delta) >= self.approach_turn_min_deg:
            self._commanded_yaw_deg = desired
            return ActionPrimitive.turn(delta)
        yaw = float(self._commanded_yaw_deg if self._commanded_yaw_deg is not None else desired)
        step = min(self.max_translate_m, distance)
        # Keep the commanded endpoint inside the circular search boundary.
        heading = math.radians(yaw)
        vn, ve = math.cos(heading), math.sin(heading)
        rel_n = pose_ned[0] - self.home_ned[0]
        rel_e = pose_ned[1] - self.home_ned[1]
        b = rel_n * vn + rel_e * ve
        c = rel_n * rel_n + rel_e * rel_e - self.search_radius_m**2
        disc = max(0.0, b * b - c)
        boundary_step = -b + math.sqrt(disc)
        step = min(step, max(0.0, boundary_step))
        if step <= 1e-6:
            return ActionPrimitive.hold(0.0)
        action = ActionPrimitive.translate(step, 0.0, 0.0)
        return self._safe_translation(pose_ned, action, yaw)

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
        action = ActionPrimitive.translate(
            dn * math.cos(yaw) + de * math.sin(yaw), -dn * math.sin(yaw) + de * math.cos(yaw), 0.0
        )
        return self._safe_translation(pose_ned, action, yaw_deg)

    @staticmethod
    def _segment_distance(
        a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]
    ) -> float:
        vx, vy = b[0] - a[0], b[1] - a[1]
        denom = vx * vx + vy * vy
        t = (
            0.0
            if denom <= 1e-12
            else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / denom))
        )
        qx, qy = a[0] + t * vx, a[1] + t * vy
        return math.hypot(p[0] - qx, p[1] - qy)

    def _segment_safe(self, pose_ned, end_n: float, end_e: float) -> bool:
        return segment_clear_of_obstacles(
            pose_ned,
            (end_n, end_e, pose_ned[2]),
            max(0.0, -float(pose_ned[2])),
            self.obstacles,
        )

    def _safe_translation(
        self, pose_ned, action: ActionPrimitive, yaw_deg: float
    ) -> ActionPrimitive:
        yaw = math.radians(yaw_deg)
        end_n = pose_ned[0] + action.forward_m * math.cos(yaw) - action.right_m * math.sin(yaw)
        end_e = pose_ned[1] + action.forward_m * math.sin(yaw) + action.right_m * math.cos(yaw)
        if self._segment_safe(pose_ned, end_n, end_e):
            return action
        self._safety_replanned = True
        phase = self._search_phase
        waypoints = self._low_waypoints if phase == "low" else self._waypoints
        waypoint_key = (phase, self._waypoint_index)
        waypoint = waypoints[self._waypoint_index] if self.state == "search" and waypoints else None
        distance = (
            math.hypot(waypoint[0] - pose_ned[0], waypoint[1] - pose_ned[1])
            if waypoint is not None
            else None
        )
        # A short perpendicular sidestep clears the blocking footprint when possible.
        # If the first pair is clipped/blocked, escalate to a larger lateral
        # offset and then a short leg in the opposite heading.  The original
        # pair remains first, preserving the historical clear-path behaviour.
        length = max(3.5, min(self.max_translate_m, math.hypot(action.forward_m, action.right_m)))
        lateral = min(self.max_translate_m, 2.0 * length)
        candidates = [(yaw, right) for right in (length, -length)]
        candidates.extend((yaw, right) for right in (lateral, -lateral) if lateral > length + 1e-6)
        opposite = yaw + math.pi
        candidates.extend((opposite, right) for right in (length, -length))
        for candidate_no, (candidate_yaw, right) in enumerate(candidates):
            cn = pose_ned[0] - right * math.sin(candidate_yaw)
            ce = pose_ned[1] + right * math.cos(candidate_yaw)
            # Obstacle recovery must retain the same zone bound as an ordinary
            # route leg; a perpendicular escape near the rim is otherwise able
            # to leave the circular search area.
            rel_n, rel_e = cn - self.home_ned[0], ce - self.home_ned[1]
            radius = math.hypot(rel_n, rel_e)
            clipped = radius > self.search_radius_m
            if clipped:
                scale = self.search_radius_m / radius
                cn = self.home_ned[0] + rel_n * scale
                ce = self.home_ned[1] + rel_e * scale
            if self._segment_safe(pose_ned, cn, ce):
                if waypoint is not None and distance is not None:
                    new_distance = math.hypot(waypoint[0] - cn, waypoint[1] - ce)
                    previous = self._waypoint_last_distance.get(waypoint_key, distance)
                    if new_distance < previous - 1e-6:
                        self._waypoint_attempts[waypoint_key] = 0
                    else:
                        self._waypoint_attempts[waypoint_key] = (
                            self._waypoint_attempts.get(waypoint_key, 0) + 1
                        )
                    self._waypoint_last_distance[waypoint_key] = new_distance
                    if self._waypoint_attempts[waypoint_key] >= self.max_blocked_attempts:
                        self.skipped_waypoints.append(
                            {
                                "waypoint_index": self._waypoint_index,
                                "ned": tuple(waypoint),
                                "phase": phase,
                                "attempts": self._waypoint_attempts[waypoint_key],
                            }
                        )
                        self._waypoint_index += 1
                        self._waypoint_attempts.pop(waypoint_key, None)
                        self._waypoint_last_distance.pop(waypoint_key, None)
                self._blocked_consecutive = 0
                if candidate_no < 2 and not clipped:
                    # Preserve the historical current-pattern action exactly.
                    return ActionPrimitive.translate(0.0, right, 0.0)
                dn, de = cn - pose_ned[0], ce - pose_ned[1]
                return ActionPrimitive.translate(
                    dn * math.cos(yaw) + de * math.sin(yaw),
                    -dn * math.sin(yaw) + de * math.cos(yaw),
                    0.0,
                )
        self._blocked_attempts += 1
        self._blocked_consecutive += 1
        attempts = (
            self._waypoint_attempts.get(waypoint_key, 0) + 1
            if waypoint is not None
            else self._blocked_attempts
        )
        self._waypoint_attempts[waypoint_key] = attempts
        if attempts >= self.max_blocked_attempts:
            if waypoint is not None:
                self.skipped_waypoints.append(
                    {
                        "waypoint_index": self._waypoint_index,
                        "ned": tuple(waypoint),
                        "phase": phase,
                        "attempts": attempts,
                    }
                )
            self._waypoint_index += 1
            self._blocked_attempts = 0
            self._waypoint_attempts.pop(waypoint_key, None)
            self._waypoint_last_distance.pop(waypoint_key, None)
        if self._blocked_consecutive >= self.max_blocked_consecutive:
            self._blocked = True
            self._search_outcome = "blocked"
            self.state = "done"
            return ActionPrimitive.stop()
        return ActionPrimitive.hold(0.0)

    @property
    def effective_standoff_m(self) -> float:
        vfov = 2.0 * math.atan2(float(self.image_size[1]), 2.0 * float(self.intrinsics["fy"]))
        geometry = max(0.0, float(self.altitude_m)) / max(
            1e-6, math.tan(self.camera_pitch_rad + vfov / 2.0)
        )
        return max(self.standoff_m, geometry)

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
            camera_pitch_rad=self.camera_pitch_rad,
        )
        self._last_inside = inside
        if self.state == "done":
            return ActionPrimitive.stop()
        if self.state == "hold":
            self.state = "done"
            if self._search_outcome != "blocked":
                self._search_outcome = "done"
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
            standoff = self.effective_standoff_m
            if horizontal_range <= standoff + 1e-6:
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
            action = ActionPrimitive.translate(
                min(self.max_translate_m, max(0.0, horizontal_range - standoff)), 0.0, 0.0
            )
            return self._safe_translation(pose_ned, action, yaw_deg)
        if self._initial_scan_remaining > 0:
            self.state = "scan"
            self._initial_scan_remaining -= 1
            return self._scan_action(yaw_deg)
        self.state = "search"
        if self.search_pattern == "two_altitude" and self._search_phase == "high":
            # Finish the high, fast sweep before spending time on a low pass.
            # The host applies the returned vertical primitive; until the next
            # observation reports the lower altitude we must not advance the
            # route or emit another descent command.
            if self._waypoint_index >= len(self._waypoints):
                low = float(self.low_altitude_m)
                altitude = max(0.0, -float(pose_ned[2]))
                if altitude > low + 0.5:
                    drop = min(altitude - low, self.max_altitude_step_m)
                    return ActionPrimitive.translate(0.0, 0.0, -drop)
                self._search_phase = "low"
                self._waypoint_index = 0
        waypoints = self._low_waypoints if self._search_phase == "low" else self._waypoints
        if self._waypoint_index >= len(waypoints):
            # A completed low pass is restarted for counting, just like the
            # historical route restarts after its final waypoint.
            self._waypoint_index = 0
        waypoint = waypoints[self._waypoint_index]
        if math.hypot(waypoint[0] - pose_ned[0], waypoint[1] - pose_ned[1]) <= 0.5:
            self._waypoint_index += 1
            if (
                self.search_pattern == "two_altitude"
                and self._search_phase == "high"
                and self._waypoint_index >= len(waypoints)
            ):
                return ActionPrimitive.hold(0.0)
            if self._waypoint_index >= len(waypoints):
                self._waypoint_index = 0
            waypoint = waypoints[self._waypoint_index]
        return self._search_leg_action(pose_ned, waypoint, yaw_deg)

    def labels(self, *, pose_ned, yaw_deg: float = 0.0) -> dict[str, Any]:
        inside, bearing, rng = target_in_fov(
            self.target_ned,
            pose_ned,
            yaw_deg,
            self.intrinsics,
            self.image_size,
            max_range_m=self.max_detection_range_m,
            camera_pitch_rad=self.camera_pitch_rad,
        )
        return {
            "phase": self.state,
            "target_in_fov": inside,
            "target_bearing_deg": bearing,
            "target_range_m": rng,
            "approach_replanned": self._approach_replanned,
            "safety_replanned": self._safety_replanned,
            "standoff_m": self.effective_standoff_m,
            "skipped_waypoints": [dict(item) for item in self.skipped_waypoints],
            "search_pass_outcome": self._search_outcome,
        }


__all__ = [
    "ExploreBounds",
    "ExplorePolicy",
    "HoverPolicy",
    "ExpertSearchPolicy",
    "SDFObstacle",
    "load_sdf_obstacles",
    "segment_clear_of_obstacles",
    "target_in_fov",
]
