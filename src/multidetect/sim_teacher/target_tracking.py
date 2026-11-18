"""Offline C11 target geometry: NED/FRD, +X optical axis, AMSL heights.

No numeric optimizer or runtime dependency is needed. Reachability is an exact
one-dimensional twist-family interval test; closest pointing uses the stationary
points and edges of the three joint-stop surfaces.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from multidetect.camera_mount import _mat_mul, _mat_transpose, _mat_vec
from multidetect.target_geolocation import WGS84_FLATTENING, WGS84_SEMI_MAJOR_AXIS_M

AXES = ("roll", "pitch", "yaw")


def track_cli_argv(argv):
    """Allow argparse's space form for targets with a negative latitude."""
    args = list(argv)
    normalized = []
    index = 0
    while index < len(args):
        value = args[index]
        if (
            value == "--track-target"
            and index + 1 < len(args)
            and args[index + 1].startswith("-")
            and "," in args[index + 1]
        ):
            normalized.append(value + "=" + args[index + 1])
            index += 2
        else:
            normalized.append(value)
            index += 1
    return normalized


def unit(vector):
    if len(vector) != 3 or not all(math.isfinite(v) for v in vector):
        raise ValueError("a finite three-component direction is required")
    length = math.hypot(*vector)
    if length < 1e-10:
        raise ValueError("target coincides with camera origin")
    return tuple(v / length for v in vector)


def pointing_error_deg(direction, rotation):
    target, optical = unit(direction), unit(rotation[0::3])
    cross = (
        target[1] * optical[2] - target[2] * optical[1],
        target[2] * optical[0] - target[0] * optical[2],
        target[0] * optical[1] - target[1] * optical[0],
    )
    return math.degrees(
        math.atan2(math.hypot(*cross), sum(a * b for a, b in zip(target, optical, strict=True)))
    )


def track_horizon_error_deg(rotation):
    """Image roll relative to earth down; undefined at the vertical optical axis."""
    if math.hypot(rotation[0], rotation[3]) < 1e-8:
        return None
    return abs(math.degrees(math.atan2(rotation[7], rotation[8])))


@dataclass(frozen=True)
class WorldTarget:
    lat_deg: float
    lon_deg: float
    alt_m: float

    def __post_init__(self):
        if (
            not all(math.isfinite(v) for v in (self.lat_deg, self.lon_deg, self.alt_m))
            or not -90 <= self.lat_deg <= 90
            or not -180 <= self.lon_deg <= 180
        ):
            raise ValueError("target must be finite LAT,LON,ALT_M (AMSL)")

    @classmethod
    def parse(cls, value):
        try:
            return cls(*map(float, value.split(",")))
        except (ValueError, TypeError) as exc:
            raise ValueError("expected LAT,LON,ALT_M (AMSL)") from exc

    def reference(self):
        return {"kind": "world_point", **asdict(self), "altitude_reference": "AMSL"}


def world_target_from_mavlink(message):
    def field(name):
        return message.get(name) if isinstance(message, Mapping) else getattr(message, name, None)

    try:
        return WorldTarget(field("lat") / 1e7, field("lon") / 1e7, field("alt") / 1000)
    except (ValueError, TypeError):
        return None


def world_offset_ned(origin, target):
    """WGS84 ECEF displacement in the vehicle's local tangent NED axes.

    The input and target heights share GLOBAL_POSITION_INT.alt's AMSL datum.
    A common local geoid offset cancels; long ranges need a geoid model.
    """

    def ecef(point):
        lat, lon = map(math.radians, (point.lat_deg, point.lon_deg))
        e2 = WGS84_FLATTENING * (2 - WGS84_FLATTENING)
        n = WGS84_SEMI_MAJOR_AXIS_M / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        return (
            (n + point.alt_m) * math.cos(lat) * math.cos(lon),
            (n + point.alt_m) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + point.alt_m) * math.sin(lat),
        )

    x, y, z = (b - a for a, b in zip(ecef(origin), ecef(target), strict=True))
    lat, lon = map(math.radians, (origin.lat_deg, origin.lon_deg))
    return (
        -math.sin(lat) * math.cos(lon) * x - math.sin(lat) * math.sin(lon) * y + math.cos(lat) * z,
        -math.sin(lon) * x + math.cos(lon) * y,
        -math.cos(lat) * math.cos(lon) * x - math.cos(lat) * math.sin(lon) * y - math.sin(lat) * z,
    )


def world_target_direction(
    origin, target, body_rotation=None, *, camera_offset_ned=(0.0, 0.0, 0.0)
):
    ray = unit(
        tuple(
            v - o for v, o in zip(world_offset_ned(origin, target), camera_offset_ned, strict=True)
        )
    )
    return _mat_vec(_mat_transpose(body_rotation), ray) if body_rotation is not None else ray


