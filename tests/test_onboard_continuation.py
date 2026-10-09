from __future__ import annotations

import copy
import json
import math
from dataclasses import replace

import pytest

from multidetect.autonomy.contracts import (
    ContextSnapshot,
    GeoPoint,
    MissionCommand,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.onboard_continuation import (
    CONTINUATION_ADAPTER_HASH,
    CONTINUATION_ADAPTER_ID,
    ContinuationCompilation,
    ContinuationCompilePolicy,
    ContinuationParentBinding,
    compile_multirotor_continuation,
)
from multidetect.autonomy.planner import PlannerPolicy
from multidetect.mission_planning import PlannedMissionCompilation
from multidetect.px4_mission import (
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
    compile_px4_mission_items,
    px4_wire_hash,
)


def _inputs():
    home = GeoPoint(1.3, 103.8)
    start = GeoPoint(1.3001, 103.80005, 30.0, "relative_home")
    target = GeoPoint(1.3004, 103.8002, 35.0, "relative_home")
    task = ResolvedTaskSpec(
        task_id="fixture-inspection",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="fixture-route",
            direction="north",
            length_m=100.0,
            buffer_m=10.0,
            centerline=(home, target),
            resolution_confidence=0.95,
        ),
        target_classes=("inspection_target",),
        on_detection=(),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )
    context = ContextSnapshot(
        snapshot_id="fixture-current",
        captured_at_s=10.0,
        captured_at_utc="2026-10-07T00:00:00Z",
        aircraft_position=start,
        home_position=home,
        airframe_profile_id="fixture-multirotor",
        airframe_profile_hash="a" * 64,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1800.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=1.0,
        wind_east_mps=-0.5,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )
    geometry = MultirotorMissionGeometry(home, (target,), 30.0, 15.0, 5.0, 2.0)
    safety = Px4MultirotorSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=2000.0,
        allowed_airframe_profile_ids=("fixture-multirotor",),
        minimum_relative_altitude_m=10.0,
        maximum_relative_altitude_m=100.0,
        maximum_mission_radius_m=1000.0,
    )
    runtime = {
        "schema_version": 1,
        "vehicle_class": "multirotor",
        **geometry.to_mapping(),
        "safety_policy": safety.to_mapping(),
    }
    parent = ContinuationParentBinding(
        "fixture-parent", "b" * 64, "c" * 64, "d" * 64, "a" * 64, home, 2, 10.0
    )
    return task, context, target, runtime, parent


def _compile(**overrides):
    task, context, target, runtime, parent = _inputs()
    arguments = dict(
        task=task,
        current_context=context,
        target=target,
        runtime=runtime,
        parent=parent,
        mission_id="fixture-child",
        now_s=10.1,
        source_intent_sha256="e" * 64,
    )
    arguments.update(overrides)
    return compile_multirotor_continuation(**arguments)


def test_three_items_are_true_continuation_not_full_sortie():
    result = _compile()
    assert not isinstance(result, PlannedMissionCompilation)
    mission = result.compiled_mission.executable_mission
    assert [item.command for item in mission.items] == [
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
    ]
    assert [item.command for item in result.compiled_mission.wire_items] == [16, 16, 19]
    assert [item.current for item in mission.items] == [1, 0, 0]
    assert result.resume_sequence == 1
    assert mission.items[0].latitude_deg == result.current_context.aircraft_position.latitude_deg
    assert mission.items[0].latitude_deg != result.current_context.home_position.latitude_deg
    assert mission.items[1].latitude_deg == result.target.latitude_deg
    assert mission.items[2].param1 == 15.0
    assert mission.items[2].param3 == 5.0
    assert mission.airframe_adapter_id == CONTINUATION_ADAPTER_ID != PX4_MULTIROTOR_ADAPTER_ID
    assert mission.airframe_adapter_hash == CONTINUATION_ADAPTER_HASH != PX4_MULTIROTOR_ADAPTER_HASH
    assert result.plan.steps[1].action == PlanAction.SEARCH_CORRIDOR
    assert result.compiled_mission.wire_items == compile_px4_mission_items(mission)
    assert result.wire_hash == px4_wire_hash(result.compiled_mission.wire_items)
    assert all(value is False for value in result.to_mapping()["authority"].values())


def test_serialized_inputs_and_outputs_recompute_exactly():
    result = _compile()
    reopened = ContinuationCompilation.from_mapping(json.loads(result.canonical_json))
    assert reopened == result and reopened.canonical_hash == result.canonical_hash
    assert ContinuationParentBinding.from_mapping(result.parent.to_mapping()) == result.parent
    assert ContinuationCompilePolicy.from_mapping(result.policy.to_mapping()) == result.policy


