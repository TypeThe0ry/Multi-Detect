from __future__ import annotations

import json

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan
from multidetect.autonomy.mission_planner import (
    EXAMPLES,
    check_plan_refs,
    compile_mission_plan,
    plan_json_schema,
    plan_prompt,
    repair_plan_document,
)


def _find_plan(*, retry_back_edge: bool = False) -> dict:
    return {
        "plan_version": 1,
        "goal": "Find the red car",
        "steps": [
            {
                "id": "search",
                "skill": "area_search",
                "params": {"region": "zone:site", "query": "red car"},
                "on_success": "home",
                "on_fail": "search" if retry_back_edge else "home",
            },
            {
                "id": "home",
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            },
        ],
        "report": ["findings", "positions"],
    }


def test_examples_are_valid_plans_and_schema_covers_ungated_skills():
    for _text, plan in EXAMPLES:
        validate_mission_plan(plan)
    variants = plan_json_schema()["properties"]["steps"]["items"]["anyOf"]
    skills = {v["properties"]["skill"]["enum"][0] for v in variants}
    assert skills == {name for name, spec in SKILLS.items() if not spec.gated}
    count = next(v for v in variants if v["properties"]["skill"]["enum"] == ["count"])
    assert set(count["properties"]["params"]["required"]) == {"region", "query"}
    assert plan_prompt("找人").endswith("Task: 找人" + chr(10) + "Plan:")


def test_repair_breaks_retry_loops_and_ends_on_a_terminal_step():
    raw = {
        "plan_version": 1,
        "goal": "find",
        "steps": [
            {
                "id": "s1",
                "skill": "area_search",
                "params": {"region": "zone:all", "query": "car"},
                "on_success": "s2",
                "on_fail": "s1",
            },
            {
                "id": "s2",
                "skill": "record",
                "params": {"query": "car"},
                "on_success": None,
                "on_fail": None,
            },
        ],
        "report": [],
    }
    plan, notes = repair_plan_document(raw)
    validated = validate_mission_plan(plan)
    steps = {s.id: s for s in validated.steps}
    assert steps["s1"].on_fail == "home" and steps["s2"].on_success == "home"
    assert steps["home"].skill == "return_home" and notes


def test_repair_clears_terminal_edges_and_routes_branches_to_terminal():
    raw = {
        "plan_version": 1,
        "goal": "find",
        "steps": [
            {
                "id": "search",
                "skill": "area_search",
                "params": {"region": "zone:all", "query": "car"},
                "on_success": "home",
                "on_fail": "detour",
            },
            {
                "id": "detour",
                "skill": "record",
                "params": {"query": "car"},
                "on_success": None,
                "on_fail": "home",
            },
            {
                "id": "home",
                "skill": "return_home",
                "params": {},
                "on_success": "detour",
                "on_fail": "search",
            },
        ],
        "report": [],
    }
    repaired, notes = repair_plan_document(raw)
    validated = validate_mission_plan(repaired)
    steps = {step.id: step for step in validated.steps}
    assert steps["detour"].on_success == "home"
    assert steps["home"].on_success is None and steps["home"].on_fail is None
    assert any("terminal" in note for note in notes)


def test_check_plan_refs_does_not_accept_known_poi_as_region():
    document = {
        "steps": [
            {"id": "s1", "params": {"region": "poi:gate"}},
        ]
    }
    checked, notes, unknown = check_plan_refs(
        document, ("poi:gate", "zone:site"), substitute=False
    )
    assert checked["steps"][0]["params"]["region"] == "poi:gate"
    assert notes == []
    assert unknown == ["poi:gate"]


def test_check_plan_refs_repairs_wrong_kind_region_to_zone():
    document = {
        "steps": [
            {"id": "s1", "params": {"region": "finding:1"}},
        ]
    }
    checked, notes, unknown = check_plan_refs(
        document, ("poi:gate", "zone:site"), substitute=True
    )
    assert checked["steps"][0]["params"]["region"] == "zone:site"
    assert any("finding:1 -> zone:site" in note for note in notes)
    assert unknown == []


def test_check_plan_refs_never_widens_wrong_region_to_unbounded_zone_all():
    document = {
        "steps": [
            {"id": "s1", "params": {"region": "finding:1"}},
        ]
    }
    checked, notes, unknown = check_plan_refs(
        document, ("poi:gate",), substitute=True
    )
    assert checked["steps"][0]["params"]["region"] == "finding:1"
    assert notes == []
    assert unknown == ["finding:1"]


