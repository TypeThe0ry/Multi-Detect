from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from multidetect.autonomy.x1_contingency_executor import RuntimeFrame
from multidetect.autonomy.x1_contingency_replay import Fault
from multidetect.sim_teacher.x1_scenario import (
    ScenarioTimeline, SitlScenarioTransport, load_schedule, wind_baseline, wind_server_config,
)
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, GimbalIngress, recorded_decisions
from scripts import run_x1_autonomy_demo as demo
from test_run_x1_autonomy_demo import inputs, raw_plan  # noqa: F401
from test_x1_planner import _fixture


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


def test_wind_config_preserves_existing_systems(tmp_path):
    source = ROOT / "simulation/gazebo/server-wide-area.config"
    cfg = tmp_path / "server.config"
    wind_server_config(source, cfg)
    names = [plugin.get("name") for plugin in ET.parse(cfg).findall("plugins/plugin")]
    assert "gz::sim::systems::Sensors" in names
    assert names.count("gz::sim::systems::WindEffects") == 1
    world = tmp_path / "world.sdf"
    world.write_text('<sdf version="1.9"><world name="custom"><wind><linear_velocity>2 -3 4</linear_velocity>'
                     '</wind></world></sdf>', encoding="utf-8")
    assert wind_baseline(world) == ("/world/custom/wind", (2, -3, 4))


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