@pytest.mark.parametrize(
    "field",
    [
        "wire_hash",
        "resume_sequence",
        "parent_binding_sha256",
        "budget",
        "authority",
        "compiled_mission",
        "plan",
        "unexpected",
    ],
)
def test_saved_artifact_cannot_replace_outputs_or_grant_authority(field):
    raw = _compile().to_mapping()
    raw[field] = "forged"
    with pytest.raises((ValueError, TypeError)):
        ContinuationCompilation.from_mapping(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"gps_fix_type": 2},
        {"gps_horizontal_accuracy_m": 7.0},
        {"geofence_valid": False},
        {"camera_available": False},
        {"perception_available": False},
        {"map_cache_available": False},
        {"dem_available": False},
        {"battery_remaining_pct": 45.0},
        {"estimated_flight_time_s": 1.0},
        {"airframe_profile_hash": None},
        {"airframe_profile_hash": "f" * 64},
        {"airframe_profile_id": "foreign"},
        {"home_position": GeoPoint(1.31, 103.8)},
    ],
)
def test_original_resource_gps_energy_and_parent_identity_gates_remain(changes):
    _, context, _, _, _ = _inputs()
    with pytest.raises(ValueError):
        _compile(current_context=replace(context, **changes))


@pytest.mark.parametrize("now", [9.99, 11.01, math.nan, math.inf, True])
def test_current_or_parent_time_must_be_finite_ordered_and_fresh(now):
    with pytest.raises(ValueError):
        _compile(now_s=now)


def test_old_parent_is_not_refreshed_by_current_context():
    _, _, _, _, parent = _inputs()
    with pytest.raises(ValueError, match="parent facts"):
        _compile(parent=replace(parent, observed_at_s=8.0))


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_current_sequence": -1},
        {"observed_current_sequence": 64},
        {"observed_current_sequence": True},
        {"source_system": 2},
        {"source_component": 0},
        {"parent_wire_sha256": "bad"},
    ],
)
def test_typed_parent_cursor_source_and_hash_fields_reject_invalid(changes):
    _, _, _, _, parent = _inputs()
    with pytest.raises(ValueError):
        replace(parent, **changes)


def test_child_id_cannot_reuse_parent():
    with pytest.raises(ValueError, match="reuse parent"):
        _compile(mission_id="fixture-parent")


@pytest.mark.parametrize(
    "point",
    [
        GeoPoint(1.3004, 103.8002),
        GeoPoint(1.3004, 103.8002, 35.0, "amsl"),
        GeoPoint(1.3004, 103.8002, 0.0, "relative_home"),
        GeoPoint(1.3004, 103.8002, 101.0, "relative_home"),
    ],
)
def test_target_altitude_is_known_relative_home_and_never_clamped(point):
    with pytest.raises(ValueError):
        _compile(target=point)


def test_current_altitude_must_also_be_known_and_in_envelope():
    _, context, _, _, _ = _inputs()
    with pytest.raises(ValueError):
        _compile(current_context=replace(context, aircraft_position=GeoPoint(1.3001, 103.80005)))


@pytest.mark.parametrize("delta", [0.0, 1e-10])
def test_zero_wire_rounded_horizontal_displacement_rejected_even_with_climb(delta):
    _, context, _, _, _ = _inputs()
    point = context.aircraft_position
    with pytest.raises(ValueError, match="wire-rounded"):
        _compile(
            target=GeoPoint(point.latitude_deg + delta, point.longitude_deg, 35.0, "relative_home")
        )


@pytest.mark.parametrize("vehicle", ["vtol", "fixed-wing", "unknown"])
def test_nonmultirotor_is_not_a_continuation_alias(vehicle):
    with pytest.raises(ValueError):
        _compile(vehicle_class=vehicle)


def test_runtime_rejects_foreign_vehicle_truth_and_bad_geometry():
    _, _, _, runtime, _ = _inputs()
    for mutation in ("vehicle", "truth", "home", "loiter"):
        raw = copy.deepcopy(runtime)
        if mutation == "vehicle":
            raw["vehicle_class"] = "vtol"
        elif mutation == "truth":
            raw["mission_geometry"]["truth"] = {"expected": "safe"}
        elif mutation == "home":
            raw["home_position"]["latitude_deg"] += 0.01
        else:
            raw["mission_geometry"]["loiter_time_s"] = 601.0
        with pytest.raises(ValueError):
            _compile(runtime=raw)


