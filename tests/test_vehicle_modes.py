from __future__ import annotations

from multidetect.autonomy.energy_estimator import EnergyConfig, EnergyParams, OnboardEnergyEstimator
from multidetect.autonomy.mission_supervisor import RecoveryPolicy
from multidetect.autonomy.safety_envelope import (
    SafetyEnvelope,
    SafetyEnvelopeConfig,
    VehicleDynamics,
)
from multidetect.sim_teacher.primitives import ActionPrimitive


class ArcObstacle:
    def segment_clear(self, start, end):
        return not (max(start[0], end[0]) > 5 and max(start[1], end[1]) > 5)


def test_fixed_wing_envelope_airspeed_and_arc_sampling():
    dynamics = VehicleDynamics(
        category="fixed_wing",
        can_hover=False,
        min_airspeed_mps=12,
        max_airspeed_mps=25,
        min_turn_radius_m=10,
        max_bank_deg=30,
    )
    envelope = SafetyEnvelope(
        SafetyEnvelopeConfig(vehicle=dynamics), home_ned=(0, 0, 0), obstacle_source=ArcObstacle()
    )
    assert envelope.apply(
        ActionPrimitive.translate(5, 0), pose_ned=(0, 0, -20), airspeed_mps=10
    ).rejected
    accepted = envelope.apply(
        ActionPrimitive.translate(5, 0), pose_ned=(0, 0, -20), airspeed_mps=15
    )
    assert not accepted.rejected
    turn = envelope.apply(
        ActionPrimitive.turn(90), pose_ned=(0, 0, -20), yaw_deg=0, airspeed_mps=15
    )
    assert turn.rejected and turn.recovery_action == "loiter"


def test_vtol_transition_uses_joint_constraints():
    dynamics = VehicleDynamics(
        category="vtol", can_hover=True, min_airspeed_mps=12, max_bank_deg=40
    )
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(vehicle=dynamics), home_ned=(0, 0, 0))
    low = envelope.apply(
        ActionPrimitive.hold(1), pose_ned=(0, 0, -2), mode="transition", airspeed_mps=14
    )
    assert low.rejected and "transition_altitude" in low.reasons
    steep = envelope.apply(
        ActionPrimitive.turn(10),
        pose_ned=(0, 0, -20),
        mode="transition",
        airspeed_mps=14,
        bank_deg=25,
    )
    assert steep.rejected and "bank" in steep.reasons


def test_fixed_wing_energy_includes_approach_loiter_and_glide():
    params = EnergyParams(
        category="fixed_wing",
        can_hover=False,
        min_airspeed_mps=12,
        cruise_airspeed_mps=17,
        cruise_power_w=300,
        min_turn_radius_m=40,
        approach_distance_m=100,
        glide_sink_rate_mps=1.2,
    )
    config = EnergyConfig(
        capacity_wh=100,
        reserve_energy_wh=10,
        nominal_voltage_v=22.2,
        cutoff_voltage_v=18.2,
        vehicle=params,
    )
    model = OnboardEnergyEstimator(config)
    model.ingest(
        {
            "mavpackettype": "BATTERY_STATUS",
            "id": 0,
            "voltages": [22200],
            "battery_remaining": 80,
            "current_battery": 2000,
        },
        received_at_s=0,
    )
    result = model.estimate(
        now_s=0,
        pose_ned=(300, 0, -100),
        home_ne=(0, 0),
        return_altitude_m=50,
        mission_remaining_s=1000,
    )
    assert result["category"] == "fixed_wing"
    assert result["route_distance_m"] > result["distance_home_m"]
    assert result["return_segments"]["loiter_wh"] > 0
    assert result["glide_distance_m"] > 0


def test_recovery_sequences_are_explicit_and_fail_closed():
    assert RecoveryPolicy(category="fixed_wing").sequence() == ("loiter", "return_home", "fw_land")
    assert RecoveryPolicy(category="vtol").sequence() == (
        "loiter",
        "return_home",
        "vtol_transition_to_mc",
        "land_now",
    )
    assert RecoveryPolicy(category="vtol", parachute_available=True).sequence(
        energy_available=False
    ) == ("parachute",)
