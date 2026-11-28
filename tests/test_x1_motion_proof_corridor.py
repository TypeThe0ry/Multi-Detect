"""Step-5 corridor motion-proof tests (DESIGN-corridor.md C16-C20, C24-C25)."""

from __future__ import annotations

import json
import math

from multidetect.autonomy.x1_plan import _segment_distance
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.mission_package import CorridorBounds, load_mission_package
from multidetect.autonomy.mission_plan import MissionPlan, MissionStep
from multidetect.autonomy.motion_intent import MotionCommand, resolve_airframe_profile
from multidetect.autonomy.x1_contingency_executor import Decision, RuntimeFrame
from multidetect.autonomy.x1_corridor import (
    CORRIDOR_TUBE_HALF_WIDTH_M,
    build_x1_corridor_steps,
    slice_leg,
)
from multidetect.autonomy.x1_mission import X1_PROFILE_ID, build_x1_mission_steps
from multidetect.autonomy.x1_motion_proof import (
    ExecutionSweep,
    LandingPad,
    SweptLeg,
    X1MotionProofBuilder,
)
from multidetect.terrain_dem import LocalDemGrid

ROOT = Path(__file__).resolve().parents[1]
CORRIDOR = CorridorBounds.parse(
    json.loads((ROOT / "configs/simulation/x1-assumed-sim-bounds-iowa-corridor.json")
               .read_text(encoding="utf-8"))["assumed_sim_corridor"])
HOME = (0.0, 0.0)


@pytest.fixture
def terrain():
    return LocalDemGrid(
        scenario_id="x1-corridor-proof", origin_latitude_deg=52.0, origin_longitude_deg=5.0,
        origin_elevation_m=100.0, north_origin_m=-3000.0, east_origin_m=-3000.0,
        north_spacing_m=100.0, east_spacing_m=100.0,
        elevations_m=((100.0,) * 61,) * 61, vertical_sigma_m=0.1,
        slope_sigma_m_per_m=0.0, source_sha256="0" * 64,
    )


@pytest.fixture
def profile():
    return resolve_airframe_profile(X1_PROFILE_ID)


