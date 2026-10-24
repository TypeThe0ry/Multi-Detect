from __future__ import annotations

from copy import deepcopy

from multidetect.autonomy.mission_package import MissionPackage, load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.plan_validation import validate_plan_for_execution


def _package():
    doc = {
        "mission_id": "validation", "risk_level": "low",
        "task_text": "Find the gate",
        "link_loss_policy": {"action": "continue", "timeout_s": 30},
        "home": [0, 0], "places": {"poi:gate": [20, 0]},
        "zones": {"zone:yard": {"centre_ne": [10, 0], "radius_m": 4}},
        "routes": {}, "oversight_steps": [], "heartbeat_timeout_s": 5,
        "operating_area": {"north_min_m": -30, "north_max_m": 30,
            "east_min_m": -30, "east_max_m": 30,
            "min_altitude_m": 5, "max_altitude_m": 60},
        "budget": {"time_s": 600, "reserve_time_s": 60,
            "energy_wh": 300, "reserve_energy_wh": 30},
    }
    return load_mission_package(doc)


def _plan(skill="goto", params=None):
    return validate_mission_plan({"plan_version": 1, "goal": "Find the gate",
        "steps": [
            {"id": "task", "skill": skill, "params": params or {"target_ref": "poi:gate"},
             "on_success": "home", "on_fail": "home"},
            {"id": "home", "skill": "return_home", "params": {},
             "on_success": None, "on_fail": None}], "report": ["status"]})


def _return_plan():
    return validate_mission_plan({"plan_version": 1, "goal": "Return safely",
        "steps": [{"id": "home", "skill": "return_home", "params": {},
                   "on_success": None, "on_fail": None}], "report": ["status"]})


def test_valid_plan_is_accepted_without_mutation():
    plan = _plan()
    result = validate_plan_for_execution(plan, _package(), "Find the gate")
    assert result.decision == "accept"
    assert result.plan == plan
    assert result.repairs == ()


def test_outside_operating_area_is_rejected():
    package = _package()
    package = MissionPackage(package.document,
        {**package.refs, "poi:gate": (100.0, 0.0)}, None)
    result = validate_plan_for_execution(_plan(), package, "Find the gate")
    assert result.decision == "reject"
    assert any("作业区" in reason for reason in result.reasons_zh)


def test_count_quantity_mismatch_requests_clarification():
    plan = _plan("count", {"region": "zone:yard", "query": "cones", "max_count": 12})
    result = validate_plan_for_execution(plan, _package(), "统计停车场内所有锥桶")
    assert result.decision == "clarify"
    assert 1 <= len(result.questions_zh) <= 3


def test_r18_real_output_missing_record_is_flagged_for_clarification():
    # r18 planning-v3 row 4: search is planned, but the requested image archive is omitted.
    plan = _plan("area_search", {"region": "zone:all", "query": "purple suitcase"})
    result = validate_plan_for_execution(
        plan,
        _package(),
        "Look for a purple suitcase, then save an image as evidence and come back",
    )
    assert result.decision == "clarify"
    assert any("影像" in question for question in result.questions_zh)


def test_conditional_or_for_return_does_not_clarify():
    result = validate_plan_for_execution(
        _return_plan(), _package(), "电量低或者丢失信号就返航"
    )
    assert result.decision == "accept"


def test_conditional_or_for_hover_does_not_clarify():
    plan = {"plan_version": 1, "goal": "hold", "report": [], "steps": [
        {"id": "hold", "skill": "hover", "params": {"duration_s": 20},
         "on_success": "home", "on_fail": "home"},
        {"id": "home", "skill": "return_home", "params": {},
         "on_success": None, "on_fail": None}]}
    result = validate_plan_for_execution(
        plan, _package(), "发现异常或者遇到障碍就悬停"
    )
    assert result.decision == "accept"


def test_negated_place_alternatives_do_not_clarify():
    result = validate_plan_for_execution(
        _return_plan(), _package(), "Do not substitute the bridge or pad"
    )
    assert result.decision == "accept"


def test_two_colors_of_same_category_do_not_clarify():
    plan = _plan("count", {"region": "zone:yard", "query": "car"})
    result = validate_plan_for_execution(plan, _package(), "统计红色或白色的车")
    assert result.decision == "accept"


def test_two_known_target_categories_with_one_selected_clarifies():
    plan = _plan("area_search", {"region": "zone:all", "query": "traffic cone"})
    result = validate_plan_for_execution(plan, _package(), "找一个消防栓或者锥桶")
    assert result.decision == "clarify"


def test_count_limit_only_clarifies_when_smaller_than_explicit_quantity():
    plan = _plan("count", {"region": "zone:yard", "query": "car", "max_count": 8})
    mismatch = validate_plan_for_execution(plan, _package(), "统计 12 辆车")
    enough = validate_plan_for_execution(
        _plan("count", {"region": "zone:yard", "query": "car", "max_count": 12}),
        _package(), "统计 12 辆车"
    )
    all_with_cap = validate_plan_for_execution(
        plan, _package(), "统计所有车辆"
    )
    assert mismatch.decision == "clarify"
    assert enough.decision == "accept"
    assert all_with_cap.decision == "clarify"


def test_unknown_skill_is_rejected_even_for_hand_built_plan():
    from multidetect.autonomy.mission_plan import MissionPlan, MissionStep

    plan = MissionPlan(1, "unsupported", (MissionStep("x", "unknown", {}, None, None),), ())
    result = validate_plan_for_execution(plan, _package(), "do unsupported thing")
    assert result.decision == "reject"


def test_slight_altitude_overshoot_is_repaired_and_reported():
    plan = _plan("goto", {"target_ref": "poi:gate", "altitude_m": 62})
    result = validate_plan_for_execution(plan, _package(), "Find the gate")
    assert result.decision == "repair"
    assert result.plan.steps[0].params["altitude_m"] == 60
    assert len(result.repairs) == 1


def test_no_fly_region_is_rejected():
    package = _package()
    doc = deepcopy(package.document)
    doc["zones"]["zone:yard"]["risk_tag"] = "no-fly"
    package = load_mission_package(doc)
    plan = _plan("area_search", {"region": "zone:yard", "query": "cars"})
    result = validate_plan_for_execution(plan, package, "Search the yard for cars")
    assert result.decision == "reject"
    assert any("禁飞区" in reason for reason in result.reasons_zh)


def test_missing_return_home_is_repaired():
    plan = {"plan_version": 1, "goal": "Find the gate", "report": ["status"],
        "steps": [{"id": "task", "skill": "goto", "params": {"target_ref": "poi:gate"},
                   "on_success": None, "on_fail": None}]}
    result = validate_plan_for_execution(plan, _package(), "Find the gate")
    assert result.decision == "repair"
    assert result.plan.steps[-1].skill == "return_home"
    assert result.repairs

