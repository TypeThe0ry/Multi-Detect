"""Strict local DEM loading, WGS-84 sampling and ray/terrain intersection.

The terrain grid is deliberately small and repository/file based.  Live,
replay, SIM and HIL callers all pass the same :class:`DemGpsTerrainObservation`
into the shared ranging engine; only the telemetry source changes.  Elevations
use MSL because MAVLink ``GPS_RAW_INT.alt`` is an MSL altitude.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from .autonomy.serialization import (
    AutonomyContractError,
    require_int,
    require_real,
    require_text,
    strict_json_loads,
    strict_object,
)
from .target_geolocation import wgs84_radii_of_curvature

_NETWORK_URI_RE = re.compile(r"(?:https?|ftp)://", re.IGNORECASE)
_DEM_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "scenario_id",
        "coordinate_reference",
        "vertical_datum",
        "origin",
        "north_origin_m",
        "east_origin_m",
        "north_spacing_m",
        "east_spacing_m",
        "elevation_m",
        "vertical_sigma_m",
        "slope_sigma_m_per_m",
    }
)
_ORIGIN_REQUIRED_FIELDS = frozenset({"latitude_deg", "longitude_deg", "elevation_m"})
_MAX_DEM_BYTES = 64 * 1024 * 1024
_MAX_LOCAL_OFFSET_M = 100_000.0
_ROOT_TOLERANCE = 1.0e-8


class TerrainDemError(ValueError):
    """Raised when a DEM document or terrain intersection is unsafe."""


@dataclass(frozen=True, slots=True)
class TerrainSurfaceSample:
    north_m: float
    east_m: float
    elevation_m: float
    north_slope_m_per_m: float
    east_slope_m_per_m: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.north_m,
                self.east_m,
                self.elevation_m,
                self.north_slope_m_per_m,
                self.east_slope_m_per_m,
            )
        ):
            raise TerrainDemError("terrain sample values must be finite")


@dataclass(frozen=True, slots=True)
class DemRayIntersection:
    slant_range_m: float
    north_m: float
    east_m: float
    elevation_m: float
    incidence_component: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (
                self.slant_range_m,
                self.north_m,
                self.east_m,
                self.elevation_m,
                self.incidence_component,
            )
        ):
            raise TerrainDemError("terrain intersection values must be finite")
        if self.slant_range_m <= 0.0 or self.incidence_component <= 0.0:
            raise TerrainDemError("terrain intersection must be forward and downward")


@dataclass(frozen=True, slots=True)
class LocalDemGrid:
    scenario_id: str
    origin_latitude_deg: float
    origin_longitude_deg: float
    origin_elevation_m: float
    north_origin_m: float
    east_origin_m: float
    north_spacing_m: float
    east_spacing_m: float
    elevations_m: tuple[tuple[float, ...], ...]
    vertical_sigma_m: float
    slope_sigma_m_per_m: float
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise TerrainDemError("terrain DEM scenario_id cannot be empty")
        numeric = (
            self.origin_latitude_deg,
            self.origin_longitude_deg,
            self.origin_elevation_m,
            self.north_origin_m,
            self.east_origin_m,
            self.north_spacing_m,
            self.east_spacing_m,
            self.vertical_sigma_m,
            self.slope_sigma_m_per_m,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in numeric
        ):
            raise TerrainDemError("terrain DEM numeric fields must be finite")
        if not -90.0 < self.origin_latitude_deg < 90.0:
            raise TerrainDemError("terrain DEM latitude is outside the WGS-84 domain")
        if not -180.0 <= self.origin_longitude_deg <= 180.0:
            raise TerrainDemError("terrain DEM longitude is outside the WGS-84 domain")
        if not -500.0 <= self.origin_elevation_m <= 9_000.0:
            raise TerrainDemError("terrain DEM origin elevation is outside supported bounds")
        if self.north_spacing_m <= 0.0 or self.east_spacing_m <= 0.0:
            raise TerrainDemError("terrain DEM spacing must be positive")
        if not 0.0 < self.vertical_sigma_m <= 100.0:
            raise TerrainDemError("terrain DEM vertical uncertainty is outside supported bounds")
        if not 0.0 <= self.slope_sigma_m_per_m <= 1.0:
            raise TerrainDemError("terrain DEM slope uncertainty is outside supported bounds")
        if len(self.elevations_m) < 2 or len(self.elevations_m[0]) < 2:
            raise TerrainDemError("terrain DEM requires at least a 2x2 elevation grid")
        width = len(self.elevations_m[0])
        for row in self.elevations_m:
            if len(row) != width:
                raise TerrainDemError("terrain DEM rows must have equal width")
            if any(not math.isfinite(value) or not -500.0 <= value <= 9_000.0 for value in row):
                raise TerrainDemError("terrain DEM elevation is outside supported bounds")
        if (
            abs(self.north_origin_m) > _MAX_LOCAL_OFFSET_M
            or abs(self.north_max_m) > _MAX_LOCAL_OFFSET_M
            or abs(self.east_origin_m) > _MAX_LOCAL_OFFSET_M
            or abs(self.east_max_m) > _MAX_LOCAL_OFFSET_M
        ):
            raise TerrainDemError("terrain DEM extent exceeds the supported local WGS-84 domain")
        if not self.contains_local(0.0, 0.0):
            raise TerrainDemError("terrain DEM grid does not cover its WGS-84 origin")
        if not math.isclose(
            self.sample_local(0.0, 0.0).elevation_m,
            self.origin_elevation_m,
            rel_tol=0.0,
            abs_tol=1.0e-6,
        ):
            raise TerrainDemError(
                "terrain DEM elevation at local zero does not match its WGS-84 origin"
            )
        if not isinstance(self.source_sha256, str):
            raise TerrainDemError("terrain DEM source SHA-256 is invalid")
        digest = self.source_sha256.lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise TerrainDemError("terrain DEM source SHA-256 is invalid")
        object.__setattr__(self, "source_sha256", digest)

    @property
    def row_count(self) -> int:
        return len(self.elevations_m)

    @property
    def column_count(self) -> int:
        return len(self.elevations_m[0])

    @property
    def north_max_m(self) -> float:
        return self.north_origin_m + self.north_spacing_m * (self.row_count - 1)

    @property
    def east_max_m(self) -> float:
        return self.east_origin_m + self.east_spacing_m * (self.column_count - 1)

    def contains_local(self, north_m: float, east_m: float) -> bool:
        return (
            math.isfinite(north_m)
            and math.isfinite(east_m)
            and self.north_origin_m <= north_m <= self.north_max_m
            and self.east_origin_m <= east_m <= self.east_max_m
        )

    def local_from_wgs84(self, latitude_deg: float, longitude_deg: float) -> tuple[float, float]:
        """Convert one nearby WGS-84 coordinate into this grid's local N/E frame."""

        if not math.isfinite(latitude_deg) or not -90.0 < latitude_deg < 90.0:
            raise TerrainDemError("terrain sample latitude is outside the WGS-84 domain")
        if not math.isfinite(longitude_deg) or not -180.0 <= longitude_deg <= 180.0:
            raise TerrainDemError("terrain sample longitude is outside the WGS-84 domain")
        longitude_delta_deg = ((longitude_deg - self.origin_longitude_deg + 180.0) % 360.0) - 180.0
        mean_latitude_deg = (latitude_deg + self.origin_latitude_deg) / 2.0
        meridional_m, prime_vertical_m = wgs84_radii_of_curvature(mean_latitude_deg)
        north_m = math.radians(latitude_deg - self.origin_latitude_deg) * meridional_m
        east_m = (
            math.radians(longitude_delta_deg)
            * prime_vertical_m
            * math.cos(math.radians(mean_latitude_deg))
        )
        if abs(north_m) > _MAX_LOCAL_OFFSET_M or abs(east_m) > _MAX_LOCAL_OFFSET_M:
            raise TerrainDemError("terrain sample is outside the supported local WGS-84 domain")
        return north_m, east_m

    def sample_wgs84(self, latitude_deg: float, longitude_deg: float) -> TerrainSurfaceSample:
        north_m, east_m = self.local_from_wgs84(latitude_deg, longitude_deg)
        return self.sample_local(north_m, east_m)

    def sample_local(self, north_m: float, east_m: float) -> TerrainSurfaceSample:
        """Bilinearly sample elevation and the analytic local cell gradient."""

        if not self.contains_local(north_m, east_m):
            raise TerrainDemError("terrain sample is outside DEM coverage")
        row_position = (north_m - self.north_origin_m) / self.north_spacing_m
        column_position = (east_m - self.east_origin_m) / self.east_spacing_m
        row = min(self.row_count - 2, max(0, int(math.floor(row_position))))
        column = min(
            self.column_count - 2,
            max(0, int(math.floor(column_position))),
        )
        north_fraction = min(1.0, max(0.0, row_position - row))
        east_fraction = min(1.0, max(0.0, column_position - column))
        z00 = self.elevations_m[row][column]
        z10 = self.elevations_m[row + 1][column]
        z01 = self.elevations_m[row][column + 1]
        z11 = self.elevations_m[row + 1][column + 1]
        cross = z11 - z10 - z01 + z00
        elevation_m = (
            z00
            + (z10 - z00) * north_fraction
            + (z01 - z00) * east_fraction
            + cross * north_fraction * east_fraction
        )
        north_slope = ((z10 - z00) + cross * east_fraction) / self.north_spacing_m
        east_slope = ((z01 - z00) + cross * north_fraction) / self.east_spacing_m
        return TerrainSurfaceSample(
            north_m=north_m,
            east_m=east_m,
            elevation_m=elevation_m,
            north_slope_m_per_m=north_slope,
            east_slope_m_per_m=east_slope,
        )

    def intersect_ray(
        self,
        *,
        reference_north_m: float,
        reference_east_m: float,
        height_m: float,
        camera_offset_ned: tuple[float, float, float],
        ray_ned: tuple[float, float, float],
        maximum_slant_range_m: float,
        minimum_incidence_component: float,
        slope_bias_north_m_per_m: float = 0.0,
        slope_bias_east_m_per_m: float = 0.0,
    ) -> DemRayIntersection | None:
        """Return the first safe ray intersection with the bilinear DEM.

        Every horizontal grid-cell crossing is used as an interval boundary.
        Inside one cell, bilinear elevation composed with the ray is quadratic,
        so all roots can be checked without step-size or iteration ambiguity.
        """

        numeric = (
            reference_north_m,
            reference_east_m,
            height_m,
            *camera_offset_ned,
            *ray_ned,
            maximum_slant_range_m,
            minimum_incidence_component,
            slope_bias_north_m_per_m,
            slope_bias_east_m_per_m,
        )
        if not all(math.isfinite(value) for value in numeric):
            return None
        if height_m <= 0.0 or maximum_slant_range_m <= 0.0 or minimum_incidence_component <= 0.0:
            return None
        if len(camera_offset_ned) != 3 or len(ray_ned) != 3:
            return None
        ray_norm = math.sqrt(sum(value * value for value in ray_ned))
        if not math.isclose(ray_norm, 1.0, rel_tol=1.0e-6, abs_tol=1.0e-6):
            return None
        if not self.contains_local(reference_north_m, reference_east_m):
            return None
        reference = self.sample_local(reference_north_m, reference_east_m)
        camera_north = reference_north_m + camera_offset_ned[0]
        camera_east = reference_east_m + camera_offset_ned[1]
        if not self.contains_local(camera_north, camera_east):
            return None
        aircraft_altitude_m = reference.elevation_m + height_m
        camera_altitude_m = aircraft_altitude_m - camera_offset_ned[2]
        camera_surface = self.sample_local(camera_north, camera_east)
        camera_surface_elevation = (
            camera_surface.elevation_m
            + slope_bias_north_m_per_m * (camera_north - reference_north_m)
            + slope_bias_east_m_per_m * (camera_east - reference_east_m)
        )
        if camera_altitude_m <= camera_surface_elevation:
            return None

        ray_north, ray_east, ray_down = ray_ned
        if ray_down <= 0.0:
            return None
        interval = _horizontal_ray_grid_interval(
            north_m=camera_north,
            east_m=camera_east,
            north_direction=ray_north,
            east_direction=ray_east,
            north_min_m=self.north_origin_m,
            north_max_m=self.north_max_m,
            east_min_m=self.east_origin_m,
            east_max_m=self.east_max_m,
            maximum_slant_range_m=maximum_slant_range_m,
        )
        if interval is None:
            return None
        interval_start, interval_end = interval
        boundaries = [interval_start, interval_end]
        boundaries.extend(
            _axis_boundary_parameters(
                origin=camera_north,
                direction=ray_north,
                first=self.north_origin_m,
                spacing=self.north_spacing_m,
                count=self.row_count,
                lower=interval_start,
                upper=interval_end,
            )
        )
        boundaries.extend(
            _axis_boundary_parameters(
                origin=camera_east,
                direction=ray_east,
                first=self.east_origin_m,
                spacing=self.east_spacing_m,
                count=self.column_count,
                lower=interval_start,
                upper=interval_end,
            )
        )
        ordered = _unique_sorted(boundaries)
        for lower, upper in pairwise(ordered):
            if upper - lower <= _ROOT_TOLERANCE:
                continue
            midpoint = (lower + upper) / 2.0
            north_mid = camera_north + midpoint * ray_north
            east_mid = camera_east + midpoint * ray_east
            row, column = self._cell_indices(north_mid, east_mid)
            q2, q1, q0 = self._ray_cell_polynomial(
                row=row,
                column=column,
                camera_north_m=camera_north,
                camera_east_m=camera_east,
                camera_altitude_m=camera_altitude_m,
                ray_ned=ray_ned,
                reference_north_m=reference_north_m,
                reference_east_m=reference_east_m,
                slope_bias_north_m_per_m=slope_bias_north_m_per_m,
                slope_bias_east_m_per_m=slope_bias_east_m_per_m,
            )
            for root in _quadratic_roots(q2, q1, q0):
                if root < lower - _ROOT_TOLERANCE or root > upper + _ROOT_TOLERANCE:
                    continue
                slant_range_m = min(upper, max(lower, root))
                if slant_range_m <= _ROOT_TOLERANCE:
                    continue
                incidence = -(2.0 * q2 * slant_range_m + q1)
                if incidence < minimum_incidence_component:
                    continue
                north_m = camera_north + slant_range_m * ray_north
                east_m = camera_east + slant_range_m * ray_east
                # A root accepted within the numerical tolerance may land a
                # few ulps outside an outer cell.  Clamp only to the proven ray
                # interval so sampling cannot turn a safe boundary hit into an
                # exception.
                north_m = min(self.north_max_m, max(self.north_origin_m, north_m))
                east_m = min(self.east_max_m, max(self.east_origin_m, east_m))
                sample = self.sample_local(north_m, east_m)
                elevation_m = (
                    sample.elevation_m
                    + slope_bias_north_m_per_m * (north_m - reference_north_m)
                    + slope_bias_east_m_per_m * (east_m - reference_east_m)
                )
                ray_altitude_m = camera_altitude_m - slant_range_m * ray_down
                if abs(ray_altitude_m - elevation_m) > 1.0e-5:
                    continue
                return DemRayIntersection(
                    slant_range_m=slant_range_m,
                    north_m=north_m,
                    east_m=east_m,
                    elevation_m=elevation_m,
                    incidence_component=incidence,
                )
        return None

    def _cell_indices(self, north_m: float, east_m: float) -> tuple[int, int]:
        row = min(
            self.row_count - 2,
            max(0, int(math.floor((north_m - self.north_origin_m) / self.north_spacing_m))),
        )
        column = min(
            self.column_count - 2,
            max(0, int(math.floor((east_m - self.east_origin_m) / self.east_spacing_m))),
        )
        return row, column

    def _ray_cell_polynomial(
        self,
        *,
        row: int,
        column: int,
        camera_north_m: float,
        camera_east_m: float,
        camera_altitude_m: float,
        ray_ned: tuple[float, float, float],
        reference_north_m: float,
        reference_east_m: float,
        slope_bias_north_m_per_m: float,
        slope_bias_east_m_per_m: float,
    ) -> tuple[float, float, float]:
        ray_north, ray_east, ray_down = ray_ned
        cell_north = self.north_origin_m + row * self.north_spacing_m
        cell_east = self.east_origin_m + column * self.east_spacing_m
        u0 = (camera_north_m - cell_north) / self.north_spacing_m
        u1 = ray_north / self.north_spacing_m
        v0 = (camera_east_m - cell_east) / self.east_spacing_m
        v1 = ray_east / self.east_spacing_m
        z00 = self.elevations_m[row][column]
        z10 = self.elevations_m[row + 1][column]
        z01 = self.elevations_m[row][column + 1]
        z11 = self.elevations_m[row + 1][column + 1]
        north_delta = z10 - z00
        east_delta = z01 - z00
        cross = z11 - z10 - z01 + z00
        terrain_0 = z00 + north_delta * u0 + east_delta * v0 + cross * u0 * v0
        terrain_1 = north_delta * u1 + east_delta * v1 + cross * (u0 * v1 + u1 * v0)
        terrain_2 = cross * u1 * v1
        terrain_0 += slope_bias_north_m_per_m * (
            camera_north_m - reference_north_m
        ) + slope_bias_east_m_per_m * (camera_east_m - reference_east_m)
        terrain_1 += slope_bias_north_m_per_m * ray_north + slope_bias_east_m_per_m * ray_east
        return (
            -terrain_2,
            -ray_down - terrain_1,
            camera_altitude_m - terrain_0,
        )


