"""Offline, pure-geometry coverage simulation for bounded onboard searches.

The simulator intentionally has no simulator/Gazebo dependency.  A path is a
sequence of ground waypoints; objects are seen when a path segment passes
within the camera ground footprint and the projected object size is above the
proposal floor.  It is useful for comparing route choices before changing the
onboard executor.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO, DEFAULT_CAMERA_PITCH_DOWN_RAD
from multidetect.simulator.task_semantics import TYPICAL_SIZE_M


@dataclass(frozen=True, slots=True)
class CoverageCamera:
    # Recorded onboard episodes use the 1280x960, 540 px focal-length camera
    # model (the same values used by the policy tests).
    fx: float = DEFAULT_CAMERA_INFO["fx"]
    fy: float = DEFAULT_CAMERA_INFO["fy"]
    cx: float = DEFAULT_CAMERA_INFO["cx"]
    # Recorded 1280x960 episodes use the optical centre at (640, 480).
    cy: float = DEFAULT_CAMERA_INFO["cy"]
    width_px: int = DEFAULT_CAMERA_INFO["width"]
    height_px: int = DEFAULT_CAMERA_INFO["height"]
    pitch_down_deg: float = math.degrees(DEFAULT_CAMERA_PITCH_DOWN_RAD)

    @classmethod
    def from_runtime(
        cls,
        camera_info: Mapping[str, float],
        *,
        pitch_down_rad: float = DEFAULT_CAMERA_PITCH_DOWN_RAD,
    ) -> CoverageCamera:
        """Build from the ``DEFAULT_CAMERA_INFO``/OnboardPolicy camera mapping."""
        return cls(
            fx=float(camera_info["fx"]), fy=float(camera_info["fy"]),
            cx=float(camera_info["cx"]), cy=float(camera_info["cy"]),
            width_px=int(camera_info["width"]), height_px=int(camera_info["height"]),
            pitch_down_deg=math.degrees(float(pitch_down_rad)),
        )

    @property
    def hfov_rad(self) -> float:
        return 2.0 * math.atan2(self.width_px, 2.0 * self.fx)

    @property
    def vfov_rad(self) -> float:
        return 2.0 * math.atan2(self.height_px, 2.0 * self.fy)

    def footprint(self, altitude_m: float) -> tuple[float, float]:
        """Return (cross-track width, along-track depth) of the ground swath.

        The four image corners are intersected with a flat ground plane.  This
        handles a pitched mount without requiring a rendering backend.
        """
        if altitude_m <= 0 or not math.isfinite(altitude_m):
            raise ValueError("altitude_m must be finite and positive")
        pitch = math.radians(self.pitch_down_deg)
        points: list[tuple[float, float]] = []
        for u in (0.0, float(self.width_px)):
            for v in (0.0, float(self.height_px)):
                x = (u - self.cx) / self.fx
                y = (v - self.cy) / self.fy
                forward = math.cos(pitch) - y * math.sin(pitch)
                down = math.sin(pitch) + y * math.cos(pitch)
                if down <= 1e-6:
                    continue
                t = altitude_m / down
                points.append((forward * t, x * t))
        # With a strongly pitched mount the upper image edge can look above
        # the horizon.  Use the two bottom corners plus the optical-axis ray
        # in that case; the useful finite ground swath is still well defined.
        if len(points) < 2:
            raise ValueError("camera rays do not intersect ground")
        if len(points) == 2:
            u, v = self.cx, self.cy
            x, y = 0.0, (v - self.cy) / self.fy
            forward = math.cos(pitch) - y * math.sin(pitch)
            down = math.sin(pitch) + y * math.cos(pitch)
            if down > 1e-6:
                t = altitude_m / down
                points.append((forward * t, x * t))
        n = [p[0] for p in points]
        e = [p[1] for p in points]
        return max(e) - min(e), max(n) - min(n)

    def expected_pixel_size(self, object_size_m: float, altitude_m: float) -> float:
        """Approximate the smallest projected object dimension in pixels."""
        if object_size_m <= 0 or not math.isfinite(object_size_m):
            raise ValueError("object_size_m must be finite and positive")
        if altitude_m <= 0 or not math.isfinite(altitude_m):
            raise ValueError("altitude_m must be finite and positive")
        return min(self.fx, self.fy) * object_size_m / max(altitude_m, 1e-6)


@dataclass(frozen=True, slots=True)
class CoverageConfig:
    radius_m: float
    altitude_m: float = 35.0
    low_altitude_m: float = 12.0
    flight_speed_mps: float = 8.0
    turn_time_s: float = 2.0
    hover_per_slow_call_s: float = 1.0
    # Runtime gates: candidate filter, slow-verdict eligibility, confirmation.
    # ``proposal_floor_px`` is retained as the public simulator name for the
    # first gate because older callers use it.
    proposal_floor_px: float = 6.0
    slow_verdict_min_px: float = 10.0
    confirmation_min_px: float = 20.0
    max_detection_range_m: float = 80.0
    overlap: float = 0.20
    object_count: int = 200
    seed: int = 7

    def __init__(self, radius_m: float | None = None, **kwargs):
        if radius_m is None:
            try:
                radius_m = kwargs.pop("radius_m")
            except KeyError as exc:
                raise TypeError("radius_m is required") from exc
        aliases = {
            "min_box_side_px": "proposal_floor_px",
            "min_verdict_side_px": "slow_verdict_min_px",
            "confirm_min_side_px": "confirmation_min_px",
        }
        for old, new in aliases.items():
            if old in kwargs:
                if new in kwargs:
                    raise TypeError(f"{old} and {new} are aliases; pass one")
                kwargs[new] = kwargs.pop(old)
        object.__setattr__(self, "radius_m", radius_m)
        for field in self.__dataclass_fields__:
            if field != "radius_m":
                default = self.__dataclass_fields__[field].default
                object.__setattr__(self, field, kwargs.pop(field, default))
        if kwargs:
            raise TypeError(f"unexpected config keys: {', '.join(kwargs)}")
        self.__post_init__()

    def __post_init__(self) -> None:
        numeric = (
            self.radius_m,
            self.altitude_m,
            self.low_altitude_m,
            self.flight_speed_mps,
            self.turn_time_s,
            self.hover_per_slow_call_s,
            self.proposal_floor_px,
            self.slow_verdict_min_px,
            self.confirmation_min_px,
            self.max_detection_range_m,
        )
        if not all(math.isfinite(value) and value > 0 for value in numeric):
            raise ValueError("radius, altitudes, timing, thresholds and range must be positive")
        if self.low_altitude_m > self.altitude_m:
            raise ValueError("low_altitude_m must not exceed altitude_m")
        if not 0 <= self.overlap < 1:
            raise ValueError("overlap must be in [0, 1)")
        if self.object_count <= 0:
            raise ValueError("object_count must be positive")

    @property
    def candidate_filter_px(self) -> float:
        """The fast proposer/candidate gate (LoopConfig.min_box_side_px)."""
        return self.proposal_floor_px


@dataclass(frozen=True, slots=True)
class CoverageMetrics:
    pattern: str
    object_size_m: float
    radius_m: float
    seen_well_fraction: float
    candidate_fraction: float
    slow_verdict_fraction: float
    confirmation_fraction: float
    time_to_cover_90_s: float | None
    candidate_time_to_cover_90_s: float | None
    slow_verdict_time_to_cover_90_s: float | None
    confirmation_time_to_cover_90_s: float | None
    total_time_s: float
    expected_slow_calls: float
    objects_seen_well: int
    candidates_seen: int
    slow_verdict_eligible: int
    confirmed: int
    object_count: int


Point = tuple[float, float]
RoutePoint = tuple[float, float, float]


def _inside(point: Point, radius: float) -> bool:
    return math.hypot(*point) <= radius + 1e-9


def _clip_to_circle(a: Point, b: Point, radius: float) -> Point:
    """Return the furthest point from ``a`` on segment ``a``-``b`` in circle."""
    dn, de = b[0] - a[0], b[1] - a[1]
    aa, bb, cc = dn * dn + de * de, 2 * (a[0] * dn + a[1] * de), a[0] ** 2 + a[1] ** 2 - radius**2
    if aa <= 1e-12:
        return a
    disc = max(0.0, bb * bb - 4 * aa * cc)
    ts = [
        t
        for t in ((-bb - math.sqrt(disc)) / (2 * aa), (-bb + math.sqrt(disc)) / (2 * aa))
        if t >= 0
    ]
    t = min(ts, default=0.0)
    return (a[0] + dn * t, a[1] + de * t)


def _segments(points: Sequence[Point]) -> list[tuple[Point, Point]]:
    return list(zip(points, points[1:], strict=False))


def current_expanding_square(
    radius_m: float, camera: CoverageCamera, altitude_m: float
) -> list[Point]:
    """Replicate ``ExpertSearchPolicy._build_spiral`` exactly (square legs)."""
    hfov = camera.hfov_rad
    vfov = camera.vfov_rad
    vertical_ratio = math.tan(vfov / 2.0)
    pitch = math.radians(camera.pitch_down_deg)
    onset = (
        altitude_m
        * (math.cos(pitch) - vertical_ratio * math.sin(pitch))
        / max(1e-6, math.sin(pitch) + vertical_ratio * math.cos(pitch))
    )
    camera_forward = onset * math.cos(pitch) + altitude_m * math.sin(pitch)
    half_swath = math.tan(hfov / 2.0) * max(0.0, camera_forward)
    spacing = min(70.0, radius_m / 2.0, max(8.0, 1.6 * half_swath))
    # ExpertSearchPolicy._build_spiral starts with the first waypoint; home is
    # deliberately not part of that list.  The simulator adds home only when
    # accounting for the initial flight leg.
    points: list[Point] = []
    north = east = 0.0
    length, direction_index = spacing, 0
    directions = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))
    while math.hypot(north, east) < radius_m:
        for _ in range(2):
            dn, de = directions[direction_index % 4]
            candidate = (north + dn * length, east + de * length)
            if math.hypot(*candidate) > radius_m:
                # The policy scales the overshoot radially from home, rather
                # than intersecting the current leg with the circle.
                scale = radius_m / max(math.hypot(*candidate), 1e-12)
                points.append((candidate[0] * scale, candidate[1] * scale))
                return points
            north, east = candidate
            points.append(candidate)
            direction_index += 1
        length += spacing
    return points


def lawnmower(
    radius_m: float, camera: CoverageCamera, altitude_m: float, overlap: float = 0.2
) -> list[Point]:
    width, _ = camera.footprint(altitude_m)
    spacing = max(1.0, width * (1.0 - overlap))
    lines: list[Point] = []
    y = -radius_m
    while y <= radius_m + 1e-9:
        x = math.sqrt(max(0.0, radius_m * radius_m - y * y))
        left, right = (-x, y), (x, y)
        lines.extend((left, right) if len(lines) // 2 % 2 == 0 else (right, left))
        y += spacing
    if not lines:
        lines = [(-radius_m, 0.0), (radius_m, 0.0)]
    return [(0.0, 0.0), *lines]


def two_altitude_route(
    radius_m: float,
    camera: CoverageCamera,
    high_altitude_m: float,
    low_altitude_m: float,
    overlap: float = 0.2,
    max_altitude_step_m: float = 8.0,
) -> list[RoutePoint]:
    """Return the finite high+descent+low route used by the runtime policy.

    Centre waypoints are intentionally dropped: the vehicle starts at the
    centre, and the low pass starts at its first sweep line after descending at
    the final high waypoint.  A caller may repeat the low suffix for counting.
    """
    if not 0 < low_altitude_m <= high_altitude_m or max_altitude_step_m <= 0:
        raise ValueError("invalid two-altitude heights or step")
    high = lawnmower(radius_m, camera, high_altitude_m, overlap)[1:]
    low = lawnmower(radius_m, camera, low_altitude_m, overlap)[1:]
    if not high or not low:
        return []
    route: list[RoutePoint] = [(n, e, high_altitude_m) for n, e in high]
    n, e, _ = route[-1]
    altitude = high_altitude_m
    while altitude - low_altitude_m > 0.5:
        altitude = max(low_altitude_m, altitude - max_altitude_step_m)
        route.append((n, e, altitude))
    route.extend((n, e, low_altitude_m) for n, e in low)
    return route


def expanding_spiral(
    radius_m: float, camera: CoverageCamera, altitude_m: float, overlap: float = 0.2
) -> list[Point]:
    width, _ = camera.footprint(altitude_m)
    spacing = max(1.0, width * (1.0 - overlap))
    points: list[Point] = [(0.0, 0.0)]
    theta, radial = 0.0, 0.0
    # Chord spacing is bounded by the swath so adjacent turns overlap.
    while radial < radius_m:
        theta += max(0.12, spacing / max(radial, spacing))
        radial = min(radius_m, radial + spacing * theta / (2 * math.pi))
        points.append((radial * math.cos(theta), radial * math.sin(theta)))
    return points


def _distance_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = 0.0 if den <= 1e-12 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _objects(radius: float, count: int, seed: int) -> list[Point]:
    rng = random.Random(seed)
    points: list[Point] = []
    for _ in range(count):
        r = math.sqrt(rng.random()) * radius
        angle = rng.random() * 2 * math.pi
        points.append((r * math.cos(angle), r * math.sin(angle)))
    return points


def _path_time(path: Sequence[Point], cfg: CoverageConfig) -> float:
    distance = sum(math.dist(a, b) for a, b in _segments(path))
    turns = max(0, len(path) - 2)
    return distance / cfg.flight_speed_mps + turns * cfg.turn_time_s


def simulate_pattern(
    pattern: str,
    *,
    object_size_m: float | None = None,
    camera: CoverageCamera | None = None,
    config: CoverageConfig,
) -> CoverageMetrics:
    """Run one deterministic Monte Carlo route and return requested metrics."""
    camera = camera or CoverageCamera()
    size = object_size_m if object_size_m is not None else TYPICAL_SIZE_M.get(pattern, 1.0)
    high = config.altitude_m
    if pattern == "current":
        path = current_expanding_square(config.radius_m, camera, high)
    elif pattern == "lawnmower":
        path = lawnmower(config.radius_m, camera, high, config.overlap)
    elif pattern == "spiral":
        path = expanding_spiral(config.radius_m, camera, high, config.overlap)
    elif pattern == "two_altitude":
        path = lawnmower(config.radius_m, camera, high, config.overlap)
    else:
        raise ValueError(f"unknown pattern: {pattern}")
    # All routes start at the zone centre in the vehicle state.  The current
    # policy's waypoint list intentionally omits home, so account for that
    # initial leg in the offline route without changing its exact waypoint
    # reproduction helper.
    if not path or path[0] != (0.0, 0.0):
        path = [(0.0, 0.0), *path]
    objects = _objects(config.radius_m, config.object_count, config.seed)
    seen: set[int] = set()
    slow: set[int] = set()
    confirmed: set[int] = set()
    candidate_times: list[float] = []
    slow_times: list[float] = []
    confirm_times: list[float] = []
    elapsed = 0.0
    def run_pass(pass_path: Sequence[Point], altitude: float) -> None:
        nonlocal elapsed
        width, _ = camera.footprint(altitude)
        pixels = camera.expected_pixel_size(size, altitude)
        for a, b in _segments(pass_path):
            elapsed += math.dist(a, b) / config.flight_speed_mps
            for i, obj in enumerate(objects):
                if _distance_to_segment(obj, a, b) > width / 2:
                    continue
                if math.dist(obj, (0.0, 0.0)) > config.radius_m:
                    continue
                ground_range = _distance_to_segment(obj, a, b)
                if math.hypot(ground_range, altitude) > config.max_detection_range_m:
                    continue
                if pixels >= config.proposal_floor_px and i not in seen:
                    seen.add(i)
                    candidate_times.append(elapsed)
                if pixels >= config.slow_verdict_min_px and i not in slow:
                    slow.add(i)
                    slow_times.append(elapsed)
                if pixels >= config.confirmation_min_px and i not in confirmed:
                    confirmed.add(i)
                    confirm_times.append(elapsed)
            elapsed += config.turn_time_s

    if pattern != "two_altitude":
        run_pass(path, high)
    else:
        low = max(1.0, config.low_altitude_m)
        route = two_altitude_route(config.radius_m, camera, high, low, config.overlap)
        # The shared route contains the high pass, stepped descent, and low pass.
        high_path = [(0.0, 0.0), *[(n, e) for n, e, z in route if z == high]]
        run_pass(high_path, high)
        for (_n1, _e1, z1), (_n2, _e2, z2) in zip(route, route[1:], strict=False):
            if z1 != z2:
                elapsed += abs(z1 - z2) / config.flight_speed_mps
        high_end = next((point for point in reversed(route) if point[2] == high), route[0])
        low_path = [(high_end[0], high_end[1]), *[(n, e) for n, e, z in route if z == low]]
        run_pass(low_path, low)
    candidate_times.sort()
    slow_times.sort()
    confirm_times.sort()
    n90 = math.ceil(0.9 * config.object_count)
    t90 = slow_times[n90 - 1] + config.hover_per_slow_call_s if len(slow_times) >= n90 else None

    def gate90(values: list[float]) -> float | None:
        return values[n90 - 1] if len(values) >= n90 else None
    total = elapsed + len(slow) * config.hover_per_slow_call_s
    return CoverageMetrics(
        pattern,
        size,
        config.radius_m,
        len(slow) / config.object_count,
        len(seen) / config.object_count,
        len(slow) / config.object_count,
        len(confirmed) / config.object_count,
        t90,
        gate90(candidate_times),
        gate90(slow_times),
        gate90(confirm_times),
        total,
        float(len(slow)),
        len(slow),
        len(seen),
        len(slow),
        len(confirmed),
        config.object_count,
    )


def compare_patterns(
    *,
    radii: Iterable[float] = (25.0, 55.0, 100.0),
    object_sizes: Iterable[tuple[str, float]] = (("cone", 0.5), ("backpack", 0.5), ("car", 4.5)),
    camera: CoverageCamera | None = None,
    base_config: CoverageConfig | None = None,
) -> list[CoverageMetrics]:
    """Return rows for all required route/radius/object-size combinations."""
    rows: list[CoverageMetrics] = []
    for radius in radii:
        for _name, size in object_sizes:
            for pattern in ("current", "lawnmower", "spiral", "two_altitude"):
                cfg = (
                    replace(base_config, radius_m=radius)
                    if base_config is not None
                    else CoverageConfig(radius_m=radius)
                )
                rows.append(
                    simulate_pattern(pattern, object_size_m=size, camera=camera, config=cfg)
                )
    return rows


__all__ = [
    "CoverageCamera",
    "CoverageConfig",
    "CoverageMetrics",
    "compare_patterns",
    "current_expanding_square",
    "expanding_spiral",
    "lawnmower",
    "simulate_pattern",
]
