"""Pure, causal step-spaced triangulation and uncertainty-aware position fusion.

All coordinates are local NED metres. Rays must already be unit vectors from
the calibrated camera model. Quality gates deliberately match the offline
evaluation: baseline and ray angle compare the latest view with each historical
view, rather than taking arbitrary pairs from an old part of the window.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, replace

Vector3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class ViewObservation:
    camera_ned: Vector3
    direction_ned: Vector3
    step: int
    timestamp_s: float


@dataclass(frozen=True, slots=True)
class PositionEstimate:
    position_ned: Vector3
    sigma_m: float
    timestamp_s: float
    source: str
    view_count: int = 0
    baseline_m: float = 0.0
    ray_angle_deg: float = 0.0
    residual_m: float = 0.0
    consistent: bool = True
    consistency_note: str | None = None


@dataclass(frozen=True, slots=True)
class RangeMeasurement:
    """Future direct/depth slant range input, independent of its implementation."""

    distance_m: float
    sigma_m: float
    timestamp_s: float
    source: str = "depth"


def _finite(value: float) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _vector(value: Vector3) -> bool:
    return isinstance(value, (tuple, list)) and len(value) == 3 and all(_finite(x) for x in value)


def _unit(value: Vector3) -> bool:
    return _vector(value) and abs(math.hypot(*value) - 1.0) <= 1e-6


def _estimate_valid(value: PositionEstimate | None) -> bool:
    return (
        isinstance(value, PositionEstimate)
        and _vector(value.position_ned)
        and _finite(value.sigma_m)
        and value.sigma_m > 0
        and _finite(value.timestamp_s)
        and isinstance(value.source, str)
        and bool(value.source)
    )


def triangulate_views(
    views: Iterable[ViewObservation],
    *,
    max_views: int = 8,
    sigma_scale: float = 2.5,
    min_baseline_m: float = 0.5,
    min_ray_angle_deg: float = 0.5,
    max_residual_m: float = 5.0,
) -> PositionEstimate | None:
    """Intersect the latest distinct steps; return None for every rejected case.

    Input order is acquisition order. A repeated step replaces its earlier
    sample so a burst or pure camera turn cannot impersonate several steps.
    Sigma is the offline residual/range/parallax propagation times sigma_scale.
    It is a scalar slant-range uncertainty, used conservatively for position
    fusion; it is not a complete 3-D covariance or a moving-target model.
    """
    if (
        isinstance(max_views, bool)
        or not isinstance(max_views, int)
        or max_views < 2
        or not all(
            _finite(x) and x > 0
            for x in (sigma_scale, min_baseline_m, min_ray_angle_deg, max_residual_m)
        )
    ):
        return None
    distinct: dict[int, ViewObservation] = {}
    for view in views:
        if (
            not isinstance(view, ViewObservation)
            or not _vector(view.camera_ned)
            or not _unit(view.direction_ned)
            or isinstance(view.step, bool)
            or not isinstance(view.step, int)
            or view.step < 0
            or not _finite(view.timestamp_s)
        ):
            return None
        distinct.pop(view.step, None)
        distinct[view.step] = view
    samples = list(distinct.values())[-max_views:]
    if len(samples) < 2:
        return None
    # Keep operation order identical to evaluate_onboard_ranging_reuse.py.
    matrix = [[0.0] * 3 for _ in range(3)]
    rhs = [0.0] * 3
    for view in samples:
        p, ray = view.camera_ned, view.direction_ned
        projection = [[float(i == j) - ray[i] * ray[j] for j in range(3)] for i in range(3)]
        for i in range(3):
            rhs[i] += sum(projection[i][j] * p[j] for j in range(3))
            for j in range(3):
                matrix[i][j] += projection[i][j]
    augmented = [matrix[i] + [rhs[i]] for i in range(3)]
    for column in range(3):
        pivot = max(range(column, 3), key=lambda i: abs(augmented[i][column]))
        if abs(augmented[pivot][column]) < 1e-9:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [x / divisor for x in augmented[column]]
        for i in range(3):
            if i != column:
                factor = augmented[i][column]
                augmented[i] = [
                    x - factor * y for x, y in zip(augmented[i], augmented[column], strict=True)
                ]
    point = tuple(augmented[i][3] for i in range(3))
    if not _vector(point):
        return None
    latest = samples[-1]
    baseline = max(math.dist(latest.camera_ned, v.camera_ned) for v in samples[:-1])
    angle = max(
        math.degrees(
            math.acos(
                max(
                    -1.0,
                    min(1.0, sum(latest.direction_ned[i] * v.direction_ned[i] for i in range(3))),
                )
            )
        )
        for v in samples[:-1]
    )
    residual = 0.0
    for view in samples:
        along = sum((point[i] - view.camera_ned[i]) * view.direction_ned[i] for i in range(3))
        if not math.isfinite(along) or along <= 0:
            return None
        projected = tuple(view.camera_ned[i] + along * view.direction_ned[i] for i in range(3))
        residual = max(residual, math.dist(projected, point))
    sigma = sigma_scale * max(
        0.5,
        residual
        + 1.5
        * math.dist(point, latest.camera_ned)
        * 0.002
        / max(math.sin(math.radians(angle)), 0.05),
    )
    if (
        not all(math.isfinite(x) for x in (baseline, angle, residual, sigma))
        or baseline < min_baseline_m
        or angle < min_ray_angle_deg
        or residual > max_residual_m
    ):
        return None
    return PositionEstimate(
        point, sigma, latest.timestamp_s, "triangulation", len(samples), baseline, angle, residual
    )


def make_ground_estimate(
    position_ned: Vector3,
    *,
    camera_ned: Vector3,
    direction_ned: Vector3,
    ground_height_sigma_m: float,
    timestamp_s: float,
) -> PositionEstimate | None:
    """Propagate vertical ground uncertainty to slant range along its hit ray.

    Ground/model bias is magnified near the horizon. The supplied point can
    come from either flat-ground or DEM intersection; no terrain truth is read.
    """
    if (
        not _vector(position_ned)
        or not _vector(camera_ned)
        or not _unit(direction_ned)
        or direction_ned[2] <= 1e-6
        or not _finite(ground_height_sigma_m)
        or ground_height_sigma_m <= 0
        or not _finite(timestamp_s)
    ):
        return None
    sigma = ground_height_sigma_m / direction_ned[2]
    if not math.isfinite(sigma):
        return None
    return PositionEstimate(tuple(position_ned), sigma, timestamp_s, "ground")


def position_from_range(
    measurement: RangeMeasurement,
    *,
    camera_ned: Vector3,
    direction_ned: Vector3,
) -> PositionEstimate | None:
    """Adapt any future distance + sigma + timestamp source to position fusion."""
    if (
        not isinstance(measurement, RangeMeasurement)
        or not _vector(camera_ned)
        or not _unit(direction_ned)
        or not _finite(measurement.distance_m)
        or measurement.distance_m <= 0
    ):
        return None
    point = tuple(camera_ned[i] + direction_ned[i] * measurement.distance_m for i in range(3))
    result = PositionEstimate(
        point, measurement.sigma_m, measurement.timestamp_s, measurement.source
    )
    return result if _estimate_valid(result) else None


def fuse_position_estimates(
    *estimates: PositionEstimate | None,
    consistency_sigma: float = 2.5,
) -> PositionEstimate | None:
    """Inverse-variance fusion with a pairwise Euclidean consistency gate.

    On disagreement select the newest source (larger sigma breaks time ties),
    never average conflicting positions, and widen sigma to cover all supplied
    positions. The source and consistency_note expose that conservative choice.
    None/invalid sources are omitted, so rejected triangulation leaves ground
    coordinates exactly intact. Independent depth sources can join this API.
    """
    if not _finite(consistency_sigma) or consistency_sigma <= 0:
        return None
    valid = [value for value in estimates if _estimate_valid(value)]
    if not valid:
        return None
    if len(valid) == 1:
        return valid[0]
    diagnostic = max(valid, key=lambda value: value.view_count)
    inconsistent = any(
        math.dist(a.position_ned, b.position_ned)
        > consistency_sigma * math.hypot(a.sigma_m, b.sigma_m)
        for i, a in enumerate(valid)
        for b in valid[i + 1 :]
    )
    if inconsistent:
        selected = max(valid, key=lambda value: (value.timestamp_s, value.sigma_m))
        sigma = max(
            selected.sigma_m,
            *(math.dist(selected.position_ned, other.position_ned) for other in valid),
        )
        if not math.isfinite(sigma):
            return None
        return replace(
            selected,
            sigma_m=sigma,
            view_count=diagnostic.view_count,
            baseline_m=diagnostic.baseline_m,
            ray_angle_deg=diagnostic.ray_angle_deg,
            residual_m=diagnostic.residual_m,
            consistent=False,
            consistency_note="source_disagreement:newest_then_larger_sigma",
        )
    minimum_sigma = min(value.sigma_m for value in valid)
    weights = [(minimum_sigma / value.sigma_m) ** 2 for value in valid]
    total_weight = sum(weights)
    point = tuple(
        sum(
            value.position_ned[i] * (weight / total_weight)
            for value, weight in zip(valid, weights, strict=True)
        )
        for i in range(3)
    )
    if not _vector(point):
        return None
    result = PositionEstimate(
        point,
        minimum_sigma / math.sqrt(total_weight),
        max(value.timestamp_s for value in valid),
        "fused",
        diagnostic.view_count,
        diagnostic.baseline_m,
        diagnostic.ray_angle_deg,
        diagnostic.residual_m,
        all(value.consistent for value in valid),
        next((value.consistency_note for value in valid if value.consistency_note), None),
    )
    return result if _estimate_valid(result) else None
