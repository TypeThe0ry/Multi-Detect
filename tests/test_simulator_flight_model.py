"""Contract tests for the deterministic flight-model fallback."""

from __future__ import annotations

import hashlib
import json

import pytest

from multidetect.simulator import (
    MAV_CMD_COMPONENT_ARM_DISARM,
    MAV_CMD_DO_VTOL_TRANSITION,
    DeterministicFlightModel,
    FlightControlSetpoint,
    FlightMode,
    FlightModelConfig,
    FlightModelError,
    FlightTelemetrySnapshot,
)
from multidetect.simulator.flight_model import SimulationEnergyProfile
from multidetect.target_geolocation import wgs84_forward_from_ned


@pytest.mark.parametrize("mode", list(FlightMode))
def test_energy_persists_across_disarm_and_multiple_sorties(mode) -> None:
    profile = SimulationEnergyProfile(10.0, 36.0, 324.0, "test assumption, not calibrated")
    model = DeterministicFlightModel(mode, energy_profile=profile)
    model.set_armed(True)
    model.step({"throttle": 1.0}, 10.0)
    assert model.battery_remaining_pct == pytest.approx(90.0)
    model.set_armed(False)
    assert model.battery_remaining_pct == pytest.approx(90.0)
    model.step({"throttle": 1.0}, 10.0)
    assert model.battery_remaining_pct == pytest.approx(89.0)
    model.set_armed(True)
    model.step({"throttle": 0.5}, 10.0)
    assert model.battery_remaining_pct == pytest.approx(83.5)
    assert model.telemetry.battery_remaining_pct == pytest.approx(83.5)
    energy = model.energy_observation()
    assert energy["remaining_wh"] == pytest.approx(8.35)
    assert energy["estimated_flight_time_s"] == pytest.approx(83.5)
    assert energy["reserve_deducted"] is False
    assert energy["physical_calibration_claimed"] is False
    assert model.evidence()["energy"] == energy


def test_energy_recharge_is_explicit_guarded_and_audited() -> None:
    model = DeterministicFlightModel(
        config=FlightModelConfig(initial_altitude_m=0.0, initial_battery_remaining_pct=50),
        energy_profile=SimulationEnergyProfile(10, 36, 324, "fixture"),
    )
    model.recharge_energy(2, reason="explicit test charge")
    assert model.battery_remaining_pct == 70
    assert model.command_history[-1]["added_wh"] == pytest.approx(2)
    model.recharge_energy(100, reason="fill")
    assert model.battery_remaining_pct == 100
    assert model.command_history[-1]["added_wh"] == pytest.approx(3)
    model.set_armed(True)
    before = model.evidence_hash
    with pytest.raises(FlightModelError, match="ground"):
        model.recharge_energy(1, reason="invalid")
    assert model.evidence_hash == before
    with pytest.raises(FlightModelError, match="override"):
        model.set_battery_remaining(100)


def test_energy_dt_replay_and_episode_reset_are_deterministic() -> None:
    def run():
        model = DeterministicFlightModel(
            energy_profile=SimulationEnergyProfile(1, 360, 0, "fixture")
        )
        initial = model.evidence_hash
        model.step({}, 10)
        assert model.battery_remaining_pct == 0
        model.step({}, 10)
        assert model.battery_remaining_pct == 0
        digest = model.evidence_hash
        model.reset()  # Explicit whole-episode reset, not a sortie boundary.
        assert model.evidence_hash == initial
        return digest

    assert run() == run()


@pytest.mark.parametrize(
    "capacity,idle,propulsion,source",
    [
        (0, 1, 1, "fixture"),
        (float("nan"), 1, 1, "fixture"),
        (True, 1, 1, "fixture"),
        (1, -1, 1, "fixture"),
        (1, 0, 0, "fixture"),
        (1, 1, float("inf"), "fixture"),
        (1, 1, 1, ""),
    ],
)
def test_energy_profile_rejects_invalid_assumptions(capacity, idle, propulsion, source) -> None:
    with pytest.raises(FlightModelError):
        SimulationEnergyProfile(capacity, idle, propulsion, source)


def _run(mode: FlightMode) -> tuple[object, str]:
    model = DeterministicFlightModel(mode, seed=42, wind_mps=(3.0, -1.0))
    for index in range(30):
        model.step(
            FlightControlSetpoint(
                roll=0.8, pitch=0.25, yaw=0.2, throttle=0.75, timestamp_s=index * 0.1
            ),
            0.1,
        )
    return model.telemetry, model.evidence_hash


