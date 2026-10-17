"""Fixed-wing geometry primitives (pure functions, local NED coordinates).

The module intentionally has no vehicle, map, or control-loop dependencies.  Angles are
radians unless a parameter name ends in ``_deg``; positions are ``(north, east, down)``
or ``(north, east)`` tuples.  The implementation is conservative around degenerate
inputs: non-finite values are rejected, while zero-length paths return a finite path.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

EPS = 1e-9
PI2 = 2.0 * math.pi


def _f(value: float, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) else default


def _pos2(value: Sequence[float]) -> tuple[float, float]:
    if len(value) < 2:
        raise ValueError("position needs north and east")
    return (_f(value[0]), _f(value[1]))


def _wrap(angle: float) -> float:
    return (angle + math.pi) % PI2 - math.pi


def _mod(angle: float) -> float:
    return angle % PI2


@dataclass(frozen=True, slots=True)
class PathPoint:
    north: float
    east: float
    heading_rad: float
    cumulative_length_m: float

    @property
    def x(self) -> float:
        return self.north

    @property
    def y(self) -> float:
        return self.east

    @property
    def yaw(self) -> float:
        return self.heading_rad

    @property
    def position_ne(self) -> tuple[float, float]:
        return (self.north, self.east)

    @property
    def position_ned(self) -> tuple[float, float, float]:
        return (self.north, self.east, 0.0)

    @property
    def heading(self) -> float:
        return self.heading_rad

    @property
    def cumulative_length(self) -> float:
        return self.cumulative_length_m


@dataclass(frozen=True, slots=True)
class DubinsPath:
    waypoints: tuple[PathPoint, ...]
    length_m: float
    word: str
    segment_lengths_m: tuple[float, float, float]

    def __iter__(self):
        return iter(self.waypoints)

    @property
    def points(self) -> tuple[PathPoint, ...]:
        return self.waypoints

    @property
    def samples(self) -> tuple[PathPoint, ...]:
        return self.waypoints


@dataclass(frozen=True, slots=True)
class SearchPath:
    waypoints: tuple[PathPoint, ...]
    lane_spacing_m: float
    lane_count: int
    mode: str
    coverage_fraction: float
    area_m2: float

    @property
    def points(self) -> tuple[PathPoint, ...]:
        return self.waypoints

    def __iter__(self):
        return iter(self.waypoints)


@dataclass(frozen=True, slots=True)
class ObservationOrbit:
    center_ned: tuple[float, float, float]
    radius_m: float
    height_m: float
    view_angle_rad: float
    waypoints: tuple[PathPoint, ...]


@dataclass(frozen=True, slots=True)
class FlyByPass:
    heading_rad: float
    entry_ned: tuple[float, float, float]
    exit_ned: tuple[float, float, float]
    path: DubinsPath
    visible_time_s: float


@dataclass(frozen=True, slots=True)
class FlyByPlan:
    passes: tuple[FlyByPass, ...]
    heading_separation_rad: float
    visible_half_length_m: float


@dataclass(frozen=True, slots=True)
class PassObservation:
    timestamp_s: float
    camera_ned: tuple[float, float, float]
    line_of_sight_ned: tuple[float, float, float]
    identity_vote: bool | str
    heading_rad: float | None = None
    ground_position_ned: tuple[float, float, float] | None = None
    triangulation_sigma_m: float | None = None
    triangulation: object | None = None


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    status: str
    reason: str
    accepted_passes: int
    distinct_headings: int
    position_spread_m: float | None = None
    max_sigma_m: float | None = None


@dataclass(frozen=True, slots=True)
class LoiterPlan:
    center_ned: tuple[float, float, float]
    radius_m: float
    side: str
    waypoints: tuple[PathPoint, ...]
    exit_point_ned: tuple[float, float, float]
    clear: bool


def _dubins_lengths(alpha: float, beta: float, d: float) -> dict[str, tuple[float, float, float]]:
    """Normalized Dubins formulas from the six standard words."""
    sa, sb = math.sin(alpha), math.sin(beta)
    ca, cb = math.cos(alpha), math.cos(beta)
    c_ab = math.cos(alpha - beta)
    out: dict[str, tuple[float, float, float]] = {}

    def add(word: str, values: tuple[float, float, float] | None) -> None:
        if values is not None and all(math.isfinite(x) and x >= -1e-8 for x in values):
            out[word] = tuple(max(0.0, x) for x in values)

    p2 = 2 + d * d - 2 * c_ab + 2 * d * (sa - sb)
    if p2 >= 0:
        p = math.sqrt(p2)
        add(
            "LSL",
            (
                _mod(-alpha + math.atan2(cb - ca, d + sa - sb)),
                p,
                _mod(beta - math.atan2(cb - ca, d + sa - sb)),
            ),
        )
    p2 = 2 + d * d - 2 * c_ab + 2 * d * (sb - sa)
    if p2 >= 0:
        p = math.sqrt(p2)
        add(
            "RSR",
            (
                _mod(alpha - math.atan2(ca - cb, d - sa + sb)),
                p,
                _mod(-beta + math.atan2(ca - cb, d - sa + sb)),
            ),
        )
    p2 = -2 + d * d + 2 * c_ab + 2 * d * (sa + sb)
    if p2 >= 0:
        p = math.sqrt(p2)
        t = math.atan2(-ca - cb, d + sa + sb) - math.atan2(-2.0, p)
        add("LSR", (_mod(-alpha + t), p, _mod(-_mod(beta) + t)))
    p2 = -2 + d * d + 2 * c_ab - 2 * d * (sa + sb)
    if p2 >= 0:
        p = math.sqrt(p2)
        t = math.atan2(ca + cb, d - sa - sb) - math.atan2(2.0, p)
        add("RSL", (_mod(alpha - t), p, _mod(beta - t)))
    p2 = (6 - d * d + 2 * c_ab + 2 * d * (sa - sb)) / 8
    if abs(p2) <= 1:
        p = _mod(PI2 - math.acos(max(-1.0, min(1.0, p2))))
        t = _mod(alpha - math.atan2(ca - cb, d - sa + sb) + p / 2)
        add("RLR", (t, p, _mod(alpha - beta - t + p)))
    p2 = (6 - d * d + 2 * c_ab + 2 * d * (sb - sa)) / 8
    if abs(p2) <= 1:
        p = _mod(PI2 - math.acos(max(-1.0, min(1.0, p2))))
        # LRL uses the mirrored tangent denominator (d + sin(alpha) - sin(beta)).
        # Keeping the sign explicit matters for the short, three-arc cases.
        t = _mod(-alpha - math.atan2(ca - cb, d + sa - sb) + p / 2)
        add("LRL", (t, p, _mod(beta - alpha - t + p)))
    return out


def dubins_path_lengths(
    start_ne_heading: tuple[Sequence[float], float],
    end_ne_heading: tuple[Sequence[float], float],
    min_turn_radius_m: float,
) -> dict[str, float]:
    """Return analytic lengths for every feasible Dubins word (metres)."""
    r = max(abs(_f(min_turn_radius_m, 1.0)), EPS)
    sx, sy = _pos2(start_ne_heading[0])
    ex, ey = _pos2(end_ne_heading[0])
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance <= EPS:
        # Analytic normalized formulas are ill-conditioned at d=0; a turn-only
        # fallback still gives a deterministic finite reference for callers.
        delta = abs(_wrap(_f(end_ne_heading[1]) - _f(start_ne_heading[1])))
        return {"L": delta * r, "R": delta * r}
    theta = math.atan2(dy, dx)
    alpha = _mod(_f(start_ne_heading[1]) - theta)
    beta = _mod(_f(end_ne_heading[1]) - theta)
    return {
        word: sum(parts) * r for word, parts in _dubins_lengths(alpha, beta, distance / r).items()
    }


def _sample_dubins(
    start: tuple[float, float],
    heading: float,
    word: str,
    lengths: tuple[float, float, float],
    radius: float,
    step: float,
) -> tuple[list[PathPoint], float]:
    step = max(abs(step), 0.25)
    x, y, yaw, total = start[0], start[1], heading, 0.0
    points = [PathPoint(x, y, yaw, 0.0)]
    for kind, seg_norm in zip(word, lengths, strict=True):
        seg = seg_norm * radius
        # A chord is very slightly shorter than its arc.  Keeping the angular
        # increment <= .01 rad makes the sampled heading/distance curvature
        # stay below the requested 1/R limit even for strict test consumers.
        local_step = min(step, radius * 0.01) if kind != "S" else step
        count = max(1, int(math.ceil(seg / local_step))) if seg > EPS else 0
        ds = seg / count if count else 0.0
        for _ in range(count):
            if kind == "S":
                x += ds * math.cos(yaw)
                y += ds * math.sin(yaw)
            else:
                sign = 1.0 if kind == "L" else -1.0
                old = yaw
                yaw += sign * ds / radius
                x += radius / sign * (math.sin(yaw) - math.sin(old))
                y += radius / sign * (-math.cos(yaw) + math.cos(old))
            total += ds
            points.append(PathPoint(x, y, _wrap(yaw), total))
    return points, total


def dubins_path(
    start_ne_heading: tuple[Sequence[float], float],
    end_ne_heading: tuple[Sequence[float], float],
    min_turn_radius_m: float,
    *,
    sample_step_m: float = 2.0,
) -> DubinsPath:
    """Generate the shortest sampled LSL/RSR/LSR/RSL/RLR/LRL path."""
    r = max(abs(_f(min_turn_radius_m, 1.0)), EPS)
    sx, sy = _pos2(start_ne_heading[0])
    ex, ey = _pos2(end_ne_heading[0])
    sh, eh = _f(start_ne_heading[1]), _f(end_ne_heading[1])
    lengths = dubins_path_lengths(((sx, sy), sh), ((ex, ey), eh), r)
    if not lengths:
        # A straight fallback is preferable to an exception for coincident/invalid data.
        length = math.hypot(ex - sx, ey - sy)
        yaw = sh if length <= EPS else math.atan2(ey - sy, ex - sx)
        p = [PathPoint(sx, sy, yaw, 0.0), PathPoint(ex, ey, eh, length)]
        return DubinsPath(tuple(p), length, "S", (0.0, length / r, 0.0))
    word, length = min(lengths.items(), key=lambda item: item[1])
    # Recompute normalized parameters for the selected word.
    dxy = math.hypot(ex - sx, ey - sy)
    if dxy <= EPS:
        delta = _wrap(eh - sh)
        kind = "L" if delta >= 0 else "R"
        arc = abs(delta)
        pts, _ = _sample_dubins((sx, sy), sh, kind + "SS", (arc, 0.0, 0.0), r, sample_step_m)
        if pts:
            pts[-1] = PathPoint(ex, ey, eh, arc * r)
        return DubinsPath(tuple(pts), arc * r, kind, (arc, 0.0, 0.0))
    theta = math.atan2(ey - sy, ex - sx)
    params = _dubins_lengths(_mod(sh - theta), _mod(eh - theta), dxy / r)[word]
    pts, actual = _sample_dubins((sx, sy), sh, word, params, r, sample_step_m)
    if pts:
        pts[-1] = PathPoint(ex, ey, eh, length)
    return DubinsPath(tuple(pts), length, word, tuple(x * r for x in params))


def max_path_curvature(path: DubinsPath | Iterable[PathPoint]) -> float:
    points = tuple(path.waypoints if isinstance(path, DubinsPath) else path)
    maximum = 0.0
    for a, b in zip(points, points[1:], strict=False):
        ds = math.hypot(b.north - a.north, b.east - a.east)
        if ds > EPS:
            maximum = max(maximum, abs(_wrap(b.heading_rad - a.heading_rad)) / ds)
    return maximum


def curvature_within_limit(
    path: DubinsPath | Iterable[PathPoint], min_turn_radius_m: float, *, tolerance: float = 1e-6
) -> bool:
    r = max(abs(_f(min_turn_radius_m, 1.0)), EPS)
    return max_path_curvature(path) <= 1.0 / r + abs(tolerance)


def _bounds(bounds: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bounds) != 4:
        raise ValueError("bounds must be (min_north, max_north, min_east, max_east)")
    n0, n1, e0, e1 = map(_f, bounds)
    return min(n0, n1), max(n0, n1), min(e0, e1), max(e0, e1)


def lawnmower_search(
    bounds: Sequence[float],
    footprint_width_m: float,
    overlap: float,
    min_turn_radius_m: float,
    *,
    sample_step_m: float = 2.0,
    start_from_low_east: bool = True,
) -> SearchPath:
    """Plan alternating fixed-wing survey lanes and rounded end turns."""
    n0, n1, e0, e1 = _bounds(bounds)
    width, r = max(abs(_f(footprint_width_m)), EPS), max(abs(_f(min_turn_radius_m)), EPS)
    overlap = min(0.99, max(0.0, _f(overlap)))
    spacing = max(EPS, width * (1.0 - overlap))
    count = max(1, int(math.ceil((e1 - e0) / spacing)))
    actual_spacing = (e1 - e0) / max(1, count)
    lane_easts = [e0 + (i + 0.5) * actual_spacing for i in range(count)]
    order = list(range(count))
    mode = "semicircle" if actual_spacing >= 2.0 * r else "skip-lane"
    if mode == "skip-lane" and count > 2:
        order = list(range(0, count, 2)) + list(range(1, count, 2))
    points: list[PathPoint] = []
    for j, index in enumerate(order):
        east = lane_easts[index]
        reverse = (j % 2 == 1) ^ (not start_from_low_east)
        a = ((n0, east), 0.0) if not reverse else ((n1, east), math.pi)
        b = ((n1, east), 0.0) if not reverse else ((n0, east), math.pi)
        lane_start = a[0]
        lane_end = b[0]
        if points:
            connector = dubins_path(
                (points[-1].position_ne, points[-1].heading_rad), a, r, sample_step_m=sample_step_m
            )
            base = points[-1].cumulative_length_m
            points.extend(
                PathPoint(p.north, p.east, p.heading_rad, base + p.cumulative_length_m)
                for p in connector.waypoints[1:]
            )
        points.extend(
            (
                PathPoint(
                    lane_start[0],
                    lane_start[1],
                    a[1],
                    points[-1].cumulative_length_m if points else 0.0,
                ),
                PathPoint(
                    lane_end[0],
                    lane_end[1],
                    b[1],
                    (points[-1].cumulative_length_m if points else 0.0) + abs(n1 - n0),
                ),
            )
        )
    area = max(0.0, (n1 - n0) * (e1 - e0))
    covered = min(e1 - e0, count * width)
    return SearchPath(
        tuple(points), actual_spacing, count, mode, (covered / (e1 - e0) if e1 > e0 else 1.0), area
    )


def rounded_square_path(
    bounds: Sequence[float], min_turn_radius_m: float, *, sample_step_m: float = 2.0
) -> tuple[PathPoint, ...]:
    """Return a square perimeter with Dubins-rounded corners."""
    n0, n1, e0, e1 = _bounds(bounds)
    r = max(abs(_f(min_turn_radius_m, 1.0)), EPS)
    corners = [
        ((n0 + r, e0), 0.0),
        ((n1 - r, e0), math.pi / 2),
        ((n1 - r, e1), math.pi),
        ((n0 + r, e1), -math.pi / 2),
    ]
    points: list[PathPoint] = []
    for i, goal in enumerate(corners):
        start = corners[i - 1] if i else corners[-1]
        segment = dubins_path(start, goal, r, sample_step_m=sample_step_m)
        offset = points[-1].cumulative_length_m if points else 0.0
        points.extend(
            PathPoint(p.north, p.east, p.heading_rad, offset + p.cumulative_length_m)
            for p in segment.waypoints[1:]
            if not points or p.position_ne != points[-1].position_ne
        )
    return tuple(points)


def estimate_coverage(
    bounds: Sequence[float], lanes: Iterable[Sequence[float] | PathPoint], swath_width_m: float
) -> float:
    """Estimate area coverage from lane positions; return a [0,1] fraction."""
    n0, n1, e0, e1 = _bounds(bounds)
    width = max(0.0, _f(swath_width_m))
    offsets = []
    for lane in lanes:
        if isinstance(lane, PathPoint):
            offsets.append(lane.east)
        elif len(lane) >= 2:
            offsets.append(_f(lane[1]))
    if not offsets or width <= 0 or e1 <= e0 or n1 <= n0:
        return 0.0
    intervals = sorted((max(e0, e - width / 2), min(e1, e + width / 2)) for e in offsets)
    merged: list[list[float]] = []
    for a, b in intervals:
        if b <= a:
            continue
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return min(1.0, max(0.0, sum(b - a for a, b in merged) / (e1 - e0)))


def observation_orbit(
    target_ned: Sequence[float],
    requested_radius_m: float,
    min_turn_radius_m: float,
    camera_depression_rad: float,
    vertical_fov_rad: float,
    *,
    sample_step_m: float = 3.0,
    points: int = 72,
) -> ObservationOrbit:
    radius = max(abs(_f(requested_radius_m, 1.0)), abs(_f(min_turn_radius_m, 1.0)), EPS)
    depression = max(1e-4, min(math.pi / 2 - 1e-4, _f(camera_depression_rad, math.pi / 4)))
    vfov = max(1e-5, abs(_f(vertical_fov_rad, math.pi / 6)))
    lower, upper = max(1e-4, depression - vfov / 2), min(math.pi / 2 - 1e-4, depression + vfov / 2)
    view = min(upper, max(lower, depression))
    height = radius * math.tan(view)
    tn, te = _pos2(target_ned)
    td = _f(target_ned[2]) if len(target_ned) > 2 else 0.0
    count = max(8, int(points))
    waypoints = tuple(
        PathPoint(
            tn + radius * math.cos(PI2 * i / count),
            te + radius * math.sin(PI2 * i / count),
            PI2 * i / count + math.pi / 2,
            PI2 * radius * i / count,
        )
        for i in range(count + 1)
    )
    return ObservationOrbit((tn, te, td - height), radius, height, view, waypoints)


def fly_by_passes(
    target_ned: Sequence[float],
    current_ned_heading: tuple[Sequence[float], float],
    passes: int,
    min_turn_radius_m: float,
    *,
    altitude_m: float = 30.0,
    airspeed_mps: float = 15.0,
    camera_depression_rad: float = math.radians(45),
    vertical_fov_rad: float = math.radians(35),
    heading_separation_rad: float = math.radians(60),
    sample_step_m: float = 3.0,
) -> FlyByPlan:
    target = (
        _f(target_ned[0]),
        _f(target_ned[1]),
        _f(target_ned[2]) if len(target_ned) > 2 else 0.0,
    )
    count = max(1, int(passes))
    sep = max(abs(_f(heading_separation_rad)), math.radians(60))
    h = max(0.1, abs(_f(altitude_m)))
    depression = max(1e-4, _f(camera_depression_rad))
    half = max(
        10.0,
        h * math.tan(max(1e-4, abs(_f(vertical_fov_rad)) / 2)) / max(math.tan(depression), 1e-3),
    )
    result: list[FlyByPass] = []
    start_pos, start_yaw = current_ned_heading[0], _f(current_ned_heading[1])
    for i in range(count):
        yaw = _wrap(start_yaw + i * sep)
        dn, de = math.cos(yaw), math.sin(yaw)
        entry = (target[0] - half * dn, target[1] - half * de)
        exit = (target[0] + half * dn, target[1] + half * de)
        path = dubins_path(
            (start_pos, start_yaw), (entry, yaw), min_turn_radius_m, sample_step_m=sample_step_m
        )
        # The returned path includes the visible straight leg.  The next pass
        # starts at its exit, so each pass is independently replayable.
        end_length = path.length_m + 2.0 * half
        path = DubinsPath(
            path.waypoints
            + (
                PathPoint(
                    exit[0],
                    exit[1],
                    yaw,
                    end_length,
                ),
            ),
            end_length,
            path.word,
            path.segment_lengths_m,
        )
        result.append(
            FlyByPass(
                yaw,
                (entry[0], entry[1], target[2] - h),
                (exit[0], exit[1], target[2] - h),
                path,
                2 * half / max(abs(_f(airspeed_mps, 1.0)), 0.1),
            )
        )
        start_pos, start_yaw = exit, yaw
    return FlyByPlan(tuple(result), sep, half)


def _as_observation(value: PassObservation | dict) -> PassObservation:
    if isinstance(value, PassObservation):
        if value.triangulation is None:
            return value
        position = value.ground_position_ned
        sigma = value.triangulation_sigma_m
        if position is None:
            position = getattr(value.triangulation, "position_ned", None)
        if sigma is None:
            sigma = getattr(value.triangulation, "sigma_m", None)
        return PassObservation(
            value.timestamp_s,
            value.camera_ned,
            value.line_of_sight_ned,
            value.identity_vote,
            value.heading_rad,
            position,
            sigma,
            value.triangulation,
        )
    tri = value.get("triangulation")
    pos = value.get("ground_position_ned", value.get("ground_position"))
    if pos is None and tri is not None:
        pos = getattr(tri, "position_ned", None) or (
            tri.get("position_ned") if isinstance(tri, dict) else None
        )
    sigma = value.get("triangulation_sigma_m", value.get("sigma_m"))
    if sigma is None and tri is not None:
        sigma = getattr(tri, "sigma_m", None) or (
            tri.get("sigma_m") if isinstance(tri, dict) else None
        )
    return PassObservation(
        _f(value.get("timestamp_s", 0.0)),
        tuple(value.get("camera_ned", (0, 0, 0))),
        tuple(value.get("line_of_sight_ned", (1, 0, 0))),
        value.get("identity_vote", value.get("identity", False)),
        value.get("heading_rad"),
        tuple(pos) if pos is not None else None,
        sigma,
        tri,
    )


def evaluate_fixed_wing_confirmation(
    observations: Iterable[PassObservation | dict],
    *,
    min_passes: int = 3,
    min_heading_separation_deg: float = 60.0,
    position_tolerance_m: float = 5.0,
    max_sigma_m: float = 5.0,
) -> ConfirmationResult:
    records = [_as_observation(x) for x in observations]
    if any(
        v.identity_vote is False or str(v.identity_vote).lower() in {"reject", "negative", "false"}
        for v in records
    ):
        return ConfirmationResult("reject", "identity_vote_negative", 0, 0)
    positives = [v for v in records if bool(v.identity_vote)]
    if any(v.triangulation_sigma_m is None for v in positives):
        return ConfirmationResult(
            "need_more_passes", "triangulation_sigma_missing", len(positives), 0
        )
    if any(
        v.triangulation_sigma_m is not None
        and (
            not math.isfinite(float(v.triangulation_sigma_m))
            or float(v.triangulation_sigma_m) > max_sigma_m
        )
        for v in positives
    ):
        return ConfirmationResult("reject", "triangulation_sigma_exceeded", len(positives), 0)
    headings = [
        float(v.heading_rad)
        for v in positives
        if v.heading_rad is not None and math.isfinite(float(v.heading_rad))
    ]
    sep = math.radians(max(0.0, _f(min_heading_separation_deg)))
    distinct = []
    for heading in headings:
        if all(abs(_wrap(heading - old)) >= sep - 1e-9 for old in distinct):
            distinct.append(heading)
    if len(distinct) < min_passes:
        return ConfirmationResult(
            "need_more_passes",
            "different_heading_passes_insufficient",
            len(positives),
            len(distinct),
        )
    positions = [v.ground_position_ned for v in positives if v.ground_position_ned is not None]
    if len(positions) < min_passes:
        return ConfirmationResult(
            "need_more_passes", "ground_projection_missing", len(positives), len(distinct)
        )
    spread = max(math.dist(a, b) for i, a in enumerate(positions) for b in positions[i + 1 :])
    if spread > max(0.0, _f(position_tolerance_m)):
        return ConfirmationResult(
            "reject", "ground_projection_inconsistent", len(positives), len(distinct), spread
        )
    sigmas = [
        float(v.triangulation_sigma_m) for v in positives if v.triangulation_sigma_m is not None
    ]
    return ConfirmationResult(
        "confirm",
        "passes_geometry_consistent",
        len(positives),
        len(distinct),
        spread,
        max(sigmas) if sigmas else None,
    )


def thinking_loiter(
    current_ned_heading: tuple[Sequence[float], float],
    airspeed_mps: float,
    min_turn_radius_m: float,
    nav_loiter_radius_m: float,
    segment_clear: Callable[[tuple[float, float, float], tuple[float, float, float]], bool],
    *,
    sample_step_m: float = 5.0,
    points: int = 72,
) -> LoiterPlan:
    pos = (
        _f(current_ned_heading[0][0]),
        _f(current_ned_heading[0][1]),
        _f(current_ned_heading[0][2]) if len(current_ned_heading[0]) > 2 else 0.0,
    )
    yaw = _f(current_ned_heading[1])
    radius = max(abs(_f(min_turn_radius_m, 1.0)), abs(_f(nav_loiter_radius_m, 1.0)), EPS)
    for side, sign in (("left", 1.0), ("right", -1.0)):
        center = (
            pos[0] - sign * radius * math.sin(yaw),
            pos[1] + sign * radius * math.cos(yaw),
            pos[2],
        )
        count = max(16, int(points))
        way = tuple(
            PathPoint(
                center[0] + radius * math.cos(yaw - sign * math.pi / 2 + sign * PI2 * i / count),
                center[1] + radius * math.sin(yaw - sign * math.pi / 2 + sign * PI2 * i / count),
                yaw + sign * PI2 * i / count,
                radius * PI2 * i / count,
            )
            for i in range(count + 1)
        )
        clear = bool(segment_clear(pos, center)) and all(
            segment_clear((a.north, a.east, pos[2]), (b.north, b.east, pos[2]))
            for a, b in zip(way, way[1:], strict=False)
        )
        if clear:
            return LoiterPlan(center, radius, side, way, pos, True)
    center = (pos[0], pos[1], pos[2])
    return LoiterPlan(center, radius, "none", (PathPoint(*pos[:2], yaw, 0.0),), pos, False)


# Readable aliases for callers that prefer verb-noun names.
plan_dubins_path = dubins_path
plan_lawnmower_search = lawnmower_search
plan_observation_orbit = observation_orbit
plan_fly_by = fly_by_passes
confirm_fixed_wing_passes = evaluate_fixed_wing_confirmation
plan_thinking_loiter = thinking_loiter
generate_dubins_path = dubins_path
check_curvature = curvature_within_limit
generate_lawnmower = lawnmower_search
plan_rounded_square = rounded_square_path
plan_orbit = observation_orbit
plan_flyby = fly_by_passes
evaluate_confirmation = evaluate_fixed_wing_confirmation
generate_loiter = thinking_loiter
curvature_check = curvature_within_limit
search_pattern = lawnmower_search
coverage_fraction = estimate_coverage
orbit_geometry = observation_orbit
flyby_plan = fly_by_passes
confirmation_decision = evaluate_fixed_wing_confirmation
loiter_plan = thinking_loiter


__all__ = [name for name in globals() if not name.startswith("_")]
