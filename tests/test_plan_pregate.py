from __future__ import annotations

import json

from multidetect.autonomy.mission_plan import PlanDecision
from multidetect.autonomy.plan_pregate import preflight_plan_task
from scripts.onboard_policy import build_mission_from_text
from scripts.plan_from_task import compile_task


REFS = ("poi:gate (大门 / gate)", "poi:pad (起降坪 / landing pad / pad)")


def test_payload_delivery_requires_action_object_and_missing_capability():
    result = preflight_plan_task("Deliver the parcel to the gate.", REFS)
    assert isinstance(result, PlanDecision)
    assert result.reason_code == "missing_capability"
    assert result.missing_capabilities == ("payload_drop",)
    assert preflight_plan_task("把样品投递到大门。", REFS).reason_code == "missing_capability"
    assert preflight_plan_task(
        "Deliver the parcel to the gate.", REFS, capabilities={"payload_drop"}
    ) is None


def test_fixed_wing_hover_and_target_landing_are_blocked():
    caps = {"airframe:fixed_wing"}
    assert preflight_plan_task("Have the fixed-wing aircraft hover over the lot.", REFS, caps).missing_capabilities == ("hover",)
    assert preflight_plan_task("固定翼飞到大门上方定点降落。", REFS, caps).missing_capabilities == ("landing_on_target",)


def test_unknown_place_needs_explicit_move_and_map_evidence():
    result = preflight_plan_task("Fly to the nonexistent Raven Pier and return home.", REFS)
    assert result.reason_code == "unknown_place"
    assert result.unresolved_refs == ("nonexistent Raven Pier",)
    assert preflight_plan_task("飞去地图上没有的Raven Pier，找到后再返航。", REFS).reason_code == "unknown_place"
    assert preflight_plan_task("Go to the gate and return home.", REFS) is None
    assert preflight_plan_task("Inspect the gate and return home.", REFS) is None


def test_negated_capability_action_is_not_blocked():
    assert preflight_plan_task("Do not deliver the parcel; go to the gate.", REFS) is None
    assert preflight_plan_task("固定翼不要悬停，直接返航。", REFS, {"airframe:fixed_wing"}) is None


def test_builder_pregate_returns_decision_without_calling_model():
    def fail(*_args, **_kwargs):
        raise AssertionError("model should not be called")

    decision, report = build_mission_from_text(
        "Deliver the parcel to the gate.", REFS, fail, enable_plan_pregate=True
    )
    assert decision.reason_code == "missing_capability"
    assert report["source"] == "host_pregate"
    assert report["attempts"] == []


def test_builder_pregate_defaults_off():
    calls = []

    def post(*_args, **_kwargs):
        calls.append(True)
        return json.dumps({
            "plan_version": 1, "goal": "return", "steps": [
                {"id": "home", "skill": "return_home", "params": {},
                 "on_success": None, "on_fail": None}], "report": []
        })

    plan, _ = build_mission_from_text("Deliver the parcel to the gate.", REFS, post)
    assert plan.steps[0].skill == "return_home"
    assert calls


def test_evaluation_compile_task_pregate_skips_model_post():
    def fail(*_args, **_kwargs):
        raise AssertionError("model post should not be called")

    decision, raw, attempts, error = compile_task(
        fail, "Fly to the nonexistent Raven Pier.", REFS, {}, plan_pregate=True
    )
    assert decision.reason_code == "unknown_place"
    assert json.loads(raw)["unresolved_refs"] == ["nonexistent Raven Pier"]
    assert attempts == [0.0]
    assert error is None
