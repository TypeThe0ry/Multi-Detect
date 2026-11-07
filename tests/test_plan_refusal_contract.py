from __future__ import annotations

import json
import hashlib

import pytest

from multidetect.autonomy.mission_plan import (
    PlanDecision,
    validate_mission_plan,
    validate_plan_decision,
)
from multidetect.autonomy.mission_planner import (
    compile_mission_plan,
    plan_json_schema,
    plan_prompt,
)
from scripts.onboard_policy import build_mission_from_text
from scripts.plan_from_task import score_task


def _decision(**overrides):
    return {
        "plan_version": 1,
        "decision": "reject",
        "reason_code": "missing_capability",
        "reason": "No payload drop capability.",
        "missing_capabilities": ["payload_drop"],
        "unresolved_refs": [],
        "question": None,
        **overrides,
    }


def test_legacy_mission_plan_and_new_discriminator_schema_are_compatible():
    legacy = {"plan_version": 1, "goal": "return", "steps": [
        {"id": "home", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None}], "report": []}
    assert validate_mission_plan(legacy).steps[0].skill == "return_home"
    legacy_schema = plan_json_schema()
    assert "anyOf" not in legacy_schema
    legacy_bytes = json.dumps(legacy_schema, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(legacy_bytes).hexdigest() == (
        "7b58a883a6c009a843b489e554d947043bc50c406fdb210b56783810f4dcedf3"
    )
    schema = plan_json_schema(include_decision_contract=True)
    assert len(schema["anyOf"]) == 3
    assert schema["anyOf"][0]["required"] == ["plan_version", "goal", "steps", "report"]
    assert schema["anyOf"][1]["required"][1] == "decision"
    assert schema["anyOf"][1]["properties"]["decision"]["enum"] == ["reject"]


def test_refusal_validates_and_mixed_or_incomplete_shapes_fail():
    assert validate_plan_decision(_decision()).reason_code == "missing_capability"
    clarify = _decision(decision="clarify", reason_code="ambiguous_task", question="Which place?")
    assert validate_plan_decision(clarify).decision == "clarify"
    with pytest.raises(ValueError):
        validate_plan_decision(_decision(question="Which place?"))
    with pytest.raises(ValueError):
        validate_plan_decision({**_decision(), "goal": "substitute plan"})
    with pytest.raises(ValueError):
        validate_plan_decision(_decision(reason_code="made_up"))


def test_compiler_and_onboard_builder_return_decision_without_home_fallback():
    raw = json.dumps(_decision(), ensure_ascii=False)
    plan, report = compile_mission_plan(lambda *_: raw, "Drop the package")
    assert isinstance(plan, PlanDecision)
    assert report["decision"].reason_code == "missing_capability"
    built, _ = build_mission_from_text("Drop the package", (), lambda *_: raw)
    assert isinstance(built, PlanDecision)


def test_onboard_refusal_latches_result_without_constructing_executor(tmp_path):
    from scripts.onboard_policy import OnboardPolicy

    policy = OnboardPolicy.__new__(OnboardPolicy)
    policy.work_dir = tmp_path
    policy.sim_timeline = None
    policy.mission_package = None
    policy.mission_plan = object()
    decision = validate_plan_decision(_decision())
    with pytest.raises(ValueError, match="planner reject"):
        policy._record_planner_decision(decision)
    assert policy.mission_plan is None
    assert not hasattr(policy, "executor")


def test_package_refusal_is_written_to_existing_operator_downlink(tmp_path):
    from types import SimpleNamespace
    from scripts.onboard_policy import OnboardPolicy

    records = []
    notified = []
    policy = OnboardPolicy.__new__(OnboardPolicy)
    policy.work_dir = tmp_path
    policy.sim_timeline = None
    policy.mission_package = None
    policy.mission_plan = None
    policy.supervisor = SimpleNamespace(
        log=SimpleNamespace(mission_id="active", append=lambda *args, **kwargs: records.append((args, kwargs))),
        downlink=SimpleNamespace(notify=lambda: notified.append(True)),
    )
    with pytest.raises(ValueError, match="planner reject"):
        policy._record_planner_decision(validate_plan_decision(_decision()),
                                        SimpleNamespace(mission_id="requested"))
    assert records[0][0][0] == "mission_refused"
    assert records[0][1]["requested_mission_id"] == "requested"
    payload = records[0][1]["decision"]
    assert payload["plan_version"] == 1
    assert payload["decision"] == "reject"
    assert payload["reason_code"] == "missing_capability"
    assert payload["reason"] == "No payload drop capability."
    assert list(payload["missing_capabilities"]) == ["payload_drop"]
    assert list(payload["unresolved_refs"]) == []
    assert payload["question"] is None
    assert notified == [True]


def test_planner_prompt_contains_bilingual_decision_examples_with_bounded_growth():
    legacy = plan_prompt("Find a car")
    prompt = plan_prompt("Find a car", include_decision_contract=True)
    assert "missing_capability" in prompt and "ambiguous_task" in prompt
    assert "payload drop capability" in prompt and "Which place do you mean" in prompt
    assert len(prompt.encode("utf-8")) - len(legacy.encode("utf-8")) < 1200


def test_structured_rejection_scores_as_correct_only_for_expected_reason():
    task = {"text": "Drop the package", "expect_invalid": True, "reason_code": "missing_capability"}
    result = score_task(task, None, None, raw=json.dumps(_decision()))
    assert result["correct_rejection"] is True
    wrong = score_task({**task, "reason_code": "unsafe_request"}, None, None, raw=json.dumps(_decision()))
    assert wrong["correct_rejection"] is False
