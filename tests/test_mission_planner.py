from __future__ import annotations

import json

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan
from multidetect.autonomy.mission_planner import (
    EXAMPLES,
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
