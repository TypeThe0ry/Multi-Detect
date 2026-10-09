"""Paired open-loop wind experiments; not route-following or energy calibration."""

import math

import pytest

from multidetect.simulator import DeterministicFlightModel, FlightModelConfig
from multidetect.simulator.flight_model import SimulationEnergyProfile
from multidetect.target_geolocation import wgs84_forward_from_ned


@pytest.mark.parametrize("heading", [0, 90, 180, 270])
@pytest.mark.parametrize("along,cross", [(5, 0), (-5, 0), (0, 5), (0, -5)])
def test_wind_changes_actual_trajectory_but_not_equal_control_energy(heading, along, cross):
    direction = math.radians(heading)
    east_unit, north_unit = math.sin(direction), math.cos(direction)
    wind_east = along * east_unit + cross * north_unit
    wind_north = along * north_unit - cross * east_unit
    config = FlightModelConfig(
        initial_heading_deg=heading,
        initial_speed_mps=20,
        initial_armed=True,
        initial_latitude_deg=1.3,
        initial_longitude_deg=103.8,
    )
    profile = SimulationEnergyProfile(100, 36, 324, "synthetic wind-pair fixture")
    calm = DeterministicFlightModel("fixed_wing", config=config, energy_profile=profile)
    windy = DeterministicFlightModel(
        "fixed_wing",
        config=config,
        energy_profile=profile,
        wind_mps=(wind_east, wind_north),
    )
    # The fixed-wing model maps throttle to 8 + throttle * (45 - 8).
    control = {"throttle": 12 / 37}
    for _ in range(10):
        calm.step(control, 1)
        windy.step(control, 1)
    assert calm.airspeed_mps == pytest.approx(20)
    assert windy.airspeed_mps == pytest.approx(20)
    assert windy.ground_speed_mps == pytest.approx(math.hypot(20 + along, cross))
    assert calm.ground_speed_mps == pytest.approx(20)
    expected = wgs84_forward_from_ned(
        latitude_deg=1.3,
        longitude_deg=103.8,
        north_offset_m=(20 * north_unit + wind_north) * 10,
        east_offset_m=(20 * east_unit + wind_east) * 10,
    )
    assert windy.latitude_deg == pytest.approx(expected[0], abs=1e-10)
    assert windy.longitude_deg == pytest.approx(expected[1], abs=1e-10)
    assert (windy.latitude_deg, windy.longitude_deg) != (calm.latitude_deg, calm.longitude_deg)
    assert windy.battery_remaining_pct == calm.battery_remaining_pct
    assert windy.battery_remaining_pct < 100
    assert windy.evidence_hash != calm.evidence_hash


def test_wind_does_not_move_disarmed_fixture():
    model = DeterministicFlightModel(wind_mps=(20, -20))
    before = (model.latitude_deg, model.longitude_deg)
    model.step({"throttle": 1}, 10)
    assert (model.latitude_deg, model.longitude_deg) == before
    assert model.ground_speed_mps == 0
