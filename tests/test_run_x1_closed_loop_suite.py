import json
import os
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


def test_case_table():
    args, rows = suite.parse_args(["--list-cases", "--case", "quiet-cadence", "--case", "ramped-wind"])
    assert args.slots == (7, 6, 5)
    assert [r.id for r in rows] == ["ramped-wind", "quiet-cadence"]
    assert len(suite.CASES) == 8
    assert {r.phase for r in suite.CASES} == {"mc", "fw", "steady"}
    for row in suite.CASES:
        assert 0 <= row.tool_budget <= 3 and 0 < row.expected_s < row.budget_s
        assert row.fault or row.cadence_s > 0
    for names in (["bad"], ["quiet-cadence", "quiet-cadence"]):
        with pytest.raises(ValueError):
            suite.select_cases(names)


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
import json, sys, time
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