@dataclass(frozen=True, slots=True)
class DemGpsTerrainObservation:
    grid: LocalDemGrid
    reference_north_m: float
    reference_east_m: float
    height_m: float
    height_sigma_m: float
    horizontal_sigma_m: float
    captured_at_s: float

    def __post_init__(self) -> None:
        if not isinstance(self.grid, LocalDemGrid):
            raise TerrainDemError("terrain observation grid is invalid")
        values = (
            self.reference_north_m,
            self.reference_east_m,
            self.height_m,
            self.height_sigma_m,
            self.horizontal_sigma_m,
            self.captured_at_s,
        )
        if not all(math.isfinite(value) for value in values):
            raise TerrainDemError("terrain observation values must be finite")
        if self.height_m <= 0.0 or self.height_sigma_m <= 0.0:
            raise TerrainDemError("terrain observation height and sigma must be positive")
        if self.horizontal_sigma_m < 0.0 or self.captured_at_s < 0.0:
            raise TerrainDemError("terrain observation uncertainty/timestamp is invalid")
        if not self.grid.contains_local(self.reference_north_m, self.reference_east_m):
            raise TerrainDemError("terrain observation reference is outside DEM coverage")


def load_local_dem_grid(path: str | Path) -> LocalDemGrid:
    """Load one strict schema-v1 local DEM with a raw-file identity hash."""

    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise TerrainDemError(f"terrain DEM is not a regular file: {source}")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise TerrainDemError(f"terrain DEM cannot be read: {source}") from exc
    if not payload or len(payload) > _MAX_DEM_BYTES:
        raise TerrainDemError("terrain DEM size is outside supported bounds")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TerrainDemError("terrain DEM is not valid UTF-8") from exc
    if _NETWORK_URI_RE.search(text):
        raise TerrainDemError("terrain DEM cannot contain network URIs")
    try:
        raw = strict_json_loads(payload, name="terrain DEM")
        document = strict_object(
            raw,
            name="terrain DEM",
            required=_DEM_REQUIRED_FIELDS,
        )
        schema_version = require_int(document["schema_version"], "schema_version", minimum=1)
        if schema_version != 1:
            raise AutonomyContractError("terrain DEM schema_version must be 1")
        scenario_id = require_text(document["scenario_id"], "scenario_id")
        if document["coordinate_reference"] != "WGS84":
            raise AutonomyContractError("terrain DEM coordinate_reference must be WGS84")
        if document["vertical_datum"] != "MSL":
            raise AutonomyContractError("terrain DEM vertical_datum must be MSL")
        origin = strict_object(
            document["origin"],
            name="terrain DEM origin",
            required=_ORIGIN_REQUIRED_FIELDS,
        )
        latitude = require_real(
            origin["latitude_deg"],
            "terrain DEM origin latitude_deg",
            minimum=-90.0,
            maximum=90.0,
        )
        longitude = require_real(
            origin["longitude_deg"],
            "terrain DEM origin longitude_deg",
            minimum=-180.0,
            maximum=180.0,
        )
        if latitude in {-90.0, 90.0}:
            raise AutonomyContractError("terrain DEM origin latitude cannot be a pole")
        elevation = require_real(
            origin["elevation_m"],
            "terrain DEM origin elevation_m",
            minimum=-500.0,
            maximum=9_000.0,
        )
        north_origin = require_real(document["north_origin_m"], "north_origin_m")
        east_origin = require_real(document["east_origin_m"], "east_origin_m")
        north_spacing = require_real(document["north_spacing_m"], "north_spacing_m", minimum=0.0)
        east_spacing = require_real(document["east_spacing_m"], "east_spacing_m", minimum=0.0)
        if north_spacing == 0.0 or east_spacing == 0.0:
            raise AutonomyContractError("terrain DEM spacing must be positive")
        elevations = _elevation_rows(document["elevation_m"])
        vertical_sigma = require_real(
            document["vertical_sigma_m"],
            "vertical_sigma_m",
            minimum=0.0,
            maximum=100.0,
        )
        if vertical_sigma == 0.0:
            raise AutonomyContractError("terrain DEM vertical_sigma_m must be positive")
        slope_sigma = require_real(
            document["slope_sigma_m_per_m"],
            "slope_sigma_m_per_m",
            minimum=0.0,
            maximum=1.0,
        )
    except (AutonomyContractError, KeyError) as exc:
        raise TerrainDemError(str(exc)) from exc
    return LocalDemGrid(
        scenario_id=scenario_id,
        origin_latitude_deg=latitude,
        origin_longitude_deg=longitude,
        origin_elevation_m=elevation,
        north_origin_m=north_origin,
        east_origin_m=east_origin,
        north_spacing_m=north_spacing,
        east_spacing_m=east_spacing,
        elevations_m=elevations,
        vertical_sigma_m=vertical_sigma,
        slope_sigma_m_per_m=slope_sigma,
        source_sha256=hashlib.sha256(payload).hexdigest(),
    )


