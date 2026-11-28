import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_x1_closed_loop_suite as suite
from scripts.build_x1_flight_loop_sft import policy
from scripts.x1_closed_loop_harvest import harvest


def decision_trace(*, action="continue", receipt="continued", fault=None, mode="fw", trigger="cadence"):
    request = {"request_id": "case:1", "event_id": 1, "deadline_ms": 4000, "hold_refs": [],
               "actions": ["continue", "return"], "trigger": trigger, "mission_progress": {"phase": "cruise"}, "state": {
                   "t_ms": 1000, "mission_id": "x1-test", "revision": 1,
                   "telemetry": {"vtol": {"state": mode}, "fc": {}, "navigation": {},
                                 "battery": {}, "link": {}, "target": {}, "wind": {}},
                   "events": [{"type": fault, "t_ms": 1000}] if fault else []},
               "perception": {"call_index": 0, "tools": [], "results": []}}
    label = policy(request)
    raw = "  " + json.dumps({**label, "action": action}) + "\n"
    messages = [{"role": "user", "content": json.dumps({"task": "go", "request": request})}]
    return [{"kind": "request_opened", "request_id": "case:1", "request": request},
            {"kind": "turn_sent", "request_id": "case:1", "request": request, "call_index": 0,
             "messages": messages},
            {"kind": "turn_result", "request_id": "case:1", "call_index": 0, "latency_ms": 124,
             "model_output": raw},
            {"kind": "receipt", "request_id": "case:1", "action": action, "latency_ms": 130,
             "status": receipt, "reason": "no_new_command" if receipt == "continued" else "unverified_clearance"}]


def test_case_table(monkeypatch):
    # Keep the legacy catalog assertions as a regression for the original cases.
    monkeypatch.setattr(suite, "CASES", tuple(case for case in suite.CASES if case.id != "corridor-dogleg"))
    args, rows = suite.parse_args(["--list-cases", "--case", "quiet-cadence", "--case", "ramped-wind"])
    assert args.slots == (7, 6, 5)
    assert [r.id for r in rows] == ["ramped-wind", "quiet-cadence"]
    assert len(suite.CASES) == 10
    assert len(suite.select_cases([])) == 8
    assert len(suite.parse_args(["--list-cases"])[1]) == 10
    assert {r.phase for r in suite.CASES} == {"mc", "fw", "steady"}
    for row in suite.CASES:
        assert 0 <= row.tool_budget <= 3 and 0 < row.expected_s < row.budget_s
        assert row.fault or row.cadence_s > 0
    for names in (["bad"], ["quiet-cadence", "quiet-cadence"]):
        with pytest.raises(ValueError):
            suite.select_cases(names)


def test_long_range_cases_use_sitl_only_envelope():
    from scripts import run_x1_autonomy_demo as demo

    for name in ("long-range-transect", "long-range-wind"):
        case, = suite.select_cases([name])
        assert case.assumed and case.assumed_bounds == suite.LONG_RANGE_BOUNDS
        assert case.mission_package == suite.LONG_RANGE_PACKAGE
        assert case.sentence == suite.LONG_RANGE_SENTENCE
        assert case.phase == "fw" and case.cadence_s >= 1
        assert case.budget_s >= 1800 and case.expected_s < case.budget_s
        assert case.min_airborne_s == 600
    wind, = suite.select_cases(["long-range-wind"])
    scheduled, = demo.load_schedule(suite.ROOT / wind.schedule, ("wind_step",))
    assert scheduled.kind == "wind_step" and scheduled.end_ms - scheduled.at_ms >= 15000


