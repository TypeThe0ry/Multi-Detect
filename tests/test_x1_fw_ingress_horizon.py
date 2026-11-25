"""Single-thread survey capture: requests yield; only the synchronous writer owns a row."""

import io
import json
import math
from pathlib import Path
from types import CellType, CodeType, FunctionType, SimpleNamespace

import pytest

from multidetect.camera_mount import CameraMount
from multidetect.sim_teacher.continuous_video import ContinuousVideoWriter
from multidetect.sim_teacher.fw_ingress_horizon import FwIngressCaptureGuard
from multidetect.sim_teacher.gimbal_recording import DeltaCameraGeometry
from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget
from multidetect.sim_teacher.streams import (
    MavlinkReader,
    MavlinkState,
    SensorSample,
    SensorStreamReader,
)
from multidetect.sim_teacher.target_tracking import WorldTarget
from scripts import collect_sim_teacher_episodes as collector
from scripts.realworld.horizon_report import summarize
from scripts.realworld.long_range_flight import LongRangePolicy
from scripts.realworld.x1_gimbal_eval import frame_samples

MS = 1_000_000
BODY = {"roll": 0.0, "pitch": 0.0, "yaw": 23.0}
LOCK = {"roll": 0.0, "pitch": -37.0, "yaw": 5.0}


def test_survey_never_reenters_the_pre_route_fw_transition_wait():
    policy = LongRangePolicy(
        [(100.0, 0.0, -50.0), (200.0, 0.0, -50.0)], 80.0, vtol=True, gimbal_eval=True
    )
    actions = [policy.next_action() for _ in range(6)]
    assert [(a.kind, a.mode) for a in actions[:-1]] == [
        ("reposition", "FW"),
        ("reposition", "FW"),
        ("transition", "MC"),
        ("mc_reposition", "MC"),
        ("mc_hold", "MC"),
    ]
    assert actions[-1].kind == "stop"


def _video_tick_fixture(**bindings):
    # _video_tick is a closure in the live-only run_episode; bind CPU sensors
    # to its actual code, rather than copying its due/request/write branches.
    code = next(
        c
        for c in collector.run_episode.__code__.co_consts
        if isinstance(c, CodeType) and c.co_name == "_video_tick"
    )
    bindings.setdefault("fw_handback_boundary", None)
    cells = {name: CellType(bindings[name]) for name in code.co_freevars}
    tick = FunctionType(code, collector.run_episode.__globals__, closure=tuple(cells.values()))
    tick.__kwdefaults__ = {"wall_due_s": None}
    return tick, lambda: cells["next_video_sim_ns"].cell_contents


def test_late_handback_frames_keep_fw_attribution_when_video_writer_resumes(monkeypatch):
    written = []
    reader = SimpleNamespace(latest=None, request=lambda _: None)
    mav = SimpleNamespace(state=_state(3500), aligned_state=lambda _: _state(3500))
    monkeypatch.setattr(collector, "build_observation", lambda *_, **__: {})
    monkeypatch.setattr(collector, "_png_observation", lambda obs: obs)
    writer = SimpleNamespace(truncated=False, write=lambda obs, **kw: written.append({**obs, **kw}))
    tick, _ = _video_tick_fixture(
        camera_geometry=None, camera_mount=None, gimbal_session=None, horizon_stabilizer=None,
        last_video_stamp_ns=[-1], mav_reader=mav, next_video_request_s=0.,
        next_video_sim_ns=2000 * MS, fw_handback_boundary=(6, 2400 * MS),
        plan=SimpleNamespace(video_use_sim_time=True, video_hz=1., rgb_size=(1, 1), capture_depth=False),
        recorder=SimpleNamespace(record_mavlink=lambda event: pytest.fail(str(event))),
        sensor_reader=reader, simulation_gimbal_yaw_correction_deg=None, video_writer=writer,
        ingress_capture=None,
    )
    reader.latest = SimpleNamespace(sensor_stamp_ns=2100 * MS)
    tick(7, "transition_to_mc")
    reader.latest = SimpleNamespace(sensor_stamp_ns=3200 * MS)
    tick(7, "transition_to_mc")
    assert [(r["action_index"], r["flight_stage"]) for r in written] == [
        (6, "action"), (7, "transition_to_mc"),
    ]


