"""CPU contract tests for model-plan lowering on the X1."""

import json
from dataclasses import replace

import pytest

from multidetect.autonomy.mission_package import check_plan_against_package, load_mission_package
from multidetect.autonomy.mission_planner import compile_mission_plan
from multidetect.autonomy.motion_intent import resolve_airframe_profile
from multidetect.autonomy.x1_mission import (
    X1_PROFILE_ID,
    X1PlanError,
    build_x1_mission_steps,
    x1_place_from_bearing,
    x1_place_from_wgs84,
)
from multidetect.target_geolocation import wgs84_forward_from_ned
from multidetect.terrain_dem import LocalDemGrid


@pytest.fixture
def terrain():
    return LocalDemGrid(
        scenario_id="x1-cpu", origin_latitude_deg=52.0, origin_longitude_deg=5.0,
        origin_elevation_m=100.0, north_origin_m=-600.0, east_origin_m=-600.0,
        north_spacing_m=100.0, east_spacing_m=100.0,
        elevations_m=((100.0,) * 21,) * 21, vertical_sigma_m=0.1,
        slope_sigma_m_per_m=0.0, source_sha256="0" * 64,
    )


@pytest.fixture
def profile():
    return resolve_airframe_profile(X1_PROFILE_ID)


def _plan(altitude=80, reference="poi:beacon"):
    return {
        "plan_version": 1, "goal": "Visit beacon then return home", "report": [],
        "steps": [
            {"id": "out", "skill": "goto", "params": {"target_ref": reference, "altitude_m": altitude},
             "on_success": "finish", "on_fail": "finish"},
            {"id": "finish", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
    }


def _package(*, target=(400, 0), home=(0, 0), plan=None, **overrides):
    document = {
        "mission_id": "x1-cpu", "home": home,
        "places": {"poi:beacon": target}, "zones": {}, "routes": {},
        "task_text": "Fly to the beacon at 80 m, then return home.",
        "operating_area": {
            "north_min_m": -600, "north_max_m": 1400, "east_min_m": -600, "east_max_m": 1400,
            "min_altitude_m": 20, "max_altitude_m": 120,
        },
        "budget": {"time_s": 1500, "reserve_time_s": 50, "energy_wh": 65, "reserve_energy_wh": 10},
        "risk_level": "low", "link_loss_policy": {"action": "return_now", "timeout_s": 20},
        "oversight_steps": [],
    }
    document.update(overrides)
    if plan is not None:
        document["plan"] = plan
    return load_mission_package(document)


def _build(package, terrain, profile, **kwargs):
    return build_x1_mission_steps(package, terrain=terrain, profile=profile,
                                  mission_radius_m=kwargs.pop("mission_radius_m", 1250), **kwargs)


def test_text_only_package_through_onboard_planner_and_native_commands(terrain, profile):
    home = (100.0, 100.0)
    package = _package(home=home, target=x1_place_from_bearing(home, 400, 90))

    def model_reply(prompt, schema):
        assert package.document["task_text"] in prompt
        assert "poi:beacon" in prompt
        return json.dumps(_plan())

    compiled, report = compile_mission_plan(
        model_reply, package.document["task_text"], refs=tuple(package.refs),
        airframe=profile, plan_checker=lambda plan: check_plan_against_package(plan, package),
    )
    assert report["error"] is None
    assert compiled is not None
    assert package.plan is None
    steps = _build(package, terrain, profile, plan=compiled)
    assert [step.primitive for step in steps] == [
        "vtol_takeoff", "fw_transition", "fw_goto_waypoint", "back_transition",
        "mc_return_to_home", "vtol_land",
    ]
    assert [step.source_step_id for step in steps] == ["out"] * 3 + ["finish"] * 3
    assert steps[0].takeoff_height_m == 80
    assert steps[1].required_mc_heading_deg == pytest.approx(90)
    commands = [step.command for step in steps if step.command is not None]
    assert [(cmd.kind, cmd.mode) for cmd in commands] == [
        ("transition", "FW"), ("reposition", "FW"), ("transition", "MC"), ("mc_reposition", "MC"),
    ]
    assert commands[1].target_ned == pytest.approx((0, 400, -80))
    assert commands[1].metadata["long_range_home_relative"] is True
    assert commands[1].fly_through_radius_m == 25
    assert commands[1].speed_mps == 15
    assert commands[-1].target_ned == (0, 0, -80)
    assert steps[-1].command is None  # Collector lands after settled return.


def _raise_vertex(terrain, north, east, elevation):
    rows = [list(row) for row in terrain.elevations_m]
    row = round((north - terrain.north_origin_m) / terrain.north_spacing_m)
    column = round((east - terrain.east_origin_m) / terrain.east_spacing_m)
    rows[row][column] = elevation
    return replace(terrain, elevations_m=tuple(tuple(row) for row in rows))


def test_wgs84_destination_uses_destination_agl_not_home_height(terrain, profile):
    lat, lon = wgs84_forward_from_ned(latitude_deg=52.0, longitude_deg=5.0,
                                      north_offset_m=400.0, east_offset_m=0.0)
    north, east = x1_place_from_wgs84(terrain, lat, lon)
    assert (north, east) == pytest.approx((400, 0), abs=0.01)
    elevated = _raise_vertex(terrain, 400, 0, 125)
    result = _build(_package(target=(north, east), plan=_plan(60)), elevated, profile)
    height = 60 + elevated.sample_local(north, east).elevation_m - elevated.origin_elevation_m
    assert height == pytest.approx(85, abs=0.005)
    assert result[0].takeoff_height_m == pytest.approx(height)
    assert result[2].command.target_ned == pytest.approx((north, east, -height))


@pytest.mark.parametrize(
    ("target", "altitude", "options", "code"),
    [
        ((49, 0), 80, {}, "FW_LEG_TOO_SHORT"),
        ((400, 0), 80, {"fw_min_leg_m": 401}, "FW_LEG_TOO_SHORT"),
        ((400, 0), 39, {}, "AGL_OUT_OF_BOUNDS"),
        ((400, 0), 80, {"mission_radius_m": 900}, "RUNOUT_OUTSIDE_FENCE"),
        ((1200, 0), 80, {"mission_radius_m": 2000}, "RUNOUT_OUTSIDE_FENCE"),
    ],
)
def test_rejects_airframe_leg_altitude_and_runout(target, altitude, options, code, terrain, profile):
    with pytest.raises(X1PlanError, match=f"^{code}:"):
        _build(_package(target=target, plan=_plan(altitude)), terrain, profile, **options)


def test_rejects_shallow_transition_above_home(terrain, profile):
    terrain = _raise_vertex(terrain, 400, 0, 49)
    with pytest.raises(X1PlanError, match="^TRANSITION_HEIGHT:"):
        _build(_package(plan=_plan(80)), terrain, profile)


@pytest.mark.parametrize("elevation", [40, 150])
def test_checks_terrain_in_runout_beyond_waypoint(elevation, terrain, profile):
    terrain = _raise_vertex(terrain, 800, 0, elevation)
    with pytest.raises(X1PlanError, match="^CORRIDOR_AGL:"):
        _build(_package(plan=_plan()), terrain, profile)


def test_requires_dem_coverage_in_runout(terrain, profile):
    terrain = replace(terrain, elevations_m=terrain.elevations_m[:13])
    with pytest.raises(X1PlanError, match="^DEM_COVERAGE:"):
        _build(_package(plan=_plan()), terrain, profile)


def test_checks_no_fly_clearance_at_sides_of_runout(terrain, profile):
    zone = {"centre_ne": [800, 60], "radius_m": 3, "risk_tag": "no_fly"}
    with pytest.raises(X1PlanError, match="^NO_FLY_ZONE:"):
        _build(_package(plan=_plan(), zones={"zone:restricted": zone}), terrain, profile)


@pytest.mark.parametrize(("budget_key", "limit", "code"), [("time_s", 100, "TIME_BUDGET"), ("energy_wh", 20, "ENERGY_BUDGET")])
def test_checks_budget_including_mc_return_and_reserves(budget_key, limit, code, terrain, profile):
    budget = {"time_s": 1500, "reserve_time_s": 50, "energy_wh": 65, "reserve_energy_wh": 10}
    budget[budget_key] = limit
    with pytest.raises(X1PlanError, match=f"^{code}:"):
        _build(_package(plan=_plan(), budget=budget), terrain, profile)


def test_lands_only_at_explicit_home(terrain, profile):
    plan = _plan()
    plan["steps"][1].update(skill="land", params={"target_ref": "home"})
    assert _build(_package(plan=plan), terrain, profile) == _build(_package(plan=_plan()), terrain, profile)
    for params in ({}, {"target_ref": "poi:beacon"}):
        plan["steps"][1]["params"] = params
        with pytest.raises(X1PlanError, match="^LANDING_SITE:"):
            _build(_package(plan=plan), terrain, profile)


def test_refuses_uncompiled_text_and_out_of_scope_graph(terrain, profile):
    with pytest.raises(X1PlanError, match="^PLAN_REQUIRED:"):
        _build(_package(), terrain, profile)
    plan = _plan()
    plan["steps"].insert(0, {
        "id": "hold", "skill": "hover", "params": {"duration_s": 2},
        "on_success": "out", "on_fail": "finish",
    })
    with pytest.raises(X1PlanError, match="^UNSUPPORTED_PLAN:"):
        _build(_package(plan=plan), terrain, profile)


def test_refuses_non_static_destination_and_oversight(terrain, profile):
    with pytest.raises(X1PlanError, match="^UNGROUNDED_DESTINATION:"):
        _build(_package(plan=_plan(reference="finding:1")), terrain, profile)
    with pytest.raises(X1PlanError, match="^OVERSIGHT_REQUIRED:"):
        _build(_package(plan=_plan(), oversight_steps=["out"]), terrain, profile)
