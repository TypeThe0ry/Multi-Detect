from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from multidetect.autonomy.ground_model import (
    DemGround,
    FlatGround,
    GroundProjection,
    ground_projection_from_config,
)
from multidetect.autonomy.onboard_loop import CameraModel
from multidetect.simulator.camera_projection import pixel_ground_point
from multidetect.terrain_dem import LocalDemGrid, TerrainDemError


def grid(slope_n=0.1, slope_e=0.0) -> LocalDemGrid:
    return LocalDemGrid(
        scenario_id="test",
        origin_latitude_deg=1.3,
        origin_longitude_deg=103.8,
        origin_elevation_m=18.0,
        north_origin_m=-100.0,
        east_origin_m=-100.0,
        north_spacing_m=100.0,
        east_spacing_m=100.0,
        elevations_m=tuple(
            tuple(18 + n * slope_n + e * slope_e for e in (-100, 0, 100)) for n in (-100, 0, 100)
        ),
        vertical_sigma_m=0.1,
        slope_sigma_m_per_m=0.001,
        source_sha256="0" * 64,
    )


@pytest.mark.parametrize("slope_n,slope_e", [(0, 0), (0.1, 0.2), (-0.1, -0.05)])
def test_dem_intersects_analytic_slopes(slope_n, slope_e):
    model = DemGround(grid(slope_n, slope_e))
    origin, ray = (2.0, 3.0, 20.0), (1.0, 0.3, 1.0)
    t = (20 - slope_n * 2 - slope_e * 3) / (1 + slope_n + 0.3 * slope_e)
    assert model.intersect_ray(origin, ray) == pytest.approx((2 + t, 3 + 0.3 * t))


def test_vertical_and_cell_boundary_hits():
    model = DemGround(grid())
    assert model.intersect_ray((100, 100, 20), (0, 0, 1)) == (100, 100)
    assert model.intersect_ray((0, 0, 110), (1, 0, 1)) == pytest.approx((100, 0))


def test_dem_first_intersection_with_ridge_between_positive_endpoints():
    # Along n=e=t, terrain=10t-10t^2. Ray height=1-0.01t:
    # both cell endpoints are above terrain, but the middle is obstructed.
    terrain = LocalDemGrid(
        "ridge",
        1.3,
        103.8,
        18,
        0,
        0,
        1,
        1,
        ((18, 23), (23, 18)),
        0.1,
        0.001,
        "0" * 64,
    )
    hit = DemGround(terrain).intersect_ray((0, 0, 1), (1, 1, 0.01))
    expected = (10.01 - math.sqrt(10.01**2 - 40)) / 20
    assert hit == pytest.approx((expected, expected))


@pytest.mark.parametrize("ray", [(1, 0, 0), (1, 0, -1), (math.nan, 0, 1)])
def test_invalid_and_parallel_rays(ray):
    assert FlatGround().intersect_ray((0, 0, 20), ray) is None
    assert DemGround(grid(), fallback=FlatGround()).intersect_ray((0, 0, 20), ray) is None


def test_dem_coverage_and_range_fallbacks():
    assert DemGround(grid()).intersect_ray((101, 0, 20), (1, 0, 1)) is None
    assert DemGround(grid()).intersect_ray((0, 0, 200), (1, 0, 1)) is None
    assert DemGround(grid(), max_range_m=1).intersect_ray((0, 0, 20), (1, 0, 1)) is None
    model = DemGround(grid(), fallback=FlatGround(5))
    assert model.intersect_ray((101, 0, 20), (1, 0, 1)) == (116, 0)
    assert DemGround(grid()).intersect_ray((0, 0, -1), (1, 0, 1)) is None


def test_dem_explicit_msl_origin_reference():
    # Local z=0 lies 5m below the DEM origin (e.g. a different estimator datum).
    assert DemGround(grid(0), reference_elevation_m=13).intersect_ray(
        (0, 0, 20), (1, 0, 1)
    ) == pytest.approx((15, 0))


def test_ray_parallel_to_sloping_surface_has_no_hit():
    assert DemGround(grid(-1)).intersect_ray((0, 0, 20), (1, 0, 1)) is None