def test_long_range_bounds_reproduce_and_stay_sitl_only(tmp_path):
    from pathlib import Path

    from scripts import generate_x1_assumed_sim_bounds as generator
    from scripts import run_x1_autonomy_demo as demo

    document = generator.generate_long_range(mode="sim", world=generator.WORLD, image=generator.IMAGE,
                                             profile_path=demo.PROFILE)
    committed = Path(suite.ROOT / suite.LONG_RANGE_BOUNDS).read_text(encoding="utf-8")
    assert committed == json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    assert document["assumed_sim_envelope"] == {
        "basis": "ASSUMED_SIM", "scope": "SITL-only", "max_distance_from_home_m": 4000.0,
    }
    assert demo.verified_assumed_radius(suite.ROOT / suite.LONG_RANGE_BOUNDS) == 4000.0
    tampered = tmp_path / "assumed.json"
    tampered.write_text(json.dumps({**document, "suffixes": {}}))
    with pytest.raises(ValueError, match="exact Iowa long-range bounds template"):
        demo.verified_assumed_radius(tampered)
    assert generator.envelope_max_distance(generator.generate(
        mode="sim", world=generator.WORLD, image=generator.IMAGE, profile_path=demo.PROFILE)) is None
    assert set(document["suffixes"]) == {"1"}
    with pytest.raises(ValueError, match="SITL-only ceiling"):
        generator.envelope_max_distance({**document, "assumed_sim_envelope": {
            "basis": "ASSUMED_SIM", "scope": "SITL-only", "max_distance_from_home_m": 4001.0}})


def test_long_range_package_needs_the_assumed_flag():
    from multidetect.autonomy.mission_package import load_mission_package

    document = json.loads((suite.ROOT / suite.LONG_RANGE_PACKAGE).read_text(encoding="utf-8"))
    document["task_text"] = suite.LONG_RANGE_SENTENCE
    with pytest.raises(ValueError, match="pilot envelope"):
        load_mission_package(document, mode="sim")
    with pytest.raises(ValueError, match="pilot envelope"):
        load_mission_package(document, mode="sim", allow_assumed_sim=True)
    package = load_mission_package(document, mode="sim", allow_assumed_sim=True,
                                   assumed_sim_max_distance_m=4000.0)
    assert package.document["x1_planning"]["max_distance_from_home_m"] == 4000.0
    assert package.assumed_sim_max_distance_m == 4000.0
    for mode in (None, "replay", "hil", "flight"):
        with pytest.raises(ValueError, match="authorized SITL scenario runner"):
            load_mission_package(document, mode=mode, allow_assumed_sim=True,
                                 assumed_sim_max_distance_m=4000.0)
    with pytest.raises(ValueError, match="authorized SITL scenario runner"):
        load_mission_package(document, mode="sim", assumed_sim_max_distance_m=4000.0)


@pytest.mark.parametrize("name, fault, end_ms", [
    ("mc-link-loss", "link_loss", 58000), ("mc-battery-drop", "battery_drop", 62000),
])
def test_native_cases_use_mc_return_segment_of_iowa_vtol_route(name, fault, end_ms):
    from scripts import run_x1_autonomy_demo as demo

    case, = suite.select_cases([name])
    assert case.phase == "mc" and case.assumed and not case.nominal_mc
    assert case.sentence == suite.SENTENCE and case.fault == fault
    scheduled, = demo.load_schedule(suite.ROOT / case.schedule, (fault,))
    assert (scheduled.at_ms, scheduled.end_ms) == (50000, end_ms)


def test_harvest_model_wire_and_missing():
    trace = decision_trace(action="return", receipt="rejected")
    rows, metrics = harvest(trace, case_id="quiet-cadence", attempt=1)
    row, = rows
    assert row["messages"][:1] == trace[1]["messages"]
    assert row["messages"][-1]["role"] == "assistant"
    assert json.loads(row["messages"][-1]["content"])["action"] == "continue"
    assert row["model_output"] == trace[2]["model_output"]
    assert metrics["quiet_unnecessary_actions"] == 1
    assert metrics["action_matches"] == 0
    assert metrics["turn_latency_ms_histogram"]["le_250"] == 1
    trace[3].update(reason="decision_timeout", latency_ms=3001)
    rows, metrics = harvest(trace[:2] + trace[3:], case_id="quiet-cadence", attempt=1)
    assert rows[0]["model_output"] is None
    assert metrics["deadline_missed"] == metrics["missing_results"] == 1


def fixture_flight():
    return {"validator": {"verdict": "accepted"},
            "flight": {"status": "pass", "landing": True, "return_home": True}}


