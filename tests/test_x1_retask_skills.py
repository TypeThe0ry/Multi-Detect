from __future__ import annotations

import json

import pytest

from multidetect.autonomy.mission_package import check_plan_against_package, load_mission_package
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.mission_planner import plan_json_schema, plan_prompt
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.autonomy.x1_contingency_executor import DECISION_SCHEMA, decode_decision
from multidetect.autonomy.x1_plan import X1_CAPABILITY, x1_param_schema, x1_planner_context

_CAPS = frozenset({X1_CAPABILITY})
_SEARCH = ("x1_search_leg", {"route_ref": "route:survey"})
_REPOSITION = ("x1_mc_reposition", {"target_ref": "poi:yard", "altitude_agl_m": 60})


def _step(step_id, skill, params, successor="home"):
    return {"id": step_id, "skill": skill, "params": params,
            "on_success": successor, "on_fail": "home" if successor else None}


def _plan(skill, params, *, nominal=False):
    steps = [_step("work", skill, params), _step("home", "return_home", {}, None)]
    if nominal:
        steps = [
            _step("up", "vtol_takeoff", {"altitude_agl_m": 60}, "fw"),
            _step("fw", "fw_transition", {}, "work"),
            _step("work", skill, params, "back"),
            _step("back", "back_transition", {}),
            steps[-1],
        ]
    return {"plan_version": 1, "goal": "Observe the yard", "steps": steps, "report": []}


@pytest.mark.parametrize(("skill", "params"), [_SEARCH, _REPOSITION])
def test_retask_decodes_new_skill_with_existing_x1_capability(skill, params):
    wire = json.dumps({
        "schema": DECISION_SCHEMA, "request_id": "request-1", "event_id": 1,
        "mission_id": "x1-test", "revision": 1, "action": "retask",
        "params": {"plan": _plan(skill, params)},
    })
    retask = decode_decision(wire, capabilities=_CAPS)
    assert retask.plan is not None
    work, recovery = retask.plan.steps
    assert (work.skill, work.params) == (skill, params)
    assert work.on_success == work.on_fail == recovery.id
    assert recovery.skill == "return_home"
    with pytest.raises(AutonomyContractError, match="requires the X1 simulation planning capability"):
        decode_decision(wire, capabilities=frozenset({"gimbal_control"}))


@pytest.mark.parametrize("route_ref", ["route:", "yard", "poi:yard", "route:" + "a" * 91, ["poi:a", "poi:b"]])
def test_search_leg_rejects_invalid_route_references(route_ref):
    with pytest.raises(AutonomyContractError, match=r"params\.route_ref"):
        validate_mission_plan(_plan(_SEARCH[0], {"route_ref": route_ref}), capabilities=_CAPS)


@pytest.mark.parametrize("target_ref", ["poi:", "yard", "route:yard", [0, 0]])
def test_mc_reposition_rejects_invalid_targets(target_ref):
    params = {**_REPOSITION[1], "target_ref": target_ref}
    with pytest.raises(AutonomyContractError, match=r"params\.target_ref"):
        validate_mission_plan(_plan(_REPOSITION[0], params), capabilities=_CAPS)


def test_mc_reposition_uses_existing_agl_bounds_and_accepts_home():
    bounds = x1_param_schema("x1_agl")
    low, high = bounds["minimum"], bounds["maximum"]
    for altitude in (low, high):
        params = {"target_ref": "home", "altitude_agl_m": altitude}
        plan = validate_mission_plan(_plan(_REPOSITION[0], params), capabilities=_CAPS)
        assert plan.steps[0].params == params
    for altitude in (low - 1, high + 1, True, str(low), float("nan")):
        params = {**_REPOSITION[1], "altitude_agl_m": altitude}
        with pytest.raises(AutonomyContractError, match=r"params\.altitude_agl_m"):
            validate_mission_plan(_plan(_REPOSITION[0], params), capabilities=_CAPS)


@pytest.mark.parametrize(("skill", "params"), [_SEARCH, _REPOSITION])
def test_airborne_skills_require_exact_parameters(skill, params):
    for key in params:
        missing = {k: v for k, v in params.items() if k != key}
        with pytest.raises(AutonomyContractError, match=f"missing required field.*{key}"):
            validate_mission_plan(_plan(skill, missing), capabilities=_CAPS)
    with pytest.raises(AutonomyContractError, match="unknown field.*latitude"):
        validate_mission_plan(_plan(skill, {**params, "latitude": 0}), capabilities=_CAPS)


@pytest.fixture
def package():
    corridor = {"north_min_m": -850, "north_max_m": 1200, "east_min_m": -900, "east_max_m": 900}
    return load_mission_package({
        "mission_id": "x1-test", "task_text": "Observe the yard", "risk_level": "low",
        "link_loss_policy": {"action": "return_now"}, "home": [0, 0],
        "places": {"poi:yard": [250, 0]}, "zones": {}, "routes": {"route:survey": [[250, 0], [300, 20]]},
        "capabilities": sorted(_CAPS), "oversight_steps": [],
        "operating_area": {**corridor, "min_altitude_m": 15, "max_altitude_m": 145},
        "budget": {"time_s": 1000, "reserve_time_s": 120, "energy_wh": 74, "reserve_energy_wh": 14.8},
        "x1_planning": {"world": "rw_farm_iowa__clear_noon", "max_distance_from_home_m": 1200,
                        "corridor": corridor, "terrain_rel_home_m": [-5, 15]},
    }, mode="sim")


@pytest.mark.parametrize(("skill", "params"), [_SEARCH, _REPOSITION])
def test_airborne_skills_are_hidden_and_rejected_for_nominal_planning(skill, params, package):
    prompt = plan_prompt("Observe the yard", refs=package.refs, capabilities=_CAPS,
                         x1_context=x1_planner_context(package))
    assert skill not in prompt
    assert skill not in json.dumps(plan_json_schema(capabilities=_CAPS))
    plan = validate_mission_plan(_plan(skill, params, nominal=True), capabilities=_CAPS)
    findings = check_plan_against_package(plan, package, capabilities=_CAPS)
    assert [(f["code"], f["step_id"]) for f in findings] == [("X1_AIRBORNE_ONLY", "work")]
