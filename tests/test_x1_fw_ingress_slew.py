"""CPU geometry & lifetime of the opt-in, first-FW-point slew grant; no flight data."""

from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.horizon_stabilization import (
    AXES,
    C11_LIMITS,
    BridgeHorizonTarget,
    rotation_deg,
)
from multidetect.sim_teacher.target_tracking import (
    WorldTarget,
    ingress_slew_joint,
    solve_track_target,
    world_target_direction,
)

# Synthetic survey point. Here the level-preferred endpoint uses the opposite roll stop
# and a long yaw swing, while a nearby point on the same optical-ray twist family exists.
ORIGIN = WorldTarget(0.0, 0.0, 160.0)
TARGET = WorldTarget(0.0006575, 0.0004866, 117.82)
BODY = dict(roll=-90.0, pitch=60.0, yaw=0.0)
PREVIOUS = dict(roll=-45.0, pitch=-130.0, yaw=-230.0)
RAY = world_target_direction(ORIGIN, TARGET)


def setup_bridge(grant, *, previous=PREVIOUS, rate=180.0, last_time=1.0):
    bridge = BridgeHorizonTarget(max_rate_deg_s=rate)
    bridge.select_track_target(TARGET, fw_ingress_slew=grant)
    bridge.set_mode("track")
    bridge.last_joint.update(previous)
    bridge.last_time = last_time
    return bridge


def update(bridge, *, t=1.02, **extra):
    return bridge.update(BODY, now_s=t, attitude_time_s=t, position=ORIGIN, **extra)


def forward(q):
    w, x, y, z = q
    return (1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y))


@pytest.mark.parametrize("rate", [60.0, 180.0, 360.0])
def test_choose_slew_feasible_twist_without_changing_wire_ray_or_reference(rate):
    ordinary, granted = setup_bridge(False, rate=rate), setup_bridge(True, rate=rate)
    a, b = update(ordinary), update(granted)
    assert a["transport_flags"] == b["transport_flags"] == 92
    assert a["target_ref"] == b["target_ref"] == TARGET.reference()
    assert a["desired_earth_deg"] == b["desired_earth_deg"]
    assert abs(sum(x * y for x, y in zip(a["quaternion"], b["quaternion"], strict=True))) < 0.99
    assert forward(a["quaternion"]) == pytest.approx(RAY, abs=1e-12)
    assert forward(b["quaternion"]) == pytest.approx(RAY, abs=1e-12)
    assert all(
        abs(granted.last_joint[k] - PREVIOUS[k]) <= min(rate, 180.0) * 0.02 + 1e-10 for k in AXES
    )
    assert granted.last_joint["yaw"] < -180.0  # continuous mechanical winding, not wrapped to +130


@pytest.mark.parametrize(
    "how", ["ordinary_selection", "new_point", "exit_track", "assigned_targets", "midpoint"]
)
def test_grant_is_scoped_to_one_selection_and_not_reacquired_implicitly(how):
    granted, stock = setup_bridge(True), setup_bridge(False)
    history = granted.last_joint, granted._command_history
    for bridge in (granted, stock):
        if how == "ordinary_selection":
            bridge.select_track_target(TARGET)
        elif how == "new_point":
            bridge.select_track_target(WorldTarget(0.00066, 0.00049, 117.8))
        elif how == "exit_track":
            bridge.set_mode("lock")
            assert update(bridge)["transport_flags"] == 44
            bridge.set_mode("track")
        elif how == "assigned_targets":
            bridge.track_targets = (TARGET,)
        else:
            mid = SimpleNamespace(old_target=TARGET, direction_at=lambda _: RAY)
            bridge.select_track_target(TARGET, pass_midpoint=mid, fw_ingress_slew=True)
    assert update(granted) == update(stock)
    assert granted.last_joint is history[0] and granted._command_history is history[1]


def test_no_authority_on_other_ray_and_stale_updates_do_not_advance_state():
    granted, stock = setup_bridge(True), setup_bridge(False)
    assert granted.update(BODY, now_s=1.02, attitude_time_s=0.1, position=ORIGIN) is None
    assert update(granted, t=0.95) is None
    assert granted.last_joint == PREVIOUS and granted.last_time == 1.0
    assert update(granted, track_direction_ned=RAY) == update(stock, track_direction_ned=RAY)


@pytest.mark.parametrize(
    "previous,last_time", [(PREVIOUS, None), (PREVIOUS, 1.02), (dict.fromkeys(AXES, 0.0), 1.0)]
)
def test_first_tick_zero_interval_or_no_local_solution_preserves_capture(previous, last_time):
    g, s = [setup_bridge(v, previous=previous, last_time=last_time) for v in (True, False)]
    assert update(g) == update(s) and g.last_joint == s.last_joint


def test_inside_joint_budget_or_off_roll_stop_keep_original_endpoint():
    rotation = rotation_deg(BODY)
    stock, _, _ = solve_track_target(rotation, RAY, PREVIOUS, C11_LIMITS)
    steps = dict.fromkeys(AXES, 3.6)
    assert ingress_slew_joint(rotation, RAY, stock, C11_LIMITS, stock, steps) is stock
    off = {**stock, "roll": 0.0}
    assert ingress_slew_joint(rotation, RAY, PREVIOUS, C11_LIMITS, off, steps) is off