def test_check_plan_refs_keeps_unknown_zone_bounded():
    document = {
        "steps": [
            {"id": "s1", "params": {"region": "zone:missing"}},
        ]
    }
    checked, notes, unknown = check_plan_refs(
        document, ("poi:gate",), substitute=True
    )
    assert checked["steps"][0]["params"]["region"] == "zone:missing"
    assert notes == []
    assert unknown == ["zone:missing"]


def test_region_wrong_kind_retries_then_substitutes_zone():
    raw = {
        "plan_version": 1,
        "goal": "count",
        "steps": [
            {
                "id": "s1",
                "skill": "count",
                "params": {"region": "poi:gate", "query": "car"},
                "on_success": "s2",
                "on_fail": "s2",
            },
            {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
        "report": [],
    }
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return json.dumps(raw)

    compiled, report = compile_mission_plan(
        post, "count cars", refs=("poi:gate", "zone:site")
    )
    assert "unknown reference(s) poi:gate" in prompts[1]
    assert compiled is not None
    assert compiled.steps[0].params["region"] == "zone:site"
    assert report["error"] is None


def test_compile_mission_plan_accepts_valid_model_response():
    raw = json.dumps(_find_plan())
    calls = []
    schema = plan_json_schema()

    def post(prompt, supplied_schema):
        calls.append((prompt, supplied_schema))
        return raw

    plan, report = compile_mission_plan(
        post, "Find the red car", refs=("zone:site",), schema=schema
    )

    assert plan is not None
    assert [step.skill for step in plan.steps] == ["area_search", "return_home"]
    assert calls == [(plan_prompt("Find the red car", refs=("zone:site",)), schema)]
    assert set(report) == {"raw", "attempts", "repairs", "error"}
    assert report["raw"] == raw
    assert len(report["attempts"]) == 1
    assert all(seconds >= 0 for seconds in report["attempts"])
    assert report["repairs"] == []
    assert report["error"] is None


def test_compile_mission_plan_repairs_retry_back_edge():
    raw = json.dumps(_find_plan(retry_back_edge=True))
    calls = []

    def post(prompt, schema):
        calls.append(prompt)
        return raw

    plan, report = compile_mission_plan(post, "Find the red car")

    assert plan is not None
    assert plan.steps[0].on_fail == "home"
    assert len(calls) == 1
    assert report["raw"] == raw
    assert report["repairs"]
    assert any("search.on_fail" in note for note in report["repairs"])
    assert report["error"] is None


def test_compile_mission_plan_retries_invalid_json_with_error_feedback():
    valid_raw = json.dumps(_find_plan())
    responses = iter(("{not-json", valid_raw))
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return next(responses)

    plan, report = compile_mission_plan(post, "Find the red car")

    assert plan is not None
    assert len(prompts) == len(report["attempts"]) == 2
    assert "The previous plan was rejected:" not in prompts[0]
    assert "The previous plan was rejected:" in prompts[1]
    assert "Task: Find the red car" in prompts[1]
    assert report["raw"] == valid_raw
    assert report["repairs"] == []
    assert report["error"] is None


def test_compile_mission_plan_returns_error_after_exhausted_retries():
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return "{not-json"

    plan, report = compile_mission_plan(post, "Find the red car")

    assert plan is None
    assert len(prompts) == len(report["attempts"]) == 2
    assert report["raw"] == "{not-json"
    assert report["repairs"] == []
    assert isinstance(report["error"], str) and report["error"]
    assert "The previous plan was rejected:" in prompts[1]


def test_unknown_refs_are_retried_then_mapped_to_the_only_known_zone():
    import json

    from multidetect.autonomy.mission_planner import compile_mission_plan

    plan = {
        "plan_version": 1,
        "goal": "count cones",
        "steps": [
            {
                "id": "s1",
                "skill": "count",
                "params": {"region": "zone:lot", "query": "cone"},
                "on_success": "s2",
                "on_fail": "s2",
            },
            {"id": "s2", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
        "report": [],
    }
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return json.dumps(plan)

    compiled, report = compile_mission_plan(post, "count cones", refs=("zone:site",))
    assert "unknown reference(s) zone:lot" in prompts[1]
    assert compiled.steps[0].params["region"] == "zone:site"
    assert any("zone:lot -> zone:site" in note for note in report["repairs"])
