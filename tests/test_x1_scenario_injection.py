from __future__ import annotations

import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from test_run_x1_autonomy_demo import inputs, raw_plan  # noqa: F401
from test_x1_planner import _fixture
from test_x1_wind_estimate import MS
from test_x1_wind_estimate import rows as wind_rows

from multidetect.autonomy.x1_contingency_executor import RuntimeFrame
from multidetect.autonomy.x1_contingency_replay import Fault, replay_states
from multidetect.sim_teacher.x1_scenario import (
    CollectorScenario,
    ScenarioTimeline,
    SitlScenarioTransport,
    load_schedule,
    wind_baseline,
    wind_server_config,
)
from multidetect.sim_teacher.x1_scenario_decision import (
    DecisionTick,
    GimbalIngress,
    recorded_decisions,
)
from scripts import run_x1_autonomy_demo as demo

ROOT = Path(__file__).resolve().parents[1]
SCHEDULE = ROOT / "configs/simulation/x1-contingency-faults-v1.json"


def test_select_mixed_schedule_explicitly():
    with pytest.raises(ValueError, match="select supported"):
        load_schedule(SCHEDULE)
    result = load_schedule(SCHEDULE, ["terrain_conflict", "wind_step", "target_lost", "link_loss", "battery_drop"])
    assert [fault.at_ms for fault in result] == [3000, 12000, 16000, 22000, 34000]
    assert result[-1].values == {"clearance_m": -5, "conflict": True}


def test_timeline_all_five_with_actuator_restore():
    faults = load_schedule(SCHEDULE, ("wind_step", "battery_drop", "link_loss", "target_lost", "terrain_conflict"))
    commands, markers = [], []
    params = {"SIM_BAT_DRAIN": 300.0, "SIM_BAT_MIN_PCT": 50.0}
    clock = [10000]
    battery = [95]

    def command(*args):
        commands.append(args)
        if args[0] == "gz":
            return "Subscribers [Address, Message Type]:\n  tcp://local, gz.msgs.Wind"
        if args[1] == "set":
            params[args[2]] = float(args[3])
            if args[2] == "SIM_BAT_MIN_PCT" and params[args[2]] == 95:
                battery[0] = 95
            return ""
        return str(params[args[-1]])

    port = SitlScenarioTransport(command, wind=("/world/default/wind", (3, 2, 1)),
                                 battery_sample=lambda: battery[0], clearance=lambda: 35)
    port.prepare(faults)
    timeline = ScenarioTimeline(faults, lambda: clock[0], port, markers.append, origin_ms=clock[0])

    def tick(ms):
        clock[0] = 10000 + ms
        timeline.tick()

    tick(2999)
    assert not markers
    tick(3000)
    assert commands[-1][-1] == "enable_wind: true linear_velocity: {x: -4 y: 8 z: 0}"
    tick(3000)
    assert len(markers) == 1
    tick(10000)
    assert commands[-1][-1].endswith("{x: 3 y: 2 z: 1}")
    tick(12000)
    assert params == {"SIM_BAT_DRAIN": 1, "SIM_BAT_MIN_PCT": 17}
    battery[0] = 18
    tick(16000)
    assert port.link_suppressed.is_set()
    tick(22000)
    assert not port.link_suppressed.is_set()
    assert port.target_observation({"visible": True, "track_ref": "trk"}) == {"visible": False, "track_state": "lost"}
    tick(24000)
    assert params == {"SIM_BAT_DRAIN": 300, "SIM_BAT_MIN_PCT": 50}
    assert battery[0] == 95
    tick(27000)
    tracking = {"visible": True, "track_ref": "new-track"}
    assert port.target_observation(tracking) is tracking
    tick(34000)
    assert port.terrain_offset == 40
    assert port.terrain_observation() == {"conflict": True, "clearance_m": -5}
    tick(39000)
    assert port.terrain_observation() == {"conflict": False, "clearance_m": 35}
    timeline.close(completed=True)
    assert len(markers) == 10
    assert {row["fault_index"] for row in markers if row["message_type"] == "scenario_injected"} == set(range(5))
    assert all(row["origin_boot_ms"] + row["scheduled_offset_ms"] == row["observed_boot_ms"] for row in markers)
    assert [(r["fault"], r["message_type"]) for r in markers if r["observed_boot_ms"] == 32000] == [
        ("link_loss", "scenario_restored"), ("target_lost", "scenario_injected")]


