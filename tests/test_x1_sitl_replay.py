"""Replay recorded PX4 rows through the serialized collector, without vehicle I/O."""

import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.x1_perception_tools import PerceptionSources, ToolRunner
from multidetect.sim_teacher import x1_scenario
from multidetect.sim_teacher.streams import MavlinkState
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions
from multidetect.sim_teacher.x1_scenario_observation import injected_target_reading
from scripts.run_x1_autonomy_demo import evidence_follow_backend, finish_flight
from test_x1_live_retask import inputs, profile, terrain  # noqa: F401
from test_x1_mission import _build, _plan

DATA = Path(__file__).with_name("fixtures") / "x1_evidence_follow_archived_mavlink.json"
CONFIG = Path("configs/simulation")
MS = 1_000_000


class Replay:
    def __init__(self, episode, inputs, profile, monkeypatch, *, cadence, with_target):
        self.data = json.loads(DATA.read_text())[episode + "_episode"]
        package, evidence, _, _ = inputs
        self.frames, self.records, self.turns = [], [], []
        self.state = MavlinkState()
        self.now_ns = self.boot_ms = 0
        detection = self.data["injected_marker"]["values"]["position_estimate"]
        # Use the existing small CPU proof geometry. The archived peer, receive
        # clocks, flight state, relative height, velocity and target are intact.
        terrain = SimpleNamespace(local_from_wgs84=lambda lat, lon: (
            (410, 95) if (lat, lon) == (detection["lat_deg"], detection["lon_deg"])
            else (self.state.get("LOCAL_POSITION_NED")["x"], self.state.get("LOCAL_POSITION_NED")["y"])
        ))
        faults = x1_scenario.load_schedule(CONFIG / "x1-evidence-follow-faults-v1.json") if with_target else ()
        steps = _build(replace(package, plan=validate_mission_plan(_plan(85))), evidence.terrain, profile,
                       mission_radius_m=1200)
        self.scenario = scenario = x1_scenario.CollectorScenario(
            faults, clearance=None, terrain=terrain, motion_evidence=evidence,
            cadence_steps=steps if cadence else (), perception_tick=self.publish,
        )
        real_motion_position = scenario._motion_position

        def rebased_position(row):
            point = real_motion_position(row)
            return (*point[:2], 100 + row["relative_alt"] / 1000) if point else None

        scenario._motion_position = rebased_position
        scenario.worker.timeline = SimpleNamespace(origin=self.data["timeline_origin_boot_ms"])
        scenario.record = self.records.append
        scenario.target_peer = (1, 1)
        scenario.clock = lambda: self.boot_ms
        scenario.reader = SimpleNamespace(latest_control_state=lambda: self.state)
        scenario.markers = deque()
        scenario.transport = x1_scenario.SitlScenarioTransport(None)
        if faults:
            scenario.transport.apply(faults[0])
            scenario.markers.append(self.data["injected_marker"])
        self.reading = None
        post = (evidence_follow_backend(CONFIG / "x1-evidence-follow-decision-v1.json", cadence=cadence)
                if with_target else recorded_decisions([{"action": "continue", "params": {}}] * 10))

        def backend(turn):
            self.turns.append(turn)
            return post(turn)

        def proof(decision, frame):
            self.frames.append(frame)
            return evidence.build_proof(decision, frame)

        runner = ToolRunner(PerceptionSources(injected_target=lambda: self.reading), clock_ms=self.now_ms)
        scenario.decision = self.decision = DecisionTick(
            package, profile, backend, None, self.records.append, run_id=self.data["episode_id"],
            plan_digest=self.data["compiled_mission_sha256"], now_ms=self.now_ms, cpu_replay=True,
            build_proof=proof, tool_runner=runner, decision_cadence_s=int(cadence),
            recorded_cadence_followup=cadence, replace_remaining=scenario.retask.replace_remaining,
        )
        monkeypatch.setattr(x1_scenario.time, "monotonic_ns", lambda: self.now_ns)

    def now_ms(self):
        return self.now_ns // MS

    def publish(self, *_, perception=None):
        self.reading = injected_target_reading(perception)

    def at(self, key, index, *, advance_ms=0, lifecycle=None, boundary=False):
        snap = self.data[key]
        self.now_ns = snap["tick_monotonic_ns"] + advance_ms * MS
        self.boot_ms = snap["pose_boot_ms"]
        self.state = MavlinkState(messages=snap["messages"], received_monotonic_ns=snap["received_monotonic_ns"],
                                  received_vehicle_ms=snap["received_vehicle_ms"],
                                  vehicle_time_boot_ms=snap["vehicle_time_boot_ms"])
        self.scenario.tick(index, None, lifecycle=lifecycle, step_boundary=boundary)


