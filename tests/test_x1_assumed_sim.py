"""Isolation and reproducibility of the Iowa-only assumed bounds loader."""
import ast
import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from scripts import generate_x1_assumed_sim_bounds as generator
from scripts import run_x1_autonomy_demo as demo
from test_x1_planner import _fixture

PATH = demo.ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa.json"


def test_committed_assumptions_reproduce():
    generated = generator.generate(mode="sim", world=generator.WORLD, image=generator.IMAGE, profile_path=demo.PROFILE)
    assert PATH.read_text(encoding="utf-8") == json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    assert generated["dem_error_m"]["value"] >= 10
    assert generated["battery_capacity_wh"]["value"] == 72
    for entry in [generated[key] for key in ("dem_error_m", "landing_pad", "battery", "battery_capacity_wh")] + list(
            generated["suffixes"].values()):
        assert entry["basis"] == "ASSUMED_SIM" and len(entry["justification"].splitlines()) == 1


@pytest.mark.parametrize(("mode", "world", "image"), [
    (None, generator.WORLD, generator.IMAGE),
    ("flight", generator.WORLD, generator.IMAGE),
    ("real", generator.WORLD, generator.IMAGE),
    ("hil", generator.WORLD, generator.IMAGE),
    ("sim", "other_world", generator.IMAGE),
    ("sim", generator.WORLD, "vehicle-image"),
])
def test_generator_and_runner_reject_other_contexts(mode, world, image):
    with pytest.raises(ValueError, match="explicit sim mode"):
        generator.generate(mode=mode, world=world, image=image, profile_path=demo.PROFILE)
    with pytest.raises(ValueError, match="explicit sim mode"):
        demo.load_assumed_sim(PATH, package=SimpleNamespace(mode=mode), site=SimpleNamespace(scenario_id=world),
                              collector_plan=SimpleNamespace(image=image), faults=(), site_bounds=None,
                              scope=None, base_digest=None)


def test_vehicle_and_generic_package_loader_refuse_assumed_package():
    with pytest.raises(ValueError):
        load_mission_package(PATH, mode="flight", flight_mode=True, allow_assumed_sim=True)
    package = _fixture("Fly over the yard, return home and land", target=(320, 0))
    doc = {**package.document, "x1_live_evidence": {"basis": generator.BASIS}}
    with pytest.raises(ValueError, match="authorized SITL scenario runner"):
        load_mission_package(doc, mode="sim")
    with pytest.raises(ValueError):
        load_mission_package(doc, mode="flight", flight_mode=True, allow_assumed_sim=True)
    with pytest.raises(ValueError, match="unknown field"):
        from multidetect.sim_teacher.x1_live_evidence_builder import build_live_evidence

        bound = json.loads(PATH.read_text(encoding="utf-8"))
        build_live_evidence(package, terrain=SimpleNamespace(source_sha256=None), scope_sha256=None,
                            base_mission_sha256=None, surveyed_bounds=bound)


def test_production_import_and_cli_opt_in_are_confined_to_the_scenario_runner():
    # Scan project-owned Python entry points, including the vehicle code under src.
    references = {"generate_x1_assumed_sim_bounds": set(), "allow_assumed_sim": set(), "--assumed-sim-bounds": set()}
    for directory in ("scripts", "src", "deploy"):
        for path in (demo.ROOT / directory).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if not any(word in source for word in references):
                continue
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and (
                    any(alias.name.endswith("generate_x1_assumed_sim_bounds") for alias in node.names)
                    or (getattr(node, "module", None) or "").endswith("generate_x1_assumed_sim_bounds")
                ):
                    references["generate_x1_assumed_sim_bounds"].add(path)
                if isinstance(node, ast.keyword) and node.arg == "allow_assumed_sim":
                    references["allow_assumed_sim"].add(path)
                if isinstance(node, ast.Constant) and node.value == "--assumed-sim-bounds":
                    references["--assumed-sim-bounds"].add(path)
    assert all(paths == {demo.ROOT / "scripts/run_x1_autonomy_demo.py"} for paths in references.values())


@pytest.fixture
def iowa_base():
    # This is the existing recorded Iowa route, with the pre-evidence package
    # reconstructed without the SIM-only battery override.
    archive = demo.read_object(demo.ROOT / "tests/fixtures/x1_continue_after_retask/episode-0000-68bf5c36.json")
    doc = archive["package"]
    del doc["x1_live_evidence"], doc["budget"]["battery_capacity_wh"]
    return load_mission_package(doc, mode="sim"), archive["original_plan"]


def _model_wire(recorded):
    ids = {step["id"]: f"s{i}" for i, step in enumerate(recorded["steps"], 1)}
    return {**recorded, "goal": "Fly over the yard at 85 m AGL, then return home and land.", "steps": [
        {**step, "id": ids[step["id"]], "on_success": ids.get(step["on_success"]),
         "on_fail": ids.get(step["on_fail"])} for step in recorded["steps"]
    ]}