def test_partial_write_restored_and_missed_window_not_replayed():
    marks = []
    clock = [30]
    fault = Fault(1, 2, "link_loss", "link", {})
    restore_calls = []

    def error(_):
        raise RuntimeError("write timed out")

    transport = SimpleNamespace(apply=error, restore=lambda f: (restore_calls.append(f), {})[1])
    timeline = ScenarioTimeline((fault,), lambda: clock[0], transport, marks.append, origin_ms=30)
    clock[0] = 31
    with pytest.raises(RuntimeError, match="timed out"):
        timeline.tick()
    timeline.close()
    assert restore_calls == [fault]
    assert [m["message_type"] for m in marks] == ["scenario_error", "scenario_restored"]
    timeline = ScenarioTimeline((fault,), lambda: clock[0], transport, marks.append, origin_ms=30)
    clock[0] = 32
    with pytest.raises(RuntimeError, match="window missed"):
        timeline.tick()
    assert marks[-1]["message_type"] == "scenario_missed"


@pytest.mark.parametrize("ramp", [0, 3500, 12.5, -1, 3500.1, True, "100", None,
                                  float("inf"), float("nan")])
def test_schedule_optional_wind_ramp(tmp_path, ramp):
    schedule = tmp_path / "wind.json"
    doc = json.loads(SCHEDULE.read_text())
    wind = doc["faults"][0]
    wind["values"]["ramp_ms"] = ramp
    schedule.write_text(json.dumps({**doc, "faults": [wind]}))
    if type(ramp) in (int, float) and 0 <= ramp <= wind["duration_ms"] / 2:
        assert load_schedule(schedule)[0].values["ramp_ms"] == ramp
    else:
        with pytest.raises(ValueError):
            load_schedule(schedule)


def test_ramp_metadata_is_not_replay_telemetry(tmp_path):
    capture = tmp_path / "mavlink.jsonl"
    capture.write_text('{"receive_monotonic_ns": 0, "message_type": "HEARTBEAT"}\n')
    fault = Fault(200, 1000, "wind_step", "wind",
                  {"north_mps": -2, "east_mps": 8, "down_mps": -6, "ramp_ms": 200})
    rows = {row["t_ms"]: row for row in replay_states(capture, mission_id="ramp", faults=(fault,))}
    assert rows[200]["telemetry"]["wind"] == {
        "north_mps": -2, "east_mps": 8, "down_mps": -6, "source": "fault", "age_ms": 0,
    }


def _wind_harness(ramp_ms, *, command=None, adjacent=False):
    clock, commands, markers = [1000], [], []
    values = {"east_mps": 8, "north_mps": -2, "down_mps": -6}
    if ramp_ms is not None:
        values["ramp_ms"] = ramp_ms
    faults = (Fault(200, 1000, "wind_step", "wind", values),)
    if adjacent:
        faults += (Fault(1000, 1100, "wind_step", "wind", {**values, "ramp_ms": 0}),)

    def run(*args):
        commands.append(args)
        if command is not None:
            command(args[-1])

    transport = SitlScenarioTransport(run, wind=("/world/fixture/wind", (4, 2, 0)))
    timeline = ScenarioTimeline(faults, lambda: clock[0], transport, markers.append, origin_ms=clock[0])

    def tick(offset):
        clock[0] = 1000 + offset
        timeline.tick()

    def vectors():
        return [tuple(float(payload.partition("{")[2].split(f"{axis}: ")[1].split()[0].rstrip("}"))
                      for axis in "xyz")
                for *_, payload in commands]

    return timeline, markers, commands, tick, vectors


def _settle_wind(timeline, tick, offset):
    tick(offset)
    while timeline.wind_ramp is not None and timeline.wind_ramp.future is not None:
        timeline.wind_ramp.future.exception(timeout=2)
        tick(offset)


def test_ramp_interpolates_enu_in_order_and_restores_before_adjacent_wind():
    timeline, markers, commands, tick, vectors = _wind_harness(200, adjacent=True)
    try:
        for offset in (199, 200, 299, 300, 400, 600, 800, 899, 900, 1000, 1100):
            _settle_wind(timeline, tick, offset)
        assert vectors() == [(4, 2, 0), (6, 0, 3), (8, -2, 6), (6, 0, 3), (4, 2, 0),
                             (8, -2, 6), (4, 2, 0)]
        assert all(c[:-1] == ("gz", "topic", "-t", "/world/fixture/wind", "-m", "gz.msgs.Wind", "-p")
                   for c in commands)
        assert [(m["message_type"], m["fault_index"]) for m in markers] == [
            ("scenario_injected", 0), ("scenario_restored", 0),
            ("scenario_injected", 1), ("scenario_restored", 1)]
        assert markers[0]["applied"] == {"ramp_ms": 200, "publish_interval_ms": 100,
                                         "baseline_enu_mps": [4, 2, 0], "target_enu_mps": [8, -2, 6],
                                         "enu_mps": [4, 2, 0], "topic": "/world/fixture/wind",
                                         "readback": "publish_only"}
        assert markers[1]["applied"] == markers[0]["applied"]
    finally:
        timeline.close(completed=True)