def test_outcomes_require_phase_observation_and_action():
    wind = suite.select_cases(["ramped-wind"])[0]
    result = fixture_flight()
    trace = decision_trace(fault="wind_step", trigger="event")
    assert not suite.check_flight(wind, result, trace)[0]["fault_observed"]
    result["contingency_scenario"] = {"markers": [
        {"message_type": f"scenario_{state}", "fault": "wind_step", "source": "mavlink",
         "reason": "window_end", "wind_method": "kinematic", "wind_age_ms": 100}
        for state in ("injected", "observed", "restored")]}
    assert all(suite.check_flight(wind, result, trace)[0].values())
    mc = suite.select_cases(["mc-link-loss"])[0]
    trace = decision_trace(action="return", receipt="accepted_unconfirmed", fault="link_loss", mode="mc", trigger="event")
    result["flight"]["native_handoff"] = {"action": "return", "confirmed": True, "landed_by_native": True}
    assert suite.check_flight(mc, result, trace)[0]["executor_effect"]
    result["flight"]["native_handoff"]["confirmed"] = False
    assert not suite.check_flight(mc, result, trace)[0]["executor_effect"]


def test_long_range_requires_simulated_airborne_time_waypoint_and_repeated_cadence():
    case, = suite.select_cases(["long-range-transect"])
    result = fixture_flight()
    trace = decision_trace()
    checks, _ = suite.check_flight(case, result, trace)
    for key in ("min_airborne_time", "waypoint_reached", "repeated_cadence"):
        assert not checks[key]
    result["flight"].update(takeoff_complete_to_landing_sim_s=599.9, waypoints_reached=["w"])
    assert not suite.check_flight(case, result, trace)[0]["min_airborne_time"]
    result["flight"]["takeoff_complete_to_landing_sim_s"] = 600
    second = json.loads(json.dumps(trace).replace("case:1", "case:2"))
    assert all(suite.check_flight(case, result, trace + second)[0].values())


def test_cadence_counts_model_proposal_even_without_executor_action():
    case = suite.select_cases(["quiet-cadence"])[0]
    trace = decision_trace(action="return", receipt="rejected")
    trace[-1]["action"] = None
    checks, scores = suite.check_flight(case, fixture_flight(), trace)
    assert not checks["quiet_action_limit"] and scores["unnecessary_actions"] == 1


def test_quiet_ticks_cannot_pass_with_missing_model_output():
    case = suite.select_cases(["quiet-cadence"])[0]
    trace = decision_trace()
    missing = decision_trace()
    for row in missing:
        row["request_id"] = "case:2"
        if "request" in row:
            row["request"]["request_id"] = "case:2"
    del missing[2]
    checks, _ = suite.check_flight(case, fixture_flight(), trace + missing)
    assert not checks["expected_receipt"] and checks["quiet_action_limit"]


FAKE_RUNNER = '''
import json
import socket, sys, time
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text())
root = Path(spec['attempt_dir'])
mode = sys.argv[2]
if mode == 'timeout':
    time.sleep(20)
if root.name == 'attempt-01' and mode == 'retry':
    print('SITL slots busy')
    sys.exit(1)
(root / 'flight').mkdir()
(root / 'flight/result.json').write_text(sys.argv[3])
(root / 'decisions.jsonl').write_text(sys.argv[4])
'''


def test_fake_runner_retry_summary_and_budget(tmp_path):
    case = suite.select_cases(["quiet-cadence"])[0]
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    args = SimpleNamespace(case_budget_s=3, retries=1, slots=(7, 6), retry_interval_s=.02, plan_only=False)

    def command(mode):
        rows = "".join(json.dumps(row) + "\n" for row in decision_trace())
        return lambda spec: [sys.executable, "-c", FAKE_RUNNER, str(spec), mode, json.dumps(fixture_flight()), rows]

    record, failures = suite.run_case(case, args, tmp_path, env, command_factory=command("retry"))
    assert record["status"] == "pass" and not failures
    assert [r["slot"] for r in record["attempts"]] == [7, 6]
    summary = suite.summarize(tmp_path, [record], failures)
    assert summary["metrics"]["action_matches"] == 1
    assert (tmp_path / "failures.jsonl").read_text() == ""
    assert "quiet-cadence | pass" in (tmp_path / "summary.md").read_text()

    timeout = tmp_path / "timeout"
    timeout.mkdir()
    args.case_budget_s = .4
    record, _ = suite.run_case(case, args, timeout, env, command_factory=command("timeout"))
    assert record["status"] == "budget_exceeded"
    assert len(record["attempts"]) == 1
    assert record["attempts"][0]["exit_status"] < 0
    assert record["elapsed_s"] < 1.5


