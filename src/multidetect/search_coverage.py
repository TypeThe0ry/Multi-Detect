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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from multidetect.simulator.task_semantics import TYPICAL_SIZE_M


@dataclass(frozen=True, slots=True)
class CoverageCamera:
    # Recorded onboard episodes use the 1280x960, 540 px focal-length camera
    # model (the same values used by the policy tests).
    fx: float = 540.0
    fy: float = 540.0
    cx: float = 640.0
    cy: float = 360.0
    width_px: int = 1280
    height_px: int = 960
    pitch_down_deg: float = 10.3

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
        return min(self.fx, self.fy) * object_size_m / max(altitude_m, 1e-6)


@dataclass(frozen=True, slots=True)
class CoverageConfig:
    radius_m: float
    altitude_m: float = 35.0
    low_altitude_m: float = 12.0
    flight_speed_mps: float = 8.0
    turn_time_s: float = 2.0
    hover_per_slow_call_s: float = 1.0
    proposal_floor_px: float = 10.0
    max_detection_range_m: float = 80.0
    overlap: float = 0.20
    object_count: int = 200
    seed: int = 7

    def __post_init__(self) -> None:
        if self.radius_m <= 0 or self.flight_speed_mps <= 0:
            raise ValueError("radius and flight speed must be positive")
        if not 0 <= self.overlap < 1:
            raise ValueError("overlap must be in [0, 1)")
        if self.object_count <= 0:
            raise ValueError("object_count must be positive")


@dataclass(frozen=True, slots=True)
class CoverageMetrics:
    pattern: str
    object_size_m: float
    radius_m: float
    seen_well_fraction: float
    time_to_cover_90_s: float | None
    total_time_s: float
    expected_slow_calls: float
    objects_seen_well: int
    object_count: int


Point = tuple[float, float]


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
    points: list[Point] = [(0.0, 0.0)]
    north = east = 0.0
    length, direction_index = spacing, 0
    directions = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))
    while math.hypot(north, east) < radius_m:
        for _ in range(2):
            dn, de = directions[direction_index % 4]
            candidate = (north + dn * length, east + de * length)
            if math.hypot(*candidate) > radius_m:
                points.append(_clip_to_circle((north, east), candidate, radius_m))
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
    objects = _objects(config.radius_m, config.object_count, config.seed)
    width, _ = camera.footprint(high)
    high_range = min(
        config.max_detection_range_m,
        high / max(1e-6, math.sin(math.radians(camera.pitch_down_deg))),
    )
    high_floor = config.proposal_floor_px * (0.5 if pattern == "two_altitude" else 1.0)
    seen: set[int] = set()
    first_seen: list[float] = []
    elapsed = 0.0
    for a, b in _segments(path):
        elapsed += math.dist(a, b) / config.flight_speed_mps
        for i, obj in enumerate(objects):
            if i in seen or _distance_to_segment(obj, a, b) > width / 2:
                continue
            if math.dist(obj, (0.0, 0.0)) > config.radius_m:
                continue
            rng = math.hypot(obj[0] - b[0], obj[1] - b[1])
            if rng <= high_range and camera.expected_pixel_size(size, high) >= high_floor:
                seen.add(i)
                first_seen.append(elapsed)
        elapsed += config.turn_time_s
    if pattern == "two_altitude":
        low = max(1.0, config.low_altitude_m)
        low_path = lawnmower(config.radius_m, camera, low, config.overlap)
        low_width, _ = camera.footprint(low)
        for a, b in _segments(low_path):
            elapsed += math.dist(a, b) / config.flight_speed_mps
            for i, obj in enumerate(objects):
                if i in seen or _distance_to_segment(obj, a, b) > low_width / 2:
                    continue
                rng = math.hypot(obj[0] - b[0], obj[1] - b[1])
                if (
                    rng <= config.max_detection_range_m
                    and camera.expected_pixel_size(size, low) >= config.proposal_floor_px
                ):
                    seen.add(i)
                    first_seen.append(elapsed)
            elapsed += config.turn_time_s
    first_seen.sort()
    n90 = math.ceil(0.9 * config.object_count)
    t90 = first_seen[n90 - 1] + config.hover_per_slow_call_s if len(first_seen) >= n90 else None
    total = elapsed + len(seen) * config.hover_per_slow_call_s
    return CoverageMetrics(
        pattern,
        size,
        config.radius_m,
        len(seen) / config.object_count,
        t90,
        total,
        float(len(seen)),
        len(seen),
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