def test_budget_counts_round_trip_vertical_loiter_wait_landing_reserve():
    result = _compile()
    budget, policy = result.budget, result.policy
    assert budget.return_distance_m > 0 and budget.outbound_distance_m > 0
    assert budget.segment_climb_m == 5.0 and budget.return_climb_m == 25.0
    assert budget.return_descent_m == 60.0
    assert budget.vertical_duration_s == 75.0
    assert budget.post_segment_wait_budget_s == 30.0
    total = sum(
        (
            budget.outbound_travel_duration_s,
            budget.return_travel_duration_s,
            budget.buffer_travel_duration_s,
            budget.vertical_duration_s,
            budget.loiter_duration_s,
            budget.post_segment_wait_budget_s,
            budget.landing_duration_s,
            budget.detection_overhead_duration_s,
        )
    )
    assert budget.estimated_duration_s == pytest.approx(
        total * policy.planner.duration_safety_factor
    )
    assert budget.available_duration_s == pytest.approx(1800 * (90 - 40 - 5) / 90)
    assert budget.return_reserve_pct == 40.0 and budget.battery_margin_pct == 5.0
    assert budget.to_mapping()["watchdog_implemented"] is False
    assert budget.to_mapping()["physical_energy_calibration_verified"] is False


def test_land_task_does_not_erase_continuation_return_budget():
    task, _, _, _, _ = _inputs()
    result = _compile(task=replace(task, completion_action=PlanAction.LAND))
    assert result.original_task_estimate.return_distance_m == 0
    assert result.budget.return_distance_m > 0 and result.budget.return_descent_m > 0


def test_original_detection_actions_and_landing_offset_are_not_free_energy():
    task, _, _, runtime, _ = _inputs()
    base = _compile()
    task = replace(task, on_detection=(PlanAction.CAPTURE_EVIDENCE, PlanAction.REVISIT))
    runtime["mission_geometry"]["landing"]["latitude_deg"] += 0.00005
    result = _compile(task=task, runtime=runtime)
    assert result.budget.detection_overhead_duration_s == 25.0
    assert result.budget.return_distance_m > base.budget.return_distance_m
    assert result.budget.estimated_duration_s > base.budget.estimated_duration_s


def test_wire_float32_altitude_must_also_be_inside_policy():
    _, _, _, runtime, _ = _inputs()
    runtime["safety_policy"]["maximum_relative_altitude_m"] = 60.000002
    policy = ContinuationCompilePolicy(assumed_return_relative_altitude_m=60.0)
    with pytest.raises(ValueError, match="wire-quantized"):
        _compile(
            target=GeoPoint(1.3004, 103.8002, 60.000002, "relative_home"),
            runtime=runtime,
            policy=policy,
        )


def test_wind_and_vertical_wait_budget_can_independently_reject():
    _, context, _, _, _ = _inputs()
    with pytest.raises(ValueError):
        _compile(current_context=replace(context, wind_north_mps=8.0, wind_east_mps=0.0))
    with pytest.raises(ValueError, match="energy"):
        _compile(policy=ContinuationCompilePolicy(assumed_climb_rate_mps=0.01))
    with pytest.raises(ValueError, match="energy"):
        _compile(policy=ContinuationCompilePolicy(post_segment_wait_budget_s=1000.0))


def test_climb_descent_have_separate_budget_and_measured_speed_does_not_shorten_assumption():
    _, context, target, _, _ = _inputs()
    normal = _compile(target=replace(target, altitude_m=20.0))
    faster = _compile(
        current_context=replace(context, ground_speed_mps=35.0, airspeed_mps=35.0),
        target=replace(target, altitude_m=20.0),
    )
    assert normal.budget.segment_descent_m == 10.0 and normal.budget.segment_climb_m == 0.0
    assert normal.budget.estimated_duration_s == faster.budget.estimated_duration_s


def test_source_target_invariant_when_latest_position_changes():
    _, context, _, _, _ = _inputs()
    first = _compile()
    second = _compile(
        current_context=replace(
            context,
            snapshot_id="fixture-later",
            aircraft_position=GeoPoint(1.3002, 103.8001, 30.0, "relative_home"),
        )
    )
    assert first.compiled_mission.wire_items[1:] == second.compiled_mission.wire_items[1:]
    assert first.compiled_mission.wire_items[0] != second.compiled_mission.wire_items[0]
    assert first.wire_hash != second.wire_hash


def test_dateline_target_is_fixed_and_short_wgs84_route():
    task, context, _, runtime, parent = _inputs()
    home = GeoPoint(0.0, -179.9998)
    start = GeoPoint(0.0, 179.9998, 30.0, "relative_home")
    target = GeoPoint(0.0, -179.9997, 35.0, "relative_home")
    context = replace(context, aircraft_position=start, home_position=home)
    parent = replace(parent, home_position=home)
    task = replace(task, geometry=replace(task.geometry, centerline=(start, target)))
    runtime["home_position"] = home.to_mapping()
    runtime["mission_geometry"]["landing"] = GeoPoint(
        0.0, -179.9998, 0.0, "relative_home"
    ).to_mapping()
    runtime["safety_policy"]["geofence_center"] = home.to_mapping()
    result = _compile(
        task=task, current_context=context, target=target, runtime=runtime, parent=parent
    )
    assert result.budget.outbound_distance_m < 60.0
    assert result.compiled_mission.wire_items[1].y == -1799997000


