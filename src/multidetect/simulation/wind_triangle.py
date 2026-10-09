"""Steady, level-flight wind triangle for offline model comparisons.

Wind components are velocities *towards* north/east, not meteorological
wind-from bearings. This is not a flight controller or a mission admission
rule: acceleration, turns, gusts and energy are outside this model.
"""

import math
from dataclasses import dataclass
from numbers import Real


@dataclass(frozen=True)
class WindTriangle:
    feasible: bool
    reason: str
    heading_deg: float | None
    along_track_speed_mps: float | None
    wind_along_mps: float
    wind_cross_mps: float


def solve_wind_triangle(
    *,
    track_deg: float,
    airspeed_mps: float,
    wind_north_mps: float,
    wind_east_mps: float,
    minimum_progress_mps: float = 0.0,
) -> WindTriangle:
    """Find the forward air-velocity branch that cancels cross-track wind.

    A feasible result requires ground progress strictly above the specified
    minimum. No speed floor is substituted for a physically infeasible leg.
    Exact crosswind equality is allowed if tailwind supplies positive progress;
    such a boundary solution has no gust margin and is not flight approval.
    """
    values = {
        "track_deg": track_deg,
        "airspeed_mps": airspeed_mps,
        "wind_north_mps": wind_north_mps,
        "wind_east_mps": wind_east_mps,
        "minimum_progress_mps": minimum_progress_mps,
    }
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite real number")
    if airspeed_mps <= 0 or minimum_progress_mps < 0:
        raise ValueError("airspeed must be positive and minimum progress nonnegative")
    normalized_track = track_deg % 360
    track = math.radians(normalized_track)
    north, east = math.cos(track), math.sin(track)
    along = wind_north_mps * north + wind_east_mps * east
    cross = wind_east_mps * north - wind_north_mps * east
    if not math.isfinite(along) or not math.isfinite(cross):
        raise ValueError("wind projection overflow")
    if abs(cross) > airspeed_mps:
        return WindTriangle(False, "crosswind_exceeds_airspeed", None, None, along, cross)
    ratio = cross / airspeed_mps
    forward_air = airspeed_mps * math.sqrt(max(0.0, (1 - ratio) * (1 + ratio)))
    progress = forward_air + along
    if not math.isfinite(progress):
        raise ValueError("ground speed overflow")
    if progress <= minimum_progress_mps:
        return WindTriangle(False, "insufficient_forward_progress", None, None, along, cross)
    heading = (normalized_track - math.degrees(math.asin(ratio))) % 360
    return WindTriangle(True, "steady_track_solution", heading, progress, along, cross)