def test_owned_fake_llama_and_planner_reuse(tmp_path, monkeypatch):
    # The sandbox denies socket creation. Exercise real process ownership with
    # an executable fake and stub only the port probe and HTTP transport.
    port = 9957
    fake = tmp_path / "fake-llama-server"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(20)\n")
    fake.chmod(0o700)
    gguf = tmp_path / "fake.gguf"
    gguf.write_text("fake")
    args = SimpleNamespace(port=port, llama_server=fake, startup_timeout_s=3, checkpoint=gguf, cuda_device=None)
    calls = []

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def setsockopt(self, level, option, value):
            assert (level, option, value) == (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        def bind(self, address):
            assert address == ("127.0.0.1", port)

    def fake_http(actual_port, path, body=None, *, timeout):
        assert actual_port == port and timeout > 0
        calls.append(path)
        if path == "/health":
            return {"status": "ok"}
        return {"choices": [{"message": {"content": body["messages"][-1]["content"]}}]}

    monkeypatch.setattr(suite.socket, "socket", FakeSocket)
    monkeypatch.setattr(suite, "local_json", fake_http)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        with suite.Server(args, tmp_path) as server:
            pid = server.process.pid
            post = suite.shared_planner(port, 1)
            assert post("hello", {}) == post("hello", {}) == "hello"
            with pytest.raises(ValueError, match="prompt changed"):
                post("other", {})
            assert calls == ["/health", "/v1/chat/completions"]
        receipt = suite.read_json(tmp_path / "server.json")
        assert receipt["pid"] == pid and receipt["state"] == "stopped"
        assert receipt["argv"][receipt["argv"].index("-ngl") + 1] == "0"
        assert server.process.poll() is not None and unrelated.poll() is None
        assert "pkill" not in Path(suite.__file__).read_text()
    finally:
        suite.terminate_owned(unrelated)


def test_worker_reuses_model_reply_and_applies_evidence_flags(tmp_path, monkeypatch):
    from dataclasses import asdict

    from scripts import run_x1_autonomy_demo as demo
    from scripts.realworld import x1_site_evidence

    case = suite.select_cases(["evidence-follow-cadence"])[0]
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{}')
    spec = {"case": asdict(case), "slot": 6, "attempt_dir": str(tmp_path), "args": {
        "port": 9998, "planner_timeout_s": 3, "checkpoint": "fake.gguf", "llama_server": "fake-llama",
        "mission_package": "context.json", "site_inventory": str(inventory), "plan_only": False}}
    prompts, runs = [], []

    def fake_http(port, endpoint, body, *, timeout):
        assert port == 9998 and endpoint == "/v1/chat/completions" and timeout == 3
        prompts.append(body)
        return {"choices": [{"message": {"content": "real-model-wire"}}]}

    def fake_demo(argv, shared_post):
        runs.append(argv)
        assert shared_post("plan this", {}) == "real-model-wire"
        output = Path(argv[argv.index("--output") + 1])
        output.mkdir()
        (output / "result.json").write_text('{}')
        return 0

    bound = []
    monkeypatch.setattr(x1_site_evidence, "write_bounds", lambda *args: bound.append(args))
    monkeypatch.setattr(suite, "local_json", fake_http)
    monkeypatch.setattr(demo, "main", fake_demo)
    assert suite.run_worker(spec) == 0
    assert len(prompts) == len(bound) == 1 and len(runs) == 2
    assert runs[0][runs[0].index("--planner-port") + 1] == runs[0][runs[0].index("--decision-port") + 1] == "9998"
    assert "--plan-only" in runs[0] and "--target-cadence-only" in runs[0]
    assert "--recorded-output" not in runs[1] and "--recorded-retask" not in runs[1]
    assert "--assumed-sim-bounds" in runs[1] and "--decision-audit" in runs[1]


def test_long_range_worker_uses_only_its_own_assumed_bounds(tmp_path, monkeypatch):
    from dataclasses import asdict

    from scripts import run_x1_autonomy_demo as demo
    from scripts.realworld import x1_site_evidence

    case = suite.select_cases(["long-range-transect"])[0]
    inventory = tmp_path / "inventory.json"
    inventory.write_text('{}')
    spec = {"case": asdict(case), "slot": 5, "attempt_dir": str(tmp_path), "args": {
        "port": 9999, "planner_timeout_s": 3, "checkpoint": "fake.gguf", "llama_server": "fake-llama",
        "mission_package": "context.json", "site_inventory": str(inventory), "plan_only": False}}
    runs = []

    def fake_http(port, endpoint, body, *, timeout):
        return {"choices": [{"message": {"content": "model-wire"}}]}

    def fake_demo(argv, shared_post):
        runs.append(argv)
        output = Path(argv[argv.index("--output") + 1])
        output.mkdir()
        (output / "result.json").write_text('{}')
        return 0

    monkeypatch.setattr(x1_site_evidence, "write_bounds", lambda *args: None)
    monkeypatch.setattr(suite, "local_json", fake_http)
    monkeypatch.setattr(demo, "main", fake_demo)
    assert suite.run_worker(spec) == 0
    assert len(runs) == 2
    for argv in runs:
        # Exactly one flag per run: the long-range bounds, never the default Iowa one.
        assert argv.count("--assumed-sim-bounds") == 1
        bounds = Path(argv[argv.index("--assumed-sim-bounds") + 1]).name
        assert bounds == Path(suite.LONG_RANGE_BOUNDS).name
        assert Path(argv[argv.index("--mission-package") + 1]).name == Path(suite.LONG_RANGE_PACKAGE).name


def test_retask_installation_bound_to_expected_trigger():
    case = suite.select_cases(["evidence-follow-cadence"])[0]
    result = fixture_flight()
    result["contingency_scenario"] = {"evidence_follow": {"landed_monotonic_ns": 123}, "markers": [
        {"message_type": "scenario_injected", "fault": "target_found"},
        {"message_type": "scenario_observed", "fault": "target_found"},
        {"message_type": "scenario_plan_replaced", "request_id": "case:1"}]}
    trace = decision_trace(action="retask", receipt="installed", fault="target_found")
    assert all(suite.check_flight(case, result, trace)[0].values())
    trace[1]["request"]["trigger"] = "event"
    checks, _ = suite.check_flight(case, result, trace)
    assert not checks["expected_receipt"] and not checks["executor_effect"]


def test_mc_plan_uses_model_graph_and_iowa_dem():
    from scripts import run_x1_autonomy_demo as demo, x1_closed_loop_mc as mc
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.autonomy.airframe_profile import load_airframe_profile
    from multidetect.terrain_dem import load_local_dem_grid
    from test_x1_planner import _fixture

    sentence = suite.CASES[0].sentence
    document = _fixture(sentence, target=(320, 0), corridor=(-950, 1100, -950, 950), terrain=(-25, 25)).document
    document["x1_planning"]["world"] = suite.WORLD
    package = load_mission_package(document, mode="sim")
    plan_wire = {"plan_version": 1, "goal": sentence, "report": [], "steps": [
        {"id": "up", "skill": "vtol_takeoff", "params": {"altitude_agl_m": 85},
         "on_success": "yard", "on_fail": "home"},
        {"id": "yard", "skill": "x1_mc_reposition", "params": {"altitude_agl_m": 85, "target_ref": "poi:yard"},
         "on_success": "home", "on_fail": "home"},
        {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}]}
    plan, _ = mc.compile_mc_plan(package, lambda _prompt, _schema: json.dumps(plan_wire), {})
    terrain = load_local_dem_grid(demo.ROOT / "simulation/terrain" / f"{suite.WORLD}.dem.json")
    profile = load_airframe_profile(demo.PROFILE)
    steps = mc.build_mc_steps(package, plan, terrain, profile, 1200)
    assert [step.primitive for step in steps] == ["vtol_takeoff", "mc_goto_waypoint", "mc_return_to_home", "vtol_land"]
    assert steps[1].source_step_id == "yard" and steps[1].command.target_ned[:2] == (320, 0)
    assert demo.MissionPolicy(steps, -56, nominal_mc=True).vtol_hover_mission
    plan_wire["steps"][1]["params"]["altitude_agl_m"] = 90
    with pytest.raises(ValueError, match="85 m AGL"):
        mc.compile_mc_plan(package, lambda _prompt, _schema: json.dumps(plan_wire), {})


def test_corridor_dogleg_is_opt_in_and_statically_provable():
    import math

    from multidetect.autonomy.mission_plan import MissionPlan, MissionStep
    from multidetect.autonomy.x1_corridor_energy import default_constants, max_one_way_m
    from multidetect.autonomy.x1_plan import x1_static_violations
    from scripts import run_x1_autonomy_demo as demo

    case, = suite.select_cases(["corridor-dogleg"])
    assert case not in suite.select_cases([])
    assert case in suite.parse_args(["--list-cases"])[1]
    assert suite.parse_args(["--list-cases", "--case", case.id])[1] == [case]
    assert case.opt_in and case.assumed and case.fault is None
    assert case.phase == "fw" and case.expected == ("continue",) and case.cadence_s > 0
    assert 300 <= case.expected_s <= 420 and case.budget_s > case.expected_s
    assert case.assumed_bounds == suite.CORRIDOR_BOUNDS
    assert case.mission_package == suite.CORRIDOR_DOGLEG_PACKAGE
    generator = demo.assumptions
    generated = generator.generate_corridor(
        mode="sim", world=generator.WORLD, image=generator.IMAGE, profile_path=demo.PROFILE)
    assert (suite.ROOT / case.assumed_bounds).read_bytes() == (
        json.dumps(generated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    document = suite.read_json(suite.ROOT / case.mission_package)
    radius, bounds = demo.verified_assumed_corridor(suite.ROOT / case.assumed_bounds)
    assert not bounds.standoff_gate_enabled and bounds.fw_leg_peak_power_w == 500.0
    package = demo.load_mission_package({**document, "task_text": case.sentence}, mode="sim",
                                        allow_assumed_sim=True, assumed_sim_max_distance_m=radius,
                                        assumed_sim_corridor=bounds)
    first, second = document["places"]["poi:first-beacon"], document["places"]["poi:second-beacon"]
    vertices = (document["home"], first, second, document["home"])
    distance = sum(math.dist(a, b) for a, b in zip(vertices, vertices[1:]))
    # K is deliberately short (about 180 m) to prove the corridor-continue
    # mechanism and clearance. It is not a generalizable range case.
    assert 175 <= distance <= 185
    agl = 75.0
    limit = max_one_way_m("proof", agl_m=agl,
                          ground_high_m=document["x1_planning"]["terrain_rel_home_m"][1],
                          fw_peak_power_w=bounds.fw_leg_peak_power_w,
                          battery_wh=document["budget"]["energy_wh"],
                          package_reserve_wh=document["budget"]["reserve_energy_wh"],
                          constants=default_constants())
    assert 1 - distance / (2 * limit) >= 0.10
    plan = MissionPlan(1, "X1 mission", (
        MissionStep("takeoff", "vtol_takeoff", {"altitude_agl_m": agl}, "fw", "home"),
        MissionStep("fw", "fw_transition", {}, "w", "home"),
        MissionStep("w", "fw_goto_waypoint", {"target_ref": "poi:first-beacon", "altitude_agl_m": agl},
                    "w_2", "home"),
        MissionStep("w_2", "fw_goto_waypoint", {"target_ref": "poi:second-beacon", "altitude_agl_m": agl},
                    "w_3", "home"),
        MissionStep("w_3", "fw_goto_waypoint", {"target_ref": "home", "altitude_agl_m": agl}, "back", "home"),
        MissionStep("back", "back_transition", {}, "home", "home"),
        MissionStep("home", "return_home", {}, None, None),
    ), ("status",))
    assert x1_static_violations(plan, package, frozenset(document["capabilities"])) == []
    assert demo.compiled_mission_digest(package, plan) == demo.assumptions.CORRIDOR_BASE_SHA


@pytest.mark.parametrize("drift", [None, "base_mission_sha256", "motion_scope_sha256"])
def test_corridor_plan_only_worker_checks_exact_evidence_binding(tmp_path, monkeypatch, drift):
    from dataclasses import asdict

    from scripts import run_x1_autonomy_demo as demo

    case, = suite.select_cases(["corridor-dogleg"])
    spec = {"case": asdict(case), "slot": 7, "attempt_dir": str(tmp_path), "args": {
        "port": 9998, "planner_timeout_s": 3, "checkpoint": "fake.gguf", "llama_server": "fake-llama",
        "mission_package": "unused.json", "plan_only": True}}
    calls = []

    def fake_demo(argv, shared_post):
        calls.append(argv)
        assert shared_post("plan this", {}) == "model-wire"
        assert argv.count(demo.ASSUMED_SIM_FLAG) == 1 and "--plan-only" in argv
        assert Path(argv[argv.index(demo.ASSUMED_SIM_FLAG) + 1]) == suite.ROOT / case.assumed_bounds
        assert Path(argv[argv.index("--mission-package") + 1]) == suite.ROOT / case.mission_package
        output = Path(argv[argv.index("--output") + 1])
        output.mkdir()
        suite.write_json(output / "result.json", {"contingency_scenario": {
            "live_evidence": {"base_mission_sha256": "0" * 64 if drift == "base_mission_sha256"
                              else demo.assumptions.CORRIDOR_BASE_SHA},
            "motion_scope_sha256": "0" * 64 if drift == "motion_scope_sha256"
            else demo.assumptions.CORRIDOR_SCOPES["nominal"],
        }})
        return 0

    monkeypatch.setattr(suite, "local_json", lambda *args, **kw: {"choices": [{"message": {"content": "model-wire"}}]})
    monkeypatch.setattr(demo, "main", fake_demo)
    if drift:
        with pytest.raises(ValueError, match="pinned dogleg plan"):
            suite.run_worker(spec)
    else:
        assert suite.run_worker(spec) == 0
    assert len(calls) == 1


def test_corridor_pass_requires_proved_cruise_continues_and_every_fw_passage():
    case, = suite.select_cases(["corridor-dogleg"])
    result = fixture_flight()
    result["flight"]["waypoints_reached"] = ["w", "w_2", "w_3"]
    first = decision_trace()
    second = json.loads(json.dumps(first).replace("case:1", "case:2"))
    for rows, step_id in ((first, "w_2"), (second, "w_3")):
        for row in rows[:2]:
            row["request"]["mission_progress"].update(step_id=step_id, primitive="fw_goto_waypoint")
    trace = first + second
    assert all(suite.check_flight(case, result, trace)[0].values())
    second[-1].update(status="rejected", reason="unverified_clearance")
    checks, _ = suite.check_flight(case, result, trace)
    assert checks["expected_receipt"] and not checks["repeated_fw_continue_admitted"]
    result["flight"]["waypoints_reached"].remove("w_2")
    assert not suite.check_flight(case, result, trace)[0]["all_fw_legs_reached"]


def test_corridor_uncovered_phases_do_not_block_cruise_acceptance():
    case, = suite.select_cases(["corridor-dogleg"])
    result = fixture_flight()
    result["flight"]["waypoints_reached"] = ["w", "w_2", "w_3"]
    first = decision_trace()
    second = json.loads(json.dumps(first).replace("case:1", "case:2"))
    for rows, step_id in ((first, "w_2"), (second, "w_3")):
        for row in rows[:2]:
            row["request"]["mission_progress"].update(step_id=step_id, primitive="fw_goto_waypoint")
    # A rejected back_transition/approach continue has no corridor sweep. It is
    # reported as uncovered, not as a failure of the FW-cruise acceptance.
    uncovered = json.loads(json.dumps(first).replace("case:1", "case:3"))
    for row in uncovered:
        if "request" in row:
            row["request"]["mission_progress"].update(
                step_id="back", primitive="fw_transition", phase="transition")
        if row.get("kind") == "receipt":
            row.update(action=None, status="rejected", reason="cadence_mode_changed")
    checks, scores = suite.check_flight(case, result, first + second + uncovered)
    assert all(checks.values())
    assert scores["uncovered_phase_receipts"]["case:3"]["reason"] == "cadence_mode_changed"
    assert {row["request_id"] for row in scores["admitted"]} == {"case:1", "case:2"}
