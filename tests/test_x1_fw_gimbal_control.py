from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.fw_gimbal_control import (
    X1FwPassMidpoint,
    X1FwTerminalLockGate,
    estimate_fw_terminal_eta_s,
    spherical_bisector,
    terminal_lock_fk_horizon_error_deg,
)
from multidetect.sim_teacher.gimbal_recording import x1_track_preview
from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget
from multidetect.sim_teacher.target_tracking import WorldTarget, world_target_direction
from scripts import collect_sim_teacher_episodes as collector
from scripts.realworld import long_range_flight as flight


def _eta(enabled=True, *, pos=(70.0, 0.0, 0.0), speed=(20.0, 0.0, 0.0), age=0.05,
         endpoint=(100.0, 0.0, 0.0), plane=True):
    return estimate_fw_terminal_eta_s(
        enabled=enabled, has_end_tangent_plane=plane,
        position_ned_m=pos, velocity_ned_mps=speed, endpoint_ned_m=endpoint,
        tangent_ne=(1.0, 0.0), position_age_s=age, velocity_age_s=age,
    )


def test_terminal_lock_eta_requires_fresh_forward_state_and_clear_plane():
    eta, remaining, speed = _eta()
    assert (eta, remaining, speed) == (1.5, 30.0, 20.0)
    assert _eta(enabled=False) is None
    assert _eta(plane=False) is None
    assert _eta(age=0.21) is None
    assert _eta(speed=(-20.0, 0.0, 0.0)) is None
    assert _eta(pos=(101.0, 0.0, 0.0)) is None
    assert _eta(speed=(0.0, 5.0, 0.0)) is None
    assert _eta(speed=(0.5, 0.0, 0.0))[0] == 60.0


def test_terminal_lock_gate_triggers_first_eta_at_or_below_three_seconds_once():
    wall = [12.0]
    gate = X1FwTerminalLockGate(enabled=True, clock=lambda: wall[0])
    assert not gate.observe_eta(3.2, sim_stamp_ns=1_000, dispatch_sequence=40)
    assert not gate.observe_eta(None, sim_stamp_ns=2_000, dispatch_sequence=41)
    wall[0] = 13.0
    assert gate.observe_eta(3.0, sim_stamp_ns=3_000, dispatch_sequence=42)
    assert gate.requested_at_s == 13.0
    assert gate.requested_sim_stamp_ns == 3_000
    assert not gate.observe_eta(0.1, sim_stamp_ns=4_000, dispatch_sequence=43)
    gate.note_dispatch(42, 3000)
    assert gate.dispatch_sequence is None
    gate.note_dispatch(43, 3040)
    assert (gate.dispatch_sequence, gate.dispatch_vehicle_ms) == (43, 3040)


def test_terminal_lock_gate_is_a_noop_when_fw_legs_are_disabled():
    gate = X1FwTerminalLockGate(enabled=False, clock=lambda: 0.0)
    assert _eta(enabled=False) is None
    assert not gate.observe_eta(0.0, sim_stamp_ns=10, dispatch_sequence=1)
    assert gate.requested is False


def test_handoff_requires_dispatch_fresh_meta_fk_and_clear_hdeg():
    gate = X1FwTerminalLockGate(enabled=True, clock=lambda: 1.0)
    assert gate.observe_eta(2.0, sim_stamp_ns=1_000_000_000, dispatch_sequence=10)
    gate.note_dispatch(11, 1050)
    ready = gate.handoff_diagnostics(
        joint_fk_horizon_error_deg=1.0, hdeg=False,
        meta_stamp_ns=1_100_000_000, current_sim_stamp_ns=1_200_000_000,
        meta_receive_age_s=0.05,
    )
    assert ready["ready"] is True
    assert gate.handoff_diagnostics(
        joint_fk_horizon_error_deg=1.0, hdeg=True,
        meta_stamp_ns=1_100_000_000, current_sim_stamp_ns=1_200_000_000,
        meta_receive_age_s=0.05,
    )["ready"] is False
    assert gate.handoff_diagnostics(
        joint_fk_horizon_error_deg=1.0, hdeg=False,
        meta_stamp_ns=1_100_000_000, current_sim_stamp_ns=1_200_000_000,
        meta_receive_age_s=0.25,
    )["ready"] is False
    assert gate.handoff_diagnostics(
        joint_fk_horizon_error_deg=5.0, hdeg=False,
        meta_stamp_ns=1_100_000_000, current_sim_stamp_ns=1_200_000_000,
        meta_receive_age_s=0.05,
    )["ready"] is False