def _package(places, *, corridor=CORRIDOR, rect=(-3000.0, 3000.0), area=(-3500.0, 3500.0),
             max_distance=4000.0, mode="sim"):
    document = {
        "mission_id": "x1-corridor-proof", "task_text": "fly the corridor",
        "risk_level": "low", "link_loss_policy": {"action": "return_now"},
        "home": [0.0, 0.0], "places": places, "zones": {}, "routes": {},
        "ref_labels": {ref: ref for ref in places},
        "capabilities": ["airframe:x1", "gimbal_control"], "oversight_steps": [],
        "operating_area": {
            "north_min_m": area[0], "north_max_m": area[1],
            "east_min_m": area[0], "east_max_m": area[1],
            "min_altitude_m": 15.0, "max_altitude_m": 145.0,
        },
        "budget": {"time_s": 2400.0, "reserve_time_s": 240.0,
                   "energy_wh": 200.0, "reserve_energy_wh": 14.8},
        "x1_planning": {
            "world": "rw_farm_iowa_w6km__clear_noon",
            "max_distance_from_home_m": max_distance,
            "corridor": {
                "north_min_m": rect[0], "north_max_m": rect[1],
                "east_min_m": rect[0], "east_max_m": rect[1],
            },
            "terrain_rel_home_m": [0.0, 10.0],
        },
    }
    return load_mission_package(
        document, mode=mode, allow_assumed_sim=True,
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


def _lowering(package, refs, terrain, profile):
    return build_x1_corridor_steps(
        package, plan=_plan(refs), terrain=terrain, profile=profile, mission_radius_m=4000.0)


def _builder(package, terrain, profile, lowering, **options):
    return X1MotionProofBuilder(
        package, profile, terrain, lowering.steps, mission_radius_m=4000.0,
        terrain_error_m=1.0, obstacle_clearance=lambda box: 25.0, corridor=lowering, **options)


def _decision(package):
    return Decision("continue", "request", 1, package.mission_id, 1, "d" * 64, {})


def _frame(package, position, index, *, mode="fw"):
    return RuntimeFrame(
        1, 1000, package.mission_id, 1, 1, False, True, mode, 60.0, 15.0, True, {},
        position_ne_amsl=position, active_motion_index=index,
        remaining_energy_wh=200.0, remaining_time_s=2000.0)


def _swept(slices, phase=None):
    return tuple(SweptLeg(slice_.phase if phase is None else phase, slice_.start, slice_.end,
                          slice_.padding_ne_m, slice_.padding_alt_m, slice_.duration_s,
                          slice_.peak_power_w, slice_.command_end) for slice_ in slices)


def _split(start, end, phase, duration):
    return _swept(slice_leg(
        start, end, CORRIDOR.fw_tracking_m, 1.0, duration, 750.0, CORRIDOR.slice_m,
        max_slices=CORRIDOR.max_sweep_legs), phase=phase)


def _runout_end(lowering):
    last = lowering.legs[-1]
    if lowering.gate_ne is not None:
        gate = lowering.gate_ne
        distance = math.dist(gate, HOME)
        ux = (HOME[0] - gate[0]) / distance if distance else 0.0
        uy = (HOME[1] - gate[1]) / distance if distance else 0.0
        length = max(distance, lowering.runout_m)
        return (gate[0] + ux * length, gate[1] + uy * length, lowering.cruise_msl)
    length = math.dist(last.start_ne, last.end_ne)
    ux = (last.end_ne[0] - last.start_ne[0]) / length if length else 0.0
    uy = (last.end_ne[1] - last.start_ne[1]) / length if length else 0.0
    return (last.end_ne[0] + ux * lowering.runout_m,
            last.end_ne[1] + uy * lowering.runout_m, lowering.cruise_msl)


def _sweep(lowering, terrain, decision, frame, position, index, *, failure=True):
    leg = lowering.legs[index - 1]
    duration = max(0.001, math.dist(position, leg.end_ne) / CORRIDOR.min_ground_speed_mps)
    legs = _swept(slice_leg(
        (*position, lowering.cruise_msl), leg.end_amsl, CORRIDOR.fw_tracking_m, 0.0,
        duration, CORRIDOR.fw_leg_peak_power_w, CORRIDOR.slice_m,
        max_slices=CORRIDOR.max_sweep_legs))
    for later in lowering.legs[index:]:
        legs += _swept(lowering.leg_slices[later.index - 1])
    fw_end = legs[-1].end
    runout = _runout_end(lowering)
    legs += _split(fw_end, runout, "transition", 20.0)
    legs += _split(runout, (*HOME, lowering.cruise_msl), "mc", 90.0)
    legs += (
        SweptLeg("landing", (*HOME, lowering.cruise_msl),
                 (*HOME, terrain.sample_local(*HOME).elevation_m), 8.0, 1.0, 50.0, 750.0),
    )
    return ExecutionSweep(
        frame.frame_id, decision.digest, legs, max_bank_deg=20.0, max_climb_mps=2.0,
        max_descent_mps=2.0, max_acceleration_mps2=1.0, max_angular_rate_dps=30.0,
        landing_pad=LandingPad((*HOME, terrain.sample_local(*HOME).elevation_m), 8.0, 2.0),
        failure_revalidation_enforced=failure)


def _midpoint(leg):
    return ((leg.start_ne[0] + leg.end_ne[0]) / 2.0,
            (leg.start_ne[1] + leg.end_ne[1]) / 2.0)


# C16 -- route identity is bound to the exact corridor lowering.
def test_route_identity_rejects_a_short_or_extended_pattern(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    for steps in (lowering.steps[:-1],
                  lowering.steps[:2] + (lowering.steps[2],) + lowering.steps[2:]):
        with pytest.raises(ValueError):
            X1MotionProofBuilder(package, profile, terrain, steps, mission_radius_m=4000.0,
                                 corridor=lowering)


def test_route_identity_rejects_gate_and_vertex_mismatch(terrain, profile):  # noqa: F811
    gated = replace(CORRIDOR, standoff_gate_enabled=True)
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]}, corridor=gated)
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    assert lowering.gate_ne is not None
    moved = replace(lowering, gate_ne=(lowering.gate_ne[0] + 50.0, lowering.gate_ne[1]))
    with pytest.raises(ValueError):
        X1MotionProofBuilder(package, profile, terrain, lowering.steps,
                             mission_radius_m=4000.0, corridor=moved)

    index = 2
    step = lowering.steps[index]
    command = replace(step.command, target_ned=(
        step.command.target_ned[0] + 25.0, step.command.target_ned[1],
        step.command.target_ned[2]))
    steps = lowering.steps[:index] + (replace(step, command=command),) + lowering.steps[index + 1:]
    with pytest.raises(ValueError):
        X1MotionProofBuilder(package, profile, terrain, steps, mission_radius_m=4000.0,
                             corridor=lowering)


