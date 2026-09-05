from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import (
    LocalDemGrid,
    TerrainDemError,
    dem_gps_terrain_observation,
    load_local_dem_grid,
)

ROOT = Path(__file__).resolve().parents[1]
DEM = ROOT / "simulation/terrain/river_rescue_vtol_001.dem.json"


def _planar_grid(
    *,
    north_slope: float = 0.10,
    east_slope: float = 0.05,
    slope_sigma: float = 0.002,
) -> LocalDemGrid:
    axis = (-200.0, 0.0, 200.0)
    elevations = tuple(
        tuple(5.0 + north_slope * north + east_slope * east for east in axis) for north in axis
    )
    return LocalDemGrid(
        scenario_id="slope-test",
        origin_latitude_deg=1.3,
        origin_longitude_deg=103.8,
        origin_elevation_m=5.0,
        north_origin_m=-200.0,
        east_origin_m=-200.0,
        north_spacing_m=200.0,
        east_spacing_m=200.0,
        elevations_m=elevations,
        vertical_sigma_m=0.35,
        slope_sigma_m_per_m=slope_sigma,
        source_sha256="a" * 64,
    )


def test_dem_grid_rejects_empty_elevation_matrix_with_domain_error() -> None:
    with pytest.raises(TerrainDemError, match="2x2 elevation grid"):
        LocalDemGrid(
            scenario_id="empty-grid",
            origin_latitude_deg=1.3,
            origin_longitude_deg=103.8,
            origin_elevation_m=5.0,
            north_origin_m=0.0,
            east_origin_m=0.0,
            north_spacing_m=1.0,
            east_spacing_m=1.0,
            elevations_m=(),
            vertical_sigma_m=0.35,
            slope_sigma_m_per_m=0.002,
            source_sha256="a" * 64,
        )


def test_repository_dem_loads_with_explicit_msl_datum() -> None:
    grid = load_local_dem_grid(DEM)

    assert grid.scenario_id == "river_rescue_vtol_001"
    assert grid.source_sha256 == hashlib.sha256(DEM.read_bytes()).hexdigest()
    assert grid.contains_local(0.0, 0.0)
    sample = grid.sample_local(0.0, 0.0)
    assert sample.elevation_m == pytest.approx(5.0)
    assert sample.north_slope_m_per_m == pytest.approx(0.0)
    assert sample.east_slope_m_per_m == pytest.approx(0.0)


