"""Offline collection wiring: no runtime, sockets or simulator required."""

from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.streams import MavlinkState
from multidetect.sim_teacher.target_tracking import WorldTarget
from scripts import collect_sim_teacher_episodes as collector


def plan(**options):
    return collector.CollectionPlan(
        "scenario", "runtime", "truth", "explore", 1, 1, 0, "out", **options
    )


def test_explicit_track_config_enables_supported_host_loop_without_changing_policy():
    target = WorldTarget(42, -93, 300)
    policy = SimpleNamespace()
    profile = collector._airframe_profile("delta_tailsitter_gimbal_cam")
    config = collector._collection_gimbal_config(
        plan(gimbal_mode="track", track_targets=(target,)), policy, profile
    )
    assert config == ({"roll": 0.0, "pitch": -45.0, "yaw": 0.0}, "track", "follow", (target,))
    assert vars(policy) == {}


def test_existing_policy_stabilization_default_is_preserved():
    policy = SimpleNamespace(cruise_gimbal_target={"roll": 0.0, "pitch": -56.0, "yaw": 0.0})
    target, mode, yaw_mode, targets = collector._collection_gimbal_config(plan(), policy, None)
    assert target == policy.cruise_gimbal_target
    assert (mode, yaw_mode, targets) == ("lock", "follow", ())


def test_track_without_targets_is_lock_in_serialized_plan(caplog):
    result = plan(gimbal_mode="track")
    assert result.gimbal_mode == "lock"
    assert "falling back to lock" in caplog.text


def test_explicit_host_mode_rejects_unsupported_airframe():
    with pytest.raises(ValueError, match="C11"):
        collector._collection_gimbal_config(plan(gimbal_mode="lock"), SimpleNamespace(), None)


def test_track_settle_waits_for_slew_and_accepts_unreachable_pointing_without_level_horizon():
    settled = collector._stabilization_command_settled
    assert not settled(None, "track")
    assert not settled({"target_reachable": None, "rate_limited": False}, "track")
    assert not settled({"target_reachable": True, "rate_limited": True}, "track")
    assert settled(
        {"target_reachable": True, "rate_limited": False, "horizon_degraded": True}, "track"
    )
    assert settled(
        {"target_reachable": False, "rate_limited": False, "horizon_degraded": True}, "track"
    )
    assert not settled({"horizon_degraded": True}, "lock")
    assert settled({"horizon_degraded": False}, "lock")


def test_track_position_decodes_amsl_and_checks_both_clocks():
    state = MavlinkState(
        messages={
            "GLOBAL_POSITION_INT": {
                "lat": 420000000,
                "lon": -930000000,
                "alt": 400000,
                "relative_alt": 85000,
                "time_boot_ms": 1000,
            }
        },
        received_monotonic_ns={"GLOBAL_POSITION_INT": 1_000_000_000},
        vehicle_time_boot_ms=1100,
    )
    assert collector._fresh_track_position(state, now_ns=1_100_000_000) == WorldTarget(42, -93, 400)
    assert collector._fresh_track_position(state, now_ns=1_300_000_000) is None
    state.vehicle_time_boot_ms = 1300
    assert collector._fresh_track_position(state, now_ns=1_100_000_000) is None
    state.vehicle_time_boot_ms = 900
    assert collector._fresh_track_position(state, now_ns=1_100_000_000) is None
    state.vehicle_time_boot_ms = 1100
    assert collector._fresh_track_position(state, now_ns=900_000_000) is None
    state.received_monotonic_ns.clear()
    assert collector._fresh_track_position(state, now_ns=1_100_000_000) is None