def test_route_identity_rejects_inconsistent_height_and_home_terminal(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)

    index = 2
    step = lowering.steps[index]
    command = replace(step.command, target_ned=(
        step.command.target_ned[0], step.command.target_ned[1],
        step.command.target_ned[2] + 10.0))
    bad_height = lowering.steps[:index] + (replace(step, command=command),) + lowering.steps[index + 1:]
    with pytest.raises(ValueError):
        X1MotionProofBuilder(package, profile, terrain, bad_height, mission_radius_m=4000.0,
                             corridor=lowering)

    index = len(lowering.legs) + 3
    step = lowering.steps[index]
    command = replace(step.command, target_ned=(5.0, 0.0, step.command.target_ned[2]))
    bad_home = lowering.steps[:index] + (replace(step, command=command),) + lowering.steps[index + 1:]
    with pytest.raises(ValueError):
        X1MotionProofBuilder(package, profile, terrain, bad_home, mission_radius_m=4000.0,
                             corridor=lowering)


@pytest.mark.parametrize("overrides", [
    {"mode": "flight"}, {"assumed_sim_corridor": None},
])
def test_corridor_constructor_requires_the_assumed_sim_package(terrain, profile, overrides):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    bad = replace(package, **overrides)
    with pytest.raises(ValueError):
        X1MotionProofBuilder(bad, profile, terrain, lowering.steps, mission_radius_m=4000.0,
                             corridor=lowering)


# C17 -- every FW stage proves from its own leg tube; skipping a leg is closed.
@pytest.mark.parametrize("index", [1, 2, 3])
def test_continue_proves_from_each_current_leg(terrain, profile, index):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    position = (*_midpoint(lowering.legs[index - 1]), lowering.cruise_msl)
    decision = _decision(package)
    frame = _frame(package, position, index)
    proof = _builder(package, terrain, profile, lowering).build(
        decision, frame, _sweep(lowering, terrain, decision, frame, position[:2], index))
    assert proof.geofence_clear is True
    assert proof.required_energy_wh is not None and proof.required_time_s is not None


