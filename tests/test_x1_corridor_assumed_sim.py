"""Step 2 isolation gates for the SITL-only X1 corridor template."""
from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_x1_planner import _fixture

from multidetect.autonomy.mission_package import CorridorBounds, load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.x1_plan import one_waypoint_primitives_input
from scripts import generate_x1_assumed_sim_bounds as generator
from scripts import run_x1_autonomy_demo as demo

CORRIDOR_PATH = demo.ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json"
ONBOARD_PLANNER_SHA = "92f51236edf609cf00d550dfb9b86ed2f22722e75a89292375b12808124f25df"
MODEL_GATE_SHA = "45b6e30986446ec2ae9d533bb166bd64fe9e19d648e1594f1a71f11dfdb4fe94"


def _corridor_document():
    return json.loads(CORRIDOR_PATH.read_text(encoding="utf-8"))


def _corridor_package():
    package = _fixture("Fly to the yard and return", target=(250, 0))
    document = deepcopy(package.document)
    document["x1_planning"]["max_distance_from_home_m"] = 4000.0
    return load_mission_package(document, mode="sim", allow_assumed_sim=True,
                                assumed_sim_max_distance_m=4000.0)


def _load_corridor(path, *, package=None, scope=None, base_digest=None):
    fault_scope = generator.CORRIDOR_SCOPES["nominal"]
    return demo.load_assumed_sim(
        path,
        package=package or _corridor_package(),
        site=SimpleNamespace(scenario_id=generator.WORLD, world_sha256=generator.WORLD_SHA,
                             terrain=SimpleNamespace(source_sha256=generator.DEM_SHA)),
        collector_plan=SimpleNamespace(image=generator.IMAGE),
        faults=(),
        site_bounds=dict(scope_sha256=scope or fault_scope, dem_sha256=generator.DEM_SHA,
                         base_mission_sha256=base_digest or generator.CORRIDOR_BASE_SHA,
                         obstacle_coverage=[], obstacles=[]),
        scope=scope or fault_scope,
        base_digest=base_digest or generator.CORRIDOR_BASE_SHA,
    )


def test_corridor_template_reproduces_and_has_exact_fields():
    generated = generator.generate_corridor(
        mode="sim", world=generator.WORLD, image=generator.IMAGE, profile_path=demo.PROFILE)
    assert CORRIDOR_PATH.read_text(encoding="utf-8") == json.dumps(
        generated, ensure_ascii=False, indent=2) + "\n"
    corridor = generated["assumed_sim_corridor"]
    assert set(corridor) == generator.CORRIDOR_ENVELOPE_KEYS
    assert corridor["standoff_m"] == 252.5
    assert corridor["standoff_gate_enabled"] is False
    assert corridor["fw_leg_peak_power_w"] == 344.0
    assert corridor["mc_peak_power_w"] == 750.0
    assert corridor["max_fw_legs"] == 4 and corridor["max_sweep_legs"] == 384
    assert generator.corridor_bounds(generated) == CorridorBounds.parse(corridor)
    envelope_only = {"assumed_sim_envelope": generated["assumed_sim_envelope"]}
    assert generator.corridor_bounds(envelope_only) is None


def test_d26_corridor_references_stay_in_the_scenario_runner_and_generator():
    roots = (demo.ROOT / "scripts", demo.ROOT / "src", demo.ROOT / "deploy")
    generated_calls = set()
    loader_keywords = set()
    for root in roots:
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if not any(token in source for token in ("generate_corridor", "assumed_sim_corridor")):
                continue
            for node in ast.walk(ast.parse(source)):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == "generate_corridor"):
                    generated_calls.add(path)
                if isinstance(node, ast.Call):
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if name == "generate_corridor":
                        generated_calls.add(path)
                    if name == "load_mission_package" and any(
                        keyword.arg == "assumed_sim_corridor" for keyword in node.keywords
                    ):
                        loader_keywords.add(path)
    assert generated_calls == {demo.ROOT / "scripts/run_x1_autonomy_demo.py",
                               demo.ROOT / "scripts/generate_x1_assumed_sim_bounds.py"}
    assert loader_keywords == {demo.ROOT / "scripts/run_x1_autonomy_demo.py"}


