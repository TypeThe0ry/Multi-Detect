from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from multidetect.autonomy.mission_executor import ExecutorConfig
from multidetect.autonomy.mission_package import (
    MissionPackage,
    check_plan_against_package,
    estimate_plan_duration,
    load_mission_package,
)
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep, validate_mission_plan


def _document() -> dict:
    return {
        "mission_id": "preflight",
        "risk_level": "low",
        "link_loss_policy": {"action": "continue", "timeout_s": 30},
        "home": [0, 0],
        "places": {"poi:gate": [20, 0]},
        "zones": {"zone:yard": {"centre_ne": [10, 0], "radius_m": 4}},
        "routes": {"route:yard": [[0, 0], [10, 0], [20, 0]]},
        "oversight_steps": [],
        "heartbeat_timeout_s": 5,
        "operating_area": {
            "north_min_m": -30,
            "north_max_m": 30,
            "east_min_m": -30,
            "east_max_m": 30,
            "min_altitude_m": 5,
            "max_altitude_m": 60,
        },
        "budget": {
            "time_s": 600,
            "reserve_time_s": 60,
            "energy_wh": 300,
            "reserve_energy_wh": 30,
        },
    }


def _plan(steps: list[dict]) -> dict:
    return {
        "plan_version": 1,
        "goal": "preflight test",
        "steps": steps,
        "report": ["status"],
    }


def _package(plan: dict | None = None, *, raw: dict | None = None) -> MissionPackage:
    document = deepcopy(raw or _document())
    if plan is not None:
        document["plan"] = plan
    return load_mission_package(document)


def _goto_home() -> list[dict]:
    return [
        {
            "id": "go",
            "skill": "goto",
            "params": {"target_ref": "poi:gate"},
            "on_success": "home",
            "on_fail": "home",
        },
        {
            "id": "home",
            "skill": "return_home",
            "params": {},
            "on_success": None,
            "on_fail": None,
        },
    ]


def test_clean_plan_passes_package_preflight() -> None:
    package = _package(_plan(_goto_home()))
    assert check_plan_against_package(package.plan, package) == []


@pytest.mark.parametrize(
    ("code", "step"),
    [
        (
            "OUTSIDE_OPERATING_AREA",
            {
                "id": "go",
                "skill": "goto",
                "params": {"target_ref": "poi:outside"},
                "on_success": "home",
                "on_fail": "home",
            },
        ),
        (
            "NO_FLY_ZONE",
            {
                "id": "search",
                "skill": "area_search",
                "params": {"region": "zone:yard", "query": "car"},
                "on_success": "home",
                "on_fail": "home",
            },
        ),
        (
            "ALTITUDE_OUT_OF_BOUNDS",
            {
                "id": "go",
                "skill": "goto",
                "params": {"target_ref": "poi:gate", "altitude_m": 100},
                "on_success": "home",
                "on_fail": "home",
            },
        ),
    ],
)
def test_geometric_and_altitude_violations_have_bilingual_messages(code, step) -> None:
    raw = _document()
    if code == "OUTSIDE_OPERATING_AREA":
        raw["task_text"] = "test"
        package = _package(raw=raw)
        package = MissionPackage(package.document, {**package.refs, "poi:outside": (100, 0)}, None)
    else:
        if code == "NO_FLY_ZONE":
            raw["zones"]["zone:yard"]["risk_tag"] = "no-fly"
        raw["task_text"] = "test"
        package = _package(raw=raw)
    plan = validate_mission_plan(_plan([step, _goto_home()[1]]))
    violations = check_plan_against_package(plan, package)
    item = next(v for v in violations if v["code"] == code)
    assert item["step_id"] == step["id"]
    assert item["message_zh"] and item["message_en"]