@pytest.mark.parametrize("roll,pitch,yaw", [(12, -8, 30), (-10, 5, -20), (0, 0, 90)])
def test_dem_contact_projection_with_full_vehicle_attitude(roll, pitch, yaw):
    from multidetect.simulator.camera_projection import (
        _world_to_body_ned,
        camera_center_ned_up,
    )

    pose = {
        "local_ned_m": [4.0, -3.0, -20.0],
        "attitude": {"roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw},
    }
    camera = camera_center_ned_up(pose)
    target = (40.0, 20.0)
    up = 0.1 * target[0] + 0.05 * target[1]
    body = _world_to_body_ned(
        (target[0] - camera[0], target[1] - camera[1], camera[2] - up),
        roll_rad=math.radians(roll),
        pitch_rad=math.radians(pitch),
        yaw_rad=math.radians(yaw),
    )
    depth = math.cos(0.18) * body[0] + math.sin(0.18) * body[2]
    vertical = -math.sin(0.18) * body[0] + math.cos(0.18) * body[2]
    u, v = 320 + 270 * body[1] / depth, 240 + 270 * vertical / depth
    result = pixel_ground_point(
        u,
        v,
        pose=pose,
        camera_info={"fx": 270, "fy": 270, "cx": 320, "cy": 240},
        image_size=(640, 480),
        ground_model=DemGround(grid(0.1, 0.05)),
    )
    assert result == pytest.approx(target, abs=1e-9)


def legacy_pixel(u, v, pose, camera):
    # Frozen original (2026-10-15) final intersection, independent of GroundModel.
    from multidetect.simulator.camera_projection import (
        _body_to_world_ned,
        camera_center_ned_up,
    )

    centre = camera_center_ned_up(pose)
    p = 0.18
    x, z = (u - camera["cx"]) / camera["fx"], (v - camera["cy"]) / camera["fy"]
    n, e, d = _body_to_world_ned(
        (math.cos(p) - math.sin(p) * z, x, math.sin(p) + math.cos(p) * z),
        **{
            f"{k}_rad": math.radians(pose["attitude"][f"{k}_deg"]) for k in ("roll", "pitch", "yaw")
        },
    )
    if d <= 1e-6 or centre[2] <= 0:
        return None
    t = centre[2] / d
    return centre[0] + n * t, centre[1] + e * t


@pytest.mark.parametrize("roll,pitch,yaw", [(0, 0, 0), (12, -8, 123), (-5, 4, -179)])
@pytest.mark.parametrize("u,v", [(320, 240), (400, 400), (10, 10), (639, 479)])
def test_full_projection_default_float_bytes_unchanged(roll, pitch, yaw, u, v):
    pose = {
        "local_ned_m": [4.0, -3.0, -20.0],
        "attitude": {"roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw},
    }
    cam = {"fx": 270.0, "fy": 270.0, "cx": 320.0, "cy": 240.0}
    expected = legacy_pixel(u, v, pose, cam)
    for model in (None, FlatGround()):
        actual = pixel_ground_point(
            u, v, pose=pose, camera_info=cam, image_size=(640, 480), ground_model=model
        )
        assert actual == expected
        if actual is not None:
            assert struct.pack("dd", *actual) == struct.pack("dd", *expected)


def test_level_camera_default_exact_and_dem_opt_in():
    camera = CameraModel(270, 270, 320, 240, 640, 480)
    for u, v, yaw in ((320, 240, 0), (400, 300, 123), (600, 479, -70)):
        x, y = (u - 320) / 270, (v - 240) / 270
        forward, down = math.cos(0.18) - y * math.sin(0.18), math.sin(0.18) + y * math.cos(0.18)
        t, angle = 20 / down, math.radians(yaw)
        f, r = forward * t, x * t
        expected = (
            4 + f * math.cos(angle) - r * math.sin(angle),
            -3 + f * math.sin(angle) + r * math.cos(angle),
        )
        assert camera.ground_point(u, v, north=4, east=-3, height_m=20, yaw_deg=yaw) == expected
    assert camera.ground_point(
        320, 240, north=0, east=0, height_m=20, yaw_deg=0, ground_model=DemGround(grid())
    ) == pytest.approx((20 / (math.tan(0.18) + 0.1), 0))


def test_config_default_and_explicit_dem(tmp_path):
    assert ground_projection_from_config(None, base_dir=tmp_path) == GroundProjection()
    source = Path("simulation/terrain/gen_v2_w0035.dem.json")
    (tmp_path / "terrain.json").write_bytes(source.read_bytes())
    settings = ground_projection_from_config(
        {"type": "dem", "path": "terrain.json", "fallback": "flat", "use_agl": True},
        base_dir=tmp_path,
    )
    assert isinstance(settings.model, DemGround) and settings.use_agl
    assert settings.model.grid.origin_elevation_m == 18
    assert isinstance(settings.model.fallback, FlatGround)
    with pytest.raises(TerrainDemError, match="not a regular file"):
        ground_projection_from_config({"type": "dem", "path": "missing"}, base_dir=tmp_path)


@pytest.mark.parametrize(
    "config",
    [{"type": "oops"}, {"use_agl": "yes"}, {"agl_max_age_s": 0}, {"ground_up_m": math.nan}],
)
def test_invalid_config(config, tmp_path):
    with pytest.raises(ValueError):
        ground_projection_from_config(config, base_dir=tmp_path)


@pytest.mark.parametrize(
    "measurement",
    [
        None,
        {},
        {"height_m": -1, "age_s": 0},
        {"height_m": math.nan, "age_s": 0},
        {"height_m": 10, "age_s": 2},
        {"height_m": 10, "age_s": -0.1},
    ],
)
def test_agl_missing_invalid_stale_falls_back(measurement):
    model = DemGround(grid())
    obs = {"pose": {"local_ned_m": [0, 0, -20]}, "ground_agl": measurement}
    assert GroundProjection(model, True).for_observation(obs) is model


def test_agl_opt_in_plane_is_vehicle_up_minus_agl():
    obs = {"pose": {"local_ned_m": [0, 0, -20]}, "ground_agl": {"height_m": 12, "age_s": 0.2}}
    assert GroundProjection().for_observation(obs) is None
    model = GroundProjection(use_agl=True).for_observation(obs)
    assert model == FlatGround(8)
    assert model.intersect_ray((0.18, 0, 20.12), (1, 0, 1)) == (12.3, 0)