def test_dem_loader_rejects_missing_vertical_datum_and_duplicate_keys(tmp_path: Path) -> None:
    document = json.loads(DEM.read_text(encoding="utf-8"))
    document.pop("vertical_datum")
    missing = tmp_path / "missing-datum.dem.json"
    missing.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(TerrainDemError, match="vertical_datum"):
        load_local_dem_grid(missing)

    duplicate = tmp_path / "duplicate.dem.json"
    duplicate.write_text(
        DEM.read_text(encoding="utf-8").replace(
            '"schema_version": 1,',
            '"schema_version": 1, "schema_version": 1,',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(TerrainDemError, match="duplicate JSON key"):
        load_local_dem_grid(duplicate)


def test_dem_loader_rejects_surface_that_disagrees_with_wgs84_origin(tmp_path: Path) -> None:
    document = json.loads(DEM.read_text(encoding="utf-8"))
    document["origin"]["elevation_m"] = 6.0
    mismatched = tmp_path / "mismatched-origin.dem.json"
    mismatched.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(TerrainDemError, match="local zero"):
        load_local_dem_grid(mismatched)


def test_wgs84_sampling_recovers_nearby_local_coordinate() -> None:
    grid = _planar_grid()
    latitude, longitude = wgs84_forward_from_ned(
        latitude_deg=grid.origin_latitude_deg,
        longitude_deg=grid.origin_longitude_deg,
        north_offset_m=80.0,
        east_offset_m=-60.0,
    )

    sample = grid.sample_wgs84(latitude, longitude)

    assert sample.north_m == pytest.approx(80.0, abs=0.02)
    assert sample.east_m == pytest.approx(-60.0, abs=0.02)
    assert sample.elevation_m == pytest.approx(10.0, abs=0.01)
    assert sample.north_slope_m_per_m == pytest.approx(0.10)
    assert sample.east_slope_m_per_m == pytest.approx(0.05)


def test_exact_ray_intersection_matches_planar_closed_form() -> None:
    grid = _planar_grid()
    ray = (0.6, 0.0, 0.8)

    result = grid.intersect_ray(
        reference_north_m=0.0,
        reference_east_m=0.0,
        height_m=100.0,
        camera_offset_ned=(0.0, 0.0, 0.0),
        ray_ned=ray,
        maximum_slant_range_m=500.0,
        minimum_incidence_component=0.03,
    )

    assert result is not None
    expected = 100.0 / (0.8 + 0.10 * 0.6)
    assert result.slant_range_m == pytest.approx(expected, rel=1.0e-9)
    assert result.north_m == pytest.approx(expected * 0.6)
    assert result.elevation_m == pytest.approx(5.0 + result.north_m * 0.10)
    assert result.incidence_component == pytest.approx(0.86)


def test_bilinear_cross_term_intersection_has_zero_vertical_residual() -> None:
    grid = replace(
        _planar_grid(north_slope=0.0, east_slope=0.0),
        origin_elevation_m=8.0,
        elevations_m=((5.0, 5.0, 5.0), (5.0, 8.0, 11.0), (5.0, 11.0, 17.0)),
    )
    norm = math.sqrt(0.3**2 + 0.4**2 + 0.8660254037844386**2)
    ray = (0.3 / norm, 0.4 / norm, 0.8660254037844386 / norm)

    result = grid.intersect_ray(
        reference_north_m=0.0,
        reference_east_m=0.0,
        height_m=80.0,
        camera_offset_ned=(0.0, 0.0, 0.0),
        ray_ned=ray,
        maximum_slant_range_m=300.0,
        minimum_incidence_component=0.03,
    )

    assert result is not None
    ray_altitude = 8.0 + 80.0 - result.slant_range_m * ray[2]
    assert ray_altitude == pytest.approx(result.elevation_m, abs=1.0e-7)


def test_dem_gps_observation_propagates_vertical_horizontal_and_slope_sigma() -> None:
    grid = _planar_grid(slope_sigma=0.01)
    observation = dem_gps_terrain_observation(
        grid,
        latitude_deg=grid.origin_latitude_deg,
        longitude_deg=grid.origin_longitude_deg,
        gps_absolute_altitude_m=105.0,
        gps_horizontal_sigma_m=2.0,
        gps_vertical_sigma_m=3.0,
        captured_at_s=10.0,
    )

    assert observation.height_m == pytest.approx(100.0)
    expected = math.hypot(
        3.0,
        0.35,
        2.0 * math.hypot(0.10, 0.05),
        math.sqrt(2.0) * 2.0 * 0.01,
    )
    assert observation.height_sigma_m == pytest.approx(expected)
    assert observation.reference_north_m == pytest.approx(0.0)
    assert observation.reference_east_m == pytest.approx(0.0)


def test_ray_intersection_fails_closed_outside_dem_coverage() -> None:
    grid = _planar_grid()
    result = grid.intersect_ray(
        reference_north_m=190.0,
        reference_east_m=0.0,
        height_m=100.0,
        camera_offset_ned=(0.0, 0.0, 0.0),
        ray_ned=(0.8, 0.0, 0.6),
        maximum_slant_range_m=500.0,
        minimum_incidence_component=0.03,
    )

    assert result is None


def test_ray_intersection_returns_none_for_reference_outside_dem() -> None:
    grid = _planar_grid()

    result = grid.intersect_ray(
        reference_north_m=201.0,
        reference_east_m=0.0,
        height_m=100.0,
        camera_offset_ned=(0.0, 0.0, 0.0),
        ray_ned=(0.0, 0.0, 1.0),
        maximum_slant_range_m=500.0,
        minimum_incidence_component=0.03,
    )

    assert result is None
