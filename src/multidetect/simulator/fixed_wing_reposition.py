"""Telemetry checks for sustained fixed-wing loiter capture."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from typing import Any

DEFAULT_NAV_LOITER_RAD_M = 80.0


def reposition_ring_metrics(
    observations: Iterable[Mapping[str, Any]],
    *,
    target_ned: tuple[float, float, float] | list[float],
    radius_m: float,
    tolerance_m: float = 8.0,
    minimum_dwell_s: float = 30.0,
    minimum_swept_deg: float = 360.0,
) -> dict[str, Any]:
    """Measure the best continuous FW interval captured on the target loiter ring.

    Each observation has ``sim_time_s``, ``x``, ``y`` and ``fw_active`` fields.
    Invalid/off-ring/non-FW observations reset the candidate interval.
    """
    target_n, target_e = float(target_ned[0]), float(target_ned[1])
    run_errors: list[float] = []
    best_errors: list[float] = []
    run_dwell = 0.0
    run_swept = 0.0
    best_dwell = 0.0
    best_swept = 0.0
    previous_time: float | None = None
    previous_angle: float | None = None
    for row in observations:
        try:
            sim_time = float(row["sim_time_s"])
            north, east = float(row["x"]), float(row["y"])
            fw_active = bool(row["fw_active"])
        except (KeyError, TypeError, ValueError):
            fw_active = False
            sim_time = north = east = float("nan")
        distance = math.hypot(north - target_n, east - target_e)
        error = abs(distance - float(radius_m))
        valid = (
            fw_active
            and math.isfinite(sim_time)
            and math.isfinite(error)
            and error <= tolerance_m
        )
        angle = math.atan2(east - target_e, north - target_n) if valid else None
        if (
            valid
            and previous_time is not None
            and previous_angle is not None
            and sim_time > previous_time
        ):
            dt = sim_time - previous_time
            # Ignore telemetry gaps: they cannot prove continuous capture or sweep.
            if dt <= 1.0:
                run_dwell += dt
                delta = (angle - previous_angle + math.pi) % (2.0 * math.pi) - math.pi
                run_swept += abs(math.degrees(delta))
                run_errors.append(error)
            else:
                run_errors = []
                run_dwell = run_swept = 0.0
        elif not valid:
            run_errors = []
            run_dwell = run_swept = 0.0
        previous_time = sim_time if valid else None
        previous_angle = angle
        if run_dwell > best_dwell or (run_dwell == best_dwell and run_swept > best_swept):
            best_dwell, best_swept, best_errors = run_dwell, run_swept, list(run_errors)
    error_p95 = (
        float(statistics.quantiles(best_errors, n=20, method="inclusive")[18])
        if len(best_errors) >= 2
        else (best_errors[0] if best_errors else None)
    )
    return {
        "fw_reposition_ring_error_p95_m": error_p95,
        "fw_reposition_dwell_s": best_dwell,
        "fw_reposition_swept_deg": best_swept,
        "fw_reposition_success": bool(
            best_dwell >= minimum_dwell_s and best_swept >= minimum_swept_deg
        ),
    }