def test_model_aliases_share_exact_canonical_graph_and_binding(iowa_base):
    package, recorded = iowa_base
    caps = frozenset(package.document["capabilities"])
    baseline = demo.canonical_mission_plan(validate_mission_plan(recorded, capabilities=caps))
    model = _model_wire(recorded)
    record = {}
    plan, _ = demo.compile_plan(package, lambda *_: json.dumps(model), record)
    canonical = demo.canonical_mission_plan(plan)
    assert record["plan_json"] == model and json.loads(record["raw_planner_output"]) == model
    assert [step.id for step in plan.steps] == ["s1", "s2", "s3", "s4", "s5"]
    assert canonical == baseline == demo.canonical_mission_plan(canonical)
    assert [step.id for step in canonical.steps] == [step["id"] for step in recorded["steps"]]
    assert demo.compiled_mission_digest(package, canonical) == generator.BASE_SHA


@pytest.mark.parametrize("change", ["skill", "param", "order", "success", "failure", "target", "altitude", "report", "task"])
def test_exact_iowa_binding_rejects_semantic_changes(iowa_base, change):
    package, recorded = iowa_base
    caps = frozenset(package.document["capabilities"])
    graph = _model_wire(recorded)
    steps = graph["steps"]
    if change == "skill":
        steps[-1].update(skill="land")
    elif change == "param":
        steps[-1]["params"] = {"altitude_m": 70}
    elif change == "order":
        steps[1], steps[2] = steps[2], steps[1]
    elif change == "success":
        steps[2]["on_success"] = "s5"
    elif change == "failure":
        steps[1]["on_fail"] = "s4"
    elif change == "target":
        steps[2]["params"] = {**steps[2]["params"], "target_ref": "poi:other"}
    elif change == "altitude":
        steps[2]["params"] = {**steps[2]["params"], "altitude_agl_m": 86}
    elif change == "report":
        graph["report"] = []
    elif change == "task":
        package = replace(package, document={**package.document, "task_text": "Different operator request"})
    plan = demo.canonical_mission_plan(validate_mission_plan(graph, capabilities=caps))
    base = demo.compiled_mission_digest(package, plan)
    assert base != generator.BASE_SHA
    # Supply even an otherwise-admitted motion digest: the exact graph/package
    # pin must still reject changes a lowerer might not represent in motion.
    scope, dem = generator.SCOPES["link_loss:mc_return"], generator.DEM_SHA
    with pytest.raises(ValueError, match="exact Iowa route"):
        demo.load_assumed_sim(PATH, package=package,
                              site=SimpleNamespace(scenario_id=generator.WORLD, world_sha256=generator.WORLD_SHA,
                                                   terrain=SimpleNamespace(source_sha256=dem)),
                              collector_plan=SimpleNamespace(image=generator.IMAGE),
                              faults=(SimpleNamespace(kind="link_loss"),), scope=scope, base_digest=base,
                              site_bounds=dict(scope_sha256=scope, dem_sha256=dem, base_mission_sha256=base,
                                               obstacle_coverage=[], obstacles=[]))


@pytest.mark.parametrize("change", ["window", "other_fault", "world", "dem"])
def test_battery_scope_is_not_a_wildcard(iowa_base, change):
    package, _ = iowa_base
    scope = generator.SCOPES["battery_drop:mc_return"]
    dem, world = generator.DEM_SHA, generator.WORLD_SHA
    if change == "window":
        scope = "0" * 64
    elif change == "other_fault":
        scope = generator.SCOPES["link_loss:mc_return"]
    elif change == "world":
        world = "0" * 64
    elif change == "dem":
        dem = "0" * 64
    with pytest.raises(ValueError, match="exact Iowa route"):
        demo.load_assumed_sim(PATH, package=package,
                              site=SimpleNamespace(scenario_id=generator.WORLD, world_sha256=world,
                                                   terrain=SimpleNamespace(source_sha256=dem)),
                              collector_plan=SimpleNamespace(image=generator.IMAGE),
                              faults=(SimpleNamespace(kind="battery_drop"),), scope=scope,
                              base_digest=generator.BASE_SHA,
                              site_bounds=dict(scope_sha256=scope, dem_sha256=dem, obstacle_coverage=[],
                                               base_mission_sha256=generator.BASE_SHA, obstacles=[]))


