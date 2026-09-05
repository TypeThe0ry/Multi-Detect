from __future__ import annotations

import hashlib
import json

import pytest

from multidetect.clock import ManualClock
from multidetect.simulator import (
    ControlAuthorityState,
    FlightMode,
    SimulatorClosedLoop,
    SimulatorClosedLoopError,
    SimulatorMavlinkControlConfig,
    SimulatorTelemetrySnapshot,
)


def test_ai_and_manual_use_one_closed_loop_path() -> None:
    sim = SimulatorClosedLoop(FlightMode.VTOL, seed=123, wind_mps=(2.0, -1.0))
    assert sim.telemetry.armed is False
    assert sim.arm() is True
    sim.enable_ai()
    first = sim.step()
    assert first.decision is not None
    assert first.decision.mode.value == "hover"
    assert first.decision.accepted is True
    assert first.telemetry_after.step_count == 1
    ai_messages = sim.connection.mav.messages

    manual = sim.submit_manual(roll=0.25, pitch=0.10, yaw=0.20, throttle=0.70)
    assert manual.source == "manual"
    assert sim.authority.state is ControlAuthorityState.MANUAL
    suppressed = sim.step()
    assert suppressed.decision is not None
    assert suppressed.decision.mode.value == "suppressed"
    assert suppressed.decision.accepted is False
    assert len(ai_messages) == 2
    assert suppressed.telemetry_after.step_count == 2

    sim.release_manual_control()
    resumed = sim.step()
    assert resumed.decision is not None
    assert resumed.decision.mode.value == "hover"
    assert resumed.decision.accepted is True
    assert sim.authority.state is ControlAuthorityState.AI
    sim.close()


def test_sensor_observation_override_reaches_the_same_ai_policy() -> None:
    sim = SimulatorClosedLoop(seed=5)
    sim.arm()
    sim.enable_ai()
    observed = SimulatorTelemetrySnapshot(
        observed_at_s=0.0,
        altitude_agl_m=10.0,
        target_visible=True,
        target_bearing_deg=20.0,
        target_distance_m=90.0,
        target_confidence=0.9,
        link_healthy=True,
        armed=True,
    )
    tick = sim.step_with_observation(observed)
    assert tick.decision is not None
    assert tick.decision.mode.value == "track"
    assert tick.decision.accepted is True
    sim.close()


def test_closed_loop_ai_waits_for_explicit_arm_and_command_sink_updates_state() -> None:
    sim = SimulatorClosedLoop(seed=17)
    try:
        assert sim.telemetry.armed is False
        sim.enable_ai()
        waiting = sim.step()
        assert waiting.decision is not None
        assert waiting.decision.mode.value == "paused"
        assert sim.connection.mav.messages == []
        # Use the generated MAVLink-shaped sink, as QGC/PX4 would.
        sim.connection.mav.command_long_send(
            1, 1, 400, 0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        )
        assert sim.telemetry.armed is True
        released = sim.step()
        assert released.decision is not None
        assert released.decision.mode.value == "hover"
        assert released.decision.accepted is True
    finally:
        sim.close()


def test_fixed_wing_signed_wire_mode_and_deterministic_evidence() -> None:
    config = SimulatorMavlinkControlConfig(throttle_wire_mode="signed")
    first = SimulatorClosedLoop(
        FlightMode.FIXED_WING,
        seed=99,
        wind_mps=(4.0, 1.0),
        control_config=config,
    )
    second = SimulatorClosedLoop(
        FlightMode.FIXED_WING,
        seed=99,
        wind_mps=(4.0, 1.0),
        control_config=config,
    )
    for sim in (first, second):
        sim.arm()
        sim.enable_ai()
        sim.run(20, dt_s=0.1)
    assert first.telemetry == second.telemetry
    assert first.evidence_hash == second.evidence_hash
    # The initial altitude controller commands 0.8 throttle; signed wire
    # encoding therefore yields z=+600 (midpoint 0.5 would be z=0).
    assert first.connection.mav.messages[0][3] == 600
    assert first.telemetry.airspeed_mps > 0.0
    first.close()
    second.close()


def test_multirotor_closed_loop_uses_shared_ai_manual_watchdog_path() -> None:
    sim = SimulatorClosedLoop(FlightMode.MULTIROTOR, seed=101)
    try:
        sim.arm()
        sim.enable_ai()
        step = sim.step()
        assert step.decision is not None
        assert step.decision.accepted is True
        assert step.telemetry_after.mode == FlightMode.MULTIROTOR.value
        assert step.telemetry_after.ground_speed_mps > 0.0
        assert sim.evidence()["flight_model"]["mode"] == "multirotor"
    finally:
        sim.close()


def test_watchdog_fails_closed_before_a_long_physics_tick() -> None:
    sim = SimulatorClosedLoop(
        FlightMode.VTOL,
        control_config=SimulatorMavlinkControlConfig(watchdog_s=0.25),
    )
    sim.arm()
    sim.enable_ai()
    tick = sim.step(dt_s=0.5)
    assert tick.watchdog_receipt is not None
    assert tick.watchdog_receipt.source == "watchdog"
    # QGC legacy neutral is z=500, i.e. the hover throttle, so the model holds
    # altitude while the watchdog transitions authority to failsafe.
    assert tick.telemetry_after.altitude_agl_m == pytest.approx(10.0)
    assert sim.authority.state is ControlAuthorityState.FAILSAFE
    assert sim.connection.mav.messages[-1][3] == 500
    sim.close()


def test_evidence_hash_and_close_contract() -> None:
    sim = SimulatorClosedLoop(seed=7)
    sim.step()
    evidence = sim.evidence()
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert sim.evidence_hash == hashlib.sha256(encoded.encode()).hexdigest()
    assert sim.evidence_with_hash()["sha256"] == sim.evidence_hash
    assert evidence["truth_free"] is True
    sim.close()
    sim.close()
    with pytest.raises(SimulatorClosedLoopError, match="closed"):
        sim.step()


def test_closed_loop_uses_one_injected_clock_for_ai_watchdog_and_evidence() -> None:
    clock = ManualClock()
    sim = SimulatorClosedLoop(clock=clock, seed=21)
    try:
        assert sim.clock is clock
        assert sim.time_s == 0.0
        sim.arm()
        sim.enable_ai()
        step = sim.step(dt_s=0.25)
        assert step.telemetry_after.observed_at_s == pytest.approx(0.25)
        assert sim.time_s == pytest.approx(0.25)
        assert clock.monotonic() == pytest.approx(0.25)
        evidence = sim.evidence()
        assert evidence["clock"]["shared_with_runtime"] is True
        assert evidence["clock"]["monotonic_s"] == pytest.approx(0.25)
    finally:
        sim.close()


def test_closed_loop_rejects_a_clock_that_starts_in_another_time_domain() -> None:
    with pytest.raises(SimulatorClosedLoopError, match="clock must start"):
        SimulatorClosedLoop(clock=ManualClock(initial_monotonic_s=1.0))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dt_s": 0.0},
        {"dt_s": -0.1},
        {"dt_s": float("nan")},
        {"initial_authority": "bogus"},
    ],
)
def test_invalid_closed_loop_configuration_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises((SimulatorClosedLoopError, ValueError)):
        SimulatorClosedLoop(**kwargs)