def test_continue_rejects_a_position_in_another_leg(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    position = (*_midpoint(lowering.legs[2]), lowering.cruise_msl)
    decision = _decision(package)
    frame = _frame(package, position, 1)
    sweep = _sweep(lowering, terrain, decision, frame, position[:2], 1)
    assert _builder(package, terrain, profile, lowering).build(decision, frame, sweep).geofence_clear is False


def test_continue_rejects_a_sweep_that_skips_an_intermediate_leg(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    position = (*_midpoint(lowering.legs[0]), lowering.cruise_msl)
    decision = _decision(package)
    frame = _frame(package, position, 1)
    sweep = _sweep(lowering, terrain, decision, frame, position[:2], 1)
    # Replace leg-2's slices with one coarse leg straight to leg 3's end.
    first = next(i for i, leg in enumerate(sweep.legs) if leg.phase == "fw")
    last_fw = max(i for i, leg in enumerate(sweep.legs) if leg.phase == "fw")
    coarse = SweptLeg("fw", sweep.legs[first].start, lowering.legs[-1].end_amsl,
                      19.0, 1.0, 60.0, 500.0, command_end=True)
    legs = sweep.legs[:first] + (coarse,) + sweep.legs[last_fw + 1:]
    assert _builder(package, terrain, profile, lowering).build(
        decision, frame, replace(sweep, legs=legs)).geofence_clear is False


# C18 -- one slice outside the tube union (or a diagonal AABB) closes the proof.
@pytest.mark.parametrize(("places", "padding"), [
    ({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]}, 60.0),
    ({"poi:a": [600.0, 600.0], "poi:b": [600.0, 1200.0]}, 50.0),
])
def test_one_slice_outside_the_tube_union_is_closed(terrain, profile, places, padding):  # noqa: F811
    package = _package(places)
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    position = (*_midpoint(lowering.legs[0]), lowering.cruise_msl)
    decision = _decision(package)
    frame = _frame(package, position, 1)
    sweep = _sweep(lowering, terrain, decision, frame, position[:2], 1)
    fw_index = next(i for i, leg in enumerate(sweep.legs) if leg.phase == "fw")
    legs = list(sweep.legs)
    legs[fw_index] = replace(legs[fw_index], padding_ne_m=padding)
    assert _builder(package, terrain, profile, lowering).build(
        decision, frame, replace(sweep, legs=tuple(legs))).geofence_clear is False


# C19 -- command_end/slice properties and the corridor-only sweep cap.
def test_swept_leg_defaults_to_a_command_end_and_slices_mark_only_their_last(terrain, profile):  # noqa: F811
    assert SweptLeg("fw", (0.0, 0.0, 160.0), (1.0, 0.0, 160.0), 1.0, 0.0, 1.0, 500.0).command_end is True
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    assert all(group[-1].command_end and all(not item.command_end for item in group[:-1])
               for group in lowering.leg_slices)


@pytest.mark.parametrize("heading_deg", [0.0, 17.0, 45.0, 73.0, 90.0, 135.0, 179.0, 251.0, 314.0])
def test_slice_leg_boxes_stay_in_the_tube_and_durations_sum(heading_deg):
    length = 600.0
    ux = math.cos(math.radians(heading_deg))
    uy = math.sin(math.radians(heading_deg))
    start = (0.0, 0.0, 160.0)
    end = (ux * length, uy * length, 160.0)
    duration = length / CORRIDOR.min_ground_speed_mps
    slices = slice_leg(start, end, CORRIDOR.fw_tracking_m, 0.0, duration,
                       CORRIDOR.fw_leg_peak_power_w, CORRIDOR.slice_m,
                       max_slices=CORRIDOR.max_sweep_legs)
    for item in slices:
        box = item.box()
        for n, e in ((box[0], box[2]), (box[0], box[3]),
                     (box[1], box[2]), (box[1], box[3])):
            dn, de = end[0] - start[0], end[1] - start[1]
            length2 = dn * dn + de * de
            t = min(1.0, max(0.0, ((n - start[0]) * dn + (e - start[1]) * de) / length2))
            distance = math.hypot(n - (start[0] + t * dn), e - (start[1] + t * de))
            assert distance <= CORRIDOR_TUBE_HALF_WIDTH_M + 1e-9
    assert math.isclose(sum(item.duration_s for item in slices), duration, abs_tol=1e-9)
    assert [item.command_end for item in slices] == [False] * (len(slices) - 1) + [True]
    assert len(slices) <= CORRIDOR.max_sweep_legs


def test_diagonal_command_box_outside_the_tube_union_is_closed(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 800.0], "poi:b": [800.0, 1600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    builder = _builder(package, terrain, profile, lowering)
    position = (*_midpoint(lowering.legs[0]), lowering.cruise_msl)
    slices = slice_leg(position, lowering.legs[0].end_amsl, CORRIDOR.fw_tracking_m, 0.0,
                       10.0, CORRIDOR.fw_leg_peak_power_w, CORRIDOR.slice_m,
                       max_slices=CORRIDOR.max_sweep_legs)
    assert builder._in_corridor_union((slices[0].box(),)) is True
    diagonal = (SweptLeg("fw", position, lowering.legs[0].end_amsl,
                         CORRIDOR.fw_tracking_m, 0.0, 10.0,
                         CORRIDOR.fw_leg_peak_power_w, command_end=True).box(),)
    assert builder._in_corridor_union(diagonal) is False


def test_corridor_sweep_cap_comes_from_the_lowering(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    position = (*_midpoint(lowering.legs[0]), lowering.cruise_msl)
    decision = _decision(package)
    frame = _frame(package, position, 1)
    sweep = _sweep(lowering, terrain, decision, frame, position[:2], 1)
    assert len(sweep.legs) > 1
    assert _builder(package, terrain, profile, lowering).build(
        decision, frame, sweep).geofence_clear is True
    assert _builder(package, terrain, profile, replace(lowering, max_sweep_legs=1)).build(
        decision, frame, sweep).geofence_clear is False


# C20 -- the wider cap never leaks to the single-waypoint path.
def _single_package():
    document = {
        "mission_id": "x1-single-proof", "home": [0.0, 0.0],
        "places": {"poi:beacon": [400.0, 0.0]}, "zones": {}, "routes": {},
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
    return load_mission_package(document)


def test_129_legs_stay_closed_without_a_corridor(terrain, profile):  # noqa: F811
    package = _single_package()
    steps = build_x1_mission_steps(package, terrain=terrain, profile=profile, mission_radius_m=1250.0)
    builder = X1MotionProofBuilder(package, profile, terrain, steps, mission_radius_m=1250.0,
                                   terrain_error_m=1.0, obstacle_clearance=lambda box: 25.0)
    decision = _decision(package)
    position = (0.0, 0.0, 180.0)
    frame = _frame(package, position, 3, mode="mc")
    legs = tuple(SweptLeg("mc", position, position, 8.0, 1.0, 5.0, 750.0) for _ in range(128))
    legs += (SweptLeg("landing", position, (0.0, 0.0, 100.0), 8.0, 1.0, 50.0, 750.0),)
    sweep = ExecutionSweep(
        frame.frame_id, decision.digest, legs, max_bank_deg=20.0, max_climb_mps=2.0,
        max_descent_mps=2.0, max_acceleration_mps2=1.0, max_angular_rate_dps=30.0,
        landing_pad=LandingPad((0.0, 0.0, 100.0), 8.0, 2.0), failure_revalidation_enforced=True)
    assert len(sweep.legs) == 129
    assert builder.build(decision, frame, sweep).geofence_clear is False


# C24 -- every FW leg must keep the enforced failure revalidation.
@pytest.mark.parametrize("index", [1, 2, 3])
def test_failure_revalidation_is_required_on_every_leg(terrain, profile, index):  # noqa: F811
    package = _package({"poi:a": [800.0, 0.0], "poi:b": [800.0, 600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    position = (*_midpoint(lowering.legs[index - 1]), lowering.cruise_msl)
    decision = _decision(package)
    frame = _frame(package, position, index)
    builder = _builder(package, terrain, profile, lowering)
    assert builder.build(
        decision, frame, _sweep(lowering, terrain, decision, frame, position[:2], index)
    ).geofence_clear is True
    assert builder.build(
        decision, frame,
        _sweep(lowering, terrain, decision, frame, position[:2], index, failure=False)
    ).geofence_clear is False


def test_turn_pad_is_a_circle_not_a_square(terrain, profile):  # noqa: F811
    package = _package({"poi:a": [800.0, 800.0], "poi:b": [800.0, 1600.0]})
    lowering = _lowering(package, ["poi:a", "poi:b", "home"], terrain, profile)
    builder = _builder(package, terrain, profile, lowering)
    _, pads = builder._corridor_segments()
    assert pads
    centre, pad = pads[0]
    # Inside the square (|dn|,|de| <= pad) but beyond the circle (distance > pad) and off every tube.
    offset = pad * 0.99
    box = (centre[0] + offset - 0.1, centre[0] + offset + 0.1,
           centre[1] + offset - 0.1, centre[1] + offset + 0.1, 0.0, 1.0)
    assert math.dist((box[0], box[2]), centre) > pad
    segments, _ = builder._corridor_segments()
    far_from_tubes = all(
        builder_distance > width for builder_distance, width in (
            (_segment_distance(a, b, (centre[0] + offset, centre[1] + offset)), w) for a, b, w in segments))
    assert far_from_tubes
    assert builder._in_corridor_union((box,)) is False
