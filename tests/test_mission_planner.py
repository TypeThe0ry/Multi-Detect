from __future__ import annotations

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan
from multidetect.autonomy.mission_planner import (
    EXAMPLES,
    plan_json_schema,
    plan_prompt,
    repair_plan_document,
)


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