def test_d27_corridor_requires_envelope_and_lowering_still_rejects_multileg():
    document = _corridor_document()
    package = _corridor_package()
    with pytest.raises(ValueError, match="requires the assumed SIM envelope"):
        load_mission_package(package.document, mode="sim", allow_assumed_sim=True,
                             assumed_sim_corridor=document["assumed_sim_corridor"])
    # The package with a widened radius but no corridor still cannot lower a
    # multi-waypoint plan in step 2 (CORRIDOR_NOT_ENABLED arrives with lowering).
    document = deepcopy(package.document)
    document["places"]["poi:far"] = [500, 0]
    document["ref_labels"]["poi:far"] = "far marker"
    widened = load_mission_package(document, mode="sim", allow_assumed_sim=True,
                                   assumed_sim_max_distance_m=4000.0)
    graph = {
        "plan_version": 1, "goal": "two waypoints", "report": ["status"], "steps": [
            {"id": "takeoff", "skill": "vtol_takeoff", "params": {"altitude_agl_m": 60},
             "on_success": "fw", "on_fail": "home"},
            {"id": "fw", "skill": "fw_transition", "params": {},
             "on_success": "w1", "on_fail": "home"},
            {"id": "w1", "skill": "fw_goto_waypoint",
             "params": {"target_ref": "poi:yard", "altitude_agl_m": 60},
             "on_success": "w2", "on_fail": "home"},
            {"id": "w2", "skill": "fw_goto_waypoint",
             "params": {"target_ref": "poi:far", "altitude_agl_m": 60},
             "on_success": "back", "on_fail": "home"},
            {"id": "back", "skill": "back_transition", "params": {},
             "on_success": "home", "on_fail": "home"},
            {"id": "home", "skill": "return_home", "params": {},
             "on_success": None, "on_fail": None},
        ],
    }
    plan = validate_mission_plan(graph, capabilities=frozenset(widened.document["capabilities"]))
    with pytest.raises(ValueError, match="only one waypoint"):
        one_waypoint_primitives_input(plan, widened, frozenset(widened.document["capabilities"]))


@pytest.mark.parametrize("change", ["mode", "flight_mode", "signature", "key"])
def test_d28_corridor_is_unreachable_from_vehicle_paths(change):
    document = _corridor_document()
    package_doc = _corridor_package().document
    kwargs = {"mode": "sim", "allow_assumed_sim": True, "assumed_sim_max_distance_m": 4000.0,
              "assumed_sim_corridor": document["assumed_sim_corridor"]}
    if change == "mode":
        kwargs["mode"] = "flight"
    elif change == "flight_mode":
        kwargs["flight_mode"] = True
    elif change == "signature":
        kwargs["require_signature"] = True
    else:
        kwargs["key"] = b"0" * 32
    with pytest.raises(ValueError):
        load_mission_package(package_doc, **kwargs)
    if change != "mode":
        for vehicle_mode in ("flight", "real", "hil", "live"):
            with pytest.raises(ValueError):
                load_mission_package(package_doc, **{**kwargs, "mode": vehicle_mode})


def test_d28_x1_planning_key_set_is_unchanged():
    document = deepcopy(_corridor_package().document)
    document["x1_planning"]["corridor_extra"] = True
    with pytest.raises(ValueError, match="X1 requires world"):
        load_mission_package(document, mode="sim", allow_assumed_sim=True,
                             assumed_sim_max_distance_m=4000.0,
                             assumed_sim_corridor=_corridor_document()["assumed_sim_corridor"])


@pytest.mark.parametrize(("path", "delta"), [
    (("standoff_m",), 0.1),
    (("fw_leg_peak_power_w",), -1.0),
    (("max_sweep_legs",), 1),
    (("slice_m",), 0.1),
])
def test_d29_any_corridor_number_change_is_rejected(tmp_path, path, delta):
    raw = _corridor_document()
    raw["assumed_sim_corridor"][path[0]] += delta
    changed = tmp_path / "corridor.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="corridor bounds template"):
        demo.verified_assumed_corridor(changed)
    with pytest.raises(ValueError, match="corridor bounds template"):
        _load_corridor(changed)


def test_d29_any_justification_change_is_rejected(tmp_path):
    raw = _corridor_document()
    raw["dem_error_m"]["justification"] += "."
    changed = tmp_path / "corridor.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="corridor bounds template"):
        demo.verified_assumed_corridor(changed)
    with pytest.raises(ValueError, match="corridor bounds template"):
        _load_corridor(changed)


def test_d29_envelope_ceiling_still_applies(tmp_path):
    raw = _corridor_document()
    raw["assumed_sim_envelope"]["max_distance_from_home_m"] = 4001.0
    assert raw["assumed_sim_corridor"]["standoff_gate_enabled"] is False
    changed = tmp_path / "corridor.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="SITL-only ceiling"):
        demo.verified_assumed_corridor(changed)
    with pytest.raises(ValueError, match="SITL-only ceiling"):
        _load_corridor(changed)


def test_corridor_template_loads_as_an_assumed_sim_package():
    package, assumed_bounds, state = _load_corridor(CORRIDOR_PATH)
    assert package.mode == "sim"
    assert package.assumed_sim_max_distance_m == 4000.0
    assert package.assumed_sim_corridor == generator.corridor_bounds(_corridor_document())
    assert package.assumed_sim_corridor.standoff_gate_enabled is False
    assert assumed_bounds["dem_error_m"] == 12.0
    assert state["image_id"] == generator.IMAGE_ID


def test_d30_onboard_and_model_gate_configs_are_pinned():
    for relative, expected in (
        ("configs/onboard/x1-planner.json", ONBOARD_PLANNER_SHA),
        ("configs/simulation/x1-model-gate-v1.json", MODEL_GATE_SHA),
    ):
        digest = hashlib.sha256((demo.ROOT / relative).read_bytes()).hexdigest()
        assert digest == expected