def test_terminal_lock_fk_uses_measured_joint_angles():
    desired = {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    body = dict(desired)
    assert terminal_lock_fk_horizon_error_deg(body, dict(desired), desired) == 0.0
    error = terminal_lock_fk_horizon_error_deg(
        body, {"roll": 10.0, "pitch": 0.0, "yaw": 0.0}, desired
    )
    assert error is not None and math.isclose(error, 10.0, abs_tol=1e-8)


def test_live_eta_adapter_checks_both_clocks_and_uses_only_bound_path():
    from scripts.collect_sim_teacher_episodes import _x1_terminal_eta
    from multidetect.sim_teacher.streams import MavlinkState

    msg = dict(x=60, y=10, z=-70, vx=20, vy=0, vz=0, time_boot_ms=900)
    state = MavlinkState(messages={"LOCAL_POSITION_NED": msg}, vehicle_time_boot_ms=1000,
                         received_monotonic_ns={"LOCAL_POSITION_NED": 1_000_000_000})
    path = {"target_ned_m": (100, 0, -70), "tangent_ne": (1, 0)}
    assert _x1_terminal_eta(state, None, 1_050_000_000) is None
    assert _x1_terminal_eta(state, path, 1_050_000_000) == (2, 40, 20)
    assert _x1_terminal_eta(state, path, 1_210_000_000) is None
    state.vehicle_time_boot_ms = 1150
    assert _x1_terminal_eta(state, path, 1_050_000_000) is None


def test_original_handoff_requires_post_dispatch_joint_fk_and_post_dispatch_hdeg():
    from scripts.collect_sim_teacher_episodes import _x1_terminal_handoff
    from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget

    zero = dict.fromkeys(("roll", "pitch", "yaw"), 0.)
    bridge = BridgeHorizonTarget(zero, mode="lock", yaw_mode="follow", status_sender=(1, 1))
    reader = SimpleNamespace(state=SimpleNamespace(vehicle_time_boot_ms=1130), attitude_at=lambda _: zero)
    sidecar = {"fresh": True, "source": "gazebo_joint_state", "position_rad": dict(zero)}
    meta = SimpleNamespace(sensor_stamp_ns=1_100_000_000, received_monotonic_ns=2_000_000_000,
                           joint_state={"source_timestamp_ns": 1_088_000_000, "gimbal_joint_state": sidecar})
    gate = X1FwTerminalLockGate(enabled=True, clock=lambda: 2.)
    gate.observe_eta(2.9, sim_stamp_ns=1_000_000_000, dispatch_sequence=12)

    def status(ms, v):
        bridge.record_bridge_status({"message_type": "NAMED_VALUE_FLOAT", "name": "c11_hdeg",
                                     "_source_system": 1, "_source_component": 1,
                                     "time_boot_ms": ms, "value": v})

    def inspect():
        return _x1_terminal_handoff(gate, bridge, reader, meta, 2_050_000_000,
                                    dispatch=(13, 1030), now_ms=1130)

    status(1020, 0)
    assert not inspect()["ready"]  # hdeg from before physical send
    status(1080, 0)
    assert inspect()["ready"]
    sidecar["position_rad"]["roll"] = math.radians(13)
    assert math.isclose(inspect()["joint_fk_horizon_error_deg"], 13)
    assert not inspect()["ready"]  # hdeg=0 alone doesn't measure the joint
    sidecar["position_rad"] = {}
    assert inspect()["joint_fk_horizon_error_deg"] is None
    meta.joint_state = {}
    assert not inspect()["ready"]


def test_lock_dispatch_identifies_the_completed_44_ticket_not_queued_or_previous_track():
    import threading

    from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget, HostHorizonLoop

    zero = dict.fromkeys(("roll", "pitch", "yaw"), 0.)
    origin = WorldTarget(0., 0., 10.)
    bridge = BridgeHorizonTarget(zero, mode="track", yaw_mode="follow",
                                 track_targets=(WorldTarget(.001, 0., 0.),))
    sent = []

    def send(q, flags):
        ticket = SimpleNamespace(done=threading.Event(), error=None, started_ns=100,
                                 started_vehicle_ms=1000 + 20 * len(sent))
        sent.append((flags, ticket))
        return ticket

    loop = HostHorizonLoop(bridge, lambda: (zero, 1., 1., origin), send, clock=lambda: 1.)
    loop.step()
    bridge.set_mode("lock")
    loop.step()
    assert [f for f, _ in sent] == [92] and loop.lock_dispatch is None
    sent[-1][1].done.set()
    loop.step()
    assert [f for f, _ in sent] == [92, 44] and loop.lock_dispatch is None
    sent[-1][1].done.set()
    loop.step()
    assert loop.lock_dispatch == (2, 1020)


def test_spherical_bisector_is_exact_midpoint_when_under_the_cap():
    old = (1.0, 0.0, 0.0)
    new = (math.cos(math.radians(12.0)), math.sin(math.radians(12.0)), 0.0)
    direction, switch_angle, deflection = spherical_bisector(old, new)
    assert math.isclose(switch_angle, 12.0, abs_tol=1e-7)
    assert math.isclose(deflection, 6.0, abs_tol=1e-7)
    dot = sum(a * b for a, b in zip(old, direction))
    assert math.isclose(math.degrees(math.acos(max(-1.0, min(1.0, dot)))),
                        6.0, abs_tol=1e-7)


def test_spherical_bisector_never_deflects_more_than_six_point_five_degrees():
    old = (0.0, 1.0, 0.0)
    for angle in (8.1, 12.7, 40.0, 90.0, 179.0, 180.0):
        new = (math.cos(math.radians(angle)), math.sin(math.radians(angle)), 0.0)
        direction, _, deflection = spherical_bisector(old, new)
        dot = sum(a * b for a, b in zip(old, direction))
        measured = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        assert deflection <= 6.5 + 1e-8
        assert measured <= 6.5 + 1e-7


def _midpoint(**overrides):
    settings = dict(enabled=True, short_leg_m=15., has_fly_through=True, planned_switch_angle_deg=12.,
                    old_target=WorldTarget(.001, 0., 0.), new_target=WorldTarget(0., .001, 0.))
    return X1FwPassMidpoint(**(settings | overrides))


@pytest.mark.parametrize("invalid", [dict(enabled=False), dict(has_fly_through=False), dict(short_leg_m=80.),
                                     dict(short_leg_m=None), dict(planned_switch_angle_deg=8.),
                                     dict(planned_switch_angle_deg=None), dict(new_target=None)])
def test_no_pass_callback_outside_planned_short_fw_switch(invalid):
    control = _midpoint(**invalid)
    assert collector._x1_fw_pass_wait_options(control, 1, None) == {}
    assert control.observe_crossing(sim_stamp_ns=0) is None
    assert control.direction_at(WorldTarget(0., 0., 0.)) is None


def test_live_midpoint_bounds_direction_until_original_switch_without_changing_score_or_preview():
    control = _midpoint()
    lead, cap = x1_track_preview({}, fw_leg=True)
    bridge, plain = (BridgeHorizonTarget(mode="track", track_preview_s=lead, track_preview_max_rad=cap)
                     for _ in range(2))
    bridge.select_track_target(WorldTarget(.001, 0., 0.), pass_midpoint=control)
    origin = WorldTarget(0., 0., 80.)
    assert control.direction_at(origin) is None
    ticks = [(origin, control.old_target)]
    # Independent old/current LOS angle below uses a chord, not the implementation's acos.
    for i in range(1, 101):
        ticks.append((WorldTarget(i * .000009, -i * .000004, 80. + i / 2), control.old_target))
    ticks.append((ticks[-1][0], control.new_target))
    for i, (pos, scoring_point) in enumerate(ticks):
        now = 1. + .02 * i
        if i == 1:
            assert control.observe_crossing(sim_stamp_ns=1_020_000_000)["planned_switch_angle_deg"] == 12.
            assert control.observe_crossing(sim_stamp_ns=1_021_000_000) is None
            assert bridge.update(dict(roll=0., pitch=-83., yaw=0.), now_s=now, attitude_time_s=now,
                                 position=None) is None  # never reuse a ray through a GPS dropout
        if scoring_point is control.new_target:
            bridge.select_track_target(control.new_target)
        expected = world_target_direction(pos, scoring_point)
        if 0 < i < len(ticks) - 1:
            new_los = world_target_direction(pos, control.new_target)
            span = 2 * math.asin(math.dist(expected, new_los) / 2)
            actual = control.direction_at(pos)
            theta = 2 * math.asin(math.dist(expected, actual) / 2)
            residual = 2 * math.asin(math.dist(actual, new_los) / 2)
            assert math.degrees(theta) == pytest.approx(min(math.degrees(span) / 2, 6.5), abs=1e-9)
            assert theta + residual == pytest.approx(span, abs=1e-9)
            if i == 1:
                first = actual
            if i == len(ticks) - 2:
                assert math.dist(first, expected) > .3  # a ray frozen at passage violates the cap
            expected = actual
        params = dict(body_deg=dict(roll=10., pitch=-83., yaw=13.), now_s=now, attitude_time_s=now,
                      body_rates_rad_s=(.35, .1, -.1))
        cmd = bridge.update(position=pos, **params)
        oracle = plain.update(track_direction_ned=expected, **params)
        assert cmd["quaternion"] == pytest.approx(oracle["quaternion"], abs=1e-10)
        assert cmd["target_ref"] == scoring_point.reference()
    assert control.direction_at(None) is None
    assert bridge.update(position=None, **params) is None


def test_policy_binds_next_planned_angle_only_on_fw_eval_flythrough():
    points = [(0., 40., -80.), (0., 55., -80.), (80., 55., -80.)]
    plain_meta = {"long_range_home_relative": True}
    for opts in ({}, {"vtol": True}, {"vtol": True, "gimbal_eval": True}):
        policy = flight.LongRangePolicy(points, 80., fly_through_radius_m=15., **opts)
        assert [policy.next_action().metadata for _ in points] == [plain_meta] * 3
    policy = flight.LongRangePolicy(points, 80., vtol=True, gimbal_eval=True,
                                    fly_through_radius_m=[None, 15., 15.],
                                    fw_eval_switch_angles_deg=(None, 5., 13.))
    assert [policy.next_action().metadata for _ in points] == [
        plain_meta, dict(plain_meta, long_range_leg_length_m=15., fw_next_switch_angle_deg=13.), plain_meta]


def test_wait_notifies_only_its_own_control_from_a_validated_post_plane_sample():
    home = flight.GeoPoint(0., 0.)
    safety = dict(geofence_center=home.to_mapping(), geofence_radius_m=1_000., maximum_mission_radius_m=500.)
    mission = {"geometry": SimpleNamespace(home=home),
               "source": {"mission_compile_runtime": {"safety_policy": safety}}}
    command = flight.LongRangePolicy([(10., 0., -80.)], 80., vtol=True, fly_through_radius_m=15.).next_action()
    control, other = _midpoint(), _midpoint()
    records = []
    enabled = collector._x1_fw_pass_wait_options(control, 11, records.append)
    off = collector._x1_fw_pass_wait_options(None, 12, records.append)

    def wait(samples, *, revision=flight.scenario_policy.FLIGHT_MARGIN_REVISION, **options):
        mission["route_preflight"] = {"metrics": {"flight_margin_revision": revision}}
        state = collector.MavlinkState(messages={"HEARTBEAT": {"custom_mode": 6 << 16},
                                               "ATTITUDE": {"yaw": 0.},
                                               "HOME_POSITION": dict.fromkeys(("latitude", "longitude", "altitude",
                                                                              "x", "y", "z"), 0)},
                                       received_monotonic_ns={"HEARTBEAT": 0})
        samples = iter(samples)

        def advance(_=None):
            t, north = next(samples)
            state.messages["LOCAL_POSITION_NED"] = dict(time_boot_ms=t, x=north, y=0., z=-80.)

        advance()
        reader = SimpleNamespace(state=state, raise_if_failed=lambda: None)
        return flight.wait_for_leg(command, reader, collector=collector, mission=mission, cruise_mps=15.,
                                   offboard_streamer=reader, fw_pass_tangent=(1., 0.), wall=lambda: 0.,
                                   sleep=advance, **options)

    with pytest.raises(RuntimeError, match="missed the bounded"):
        wait([(1_000, 11.)], **enabled)  # already past on entry
    with pytest.raises(RuntimeError, match="missed the bounded"):
        wait([(1_000, 9.), (4_001, 11.)], **enabled)  # stale bracket
    assert wait([(1_000, 11.)], revision=flight.scenario_policy.SAMPLED_FLIGHT_MARGIN_REVISION,
                **enabled)["waited"] == "arrived"  # legacy sampled success is not a crossing
    samples = [(1_000, 9.), (1_100, 11.)]
    baseline = wait(samples, **off)
    assert records == []
    assert wait(samples, **enabled) == baseline
    assert len(records) == 1 and records[0]["step_index"] == 11
    assert records[0]["crossing_sim_stamp_ns"] == 1_050_000_000
    assert records[0]["sim_stamp_ns"] == control.observed_sim_stamp_ns == 1_100_000_000
    assert other.observed_sim_stamp_ns is None


def test_runner_wait_forwards_only_explicit_per_call_callbacks(monkeypatch):
    monkeypatch.setattr(flight, "wait_for_leg", lambda *_, **options: options)
    command = flight.LongRangePolicy([(10., 0., -80.)], 80., vtol=True, fly_through_radius_m=15.).next_action()
    with flight.collector_overrides(collector, {"geometry": None}):
        for control in (_midpoint(), None, _midpoint(), None):
            observer = collector._x1_fw_pass_wait_options(control, 0, None)
            forwarded = collector._wait_motion_command(command, None, cruise_mps=15., **observer)
            if control is None:
                assert "on_passage" not in forwarded
            else:
                assert forwarded["on_passage"] is observer["on_passage"]
