from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.fw_gimbal_control import (
    FW_GUIDED_LOITER_ALONG_DONE_M,
    FW_GUIDED_LOITER_CROSS_DONE_M,
    FW_GUIDED_LOITER_MIN_CLOSING_MPS,
    FW_TERMINAL_LOCK_ETA_S,
    X1FwPassMidpoint,
    X1FwTerminalLockGate,
    estimate_fw_guided_loiter_exit_eta_s,
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


def _loiter_eta(
    enabled=True,
    *,
    has_fly_through=False,
    pos=(110.0, 38.0, -80.0),
    vel=(0.0, -15.0, 0.0),
    start=(0.0, 0.0),
    target=(100.0, 0.0),
    age=0.05,
):
    return estimate_fw_guided_loiter_exit_eta_s(
        enabled=enabled,
        has_fly_through=has_fly_through,
        position_ned_m=pos,
        velocity_ned_mps=vel,
        start_ne_m=start,
        target_ne_m=target,
        position_age_s=age,
        velocity_age_s=age,
    )


def test_guided_loiter_exit_eta_requires_behind_target_closing_cross_and_fresh_state():
    assert (
        FW_GUIDED_LOITER_ALONG_DONE_M,
        FW_GUIDED_LOITER_CROSS_DONE_M,
        FW_GUIDED_LOITER_MIN_CLOSING_MPS,
        FW_TERMINAL_LOCK_ETA_S,
    ) == (5.0, 8.0, 1.0, 3.0)
    eta, remaining_cross, closing = _loiter_eta()
    assert (eta, remaining_cross, closing) == pytest.approx((2.0, 30.0, 15.0))
    # Opposite side of track (signed_cross < 0) closing back toward track (+east velocity).
    eta_neg, rem_neg, close_neg = _loiter_eta(pos=(110.0, -38.0, -80.0), vel=(0.0, 15.0, 0.0))
    assert (eta_neg, rem_neg, close_neg) == pytest.approx((2.0, 30.0, 15.0))
    assert _loiter_eta(enabled=False) is None
    assert _loiter_eta(has_fly_through=True) is None
    assert _loiter_eta(age=0.21) is None
    assert _loiter_eta(age=-0.01) is None
    # Still on the approach (along = +5.01 m > 5.0 m): never trigger early.
    assert _loiter_eta(pos=(94.99, 38.0, -80.0)) is None
    # Already within Branch-3 cross-track threshold (cross <= 8.0 m).
    assert _loiter_eta(pos=(110.0, 8.0, -80.0)) is None
    assert _loiter_eta(pos=(110.0, 7.5, -80.0)) is None
    # Opening or closing rate <= 1.0 m/s.
    assert _loiter_eta(vel=(0.0, 15.0, 0.0)) is None
    assert _loiter_eta(vel=(0.0, -1.0, 0.0)) is None
    assert _loiter_eta(vel=(0.0, -0.5, 0.0)) is None
    assert _loiter_eta(start=(100.0, 0.0), target=(100.0, 0.0)) is None


def test_guided_loiter_recorded_like_sequence_pitch_71_5_branch3_exit_and_one_shot_gate():
    # Iowa step-6 geometry: start ~(685.46, -842.29) -> target (300.0, -360.0), R=80 m.
    start_ne = (685.462158, -842.286560)
    target_ne = (300.0, -360.0)
    leg_n, leg_e = target_ne[0] - start_ne[0], target_ne[1] - start_ne[1]
    leg = math.hypot(leg_n, leg_e)
    un, ue = leg_n / leg, leg_e / leg
    # Right-hand orthogonal unit vector (+cross direction).
    pn, pe = -ue, un
    radius = 80.0
    omega = 17.8 / radius  # ~17.8 m/s ground speed on 80 m loiter ring

    gate = X1FwTerminalLockGate(enabled=True, clock=lambda: 100.0)
    path = {"guidance": "guided_loiter", "start_ne_m": start_ne, "target_ne_m": target_ne}
    triggers = []
    branch3_exit_t = None

    # 1) 54 s approach from along = 617 m down to along = 5.2 m with cross offset ~80 m:
    # must never trigger ETA, and never complete reposition.
    for i in range(270):
        t_s = 607.8 + i * 0.2
        along_rem = 617.0 - (611.8 * i / 269.0)
        cross_off = 25.0 + 55.0 * (i / 269.0)
        n = target_ne[0] - along_rem * un + cross_off * pn
        e = target_ne[1] - along_rem * ue + cross_off * pe
        vn = 17.5 * un - 2.0 * pn
        ve = 17.5 * ue - 2.0 * pe
        ms = round(t_s * 1000)
        lpn = dict(x=n, y=e, z=-82.7, vx=vn, vy=ve, vz=0.0, time_boot_ms=ms)
        att = dict(roll=math.radians(5.0), pitch=math.radians(-71.5), yaw=0.0)
        state = collector.MavlinkState(
            messages={"LOCAL_POSITION_NED": lpn, "ATTITUDE": att},
            vehicle_time_boot_ms=ms + 10,
            received_monotonic_ns={"LOCAL_POSITION_NED": ms * 1_000_000},
        )
        assert collector._x1_terminal_eta(state, path, (ms + 20) * 1_000_000) is None

    # 2) Loiter ring behind target (theta from -70 deg through 0 deg where cross -> 0):
    # In tailsitter FW bank (roll ~ +68 deg, pitch ~ -71.5 deg), PX4 ATTITUDE.yaw lags the
    # horizontal velocity heading by ~68 deg, so _fixed_wing_tangent_error_deg stays ~67.5 deg
    # (> 35 deg) and the tangent branch never fires; exit occurs via Branch 3 (along<=5, cross<=8).
    for step in range(800):
        t_s = 656.0 + step * 0.02
        theta = math.radians(-70.0) + omega * (step * 0.02)
        # Position on the 80 m circle behind the target.
        n = target_ne[0] + radius * math.cos(theta) * un - radius * math.sin(theta) * pn
        e = target_ne[1] + radius * math.cos(theta) * ue - radius * math.sin(theta) * pe
        vn = -radius * omega * math.sin(theta) * un - radius * omega * math.cos(theta) * pn
        ve = -radius * omega * math.sin(theta) * ue - radius * omega * math.cos(theta) * pe
        vel_heading_deg = math.degrees(math.atan2(ve, vn))
        # Tailsitter at pitch = -71.5 deg, roll = +68.0 deg has Euler yaw offset ~67.5 deg.
        tailsitter_pitch_deg = -71.5
        assert tailsitter_pitch_deg == -71.5
        euler_yaw_deg = ((vel_heading_deg - 67.5 + 180.0) % 360.0) - 180.0
        dn, de = target_ne[0] - n, target_ne[1] - e
        dist = math.hypot(dn, de)
        terr = collector._fixed_wing_tangent_error_deg(dn, de, euler_yaw_deg)
        assert terr > 35.0  # tangent branch never fires for banked tailsitter

        ms = round(t_s * 1000)
        lpn = dict(x=n, y=e, z=-82.7, vx=vn, vy=ve, vz=0.0, time_boot_ms=ms)
        state = collector.MavlinkState(
            messages={"LOCAL_POSITION_NED": lpn},
            vehicle_time_boot_ms=ms + 8,
            received_monotonic_ns={"LOCAL_POSITION_NED": ms * 1_000_000},
        )
        eta = collector._x1_terminal_eta(state, path, (ms + 15) * 1_000_000)
        if eta is not None and gate.observe_eta(
            eta[0], sim_stamp_ns=ms * 1_000_000, dispatch_sequence=100 + step
        ):
            triggers.append((t_s, eta[0], eta[1], eta[2]))

        if collector._fixed_wing_reposition_complete(
            dn=dn,
            de=de,
            leg_n=leg_n,
            leg_e=leg_e,
            distance_m=dist,
            loiter_radius_m=radius,
            yaw_deg=euler_yaw_deg,
            centre_n=target_ne[0],
            centre_e=target_ne[1],
            angle_deg=180.0,
            fly_through_radius_m=None,
        ):
            branch3_exit_t = t_s
            break

    assert branch3_exit_t is not None
    assert len(triggers) == 1  # strictly one-shot
    trig_t, trig_eta, trig_rem, trig_rate = triggers[0]
    assert 0.0 <= trig_eta <= FW_TERMINAL_LOCK_ETA_S
    assert 2.5 <= (branch3_exit_t - trig_t) <= 3.1
    assert trig_rem > 0.0 and trig_rate > 1.0


def test_guided_loiter_path_and_eta_keep_offboard_and_non_fw_unchanged():
    from multidetect.autonomy.motion_intent import MotionCommand

    msg = dict(x=60.0, y=10.0, z=-70.0, vx=20.0, vy=0.0, vz=0.0, time_boot_ms=900)
    home = dict(latitude=0, longitude=0, altitude=0, x=5.0, y=-10.0, z=0.0)
    state = collector.MavlinkState(
        messages={"LOCAL_POSITION_NED": msg, "HOME_POSITION": home},
        vehicle_time_boot_ms=1000,
        received_monotonic_ns={"LOCAL_POSITION_NED": 1_000_000_000},
    )

    # 1) Offboard fly-through command & path are byte-for-byte unchanged.
    offboard_cmd = MotionCommand(
        "reposition", mode="FW", target_ned=(100.0, 0.0, -70.0), fly_through_radius_m=15.0
    )
    assert collector._x1_guided_loiter_terminal_path(offboard_cmd, state) is None
    offboard_path = {"target_ned_m": (100.0, 0.0, -70.0), "tangent_ne": (1.0, 0.0)}
    assert collector._x1_terminal_eta(state, offboard_path, 1_050_000_000) == (2.0, 40.0, 20.0)

    # 2) Non-FW commands never produce a guided-loiter terminal path.
    for non_fw in (
        MotionCommand("mc_reposition", mode="MC", target_ned=(100.0, 0.0, -70.0)),
        MotionCommand("reposition", mode="MC", target_ned=(100.0, 0.0, -70.0)),
        MotionCommand("transition", mode="MC"),
        MotionCommand("mc_hold", mode="MC", target_ned=(0.0, 0.0, -70.0), duration_s=10.0),
    ):
        assert collector._x1_guided_loiter_terminal_path(non_fw, state) is None

    # 3) Guided-loiter FW command binds home-relative or local target and enforces 0.2 s freshness.
    loiter_cmd = MotionCommand(
        "reposition",
        mode="FW",
        target_ned=(100.0, 0.0, -70.0),
        fly_through_radius_m=None,
        metadata={"long_range_home_relative": True},
    )
    loiter_path = collector._x1_guided_loiter_terminal_path(loiter_cmd, state)
    assert loiter_path == {
        "guidance": "guided_loiter",
        "start_ne_m": (60.0, 10.0),
        "target_ne_m": (105.0, -10.0),
    }

    local_cmd = MotionCommand("reposition", mode="FW", target_ned=(100.0, 10.0, -70.0))
    local_path = collector._x1_guided_loiter_terminal_path(local_cmd, state)
    assert local_path == {
        "guidance": "guided_loiter",
        "start_ne_m": (60.0, 10.0),
        "target_ne_m": (100.0, 10.0),
    }
    state.messages["LOCAL_POSITION_NED"] = dict(
        x=110.0, y=48.0, z=-70.0, vx=0.0, vy=-15.0, vz=0.0, time_boot_ms=950
    )
    live_eta = collector._x1_terminal_eta(state, local_path, 1_050_000_000)
    assert live_eta == pytest.approx((2.0, 30.0, 15.0))
    # Stale wall clock (> 0.2 s) -> None.
    assert collector._x1_terminal_eta(state, local_path, 1_210_000_000) is None
    # Stale PX4 sim clock (> 0.2 s) -> None.
    state.vehicle_time_boot_ms = 1160
    assert collector._x1_terminal_eta(state, local_path, 1_050_000_000) is None


def test_iowa_six_batch_recorded_eta_arrival_table():
    """Verify the 6-batch Iowa clear_noon ETA table from recorded data when NAS is mounted."""
    import glob
    import json
    import os
    from pathlib import Path

    base = Path("/mnt/nas/AdminHome/volatic/x1-gimbal-eval/collection/rw_farm_iowa__clear_noon")
    expected = [
        ("a502f02b6aae700eb4e6", 664.540, 664.524, -0.016, 662.264, 2.276, 661.880, 2.660),
        ("3e6e4bf4386a71692f43", 666.252, 666.252, 0.000, 663.992, 2.260, 663.632, 2.620),
        ("b8382268fd892e1c3e1e", 677.524, 677.484, -0.040, 675.220, 2.304, 674.844, 2.680),
        ("487b2660e8915e43cfb6", 666.424, 666.160, -0.264, 663.904, 2.520, 663.544, 2.880),
        ("8b877da15ad91535799a", 674.416, 674.376, -0.040, 672.112, 2.304, 671.736, 2.680),
        ("04caae4291f6e47848bd", 669.984, 669.960, -0.024, 667.704, 2.280, 667.344, 2.640),
    ]
    if not base.is_dir():
        pytest.skip("Iowa clear_noon NAS collection directory not mounted")

    for bid, exp_lock, exp_arr, exp_err, exp_t25, exp_lead25, exp_t30, exp_lead30 in expected:
        pattern = str(base / bid / "jobs" / "*" / "attempt-1" / "flight" / "episodes" / "episode-*")
        matches = sorted(glob.glob(pattern))
        assert matches, f"missing episode for {bid}"
        mav_path = Path(matches[0]) / "mavlink.jsonl"
        size = os.path.getsize(mav_path)
        home_n = home_e = None
        track_s = lock_s = None
        pos = []
        with open(mav_path, "rb") as handle:
            head = handle.read(256 * 1024).decode("utf-8", errors="ignore")
            for line in head.splitlines():
                if '"LOCAL_POSITION_NED"' in line:
                    row = json.loads(line)
                    if row.get("message_type") == "LOCAL_POSITION_NED":
                        home_n, home_e = float(row["x"]), float(row["y"])
                        break
            handle.seek(max(0, int(size * 0.62)))
            chunk = handle.read(int(size * 0.22)).decode("utf-8", errors="ignore")
        for line in chunk.splitlines()[1:]:
            if '"collector_gimbal_eval_fw_mode"' in line:
                row = json.loads(line)
                if row.get("step_index") == 6 and row.get("mode") == "track":
                    track_s = row["sim_stamp_ns"] / 1e9
                elif row.get("mode") == "lock":
                    lock_s = row["sim_stamp_ns"] / 1e9
                    break
            elif track_s is not None and '"LOCAL_POSITION_NED"' in line:
                row = json.loads(line)
                if row.get("message_type") == "LOCAL_POSITION_NED":
                    t_s = row["time_boot_ms"] / 1e3
                    if t_s >= track_s:
                        pos.append((
                            t_s,
                            float(row["x"]),
                            float(row["y"]),
                            float(row["vx"]),
                            float(row["vy"]),
                        ))

        assert home_n is not None and track_s is not None and lock_s is not None and pos
        start_ne = (pos[0][1], pos[0][2])
        target_ne = (300.0 + home_n, -360.0 + home_e)
        leg_n, leg_e = target_ne[0] - start_ne[0], target_ne[1] - start_ne[1]
        leg = math.hypot(leg_n, leg_e)
        gate30 = X1FwTerminalLockGate(enabled=True, clock=lambda: 0.0)
        gate25 = X1FwTerminalLockGate(enabled=True, clock=lambda: 0.0, trigger_eta_s=2.5)
        trig30_s = trig25_s = pred_arr_s = None
        false_approach_triggers = 0

        for idx, (t_s, x, y, vx, vy) in enumerate(pos):
            dn, de = target_ne[0] - x, target_ne[1] - y
            along = (dn * leg_n + de * leg_e) / leg
            cross = abs(leg_n * de - leg_e * dn) / leg
            if (
                along <= FW_GUIDED_LOITER_ALONG_DONE_M
                and cross <= FW_GUIDED_LOITER_CROSS_DONE_M
                and pred_arr_s is None
            ):
                pred_arr_s = t_s
            eta = estimate_fw_guided_loiter_exit_eta_s(
                enabled=True,
                has_fly_through=False,
                position_ned_m=(x, y, -80.0),
                velocity_ned_mps=(vx, vy, 0.0),
                start_ne_m=start_ne,
                target_ne_m=target_ne,
                position_age_s=0.0,
                velocity_age_s=0.0,
            )
            if along > FW_GUIDED_LOITER_ALONG_DONE_M and eta is not None:
                false_approach_triggers += 1
            eta_val = eta[0] if eta is not None else None
            if gate30.observe_eta(eta_val, sim_stamp_ns=round(t_s * 1e9), dispatch_sequence=idx):
                trig30_s = t_s
            if gate25.observe_eta(eta_val, sim_stamp_ns=round(t_s * 1e9), dispatch_sequence=idx):
                trig25_s = t_s

        assert false_approach_triggers == 0
        assert lock_s == pytest.approx(exp_lock, abs=1e-3)
        assert pred_arr_s == pytest.approx(exp_arr, abs=1e-3)
        assert (pred_arr_s - lock_s) == pytest.approx(exp_err, abs=1e-3)
        assert trig25_s == pytest.approx(exp_t25, abs=1e-3)
        assert (lock_s - trig25_s) == pytest.approx(exp_lead25, abs=1e-3)
        assert trig30_s == pytest.approx(exp_t30, abs=1e-3)
        assert (lock_s - trig30_s) == pytest.approx(exp_lead30, abs=1e-3)
