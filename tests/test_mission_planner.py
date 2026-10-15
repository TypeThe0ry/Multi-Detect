from __future__ import annotations

import hashlib
import json

import pytest

from multidetect.autonomy.mission_plan import SKILLS, validate_mission_plan
from multidetect.autonomy.mission_planner import (
    EXAMPLES,
    PLAN_PROMPT_VARIANTS,
    compile_mission_plan,
    plan_json_schema,
    plan_prompt,
    plan_prompt_parts,
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


def test_default_prompt_bytes_match_original_training_prompt():
    # Snapshot recorded from the unchanged implementation before adding ablations.
    prompt = plan_prompt(
        "Find the red car", refs=("zone:site (the site)", "poi:pad (launch pad)"),
        error="unknown reference",
    )
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "24681904d570110416b1482a14f6db3d235b8973e89af0533c5c6c846cb6f4ca"
    )
    assert prompt == plan_prompt(
        "Find the red car", refs=("zone:site (the site)", "poi:pad (launch pad)"),
        error="unknown reference", prompt_variant="default",
    )


@pytest.mark.parametrize("variant", PLAN_PROMPT_VARIANTS)
def test_prompt_parts_render_exact_prompt_in_order(variant):
    parts = plan_prompt_parts("找人", refs=("zone:yard",), prompt_variant=variant)
    assert list(parts) == ["system", "rules", "refs", "examples", "repair", "task"]
    assert "".join(parts.values()) == plan_prompt(
        "找人", refs=("zone:yard",), prompt_variant=variant
    )
    assert parts["repair"] == ""
    assert parts["refs"] == "Known places: zone:yard.\n"


@pytest.mark.parametrize(
    ("variant", "example_count", "compact_rules"),
    [("default", 6, False), ("no_examples", 0, False), ("two_examples", 2, False),
     ("compact_rules", 6, True), ("compact", 0, True)],
)
def test_prompt_ablations_isolate_rules_and_example_count(variant, example_count, compact_rules):
    original = plan_prompt_parts("Inspect the truck")
    parts = plan_prompt_parts("Inspect the truck", prompt_variant=variant)
    assert parts["examples"].count("Task:") == example_count
    assert (parts["rules"] != original["rules"]) == compact_rules
    assert parts["system"] == original["system"]
    for name, spec in SKILLS.items():
        if not spec.gated:
            assert name in parts["rules"]
            assert all(param in parts["rules"] for param in spec.allowed_params)
    assert "return_home" in parts["rules"] and "land" in parts["rules"]


def test_unknown_prompt_variant_is_rejected_before_model_call():
    with pytest.raises(ValueError, match="unknown planning prompt variant"):
        plan_prompt("Find the car", prompt_variant="typo")
    with pytest.raises(ValueError, match="unknown planning prompt variant"):
        compile_mission_plan(lambda *args: pytest.fail("called model"), "Find the car",
                             prompt_variant="typo")


def test_short_prompt_variant_is_preserved_when_retrying():
    responses = iter(("invalid json", json.dumps(_find_plan())))
    prompts = []

    def post(prompt, schema):
        prompts.append(prompt)
        return next(responses)

    plan, report = compile_mission_plan(post, "Find the car", prompt_variant="compact")
    assert plan is not None and len(report["attempts"]) == 2
    assert prompts[0] == plan_prompt("Find the car", prompt_variant="compact")
    assert "The previous plan was rejected:" in prompts[1]
    assert all(prompt.count("Task:") == 1 for prompt in prompts)
    assert all("Skills (* required):" in prompt for prompt in prompts)


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


def test_compile_passes_payload_capability_to_plan_validation():
    raw = json.dumps({
        "plan_version": 1,
        "goal": "deliver",
        "steps": [
            {
                "id": "drop",
                "skill": "deliver",
                "params": {"target_ref": "home", "payload_ref": "home"},
                "on_success": "home",
                "on_fail": "home",
            },
            {
                "id": "home", "skill": "return_home", "params": {},
                "on_success": None, "on_fail": None,
            },
        ],
        "report": [],
    })
    plan, report = compile_mission_plan(
        lambda _prompt, _schema: raw,
        "deliver",
        capabilities=frozenset({"payload"}),
    )
    assert plan is not None
    assert report["error"] is None


def test_retry_clears_package_violations_and_raw_before_transport_error():
    responses = iter(("{}", RuntimeError("transport down")))

    def post(_prompt, _schema):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    def reject(_plan):
        return [{"code": "NO_FLY_ZONE", "message_zh": "禁飞", "message_en": "no fly"}]

    plan, report = compile_mission_plan(post, "find", plan_checker=reject)
    assert plan is None
    assert report["error"] == "transport down"
    assert report["raw"] == ""
    assert "package_violations" not in report


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