def pixel_roi_ray(intrinsics, *, pixel=None, roi_xyxy=None):
    """Reserved ROI adapter: calibrated pinhole pixel -> unit +X FRD ray.

    Exactly one pixel (u,v) or ROI (left,top,right,bottom) must be supplied.
    Distorted inputs must first be undistorted by the future image tracker.
    This adapter does not choose targets or read images.
    """
    if (pixel is None) == (roi_xyxy is None):
        raise ValueError("provide exactly one pixel or ROI")
    try:
        fx, fy, cx, cy = (float(intrinsics[k + "_px"]) for k in ("fx", "fy", "cx", "cy"))
        if not all(map(math.isfinite, (fx, fy, cx, cy))) or min(fx, fy) <= 0:
            raise ValueError("invalid intrinsics")
        if roi_xyxy is not None:
            left, top, right, bottom = roi_xyxy
            if not all(map(math.isfinite, roi_xyxy)) or right <= left or bottom <= top:
                raise ValueError("invalid ROI")
            pixel = ((left + right) / 2, (top + bottom) / 2)
        u, v = pixel
        return unit((1.0, (u - cx) / fx, (v - cy) / fy))
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid intrinsics, pixel or ROI") from exc


def level_target_rotation(direction):
    x = unit(direction)
    h = math.hypot(x[0], x[1])
    y = (-x[1] / h, x[0] / h, 0.0) if h > 1e-10 else (0.0, 1.0, 0.0)
    z = (x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0])
    return tuple(v for row in zip(x, y, z, strict=True) for v in row)


def _windings(value, bounds):
    low, high = bounds
    return [
        min(high, max(low, value + 360 * k))
        for k in range(
            math.ceil((low - value - 1e-8) / 360), math.floor((high - value + 1e-8) / 360) + 1
        )
    ]


def _exact_pointing(body_rotation, direction, previous, limits):
    # Local import avoids the stabilizer importing itself at module load time.
    from .horizon_stabilization import c11_rotation_deg

    desired = level_target_rotation(direction)
    relative = _mat_mul(_mat_transpose(body_rotation), desired)

    # relative * Rx(t): first column constant; columns 1/2 sinusoidal.
    def entry(row, col):
        if col == 0:
            return (0.0, 0.0, relative[3 * row])
        a, b = relative[3 * row + 1 : 3 * row + 3]
        return (a, b, 0.0) if col == 1 else (b, -a, 0.0)

    sr = entry(2, 1)
    sp = tuple(-v for v in entry(2, 0))
    cp = entry(2, 2)
    sy = tuple(-v for v in entry(0, 1))
    cy = entry(1, 1)
    cuts = {0.0, math.tau}

    def add_roots(coeff, value=0.0):
        a, b, c = coeff
        amplitude = math.hypot(a, b)
        if amplitude < 1e-14 or abs(value - c) > amplitude + 1e-13:
            return
        delta = math.acos(max(-1.0, min(1.0, (value - c) / amplitude)))
        phase = math.atan2(b, a)
        cuts.update(((phase + delta) % math.tau, (phase - delta) % math.tau))

    for bound in limits["roll"]:
        add_roots(sr, math.sin(math.radians(bound)))
    for axis, sine, cosine in (("pitch", sp, cp), ("yaw", sy, cy)):
        for bound in limits[axis]:
            a = math.radians(bound)
            add_roots(
                tuple(s * math.cos(a) - c * math.sin(a) for s, c in zip(sine, cosine, strict=True))
            )

    def candidate(t):
        c, s = math.cos(t), math.sin(t)
        a, b, d, e, f = (x * c + y * s + z for x, y, z in (sr, sp, cp, sy, cy))
        values = (
            math.degrees(math.asin(max(-1.0, min(1.0, a)))),
            math.degrees(math.atan2(b, d)),
            math.degrees(math.atan2(e, f)),
        )
        joints = {}
        for axis, value in zip(AXES, values, strict=True):
            choices = _windings(value, limits[axis])
            if not choices:
                return None
            joints[axis] = min(choices, key=lambda v: abs(v - previous[axis]))
        world = _mat_mul(body_rotation, c11_rotation_deg(joints))
        if pointing_error_deg(direction, world) > 1e-6:
            return None
        error = track_horizon_error_deg(world)
        return (
            error if error is not None else 0.0,
            sum((joints[a] - previous[a]) ** 2 for a in AXES),
            joints,
        )

    cuts = sorted(cuts)
    # On each feasible interval |twist| has its minimum at a boundary or zero.
    # Endpoints also retain tangent-only solutions at a joint stop.
    candidates = [c for t in cuts if (c := candidate(t)) is not None]
    for low, high in zip(cuts, cuts[1:], strict=False):
        middle = candidate((low + high) / 2)
        if middle is not None:
            candidates.append(middle)
    if not candidates:
        return None, desired
    error = min(c[0] for c in candidates)
    chosen = min((c for c in candidates if c[0] <= error + 1e-7), key=lambda c: c[1])
    return chosen[2], desired


