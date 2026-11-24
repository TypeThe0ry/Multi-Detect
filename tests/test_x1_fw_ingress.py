"""The single FW confirmation can change a camera command, never a scoring epoch."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget
from multidetect.sim_teacher.streams import MavlinkReader
from multidetect.sim_teacher.target_tracking import WorldTarget
from scripts import collect_sim_teacher_episodes as collector

NOW = 2_000_000_000
HOVER = WorldTarget(0.01, 0.02, 530.0)
FW = WorldTarget(0.005, 0.003, 100.0)
BODY = dict.fromkeys(("roll", "pitch", "yaw"), 0.0)


def confirm(plan, reader, bridge, session, record):
    capture_guard = collector.FwIngressCaptureGuard()
    capture_guard.note_commit(800_000_000)
    collector._x1_fw_transition_complete(
        plan,
        reader,
        bridge,
        session,
        record,
        capture_guard=capture_guard,
        next_video_sim_ns=1_200_000_000,
    )


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(collector.time, "monotonic_ns", lambda: NOW)
    reader = MavlinkReader(None)
    state = reader.state
    state.messages.update(
        EXTENDED_SYS_STATE=dict(landed_state=2, vtol_state=4),
        ATTITUDE=dict(time_boot_ms=1000, **BODY),
        GLOBAL_POSITION_INT=dict(lat=0, lon=0, alt=500_000, time_boot_ms=990),
    )
    state.received_monotonic_ns = {k: NOW - 25_000_000 for k in state.messages}
    state.received_vehicle_ms = {"EXTENDED_SYS_STATE": 997}
    state.vehicle_time_boot_ms = 1001
    session = SimpleNamespace(
        owned=True, latest_attitude=({"failure_flags": 0, "time_boot_ms": 1000}, NOW - 10_000_000)
    )
    plan = SimpleNamespace(
        gimbal_bridge_stabilize=True, gimbal_eval_fw_legs=1, gimbal_eval_fw_targets=(FW,)
    )
    bridge = BridgeHorizonTarget(mode="lock", track_targets=(HOVER,))
    bridge.select_track_target = Mock(wraps=bridge.select_track_target)
    bridge.set_mode = Mock(wraps=bridge.set_mode)
    return plan, reader, bridge, session


def test_fw_confirmation_one_control_switch_original_leg_marker_and_capture(rig):
    plan, reader, bridge, session = rig
    rows = []
    # No camera/exposure callbacks and no flight executor are needed here.
    confirm(plan, reader, bridge, session, rows.append)
    assert [row["message_type"] for row in rows] == [
        "gimbal_measurement_fw_transition_complete",
        "collector_gimbal_fw_ingress",
    ]
    assert rows[-1]["requested"] and rows[-1]["target_ref"] == FW.reference()
    sol = bridge.update(BODY, now_s=2.0, attitude_time_s=2.0, position=WorldTarget(0.0, 0.0, 500.0))
    assert sol["transport_flags"] == 92 and sol["target_ref"] == FW.reference()
    # Even a duplicate confirmation cannot reselect or reinitialize tracking.
    confirm(plan, reader, bridge, session, rows.append)
    # The real leg starts later, not when C was requested or first dispatched.
    capture = collector._x1_enter_fw_leg(bridge, FW, None, 0, 1_880_000_000, rows.append)
    assert capture.due_ns == rows[-1]["sim_stamp_ns"] == 1_880_000_000
    assert [r for r in rows if r.get("step_index") is not None] == [
        dict(
            message_type="collector_gimbal_eval_fw_mode",
            step_index=0,
            mode="track",
            sim_stamp_ns=capture.due_ns,
            marker_monotonic_ns=NOW,
            target_ref=FW.reference(),
        )
    ]
    bridge.select_track_target.assert_called_once_with(FW, fw_ingress_slew=True)
    bridge.set_mode.assert_called_once_with("track")
    # A short first leg can still attach its genuine B crossing callback at entry.
    midpoint = SimpleNamespace(enabled=True, old_target=FW)
    collector._x1_enter_fw_leg(bridge, FW, midpoint, 0, capture.due_ns, rows.append)
    bridge.select_track_target.assert_called_with(FW, pass_midpoint=midpoint)
    bridge.set_mode.assert_called_once()


@pytest.mark.parametrize(
    "signal,clock",
    [
        (s, c)
        for s in ("ATTITUDE", "EXTENDED_SYS_STATE", "GLOBAL_POSITION_INT")
        for c in ("wall", "vehicle", "future", "missing")
    ],
)
def test_unfresh_confirmation_keeps_original_entry_even_after_state_recovers(rig, signal, clock):
    plan, reader, bridge, session = rig
    state = reader.state
    if clock == "wall":
        state.received_monotonic_ns[signal] = NOW - 200_000_001
    elif clock == "missing":
        del state.received_monotonic_ns[signal]
    else:
        value = 800 if clock == "vehicle" else 1002
        if signal == "EXTENDED_SYS_STATE":
            state.received_vehicle_ms[signal] = value
        else:
            state.get(signal)["time_boot_ms"] = value
    rows = []
    confirm(plan, reader, bridge, session, rows.append)
    assert not rows[-1]["requested"] and rows[-1]["skip_reason"]
    assert bridge.mode == "lock" and bridge.track_targets == (HOVER,)
    bridge.set_mode.assert_not_called()
    state.vehicle_time_boot_ms = 1100
    collector._x1_enter_fw_leg(bridge, FW, None, 0, 1_100_000_000, rows.append)
    assert bridge.mode == "track" and rows[-1]["sim_stamp_ns"] == 1_100_000_000
    assert bridge.track_targets == (FW,) and rows[-1]["target_ref"] == FW.reference()


@pytest.mark.parametrize(
    "case",
    [
        "ground",
        "transition",
        "no_target",
        "no_bridge",
        "non_bridge",
        "no_owner",
        "feedback_failure",
        "feedback_stale",
        "feedback_stale_vehicle",
        "no_feedback",
        "invalid_position",
        "invalid_attitude",
        "undefined_los",
    ],
)
def test_unmet_preconditions_do_not_select_any_target(rig, case):
    plan, reader, bridge, session = rig
    state = reader.state
    if case == "ground":
        state.get("EXTENDED_SYS_STATE")["landed_state"] = 1
    elif case == "transition":
        state.get("EXTENDED_SYS_STATE")["vtol_state"] = 5
    elif case == "no_target":
        plan.gimbal_eval_fw_targets = ()
    elif case == "no_bridge":
        bridge = None
    elif case == "non_bridge":
        plan.gimbal_bridge_stabilize = False
    elif case == "no_owner":
        session.owned = False
    elif case == "feedback_failure":
        session.latest_attitude[0]["failure_flags"] = 1
    elif case == "feedback_stale":
        session.latest_attitude = ({"failure_flags": 0, "time_boot_ms": 1000}, NOW - 500_000_001)
    elif case == "feedback_stale_vehicle":
        session.latest_attitude[0]["time_boot_ms"] = 500
    elif case == "no_feedback":
        session.latest_attitude = None
    elif case == "invalid_position":
        del state.get("GLOBAL_POSITION_INT")["lat"]
    elif case == "invalid_attitude":
        state.get("ATTITUDE")["roll"] = float("nan")
    elif case == "undefined_los":
        plan.gimbal_eval_fw_targets = (WorldTarget(0.0, 0.0, 500.0),)
    rows = []
    confirm(plan, reader, bridge, session, rows.append)
    assert not rows[-1]["requested"] and rows[-1]["skip_reason"]
    assert bridge is None or (bridge.mode, bridge.track_targets) == ("lock", (HOVER,))
    assert not any("step_index" in r for r in rows)


def test_non_fw_plan_keeps_event_and_control_untouched(rig):
    plan, reader, bridge, _ = rig
    plan.gimbal_eval_fw_legs = 0
    reader.latest_control_state = Mock(side_effect=AssertionError("unexpected FW work"))
    rows = []
    confirm(plan, reader, bridge, None, rows.append)
    assert rows == [
        dict(
            message_type="gimbal_measurement_fw_transition_complete",
            flight_state=collector._flight_state_snapshot(reader.state),
        )
    ]
    assert (bridge.mode, bridge.track_targets) == ("lock", (HOVER,))