def test_ramp_slow_publish_does_not_block_tick_or_replay_old_samples():
    entered, unblock = Event(), Event()

    def command(payload):
        if payload.endswith("{x: 4 y: 2 z: 0}") and not entered.is_set():
            entered.set()
            assert unblock.wait(timeout=5)

    timeline, markers, commands, tick, vectors = _wind_harness(300, command=command)
    try:
        tick(200)
        assert entered.wait(timeout=2)
        tick(300)
        tick(400)
        assert len(commands) == 1
        assert not markers
        unblock.set()
        _settle_wind(timeline, tick, 400)
        assert vectors()[1] == pytest.approx((20 / 3, -2 / 3, 4))
        assert len(commands) == 2
    finally:
        unblock.set()
        timeline.close()
    assert vectors()[-1] == (4, 2, 0)


@pytest.mark.parametrize("failed_publish", [1, 2, 3])
def test_ramp_failed_publish_is_followed_by_exact_baseline(failed_publish):
    attempted = []

    def command(payload):
        attempted.append(payload)
        if len(attempted) == failed_publish:
            raise RuntimeError("Gazebo write failed")

    timeline, markers, _, tick, vectors = _wind_harness(200, command=command)
    try:
        with pytest.raises(RuntimeError, match="Gazebo write failed"):
            for offset in (200, 300, 1000):
                _settle_wind(timeline, tick, offset)
    finally:
        timeline.close()
    assert len(attempted) == failed_publish + 1
    assert vectors()[-1] == (4, 2, 0)
    assert markers[-2]["message_type"] == "scenario_error"
    assert markers[-2]["applied"]["enu_mps"] == {1: None, 2: [4, 2, 0], 3: [6, 0, 3]}[failed_publish]
    assert markers[-1]["message_type"] == "scenario_restored"
    assert markers[-1]["applied"]["enu_mps"] == [4, 2, 0]


def test_ramp_early_close_retries_failed_baseline():
    attempted = []

    def command(payload):
        attempted.append(payload)
        if len(attempted) == 3:
            raise RuntimeError("restore transport timeout")

    timeline, markers, _, tick, vectors = _wind_harness(200, command=command)
    _settle_wind(timeline, tick, 200)
    _settle_wind(timeline, tick, 300)
    with pytest.raises(RuntimeError, match="restore transport timeout"):
        timeline.close()
    assert vectors() == [(4, 2, 0), (6, 0, 3), (4, 2, 0), (4, 2, 0)]
    assert markers[-2]["message_type"] == "scenario_restore_error"
    assert markers[-1]["applied"]["enu_mps"] == [4, 2, 0]


def test_zero_ramp_matches_omitted_ramp():
    outcomes = []
    for ramp_ms in (None, 0):
        timeline, markers, commands, tick, vectors = _wind_harness(ramp_ms)
        for offset in (199, 200, 300, 900, 1000):
            tick(offset)
        timeline.close(completed=True)
        assert vectors() == [(8, -2, 6), (4, 2, 0)]
        outcomes.append((commands, [{k: v for k, v in row.items() if k not in {"values", "receive_monotonic_ns"}}
                                    for row in markers]))
    assert outcomes[0] == outcomes[1]


def test_wind_config_preserves_existing_systems(tmp_path):
    source = ROOT / "simulation/gazebo/server-wide-area.config"
    cfg = tmp_path / "server.config"
    wind_server_config(source, cfg)
    names = [plugin.get("name") for plugin in ET.parse(cfg).findall("plugins/plugin")]
    assert "gz::sim::systems::Sensors" in names
    assert names.count("gz::sim::systems::WindEffects") == 1
    assert names.count("gz::sim::systems::AirSpeed") == 1
    assert "multidetect::x1::WindAirspeed" not in names
    world = tmp_path / "world.sdf"
    world.write_text('<sdf version="1.9"><world name="custom"><wind><linear_velocity>2 -3 4</linear_velocity>'
                     '</wind></world></sdf>', encoding="utf-8")
    assert wind_baseline(world) == ("/world/custom/wind", (2, -3, 4))