def test_geofence_center_inside_is_not_enough_for_loiter_envelope():
    _, _, _, runtime, _ = _inputs()
    runtime["safety_policy"]["geofence_radius_m"] = 52.0
    runtime["safety_policy"]["maximum_mission_radius_m"] = 52.0
    with pytest.raises(ValueError, match="envelope"):
        _compile(runtime=runtime)


@pytest.mark.parametrize(
    "changes",
    [
        {"assumed_climb_rate_mps": 0.0},
        {"maximum_context_age_s": math.inf},
        {"post_segment_wait_budget_s": math.nan},
        {"planner": "fake"},
        {"planner": PlannerPolicy(landing_duration_s=0.0)},
    ],
)
def test_compile_policy_rejects_nonfinite_or_disabled_budget(changes):
    with pytest.raises(ValueError):
        ContinuationCompilePolicy(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"assumed_climb_rate_mps": 1e6},
        {"assumed_descent_rate_mps": 1e6},
        {"post_segment_wait_budget_s": 0.001},
        {"assumed_return_relative_altitude_m": 59.0},
        {"maximum_context_age_s": 1.01},
        {"maximum_parent_age_s": 1.01},
        *(
            {"planner": replace(PlannerPolicy(), **{name: value})}
            for name, value in (
                ("minimum_gps_fix_type", 2),
                ("maximum_gps_horizontal_accuracy_m", 7.0),
                ("minimum_geometry_confidence", 0.49),
                ("minimum_battery_margin_pct", 4.99),
                ("minimum_speed_mps", 0.99),
                ("maximum_planning_speed_mps", 36.0),
                ("multirotor_cruise_speed_mps", 8.01),
                ("fixed_wing_cruise_speed_mps", 18.01),
                ("vtol_cruise_speed_mps", 15.01),
                ("takeoff_duration_s", 29.99),
                ("landing_duration_s", 29.99),
                ("vtol_transition_duration_s", 44.99),
                ("duration_safety_factor", 1.24),
            )
        ),
    ],
)
def test_development_policy_cannot_weaken_unreviewed_performance_or_safety(changes):
    with pytest.raises(ValueError, match="only tighten"):
        ContinuationCompilePolicy(**changes)


def test_tightening_development_policy_is_allowed():
    policy = ContinuationCompilePolicy(
        planner=replace(
            PlannerPolicy(),
            minimum_battery_margin_pct=10.0,
            multirotor_cruise_speed_mps=6.0,
            landing_duration_s=40.0,
            duration_safety_factor=1.5,
        ),
        maximum_context_age_s=0.5,
        maximum_parent_age_s=0.5,
        assumed_climb_rate_mps=1.0,
        assumed_descent_rate_mps=0.5,
        assumed_return_relative_altitude_m=70.0,
        post_segment_wait_budget_s=60.0,
    )
    assert (
        _compile(policy=policy).budget.estimated_duration_s > _compile().budget.estimated_duration_s
    )


def test_low_endurance_cannot_be_rescued_by_unreviewed_faster_rates_or_short_wait():
    _, context, _, _, _ = _inputs()
    context = replace(context, estimated_flight_time_s=240.0)
    with pytest.raises(ValueError, match="energy"):
        _compile(current_context=context)
    with pytest.raises(ValueError, match="only tighten"):
        ContinuationCompilePolicy(
            assumed_climb_rate_mps=1e6,
            assumed_descent_rate_mps=1e6,
            post_segment_wait_budget_s=0.001,
        )


def test_equivalent_dateline_coordinates_are_zero_physical_displacement():
    _, context, _, _, _ = _inputs()
    context = replace(context, aircraft_position=GeoPoint(0.0, 180.0, 30.0, "relative_home"))
    with pytest.raises(ValueError, match="zero physical"):
        _compile(current_context=context, target=GeoPoint(0.0, -180.0, 35.0, "relative_home"))


def test_runtime_snapshot_is_immutable_after_compile():
    _, _, _, runtime, _ = _inputs()
    result = _compile(runtime=runtime)
    original = result.canonical_hash
    runtime["mission_geometry"]["loiter_time_s"] = 100.0
    assert result.canonical_hash == original


def test_runtime_json_rejects_duplicate_keys_and_finite_overflow():
    result = _compile()
    for raw in ('{"schema_version":1,"schema_version":1}', '{"schema_version":1,"bad":1e309}'):
        with pytest.raises(ValueError):
            replace(result, runtime_json=raw)
