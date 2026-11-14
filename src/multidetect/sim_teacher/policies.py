"""Deterministic teacher policies for synthetic SIM episodes."""

from __future__ import annotations

import math
import random
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from multidetect.camera_mount import CameraMount, compose_camera_pose

from .primitives import ActionPrimitive, body_to_ned


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
    # ``altitude_m`` is the start altitude; a descending segment ends lower, and an
    # obstacle top between the two is in its way (a roof 2.4 m under a 17.2 m start
    # was skipped and a 4 m approach descent flew into it in SITL gen_cnt2_w0001).
    lowest_m = float(altitude_m)
    if not not_descending:
        lowest_m -= float(end_ned[2]) - float(start_ned[2])
    for obstacle in obstacles:
        if obstacle.top_m <= lowest_m - 2.0:
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
    camera_mount: CameraMount | None = None,
    camera_pose: Mapping[str, Any] | None = None,
    require_camera_feedback: bool = False,
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
    if require_camera_feedback and camera_mount is not None and camera_mount.kind == "gimbal" and camera_pose is None:
        return False, bearing, rng
    if camera_pose is not None:
        try:
            center = tuple(float(value) for value in camera_pose["center_ned"])
            rotation = tuple(float(value) for value in camera_pose["optical_to_world"])
            if len(center) != 3 or len(rotation) != 9:
                raise ValueError
            vector = tuple(target_ned[i] - center[i] for i in range(3))
            x, y, z = tuple(
                sum(rotation[row * 3 + col] * vector[row] for row in range(3))
                for col in range(3)
            )
            if x <= 0:
                return False, bearing, rng
            px, py = cx + fx * y / x, cy + fy * z / x
            inside = 0 <= px < image_size[0] and 0 <= py < image_size[1]
            return inside and (max_range_m is None or rng <= max_range_m), bearing, rng
        except (KeyError, TypeError, ValueError):
            return False, bearing, rng
    if camera_mount is not None and camera_mount.kind == "gimbal":
        composed = compose_camera_pose(
            pose_ned,
            vehicle_roll_rad=0.0,
            vehicle_pitch_rad=0.0,
            vehicle_yaw_rad=math.radians(yaw_deg),
            mount=camera_mount,
        )
        vector = tuple(target_ned[i] - composed.center_ned[i] for i in range(3))
        rotation = composed.optical_to_world
        x, y, z = (
            sum(rotation[row * 3 + col] * vector[row] for row in range(3))
            for col in range(3)
        )
        if x <= 0:
            return False, bearing, rng
        px, py = cx + fx * y / x, cy + fy * z / x
        inside = 0 <= px < image_size[0] and 0 <= py < image_size[1]
        return inside and (max_range_m is None or rng <= max_range_m), bearing, rng
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
    camera_mount: CameraMount | None = None
    # Route used for target-blind bounded searches.  ``current`` preserves the
    # historical expanding-square behaviour; alternate routes are useful for
    # zone search/count experiments and remain target independent.
    search_pattern: str = "current"
    # Historical callers keep looping; onboard coverage/count runs can opt into
    # a single pass and receive a terminal route-finished outcome.
    restart_route: bool = True
    # ``two_altitude`` flies its configured route at ``altitude_m`` first, then
    # descends in place and repeats a close-in route.  Keeping the lower height
    # on the policy (rather than in the host) makes the transition one bounded
    # primitive and keeps the route target-blind.
    low_altitude_m: float | None = None
    max_altitude_step_m: float = 8.0
    max_blocked_attempts: int = 3
    max_blocked_consecutive: int = 10
    # A leg blocked only because the vehicle sits below the planned search
    # altitude (which the host sets to clear the mapped obstacles) is flown after
    # climbing to that altitude plus this margin, instead of being sidestepped and
    # its waypoints skipped.  The host tolerates 1 m under its search altitude, and
    # an obstacle 2 m under the planned altitude is cleared only at it (SITL
    # gen_cnt2_w0001: 16.4 m under a 16.8 m plan, the lane over the 14.8 m block
    # was skipped and a hydrant next to it never seen).
    blocked_climb_margin_m: float = 0.5
    # Climbs in a row without a clear leg before falling back to sidesteps.
    max_blocked_climbs: int = 2
    # Re-adopt the measured yaw when it differs from the last commanded heading by
    # more than this (small differences are tracking noise and are ignored).
    resync_yaw_deg: float = 5.0
    observation_frames: int = 0
    observation_waypoints: tuple[tuple[float, float, float], ...] = ()
    observation_view_names: tuple[str, ...] = ()
    fixed_wing_orbit_radius_m: float | None = None
    fixed_wing_mode: bool = False
    vtol_hover_mission: bool = False
    # A hovering tailsitter in the delta SITL model flies translations rotated by a roughly constant
    # angle from the NED vector commanded (PX4 tilts the thrust vector with a wrong yaw reference).
    # When enabled, the policy measures that rotation from the pose change after each translation and
    # pre-rotates later translations by its opposite.  Target-blind: it only uses commanded vs measured motion.
    estimate_motion_offset: bool = False
    motion_offset_gain: float = 0.6

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
        if not math.isfinite(self.blocked_climb_margin_m) or self.blocked_climb_margin_m < 0:
            raise ValueError("blocked_climb_margin_m must be finite and non-negative")
        if (
            isinstance(self.max_blocked_climbs, bool)
            or not isinstance(self.max_blocked_climbs, int)
            or self.max_blocked_climbs < 0
        ):
            raise ValueError("max_blocked_climbs must be a non-negative integer")
        if self.observation_frames > 0 and self.observation_waypoints:
            self.observation_waypoints = self._make_observation_waypoints_visible(
                self.observation_waypoints
            )
        self._blocked_climbs = 0
        self._observation_orbit_active = False
        self._commanded_yaw_deg: float | None = None
        self._initial_scan_remaining = (
            0 if self.fixed_wing_mode else int(round(360.0 / self.scan_increment_deg))
        )
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
        self._coverage_trace: list[tuple[float, float]] = []
        self._search_outcome = "running"
        self._observation_index = 0
        self._observation_count = 0
        self._observation_sample_index: int | None = None
        self._observation_sample_count: int | None = None

    def _make_observation_waypoints_visible(
        self, waypoints: tuple[tuple[float, float, float], ...]
    ) -> tuple[tuple[float, float, float], ...]:
        """Move only vertically occluded view centers outward, within detection range."""
        adjusted = []
        for waypoint in waypoints:
            north, east, down = (float(value) for value in waypoint)
            dn = self.target_ned[0] - north
            de = self.target_ned[1] - east
            radius = math.hypot(dn, de)
            if radius <= 1e-6:
                adjusted.append((north, east, down))
                continue
            if math.dist(self.target_ned, (north, east, down)) > self.max_detection_range_m:
                adjusted.append((north, east, down))
                continue
            yaw = math.degrees(math.atan2(de, dn))
            visible, _, _ = target_in_fov(
                self.target_ned,
                (north, east, down),
                yaw,
                self.intrinsics,
                self.image_size,
                max_range_m=self.max_detection_range_m,
                camera_pitch_rad=self.camera_pitch_rad,
                camera_mount=self._camera_mount_for_target((north, east, down)),
            )
            if visible:
                adjusted.append((north, east, down))
                continue

            # Extend the same target-facing ray. Preserve the declared station
            # when visibility is blocked by the range gate or cannot be recovered.
            candidate_radius = radius
            candidate = (north, east, down)
            while candidate_radius + 0.5 < self.max_detection_range_m:
                candidate_radius += 0.5
                candidate = (
                    self.target_ned[0] - dn * candidate_radius / radius,
                    self.target_ned[1] - de * candidate_radius / radius,
                    down,
                )
                visible, _, _ = target_in_fov(
                    self.target_ned,
                    candidate,
                    yaw,
                    self.intrinsics,
                    self.image_size,
                    max_range_m=self.max_detection_range_m,
                    camera_pitch_rad=self.camera_pitch_rad,
                    camera_mount=self._camera_mount_for_target(candidate),
                )
                if visible:
                    break
            else:
                candidate = (north, east, down)
            adjusted.append(candidate)
        return tuple(adjusted)

    def _camera_mount_for_target(self, pose_ned: tuple[float, float, float]) -> CameraMount | None:
        mount = self.camera_mount
        if mount is None or mount.kind != "gimbal":
            return None
        dn = self.target_ned[0] - pose_ned[0]
        de = self.target_ned[1] - pose_ned[1]
        down = self.target_ned[2] - pose_ned[2]
        yaw = math.atan2(de, dn)
        pitch = math.atan2(down, math.hypot(dn, de))
        yaw = min(mount.limits.yaw_max_rad, max(mount.limits.yaw_min_rad, yaw))
        pitch = min(mount.limits.pitch_max_rad, max(mount.limits.pitch_min_rad, pitch))
        return replace(mount, pitch_rad=pitch, yaw_rad=yaw, roll_rad=0.0)

    def _coverage_radius_m(self, phase: str) -> float:
        from multidetect.search_coverage import CoverageCamera
        altitude = self.low_altitude_m if phase == "low" and self.low_altitude_m else self.altitude_m
        camera = CoverageCamera(
            fx=float(self.intrinsics["fx"]), fy=float(self.intrinsics["fy"]),
            cx=float(self.intrinsics.get("cx", self.image_size[0] / 2)),
            cy=float(self.intrinsics.get("cy", self.image_size[1] / 2)),
            width_px=int(self.image_size[0]), height_px=int(self.image_size[1]),
            pitch_down_deg=math.degrees(self.camera_pitch_rad),
        )
        width, depth = camera.footprint(max(float(altitude), 1e-3))
        return 0.5 * max(width, depth)

    def _waypoint_covered(self, waypoint: tuple[float, float], phase: str) -> bool:
        if not self._coverage_trace:
            return False
        radius = self._coverage_radius_m(phase)
        if math.dist(waypoint, self._coverage_trace[-1]) <= radius:
            return True
        return any(self._segment_distance(a, b, waypoint) <= radius for a, b in zip(self._coverage_trace, self._coverage_trace[1:]))

    def _coverage_report(self) -> dict[str, Any]:
        routes = [("high", self._waypoints)]
        if self._low_waypoints:
            routes.append(("low", self._low_waypoints))
        total = sum(len(points) for _, points in routes)
        covered = sum(sum(self._waypoint_covered(point, phase) for point in points) for phase, points in routes)
        return {"planned_waypoints": total, "covered_waypoints": covered,
                "coverage_fraction": round(covered / total, 6) if total else 1.0,
                "skipped_waypoints": len(self.skipped_waypoints)}

    def _finish_search(self) -> None:
        coverage = self._coverage_report()
        self._search_outcome = "coverage_partial" if self.skipped_waypoints and coverage["covered_waypoints"] < coverage["planned_waypoints"] else "coverage_complete"

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
        self._initial_scan_remaining = (
            0 if self.fixed_wing_mode else int(round(360.0 / self.scan_increment_deg))
        )
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
        self._blocked_climbs = 0
        self.skipped_waypoints.clear()
        self._search_outcome = "running"
        self._coverage_trace.clear()
        self._observation_index = 0
        self._observation_count = 0
        self._observation_sample_index = None
        self._observation_sample_count = None
        if not hasattr(self, "_observation_orbit_active"):
            self._observation_orbit_active = False

    @property
    def search_outcome(self) -> str:
        """Outcome of the target-blind search pass (for host state machines)."""
        return self._search_outcome

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

    def _climb_to_clear(self, pose_ned, end_n: float, end_e: float) -> ActionPrimitive | None:
        """A climb in place that clears a leg blocked below the planned altitude.

        Only when the vehicle is at least 0.2 m under the planned altitude plus
        ``blocked_climb_margin_m``, the climb itself is clear and the leg is clear
        from the raised altitude, and at most ``max_blocked_climbs`` times in a row.
        """
        if self._blocked_climbs >= self.max_blocked_climbs:
            return None
        planned = (
            self.low_altitude_m
            if self.search_pattern == "two_altitude"
            and self._search_phase == "low"
            and self.low_altitude_m is not None
            else self.altitude_m
        )
        altitude = max(0.0, -float(pose_ned[2]))
        up = min(float(planned) + self.blocked_climb_margin_m - altitude, self.max_altitude_step_m)
        if up < 0.2:
            return None
        raised = (float(pose_ned[0]), float(pose_ned[1]), -(altitude + up))
        if not segment_clear_of_obstacles(pose_ned, raised, altitude, self.obstacles):
            return None
        if not self._segment_safe(raised, end_n, end_e):
            return None
        self._blocked_climbs += 1
        return ActionPrimitive.translate(0.0, 0.0, up)

    def _safe_translation(
        self, pose_ned, action: ActionPrimitive, yaw_deg: float
    ) -> ActionPrimitive:
        yaw = math.radians(yaw_deg)
        end_n = pose_ned[0] + action.forward_m * math.cos(yaw) - action.right_m * math.sin(yaw)
        end_e = pose_ned[1] + action.forward_m * math.sin(yaw) + action.right_m * math.cos(yaw)
        if self._segment_safe(pose_ned, end_n, end_e):
            self._blocked_climbs = 0
            return action
        self._safety_replanned = True
        climb = self._climb_to_clear(pose_ned, end_n, end_e)
        if climb is not None:
            return climb
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
                                "reason": "obstacle",
                                "covered_by_other_leg": self._waypoint_covered(tuple(waypoint), phase),
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
                        "reason": "obstacle",
                        "covered_by_other_leg": self._waypoint_covered(tuple(waypoint), phase),
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

    def next_action(
        self,
        *,
        pose_ned,
        yaw_deg: float = 0.0,
        camera_pose: Mapping[str, Any] | None = None,
    ) -> ActionPrimitive:
        if not self.estimate_motion_offset:
            return self._next_action_core(pose_ned=pose_ned, yaw_deg=yaw_deg, camera_pose=camera_pose)
        offset = getattr(self, "_motion_offset_deg", 0.0)
        issued = getattr(self, "_motion_issued", None)
        if issued is not None:
            intended, start = issued
            moved = (float(pose_ned[0]) - start[0], float(pose_ned[1]) - start[1])
            if math.hypot(*intended) >= 5.0 and math.hypot(*moved) >= 3.0:
                error = math.degrees(math.atan2(moved[1], moved[0]) - math.atan2(intended[1], intended[0]))
                error = (error + 180.0) % 360.0 - 180.0
                offset = (offset + float(self.motion_offset_gain) * error + 180.0) % 360.0 - 180.0
        self._motion_offset_deg = offset
        self._motion_issued = None
        action = self._next_action_core(pose_ned=pose_ned, yaw_deg=yaw_deg, camera_pose=camera_pose)
        if action.kind == "translate" and math.hypot(action.forward_m, action.right_m) > 1e-6:
            north, east, _ = body_to_ned(action.forward_m, action.right_m, 0.0, yaw_deg)
            self._motion_issued = ((north, east), (float(pose_ned[0]), float(pose_ned[1])))
            magnitude = math.hypot(action.forward_m, action.right_m)
            angle = math.atan2(action.right_m, action.forward_m) - math.radians(offset)
            action = ActionPrimitive.translate(
                magnitude * math.cos(angle), magnitude * math.sin(angle), action.up_m
            )
        return action

    def _next_action_core(
        self,
        *,
        pose_ned,
        yaw_deg: float = 0.0,
        camera_pose: Mapping[str, Any] | None = None,
    ) -> ActionPrimitive:
        self._observation_sample_index = None
        self._observation_sample_count = None
        point = (float(pose_ned[0]), float(pose_ned[1]))
        if not self._coverage_trace or math.dist(point, self._coverage_trace[-1]) > 1e-6:
            self._coverage_trace.append(point)
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
            camera_mount=self._camera_mount_for_target(tuple(pose_ned)),
            camera_pose=(
                camera_pose
                if self.camera_mount is not None and self.camera_mount.kind == "gimbal"
                else None
            ),
            require_camera_feedback=self.camera_mount is not None and self.camera_mount.kind == "gimbal",
        )
        self._last_inside = inside
        if self.observation_frames > 0 and self.observation_waypoints:
            waypoint = self.observation_waypoints[self._observation_index]
            # A fixed-wing cannot stop at a waypoint.  Sending a multirotor
            # translate first makes PX4 enter a loiter around an intermediate
            # point, with altitude and FOV drifting before observation starts.
            # Establish the intended orbit immediately; its centre and altitude
            # are the teacher geometry and the executor handles PX4 capture.
            if self.fixed_wing_orbit_radius_m is not None and not self._observation_orbit_active:
                from multidetect.autonomy.motion_intent import MotionCommand
                self._observation_orbit_active = True
                return MotionCommand(
                    "orbit", target_ned=waypoint, radius_m=self.fixed_wing_orbit_radius_m,
                    duration_s=self.hold_seconds, speed_mps=15.0,
                )
            altitude = max(0.0, -float(pose_ned[2]))
            desired_altitude = max(0.0, -float(waypoint[2])) or self.altitude_m
            if self.fixed_wing_orbit_radius_m is None and abs(altitude - desired_altitude) > 1.0:
                return ActionPrimitive.translate(
                    0.0, 0.0, math.copysign(min(self.max_altitude_step_m, abs(desired_altitude - altitude)), desired_altitude - altitude)
                )
            dn, de = waypoint[0] - pose_ned[0], waypoint[1] - pose_ned[1]
            distance = math.hypot(dn, de)
            if self.fixed_wing_orbit_radius_m is not None and self._observation_orbit_active:
                pass
            elif distance > 2.0:
                yaw = math.radians(yaw_deg)
                return self._safe_translation(
                    pose_ned,
                    ActionPrimitive.translate(
                        min(self.max_translate_m, distance) * (dn * math.cos(yaw) + de * math.sin(yaw)) / distance,
                        min(self.max_translate_m, distance) * (-dn * math.sin(yaw) + de * math.cos(yaw)) / distance,
                        0.0,
                    ),
                    yaw_deg,
                )
            if self.fixed_wing_orbit_radius_m is None and (
                self.camera_mount is None
                or self.camera_mount.kind != "gimbal"
                or camera_pose is None
            ):
                target_bearing = math.degrees(math.atan2(self.target_ned[1] - pose_ned[1], self.target_ned[0] - pose_ned[0]))
                turn = self._wrap(target_bearing - yaw_deg)
                if abs(turn) > 4.0:
                    return ActionPrimitive.turn(turn)
            if inside:
                self.state = "observe"
                self._observation_count += 1
                self._observation_sample_index = self._observation_index
                self._observation_sample_count = self._observation_count
            else:
                self.state = "search"
            if self._observation_count >= self.observation_frames:
                if self._observation_index + 1 < len(self.observation_waypoints):
                    self._observation_index += 1
                    self._observation_count = 0
                    self._observation_orbit_active = False
                    self.state = "search"
                else:
                    self.state = "done"
                    self._search_outcome = "done"
                    return ActionPrimitive.stop()
            if self.fixed_wing_orbit_radius_m is not None:
                from multidetect.autonomy.motion_intent import MotionCommand
                return MotionCommand(
                    "orbit", target_ned=waypoint, radius_m=self.fixed_wing_orbit_radius_m,
                    duration_s=self.hold_seconds, speed_mps=15.0,
                )
            return ActionPrimitive.hold(self.hold_seconds)
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
            if self.restart_route:
                self._waypoint_index = 0
            else:
                self.state = "done"
                self._finish_search()
                return ActionPrimitive.stop()
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
                if self.restart_route:
                    self._waypoint_index = 0
                else:
                    self.state = "done"
                    self._finish_search()
                    return ActionPrimitive.stop()
            waypoint = waypoints[self._waypoint_index]
        return self._search_leg_action(pose_ned, waypoint, yaw_deg)

    def labels(
        self,
        *,
        pose_ned,
        yaw_deg: float = 0.0,
        camera_pose: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        inside, bearing, rng = target_in_fov(
            self.target_ned,
            pose_ned,
            yaw_deg,
            self.intrinsics,
            self.image_size,
            max_range_m=self.max_detection_range_m,
            camera_pitch_rad=self.camera_pitch_rad,
            camera_mount=self._camera_mount_for_target(tuple(pose_ned)),
            camera_pose=(
                camera_pose
                if self.camera_mount is not None and self.camera_mount.kind == "gimbal"
                else None
            ),
            require_camera_feedback=self.camera_mount is not None and self.camera_mount.kind == "gimbal",
        )
        commanded_mount = self._camera_mount_for_target(tuple(pose_ned))
        return {
            "phase": "observe" if self._observation_sample_index is not None else self.state,
            "target_in_fov": inside,
            "target_bearing_deg": bearing,
            "target_range_m": rng,
            **(
                {"gimbal_command": {
                    "pitch_down_rad": commanded_mount.pitch_rad,
                    "yaw_world_rad": commanded_mount.yaw_rad,
                    "mode": commanded_mount.stabilization,
                }}
                if commanded_mount is not None
                else {}
            ),
            "approach_replanned": self._approach_replanned,
            "safety_replanned": self._safety_replanned,
            "standoff_m": self.effective_standoff_m,
            "skipped_waypoints": [dict(item) for item in self.skipped_waypoints],
            "coverage": self._coverage_report(),
            "search_pass_outcome": self._search_outcome,
            **(
                {
                    "observation_view_index": (
                        self._observation_sample_index
                        if self._observation_sample_index is not None
                        else self._observation_index
                    ),
                    **(
                        {"observation_view": self.observation_view_names[
                            self._observation_sample_index
                            if self._observation_sample_index is not None
                            else self._observation_index
                        ]}
                        if len(self.observation_view_names) > (
                            self._observation_sample_index
                            if self._observation_sample_index is not None
                            else self._observation_index
                        )
                        else {}
                    ),
                    **(
                        {"observation_frame_index": self._observation_sample_count}
                        if self._observation_sample_count is not None
                        else {}
                    ),
                }
                if self.observation_waypoints
                else {}
            ),
        }


class CruiseNotFeasible(ValueError):
    """The tallest obstacle near the cruise ring needs a higher altitude than the cap allows."""


def cruise_altitude_for_obstacles(
    obstacles: Sequence[SDFObstacle],
    centre_ne: tuple[float, float],
    *,
    base_altitude_m: float,
    envelope_radius_m: float,
    clearance_m: float = 25.0,
    max_altitude_m: float = 120.0,
) -> float:
    """Lowest cruise altitude (>= base) that clears every obstacle inside the flight envelope.

    The envelope is the disc around the target that the cruise ring, its chords and the high orbit sweep.
    Raises CruiseNotFeasible when the required altitude exceeds ``max_altitude_m``.
    """
    needed = float(base_altitude_m)
    for obstacle in obstacles:
        reach = math.hypot(obstacle.size_north_m, obstacle.size_east_m) / 2.0
        if math.hypot(obstacle.north_m - centre_ne[0], obstacle.east_m - centre_ne[1]) - reach <= envelope_radius_m:
            needed = max(needed, obstacle.top_m + clearance_m)
    if needed > max_altitude_m:
        raise CruiseNotFeasible(
            f"fw cruise needs {needed:.0f} m clearance over the cruise ring, above the {max_altitude_m:.0f} m cap"
        )
    return needed


class FwCruiseObservePolicy:
    """Take off, cruise in FW between waypoints around the target, orbit it, transition back to MC, then observe.

    A tailsitter mission that exercises the whole envelope: forward-flight legs and a high orbit
    (gimbal frames from cruise altitude), the FW->MC back-transition, and the close-range hover
    observation of the wrapped ``inner`` policy.  The collector sees ``vtol_hover_mission=False`` so the
    vehicle is transitioned to FW before the first step; the inner policy keeps its hover behaviour.
    """

    vtol_hover_mission = False

    def __init__(
        self,
        inner: ExpertSearchPolicy,
        *,
        altitude_m: float = 40.0,
        ring_radius_m: float = 110.0,
        legs: int = 4,
        orbit_radius_m: float = 70.0,
        orbit_s: float = 30.0,
        cruise_speed_mps: float = 18.0,
        approach_m: float = 60.0,
        min_airspeed_mps: float = 13.0,
        max_speed_retries: int = 2,
    ) -> None:
        if legs < 1 or altitude_m <= 0 or ring_radius_m <= 0 or orbit_radius_m <= 0 or approach_m < 0:
            raise ValueError("legs, altitude, ring, orbit and approach must be positive")
        self.inner = inner
        self.altitude_m = float(altitude_m)
        self.orbit_radius_m = float(orbit_radius_m)
        self.orbit_s = float(orbit_s)
        self.cruise_speed_mps = float(cruise_speed_mps)
        # Below this airspeed the next leg's turn risks a stall/tumble (FW_AIRSPD_STALL 10, MIN 12): re-issue the
        # previous leg so the vehicle speeds up first.
        self.min_airspeed_mps = float(min_airspeed_mps)
        self.max_speed_retries = int(max_speed_retries)
        self._speed_retries = 0
        north, east = float(inner.target_ned[0]), float(inner.target_ned[1])
        start = math.atan2(-east, -north)  # first ring point on the home side of the target
        self._centre = (north, east, -self.altitude_m)
        self._waypoints = [
            (
                north + ring_radius_m * math.cos(start + k * 2.0 * math.pi / legs),
                east + ring_radius_m * math.sin(start + k * 2.0 * math.pi / legs),
                -self.altitude_m,
            )
            for k in range(legs)
        ]
        # The last cruise point is where the vehicle slows into the back-transition.
        end = start + (legs - 1) * 2.0 * math.pi / legs
        self._waypoints[-1] = (
            north + approach_m * math.cos(end), east + approach_m * math.sin(end), -self.altitude_m
        )
        self._index = 0
        self.phase = "cruise"

    def __getattr__(self, name: str) -> Any:
        if name in {"inner", "__setstate__"} or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def labels(self, **kwargs: Any) -> dict[str, Any]:
        labels = self.inner.labels(**kwargs)
        if self.phase != "observe":
            labels["phase"] = self.phase
            labels["cruise_leg"] = self._index
        return labels

    def next_action(
        self, *, pose_ned, yaw_deg: float = 0.0, camera_pose: Mapping[str, Any] | None = None,
        airspeed_mps: float | None = None,
    ):
        from multidetect.autonomy.motion_intent import MotionCommand

        if self.phase == "cruise":
            if self._index < len(self._waypoints):
                if (
                    airspeed_mps is not None and 0 < self._index and airspeed_mps < self.min_airspeed_mps
                    and self._speed_retries < self.max_speed_retries
                ):
                    self._speed_retries += 1
                    return MotionCommand(
                        "reposition", target_ned=self._waypoints[self._index - 1], speed_mps=self.cruise_speed_mps,
                        mode="FW", reason="cruise_speed_recovery",
                    )
                self._speed_retries = 0
                target = self._waypoints[self._index]
                self._index += 1
                return MotionCommand(
                    "reposition", target_ned=target, speed_mps=self.cruise_speed_mps, mode="FW", reason="cruise_leg"
                )
            self.phase = "orbit" if self.orbit_s > 0 else "back_transition"
        if self.phase == "orbit":
            self.phase = "back_transition"
            return MotionCommand(
                "orbit", target_ned=self._centre, radius_m=self.orbit_radius_m, duration_s=self.orbit_s,
                speed_mps=self.cruise_speed_mps, mode="FW", reason="high_orbit",
            )
        if self.phase == "back_transition":
            self.phase = "observe"
            return MotionCommand("transition", mode="MC", reason="back_transition")
        return self.inner.next_action(pose_ned=pose_ned, yaw_deg=yaw_deg, camera_pose=camera_pose)


__all__ = [
    "ExploreBounds",
    "CruiseNotFeasible",
    "FwCruiseObservePolicy",
    "cruise_altitude_for_obstacles",
    "ExplorePolicy",
    "HoverPolicy",
    "ExpertSearchPolicy",
    "SDFObstacle",
    "load_sdf_obstacles",
    "segment_clear_of_obstacles",
    "target_in_fov",
]
