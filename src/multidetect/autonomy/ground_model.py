"""Opt-in ground surfaces in local north/east/up metres.

DEM elevations are MSL: ``reference_elevation_m`` is the MSL altitude of local
NED z=0, not necessarily the launch/home altitude. No SIM assets are discovered
implicitly. AGL supplies a local plane, not terrain at a distant target.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from multidetect.terrain_dem import LocalDemGrid, load_local_dem_grid

Vector3 = tuple[float, float, float]
GroundPoint = tuple[float, float]


class GroundModel(Protocol):
    def intersect_ray(self, origin_neu: Vector3, ray_ned: Vector3) -> GroundPoint | None:
        """Return the first forward ground hit, or None for an unsupported ray."""
        ...


def _valid_ray(origin: Vector3, ray: Vector3) -> bool:
    return all(math.isfinite(v) for v in (*origin, *ray)) and ray[2] > 1e-6


@dataclass(frozen=True, slots=True)
class FlatGround:
    ground_up_m: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.ground_up_m):
            raise ValueError("ground_up_m must be finite")

    def intersect_ray(self, origin_neu: Vector3, ray_ned: Vector3) -> GroundPoint | None:
        if not _valid_ray(origin_neu, ray_ned):
            return None
        height = origin_neu[2] - self.ground_up_m
        if height <= 0:
            return None
        t = height / ray_ned[2]
        return origin_neu[0] + ray_ned[0] * t, origin_neu[1] + ray_ned[1] * t


@dataclass(frozen=True, slots=True)
class DemGround:
    grid: LocalDemGrid
    reference_elevation_m: float | None = None
    max_range_m: float = 10_000.0
    fallback: GroundModel | None = None

    def __post_init__(self) -> None:
        if self.reference_elevation_m is None:
            object.__setattr__(self, "reference_elevation_m", self.grid.origin_elevation_m)
        if not math.isfinite(self.reference_elevation_m):
            raise ValueError("reference_elevation_m must be finite")
        if not math.isfinite(self.max_range_m) or self.max_range_m <= 0:
            raise ValueError("max_range_m must be finite and positive")

    def intersect_ray(self, origin_neu: Vector3, ray_ned: Vector3) -> GroundPoint | None:
        if not _valid_ray(origin_neu, ray_ned):
            return None
        result = self._march(origin_neu, ray_ned)
        if result is None and self.fallback is not None:
            return self.fallback.intersect_ray(origin_neu, ray_ned)
        return result

    def _march(self, origin: Vector3, ray: Vector3) -> GroundPoint | None:
        """March through grid cells, checking *all* crossings within each cell.

        A bilinear surface along a ray is quadratic. Split each cell interval
        at its extremum before bisection so a narrow ridge cannot be skipped
        by two above-ground endpoints. This also bounds work by grid size.
        """
        grid = self.grid
        if not grid.contains_local(origin[0], origin[1]):
            return None
        end = self.max_range_m / math.hypot(*ray)
        boundaries = [0.0]
        for pos, speed, lo, spacing, count in (
            (origin[0], ray[0], grid.north_origin_m, grid.north_spacing_m, grid.row_count),
            (origin[1], ray[1], grid.east_origin_m, grid.east_spacing_m, grid.column_count),
        ):
            if abs(speed) < 1e-15:
                continue
            exit_t = ((lo + spacing * (count - 1) if speed > 0 else lo) - pos) / speed
            end = min(end, exit_t)
            boundaries.extend(
                t for i in range(count) if 0 < (t := (lo + spacing * i - pos) / speed)
            )
        if end <= 0:
            return None
        boundaries = sorted({t for t in boundaries if t < end} | {end})

        def point(t: float) -> GroundPoint:
            return origin[0] + ray[0] * t, origin[1] + ray[1] * t

        def clearance(t: float) -> float:
            n, e = point(t)
            # Only roundoff at the proven outer boundary is clamped.
            n = min(grid.north_max_m, max(grid.north_origin_m, n))
            e = min(grid.east_max_m, max(grid.east_origin_m, e))
            terrain_up = grid.sample_local(n, e).elevation_m - self.reference_elevation_m
            return origin[2] - ray[2] * t - terrain_up

        if clearance(0.0) <= 0:
            return None
        for lower, upper in pairwise(boundaries):
            middle = (lower + upper) / 2
            a, b, c = clearance(lower), clearance(middle), clearance(upper)
            q2, q1 = 2 * (a + c - 2 * b), 4 * b - 3 * a - c
            cuts = [lower, upper]
            if abs(q2) > 1e-14:
                extremum = -q1 / (2 * q2)
                if 0 < extremum < 1:
                    cuts.insert(1, lower + extremum * (upper - lower))
            for left, right in pairwise(cuts):
                if clearance(right) > 0:
                    continue
                for _ in range(48):
                    mid = (left + right) / 2
                    if clearance(mid) > 0:
                        left = mid
                    else:
                        right = mid
                return point(right)
        return None


@dataclass(frozen=True, slots=True)
class GroundProjection:
    """Policy configuration and per-frame selection; None preserves legacy math."""

    model: GroundModel | None = None
    use_agl: bool = False
    agl_max_age_s: float = 1.0

    def for_observation(self, observation: Mapping[str, Any]) -> GroundModel | None:
        if self.use_agl:
            measurement = observation.get("ground_agl")
            pose = observation.get("pose", observation)
            if isinstance(measurement, Mapping) and isinstance(pose, Mapping):
                agl, age = measurement.get("height_m"), measurement.get("age_s")
                ned = pose.get("local_ned_m")
                if (
                    isinstance(agl, (int, float))
                    and not isinstance(agl, bool)
                    and math.isfinite(agl)
                    and agl > 0
                    and isinstance(age, (int, float))
                    and not isinstance(age, bool)
                    and math.isfinite(age)
                    and 0 <= age <= self.agl_max_age_s
                    and isinstance(ned, (list, tuple))
                    and len(ned) == 3
                    and isinstance(ned[2], (int, float))
                    and not isinstance(ned[2], bool)
                    and math.isfinite(ned[2])
                ):
                    return FlatGround(-ned[2] - agl)
        return self.model


def ground_projection_from_config(
    config: Mapping[str, Any] | None, *, base_dir: Path
) -> GroundProjection:
    """Load only the explicitly supplied mission DEM; paths resolve at base_dir.

    Reference elevation and horizontal registration are the mission loader's
    responsibility: DEM local (0,0) must coincide with local NED (0,0).
    """
    if config is None:
        return GroundProjection()
    if not isinstance(config, Mapping):
        raise ValueError("ground_model must be an object")
    kind = config.get("type", "flat")
    use_agl = config.get("use_agl", False)
    if not isinstance(use_agl, bool):
        raise ValueError("use_agl must be boolean")
    age = float(config.get("agl_max_age_s", 1.0))
    if not math.isfinite(age) or age <= 0:
        raise ValueError("agl_max_age_s must be finite and positive")
    if kind == "flat":
        model = FlatGround(float(config.get("ground_up_m", 0.0)))
    elif kind == "dem":
        path = Path(config["path"])
        fallback = config.get("fallback", "none")
        if fallback not in {"none", "flat"}:
            raise ValueError("DEM fallback must be 'none' or 'flat'")
        model = DemGround(
            load_local_dem_grid(path if path.is_absolute() else base_dir / path),
            reference_elevation_m=config.get("reference_elevation_m"),
            max_range_m=float(config.get("max_range_m", 10_000.0)),
            fallback=FlatGround(float(config.get("ground_up_m", 0.0)))
            if fallback == "flat"
            else None,
        )
    else:
        raise ValueError(f"unknown ground model: {kind!r}")
    return GroundProjection(model, use_agl, age)