def dem_gps_terrain_observation(
    grid: LocalDemGrid,
    *,
    latitude_deg: float,
    longitude_deg: float,
    gps_absolute_altitude_m: float,
    gps_horizontal_sigma_m: float,
    gps_vertical_sigma_m: float,
    captured_at_s: float,
) -> DemGpsTerrainObservation:
    """Bind one qualified GPS fix to the DEM and propagate its height sigma."""

    numeric = (
        gps_absolute_altitude_m,
        gps_horizontal_sigma_m,
        gps_vertical_sigma_m,
        captured_at_s,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise TerrainDemError("DEM/GPS observation values must be finite")
    if gps_horizontal_sigma_m < 0.0 or gps_vertical_sigma_m < 0.0 or captured_at_s < 0.0:
        raise TerrainDemError("DEM/GPS uncertainty and timestamp must be non-negative")
    surface = grid.sample_wgs84(latitude_deg, longitude_deg)
    height_m = gps_absolute_altitude_m - surface.elevation_m
    if height_m <= 0.0:
        raise TerrainDemError("GPS altitude is not above the DEM surface")
    nominal_slope_sigma = gps_horizontal_sigma_m * math.hypot(
        surface.north_slope_m_per_m,
        surface.east_slope_m_per_m,
    )
    uncertain_slope_sigma = math.sqrt(2.0) * gps_horizontal_sigma_m * grid.slope_sigma_m_per_m
    height_sigma_m = math.hypot(
        gps_vertical_sigma_m,
        grid.vertical_sigma_m,
        nominal_slope_sigma,
        uncertain_slope_sigma,
    )
    return DemGpsTerrainObservation(
        grid=grid,
        reference_north_m=surface.north_m,
        reference_east_m=surface.east_m,
        height_m=height_m,
        height_sigma_m=height_sigma_m,
        horizontal_sigma_m=gps_horizontal_sigma_m,
        captured_at_s=captured_at_s,
    )


def _elevation_rows(value: object) -> tuple[tuple[float, ...], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AutonomyContractError("elevation_m must be an array")
    rows: list[tuple[float, ...]] = []
    width: int | None = None
    for row_index, row in enumerate(value):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
            raise AutonomyContractError(f"elevation_m[{row_index}] must be an array")
        parsed = tuple(
            require_real(
                item,
                f"elevation_m[{row_index}][{column_index}]",
                minimum=-500.0,
                maximum=9_000.0,
            )
            for column_index, item in enumerate(row)
        )
        if len(parsed) < 2:
            raise AutonomyContractError("terrain DEM requires at least two columns")
        if width is None:
            width = len(parsed)
        elif len(parsed) != width:
            raise AutonomyContractError("terrain DEM rows must have equal width")
        rows.append(parsed)
    if len(rows) < 2:
        raise AutonomyContractError("terrain DEM requires at least two rows")
    return tuple(rows)


def _horizontal_ray_grid_interval(
    *,
    north_m: float,
    east_m: float,
    north_direction: float,
    east_direction: float,
    north_min_m: float,
    north_max_m: float,
    east_min_m: float,
    east_max_m: float,
    maximum_slant_range_m: float,
) -> tuple[float, float] | None:
    lower, upper = 0.0, maximum_slant_range_m
    for origin, direction, minimum, maximum in (
        (north_m, north_direction, north_min_m, north_max_m),
        (east_m, east_direction, east_min_m, east_max_m),
    ):
        if abs(direction) <= 1.0e-15:
            if origin < minimum or origin > maximum:
                return None
            continue
        first = (minimum - origin) / direction
        second = (maximum - origin) / direction
        axis_lower, axis_upper = sorted((first, second))
        lower = max(lower, axis_lower)
        upper = min(upper, axis_upper)
        if lower > upper:
            return None
    return max(0.0, lower), min(maximum_slant_range_m, upper)


def _axis_boundary_parameters(
    *,
    origin: float,
    direction: float,
    first: float,
    spacing: float,
    count: int,
    lower: float,
    upper: float,
) -> list[float]:
    if abs(direction) <= 1.0e-15:
        return []
    result: list[float] = []
    for index in range(1, count - 1):
        parameter = (first + index * spacing - origin) / direction
        if lower + _ROOT_TOLERANCE < parameter < upper - _ROOT_TOLERANCE:
            result.append(parameter)
    return result


def _unique_sorted(values: Sequence[float]) -> list[float]:
    ordered = sorted(values)
    result: list[float] = []
    for value in ordered:
        if not result or abs(value - result[-1]) > _ROOT_TOLERANCE:
            result.append(value)
    return result


def _quadratic_roots(a: float, b: float, c: float) -> tuple[float, ...]:
    scale = max(1.0, abs(a), abs(b), abs(c))
    if abs(a) <= 1.0e-14 * scale:
        if abs(b) <= 1.0e-14 * scale:
            return ()
        return (-c / b,)
    discriminant = b * b - 4.0 * a * c
    tolerance = 1.0e-12 * max(1.0, b * b, abs(4.0 * a * c))
    if discriminant < -tolerance:
        return ()
    root = math.sqrt(max(0.0, discriminant))
    if root == 0.0:
        return (-b / (2.0 * a),)
    # The textbook formula loses the small root when b and sqrt(D) nearly
    # cancel.  This q-form keeps both intersections stable across shallow DEM
    # cells and long outdoor rays.
    q = -0.5 * (b + math.copysign(root, b))
    roots = (q / a, c / q)
    return tuple(sorted(roots))


__all__ = [
    "DemGpsTerrainObservation",
    "DemRayIntersection",
    "LocalDemGrid",
    "TerrainDemError",
    "TerrainSurfaceSample",
    "dem_gps_terrain_observation",
    "load_local_dem_grid",
]
