# ruff: noqa
import json

import pytest

from multidetect.autonomy.mission_plan import (
    MISSION_PLAN_JSON_SCHEMA,
    validate_mission_plan,
)
from multidetect.autonomy.serialization import AutonomyContractError


def plan(goal, steps, report=("photos",)):
    return {"plan_version": 1, "goal": goal, "steps": steps, "report": list(report)}


def step(i, skill, params, success=None, fail=None):
    if fail is None and success is not None:
        fail = success
    return {"id": i, "skill": skill, "params": params, "on_success": success, "on_fail": fail}


VALID = [
    plan("Find any red vehicle near the gate", [step("s1", "area_search", {"region": "zone:A", "query": "red vehicle"}, "s2"), step("s2", "return_home", {}, None, None)]),
    plan("Count bicycles in the north lot", [step("s1", "count", {"region": "zone:north", "query": "bicycle", "max_count": 20}, "s2"), step("s2", "return_home", {}, None, None)], ("count",)),
    plan("Inspect the gate for damage and photograph it", [step("s1", "inspect", {"target_ref": "poi:gate", "query": "gate damage", "views": 4, "standoff_m": 8}, "s2"), step("s2", "land", {}, None, None)], ("photos", "states")),
    plan("Patrol the perimeter three times and alert on smoke", [step("s1", "patrol", {"route_ref": "route:perimeter", "query": "smoke", "loops": 3}, "s2"), step("s2", "return_home", {}, None, None)], ("alerts",)),
    plan("Survey the lawn with overlapping images", [step("s1", "survey", {"region": "zone:lawn", "overlap": 0.7}, "s2"), step("s2", "return_home", {}, None, None)], ("photos", "positions")),
    plan("Follow the moving white truck for one minute", [step("s1", "follow", {"track_ref": "track:7", "query": "white truck", "duration_s": 60}, "s2"), step("s2", "return_home", {}, None, None)]),
    plan("Watch the loading dock for smoke for five minutes", [step("s1", "monitor", {"region": "zone:dock", "query": "smoke", "duration_s": 300}, "s2"), step("s2", "return_home", {}, None, None)], ("alerts", "photos")),
    plan("Go to gate, inspect it, then come home", [step("s1", "goto", {"target_ref": "poi:gate"}, "s2"), step("s2", "inspect", {"target_ref": "poi:gate", "query": "door open", "views": 3, "standoff_m": 6}, "s3"), step("s3", "return_home", {}, None, None)], ("states", "photos")),
]


@pytest.mark.parametrize("document", VALID)
def test_valid_mission_kinds(document):
    parsed = validate_mission_plan(json.dumps(document), capabilities=frozenset())
    assert parsed.plan_version == 1


def test_deliver_requires_capability():
    document = plan("Deliver payload to the pad", [step("s1", "deliver", {"target_ref": "poi:pad", "payload_ref": "payload:camera"}, "s2"), step("s2", "land", {}, None, None)])
    with pytest.raises(AutonomyContractError, match="payload capability"):
        validate_mission_plan(document)
    assert validate_mission_plan(document, capabilities=frozenset({"payload"})).steps[0].skill == "deliver"


@pytest.mark.parametrize("mutator, message", [
    (lambda p: p["steps"][0].update({"extra": 1}), "unknown"),
    (lambda p: p["steps"].append(p["steps"][0].copy()), "duplicate"),
    (lambda p: p["steps"][0].update({"skill": "bogus"}), "unknown skill"),
    (lambda p: p["steps"][0]["params"].update({"bad": 1}), "unknown"),
    (lambda p: p["steps"][0]["params"].update({"query": "one two three four five six seven eight nine"}), "8 words"),
    (lambda p: p["steps"][0].update({"on_success": "missing"}), "references unknown"),
    (lambda p: p["steps"][0].update({"on_success": "s1"}), "cycle"),
    (lambda p: p["steps"][-1].update({"skill": "area_search", "params": {"region": "zone:A", "query": "car"}}), "terminal"),
    (lambda p: p["steps"][-1].update({"on_success": "s1"}), "terminal step"),
])
def test_rejection_rules(mutator, message):
    import copy
    document = copy.deepcopy(VALID[0]); mutator(document)
    with pytest.raises(AutonomyContractError, match=message):
        validate_mission_plan(document)


def test_duplicate_nan_and_bad_params_rejected():
    with pytest.raises(AutonomyContractError, match="duplicate"):
        validate_mission_plan('{"plan_version":1,"plan_version":1,"goal":"x","steps":[],"report":[]}')
    bad = plan("x", [step("s1", "hover", {"duration_s": float("nan")}, None, None)])
    with pytest.raises(AutonomyContractError):
        validate_mission_plan(json.dumps(bad, allow_nan=True))


@pytest.mark.parametrize("region", ["poi:gate", "route:north", "finding:1", "track:2", "home"])
def test_region_must_be_a_zone_reference(region):
    document = plan(
        "search",
        [
            step("s1", "area_search", {"region": region, "query": "car"}, "s2", "s2"),
            step("s2", "return_home", {}, None, None),
        ],
    )
    with pytest.raises(AutonomyContractError, match="region must be a zone reference"):
        validate_mission_plan(document)


def test_inspect_min_views_seen_has_bounded_contract():
    document = plan(
        "inspect",
        [
            step(
                "s1",
                "inspect",
                {"target_ref": "poi:gate", "query": "gate", "views": 4, "min_views_seen": 2, "standoff_m": 8},
                "s2",
                "s2",
            ),
            step("s2", "land", {}, None, None),
        ],
    )
    parsed = validate_mission_plan(document)
    assert parsed.steps[0].params["min_views_seen"] == 2
    document["steps"][0]["params"]["min_views_seen"] = 25
    with pytest.raises(AutonomyContractError):
        validate_mission_plan(document)


def test_nonterminal_null_edge_is_rejected():
    import copy
    document = copy.deepcopy(VALID[0])
    document["steps"][0]["on_fail"] = None
    with pytest.raises(AutonomyContractError, match="must reach a terminal"):
        validate_mission_plan(document)


def test_schema_is_json_and_structurally_valid():
    json.dumps(MISSION_PLAN_JSON_SCHEMA, allow_nan=False)
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(MISSION_PLAN_JSON_SCHEMA)
    for document in VALID:
        validator.validate(document)