@pytest.mark.parametrize(("case_id", "scope_key"), [
    ("mc-link-loss", "link_loss:mc_return"), ("mc-battery-drop", "battery_drop:mc_return"),
    ("evidence-follow-event", "target_found"), ("evidence-follow-cadence", "target_found"),
])
def test_suite_model_reaches_scoped_sim_boundary(iowa_base, tmp_path, monkeypatch, case_id, scope_key):
    from multidetect.autonomy.x1_contingency_executor import RuntimeFrame
    from multidetect.sim_teacher.streams import MavlinkState
    from scripts import run_x1_closed_loop_suite as suite

    case, = suite.select_cases([case_id])
    package, recorded = iowa_base
    model = _model_wire(recorded)
    scenario = demo.ROOT / suite.SCENARIO
    # Use the actual Iowa DEM/runtime for lowering. Independent tests cover
    # provenance verification; this fixture supplies no real obstacle survey.
    source = demo.read_object(demo.long_range.sidecar_path(scenario))
    dem_path = demo.ROOT / "simulation/terrain" / f"{suite.WORLD}.dem.json"
    terrain = demo.load_local_dem_grid(dem_path)
    site = demo.Site(scenario, suite.WORLD, tmp_path / "iowa.sdf", dem_path, terrain, generator.WORLD_SHA,
                     demo.GeoPoint.from_mapping(source["mission_compile_runtime"]["home_position"]), source)
    monkeypatch.setattr(demo, "load_site", lambda *_: site)
    context = tmp_path / "context.json"
    context.write_text(json.dumps(package.document), encoding="utf-8")
    schedule = demo.ROOT / case.schedule
    bounds = tmp_path / "iowa.site-bounds.json"
    audit = tmp_path / "decisions.jsonl"
    base_args = dict(sentence=case.sentence, world=suite.SITE, scenario=scenario, mission_package=context,
                     slot=7, contingency_scenario=schedule, inject_fault=[case.fault], generate_live_evidence=True,
                     decision_port=9957, decision_cadence_s=case.cadence_s,
                     target_cadence_only=case.trigger == "cadence")
    preflight = tmp_path / "preflight"

    def post(*_):
        return json.dumps(model)

    assert demo.run(SimpleNamespace(**base_args, plan_only=True, output=preflight), post=post) == 0
    record = demo.read_object(preflight / "result.json")
    contingency = record["contingency_scenario"]
    scope = contingency["motion_scope_sha256"]
    assert scope == generator.SCOPES[scope_key]
    assert contingency["live_evidence"]["base_mission_sha256"] == generator.BASE_SHA
    bounds.write_text(json.dumps(dict(scope_sha256=scope, dem_sha256=terrain.source_sha256,
                                     base_mission_sha256=generator.BASE_SHA,
                                     obstacle_coverage=[-1800, 1800, -1500, 1500, 200, 600], obstacles=[])),
                      encoding="utf-8")
    monkeypatch.setattr(demo.x1_site_evidence, "verify_site_bounds", lambda path, **_: demo.read_object(path))
    monkeypatch.setattr(demo, "llama_decisions", lambda *_, **__: lambda request: None)
    live = tmp_path / "live"

    def stop_at_launch(args, collector_plan, steps, mission, current):
        assert [asdict(step) for step in steps] == current["prepared"]["steps"]
        assert [step.source_step_id for step in steps] == ["w", "w", "w", "home", "home", "home"]
        assert current["validator"]["verdict"] == "accepted"
        assert mission["scenario_package"].document["x1_live_evidence"]["basis"] == generator.BASIS
        assert ("airborne_retask" in mission["assumed_sim"]) == (case.fault == "target_found")
        scenario_run = demo.make_scenario(args, mission, steps, current)
        tick = scenario_run.make_decision(None, lambda _: None, "cpu", (1, 1))
        scenario_run.decision = tick
        try:
            frame = RuntimeFrame(1, 1, package.mission_id, 1, 1, False, True, case.phase, 85, None, True, {},
                                 active_motion_index=3 if case.phase == "mc" else 1)
            progress = (scenario_run._mission_progress(1, None, MavlinkState(), frame, now_ns=1_000_000)
                        if case.cadence_s else None)
            tick.tick(frame, events=((case.fault, "fault"),), mission_progress=progress)
            row = json.loads(audit.read_text().splitlines()[0])
            assert row["kind"] == "request_opened"
            request = row["request"]
            assert request.get("trigger", "event") == case.trigger
            digest = current["compiled_mission_sha256"]
            assert tick.plan_digest == digest
            if progress is not None:
                assert request["mission_progress"]["step_id"] == "w"
                digest = demo.canonical_sha256({"compiled_plan_sha256": digest,
                                                "steps": [asdict(step) for step in steps[2:]]})
            assert request["remaining_plan_sha256"] == digest
        finally:
            tick.close()
        raise RuntimeError("CPU test stopped before SITL launch")

    args = SimpleNamespace(**base_args, plan_only=False, output=live, assumed_sim_bounds=PATH,
                           live_evidence_bounds=bounds, decision_audit=audit)
    assert demo.run(args, post=post, launch=stop_at_launch) == 1
    result = demo.read_object(live / "result.json")
    assert result["flight"]["reason"] == "CPU test stopped before SITL launch"
    assert result["plan_json"] == model
    assert result["canonical_plan"] == record["canonical_plan"]
    assert result["contingency_scenario"]["motion_scope_sha256"] == scope
