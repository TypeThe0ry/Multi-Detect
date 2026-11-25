from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.capability_contract import capabilities_for_airframe
from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_planner import compile_mission_plan, plan_json_schema, plan_prompt
from multidetect.autonomy.x1_plan import X1_CAPABILITY, x1_planner_context
from scripts.build_x1_plan_sft import generate
from scripts.x1_plan_eval import score_x1_task, summarize_x1

ROOT = Path(__file__).resolve().parents[1]


def _fixture(task: str, *, target=(250, 0), caps=None, terrain=(-5, 15), max_distance=1200,
             corridor=(-850, 1600, -900, 900), altitude_limits=(15, 145)):
    capabilities = sorted(caps or {X1_CAPABILITY, "gimbal_control"})
    return load_mission_package({
        "mission_id": "x1-test", "task_text": task, "risk_level": "low",
        "link_loss_policy": {"action": "return_now"}, "home": [0, 0],
        "places": {"poi:yard": list(target)}, "zones": {}, "routes": {},
        "ref_labels": {"poi:yard": "院子 / yard"}, "capabilities": capabilities,
        "oversight_steps": [],
        "operating_area": {"north_min_m": -1800, "north_max_m": 1800,
                           "east_min_m": -1500, "east_max_m": 1500,
                           "min_altitude_m": altitude_limits[0], "max_altitude_m": altitude_limits[1]},
        "budget": {"time_s": 1000, "reserve_time_s": 120, "energy_wh": 74, "reserve_energy_wh": 14.8},
        "x1_planning": {"world": "rw_farm_iowa__clear_noon", "max_distance_from_home_m": max_distance,
                        "corridor": dict(zip(("north_min_m", "north_max_m", "east_min_m", "east_max_m"), corridor, strict=True)),
                        "terrain_rel_home_m": list(terrain)},
    }, mode="sim")