def test_route_and_no_fly_segment_are_checked() -> None:
    raw = _document()
    raw["zones"]["zone:closed"] = {
        "centre_ne": [10, 0],
        "radius_m": 2,
        "risk_tag": "no-fly",
    }
    raw["routes"]["route:cross"] = [[-20, 0], [20, 0]]
    raw["task_text"] = "test"
    package = _package(raw=raw)
    package = MissionPackage(
        package.document,
        {**package.refs, "route:cross": ((-20, 0), (20, 0))},
        None,
    )
    plan = validate_mission_plan(
        _plan(
            [
                {
                    "id": "patrol",
                    "skill": "patrol",
                    "params": {"route_ref": "route:cross", "query": "car", "loops": 1},
                    "on_success": "home",
                    "on_fail": "home",
                },
                _goto_home()[1],
            ]
        )
    )
    codes = {v["code"] for v in check_plan_against_package(plan, package)}
    assert "NO_FLY_ZONE" in codes


def test_actual_home_to_target_leg_is_checked_for_no_fly_crossing() -> None:
    raw = _document()
    raw["zones"]["zone:closed"] = {
        "centre_ne": [10, 0], "radius_m": 2, "risk_tag": "no-fly"
    }
    raw["task_text"] = "test"
    package = _package(raw=raw)
    plan = validate_mission_plan(_plan(_goto_home()))
    codes = {v["code"] for v in check_plan_against_package(plan, package)}
    assert "NO_FLY_ZONE" in codes


def test_reserve_return_admits_open_ended_monitor_plan() -> None:
    raw = _document()
    raw["budget"] = {
        "time_s": 150, "reserve_time_s": 30,
        "energy_wh": 300, "reserve_energy_wh": 30,
    }
    raw["task_text"] = "monitor until reserve return"
    package = _package(raw=raw)
    plan = validate_mission_plan(
        _plan([
            {
                "id": "watch",
                "skill": "monitor",
                "params": {"region": "zone:yard", "query": "car", "duration_s": 900},
                "on_success": "home", "on_fail": "home",
            },
            _goto_home()[1],
        ])
    )
    assert check_plan_against_package(plan, package) == []


def test_patrol_estimate_tracks_final_waypoint_for_return_home() -> None:
    raw = _document()
    raw["task_text"] = "patrol and return"
    package = _package(raw=raw)
    plan = validate_mission_plan(
        _plan([
            {
                "id": "patrol",
                "skill": "patrol",
                "params": {"route_ref": "route:yard", "query": "fence", "loops": 1},
                "on_success": "home", "on_fail": "home",
            },
            _goto_home()[1],
        ])
    )
    estimate = estimate_plan_duration(plan, package, executor_config=ExecutorConfig())
    assert estimate > 2 * (20 / 0.75)


def test_long_return_leg_uses_conservative_calibrated_speed() -> None:
    raw = _document()
    raw["places"]["poi:far"] = [150, 0]
    raw["operating_area"].update(north_max_m=200)
    raw["task_text"] = "long return"
    raw["budget"] = {
        "time_s": 120, "reserve_time_s": 60,
        "energy_wh": 1000, "reserve_energy_wh": 30,
    }
    package = _package(raw=raw)
    package = MissionPackage(package.document, {**package.refs, "poi:far": (150.0, 0.0)}, None)
    plan = validate_mission_plan(_plan([
        {"id": "go", "skill": "goto", "params": {"target_ref": "poi:far"},
         "on_success": "home", "on_fail": "home"},
        _goto_home()[1],
    ]))
    assert any(
        v["code"] == "TIME_BUDGET_EXCEEDED"
        for v in check_plan_against_package(plan, package)
    )


def test_energy_budget_admission_refuses_mandatory_transit() -> None:
    raw = _document()
    raw["places"]["poi:far"] = [150, 0]
    raw["operating_area"].update(north_max_m=200)
    raw["task_text"] = "energy return"
    raw["budget"] = {
        "time_s": 1000, "reserve_time_s": 60,
        "energy_wh": 10, "reserve_energy_wh": 5,
    }
    package = _package(raw=raw)
    package = MissionPackage(package.document, {**package.refs, "poi:far": (150.0, 0.0)}, None)
    plan = validate_mission_plan(_plan([
        {"id": "go", "skill": "goto", "params": {"target_ref": "poi:far"},
         "on_success": "home", "on_fail": "home"},
        _goto_home()[1],
    ]))
    assert any(
        v["code"] == "ENERGY_BUDGET_EXCEEDED"
        for v in check_plan_against_package(plan, package)
    )


