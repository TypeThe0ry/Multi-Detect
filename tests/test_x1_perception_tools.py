import json
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from test_x1_planner import _fixture

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.motion_intent import MotionCommand
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_executor import (
    CADENCE_EVENT_ID_BASE,
    DECISION_SCHEMA,
    REQUEST_SCHEMA,
    RuntimeFrame,
)
from multidetect.autonomy.x1_mission import X1MissionStep
from multidetect.autonomy.x1_perception_tools import (
    TOOL_SCHEMA,
    BatteryReading,
    FrameSummary,
    GimbalReading,
    PerceptionSources,
    RouteReading,
    TargetReading,
    ToolProtocolError,
    ToolRunner,
)
from multidetect.domain import BoundingBox, Detection
from multidetect.onboard_runtime.camera import CameraCalibration
from multidetect.sim_teacher.gimbal_readback import attitude_readback
from multidetect.sim_teacher import x1_scenario_decision
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, llama_decisions
from multidetect.terrain_dem import LocalDemGrid
from scripts.run_x1_autonomy_demo import demo_perception

PROFILE = Path(__file__).resolve().parents[1] / "configs/airframes/delta_tailsitter_gimbal.json"


def request(deadline=12_000):
    return dict(
        schema=REQUEST_SCHEMA,
        deadline_ms=deadline,
        request_id="sortie:1",
        event_id=1,
        state=dict(mission_id="sortie", revision=1, telemetry={}),
    )


def call(turn, tool, **overrides):
    return json.dumps(
        dict(
            dict(
                schema=TOOL_SCHEMA,
                request_id=turn["request_id"],
                call_index=turn["perception"]["call_index"],
                tool=tool,
                args={},
            ),
            **overrides,
        )
    )


def action(turn):
    return json.dumps(
        dict(
            schema=DECISION_SCHEMA,
            request_id=turn["request_id"],
            event_id=turn["event_id"],
            mission_id=turn["state"]["mission_id"],
            revision=turn["state"]["revision"],
            action="gimbal-retarget",
            params={"pitch_deg": -45, "yaw_deg": 0},
        )
    )


def once(sources, tool):
    turns = []

    def backend(turn):
        turns.append(turn)
        return call(turn, tool) if len(turns) == 1 else action(turn)

    req = request()
    assert ToolRunner(sources, clock_ms=lambda: 10_000).run(
        req, backend, req["deadline_ms"]
    ) == action(req)
    return turns[1]["perception"]["results"][0]


def test_real_readback_and_camera_projection_only_expose_structured_detections():
    profile = load_airframe_profile(PROFILE)
    readback = attitude_readback(
        {"q": [1, 0, 0, 0], "gimbal_device_id": 1},
        {"roll": 0, "pitch": 0, "yaw": 0},
        gazebo_body_joints=True,
    )
    limits = {"pitch": (-90, 0), "roll": (-45, 45), "yaw": (-180, 180)}
    gimbal = GimbalReading(9_960, readback, limits)
    calibration = CameraCalibration(640, 480, 320, 240, 320, 240)
    detections = (
        Detection(
            "marker", 0.9, BoundingBox(0.4, 0.4, 0.6, 0.6), metadata={"pixels": b"do not send"}
        ),
        Detection("roof", 0.5, BoundingBox(0.7, 0.4, 0.9, 0.6)),
    )
    frame = FrameSummary(9_990, calibration, detections, gimbal, "mc")
    sources = PerceptionSources(frame=lambda: frame, gimbal=lambda: gimbal, profile=profile)
    result = once(sources, "look")
    assert result["ok"] and result["age_ms"] == 40
    center, right = result["data"]["detections"]
    assert set(center) == {"class", "confidence", "bbox", "bearing_deg", "elevation_deg"}
    assert center["class"] == "marker" and right["bearing_deg"] > center["bearing_deg"]
    assert (
        once(replace(sources, frame=lambda: replace(frame, detections=detections[::-1])), "look")
        == result
    )
    failed = replace(gimbal, readback={**readback, "failure_flags": 1})
    assert (
        once(replace(sources, gimbal=lambda: failed), "gimbal_state")["error"]
        == "gimbal_readback_unavailable"
    )
    assert once(sources, "gimbal_state")["data"]["angles_deg"] == {
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": 0.0,
    }
    assert once(replace(sources, frame=lambda: replace(frame, captured_ms=9_800)), "look")[
        "error"
    ] == ("frame_pose_not_aligned")


