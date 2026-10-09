"""Analytic and actual deterministic-flight integration checks."""

import math

import pytest

from multidetect.simulation.wind_triangle import solve_wind_triangle
from multidetect.simulator import DeterministicFlightModel, FlightModelConfig
from multidetect.target_geolocation import wgs84_forward_from_ned


@pytest.mark.parametrize("track", [0, 45, 90, 180, 270, 359])
@pytest.mark.parametrize("along,cross", [(0, 0), (5, 0), (-5, 0), (0, 5), (0, -5), (4, 8)])
def test_compensated_heading_holds_track_in_actual_model(track, along, cross):
    angle = math.radians(track)
    north, east = math.cos(angle), math.sin(angle)
    wn, we = along * north - cross * east, along * east + cross * north
    solution = solve_wind_triangle(
        track_deg=track,
        airspeed_mps=20,
        wind_north_mps=wn,
        wind_east_mps=we,
    )
    assert solution.feasible
    expected_speed = math.sqrt(400 - cross * cross) + along
    assert solution.along_track_speed_mps == pytest.approx(expected_speed)
    model = DeterministicFlightModel(
        "fixed_wing",
        wind_mps=(we, wn),
        config=FlightModelConfig(
            initial_heading_deg=solution.heading_deg,
            initial_speed_mps=20,
            initial_armed=True,
            initial_latitude_deg=1.3,
            initial_longitude_deg=103.8,
        ),
    )
    for _ in range(100):
        model.step({"throttle": 12 / 37}, 0.1)
    expected = wgs84_forward_from_ned(
        latitude_deg=1.3,
        longitude_deg=103.8,
        north_offset_m=expected_speed * north * 10,
        east_offset_m=expected_speed * east * 10,
    )
    assert model.latitude_deg == pytest.approx(expected[0], abs=1e-10)
    assert model.longitude_deg == pytest.approx(expected[1], abs=1e-10)
    assert model.ground_speed_mps == pytest.approx(expected_speed)


@pytest.mark.parametrize(
    "north,east,reason",
    [
        (0, 21, "crosswind_exceeds_airspeed"),
        (0, -21, "crosswind_exceeds_airspeed"),
        (-20, 0, "insufficient_forward_progress"),
        (-25, 0, "insufficient_forward_progress"),
        (0, 20, "insufficient_forward_progress"),
    ],
)
def test_impossible_legs_do_not_get_artificial_speed_floor(north, east, reason):
    result = solve_wind_triangle(
        track_deg=0,
        airspeed_mps=20,
        wind_north_mps=north,
        wind_east_mps=east,
    )
    assert not result.feasible
    assert result.reason == reason
    assert result.heading_deg is None
    assert result.along_track_speed_mps is None


def test_boundary_crosswind_and_explicit_progress_margin():
    inputs = dict(track_deg=0, airspeed_mps=20, wind_north_mps=5, wind_east_mps=20)
    result = solve_wind_triangle(**inputs)
    assert result.feasible
    assert result.heading_deg == 270
    assert result.along_track_speed_mps == 5
    assert not solve_wind_triangle(**inputs, minimum_progress_mps=5).feasible


@pytest.mark.parametrize("track", [-720, 720, 1e25])
def test_heading_normalization_preserves_wind_correction(track):
    inputs = dict(airspeed_mps=20, wind_north_mps=0, wind_east_mps=5)
    assert solve_wind_triangle(track_deg=track, **inputs) == solve_wind_triangle(
        track_deg=track % 360,
        **inputs,
    )


def test_return_route_headwind_and_tailwind_times_do_not_cancel():
    speeds = [
        solve_wind_triangle(
            track_deg=track,
            airspeed_mps=20,
            wind_north_mps=5,
            wind_east_mps=0,
        ).along_track_speed_mps
        for track in (0, 180)
    ]
    duration = sum(1000 / speed for speed in speeds)
    assert duration == pytest.approx(106.6666666667)
    assert duration > 2000 / 20
    assert duration < 2000 / (20 - 5)


@pytest.mark.parametrize(
    "field",
    [
        "track_deg",
        "airspeed_mps",
        "wind_north_mps",
        "wind_east_mps",
        "minimum_progress_mps",
    ],
)
@pytest.mark.parametrize("bad", [True, "5", None, float("nan"), float("inf")])
def test_invalid_inputs_fail_closed(field, bad):
    inputs = dict(track_deg=0, airspeed_mps=20, wind_north_mps=0, wind_east_mps=0)
    inputs[field] = bad
    with pytest.raises(ValueError):
        solve_wind_triangle(**inputs)


@pytest.mark.parametrize("speed,margin", [(0, 0), (-1, 0), (20, -1)])
def test_invalid_speed_and_margin(speed, margin):
    with pytest.raises(ValueError):
        solve_wind_triangle(
            track_deg=0,
            airspeed_mps=speed,
            wind_north_mps=0,
            wind_east_mps=0,
            minimum_progress_mps=margin,
        )
