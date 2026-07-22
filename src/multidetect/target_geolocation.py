"""Conservative WGS-84 target geolocation from qualified aircraft GPS and NED offsets."""

from __future__ import annotations

import math
from dataclasses import dataclass

WGS84_LOCAL_RADIUS_M = 6_378_137.0


@dataclass(frozen=True, slots=True)
class TargetGeolocation:
    """Target coordinates and a conservative one-sigma horizontal uncertainty."""

    latitude_deg: float
    longitude_deg: float
    horizontal_sigma_m: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
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
) -> TargetGeolocation:
    """Project a short NED target offset to WGS-84 and propagate horizontal error.

    The pipeline is restricted to a few hundred metres, where a WGS-84 tangent
    plane is both more stable and more interpretable than treating latitude and
    longitude as independent linear units. The result combines aircraft GPS
    EPH, range CI and bearing error so callers do not mistake a coordinate for
    a surveyed point.
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

    latitude_rad = math.radians(aircraft_latitude_deg)
    latitude_deg = aircraft_latitude_deg + math.degrees(north_offset_m / WGS84_LOCAL_RADIUS_M)
    longitude_deg = aircraft_longitude_deg + math.degrees(
        east_offset_m / (WGS84_LOCAL_RADIUS_M * math.cos(latitude_rad))
    )
    if not -90.0 <= latitude_deg <= 90.0:
        raise ValueError("target offset crosses a geographic pole")
    longitude_deg = ((longitude_deg + 180.0) % 360.0) - 180.0

    range_sigma_m = _range_sigma_m(ground_range_ci95_m)
    bearing_sigma_m = _bearing_sigma_m(ground_range_m, bearing_sigma_deg)
    return TargetGeolocation(
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        horizontal_sigma_m=math.hypot(
            aircraft_horizontal_sigma_m,
            range_sigma_m,
            bearing_sigma_m,
        ),
    )


def _range_sigma_m(interval: tuple[float, float] | None) -> float:
    if interval is None:
        return 0.0
    lower, upper = interval
    if not all(math.isfinite(value) for value in interval) or lower < 0.0 or upper < lower:
        return 0.0
    return (upper - lower) / (2.0 * 1.96)


def _bearing_sigma_m(ground_range_m: float | None, bearing_sigma_deg: float | None) -> float:
    if (
        ground_range_m is None
        or bearing_sigma_deg is None
        or not math.isfinite(ground_range_m)
        or not math.isfinite(bearing_sigma_deg)
        or ground_range_m < 0.0
        or bearing_sigma_deg < 0.0
    ):
        return 0.0
    return ground_range_m * math.sin(math.radians(min(90.0, bearing_sigma_deg)))


__all__ = [
    "TargetGeolocation",
    "WGS84_LOCAL_RADIUS_M",
    "target_geolocation_from_ned_offset",
]