@pytest.fixture
def dem():
    return LocalDemGrid(
        scenario_id="fixture",
        origin_latitude_deg=10.0,
        origin_longitude_deg=20.0,
        origin_elevation_m=100.0,
        source_sha256="a" * 64,
        north_origin_m=0,
        east_origin_m=0,
        north_spacing_m=25,
        east_spacing_m=25,
        elevations_m=((100.0, 100.0), (110.0, 110.0)),
        vertical_sigma_m=1,
        slope_sigma_m_per_m=0.1,
    )


def test_route_and_dem_use_existing_grid_and_do_not_invent_out_of_coverage_values(dem):
    step = X1MissionStep(
        "fw_goto_waypoint",
        "wp",
        MotionCommand("reposition", target_ned=(20.0, 0.0, -50.0), mode="FW"),
    )
    route = RouteReading(
        9_990, (step, X1MissionStep("land", "end")), 0, (0.0, 0.0, -50.0), (0.0, 0.0, 100.0), 40
    )
    sources = PerceptionSources(route=lambda: route, dem=dem)
    data = once(sources, "terrain_ahead")["data"]
    assert len(data["profile"]) == 9
    assert data["profile"][-1] == {
        "distance_m": 20.0,
        "elevation_amsl_m": 108.0,
        "clearance_m": 42.0,
    }
    assert data["min_sample_margin_m"] == 2.0
    assert [s["endpoint_distance_m"] for s in once(sources, "route_status")["data"]["steps"]] == [
        20.0,
        None,
    ]
    outside = replace(step, command=replace(step.command, target_ned=(40.0, 0.0, -50.0)))
    assert (
        once(replace(sources, route=lambda: replace(route, steps=(outside,))), "terrain_ahead")[
            "error"
        ]
        == "dem_segment_outside_coverage"
    )
    assert (
        once(replace(sources, route=lambda: replace(route, active_index=1)), "terrain_ahead")[
            "error"
        ]
        == "straight_active_segment_unavailable"
    )