def test_wind_config_replaces_only_stock_airspeed(tmp_path):
    source = ROOT / "simulation/gazebo/server-wide-area.config"
    default = tmp_path / "default.config"
    enabled = tmp_path / "enabled.config"
    wind_server_config(source, default)
    wind_server_config(source, enabled, wind_relative_airspeed=True)
    expected = ET.parse(default).find("plugins")
    stock = expected.find("plugin[@name='gz::sim::systems::AirSpeed']")
    stock.set("filename", "libx1_wind_airspeed.so")
    stock.set("name", "multidetect::x1::WindAirspeed")
    assert ET.tostring(ET.parse(enabled).find("plugins")) == ET.tostring(expected)
    with pytest.raises(ValueError, match="exactly one stock AirSpeed"):
        wind_server_config(enabled, tmp_path / "invalid.config", wind_relative_airspeed=True)


@pytest.mark.parametrize("choice, elapsed, expected_status, sends", [
    ({"action": "gimbal-retarget", "params": {"pitch_deg": -45, "yaw_deg": 0}}, 10, "accepted_unconfirmed", 1),
    ({"action": "gimbal-retarget", "params": {"pitch_deg": -45, "yaw_deg": 0}}, 3100, "rejected", 0),
    ({"action": "return", "params": {}}, 10, "rejected", 0),
])
def test_recorded_altair_decision_admission(choice, elapsed, expected_status, sends):
    package = _fixture("Fly over the yard.")
    calls, markers = [], []
    now = [10000]
    port = SimpleNamespace(gimbal_control=lambda **kwargs: (
        calls.append(kwargs) or SimpleNamespace(ok=True, ack_result=0)))
    runtime = DecisionTick(package, demo.load_airframe_profile(demo.PROFILE), recorded_decisions((choice,)), port,
                           markers.append, run_id="fixture", plan_digest="f" * 64, now_ms=lambda: now[0])
    frame = RuntimeFrame(1, now[0], package.mission_id, 1, 0, False, True, "mc", 85, None, None, {},
                         gimbal_available=True)
    try:
        runtime.tick(frame, events=(("target_lost", "tracker"),),
                     observations=(("target", {"visible": False}, "tracker"),))
        assert markers[-1]["request"]["state"]["telemetry"]["target"]["visible"] is False
        runtime.future.result(timeout=2)
        now[0] += elapsed
        runtime.tick(replace(frame, t_ms=now[0]))
        assert len(calls) == sends
        assert markers[-1]["status"] == expected_status
    finally:
        runtime.close()


def test_demo_plan_only_accepts_opt_in_schedule(inputs):  # noqa: F811
    response, argv, result = inputs
    response.write_text(raw_plan(), encoding="utf-8")
    assert demo.main([*argv, "--plan-only", "--contingency-scenario", str(SCHEDULE), "--inject-fault", "link_loss"]) == 0
    record = json.loads(result.read_text())
    assert record["flight"]["status"] == "plan_only"
    assert record["contingency_scenario"]["faults"][0]["kind"] == "link_loss"
    assert "markers" not in record["contingency_scenario"]


def test_gimbal_ingress_does_not_defer_an_unsent_command():
    ingress = GimbalIngress()
    target = {"pitch": -56, "yaw": 0, "roll": 0}
    ingress.hold = SimpleNamespace(target=target, sent_vehicle_ms=117, awaiting_readback=False,
                                   session=SimpleNamespace(poll=lambda: "ready"), tick=lambda: None)
    assert ingress.gimbal_control(pitch_deg=-36, yaw_deg=2).ok is False
    assert ingress.hold.target is target
    assert ingress.hold.sent_vehicle_ms == 117


def test_frame_telemetry_helpers_never_guess():
    from multidetect.sim_teacher.x1_scenario import CollectorScenario

    scenario = CollectorScenario.__new__(CollectorScenario)
    fresh = {"LOCAL_POSITION_NED": {}, "GLOBAL_POSITION_INT": {"relative_alt": 85_500}}
    scenario._fresh_sample = lambda name, **_: fresh.get(name)
    assert scenario._home_height_m() == 85.5
    assert scenario._position_ok({"GPS_RAW_INT": {"fix_type": 3}}) is True
    assert scenario._position_ok({"GPS_RAW_INT": {"fix_type": 1}}) is False
    assert scenario._position_ok({}) is None
    fresh.pop("LOCAL_POSITION_NED")
    assert scenario._position_ok({"GPS_RAW_INT": {"fix_type": 3}}) is None
    assert scenario._airspeed_mps({"VFR_HUD": {"airspeed": 21}}) == 21.0
    assert scenario._airspeed_mps({}) is None


@pytest.mark.parametrize("case", ["estimate", "native", "invalid_native", "stale_native", "small", "hover",
                                 "before_injection", "foreign_velocity"])
