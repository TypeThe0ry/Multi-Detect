"""CPU command/schema fixtures; no executed vehicle, expert or training evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    MissionCommand,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.onboard_teacher_mission import (
    CONTRACT,
    HeldTeacherLeg,
    TeacherMissionDraft,
    compile_teacher_held_mission,
)
from multidetect.autonomy.planner import PlannerPolicy, estimate_task
from multidetect.mission_planning import PlannedMissionCompilation
from multidetect.px4_mission import (
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    CompiledPx4Mission,
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
    compile_multirotor_executable_mission,
    compile_px4_mission_items,
    px4_wire_hash,
)

PROFILE_HASH = "b" * 64


@pytest.fixture
def inputs():
    home = GeoPoint(1.3, 103.8)
    points = (
        GeoPoint(1.3, 103.80003, 12.0, "relative_home"),
        GeoPoint(1.3, 103.80003, 14.0, "relative_home"),
        GeoPoint(1.3, 103.80006, 14.0, "relative_home"),
    )
    context = ContextSnapshot(
        snapshot_id="software-teacher-context",
        captured_at_s=10.0,
        captured_at_utc="2026-10-08T00:00:00Z",
        aircraft_position=home,
        home_position=home,
        airframe_profile_id="px4_multirotor_gazebo_v1",
        airframe_profile_hash=PROFILE_HASH,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1800.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )
    geometry = MultirotorMissionGeometry(
        home=home,
        waypoints=points,
        takeoff_altitude_m=12.0,
        loiter_time_s=2.0,
        loiter_radius_m=1.0,
        acceptance_radius_m=0.5,
    )
    task = ResolvedTaskSpec(
        task_id="experimental-teacher-fixture",
        task_type=TaskType.SURVEY,
        geometry=TaskGeometry("polyline", "declared-route", "forward", 6.0, points, 1.0),
        target_classes=("declared-route",),
        on_detection=(),
        constraints=TaskConstraints(25.0, 1200.0, True),
        completion_action=PlanAction.RETURN,
    )
    safety = Px4MultirotorSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=100.0,
        allowed_airframe_profile_ids=(context.airframe_profile_id,),
        minimum_relative_altitude_m=5.0,
        maximum_relative_altitude_m=30.0,
        maximum_mission_radius_m=50.0,
        maximum_loiter_radius_m=2.0,
        maximum_loiter_time_s=100.0,
    )
    return {
        "task": task,
        "context": context,
        "geometry": geometry,
        "safety_policy": safety,
        "legs": (
            HeldTeacherLeg(1, 5.0, 0.0),
            HeldTeacherLeg(2, 5.0, 90.0),
            HeldTeacherLeg(3, 5.0, -30.0),
        ),
        "airframe_profile_hash": PROFILE_HASH,
    }


def test_single_takeoff_held_waypoints_yaw_and_unchanged_terminal_lifecycle(inputs):
    draft = compile_teacher_held_mission(**inputs)
    original = compile_multirotor_executable_mission(
        platform_plan=draft.compilation.plan,
        context_snapshot=inputs["context"],
        geometry=inputs["geometry"],
        policy=inputs["safety_policy"],
        airframe_profile_hash=PROFILE_HASH,
        mission_id=draft.mission_id,
    )
    items = draft.compiled_mission.executable_mission.items
    assert tuple(i.command for i in items) == (
        MissionCommand.TAKEOFF,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
        MissionCommand.LAND,
    )
    assert items[0] == original.executable_mission.items[0]
    assert items[-2:] == original.executable_mission.items[-2:]
    for item, old, leg in zip(
        items[1:-2], original.executable_mission.items[1:-2], draft.legs, strict=True
    ):
        assert replace(item, param1=0.0, param4=0.0) == old
        assert item.param1 == leg.hold_s
        assert item.param4 == leg.yaw_deg
        assert item.param2 == 0.5
        assert item.param3 == 0.0  # no pass radius / fly-through authoring
    assert items[-2].param4 == 0.0  # LOITER param4 has other semantics; never reuse as leg yaw
    mission = draft.compiled_mission.executable_mission
    assert mission.airframe_adapter_id == PX4_MULTIROTOR_ADAPTER_ID
    assert mission.airframe_adapter_hash == PX4_MULTIROTOR_ADAPTER_HASH
    assert [w.param4 for w in draft.compiled_mission.wire_items[1:-2]] == [0.0, 90.0, 330.0]
    assert [w.current for w in draft.compiled_mission.wire_items] == [1, 0, 0, 0, 0, 0]
    assert draft.compiled_mission.wire_hash != original.wire_hash
    assert draft.compilation.plan.canonical_hash == original.executable_mission.platform_plan_hash


@pytest.mark.parametrize("yaw,expected", [(-360, 0), (360, 0), (-30, 330), (725, 5), (-0.0, 0)])
def test_yaw_wrap_is_explicit_clockwise_north_degrees(yaw, expected):
    assert HeldTeacherLeg(1, 2, yaw).yaw_deg == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", True),
        ("sequence", 1.0),
        ("sequence", 0),
        ("sequence", 17),
        ("hold_s", True),
        ("hold_s", -0.01),
        ("hold_s", 60.01),
        ("hold_s", float("inf")),
        ("hold_s", float("nan")),
        ("yaw_deg", True),
        ("yaw_deg", float("nan")),
        ("yaw_deg", float("inf")),
        ("yaw_deg", 3601),
        ("yaw_deg", -3601),
    ],
)
def test_leg_rejects_bool_nonfinite_and_out_of_bounds(field, value):
    raw = {"sequence": 1, "hold_s": 5.0, "yaw_deg": 30.0, field: value}
    with pytest.raises(ValueError):
        HeldTeacherLeg.from_mapping(raw)


def test_budget_includes_explicit_3d_yaw_dwell_without_refunding_shared_estimate(inputs):
    draft = compile_teacher_held_mission(**inputs)
    baseline = estimate_task(inputs["task"], inputs["context"], "multirotor")
    budget = draft.budget_review
    assert budget["shared_estimated_duration_s"] == baseline.estimated_duration_s
    assert budget["total_dwell_s"] == 17.0
    assert budget["supplemental_vertical_distance_m"] == 28.0
    assert budget["supplemental_vertical_time_s"] == 56.0
    assert budget["supplemental_yaw_time_s"] == 216.0
    assert budget["supplemental_horizontal_distance_m"] > 13.0
    expected = (17 + 56 + 216 + budget["supplemental_horizontal_time_s"]) * 1.25
    assert budget["supplemental_duration_s"] == pytest.approx(expected)
    assert draft.compilation.estimate.estimated_duration_s == pytest.approx(
        baseline.estimated_duration_s + expected
    )
    assert draft.compilation.estimate.available_duration_s == baseline.available_duration_s
    assert draft.compilation.plan.task_spec_hash == inputs["task"].canonical_hash


@pytest.mark.parametrize("failure", ["task_time", "energy", "total_dwell"])
def test_supplemental_budget_failures_even_when_original_compile_would_pass(inputs, failure):
    if failure == "task_time":
        task = inputs["task"]
        inputs["task"] = replace(
            task, constraints=replace(task.constraints, maximum_duration_s=150)
        )
        match = "maximum_duration"
    elif failure == "energy":
        inputs["context"] = replace(inputs["context"], estimated_flight_time_s=300)
        match = "energy budget"
    else:
        inputs["safety_policy"] = replace(inputs["safety_policy"], maximum_loiter_time_s=10)
        match = "dwell policy"
    with pytest.raises(ValueError, match=match):
        compile_teacher_held_mission(**inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("gps_fix_type", 2),
        ("gps_horizontal_accuracy_m", 7.0),
        ("battery_remaining_pct", 29.0),
        ("geofence_valid", False),
        ("camera_available", False),
        ("perception_available", False),
        ("map_cache_available", False),
        ("dem_available", False),
    ],
)
def test_original_readiness_and_return_reserve_gates_are_not_bypassed(inputs, field, value):
    inputs["context"] = replace(inputs["context"], **{field: value})
    with pytest.raises(ValueError):
        compile_teacher_held_mission(**inputs)


@pytest.mark.parametrize(
    "failure",
    [
        "altitude",
        "fence",
        "home",
        "loiter",
        "items",
        "profile",
        "acceptance_low",
        "acceptance_high",
        "acceptance_alt_margin",
        "acceptance_fence_margin",
        "home_radius_margin",
    ],
)
def test_original_safety_and_new_acceptance_envelope_gates(inputs, failure):
    geometry, safety = inputs["geometry"], inputs["safety_policy"]
    if failure == "altitude":
        inputs["safety_policy"] = replace(safety, maximum_relative_altitude_m=13)
    elif failure == "fence":
        inputs["safety_policy"] = replace(safety, geofence_center=GeoPoint(1.31, 103.8))
    elif failure == "home":
        inputs["context"] = replace(inputs["context"], home_position=GeoPoint(1.31, 103.8))
    elif failure == "loiter":
        inputs["geometry"] = replace(geometry, loiter_radius_m=3)
    elif failure == "items":
        fourth = replace(geometry.waypoints[-1], longitude_deg=103.80008)
        inputs["geometry"] = replace(geometry, waypoints=(*geometry.waypoints, fourth))
        inputs["task"] = replace(
            inputs["task"],
            geometry=replace(inputs["task"].geometry, centerline=inputs["geometry"].waypoints),
        )
        inputs["legs"] = (*inputs["legs"], HeldTeacherLeg(4, 5, 20))
        inputs["safety_policy"] = replace(safety, maximum_items=6)
    elif failure == "profile":
        inputs["safety_policy"] = replace(safety, allowed_airframe_profile_ids=("wrong",))
    elif failure == "acceptance_low":
        inputs["geometry"] = replace(geometry, acceptance_radius_m=0.01)
    elif failure == "acceptance_high":
        inputs["geometry"] = replace(geometry, acceptance_radius_m=1.01)
    elif failure == "acceptance_alt_margin":
        inputs["safety_policy"] = replace(safety, minimum_relative_altitude_m=11.8)
    elif failure == "acceptance_fence_margin":
        inputs["safety_policy"] = replace(
            safety, geofence_radius_m=6.9, maximum_mission_radius_m=6.9
        )
    else:
        inputs["safety_policy"] = replace(safety, maximum_mission_radius_m=6.9)
    with pytest.raises(ValueError):
        compile_teacher_held_mission(**inputs)


@pytest.mark.parametrize(
    "failure",
    [
        "reordered",
        "missing",
        "list",
        "centerline",
        "branch",
        "not_survey",
        "land_only",
        "geofence_opt_out",
        "landing_elsewhere",
        "hash",
        "one_leg",
        "too_many",
        "unresolved",
    ],
)
def test_experimental_route_contract_is_narrow_and_ordered(inputs, failure):
    task, geometry, legs = inputs["task"], inputs["geometry"], inputs["legs"]
    if failure == "reordered":
        inputs["legs"] = tuple(reversed(legs))
    elif failure == "missing":
        inputs["legs"] = legs[:2]
    elif failure == "list":
        inputs["legs"] = list(legs)
    elif failure == "centerline":
        inputs["task"] = replace(
            task,
            geometry=replace(task.geometry, centerline=tuple(reversed(task.geometry.centerline))),
        )
    elif failure == "branch":
        inputs["task"] = replace(task, on_detection=(PlanAction.CAPTURE_EVIDENCE,))
    elif failure == "not_survey":
        inputs["task"] = replace(task, task_type=TaskType.AREA_SEARCH)
    elif failure == "land_only":
        inputs["task"] = replace(task, completion_action=PlanAction.LAND)
    elif failure == "geofence_opt_out":
        inputs["task"] = replace(
            task, constraints=replace(task.constraints, remain_inside_geofence=False)
        )
    elif failure == "landing_elsewhere":
        inputs["geometry"] = replace(geometry, landing=GeoPoint(1.3, 103.80001, 0, "relative_home"))
    elif failure == "hash":
        inputs["airframe_profile_hash"] = "c" * 64
    elif failure == "one_leg":
        inputs["legs"] = legs[:1]
    elif failure == "too_many":
        inputs["legs"] = legs * 6
    else:
        inputs["task"] = replace(task, unresolved_assumptions=("unknown surveyed geometry",))
    with pytest.raises((ValueError, TypeError)):
        compile_teacher_held_mission(**inputs)


def test_zero_hold_is_not_claimed_settled_stop_or_completion(inputs):
    inputs["legs"] = tuple(replace(leg, hold_s=0) for leg in inputs["legs"])
    draft = compile_teacher_held_mission(**inputs)
    assert all(item.param1 == 0.0 for item in draft.compiled_mission.executable_mission.items[1:-2])
    claims = draft.to_mapping()["claims"]
    assert claims and all(value is False for value in claims.values())
    assert draft.validation_checks["planned_hold_is_not_stop_complete_or_rtl"] is True
    assert not any(
        item.command is MissionCommand.RETURN_TO_LAUNCH
        for item in draft.compiled_mission.executable_mission.items
    )


def test_exact_json_roundtrip_and_shared_compiled_type_compatibility(inputs):
    draft = compile_teacher_held_mission(**inputs)
    reopened = TeacherMissionDraft.from_json(json.dumps(draft.to_mapping()))
    assert reopened == draft
    assert reopened.canonical_hash == draft.canonical_hash
    assert reopened.authoring_hash == draft.authoring_hash
    assert draft.to_mapping()["contract"] == CONTRACT
    assert (
        PlannedMissionCompilation.from_mapping(draft.compilation.to_mapping()) == draft.compilation
    )
    assert (
        CompiledPx4Mission.from_mapping(draft.compiled_mission.to_mapping())
        == draft.compiled_mission
    )


@pytest.mark.parametrize(
    "failure",
    [
        "contract",
        "schema_bool",
        "schema",
        "unknown",
        "gold",
        "planner_unknown",
        "authoring",
        "wire",
        "compiled_hold",
        "budget",
        "rules",
        "checks",
        "authority",
        "teacher",
        "leg_extra",
    ],
)
def test_tampered_or_truth_augmented_artifact_is_rejected_by_recompile(inputs, failure):
    value = copy.deepcopy(compile_teacher_held_mission(**inputs).to_mapping())
    if failure == "contract":
        value["contract"] = "other"
    elif failure == "schema_bool":
        value["schema_version"] = True
    elif failure == "schema":
        value["schema_version"] = 2
    elif failure == "unknown":
        value["unknown"] = 1
    elif failure == "gold":
        value["inputs"]["geometry"]["gold_pose"] = {"x": 1}
    elif failure == "planner_unknown":
        value["inputs"]["planner_policy"]["magic_budget"] = 10
    elif failure == "authoring":
        value["authoring_hash"] = "a" * 64
    elif failure == "wire":
        value["compilation"]["wire_hash"] = "a" * 64
    elif failure == "compiled_hold":
        value["compilation"]["compiled_mission"]["executable_mission"]["items"][1]["param1"] = 4
    elif failure == "budget":
        value["budget_review"]["supplemental_yaw_time_s"] = 0
    elif failure == "rules":
        value["authoring_rules"]["shared_plan_does_not_include_holds"] = False
    elif failure == "checks":
        value["validation_checks"]["scope"] = "actual teacher approval"
    elif failure == "authority":
        value["claims"]["execution_authority"] = True
    elif failure == "teacher":
        value["claims"]["teacher_expert_verified"] = True
    else:
        value["inputs"]["legs"][0]["planned_actual_pose"] = [1, 2, 3]
    with pytest.raises(ValueError):
        TeacherMissionDraft.from_mapping(value)


@pytest.mark.parametrize(
    "payload",
    ['{"contract":"x","contract":"y"}', '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', "{"],
)
def test_strict_json_duplicate_nonfinite_and_truncation(payload):
    with pytest.raises(ValueError):
        TeacherMissionDraft.from_json(payload)


def test_command_policy_changes_have_distinct_authoring_and_wire_hashes(inputs):
    first = compile_teacher_held_mission(**inputs)
    changed_leg = replace(first, legs=(replace(first.legs[0], hold_s=6), *first.legs[1:]))
    assert changed_leg.authoring_hash != first.authoring_hash
    assert changed_leg.compiled_mission.wire_hash != first.compiled_mission.wire_hash
    assert (
        changed_leg.compilation.plan == first.compilation.plan
    )  # shared plan not falsely augmented
    changed_policy = replace(first, planner_policy=PlannerPolicy(duration_safety_factor=1.5))
    assert changed_policy.authoring_hash != first.authoring_hash
    assert changed_policy.canonical_hash != first.canonical_hash
    assert changed_policy.compiled_mission.wire_hash == first.compiled_mission.wire_hash
    assert (
        changed_policy.budget_review["total_estimated_duration_s"]
        > (first.budget_review["total_estimated_duration_s"])
    )


def test_mappings_do_not_expose_mutable_draft_internal_state(inputs):
    draft = compile_teacher_held_mission(**inputs)
    identity = draft.canonical_hash
    budget = draft.budget_review
    budget["total_estimated_duration_s"] = 0
    raw = draft.to_mapping()
    raw["claims"]["execution_authority"] = True
    raw["inputs"]["legs"][0]["hold_s"] = 0
    assert draft.canonical_hash == identity


def test_compilation_preserves_strict_wire_flags_and_hash_semantics(inputs):
    draft = compile_teacher_held_mission(**inputs)
    items = draft.compiled_mission.wire_items
    with pytest.raises(ValueError, match="current"):
        replace(items[0], current=0)
    with pytest.raises(ValueError, match="current"):
        replace(items[2], current=1)
    changed = list(items)
    changed[1] = replace(changed[1], param4=1.0)
    assert px4_wire_hash(tuple(changed)) != px4_wire_hash(items)
    assert compile_px4_mission_items(draft.compiled_mission.executable_mission) == items


def test_authoring_has_no_transport_authority_or_frozen_source_mutation(inputs):
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "src/multidetect/px4_mission.py",
        root / "scripts/onboard_teacher_mission_intervals.py",
        root / "src/multidetect/autonomy/onboard_executed_trajectory.py",
    )
    before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    draft = compile_teacher_held_mission(**inputs)
    after = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    assert before == after
    assert not hasattr(draft, "operator_grant")
    assert not hasattr(draft, "permit")
    assert not hasattr(draft, "execute")
    assert not hasattr(draft, "runtime_actor")