@pytest.mark.parametrize("variant", ["event", "cadence"])
def test_archived_target_is_sampled_at_the_serial_boundary(inputs, profile, monkeypatch, variant):
    cadence = variant == "cadence"
    run = Replay(variant, inputs, profile, monkeypatch, cadence=cadence, with_target=True)

    def wait_for_fast_recorded_reply(futures, *, timeout):
        assert timeout == 0.05
        future, = futures
        future.result(timeout=2)
        run.now_ns += 10 * MS  # deterministic boundary processing cost, not a backend latency claim
        return {future}, set()

    monkeypatch.setattr(x1_scenario, "wait", wait_for_fast_recorded_reply)
    try:
        run.at("step1_mid_leg", 1)
        observed_before_boundary = any(row["message_type"] == "scenario_observed" for row in run.records)
        if run.decision.future is not None:
            run.decision.future.result(timeout=2)
        run.at("step2_boundary", 2, boundary=True)
        observed, = [r["observed_value"] for r in run.records if r["message_type"] == "scenario_observed"]
        installed = [r for r in run.records if r.get("status") == "installed"]
        assert installed, [(r.get("status"), r.get("reason")) for r in run.records]
        assert not observed_before_boundary
        assert installed[0]["trigger"] == variant
        assert observed["t_ms"] == run.data["step2_boundary"]["tick_monotonic_ns"] // MS
        tools = installed[0]["injected_tool_results"]
        assert [r["tool"] for r in tools] == ["look", "target_status"]
        assert all(r["data"]["captured_ms"] == observed["t_ms"] for r in tools)
        assert run.decision.revision == 2 and run.scenario.retask.at(2).revision == 2
        assert run.decision.replay_commands == []
    finally:
        run.decision.close()


def test_archived_lifecycle_is_not_cruise_and_does_not_open_cadence(inputs, profile, monkeypatch):
    run = Replay("cadence", inputs, profile, monkeypatch, cadence=True, with_target=False)
    try:
        run.at("takeoff_climb", 0, lifecycle="takeoff")
        assert run.decision.future is None
        run.at("cruise_steady", 1)
        run.decision.future.result(timeout=2)
        run.at("cruise_steady", 1, advance_ms=10)
        assert run.frames[-1].armed_in_air is True and run.frames[-1].px4_failsafe is False
        # The rebased test terrain does not prove the original suffix. The
        # archived flight state must still get as far as the real proof gate.
        assert run.decision.recent_receipts[-1]["reason"] == "unverified_clearance"
        assert run.turns[-1]["mission_progress"]["phase"] == "cruise"
        run.at("landing_descent", 3, lifecycle="approach")
        assert run.decision.future is None
        assert len(run.turns) == 1 and run.decision.replay_commands == []
    finally:
        run.decision.close()


def test_quiet_flight_state_rejections_are_summarized(inputs, profile, monkeypatch):
    run = Replay("cadence", inputs, profile, monkeypatch, cadence=True, with_target=False)
    _, _, frame, _ = inputs
    frame = replace(frame, px4_failsafe=None, armed_in_air=False)
    progress = {"phase": "approach", "step_index": 4, "remaining_plan_sha256": "a" * 64}
    for now in (10_000, 10_500):
        run.now_ns = now * MS
        run.decision.tick(replace(frame, t_ms=now), mission_progress=progress)
        run.decision.future.result(timeout=2)
        run.decision.tick(replace(frame, t_ms=now), mission_progress=progress)
    assert [r["reason"] for r in run.decision.recent_receipts] == ["px4_owns_or_flight_state_unknown"] * 2
    assert not run.records and run.decision.replay_commands == []
    run.decision.close()
    summary, = run.records
    assert summary["message_type"] == "scenario_decision_cadence_summary"
    assert summary["quiet_continue_rejected_count"] == 2
    assert summary["by_reason"] == {"px4_owns_or_flight_state_unknown": 2}


def test_demo_marker_summary_counts_only_quiet_cadence_continue(tmp_path):
    receipt = {"message_type": "scenario_decision_receipt", "trigger": "cadence",
               "action": "continue", "status": "rejected", "receive_monotonic_ns": 1}
    quiet = dict(receipt, reason="px4_owns_or_flight_state_unknown")
    visible = [dict(quiet, trigger="event"), dict(quiet, action="retask"),
               dict(receipt, reason="unverified_clearance"),
               dict(receipt, reason="current_fw_airspeed_limit")]
    summary = {"message_type": "scenario_decision_cadence_summary", "receive_monotonic_ns": 2,
               "quiet_continue_rejected_count": 2, "by_reason": {"flight_mode_or_position_unverified": 2}}
    (tmp_path / "episode.json").write_text('{"status":"complete"}', encoding="utf-8")
    (tmp_path / "mavlink.jsonl").write_text("\n".join(map(json.dumps, [quiet, *visible, summary])), encoding="utf-8")
    (tmp_path / "steps.jsonl").touch()
    record = {"safety": {"observed": {}}, "flight": {}, "markers": [], "contingency_scenario": {}}
    assert not finish_flight(tmp_path, (), record)
    scenario = record["contingency_scenario"]
    counts = {"px4_owns_or_flight_state_unknown": 1, "flight_mode_or_position_unverified": 2}
    assert scenario["quiet_cadence_rejected_continue"] == {"count": 3, "by_reason": counts}
    assert scenario["markers"] == [*visible, dict(summary, quiet_continue_rejected_count=3, by_reason=counts)]