@pytest.mark.parametrize("mode", [FlightMode.VTOL, FlightMode.FIXED_WING, FlightMode.MULTIROTOR])
def test_deterministic_bounded_truth_free_telemetry(mode: FlightMode) -> None:
    snapshot, digest = _run(mode)
    assert isinstance(snapshot, FlightTelemetrySnapshot)
    assert snapshot.to_mapping()["truth_free"] is True
    assert snapshot.mode == mode.value
    assert 0.0 <= snapshot.airspeed_mps <= 45.0
    assert 0.0 <= snapshot.altitude_agl_m <= 2_000.0
    assert -55.0 <= snapshot.roll_deg <= 55.0
    assert -30.0 <= snapshot.pitch_deg <= 30.0
    assert 0.0 <= snapshot.heading_deg < 360.0
    assert -90.0 <= snapshot.latitude_deg <= 90.0
    assert -180.0 <= snapshot.longitude_deg <= 180.0
    assert len(digest) == 64


def test_same_seed_wind_and_commands_have_same_evidence() -> None:
    first, first_hash = _run(FlightMode.VTOL)
    second, second_hash = _run(FlightMode.VTOL)
    assert first == second
    assert first_hash == second_hash


def test_evidence_hash_is_sha256_of_canonical_evidence() -> None:
    model = DeterministicFlightModel(seed=9)
    model.step({"roll": 0.1, "pitch": 0.0, "yaw": 0.0, "throttle": 0.5}, 0.2)
    encoded = json.dumps(model.evidence(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert model.evidence_hash == hashlib.sha256(encoded.encode()).hexdigest()
    assert model.evidence_with_hash()["sha256"] == model.evidence_hash


def test_invalid_and_backwards_time_are_rejected_without_advancing() -> None:
    model = DeterministicFlightModel()
    for dt in (0.0, -0.1, float("nan"), float("inf")):
        with pytest.raises(FlightModelError):
            model.step(FlightControlSetpoint(throttle=0.5), dt)
    model.step(FlightControlSetpoint(throttle=0.5, timestamp_s=2.0), 0.1)
    before = model.telemetry
    with pytest.raises(FlightModelError, match="backwards"):
        model.step(FlightControlSetpoint(throttle=0.5, timestamp_s=1.0), 0.1)
    assert model.telemetry == before


def test_bounds_are_configurable_and_controls_are_validated() -> None:
    config = FlightModelConfig(max_speed_mps=12.0, max_altitude_m=25.0, initial_altitude_m=3.0)
    model = DeterministicFlightModel("fixed_wing", config=config)
    with pytest.raises(FlightModelError):
        model.step({"roll": 2.0, "throttle": 0.4}, 0.1)
    with pytest.raises(FlightModelError):
        model.step({"roll": 0.0, "throttle": 0.4}, -1.0)
    for _ in range(100):
        model.step({"pitch": 1.0, "throttle": 1.0}, 0.2)
    assert model.speed_mps <= 12.0
    assert model.altitude_m <= 25.0


def test_model_starts_disarmed_and_explicit_arm_updates_snapshot() -> None:
    model = DeterministicFlightModel()
    assert model.telemetry.armed is False
    before = model.telemetry
    # A disarmed model remains stationary even when a stale actuator command
    # is presented; no implicit launch/arm occurs in ``step``.
    after = model.step(FlightControlSetpoint(pitch=0.5, yaw=0.5, throttle=1.0), 0.5)
    assert after.armed is False
    assert after.latitude_deg == pytest.approx(before.latitude_deg)
    assert after.longitude_deg == pytest.approx(before.longitude_deg)
    assert model.arm() is True
    assert model.telemetry.armed is True
    moved = model.step(FlightControlSetpoint(pitch=0.2, yaw=0.1, throttle=0.8), 0.5)
    assert moved.armed is True
    assert moved.step_count == after.step_count + 1


def test_model_emits_gps_observation_timestamp_and_clears_it_for_degraded_fix() -> None:
    model = DeterministicFlightModel()
    assert model.telemetry.gps_observed_at_s == pytest.approx(0.0)

    model.arm()
    model.step({"throttle": 0.5}, 0.25)
    assert model.telemetry.gps_observed_at_s == pytest.approx(model.time_s)

    model.set_gps_status(
        fix_type=2,
        horizontal_accuracy_m=1.5,
        vertical_accuracy_m=2.5,
        satellites_visible=12,
    )
    assert model.telemetry.gps_observed_at_s is None

    model.set_gps_status(
        fix_type=3,
        horizontal_accuracy_m=1.5,
        vertical_accuracy_m=float("nan"),
        satellites_visible=12,
    )
    assert model.telemetry.gps_observed_at_s is None

    model.set_gps_status(
        fix_type=3,
        horizontal_accuracy_m=1.5,
        vertical_accuracy_m=2.5,
        satellites_visible=12,
    )
    assert model.telemetry.gps_observed_at_s == pytest.approx(model.time_s)

    model.set_gps_status(
        fix_type=3,
        horizontal_accuracy_m=float("nan"),
        vertical_accuracy_m=2.5,
        satellites_visible=12,
    )
    assert model.telemetry.gps_observed_at_s is None

    model.set_gps_status(
        fix_type=3,
        horizontal_accuracy_m=1.5,
        vertical_accuracy_m=2.5,
        satellites_visible=255,
    )
    assert model.telemetry.gps_observed_at_s is None


def test_model_command_subset_and_unknown_commands_fail_closed() -> None:
    model = DeterministicFlightModel()
    assert model.command_long(MAV_CMD_COMPONENT_ARM_DISARM, 2.0) is False
    assert model.armed is False
    assert model.command_long(999_999, 1.0) is False
    assert model.armed is False
    assert model.command_long(MAV_CMD_COMPONENT_ARM_DISARM, 1.0) is True
    assert model.armed is True
    assert model.command_long(MAV_CMD_DO_VTOL_TRANSITION, 4.0) is True
    assert model.flight_mode == "FW"
    assert model.telemetry.flight_mode == "FW"
    assert model.command_history[-1]["accepted"] is True


def test_plain_multirotor_uses_hover_dynamics_and_rejects_vtol_transition() -> None:
    model = DeterministicFlightModel("quadcopter", seed=11)
    assert model.mode is FlightMode.MULTIROTOR
    model.arm()
    before = model.telemetry
    after = model.step(FlightControlSetpoint(pitch=0.15, yaw=0.1, throttle=0.75), 0.25)
    assert after.mode == FlightMode.MULTIROTOR.value
    assert after.armed is True
    assert after.ground_speed_mps > before.ground_speed_mps
    assert model.command_long(MAV_CMD_DO_VTOL_TRANSITION, 4.0) is False
    assert model.flight_mode == "MANUAL"
    assert model.command_history[-1]["reason"] == "unsupported_for_mode"


def test_gps_rtk_fix_types_are_preserved_through_the_sensor_contract() -> None:
    model = DeterministicFlightModel(config=FlightModelConfig(initial_gps_fix_type=8))
    assert model.telemetry.gps_fix_type == 8
    model.set_gps_status(
        fix_type=7,
        horizontal_accuracy_m=0.2,
        vertical_accuracy_m=0.4,
        satellites_visible=20,
    )
    assert model.telemetry.gps_fix_type == 7
    assert model.telemetry.gps_quality_valid is True


def test_flight_model_uses_shared_wgs84_endpoint_at_high_latitude() -> None:
    model = DeterministicFlightModel(
        "multirotor",
        config=FlightModelConfig(
            initial_latitude_deg=60.0,
            initial_longitude_deg=10.0,
            initial_heading_deg=0.0,
            initial_armed=True,
        ),
    )

    # A one-second full-throttle step accelerates from rest to 16 m/s and
    # travels 16 m north.  Compare the telemetry endpoint against the same
    # WGS-84 primitive used by target geolocation rather than a metres/degree
    # approximation.
    snapshot = model.step({"throttle": 1.0}, 1.0)
    expected_latitude, expected_longitude = wgs84_forward_from_ned(
        latitude_deg=60.0,
        longitude_deg=10.0,
        north_offset_m=16.0,
        east_offset_m=0.0,
    )
    assert snapshot.latitude_deg == pytest.approx(expected_latitude, abs=1e-12)
    assert snapshot.longitude_deg == pytest.approx(expected_longitude, abs=1e-12)


def test_flight_model_wraps_wgs84_longitude_at_dateline() -> None:
    model = DeterministicFlightModel(
        "multirotor",
        config=FlightModelConfig(
            initial_latitude_deg=1.0,
            initial_longitude_deg=179.9999,
            initial_heading_deg=90.0,
            initial_armed=True,
        ),
    )

    snapshot = model.step({"throttle": 1.0}, 1.0)
    assert -180.0 <= snapshot.longitude_deg < -179.999


def test_flight_model_fails_closed_before_crossing_a_geographic_pole() -> None:
    model = DeterministicFlightModel(
        "multirotor",
        config=FlightModelConfig(
            initial_latitude_deg=89.9999,
            initial_longitude_deg=10.0,
            initial_heading_deg=0.0,
            initial_armed=True,
        ),
    )

    with pytest.raises(FlightModelError, match="crosses a geographic pole"):
        model.step({"throttle": 1.0}, 1.0)


@pytest.mark.parametrize("latitude_deg", [-90.0, 90.0])
def test_model_rejects_exact_pole_origin(latitude_deg: float) -> None:
    with pytest.raises(FlightModelError, match=r"within \(-90, 90\)"):
        DeterministicFlightModel(config=FlightModelConfig(initial_latitude_deg=latitude_deg))