def _state(ms):
    return MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"time_boot_ms": ms, "z": -50.0},
            "ATTITUDE": {"time_boot_ms": ms, **{a: math.radians(v) for a, v in BODY.items()}},
            "GLOBAL_POSITION_INT": {"time_boot_ms": ms, "lat": 0, "lon": 0, "alt": 500_000},
            "EXTENDED_SYS_STATE": {"vtol_state": 4, "landed_state": 2},
            "GPS_RAW_INT": {"fix_type": 3},
        }
    )


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("clear", None),
        ("on_deadline", "pre_route_video_due"),
        ("past_deadline", "pre_route_video_due"),
        ("in_flight", "pre_route_video_request_pending"),
        ("decoded_not_written", "pre_route_video_request_pending"),
    ],
)
def test_yield_confirmation_leg0_preserves_original_rows_and_horizon(
    tmp_path, monkeypatch, scenario, reason
):
    guard = FwIngressCaptureGuard()
    reader = SensorStreamReader(io.BytesIO(), request_stream=io.BytesIO(), meta_requests=True)
    writer = ContinuousVideoWriter(tmp_path, hz=1.0, max_bytes=100_000, require_gimbal=True)
    bridge = BridgeHorizonTarget(LOCK, mode="lock", yaw_mode="follow", status_sender=(1, 1))
    geometry = DeltaCameraGeometry.from_sdf(
        Path(__file__).resolve().parents[1]
        / "simulation/gazebo/models/delta_tailsitter_gimbal_cam/model.sdf"
    )
    mav = MavlinkReader(None)
    monkeypatch.setattr(mav, "aligned_state", lambda ns: _state(ns // MS))
    plan = SimpleNamespace(
        video_use_sim_time=True,
        video_hz=1.0,
        rgb_size=(1, 1),
        capture_depth=False,
        gimbal_eval_fw_legs=1,
        gimbal_bridge_stabilize=True,
        gimbal_eval_fw_targets=(WorldTarget(0.01, 0.02, 100.0),),
    )
    events = []
    now_ns = 10**10
    monkeypatch.setattr(collector.time, "monotonic_ns", lambda: now_ns)
    wall = [10.0]
    monkeypatch.setattr(collector.time, "monotonic", lambda: wall[0])
    tick, due = _video_tick_fixture(
        camera_geometry=geometry,
        camera_mount=CameraMount.gimbal(),
        gimbal_session=None,
        horizon_stabilizer=bridge,
        last_video_stamp_ns=[-1],
        mav_reader=mav,
        next_video_request_s=0.0,
        next_video_sim_ns=1000 * MS,
        plan=plan,
        recorder=SimpleNamespace(record_mavlink=events.append),
        sensor_reader=reader,
        simulation_gimbal_yaw_correction_deg=None,
        video_writer=writer,
        ingress_capture=guard,
    )

    def expose(ms):
        # Raw measured joint and lock/track command at *exposure*, not receipt.
        stamp = ms * MS
        result = bridge.update(
            BODY,
            now_s=ms / 1000.0,
            attitude_time_s=ms / 1000.0,
            position=WorldTarget(0.0, 0.0, 500.0),
        )
        bridge.record_command(result, ms / 1000.0)
        bridge.record_bridge_status(
            {
                "message_type": "NAMED_VALUE_FLOAT",
                "name": "c11_hdeg",
                "_source_system": 1,
                "_source_component": 1,
                "time_boot_ms": ms,
                "value": float(ms == 2010),
            }
        )
        return SensorSample(
            rgb=b"rgb",
            width=1,
            height=1,
            depth=None,
            camera_info={"width": 1, "height": 1},
            pose={},
            velocity=[0.0, 0.0, 0.0],
            sensor_stamp_ns=stamp,
            received_monotonic_ns=now_ns,
            received_wall_time=1.0,
            joint_state={
                "source_timestamp_ns": stamp,
                "gimbal_joint_state": {
                    "source": "gazebo_joint_state",
                    "fresh": True,
                    "timestamp_ns": stamp,
                    "position_rad": {
                        "roll": 0.0,
                        "pitch": math.radians(-17.0 if ms == 2010 else -37.0),
                        "yaw": 0.0,
                    },
                },
            },
        )

    def video(index, stage, ms):
        mav.state.messages.update(_state(ms).messages)
        wall[0] += 2.0  # allow the existing request throttle to retry without sleeping
        tick(index, stage)

    reader.latest = expose(1000)
    video(-1, "pre_transition_hover", 1000)
    video(-1, "transition_to_fw", 2000)  # GET, then return to the wait loop
    assert guard.pending_due_ns == 2000 * MS and writer.frames_written == 1
    video(-1, "transition_to_fw", 2001)  # same deadline, no second writer callback
    reader.latest = expose(2010)
    video(-1, "transition_to_fw", 2012)
    assert guard.pending_due_ns is None and due() == 3010 * MS
    # Generic GETs do not own a future -1 write, even if still in flight.
    reader.request(2250 * MS)
    reader.request_meta(2500 * MS)
    event_ms = {"clear": 2999, "on_deadline": 3010, "past_deadline": 3011}.get(scenario, 3030)
    delayed = None
    if scenario in ("in_flight", "decoded_not_written"):
        video(-1, "transition_to_fw", 3010)
        delayed = expose(3025)
        if scenario == "decoded_not_written":
            reader.latest = delayed
    assert writer.frames_written == 2
    pre_manifest = writer.manifest_path.read_bytes()
    state = mav.state
    state.messages.update(_state(event_ms).messages)
    state.vehicle_time_boot_ms = event_ms
    state.received_monotonic_ns = dict.fromkeys(state.messages, now_ns)
    state.received_vehicle_ms = {"EXTENDED_SYS_STATE": event_ms}
    session = SimpleNamespace(
        owned=True, latest_attitude=({"failure_flags": 0, "time_boot_ms": event_ms}, now_ns)
    )
    collector._x1_fw_transition_complete(
        plan, mav, bridge, session, events.append, capture_guard=guard, next_video_sim_ns=due()
    )
    assert events[-1]["skip_reason"] == events[-1]["capture"]["skip_reason"] == reason
    assert events[-1]["requested"] is (reason is None)
    assert bridge.mode == ("track" if scenario == "clear" else "lock")
    cap = collector._x1_enter_fw_leg(
        bridge, plan.gimbal_eval_fw_targets[0], None, 0, 3100 * MS, events.append
    )
    assert cap.due_ns == 3100 * MS and bridge.mode == "track"
    assert [row["message_type"] for row in events] == [
        "gimbal_measurement_fw_transition_complete",
        "collector_gimbal_fw_ingress",
        "collector_gimbal_eval_fw_mode",
    ]
    reader.latest = delayed if delayed is not None else expose(3150)
    video(0, "action", 3150)
    assert writer.manifest_path.read_bytes().startswith(pre_manifest)
    rows = [json.loads(line) for line in writer.manifest_path.read_text().splitlines()]
    assert [(r["action_index"], r["sensor_stamp_ns"]) for r in rows] == [
        (-1, 1000 * MS),
        (-1, 2010 * MS),
        (0, reader.latest.sensor_stamp_ns),
    ]
    assert rows[-1]["gimbal_state"]["stabilization"]["mode"] == (
        "lock" if delayed is not None else "track"
    )
    assert writer.close()["gimbal_check"]["passed"]
    st = rows[1]["gimbal_state"]["stabilization"]
    assert st["mode"] == "lock" and st["measurement_fresh"] and st["horizon_degraded"]
    assert st["desired_earth_deg"] == pytest.approx({**LOCK, "yaw": 28.0})
    assert st["measured_earth_deg"]["pitch"] == pytest.approx(-17.0)
    report = summarize(writer.manifest_path, expected_mode="lock", exclude_action_below=1)[
        "horizon_acceptance"
    ]
    assert report["feasible_frames"] == 2 and report["degraded_frames"] == 1
    assert report["feasible_error"]["max_deg"] == pytest.approx(20.0)
    assert not report["passed"]  # the bad pre-route measurement still fails the original gate
    _, _, _, errors = frame_samples(
        writer.manifest_path, {"horizon_stabilization": {"eval_fw_legs": 1}}
    )
    assert errors == pytest.approx([0.0, 20.0], abs=1.0e-8)


def test_only_an_actual_commit_can_close_a_video_request():
    guard = FwIngressCaptureGuard()

    def evidence():
        return guard.evidence(next_video_sim_ns=3000 * MS, vehicle_time_ms=2500)["skip_reason"]

    assert evidence() == "pre_route_video_not_committed"
    guard.note_commit(1000 * MS)
    guard.note_request(2000 * MS)
    assert evidence() == "pre_route_video_request_pending"  # even with a future deadline
    guard.note_commit(1999 * MS)  # impossible proof: cannot satisfy the request
    assert guard.pending_due_ns == 2000 * MS and evidence() == "pre_route_video_history_invalid"


@pytest.mark.parametrize(
    "due,vehicle",
    [(None, 1100), (1500 * MS, None), (1500 * MS, True), (1000 * MS, 1100), (1500 * MS, 999)],
)
def test_unknown_or_inconsistent_clock_falls_back(due, vehicle):
    guard = FwIngressCaptureGuard()
    guard.note_commit(1000 * MS)
    assert (
        guard.evidence(next_video_sim_ns=due, vehicle_time_ms=vehicle)["skip_reason"]
        == "pre_route_video_clock_unproven"
    )