def test_demo_publishes_existing_route_dem_and_gimbal_without_passing_write_ports(dem):
    home = SimpleNamespace(document={"home": (5.0, 0.0)})
    step = X1MissionStep(
        "fw_goto_waypoint",
        "wp",
        MotionCommand("reposition", target_ned=(20.0, 0.0, -50.0), mode="FW"),
    )
    steps = (X1MissionStep("vtol_takeoff", "wp"), step, X1MissionStep("vtol_land", "end"))
    runner, publish = demo_perception(home, load_airframe_profile(PROFILE), dem, steps)
    now_ns = time.monotonic_ns()
    hold = SimpleNamespace(
        session=SimpleNamespace(
            latest_attitude=(
                SimpleNamespace(q=[1, 0, 0, 0], gimbal_device_id=1, time_boot_ms=100),
                now_ns,
            )
        ),
        reader=SimpleNamespace(attitude_at=lambda _: None),
        target={"roll": 0, "pitch": 0, "yaw": 0},
        yaw_correction_deg=0.0,
        gazebo_body_joints=True,
    )
    collector = SimpleNamespace(
        native_active=False, _motion_position=lambda row: (5.0, 0.0, 152.0) if row else None
    )
    samples = {"GLOBAL_POSITION_INT": {"receive_monotonic_ns": now_ns, "time_boot_ms": 109}}
    req = request(now_ns // 1_000_000 + 2_000)

    def observe(tool):
        result = []

        def backend(turn):
            if turn["perception"]["call_index"] == 0:
                return call(turn, tool)
            result.append(turn["perception"]["results"][0])
            return action(turn)

        runner.run(req, backend, req["deadline_ms"])
        return result[0]

    publish(collector, 0, samples, hold, 110, True)
    assert observe("route_status")["data"]["steps"][0]["step_id"] == "wp"
    assert observe("terrain_ahead")["data"]["min_sample_margin_m"] == 2.0
    assert observe("gimbal_state")["data"]["device_id"] == 1
    publish(collector, 0, samples, hold, 1_110, True)
    assert observe("route_status")["error"] == "active_route_unavailable"
    collector.native_active = True
    publish(collector, 0, samples, hold, 110, True)
    assert observe("route_status")["error"] == "active_route_unavailable"
    publish(collector, 0, samples, hold, 1_101, True)
    assert observe("gimbal_state")["error"] == "gimbal_readback_unavailable"


def test_target_visibility_requires_visual_confirmation_and_energy_requires_bound():
    update = SimpleNamespace(
        primary_track_id="target:4",
        captured_at_s=10.0,
        visual_confirmed_track_ids=(),
        tracks=[SimpleNamespace(track_id="target:4", last_seen_at_s=9.75)],
    )
    target = TargetReading(update, "target:4", 9_750, (10.0, 20.0, 103.0))
    battery = BatteryReading(30, 9_950, 16.0, 2.5, 9_975)
    sources = PerceptionSources(target=lambda: target, battery=lambda: battery)
    row = once(sources, "target_status")
    assert row["age_ms"] == 250 and row["data"]["visible"] is False
    assert row["data"]["last_seen_age_ms_at_snapshot"] == 250
    update.visual_confirmed_track_ids = ("target:4",)
    assert once(sources, "target_status")["data"]["visible"] is True
    assert (
        once(
            replace(sources, target=lambda: replace(target, position_track_id="target:5")),
            "target_status",
        )["error"]
        == "tracked_position_unavailable"
    )
    assert once(sources, "battery_status")["data"] == {
        "percent": 30,
        "remaining_wh": 16.0,
        "absolute_error_wh": 2.5,
    }
    partial = replace(sources, battery=lambda: replace(battery, absolute_error_wh=None))
    assert once(partial, "battery_status")["error"] == "bounded_energy_unavailable"


@pytest.mark.parametrize(
    "tool,overrides,error",
    [
        ("gimbal_control", {}, "invalid_tool_call"),
        ("battery_status", {"args": {"actuate": True}}, "invalid_tool_call"),
        ("battery_status", {"args": []}, "invalid_tool_call"),
        ("battery_status", {"call_index": True}, "invalid_tool_call"),
        ("battery_status", {"request_id": "other:1"}, "unmatched_tool_call"),
        ("battery_status", {"call_index": 1}, "unmatched_tool_call"),
    ],
)
def test_allowlist_and_binding_reject_before_read(tool, overrides, error):
    reads = []
    runner = ToolRunner(
        PerceptionSources(battery=lambda: reads.append(True)), clock_ms=lambda: 10_000
    )
    with pytest.raises(ToolProtocolError, match=error):
        runner.run(request(), lambda turn: call(turn, tool, **overrides), 12_000)
    assert reads == []


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        "[]",
        '{"schema":1,"schema":2}',
        '{"value":1e999}',
        b"\xff",
        "x" * 6145,
        '{"schema":"\\ud800"}',
    ],
)
def test_invalid_backend_json(reply):
    with pytest.raises(ToolProtocolError, match="invalid_backend_json"):
        ToolRunner(clock_ms=lambda: 10_000).run(request(), lambda _: reply, 12_000)


def test_final_reply_requires_a_valid_action():
    with pytest.raises(ToolProtocolError, match="invalid_final_action"):
        ToolRunner(clock_ms=lambda: 10_000).run(
            request(), lambda turn: action(turn).replace("gimbal-retarget", "look"), 12_000
        )


def test_tool_loop_keeps_frozen_state_index_order_and_budget():
    window = request()
    turns = []
    reads = []
    battery = BatteryReading(50, 9_900, 20, 3, 9_900)
    runner = ToolRunner(
        PerceptionSources(battery=lambda: reads.append(True) or battery), clock_ms=lambda: 10_000
    )

    def backend(turn):
        p = turn["perception"]
        assert turn["state"] == window["state"]
        assert p["state_sha256"] == canonical_sha256(window["state"])
        assert turn["deadline_ms"] == window["deadline_ms"]
        index = p["call_index"]
        assert p["calls_remaining"] == 3 - index
        assert len(p["results"]) == index
        if p["results"]:
            assert p["results"][0]["data"]["percent"] == 50
            p["results"][0]["data"]["percent"] = -5
        turns.append(index)
        turn["state"]["telemetry"]["poison"] = True
        return call(turn, "battery_status")

    with pytest.raises(ToolProtocolError, match="call_budget_exhausted"):
        runner.run(window, backend, window["deadline_ms"])
    assert turns == [0, 1, 2, 3] and reads == [True] * 3
    assert runner.run(window, action, window["deadline_ms"]) == action(window)


