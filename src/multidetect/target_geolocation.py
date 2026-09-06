"""Conservative WGS-84 target geolocation from qualified aircraft GPS and NED offsets.

The public API deliberately stays small: callers provide a qualified aircraft
fix and a target offset in the local North/East frame.  The projection itself
uses the WGS-84 ellipsoid direct geodesic instead of treating latitude and
longitude as points on a sphere.  This matters for outdoor work because the
metres-per-degree scale changes with latitude and the north/east axes are not
the same length everywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .mavlink_gps import GNSS_CANONICAL_MAX_GPS_AGE_S

WGS84_SEMI_MAJOR_AXIS_M = 6_378_137.0
WGS84_FLATTENING = 1.0 / 298.257223563
WGS84_SEMI_MINOR_AXIS_M = WGS84_SEMI_MAJOR_AXIS_M * (1.0 - WGS84_FLATTENING)

# Kept as a compatibility alias for integrations that imported the old name.
# New code should use the ellipsoid constants or ``wgs84_radii_of_curvature``.
WGS84_LOCAL_RADIUS_M = WGS84_SEMI_MAJOR_AXIS_M

_WGS84_MAX_DIRECT_DISTANCE_M = math.pi * WGS84_SEMI_MINOR_AXIS_M
_GEODESIC_CONVERGENCE_TOLERANCE = 1.0e-13
_GEODESIC_MAX_ITERATIONS = 100
_DEFAULT_MOTION_VELOCITY_SIGMA_MPS = 0.5
_MOTION_COMPENSATION_EPSILON_S = 1.0e-9


@dataclass(frozen=True, slots=True)
class TargetGeolocation:
    """Target coordinates and a conservative one-sigma horizontal uncertainty."""

    latitude_deg: float
    longitude_deg: float
    horizontal_sigma_m: float

    def __post_init__(self) -> None:
        # Exact poles do not have a stable local longitude direction.  Reject
        # them here so every WGS-84 target result matches the QGC map domain.
        if not -90.0 < self.latitude_deg < 90.0:
            raise ValueError("target latitude is outside the WGS-84 domain")
        if not -180.0 <= self.longitude_deg <= 180.0:
            raise ValueError("target longitude is outside the WGS-84 domain")
        if not math.isfinite(self.horizontal_sigma_m) or self.horizontal_sigma_m < 0.0:
            raise ValueError("target horizontal uncertainty is invalid")


def target_geolocation_from_ned_offset(
    *,
    aircraft_latitude_deg: float,
    aircraft_longitude_deg: float,
    north_offset_m: float,
    east_offset_m: float,
    aircraft_horizontal_sigma_m: float,
    ground_range_ci95_m: tuple[float, float] | None,
    ground_range_m: float | None,
    bearing_sigma_deg: float | None,
    aircraft_observed_at_s: float | None = None,
    target_captured_at_s: float | None = None,
    aircraft_velocity_north_mps: float | None = None,
    aircraft_velocity_east_mps: float | None = None,
    aircraft_velocity_observed_at_s: float | None = None,
    velocity_uncertainty_mps: float | None = None,
    acceleration_uncertainty_mps2: float | None = None,
    maximum_motion_compensation_s: float = GNSS_CANONICAL_MAX_GPS_AGE_S,
) -> TargetGeolocation:
    """Project a NED target offset to WGS-84 and propagate horizontal error.

    The target offset is interpreted as a surface displacement from the
    aircraft fix.  A Vincenty WGS-84 direct solve preserves the local N/E
    bearing while accounting for ellipsoid curvature, so the same function is
    valid for short outdoor detections and longer survey/replay fixtures.  The
    result combines aircraft GPS EPH, range CI and bearing error so callers do
    not mistake a coordinate for a surveyed point.  When the optional GPS and
    target timestamps plus a complete N/E velocity are supplied, the aircraft
    reference is propagated on WGS-84 to the target-frame time before applying
    the offset.  Missing velocity keeps the legacy no-extrapolation path.  When
    a velocity observation timestamp is supplied, it must be close to both the
    GPS and target-frame timestamps; an old or future velocity sample is
    rejected instead of being silently reused for outdoor extrapolation.  An
    optional ``acceleration_uncertainty_mps2`` adds a one-sigma budget for
    unmodelled horizontal acceleration during that bounded interval.  The
    position mean remains constant-velocity, while the uncertainty uses
    ``0.5 * dt**2 * acceleration_uncertainty_mps2`` in quadrature with the
    velocity term.  Omitting the optional value preserves the historical
    constant-velocity uncertainty behavior.
    """

    values = (
        aircraft_latitude_deg,
        aircraft_longitude_deg,
        north_offset_m,
        east_offset_m,
        aircraft_horizontal_sigma_m,
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("target geolocation inputs must be finite")
    if not -90.0 < aircraft_latitude_deg < 90.0:
        raise ValueError("aircraft latitude is outside the supported tangent-plane domain")
    if not -180.0 <= aircraft_longitude_deg <= 180.0:
        raise ValueError("aircraft longitude is outside the WGS-84 domain")
    if aircraft_horizontal_sigma_m < 0.0:
        raise ValueError("aircraft horizontal uncertainty cannot be negative")

    if acceleration_uncertainty_mps2 is not None:
        if (
            isinstance(acceleration_uncertainty_mps2, bool)
            or not isinstance(acceleration_uncertainty_mps2, (int, float))
            or not math.isfinite(acceleration_uncertainty_mps2)
            or acceleration_uncertainty_mps2 < 0.0
        ):
            raise ValueError("acceleration uncertainty must be finite and non-negative")
        acceleration_sigma_mps2 = float(acceleration_uncertainty_mps2)
    else:
        acceleration_sigma_mps2 = None

    offset_distance_m = math.hypot(north_offset_m, east_offset_m)
    if offset_distance_m > _WGS84_MAX_DIRECT_DISTANCE_M:
        raise ValueError("target offset exceeds the WGS-84 geodesic domain")

    # A replay producer can carry a nominal ground range and an N/E offset
    # independently.  Do not let contradictory fields produce a plausible
    # point with an understated uncertainty circle.  The tolerance combines
    # the reported 1-sigma range spread with a small relative allowance for
    # rounded/quantised wire values; grossly inconsistent records fail closed.
    range_sigma_m = _range_sigma_m(ground_range_ci95_m)
    if ground_range_m is not None:
        if not math.isfinite(ground_range_m) or ground_range_m < 0.0:
            raise ValueError("ground range is invalid")
        if ground_range_ci95_m is not None:
            lower, upper = ground_range_ci95_m
            if not lower <= ground_range_m <= upper:
                raise ValueError("ground range is outside its confidence interval")
        tolerance_m = max(
            1.0,
            3.0 * range_sigma_m,
            0.10 * max(1.0, float(ground_range_m)),
        )
        if abs(offset_distance_m - float(ground_range_m)) > tolerance_m:
            raise ValueError("target offset and ground range are inconsistent")

    reference_latitude_deg = aircraft_latitude_deg
    reference_longitude_deg = aircraft_longitude_deg
    motion_sigma_m = 0.0
    motion_dt_s = _motion_time_delta_s(
        aircraft_observed_at_s=aircraft_observed_at_s,
        target_captured_at_s=target_captured_at_s,
        maximum_motion_compensation_s=maximum_motion_compensation_s,
    )
    _validate_velocity_observed_time(
        aircraft_velocity_observed_at_s=aircraft_velocity_observed_at_s,
        aircraft_observed_at_s=aircraft_observed_at_s,
        target_captured_at_s=target_captured_at_s,
        maximum_motion_compensation_s=maximum_motion_compensation_s,
        motion_dt_s=motion_dt_s,
    )
    velocity_inputs_present = any(
        value is not None
        for value in (
            aircraft_velocity_north_mps,
            aircraft_velocity_east_mps,
            aircraft_velocity_observed_at_s,
        )
    )
    if motion_dt_s is not None and abs(motion_dt_s) > _MOTION_COMPENSATION_EPSILON_S:
        velocity = _finite_velocity_pair(
            aircraft_velocity_north_mps,
            aircraft_velocity_east_mps,
        )
        if velocity is None and (velocity_inputs_present or acceleration_sigma_mps2 is not None):
            # A producer that advertises a motion sample must provide both
            # horizontal axes as finite values.  Silently falling back to the
            # old GPS point would publish a coordinate with no motion error
            # budget even though the frame and GPS timestamps are separated.
            # The same fail-closed rule applies when an acceleration envelope
            # is present without the velocity mean it is meant to bound: the
            # envelope must never be silently dropped from the evidence sigma.
            raise ValueError("motion compensation requires a complete finite N/E velocity pair")
        if velocity is not None:
            reference_latitude_deg, reference_longitude_deg = wgs84_forward_from_ned(
                latitude_deg=aircraft_latitude_deg,
                longitude_deg=aircraft_longitude_deg,
                north_offset_m=velocity[0] * motion_dt_s,
                east_offset_m=velocity[1] * motion_dt_s,
            )
            if velocity_uncertainty_mps is None:
                velocity_sigma_mps = _DEFAULT_MOTION_VELOCITY_SIGMA_MPS
            else:
                if (
                    isinstance(velocity_uncertainty_mps, bool)
                    or not isinstance(velocity_uncertainty_mps, (int, float))
                    or not math.isfinite(velocity_uncertainty_mps)
                    or velocity_uncertainty_mps < 0.0
                ):
                    raise ValueError("velocity uncertainty must be finite and non-negative")
                velocity_sigma_mps = float(velocity_uncertainty_mps)
            velocity_sigma_m = abs(motion_dt_s) * velocity_sigma_mps
            acceleration_sigma_m = (
                0.5 * motion_dt_s**2 * acceleration_sigma_mps2
                if acceleration_sigma_mps2 is not None
                else 0.0
            )
            motion_sigma_m = math.hypot(velocity_sigma_m, acceleration_sigma_m)

    latitude_deg, longitude_deg = wgs84_forward_from_ned(
        latitude_deg=reference_latitude_deg,
        longitude_deg=reference_longitude_deg,
        north_offset_m=north_offset_m,
        east_offset_m=east_offset_m,
    )

    bearing_sigma_m = _bearing_sigma_m(ground_range_m, bearing_sigma_deg)
    return TargetGeolocation(
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        horizontal_sigma_m=math.hypot(
            aircraft_horizontal_sigma_m,
            range_sigma_m,
            bearing_sigma_m,
            motion_sigma_m,
        ),
    )


def _motion_time_delta_s(
    *,
    aircraft_observed_at_s: float | None,
    target_captured_at_s: float | None,
    maximum_motion_compensation_s: float,
) -> float | None:
    """Return target-frame minus GPS-observation time when both are supplied.

    Timestamp alignment is optional for backwards compatibility with older
    replay records.  If either timestamp is omitted, the caller keeps the
    original reference point and no velocity extrapolation is attempted.
    When both are present, malformed values and an interval outside the
    canonical GPS freshness horizon fail closed instead of silently shifting a
    target by an unbounded amount.
    """

    if aircraft_observed_at_s is None and target_captured_at_s is None:
        return None
    if aircraft_observed_at_s is None or target_captured_at_s is None:
        raise ValueError("motion compensation requires both observation timestamps")
    if (
        isinstance(aircraft_observed_at_s, bool)
        or isinstance(target_captured_at_s, bool)
        or not isinstance(aircraft_observed_at_s, (int, float))
        or not isinstance(target_captured_at_s, (int, float))
        or not math.isfinite(aircraft_observed_at_s)
        or not math.isfinite(target_captured_at_s)
        or aircraft_observed_at_s < 0.0
        or target_captured_at_s < 0.0
    ):
        raise ValueError("motion compensation timestamps must be finite and non-negative")
    if (
        isinstance(maximum_motion_compensation_s, bool)
        or not isinstance(maximum_motion_compensation_s, (int, float))
        or not math.isfinite(maximum_motion_compensation_s)
        or maximum_motion_compensation_s <= 0.0
    ):
        raise ValueError("maximum motion compensation interval must be finite and positive")
    delta_s = float(target_captured_at_s) - float(aircraft_observed_at_s)
    if abs(delta_s) > float(maximum_motion_compensation_s):
        raise ValueError("motion compensation interval exceeds the allowed horizon")
    return delta_s


def _validate_velocity_observed_time(
    *,
    aircraft_velocity_observed_at_s: float | None,
    aircraft_observed_at_s: float | None,
    target_captured_at_s: float | None,
    maximum_motion_compensation_s: float,
    motion_dt_s: float | None,
) -> float | None:
    """Validate optional velocity timing against the GPS/frame time pair.

    MAVLink velocity and GPS samples arrive on separate message paths.  A
    A complete finite N/E pair without its observation time is retained for
    backwards compatibility when callers use the low-level projector directly,
    but a supplied timestamp is never allowed to drift outside the bounded
    motion horizon.  A partial/non-finite velocity sample is rejected when the
    GPS and target timestamps require motion compensation; it must not be
    silently treated as a zero-motion observation.  The live/status producers
    only forward this field when it is present in the telemetry snapshot.
    """

    if aircraft_velocity_observed_at_s is None:
        return None
    if motion_dt_s is None or aircraft_observed_at_s is None or target_captured_at_s is None:
        raise ValueError("velocity observation timestamp requires motion timestamps")
    if (
        isinstance(aircraft_velocity_observed_at_s, bool)
        or not isinstance(aircraft_velocity_observed_at_s, (int, float))
        or not math.isfinite(aircraft_velocity_observed_at_s)
        or aircraft_velocity_observed_at_s < 0.0
    ):
        raise ValueError("velocity observation timestamp must be finite and non-negative")
    velocity_time_s = float(aircraft_velocity_observed_at_s)
    maximum_interval_s = float(maximum_motion_compensation_s)
    if (
        abs(velocity_time_s - float(aircraft_observed_at_s)) > maximum_interval_s
        or abs(velocity_time_s - float(target_captured_at_s)) > maximum_interval_s
    ):
        raise ValueError("velocity observation is outside the motion compensation horizon")
    return velocity_time_s


def _finite_velocity_pair(
    north_mps: float | None,
    east_mps: float | None,
) -> tuple[float, float] | None:
    """Return a complete finite N/E velocity pair, or no compensation input."""

    values = (north_mps, east_mps)
    if any(value is None for value in values):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in values
    ):
        return None
    return float(north_mps), float(east_mps)


def _range_sigma_m(interval: tuple[float, float] | None) -> float:
    if interval is None:
        return 0.0
    lower, upper = interval
    if not all(math.isfinite(value) for value in interval) or lower < 0.0 or upper < lower:
        raise ValueError("ground range confidence interval is invalid")
    return (upper - lower) / (2.0 * 1.96)


def _bearing_sigma_m(ground_range_m: float | None, bearing_sigma_deg: float | None) -> float:
    if ground_range_m is None and bearing_sigma_deg is None:
        return 0.0
    if ground_range_m is None or bearing_sigma_deg is None:
        raise ValueError("ground range and bearing uncertainty must be supplied together")
    if not math.isfinite(ground_range_m) or ground_range_m < 0.0:
        raise ValueError("ground range is invalid")
    if not math.isfinite(bearing_sigma_deg) or not 0.0 <= bearing_sigma_deg <= 90.0:
        raise ValueError("bearing uncertainty must be within [0, 90] degrees")
    # ``bearing_sigma_deg`` is a one-sigma angular standard deviation.  Its
    # tangential displacement is therefore the arc length ``r * sigma_rad``;
    # using the chord ``r * sin(sigma_rad)`` underestimates the map uncertainty
    # as the camera moves off-axis or the bearing gate is intentionally wide.
    return ground_range_m * math.radians(bearing_sigma_deg)


def wgs84_radii_of_curvature(latitude_deg: float) -> tuple[float, float]:
    """Return the WGS-84 meridional (M) and prime-vertical (N) radii in metres."""

    if not math.isfinite(latitude_deg) or not -90.0 < latitude_deg < 90.0:
        raise ValueError("latitude must be finite and strictly inside the WGS-84 domain")
    latitude_rad = math.radians(latitude_deg)
    eccentricity_squared = WGS84_FLATTENING * (2.0 - WGS84_FLATTENING)
    denominator = math.sqrt(1.0 - eccentricity_squared * math.sin(latitude_rad) ** 2)
    prime_vertical_m = WGS84_SEMI_MAJOR_AXIS_M / denominator
    meridional_m = WGS84_SEMI_MAJOR_AXIS_M * (1.0 - eccentricity_squared) / denominator**3
    return meridional_m, prime_vertical_m


def wgs84_forward_from_ned(
    *,
    latitude_deg: float,
    longitude_deg: float,
    north_offset_m: float,
    east_offset_m: float,
) -> tuple[float, float]:
    """Project a local N/E displacement to a WGS-84 coordinate.

    This is the shared geodetic primitive for both target evidence and the
    deterministic SIM flight model.  Keeping validation at this public
    boundary prevents a simulation-only caller from accidentally bypassing
    the same finite/range/pole/direct-distance checks used by REAL target
    geolocation.
    """

    values = (latitude_deg, longitude_deg, north_offset_m, east_offset_m)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("WGS-84 forward inputs must be finite")
    if not -90.0 < latitude_deg < 90.0:
        raise ValueError("latitude is outside the supported tangent-plane domain")
    if not -180.0 <= longitude_deg <= 180.0:
        raise ValueError("longitude is outside the WGS-84 domain")
    distance_m = math.hypot(north_offset_m, east_offset_m)
    if distance_m > _WGS84_MAX_DIRECT_DISTANCE_M:
        raise ValueError("N/E offset exceeds the WGS-84 geodesic domain")
    # A direct geodesic can legitimately return a wrapped latitude after
    # crossing a pole.  That wrapped point is not a valid local N/E result:
    # the tangent-plane longitude axis flips at the pole.  Reject the path
    # before solving it, using the meridional distance from the input fix to
    # the nearest pole as a conservative local guard.
    meridional_m, _ = wgs84_radii_of_curvature(latitude_deg)
    distance_to_pole_m = math.radians(90.0 - abs(latitude_deg)) * meridional_m
    if abs(north_offset_m) >= distance_to_pole_m:
        raise ValueError("N/E offset crosses a geographic pole")
    return _wgs84_forward_from_ned(
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        north_offset_m=north_offset_m,
        east_offset_m=east_offset_m,
    )


def _wgs84_forward_from_ned(
    *,
    latitude_deg: float,
    longitude_deg: float,
    north_offset_m: float,
    east_offset_m: float,
) -> tuple[float, float]:
    """Solve a WGS-84 direct geodesic from a local N/E displacement.

    Vincenty's direct equations converge rapidly for the sub-kilometre to
    kilometre-scale offsets produced by the ranging pipeline.  The explicit
    iteration bound keeps malformed replay data fail-closed instead of
    silently returning a spherical approximation.
    """

    distance_m = math.hypot(north_offset_m, east_offset_m)
    if distance_m == 0.0:
        return latitude_deg, longitude_deg

    azimuth_rad = math.atan2(east_offset_m, north_offset_m)
    latitude1_rad = math.radians(latitude_deg)
    longitude1_rad = math.radians(longitude_deg)
    semi_major_m = WGS84_SEMI_MAJOR_AXIS_M
    semi_minor_m = WGS84_SEMI_MINOR_AXIS_M
    flattening = WGS84_FLATTENING

    reduced_latitude_rad = math.atan((1.0 - flattening) * math.tan(latitude1_rad))
    sin_reduced = math.sin(reduced_latitude_rad)
    cos_reduced = math.cos(reduced_latitude_rad)
    sin_azimuth = math.sin(azimuth_rad)
    cos_azimuth = math.cos(azimuth_rad)
    sin_alpha = cos_reduced * sin_azimuth
    cos_squared_alpha = 1.0 - sin_alpha**2
    u_squared = cos_squared_alpha * (semi_major_m**2 - semi_minor_m**2) / semi_minor_m**2
    coefficient_a = 1.0 + (u_squared / 16384.0) * (
        4096.0 + u_squared * (-768.0 + u_squared * (320.0 - 175.0 * u_squared))
    )
    coefficient_b = (u_squared / 1024.0) * (
        256.0 + u_squared * (-128.0 + u_squared * (74.0 - 47.0 * u_squared))
    )
    sigma1_rad = math.atan2(math.tan(reduced_latitude_rad), cos_azimuth)
    sigma_rad = distance_m / (semi_minor_m * coefficient_a)

    for _ in range(_GEODESIC_MAX_ITERATIONS):
        two_sigma_m_rad = 2.0 * sigma1_rad + sigma_rad
        sine_sigma = math.sin(sigma_rad)
        cosine_sigma = math.cos(sigma_rad)
        cosine_two_sigma_m = math.cos(two_sigma_m_rad)
        delta_sigma_rad = (
            coefficient_b
            * sine_sigma
            * (
                cosine_two_sigma_m
                + (coefficient_b / 4.0)
                * (
                    cosine_sigma * (-1.0 + 2.0 * cosine_two_sigma_m**2)
                    - (coefficient_b / 6.0)
                    * cosine_two_sigma_m
                    * (-3.0 + 4.0 * sine_sigma**2)
                    * (-3.0 + 4.0 * cosine_two_sigma_m**2)
                )
            )
        )
        next_sigma_rad = distance_m / (semi_minor_m * coefficient_a) + delta_sigma_rad
        if abs(next_sigma_rad - sigma_rad) <= _GEODESIC_CONVERGENCE_TOLERANCE:
            sigma_rad = next_sigma_rad
            break
        sigma_rad = next_sigma_rad
    else:
        raise ValueError("WGS-84 geodesic did not converge")

    sine_sigma = math.sin(sigma_rad)
    cosine_sigma = math.cos(sigma_rad)
    cosine_two_sigma_m = math.cos(2.0 * sigma1_rad + sigma_rad)
    temporary = sin_reduced * sine_sigma - cos_reduced * cosine_sigma * cos_azimuth
    latitude2_rad = math.atan2(
        sin_reduced * cosine_sigma + cos_reduced * sine_sigma * cos_azimuth,
        (1.0 - flattening) * math.sqrt(sin_alpha**2 + temporary**2),
    )
    longitude_delta_rad = math.atan2(
        sine_sigma * sin_azimuth,
        cos_reduced * cosine_sigma - sin_reduced * sine_sigma * cos_azimuth,
    )
    correction = (
        flattening / 16.0 * cos_squared_alpha * (4.0 + flattening * (4.0 - 3.0 * cos_squared_alpha))
    )
    longitude_delta_rad -= (
        (1.0 - correction)
        * flattening
        * sin_alpha
        * (
            sigma_rad
            + correction
            * sine_sigma
            * (
                cosine_two_sigma_m
                + correction * cosine_sigma * (-1.0 + 2.0 * cosine_two_sigma_m**2)
            )
        )
    )
    latitude2_deg = math.degrees(latitude2_rad)
    if not -90.0 < latitude2_deg < 90.0:
        raise ValueError("target offset crosses a geographic pole")
    longitude2_deg = math.degrees(longitude1_rad + longitude_delta_rad)
    return latitude2_deg, ((longitude2_deg + 180.0) % 360.0) - 180.0


__all__ = [
    "TargetGeolocation",
    "WGS84_FLATTENING",
    "WGS84_LOCAL_RADIUS_M",
    "WGS84_SEMI_MAJOR_AXIS_M",
    "WGS84_SEMI_MINOR_AXIS_M",
    "target_geolocation_from_ned_offset",
    "wgs84_forward_from_ned",
    "wgs84_radii_of_curvature",
]