def test_plan_eval_v3_common_valid_witnesses_are_admitted() -> None:
    source = json.loads(
        (Path(__file__).parents[1] / "configs" / "onboard" / "plan-eval-tasks-v3.json").read_text(
            encoding="utf-8"
        )
    )
    raw = _document()
    raw["budget"] = {
        "time_s": 100000, "reserve_time_s": 60,
        "energy_wh": 100000, "reserve_energy_wh": 30,
    }
    raw["places"] = {
        ref.split(" ", 1)[0]: [10, 10]
        for ref in source["refs"]
        if ref.startswith("poi:")
    }
    raw["zones"] = {
        ref.split(" ", 1)[0]: {"centre_ne": [10, 10], "radius_m": 5}
        for ref in source["refs"] if ref.startswith("zone:")
    }
    raw["routes"] = {
        ref.split(" ", 1)[0]: [[0, 0], [10, 10]]
        for ref in source["refs"] if ref.startswith("route:")
    }
    raw["task_text"] = "plan evaluation"
    package = _package(raw=raw)
    for task in source["tasks"]:
        if task.get("expect_invalid"):
            continue
        rows = task["expected_steps"]
        steps = []
        for index, row in enumerate(rows):
            next_id = f"s{index + 1}" if index + 1 < len(rows) else None
            steps.append({
                "id": f"s{index}", "skill": row["skill"], "params": row.get("params", {}),
                "on_success": next_id, "on_fail": next_id,
            })
        if rows[-1]["skill"] not in {"return_home", "land"}:
            steps[-1]["on_success"] = "home"
            steps[-1]["on_fail"] = "home"
            steps.append({"id": "home", "skill": "return_home", "params": {},
                          "on_success": None, "on_fail": None})
        plan = validate_mission_plan(_plan(steps), capabilities=frozenset({"payload"}))
        assert check_plan_against_package(plan, package) == [], task["text"]


def test_time_budget_capability_and_terminal_reachability() -> None:
    raw = _document()
    raw["budget"] = {"time_s": 10, "reserve_time_s": 5, "energy_wh": 300, "reserve_energy_wh": 30}
    raw["task_text"] = "test"
    package = _package(raw=raw)
    monitor = {
        "id": "watch", "skill": "goto",
        "params": {"target_ref": "poi:gate"},
        "on_success": "home", "on_fail": "home",
    }
    plan = MissionPlan(
        1,
        "preflight test",
        (MissionStep("watch", "goto", monitor["params"], "home", "home"),
         MissionStep("home", "return_home", {}, None, None)),
        ("status",),
    )
    codes = {v["code"] for v in check_plan_against_package(plan, package)}
    assert "TIME_BUDGET_EXCEEDED" in codes

    unreachable = MissionPlan(
        1,
        "preflight test",
        (MissionStep("go", "goto", {"target_ref": "poi:gate"}, "home", None),
         MissionStep("home", "return_home", {}, None, None)),
        ("status",),
    )
    assert any(
        v["code"] == "TERMINAL_UNREACHABLE"
        for v in check_plan_against_package(unreachable, package)
    )

    deliver = validate_mission_plan(
        _plan(
            [
                {
                    "id": "drop",
                    "skill": "deliver",
                    "params": {"target_ref": "poi:gate", "payload_ref": "poi:gate"},
                    "on_success": "home",
                    "on_fail": "home",
                },
                _goto_home()[1],
            ]
        ),
        capabilities=frozenset({"payload"}),
    )
    assert any(
        v["code"] == "MISSING_CAPABILITY"
        for v in check_plan_against_package(deliver, package)
    )


def test_every_reachable_branch_must_reach_return_or_land() -> None:
    raw = _document()
    raw["task_text"] = "test"
    package = _package(raw=raw)
    plan = MissionPlan(
        1,
        "preflight test",
        (MissionStep("go", "goto", {"target_ref": "poi:gate"}, "home", None),
         MissionStep("home", "return_home", {}, None, None)),
        ("status",),
    )
    violations = check_plan_against_package(plan, package)
    assert any(
        v["code"] == "TERMINAL_UNREACHABLE" and v["step_id"] == "go"
        for v in violations
    )