def _nearest_pointing(body_direction, previous, limits):
    """Global projection onto C11's optical-direction workspace.

    For q=Rz(y) Rx(r) Ry(p) ex, qz=-cos(r) sin(p). At fixed yaw,
    t.q=A cos(p)+(ty sin(r)-tz cos(r)) sin(p), giving all stationary
    rolls analytically. On roll/pitch stop faces, interior optimal yaw aligns
    horizontal azimuth. Remaining extrema are exact pointing (already tested),
    cos(p)=0 on a roll face, sin(r)=0 on a pitch face, or their edges.
    """
    from .horizon_stabilization import c11_rotation_deg

    tx, ty, tz = body_direction
    candidates = []

    def add(r, p, y):
        joint = dict(zip(AXES, (r, p, y), strict=True))
        ray = c11_rotation_deg(joint)[0::3]
        score = sum(a * b for a, b in zip(body_direction, ray, strict=True))
        candidates.append((score, joint, ray))

    def best_yaw(r, p):
        rr, pp = map(math.radians, (r, p))
        angle = math.degrees(
            math.atan2(ty, tx) - math.atan2(math.sin(rr) * math.sin(pp), math.cos(pp))
        )
        yaws = list(limits["yaw"]) + _windings(angle, limits["yaw"])
        yaws.append(min(limits["yaw"][1], max(limits["yaw"][0], previous["yaw"])))
        for y in yaws:
            add(r, p, y)

    rolls = list(limits["roll"]) + _windings(0.0, limits["roll"])
    pitches = list(limits["pitch"])
    for angle in (-90.0, 90.0):
        pitches += _windings(angle, limits["pitch"])
    for r in rolls:
        for p in pitches:
            best_yaw(r, p)
    for y in limits["yaw"]:
        yy = math.radians(y)
        sx, sy = tx * math.cos(yy) + ty * math.sin(yy), -tx * math.sin(yy) + ty * math.cos(yy)
        stationary_roll = math.degrees(math.atan2(-sy, tz))
        for r in (
            list(limits["roll"])
            + _windings(stationary_roll, limits["roll"])
            + _windings(stationary_roll + 180, limits["roll"])
        ):
            rr = math.radians(r)
            optimal_pitch = math.degrees(math.atan2(sy * math.sin(rr) - tz * math.cos(rr), sx))
            for p in list(limits["pitch"]) + _windings(optimal_pitch, limits["pitch"]):
                add(r, p, y)
    score = max(c[0] for c in candidates)
    return [c for c in candidates if c[0] >= score - 1e-12]


def solve_track_target(body_rotation, direction, previous, limits):
    """Lexicographic pointing, horizon, then joint travel; rates are separate."""
    from .horizon_stabilization import c11_rotation_deg

    if not -90 < limits["roll"][0] <= limits["roll"][1] < 90:
        raise ValueError("track requires C11 roll stops strictly inside +/-90 degrees")
    direction = unit(direction)
    joint, desired = _exact_pointing(body_rotation, direction, previous, limits)
    reachable = joint is not None
    if joint is None:
        options = []
        body_direction = _mat_vec(_mat_transpose(body_rotation), direction)
        for _, nearest, ray in _nearest_pointing(body_direction, previous, limits):
            projected = _mat_vec(body_rotation, ray)
            level, _ = _exact_pointing(body_rotation, projected, previous, limits)
            candidate = level if level is not None else nearest
            world = _mat_mul(body_rotation, c11_rotation_deg(candidate))
            error = track_horizon_error_deg(world)
            options.append(
                (
                    error if error is not None else 0.0,
                    sum((candidate[a] - previous[a]) ** 2 for a in AXES),
                    candidate,
                )
            )
        best = min(c[0] for c in options)
        joint = min((c for c in options if c[0] <= best + 1e-7), key=lambda c: c[1])[2]
    world = _mat_mul(body_rotation, c11_rotation_deg(joint))
    horizon = track_horizon_error_deg(world)
    return (
        joint,
        desired,
        {
            "target_reachable": reachable,
            "horizon_feasible_given_target": (
                None if horizon is None else reachable and horizon <= 1e-6
            ),
            "target_pointing_min_error_deg": pointing_error_deg(direction, world),
            "track_feasibility_evidence": "twist_intervals_and_stop_surface_stationary_points",
        },
    )
