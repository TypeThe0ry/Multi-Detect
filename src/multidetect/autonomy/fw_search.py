"""Target-blind area search for aircraft that cannot hover.

The multirotor search scans by turning in place and stepping ~15 m; a fixed-wing
(or VTOL in FW mode) turned into "reposition here" just circles where it is.  This
search flies lawnmower lanes with Dubins turns (``fw_geometry.lawnmower_search``)
and hands the loop one leg at a time, each at most ``max_leg_m`` ahead, so
consecutive repositions chain into continuous flight along the lanes.

A plane does not fly through a reposition point: the autopilot loiters around it
(PX4 NAV_LOITER_RAD, 80 m by default).  A waypoint the aircraft keeps circling,
abeam and no closer, therefore counts as passed: in SITL (rc_cessna, w0016 and
w0031) the 10 m acceptance was never met, the plane circled the first lane start
at 64-92 m for about 100 steps and the lanes were never flown.
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
        capture_m: float | None = None,
        abeam_deg: float = 45.0,
        progress_m: float = 5.0,
        circling_calls: int = 3,
        stalled_calls: int = 6,
        grid_cell_m: float = 15.0,
        coverage_threshold: float = 0.85,
        max_search_calls: int = 80,
        return_reserve_calls: int = 35,
        min_coverage_calls: int = 2,
        camera_pitch_rad: float = math.radians(10.0),
        coverage_enabled: bool = False,
    ) -> None:
        if max_leg_m <= 0 or radius_m <= 0:
            raise ValueError("search radius and leg length must be positive")
        north, east = float(home_ned[0]), float(home_ned[1])
        width = min(max(float(footprint_width_m), 30.0), 2.0 * float(radius_m))
        path = lawnmower_search(
            (north - radius_m, north + radius_m, east - radius_m, east + radius_m),
            width,
            overlap,
            max(float(turn_radius_m), 1.0),
        )
        # One waypoint every ~0.9 leg along the sampled lanes and turns.
        turn_diameter = 2.0 * max(float(turn_radius_m), 1.0)
        spacing = max(0.9 * float(max_leg_m), turn_diameter)
        self.waypoint_spacing_m = spacing
        points = [p.position_ne for p in path.points]
        waypoints = [points[0]]
        travelled = 0.0
        for a, b in zip(points, points[1:], strict=False):
            travelled += math.hypot(b[0] - a[0], b[1] - a[1])
            if travelled >= spacing:
                waypoints.append(b)
                travelled = 0.0
        if waypoints[-1] != points[-1]:
            waypoints.append(points[-1])
        self.waypoints: tuple[tuple[float, float], ...] = tuple(waypoints)
        self.max_leg_m = float(max_leg_m)
        self.accept_m = float(accept_m)
        # Within ``capture_m`` a waypoint that stays abeam or behind without getting
        # closer for ``circling_calls`` legs is being circled, not approached.  The
        # default bound (two legs plus a turn diameter) holds the observed ring and
        # keeps a far waypoint -- e.g. after an approach elsewhere -- from counting.
        self.capture_m = max(
            self.accept_m,
            float(capture_m)
            if capture_m is not None
            else 2.0 * (self.max_leg_m + max(float(turn_radius_m), 1.0)),
        )
        self.abeam_deg = float(abeam_deg)
        self.progress_m = float(progress_m)
        self.circling_calls = max(1, int(circling_calls))
        self.stalled_calls = max(1, int(stalled_calls))
        self._closest = math.inf
        self._circling = 0
        self._stalled = 0
        self.footprint_width_m = width
        self.grid_cell_m = max(1.0, float(grid_cell_m))
        self.coverage_threshold = min(1.0, max(0.0, float(coverage_threshold)))
        self.max_search_calls = max(1, int(max_search_calls))
        self.return_reserve_calls = max(1, int(return_reserve_calls))
        self.min_coverage_calls = max(1, int(min_coverage_calls))
        self.camera_pitch_rad = max(math.radians(10.0), float(camera_pitch_rad))
        self.coverage_enabled = bool(coverage_enabled)
        self._bounds = (north - radius_m, north + radius_m, east - radius_m, east + radius_m)
        self._grid = self._make_grid()
        self._last_pose: tuple[float, float] | None = None
        self.search_calls = 0
        self.search_outcome: str | None = None
        self.passes = 0
        self._index = 0

    def _make_grid(self) -> dict[tuple[int, int], bool]:
        n0, n1, e0, e1 = self._bounds
        cells = {}
        for ni in range(math.ceil((n1 - n0) / self.grid_cell_m)):
            for ei in range(math.ceil((e1 - e0) / self.grid_cell_m)):
                cells[(ni, ei)] = False
        return cells

    @property
    def coverage_fraction(self) -> float:
        return sum(self._grid.values()) / max(1, len(self._grid))

    def _record_camera_coverage(self, pose: Sequence[float], yaw_deg: float) -> None:
        north, east = float(pose[0]), float(pose[1])
        altitude = max(1.0, -float(pose[2]))
        look_down = self.camera_pitch_rad
        along = altitude / math.tan(look_down)
        heading = math.radians(float(yaw_deg))
        # The forward camera footprint spans from the near ground intersection
        # through the far intersection.  Its conservative cell test is centered
        # on the aircraft with a radius covering that whole projected interval.
        centre = (north, east)
        half_width = self.footprint_width_m * 0.5
        radius = math.hypot(max(half_width, along), self.grid_cell_m * 0.75)
        n0, _, e0, _ = self._bounds
        for cell in self._grid:
            cn = n0 + (cell[0] + 0.5) * self.grid_cell_m
            ce = e0 + (cell[1] + 0.5) * self.grid_cell_m
            if math.dist((cn, ce), centre) <= radius:
                self._grid[cell] = True
        if self._last_pose is not None:
            # A short observed interval sweeps the forward footprint along track.
            start, end = self._last_pose, (north, east)
            length = math.dist(start, end)
            samples = max(1, math.ceil(length / (self.grid_cell_m * 0.5)))
            for index in range(1, samples):
                t = index / samples
                sample = (start[0] + t * (end[0] - start[0]), start[1] + t * (end[1] - start[1]))
                shifted = (sample[0] + along * math.cos(heading), sample[1] + along * math.sin(heading))
                for cell in self._grid:
                    cn = n0 + (cell[0] + 0.5) * self.grid_cell_m
                    ce = e0 + (cell[1] + 0.5) * self.grid_cell_m
                    if math.dist((cn, ce), shifted) <= radius:
                        self._grid[cell] = True
        self._last_pose = (north, east)

    def _next_uncovered(self) -> tuple[float, float] | None:
        n0, _, e0, _ = self._bounds
        cells = [cell for cell, covered in self._grid.items() if not covered]
        if not cells:
            return None
        cell = min(cells, key=lambda item: math.hypot(
            n0 + (item[0] + 0.5) * self.grid_cell_m - (self._last_pose or (0.0, 0.0))[0],
            e0 + (item[1] + 0.5) * self.grid_cell_m - (self._last_pose or (0.0, 0.0))[1],
        ))
        return (n0 + (cell[0] + 0.5) * self.grid_cell_m, e0 + (cell[1] + 0.5) * self.grid_cell_m)

    def _reached(self, north: float, east: float, yaw_deg: float) -> bool:
        wn, we = self.waypoints[self._index]
        distance = math.hypot(wn - north, we - east)
        if distance <= self.accept_m:
            return True
        if distance > self.capture_m:
            self._circling, self._stalled = 0, 0
            return False
        if distance < self._closest - self.progress_m:
            self._closest, self._circling, self._stalled = distance, 0, 0
            return False
        self._closest = min(self._closest, distance)
        self._stalled += 1
        bearing = math.degrees(math.atan2(we - east, wn - north))
        off_nose = abs((bearing - float(yaw_deg) + 180.0) % 360.0 - 180.0)
        if off_nose >= self.abeam_deg:
            self._circling += 1
        return (
            self._circling >= self.circling_calls
            or self._stalled >= self.stalled_calls
        )

    def _advance(self) -> None:
        self._index += 1
        self._closest, self._circling, self._stalled = math.inf, 0, 0
        if self._index >= len(self.waypoints):
            self._index, self.passes = 0, self.passes + 1

    def next_action(self, *, pose_ned: Sequence[float], yaw_deg: float = 0.0) -> ActionPrimitive:
        north, east = float(pose_ned[0]), float(pose_ned[1])
        if self.coverage_enabled:
            self._record_camera_coverage(pose_ned, yaw_deg)
            self.search_calls += 1
            if (
                self.search_calls >= self.min_coverage_calls
                and self.coverage_fraction >= self.coverage_threshold
            ):
                self.search_outcome = "coverage_complete"
                return ActionPrimitive.stop()
            if self.search_calls >= self.max_search_calls:
                self.search_outcome = "budget_reserve"
                return ActionPrimitive.stop()
            target = self._next_uncovered()
        else:
            target = None
        if target is not None:
            dn, de = target[0] - north, target[1] - east
            distance = math.hypot(dn, de)
            if distance > self.max_leg_m:
                scale = 0.95 * self.max_leg_m / distance
                dn, de = dn * scale, de * scale
            yaw = math.radians(float(yaw_deg))
            return ActionPrimitive.translate(
                dn * math.cos(yaw) + de * math.sin(yaw),
                -dn * math.sin(yaw) + de * math.cos(yaw),
                0.0,
            )
        for _ in range(len(self.waypoints)):
            if not self._reached(north, east, yaw_deg):
                break
            self._advance()
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
