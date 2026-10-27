from __future__ import annotations

from scripts.plan_from_task import score_task
from multidetect.autonomy.mission_plan import validate_mission_plan


def _plan(skill: str, params: dict | None = None):
    terminal = skill in {"land", "return_home"}
    steps = [{
        "id": "task", "skill": skill, "params": params or {},
        "on_success": None if terminal else "home",
        "on_fail": None if terminal else "home",
    }]
    if not terminal:
        steps.append({"id": "home", "skill": "return_home", "params": {},
                      "on_success": None, "on_fail": None})
    return validate_mission_plan({
        "plan_version": 1, "goal": "requested task", "report": [],
        "steps": steps,
    })


def test_fixed_wing_hover_is_correctly_rejected_by_validator():
    item = {"text": "fixed wing hover", "category": "fixed_wing_hover",
            "expect_invalid": True, "capabilities": ["airframe:fixed_wing"]}
    result = score_task(item, _plan("hover", {"duration_s": 20}), None)
    assert result["correct_rejection"] is True
    assert result["exact"] is True
    assert result["validator_decision"] == "reject"
    assert result["capability_violations"] == 0


def test_fixed_wing_target_land_is_rejected_but_nonviolating_plan_is_not():
    item = {"text": "fixed wing target land", "category": "fixed_wing_target_land",
            "expect_invalid": True, "capabilities": ["airframe:fixed_wing"]}
    landing = _plan("land", {"target_ref": "poi:gate"})
    assert score_task(item, landing, None)["correct_rejection"] is True

    item["text"] = "Go to the gate and return home"
    nonviolating = _plan("goto", {"target_ref": "poi:gate"})
    result = score_task(item, nonviolating, None)
    assert result["correct_rejection"] is False
    assert result["capability_violations"] == 0


def test_missing_payload_category_keeps_safe_plan_as_not_a_rejection():
    item = {"text": "deliver without payload", "category": "missing_payload_drop",
            "expect_invalid": True, "capabilities": []}
    result = score_task(item, _plan("return_home"), None)
    assert result["correct_rejection"] is False
    assert result["capability_violations"] == 0
