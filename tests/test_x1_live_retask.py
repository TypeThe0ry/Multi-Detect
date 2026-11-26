"""Exercise live retask admission and the serial collector slot without a vehicle."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_x1_mission import _build, _plan, profile, terrain  # noqa: F401

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.x1_contingency_executor import RuntimeFrame
from multidetect.sim_teacher.x1_live_clearance import LiveContinueEvidence
from multidetect.sim_teacher.x1_retask import AirborneRouteSlot
from multidetect.autonomy.x1_perception_tools import PerceptionSources, ToolRunner
from multidetect.sim_teacher.x1_scenario import CollectorScenario
from multidetect.sim_teacher.x1_scenario_observation import injected_target_reading, project_target_found
from scripts.run_x1_autonomy_demo import evidence_follow_backend
from multidetect.sim_teacher.x1_scenario_decision import DecisionTick, recorded_decisions

CONFIG = Path("configs/simulation")
SCOPE = "a" * 64


@pytest.fixture
def inputs(terrain, profile):  # noqa: F811
    assumptions = json.loads((CONFIG / "x1-assumed-sim-bounds-iowa.json").read_text())
    base = json.loads((CONFIG / "x1-evidence-follow-decision-v1.json").read_text())
    area = {"north_min_m": -580, "north_max_m": 1100,
            "east_min_m": -570, "east_max_m": 950}
    document = {
        "mission_id": "test", "home": (0, 0), "places": {"poi:beacon": (320, 0)},
        "zones": {}, "routes": {"route:look": [[410, 95], [530, 120]]},
        "task_text": "Visit the beacon", "capabilities": ["airframe:x1", "gimbal_control"],
        "operating_area": {**area, "min_altitude_m": 15, "max_altitude_m": 145},
        "budget": {"time_s": 1000, "reserve_time_s": 100, "energy_wh": 72,
                   "reserve_energy_wh": 14.8, "battery_capacity_wh": 72},
        "risk_level": "low", "link_loss_policy": {"action": "return_now", "timeout_s": 20},
        "oversight_steps": [],
        "x1_planning": {"world": "rw_x1_cpu", "max_distance_from_home_m": 1200,
                        "corridor": area, "terrain_rel_home_m": (-25, 25)},
    }
    scope_args = dict(scope=SCOPE, assumed_sim={"sha256": SCOPE, "capacity_wh": 72,
                     "airborne_retask": assumptions["airborne_retask"]["value"]})
    suffixes = {key: entry["value"] for key, entry in assumptions["suffixes"].items()}
    for suffix in suffixes.values():
        suffix["entry"][4:6] = [175, 195]
    document["x1_live_evidence"] = {
        "basis": "ASSUMED_SIM", "assumed_bounds_sha256": SCOPE,
        "scope_sha256": SCOPE, "dem_sha256": terrain.source_sha256, "dem_error_m": 1,
        "obstacle_coverage": [-580, 1200, -570, 960, 90, 220], "obstacles": [],
        "landing_pad": {"radius_m": 16, "elevation_error_m": 8},
        "battery": {"max_voltage_v": 17, "soc_error_pct": 1, "consumption_error_wh": 0.25},
        "suffixes": suffixes,
    }
    package = load_mission_package(document, mode="sim", allow_assumed_sim=True)
    original = _build(replace(package, plan=validate_mission_plan(_plan(85))), terrain, profile,
                      mission_radius_m=1200)
    evidence = LiveContinueEvidence(package, profile, terrain, original, **scope_args)
    point = evidence.check_entry(2, (320, 0, 185), dict(x=320, y=0, z=-85, vx=16, vy=0, vz=0))
    assert point is not None
    frame = RuntimeFrame(1, 10_000, package.mission_id, 1, 0, False, True, "fw", 85, 16, True, {},
                         position_ne_amsl=point, active_motion_index=2, remaining_energy_wh=68,
                         remaining_time_s=800, retask_boundary=True,
                         retask_finding_ne=(410, 95), retask_finding_error_m=8)
    return package, evidence, frame, base


@pytest.mark.parametrize(("case", "reason"), [
    ("valid", None), ("different_mode", "retask_entry_changed"),
    ("stale_position", "flight_mode_or_position_unverified"),
    ("outside_entry", "unverified_clearance"), ("low_energy", "energy_or_reserve_limit"),
    ("gimbal_lost", "retask_gimbal_unavailable"),
])
def test_first_retask_dispatch_revalidates_after_observation_wait(inputs, profile, case, reason):
    package, evidence, frame, reply = inputs
    if case == "gimbal_lost":
        reply["params"]["plan"]["steps"][1]["skill"] = "gimbal_stare_at"
        frame = replace(frame, retask_gimbal_ready=True)
    scenario = CollectorScenario((), clearance=None, motion_evidence=evidence)
    route = scenario.retask
    report = project_target_found(json.loads((CONFIG / "x1-evidence-follow-faults-v1.json").read_text())
                                  ["faults"][0]["values"], observed_ms=10_000, now_ms=10_000)
    trace = []
    tick = DecisionTick(package, profile, recorded_decisions((reply,)), None, trace.append,
                        run_id="entry", plan_digest=SCOPE, cpu_replay=True, now_ms=lambda: 10_000,
                        build_proof=evidence.build_proof, replace_remaining=route.replace_remaining)
    try:
        route.enter_tick(2)
        tick.tick(frame, events=[("target_found", "fault")], step_boundary=True, perception=report)
        tick.future.result(timeout=1)
        tick.tick(frame, step_boundary=True, perception=report)
        assert tick.recent_receipts[-1]["status"] == "installed"
        installed = route.remaining_at(2)
        scenario.decision = tick
        scenario.record = trace.append
        scenario._retask_entry_mode = (128, 10, 2)
        scenario.reader = SimpleNamespace(latest_control_state=lambda: object())
        scenario.clock = lambda: 2000
        samples = {
            "HEARTBEAT": {"base_mode": 128, "custom_mode": 30 if case == "different_mode" else 10,
                          "system_status": 4},
            "EXTENDED_SYS_STATE": {"vtol_state": 4, "landed_state": 2},
            "GLOBAL_POSITION_INT": {"relative_alt": 85000},
            "LOCAL_POSITION_NED": None if case == "stale_position" else {},
            "GPS_RAW_INT": {"fix_type": 3}, "VFR_HUD": {"airspeed": 16},
        }
        scenario._fresh_sample = lambda name, **_: samples.get(name)

        def motion_data(index, *_):
            live_point = (340, 100, 185) if case == "outside_entry" else (321, 0, 185)
            point = evidence.check_entry(index, live_point, dict(x=321, y=0, z=-85, vx=16, vy=0, vz=0))
            return point, 15 if case == "low_energy" else 68, 800

        scenario._motion_data = motion_data
        if reason is None:
            scenario.check_retask_entry(2, gimbal_ready=True)
            assert trace[-1]["message_type"] == "scenario_plan_replaced"
        else:
            with pytest.raises(RuntimeError, match=reason):
                scenario.check_retask_entry(2, gimbal_ready=case != "gimbal_lost")
            assert trace[-1]["message_type"] == "collector_x1_retask_dispatch_blocked"
            assert trace[-1]["reason"] == reason
        assert route.remaining_at(2) == installed and tick.revision == 2
        assert not tick.replay_commands
    finally:
        tick.close()


@pytest.mark.parametrize(("case", "reason"), [
    ("valid", "mission_plan_replaced"), ("outside", "retask_geofence"),
    ("low_energy", "energy_or_reserve_limit"), ("transition", "transition_interlock"),
    ("missing_finding", "retask_reference_unavailable"), ("past_deadline", "decision_timeout"),
    ("changed_detection", "retask_reference_unavailable"),
    ("superseded", "superseded_mission_or_event"),
])
def test_retask_is_installed_only_between_primitives(case, reason, inputs, profile):  # noqa: F811
    package, evidence, frame, reply = inputs
    if case == "outside":
        frame = replace(frame, retask_finding_ne=(1300, 0))
    elif case == "low_energy":
        frame = replace(frame, remaining_energy_wh=15)
    elif case == "transition":
        frame = replace(frame, vtol_state="back_transition")
    elif case == "missing_finding":
        frame = replace(frame, retask_finding_ne=None)
    route = AirborneRouteSlot()
    evidence.stage_retask = route.stage
    trace, now = [], [10_000]
    fault = json.loads((CONFIG / "x1-evidence-follow-faults-v1.json").read_text())["faults"][0]
    perception = project_target_found(fault["values"], observed_ms=now[0], now_ms=now[0])
    tick = DecisionTick(package, profile, recorded_decisions((reply,)), None, trace.append,
                        run_id="test", plan_digest=SCOPE, cpu_replay=True, now_ms=lambda: now[0],
                        build_proof=evidence.build_proof, replace_remaining=route.replace_remaining)
    try:
        tick.tick(frame, events=[("target_found", "fault")], step_boundary=False, perception=perception)
        tick.future.result(timeout=1)
        tick.tick(replace(frame, retask_boundary=False), step_boundary=False)
        assert tick.future is not None and route.end is None and tick.replay_commands == []
        if case == "past_deadline":
            now[0] += 3001
        route.enter_tick(2)
        if case == "changed_detection":
            perception = {**perception, "detection": {**perception["detection"], "bearing_deg": 30}}
        events = [("target_lost", "fault")] if case == "superseded" else []
        tick.tick(replace(frame, t_ms=now[0]), step_boundary=True, perception=perception, events=events)
        receipt = tick.recent_receipts[-1]
        proof = tick.last_admission[1] if tick.last_admission else None
        assert receipt["reason"] == reason, proof
        assert not tick.replay_commands
        if case == "valid":
            assert proof.basis == "ASSUMED_SIM" and proof.landing_site_clear
            assert receipt["status"] == "installed" and route.end == 7
            assert tick.revision == 2
            assert [route.at(i).step.primitive for i in range(2, 7)] == [
                "fw_goto_waypoint", "loiter_observe", "back_transition", "mc_return_to_home", "vtol_land"]
            assert route.at(2).step.command.target_ned == (410, 95, -85)
            assert route.at(3).step.command.radius_m == 65
            assert trace[-2]["message_type"] == "scenario_decision_receipt"
            marker = trace[-1]
            assert trace[-2]["request_id"] == marker["request_id"]
            assert marker["message_type"] == "scenario_plan_replaced"
            assert (marker["previous_revision"], marker["revision"], marker["step_index"]) == (1, 2, 2)
        else:
            assert receipt["status"] == "rejected" and route.end is None and tick.revision == 1
            assert not any(row["message_type"] == "scenario_plan_replaced" for row in trace)
    finally:
        tick.close()


def test_cadence_retask_can_cross_one_boundary_without_a_mode_change(inputs, profile):
    package, evidence, frame, reply = inputs
    route = AirborneRouteSlot()
    evidence.stage_retask = route.stage
    fault = json.loads((CONFIG / "x1-evidence-follow-faults-v1.json").read_text())["faults"][0]
    report = project_target_found(fault["values"], observed_ms=10_000, now_ms=10_000)
    trace = []
    tick = DecisionTick(package, profile, recorded_decisions((reply,)), None, trace.append,
                        run_id="cadence", plan_digest=SCOPE, cpu_replay=True, now_ms=lambda: 10_000,
                        build_proof=evidence.build_proof, replace_remaining=route.replace_remaining,
                        decision_cadence_s=1)
    try:
        progress = {"phase": "cruise", "step_index": 2, "remaining_plan_sha256": SCOPE}
        active = replace(frame, active_motion_index=1, retask_boundary=False)
        tick.tick(active, mission_progress=progress, step_boundary=False, perception=report)
        tick.future.result(timeout=1)
        tick.tick(active, mission_progress=progress, step_boundary=False, perception=report)
        assert route.end is None and not tick.recent_receipts
        route.enter_tick(2)
        progress.update(phase="transition", step_index=3, remaining_plan_sha256="b" * 64)
        tick.tick(frame, mission_progress=progress, step_boundary=True, perception=report)
        assert tick.recent_receipts[-1]["reason"] == "mission_plan_replaced"
        assert route.at(2).revision == tick.revision == 2
        assert not tick.replay_commands
    finally:
        tick.close()


@pytest.mark.parametrize(("variant", "reason"), [
    ("event", "mission_plan_replaced"), ("cadence", "mission_plan_replaced"),
    ("expired", "tool_result_expired"), ("masked", "retask_reference_unavailable"),
    ("new_event", "preempted_by_event"), ("mode_change", "cadence_step_changed"),
    ("skipped_boundary", "cadence_step_changed"), ("no_tool_read", "retask_reference_unavailable"),
])
def test_retask_with_real_tool_window(inputs, profile, variant, reason):
    package, evidence, frame, decision = inputs
    now = [10_000]
    values = json.loads((CONFIG / "x1-evidence-follow-faults-v1.json").read_text())["faults"][0]["values"]
    report = project_target_found(values, observed_ms=now[0], now_ms=now[0])
    reading = injected_target_reading(report)
    runner = ToolRunner(PerceptionSources(injected_target=lambda: reading), clock_ms=lambda: now[0])
    cadence = variant in {"cadence", "new_event", "mode_change", "skipped_boundary"}
    post = (recorded_decisions((decision,)) if variant == "no_tool_read" else
            evidence_follow_backend(CONFIG / "x1-evidence-follow-decision-v1.json", cadence=cadence))
    queries, trace = [], []

    def backend(turn):
        assert "scenario_observation" not in turn
        queries.append(turn)
        return post(turn).encode("utf-8")  # ToolReply may carry byte wire.

    route = AirborneRouteSlot()
    evidence.stage_retask = route.stage
    tick = DecisionTick(package, profile, backend, None, trace.append, run_id="tool-test", plan_digest=SCOPE,
                        cpu_replay=True, now_ms=lambda: now[0], build_proof=evidence.build_proof,
                        replace_remaining=route.replace_remaining, decision_cadence_s=int(cadence),
                        recorded_cadence_followup=cadence, tool_runner=runner)
    active = replace(frame, active_motion_index=1, retask_boundary=False)
    progress = {"phase": "cruise", "step_index": 2, "remaining_plan_sha256": SCOPE} if cadence else None
    try:
        if cadence:
            # Ordinary cadence continues before the observation. The event
            # then takes priority and a separate cadence ticket uses its data.
            tick.tick(active, mission_progress=progress, step_boundary=False)
            assert json.loads(tick.future.result(timeout=1).wire)["action"] == "continue"
            tick.tick(active, mission_progress=progress, step_boundary=False)
            now[0] += 1
        tick.tick(replace(active, t_ms=now[0]), events=[("target_found", "fault")], mission_progress=progress,
                  step_boundary=False, perception=report)
        if cadence:
            assert json.loads(tick.future.result(timeout=1).wire)["action"] == "continue"
            tick.tick(replace(active, t_ms=now[0]), mission_progress=progress,
                      step_boundary=False, perception=report)
            assert tick.recent_receipts[-1]["trigger"] == "event"
        assert json.loads(tick.future.result(timeout=1).wire)["action"] == "retask"
        tick.tick(replace(active, t_ms=now[0]), mission_progress=progress, step_boundary=False, perception=report)
        assert route.end is None and not tick.replay_commands
        now[0] += 1001 if variant == "expired" else 1
        index = 3 if variant == "skipped_boundary" else 2
        route.enter_tick(index)
        if cadence:
            progress = {**progress, "phase": "transition", "step_index": index + 1,
                        "remaining_plan_sha256": "b" * 64}
        tick.tick(replace(frame, t_ms=now[0], active_motion_index=index), mission_progress=progress,
                  step_boundary=True, perception=None if variant == "masked" else report,
                  mode="changed" if variant == "mode_change" else None,
                  events=[("target_lost", "fault")] if variant == "new_event" else [])
        receipt = next(row for row in reversed(trace) if row["message_type"] == "scenario_decision_receipt")
        assert receipt["reason"] == ("tool_protocol_error" if variant == "expired" else reason)
        assert tick.replay_commands == []
        if variant in {"cadence", "event"}:
            assert receipt["trigger"] == variant
            assert receipt["status"] == "installed" and route.at(2).revision == tick.revision == 2
            assert [r["tool"] for r in receipt["injected_tool_results"]] == ["look", "target_status"]
            assert receipt["injected_tool_results"][1]["data"]["error_bound_m"] == 8
            assert trace[-1]["message_type"] == "scenario_plan_replaced"
            assert trace[-1]["request_id"] == receipt["request_id"]
            request = next(r["request"] for r in trace if r["message_type"] == "scenario_decision_request"
                           and r["request"]["request_id"] == receipt["request_id"])
            assert request["scenario_observation"] == report
            if cadence:
                assert request["event_id"] != request["state"]["latest_event_id"]
        else:
            assert tick.revision == 1 and route.end is None
            if variant == "expired":
                assert trace[-1]["reason"] == reason and trace[-1]["message_type"] == "scenario_decision_error"
    finally:
        tick.close()