def _plan(skill="fw_goto_waypoint", params=None, altitude=60):
    work = {"id": "w", "skill": skill,
            "params": params or {"target_ref": "poi:yard", "altitude_agl_m": altitude},
            "on_success": "back", "on_fail": "home"}
    if skill != "fw_goto_waypoint":
        work = {**work, "on_success": "back"}
    return {"plan_version": 1, "goal": "fly to yard and return", "report": ["status"], "steps": [
        {"id": "takeoff", "skill": "vtol_takeoff", "params": {"altitude_agl_m": 60},
         "on_success": "fw", "on_fail": "home"},
        {"id": "fw", "skill": "fw_transition", "params": {}, "on_success": "w", "on_fail": "home"},
        work,
        {"id": "back", "skill": "back_transition", "params": {}, "on_success": "home", "on_fail": "home"},
        {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
    ]}


def test_airframe_capability_and_schema_are_x1_scoped():
    profile = load_airframe_profile(ROOT / "configs/airframes/delta_tailsitter_gimbal.json")
    caps = capabilities_for_airframe(profile, {X1_CAPABILITY})
    assert X1_CAPABILITY in caps and "gimbal_control" in caps
    assert X1_CAPABILITY not in capabilities_for_airframe(
        load_airframe_profile(ROOT / "configs/airframes/x500_gimbal.json"), {X1_CAPABILITY}
    )
    schema = plan_json_schema(capabilities=caps)
    encoded = json.dumps(schema)
    assert '"enum": ["vtol_takeoff"]' in encoded
    assert '"enum": ["goto"]' not in encoded
    assert "altitude_agl_m" in encoded and '"minimum": 40' in encoded
    assert "return_home" in encoded and "anyOf" in schema


def test_legacy_multicopter_default_prompt_is_byte_stable():
    assert "vtol_takeoff" not in plan_prompt("return home")
    assert "Skills (params in brackets, * = required):" in plan_prompt("return home")


def test_package_and_planner_reject_missing_x1_context_and_duplicate_json():
    with pytest.raises(ValueError, match="X1 requires world"):
        load_mission_package({"mission_id": "bad", "task_text": "fly", "risk_level": "low",
            "link_loss_policy": {"action": "continue"}, "home": [0, 0], "places": {}, "zones": {},
            "routes": {}, "capabilities": [X1_CAPABILITY], "oversight_steps": [],
            "operating_area": {"north_min_m": -100, "north_max_m": 100, "east_min_m": -100,
                "east_max_m": 100, "min_altitude_m": 0, "max_altitude_m": 200},
            "budget": {"time_s": 1000, "reserve_time_s": 0, "energy_wh": 100, "reserve_energy_wh": 0}})
    package = _fixture("Fly to yard and return")
    plan, report = compile_mission_plan(lambda *_: '{"plan_version":1,"plan_version":1}',
        package.document["task_text"], refs=("poi:yard (yard / 院子)",),
        capabilities=frozenset(package.document["capabilities"]),
        plan_checker=lambda candidate: [{"code": "test", "message_en": "test"}],
        x1_context=x1_planner_context(package))
    assert plan is None and "duplicate JSON key" in report["error"]
    assert "X1 planning requires" in compile_mission_plan(lambda *_: "{}", "fly",
        capabilities=frozenset({X1_CAPABILITY}))[1]["error"]


@pytest.mark.parametrize("kwargs,code", [
    ({"target": (1250, 0)}, "X1_DISTANCE"),
    ({"target": (450, 545)}, "X1_CORRIDOR"),
    ({"altitude_limits": (15, 70)}, "X1_TERRAIN"),
    ({"target": (20, 0)}, "X1_MIN_LEG"),
])
def test_x1_package_admission_rejects_envelope_violations(kwargs, code):
    from multidetect.autonomy.mission_package import check_plan_against_package
    package = _fixture("Fly to yard then return", **kwargs)
    # Package context is accepted independently; candidates are admitted at compile/execution time.
    plan = validate_mission_plan(_plan(), capabilities=frozenset(package.document["capabilities"]))
    findings = check_plan_against_package(plan, package, capabilities=frozenset(package.document["capabilities"]))
    assert code in {finding["code"] for finding in findings}


def test_generated_offline_splits_have_exact_fixture_labels():
    rows = {split: [] for split in ("train", "validation", "holdout")}
    for split in rows:
        rows[split] = list(generate(split))
    assert len(rows["train"]) >= 1000
    assert {item["language"] for item, _ in rows["holdout"]} == {"zh", "en"}
    assert len({item["world"] for item, _ in rows["train"]} & {item["world"] for item, _ in rows["holdout"]}) == 0
    all_eval = [item for split in rows.values() for item, _ in split]
    results = [score_x1_task(item, json.dumps(item["expected_target"])) for item in all_eval]
    assert summarize_x1(results)["positive_exact_slots_rate"] == 1
    assert summarize_x1(results)["negative_correct_decision_rate"] == 1


def test_model_compile_shared_admission_and_primitives_bridge():
    from multidetect.autonomy.mission_executor import MissionExecutor
    from multidetect.autonomy.mission_package import check_plan_against_package
    from multidetect.autonomy.x1_plan import one_waypoint_primitives_input
    from scripts.plan_from_task import compile_task

    item, sample = next((item, sample) for item, sample in generate("holdout") if item["category"] == "waypoint")
    from scripts.x1_plan_eval import x1_eval_package
    package = x1_eval_package(item)
    caps = frozenset(package.document["capabilities"])
    schema = plan_json_schema(capabilities=caps)
    calls = []

    def post(prompt, grammar):
        calls.append((prompt, grammar))
        return json.dumps(item["expected_target"])

    plan, raw, attempts, repairs = compile_task(post, item["text"], tuple(item["refs"]), schema,
                                                capabilities=caps, x1_package=package)
    assert calls == [(sample["prompt"], schema)] and len(attempts) == 1
    assert score_x1_task(item, raw, package=package)["exact_slots"] and repairs is None
    assert not check_plan_against_package(plan, package, capabilities=caps)
    assert load_mission_package({**package.document, "plan": item["expected_target"]}, mode="sim").plan == plan
    view, legacy_plan, radius = one_waypoint_primitives_input(plan, package, caps)
    assert view.plan == legacy_plan and package.plan is None
    assert radius == package.document["x1_planning"]["max_distance_from_home_m"]
    assert legacy_plan.steps[0].params["altitude_m"] == plan.steps[2].params["altitude_agl_m"]
    assert legacy_plan.steps[0].params["target_ref"] == plan.steps[2].params["target_ref"]
    with pytest.raises(ValueError, match="SIM primitive adapter"):
        MissionExecutor(plan, loop_factory=lambda: None, mission_package=package)


def test_altitude_clamp_needs_explicit_permission():
    from multidetect.autonomy.plan_validation import validate_plan_for_execution

    package = _fixture("Fly to yard at 25 m AGL and return")
    caps = frozenset(package.document["capabilities"])
    raw = _plan(altitude=40)
    raw["steps"][0]["params"]["altitude_agl_m"] = 40
    assert validate_plan_for_execution(raw, package, package.document["task_text"], capabilities=caps).decision == "reject"
    task = package.document["task_text"] + "; if that altitude is outside limits, use the nearest legal AGL."
    assert validate_plan_for_execution(raw, package, task, capabilities=caps).decision == "accept"


def test_schema_snapshot_matches_runtime_and_scorer_rejects_wrong_slot():
    schema = json.loads((ROOT / "configs/onboard/x1-plan.schema.json").read_text(encoding="utf-8"))
    assert schema["anyOf"] == plan_json_schema(capabilities=frozenset({X1_CAPABILITY, "gimbal_control"}))["anyOf"]
    item, _ = next((item, row) for item, row in generate("holdout") if item["category"] == "observe")
    target = deepcopy(item["expected_target"])
    target["steps"][3]["params"]["duration_s"] += 1
    row = score_x1_task(item, json.dumps(target))
    assert row["valid_json"] and row["legal"] and row["same_skills"] and not row["exact_slots"]
    assert not score_x1_task(item, "{")["valid_json"]
