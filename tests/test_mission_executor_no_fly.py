from __future__ import annotations

import os

import pytest

from multidetect.autonomy import mission_executor as executor_module
from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor
from multidetect.autonomy.mission_package import (
    MAX_NO_FLY_ZONES,
    check_plan_against_package,
    effective_no_fly_radius,
    load_mission_package,
)
from multidetect.autonomy.mission_plan import validate_mission_plan
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960)


def _factory(blocked=None):
    def make(_text, **overrides):
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda pose, yaw: ActionPrimitive.translate(1.0, 0.0, 0.0),
            segment_clear=blocked or (lambda pose, n, e: True),
            config=LoopConfig(**overrides),
        )

    return make


def _return_plan():
    return validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "return",
            "steps": [
                {
                    "id": "home",
                    "skill": "return_home",
                    "params": {},
                    "on_success": None,
                    "on_fail": None,
                }
            ],
            "report": [],
        }
    )


def _executor(*, zones=(((10.0, 0.0), 2.0),), area=None, config=None, blocked=None):
    package = None
    executor = MissionExecutor(
        _return_plan(),
        loop_factory=_factory(blocked),
        config=config,
        no_fly_zones=zones,
        mission_package=package,
    )
    executor.home = (0.0, 0.0)
    executor.operating_area = area or {
        "north_min_m": -30.0,
        "north_max_m": 30.0,
        "east_min_m": -30.0,
        "east_max_m": 30.0,
    }
    return executor


def test_effective_radius_is_shared_and_translate_and_move_reject_inflated_leg():
    assert effective_no_fly_radius(2.0) == 5.0
    executor = _executor()
    action = ActionPrimitive.translate(7.0, 0.0, 0.0)
    assert executor._translate_enters_no_fly((0.0, 0.0, -20.0), 0.0, action)
    run = executor._run
    assert run is not None
    assert executor._leg_enters_no_fly((0.0, 0.0), (7.0, 0.0))


def test_inflated_boundary_is_outside_but_strictly_inside_endpoint_lands():
    executor = _executor()
    assert not executor._leg_enters_no_fly((15.0, 0.0), (20.0, 0.0))
    assert executor._return_home_detour((14.999999, 0.0), (0.0, 0.0)) is None