def test_wind_observation_and_request_use_one_reading(case, monkeypatch):
    from multidetect.sim_teacher import x1_scenario
    from multidetect.sim_teacher.streams import MavlinkState

    faults = load_schedule(SCHEDULE, ("wind_step",))
    scenario = CollectorScenario(faults, clearance=lambda *_: 0)
    now = [10_000]
    monkeypatch.setattr(x1_scenario.time, "monotonic_ns", lambda: now[0] * MS)
    records = []
    scenario.target_peer = (1, 1)
    scenario.record = records.append
    scenario.clock = lambda: now[0]
    scenario.check = lambda: None
    scenario.worker.timeline = SimpleNamespace(origin=6000)  # Wind window is [9000, 16000) boot ms.
    scenario.transport = SitlScenarioTransport(None, wind=("/world/default/wind", (0, 0, 5 if case == "small" else 0)))
    injected_ms = 12_000 if case == "before_injection" else 9000
    scenario.markers = deque()
    if case != "before_injection":
        scenario.markers.append({"fault_index": 0, "receive_monotonic_ns": injected_ms * MS})
    scenario.reader = SimpleNamespace(state=MavlinkState())
    scenario.reader.latest_control_state = lambda: scenario.reader.state
    package = _fixture("Fly over the yard.")
    scenario.decision = DecisionTick(package, demo.load_airframe_profile(demo.PROFILE),
                                     recorded_decisions(({"action": "return", "params": {}},)), None,
                                     records.append, run_id="wind-test", plan_digest="f" * 64,
                                     now_ms=lambda: now[0])

    def run(t, *, stop=False):
        now[0] = t
        sample = wind_rows(t, wind=(1.5, 0) if case == "small" else (8, -4), tailsitter=True, bank=0.25)
        if case == "native" and t >= 10_800:
            sample["WIND_COV"] = {"receive_monotonic_ns": (t - 100) * MS, "wind_x": 4, "wind_y": 2, "wind_z": 1}
        elif case in {"invalid_native", "stale_native"}:
            sample["WIND_COV"] = {"receive_monotonic_ns": (t - (1501 if case == "stale_native" else 0)) * MS,
                                  "wind_x": 75, "wind_y": 20, "wind_z": float("nan") if case == "invalid_native" else 0}
        if case == "hover" or stop:
            sample["EXTENDED_SYS_STATE"]["vtol_state"] = 3
        state = scenario.reader.state
        state.count += 1
        for name, row in sample.items():
            state.received_monotonic_ns[name] = row["receive_monotonic_ns"]
            state.messages[name] = {**row, "_source_system": 2 if case == "foreign_velocity" and
                                    name == "LOCAL_POSITION_NED" else 1, "_source_component": 1}
        scenario.tick(state.count, None)

    try:
        for t in range(10_000, 11_000, 200):
            run(t)
        if case == "before_injection":
            scenario.markers.append({"fault_index": 0, "receive_monotonic_ns": injected_ms * MS})
            run(injected_ms)
            assert scenario.wind_estimator.update(scenario.reader.state.messages, now_ns=now[0] * MS) is not None
        requests = [rec["request"] for rec in records if rec["message_type"] == "scenario_decision_request"]
        seen = [rec for rec in records if rec["message_type"] == "scenario_observed"]
        if case in {"estimate", "native", "invalid_native", "stale_native"}:
            state = requests[0]["state"]
            wind = state["telemetry"]["wind"]
            assert len(seen) == len(requests) == 1
            assert seen[0]["observed_value"] == pytest.approx((4, 2, 1) if case == "native" else (8, -4, None))
            assert tuple(wind[k] for k in ("north_mps", "east_mps", "down_mps")) == pytest.approx(seen[0]["observed_value"])
            assert wind["age_ms"] == seen[0]["wind_age_ms"] == (100 if case == "native" else 25)
            assert wind["source"] == seen[0]["source"] == "mavlink"
            assert seen[0]["wind_method"] == ("wind_cov" if case == "native" else "wind_triangle")
            assert state["events"][0]["confidence"] == seen[0]["wind_confidence"] == (None if case == "native" else 1)
            scenario.decision.future.result(timeout=2)
            # Another request immediately after loss of FW must not inherit the estimate.
            run(11_000, stop=True)
            idle = RuntimeFrame(1, now[0], package.mission_id, 1, 0, False, True, "mc", 85, None, None, {})
            scenario.decision.tick(idle, events=(("target_lost", "tracker"),))
            assert records[-1]["request"]["state"]["telemetry"]["wind"] is None
        else:
            assert not requests and not seen
            run(16_000, stop=True)
            assert records[-1]["message_type"] == "scenario_unobserved"
    finally:
        scenario.decision.close()