def test_stale_or_future_samples_have_no_data_and_final_cannot_use_expired_sample():
    battery = BatteryReading(50, 9_800, 20, 3, 8_999)
    sources = PerceptionSources(battery=lambda: battery)
    row = once(sources, "battery_status")
    assert (row["ok"], row["data"], row["age_ms"], row["error"]) == (
        False,
        None,
        1001,
        "stale_observation",
    )
    battery = replace(battery, energy_captured_ms=10_001)
    assert once(sources, "battery_status")["error"] == "future_observation"
    battery = replace(battery, energy_captured_ms=9_900, percent_captured_ms=9_900)
    now = [10_000]

    def backend(turn):
        if turn["perception"]["call_index"] == 0:
            return call(turn, "battery_status")
        assert turn["perception"]["results"][0]["ok"]
        now[0] += 901
        return action(turn)

    with pytest.raises(ToolProtocolError, match="tool_result_expired"):
        ToolRunner(sources, clock_ms=lambda: now[0]).run(request(), backend, 12_000)


def test_per_call_timeout_discards_late_return_and_bounds_outstanding_reads():
    release = Event()
    entered = []

    def read():
        entered.append(True)
        release.wait(5)

    def backend(turn):
        p = turn["perception"]
        if p["call_index"] == 0:
            return call(turn, "battery_status")
        assert p["results"][0]["error"] == "tool_timeout"
        if p["call_index"] == 1:
            return call(turn, "battery_status")
        assert p["results"][1]["error"] == "tool_busy"
        assert all(row["data"] is None for row in p["results"])
        return action(turn)

    runner = ToolRunner(PerceptionSources(battery=read), per_call_ms=30, total_ms=500)
    req = request(time.monotonic_ns() // 1_000_000 + 1000)
    try:
        assert runner.run(req, backend, req["deadline_ms"]) == action(req)
        assert entered == [True]
    finally:
        release.set()


def test_total_latency_bounds_blocked_backend_and_old_ticket_deadline():
    release = Event()
    runner = ToolRunner(total_ms=35)
    now = time.monotonic_ns() // 1_000_000
    req = request(now + 1000)
    try:
        with pytest.raises(ToolProtocolError, match="window_timeout"):
            runner.run(req, lambda turn: release.wait(5) and action(turn), req["deadline_ms"])
        with pytest.raises(ToolProtocolError, match="backend_busy"):
            runner.run(req, action, req["deadline_ms"])
        with pytest.raises(ToolProtocolError, match="window_timeout"):
            runner.run(req, action, now)
    finally:
        release.set()


def decision_replay(backend, *, cadence=0, sources=None):
    package = _fixture("Fly over the yard.")
    now = [10_000]
    traces = []
    runtime = DecisionTick(
        package,
        load_airframe_profile(PROFILE),
        backend,
        None,
        traces.append,
        run_id="fixture",
        plan_digest="f" * 64,
        now_ms=lambda: now[0],
        cpu_replay=True,
        decision_cadence_s=cadence,
        tool_runner=ToolRunner(sources, clock_ms=lambda: now[0]),
    )
    frame = RuntimeFrame(
        1,
        now[0],
        package.mission_id,
        1,
        0,
        False,
        True,
        "mc",
        85,
        None,
        None,
        {},
        gimbal_available=True,
    )
    return runtime, frame, now, traces


def progress():
    return {"phase": "cruise", "step_index": 0, "remaining_plan_sha256": "a" * 64}


@pytest.mark.parametrize("cadence", [False, True])
@pytest.mark.parametrize("invalid_final,expected", [(False, 1), (True, 0)])
def test_decision_tick_passes_only_final_action_to_executor(invalid_final, expected, cadence):
    turns = []

    def backend(turn):
        turns.append(turn)
        if turn["perception"]["call_index"] == 0:
            return call(turn, "look")
        assert turn["perception"]["results"][0]["error"] == "frame_summary_unavailable"
        return "not json" if invalid_final else action(turn)

    runtime, frame, now, traces = decision_replay(backend, cadence=20 if cadence else 0)
    context = {"mission_progress": progress()} if cadence else {}
    try:
        runtime.tick(frame, events=() if cadence else (("target_lost", "tracker"),), **context)
        if invalid_final:
            with pytest.raises(ToolProtocolError, match="invalid_backend_json"):
                runtime.future.result(timeout=2)
        else:
            runtime.future.result(timeout=2)
        assert runtime.replay_commands == []
        now[0] += 10
        runtime.tick(replace(frame, t_ms=now[0]), **context)
        assert len(runtime.replay_commands) == expected
        assert (
            traces[-1]["message_type"]
            == f"scenario_decision_{'error' if invalid_final else 'receipt'}"
        )
        if invalid_final:
            assert traces[-1]["reason"] == "invalid_backend_json"
        assert len(turns) == 2
        assert {k: v for k, v in turns[0].items() if k != "perception"} == {
            k: v for k, v in turns[1].items() if k != "perception"
        }
        assert turns[0]["event_id"] == (CADENCE_EVENT_ID_BASE + 1 if cadence else 1)
    finally:
        runtime.close()


@pytest.mark.parametrize("blocked_turn", ["tool_read", "final_model"])
def test_new_event_preempts_cadence_mid_tool_turn(blocked_turn):
    entered, release = Event(), Event()
    turns = []

    def block():
        entered.set()
        assert release.wait(2)

    def battery():
        if blocked_turn == "tool_read":
            block()
        return BatteryReading(65, 10_000, 25, 3, 10_000)

    def backend(turn):
        turns.append((turn["request_id"], turn.get("trigger", "event"), turn["perception"]["call_index"]))
        if turn.get("trigger") != "cadence":
            return action(turn)
        if turn["perception"]["call_index"] == 0:
            return call(turn, "battery_status")
        assert turn["perception"]["results"][0]["ok"]
        block()
        return action(turn)

    runtime, frame, now, traces = decision_replay(backend, cadence=20, sources=PerceptionSources(battery=battery))

    def tick(elapsed_ms, **kwargs):
        now[0] = 10_000 + elapsed_ms
        runtime.tick(replace(frame, t_ms=now[0]), mission_progress=progress(), **kwargs)

    try:
        tick(0)
        assert entered.wait(2)
        old = runtime.future
        tick(20, events=(("target_lost", "tracker"),))
        assert traces[-1]["reason"] == "preempted_by_event"
        assert runtime.future is old
        assert runtime.replay_commands == []
        release.set()
        with pytest.raises(ToolProtocolError, match="window_cancelled"):
            old.result(timeout=2)
        tick(30)
        runtime.future.result(timeout=2)
        tick(40)
        assert turns == [("fixture:1", "cadence", 0)] + (
            [("fixture:1", "cadence", 1)] if blocked_turn == "final_model" else []
        ) + [("fixture:2", "event", 0)]
        assert runtime.replay_commands == [("gimbal_control", -45, 0)]
        assert [(row["trigger"], row["status"]) for row in runtime.recent_receipts] == [
            ("cadence", "rejected"), ("event", "accepted_unconfirmed")
        ]
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize("elapsed,reason", [(1001, "tool_result_expired"), (2500, "window_timeout")])
def test_collector_rechecks_tool_reply_before_admission(elapsed, reason):
    def backend(turn):
        if turn["perception"]["call_index"] == 0:
            return call(turn, "battery_status")
        assert turn["perception"]["results"][0]["ok"]
        return action(turn)

    sources = PerceptionSources(battery=lambda: BatteryReading(65, 10_000, 25, 3, 10_000))
    runtime, frame, now, traces = decision_replay(backend, cadence=20, sources=sources)
    try:
        runtime.tick(frame, mission_progress=progress())
        runtime.future.result(timeout=2)
        now[0] += elapsed
        runtime.tick(replace(frame, t_ms=now[0]), mission_progress=progress())
        assert runtime.replay_commands == []
        assert runtime.recent_receipts[-1]["status"] == "rejected"
        assert traces[-1]["message_type"] == "scenario_decision_error"
        assert traces[-1]["reason"] == reason
    finally:
        runtime.close()


def test_llama_prompt_only_advertises_tools_for_perception_requests(monkeypatch):
    prompts = []

    def urlopen(req, *, timeout):
        from contextlib import nullcontext
        from io import BytesIO

        prompts.append(json.loads(req.data)["messages"][0]["content"])
        return nullcontext(BytesIO(b'{"choices":[{"message":{"content":"reply"}}]}'))

    monkeypatch.setattr(x1_scenario_decision, "urlopen", urlopen)
    post = llama_decisions(9999, "Fly over the yard.")
    assert post(request()) == post({**request(), "perception": {"tools": ["look"]}}) == "reply"
    assert TOOL_SCHEMA not in prompts[0]
    assert TOOL_SCHEMA in prompts[1]
    assert all("event_id comes from the request" in prompt for prompt in prompts)