def test_visibility_graph_is_built_once_for_three_return_home_replans(monkeypatch):
    calls = 0
    original = MissionExecutor._build_visibility_graph

    def counted(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(MissionExecutor, "_build_visibility_graph", counted)
    executor = _executor()
    assert calls == 1
    # The helper installs the area after construction, so the first call is
    # the one-time late-area build; subsequent replans must reuse it.
    executor._return_home_detour((20.0, 0.0), (0.0, 0.0))
    assert calls == 2
    for _ in range(3):
        assert executor._return_home_detour((20.0, 0.0), (0.0, 0.0))
    assert calls == 2


def test_package_preflight_uses_inflated_radius_and_zone_cap():
    zones = {
        "zone:yard": {"centre_ne": [10, 0], "radius_m": 2, "risk_tag": "no-fly"},
    }
    document = {
        "mission_id": "nf",
        "risk_level": "low",
        "link_loss_policy": {"action": "continue", "timeout_s": 30},
        "home": [0, 0],
        "places": {"poi:edge": [7, 0]},
        "zones": zones,
        "routes": {},
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
        "budget": {"time_s": 600, "reserve_time_s": 60, "energy_wh": 300, "reserve_energy_wh": 30},
    }
    plan = validate_mission_plan(
        {
            "plan_version": 1,
            "goal": "x",
            "steps": [
                {
                    "id": "g",
                    "skill": "goto",
                    "params": {"target_ref": "poi:edge"},
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
            ],
            "report": [],
        }
    )
    document["plan"] = {
        "plan_version": 1,
        "goal": "x",
        "steps": [
            {
                "id": "g",
                "skill": "goto",
                "params": {"target_ref": "poi:edge"},
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
        ],
        "report": [],
    }
    package = load_mission_package(document)
    violations = check_plan_against_package(plan, package)
    assert any(v["code"] == "NO_FLY_ZONE" for v in violations)

    for index in range(MAX_NO_FLY_ZONES + 1):
        zones[f"zone:nf{index}"] = {"centre_ne": [0, 0], "radius_m": 1, "risk_tag": "no-fly"}
    document["plan"] = {
        "plan_version": 1,
        "goal": "x",
        "steps": [
            {
                "id": "home",
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            }
        ],
        "report": [],
    }
    capped = load_mission_package(document)
    violations = check_plan_against_package(plan, capped)
    cap = next(v for v in violations if v["code"] == "NO_FLY_ZONE_LIMIT_EXCEEDED")
    assert f"{MAX_NO_FLY_ZONES + 2}" in cap["message_en"]
    assert str(MAX_NO_FLY_ZONES) in cap["message_en"]


def test_replan_timeout_holds_then_lands_only_after_consecutive_failures(monkeypatch):
    executor = _executor(config=ExecutorConfig(detour_max_replan_failures=3))
    ticks = iter(range(1000))
    monkeypatch.setattr(executor_module.time, "perf_counter", lambda: next(ticks) * 0.1)
    run = executor._run
    assert run is not None
    first = executor._move_to(run, (20.0, 0.0, -20.0), 180.0, (0.0, 0.0))
    assert first.kind == "hold" and run.detour_replan_failures == 1
    second = executor._move_to(run, (20.0, 0.0, -20.0), 180.0, (0.0, 0.0))
    assert second.kind == "hold" and run.detour_replan_failures == 2
    third = executor._move_to(run, (20.0, 0.0, -20.0), 180.0, (0.0, 0.0))
    assert third == "land_fallback" and run.detour_replan_failures == 3
    assert executor.alerts[-1].endswith("no_fly_detour_timeout_land_fallback")


def test_each_detour_leg_checks_segment_clear_and_obstacle_holds():
    executor = _executor(blocked=lambda pose, n, e: False)
    run = executor._run
    assert run is not None
    run.detour_planned = True
    run.detour_waypoints = [(15.0, 5.0)]
    action = executor._move_to(run, (20.0, 0.0, -20.0), 135.0, (0.0, 0.0))
    assert action.kind == "hold"
    assert run.blocked == 1


def test_tangent_circle_and_area_boundary_never_produce_invalid_detour():
    executor = _executor(
        zones=(((10.0, 5.0), 2.0),),
        area={"north_min_m": 0.0, "north_max_m": 20.0, "east_min_m": -8.0, "east_max_m": 8.0},
    )
    path = executor._return_home_detour((20.0, 0.0), (0.0, 0.0))
    if path is not None:
        points = [(20.0, 0.0), *path, (0.0, 0.0)]
        assert all(executor._point_inside_area(p, executor.operating_area) for p in points)
        assert all(
            not executor._leg_enters_no_fly(a, b) for a, b in zip(points, points[1:], strict=False)
        )


@pytest.mark.timing
def test_timing_64_non_overlapping_circles_replan_under_50ms():
    if not os.environ.get("RUN_TIMING"):
        pytest.skip("timing benchmark is opt-in; onboard gate excludes timing tests")
    zones = tuple((((i % 8) * 65.0 + 20.0, (i // 8) * 65.0 + 20.0), 5.0) for i in range(64))
    executor = _executor(
        zones=zones,
        area={"north_min_m": 0.0, "north_max_m": 600.0, "east_min_m": 0.0, "east_max_m": 600.0},
    )
    result = executor._return_home_detour((590.0, 590.0), (5.0, 5.0))
    assert result is not executor_module._DETOUR_TIMEOUT
    assert executor._visibility_graph_build_ms >= 0.0
