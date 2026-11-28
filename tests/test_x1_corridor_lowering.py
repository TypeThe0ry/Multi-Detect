"""Step-4 corridor lowering tests (DESIGN-corridor.md B7-B15, C19 slice part)."""

from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.mission_package import CorridorBounds, load_mission_package
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep
from multidetect.autonomy.motion_intent import MotionCommand, resolve_airframe_profile
from multidetect.autonomy.x1_corridor import (
    CORRIDOR_TUBE_HALF_WIDTH_M,
    CorridorSlice,
    build_x1_corridor_steps,
    slice_leg,
)
from multidetect.autonomy.x1_mission import (
    FW_PASS_CROSS_M,
    X1_PROFILE_ID,
    X1MissionStep,
    X1PlanError,
    build_x1_mission_steps,
)
from multidetect.autonomy.x1_motion_proof import X1MotionProofBuilder
from multidetect.autonomy.x1_plan import corridor_primitives_input
from multidetect.terrain_dem import LocalDemGrid

ROOT = Path(__file__).resolve().parents[1]
CORRIDOR = CorridorBounds.parse(
    json.loads((ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json")
               .read_text(encoding="utf-8"))["assumed_sim_corridor"])
HOME = (0.0, 0.0)


@pytest.fixture
def terrain():
    return LocalDemGrid(
        scenario_id="x1-corridor", origin_latitude_deg=52.0, origin_longitude_deg=5.0,
        origin_elevation_m=100.0, north_origin_m=-3000.0, east_origin_m=-3000.0,
        north_spacing_m=100.0, east_spacing_m=100.0,
        elevations_m=((100.0,) * 61,) * 61, vertical_sigma_m=0.1,
        slope_sigma_m_per_m=0.0, source_sha256="0" * 64,
    )


@pytest.fixture
def profile():
    return resolve_airframe_profile(X1_PROFILE_ID)


def _raise_vertex(terrain, north, east, elevation):
    rows = [list(row) for row in terrain.elevations_m]
    row = round((north - terrain.north_origin_m) / terrain.north_spacing_m)
    column = round((east - terrain.east_origin_m) / terrain.east_spacing_m)
    rows[row][column] = elevation
    return replace(terrain, elevations_m=tuple(tuple(row) for row in rows))


def _package(places, *, corridor=CORRIDOR, area=(-3500.0, 3500.0), rect=(-3000.0, 3000.0),
             zones=None, budget=None, terrain_bounds=(0.0, 10.0), max_distance=4000.0):
    document = {
        "mission_id": "x1-corridor-lowering", "task_text": "fly the corridor",
        "risk_level": "low", "link_loss_policy": {"action": "return_now"},
        "home": [0.0, 0.0], "places": places, "zones": zones or {}, "routes": {},
        "ref_labels": {ref: ref for ref in places},
        "capabilities": ["airframe:x1", "gimbal_control"], "oversight_steps": [],
        "operating_area": {
            "north_min_m": area[0], "north_max_m": area[1],
            "east_min_m": area[0], "east_max_m": area[1],
            "min_altitude_m": 15.0, "max_altitude_m": 145.0,
        },
        "budget": budget or {
            "time_s": 2400.0, "reserve_time_s": 240.0,
            "energy_wh": 200.0, "reserve_energy_wh": 14.8,
        },
        "x1_planning": {
            "world": "rw_farm_iowa_w6km__clear_noon",
            "max_distance_from_home_m": max_distance,
            "corridor": {
                "north_min_m": rect[0], "north_max_m": rect[1],
                "east_min_m": rect[0], "east_max_m": rect[1],
            },
            "terrain_rel_home_m": list(terrain_bounds),
        },
    }
    return load_mission_package(
        document, mode="sim", allow_assumed_sim=True,
        assumed_sim_max_distance_m=max_distance, assumed_sim_corridor=corridor)


def _plan(refs, *, agl=60.0):
    steps = [
        MissionStep("takeoff", "vtol_takeoff", {"altitude_agl_m": agl}, "fw", "home"),
        MissionStep("fw", "fw_transition", {}, "w0", "home"),
    ]
    for index, ref in enumerate(refs):
        success = f"w{index + 1}" if index + 1 < len(refs) else "back"
        steps.append(MissionStep(
            f"w{index}", "fw_goto_waypoint",
            {"target_ref": ref, "altitude_agl_m": agl}, success, "home"))
    steps.append(MissionStep("back", "back_transition", {}, "home", "home"))
    steps.append(MissionStep("home", "return_home", {}, None, None))
    return MissionPlan(1, "fly the corridor", tuple(steps), ())


def _build(package, refs, terrain, profile, **kwargs):
    return build_x1_corridor_steps(
        package, plan=_plan(refs, **kwargs), terrain=terrain, profile=profile,
        mission_radius_m=kwargs.pop("mission_radius_m", 4000.0))


def _pattern(n):
    return ("vtol_takeoff", "fw_transition") + ("fw_goto_waypoint",) * n + (
        "back_transition", "mc_return_to_home", "vtol_land")


@pytest.mark.parametrize(("n", "refs"), [
    (2, ["poi:a", "home"]),
    (3, ["poi:a", "poi:b", "home"]),
    (4, ["poi:a", "poi:b", "poi:c", "home"]),
])
def test_pattern_and_indices(n, refs, terrain, profile):
    places = {"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0], "poi:c": [0.0, 600.0]}
    lowering = _build(_package(places), refs, terrain, profile)

    assert lowering.pattern == _pattern(n)
    assert lowering.fw_leg_count == n
    assert lowering.command_indices == (None, 0, *range(1, n + 1), n + 1, n + 2, None)
    assert [leg.index for leg in lowering.legs] == list(range(1, n + 1))
    assert [leg.source_step_id for leg in lowering.legs] == [f"w{i}" for i in range(n)]
    assert lowering.legs[0].kind == "out"
    assert lowering.legs[-1].kind == "return"
    assert lowering.gate_ne is None
    assert lowering.pattern[-3:] == ("back_transition", "mc_return_to_home", "vtol_land")
    assert all(step.primitive != "vtol_land" or step.command is None for step in lowering.steps)
    assert lowering.steps[0].takeoff_height_m == pytest.approx(
        terrain.sample_local(*places["poi:a"]).elevation_m + 60.0
        - terrain.sample_local(*HOME).elevation_m)


def test_gate_synthesis_and_default_is_off(terrain, profile):
    places = {"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]}
    refs = ["poi:a", "poi:b", "home"]
    plain = _build(_package(places), refs, terrain, profile)
    assert plain.standoff_gate_enabled is False
    assert plain.gate_ne is None
    assert plain.legs[-1].end_ne == HOME

    gated = _build(
        _package(places, corridor=replace(CORRIDOR, standoff_gate_enabled=True)),
        refs, terrain, profile)
    previous = (800.0, 600.0)
    unit = (previous[0] / math.dist(previous, HOME), previous[1] / math.dist(previous, HOME))
    expected = (HOME[0] + CORRIDOR.standoff_m * unit[0],
                HOME[1] + CORRIDOR.standoff_m * unit[1])
    assert gated.standoff_gate_enabled is True
    assert gated.gate_ne == pytest.approx(expected)
    assert gated.legs[-1].end_ne == pytest.approx(expected)
    assert gated.legs[-1].kind == "return"
    gate_steps = [step for step in gated.steps
                  if step.command is not None and step.command.metadata.get("corridor_gate")]
    assert len(gate_steps) == 1
    assert gate_steps[0].source_step_id == "w2"


def test_gate_return_too_short(terrain, profile):
    places = {"poi:a": [300.0, 0.0]}
    gated = _package(places, corridor=replace(CORRIDOR, standoff_gate_enabled=True))
    with pytest.raises(X1PlanError, match="^CORRIDOR_RETURN_TOO_SHORT:"):
        _build(gated, ["poi:a", "home"], terrain, profile)

    places = {"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]}
    gated = _package(places, corridor=replace(CORRIDOR, standoff_gate_enabled=True))
    with pytest.raises(X1PlanError, match="^CORRIDOR_RETURN_TOO_SHORT:"):
        _build(gated, ["poi:a", "poi:b"], terrain, profile)


def test_only_one_middle_leg_outside_the_fence_reports_that_leg(terrain, profile):
    places = {"poi:a": [400.0, 0.0], "poi:b": [1100.0, 800.0]}
    package = _package(places, rect=(-1000.0, 1000.0))
    with pytest.raises(X1PlanError, match="^RUNOUT_OUTSIDE_FENCE:") as error:
        _build(package, ["poi:a", "poi:b", "home"], terrain, profile)
    assert "leg 2" in str(error.value)


def test_only_one_middle_leg_through_a_no_fly_zone(terrain, profile):
    places = {"poi:a": [400.0, 0.0], "poi:b": [800.0, 0.0]}
    zones = {"zone:restricted": {"centre_ne": [600.0, 0.0], "radius_m": 20.0,
                                 "risk_tag": "no_fly"}}
    with pytest.raises(X1PlanError, match="^NO_FLY_ZONE:") as error:
        _build(_package(places, zones=zones), ["poi:a", "poi:b", "home"], terrain, profile)
    assert "leg 2" in str(error.value)


def test_only_one_leg_has_agl_out_of_band(terrain, profile):
    places = {"poi:a": [600.0, 0.0], "poi:b": [600.0, 600.0], "poi:c": [0.0, 600.0]}
    terrain = _raise_vertex(terrain, 300.0, 600.0, 150.0)
    with pytest.raises(X1PlanError, match="^CORRIDOR_AGL:") as error:
        _build(_package(places), ["poi:a", "poi:b", "poi:c", "home"], terrain, profile)
    assert "leg 3" in str(error.value)


def test_leg_count_over_corridor_max(terrain, profile):
    places = {"poi:a": [600.0, 0.0], "poi:b": [600.0, 600.0]}
    package = _package(places, corridor=replace(CORRIDOR, max_fw_legs=2))
    with pytest.raises(X1PlanError, match="^CORRIDOR_LEGS:"):
        _build(package, ["poi:a", "poi:b", "home"], terrain, profile)


def test_short_leg_is_rejected(terrain, profile):
    places = {"poi:a": [500.0, 0.0], "poi:b": [540.0, 0.0]}
    with pytest.raises(X1PlanError, match="^FW_LEG_TOO_SHORT:"):
        _build(_package(places), ["poi:a", "poi:b", "home"], terrain, profile)


def test_nine_step_plan_is_rejected(terrain, profile):
    places = {f"poi:{i}": [300.0 * i, 0.0] for i in range(1, 5)}
    with pytest.raises(X1PlanError, match="^UNSUPPORTED_PLAN:"):
        _build(_package(places), [*places, "home"], terrain, profile)


def test_dogleg_budget_uses_path_length_not_displacement(terrain, profile):
    budget = {"time_s": 400.0, "reserve_time_s": 10.0,
              "energy_wh": 200.0, "reserve_energy_wh": 14.8}
    straight = _package({"poi:a": [800.0, 800.0]}, budget=budget)
    _build(straight, ["poi:a", "home"], terrain, profile)

    dogleg = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 800.0]}, budget=budget)
    with pytest.raises(X1PlanError, match="^TIME_BUDGET:"):
        _build(dogleg, ["poi:a", "poi:b", "home"], terrain, profile)


def test_single_waypoint_lowering_golden(terrain, profile):
    """build_x1_mission_steps output stays field-for-field identical to the base."""
    document = {
        "mission_id": "x1-golden", "home": [0.0, 0.0],
        "places": {"poi:beacon": [0.0, 400.0]}, "zones": {}, "routes": {},
        "task_text": "Fly to the beacon at 80 m, then return home.",
        "operating_area": {
            "north_min_m": -600.0, "north_max_m": 1400.0,
            "east_min_m": -600.0, "east_max_m": 1400.0,
            "min_altitude_m": 20.0, "max_altitude_m": 120.0,
        },
        "budget": {"time_s": 1500, "reserve_time_s": 50,
                   "energy_wh": 65, "reserve_energy_wh": 10},
        "risk_level": "low", "link_loss_policy": {"action": "return_now", "timeout_s": 20},
        "oversight_steps": [],
        "plan": {
            "plan_version": 1, "goal": "Visit beacon then return home", "report": [],
            "steps": [
                {"id": "out", "skill": "goto",
                 "params": {"target_ref": "poi:beacon", "altitude_m": 80},
                 "on_success": "finish", "on_fail": "finish"},
                {"id": "finish", "skill": "return_home", "params": {},
                 "on_success": None, "on_fail": None},
            ],
        },
    }
    package = load_mission_package(document)
    steps = build_x1_mission_steps(
        package, terrain=terrain, profile=profile, mission_radius_m=1250)

    expected = (
        X1MissionStep("vtol_takeoff", "out", takeoff_height_m=80.0),
        X1MissionStep("fw_transition", "out", MotionCommand("transition", mode="FW"),
                      required_mc_heading_deg=90.0),
        X1MissionStep("fw_goto_waypoint", "out", MotionCommand(
            "reposition", target_ned=(0.0, 400.0, -80.0), speed_mps=15.0, mode="FW",
            metadata={"long_range_home_relative": True},
            fly_through_radius_m=FW_PASS_CROSS_M)),
        X1MissionStep("back_transition", "finish",
                      MotionCommand("transition", mode="MC", reason="back_transition")),
        X1MissionStep("mc_return_to_home", "finish", MotionCommand(
            "mc_reposition", target_ned=(0.0, 0.0, -80.0), speed_mps=8.0,
            mode="MC", reason="hover_return_to_home")),
        X1MissionStep("vtol_land", "finish"),
    )
    assert steps == expected


def _corner_distance(slice_: CorridorSlice, start, end) -> float:
    n0, n1, e0, e1, *_ = slice_.box()
    return max(
        _point_segment_distance(start[:2], end[:2], corner)
        for corner in ((n0, e0), (n0, e1), (n1, e0), (n1, e1)))


def _point_segment_distance(start, end, point) -> float:
    dn, de = end[0] - start[0], end[1] - start[1]
    length2 = dn * dn + de * de
    if not length2:
        return math.dist(start, point)
    t = min(1.0, max(0.0, ((point[0] - start[0]) * dn
                           + (point[1] - start[1]) * de) / length2))
    return math.dist(point, (start[0] + t * dn, start[1] + t * de))


def test_slice_leg_properties_random_headings():
    rng = random.Random(20261128)
    for _ in range(80):
        length = rng.uniform(10.0, 1000.0)
        heading = rng.uniform(0.0, 360.0)
        start = (rng.uniform(-500.0, 500.0), rng.uniform(-500.0, 500.0), 160.0)
        end = (start[0] + length * math.cos(math.radians(heading)),
               start[1] + length * math.sin(math.radians(heading)), 160.0)
        duration = length / 12.0
        slices = slice_leg(start, end, 19.0, 0.0, duration, 500.0, 100.0,
                           max_slices=CORRIDOR.max_sweep_legs)

        assert slices[-1].command_end is True
        assert all(not item.command_end for item in slices[:-1])
        assert sum(item.duration_s for item in slices) == pytest.approx(duration, abs=1e-9)
        assert len(slices) <= CORRIDOR.max_sweep_legs
        for item in slices:
            assert _corner_distance(item, start, end) <= CORRIDOR_TUBE_HALF_WIDTH_M + 1e-6
        assert slices[0].start == pytest.approx(start)
        assert slices[-1].end == pytest.approx(end)


def test_slice_cap_is_enforced():
    with pytest.raises(X1PlanError, match="^CORRIDOR_SLICE_CAP:"):
        slice_leg((0.0, 0.0, 160.0), (1000.0, 0.0, 160.0), 19.0, 0.0,
                  1000.0 / 12.0, 500.0, 10.0, max_slices=5)


def test_corridor_primitives_input_lowers_with_execution_inputs(terrain, profile):
    places = {"poi:a": [300.0, 0.0], "poi:b": [300.0, 200.0]}
    package = _package(places)
    plan = _plan(["poi:a", "poi:b", "home"])
    capabilities = frozenset(package.document["capabilities"])
    lowering = corridor_primitives_input(
        plan, package, capabilities, terrain=terrain, profile=profile,
        mission_radius_m=4000.0)
    assert lowering.pattern == _pattern(3)
    assert lowering.fw_leg_count == 3


def test_corridor_lowering_is_not_accepted_by_the_single_waypoint_builder(terrain, profile):
    places = {"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]}
    package = _package(places)
    lowering = _build(package, ["poi:a", "poi:b", "home"], terrain, profile)
    with pytest.raises(ValueError, match="one-waypoint lowering"):
        X1MotionProofBuilder(
            package, profile, terrain, lowering.steps, mission_radius_m=4000.0)
