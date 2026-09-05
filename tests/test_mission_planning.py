from __future__ import annotations

import json
from dataclasses import replace

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    plan_task,
)
from multidetect.mission_compile_cli import main as mission_compile_cli_main
from multidetect.mission_planning import (
    MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
    PlannedMissionCompilation,
    compile_planned_task_mission,
    compile_planned_task_mission_from_runtime,
    load_executable_mission_document,
    parse_mission_compile_runtime,
)
from multidetect.px4_mission import (
    FixedWingMissionGeometry,
    MultirotorMissionGeometry,
    Px4FixedWingSafetyPolicy,
    Px4MissionSafetyPolicy,
    Px4MultirotorSafetyPolicy,
    VtolMissionGeometry,
)

PROFILE_HASH = "b" * 64


def _task() -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="shared-corridor-001",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="corridor-001",
            direction="northbound",
            length_m=1_000.0,
            buffer_m=30.0,
            centerline=(
                GeoPoint(1.3000, 103.8000),
                GeoPoint(1.3040, 103.8000),
            ),
            resolution_confidence=0.95,
        ),
        target_classes=("person",),
        on_detection=(
            PlanAction.OBSERVE_TARGET,
            PlanAction.CAPTURE_EVIDENCE,
            PlanAction.REPORT_GEOLOCATION,
        ),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def _context(vehicle_class: str) -> ContextSnapshot:
    profile = {
        "vtol": "px4_vtol_gazebo_v1",
        "fixed-wing": "px4_fixed_wing_gazebo_v1",
        "multirotor": "px4_multirotor_gazebo_v1",
    }[vehicle_class]
    return ContextSnapshot(
        snapshot_id=f"shared-context-{vehicle_class}",
        captured_at_s=10.0,
        captured_at_utc="2026-09-04T00:00:00Z",
        aircraft_position=GeoPoint(1.3000, 103.8000),
        home_position=GeoPoint(1.3000, 103.8000),
        airframe_profile_id=profile,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
        ground_speed_mps=15.0 if vehicle_class == "fixed-wing" else 0.0,
        airspeed_mps=17.0 if vehicle_class == "fixed-wing" else 0.0,
        wind_north_mps=2.0,
        wind_east_mps=-1.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _geometry_and_policy(
    vehicle_class: str,
) -> tuple[
    VtolMissionGeometry | FixedWingMissionGeometry | MultirotorMissionGeometry,
    Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
]:
    home = GeoPoint(1.3000, 103.8000)
    if vehicle_class == "vtol":
        geometry = VtolMissionGeometry(
            home=home,
            waypoints=(
                GeoPoint(1.3004, 103.8002, 45.0, "relative_home"),
                GeoPoint(1.3008, 103.8004, 50.0, "relative_home"),
            ),
            takeoff_altitude_m=30.0,
            loiter_time_s=20.0,
            loiter_radius_m=35.0,
            acceptance_radius_m=5.0,
            landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        )
        policy = Px4MissionSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=2_000.0,
            allowed_airframe_profile_ids=("px4_vtol_gazebo_v1",),
            minimum_relative_altitude_m=10.0,
            maximum_relative_altitude_m=120.0,
            maximum_mission_radius_m=1_000.0,
            maximum_loiter_radius_m=100.0,
            maximum_landing_offset_m=10.0,
        )
        return geometry, policy
    if vehicle_class == "multirotor":
        geometry = MultirotorMissionGeometry(
            home=home,
            waypoints=(
                GeoPoint(1.3004, 103.8002, 35.0, "relative_home"),
                GeoPoint(1.3008, 103.8004, 40.0, "relative_home"),
            ),
            takeoff_altitude_m=30.0,
            loiter_time_s=15.0,
            loiter_radius_m=25.0,
            acceptance_radius_m=5.0,
            landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        )
        policy = Px4MultirotorSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=2_000.0,
            allowed_airframe_profile_ids=("px4_multirotor_gazebo_v1",),
            minimum_relative_altitude_m=10.0,
            maximum_relative_altitude_m=100.0,
            maximum_mission_radius_m=1_000.0,
            maximum_loiter_radius_m=100.0,
            maximum_landing_offset_m=10.0,
        )
        return geometry, policy
    geometry = FixedWingMissionGeometry(
        home=home,
        waypoints=(
            GeoPoint(1.3010, 103.8000, 60.0, "relative_home"),
            GeoPoint(1.3015, 103.8010, 60.0, "relative_home"),
        ),
        landing_approach=GeoPoint(1.3000, 103.8020, 40.0, "relative_home"),
        landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        takeoff_altitude_m=35.0,
        acceptance_radius_m=8.0,
    )
    policy = Px4FixedWingSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=6_000.0,
        allowed_airframe_profile_ids=("px4_fixed_wing_gazebo_v1",),
    )
    return geometry, policy


@pytest.mark.parametrize("vehicle_class", ("vtol", "fixed-wing", "multirotor"))
def test_planner_output_flows_through_canonical_px4_compiler(vehicle_class: str) -> None:
    task = _task()
    context = _context(vehicle_class)
    geometry, policy = _geometry_and_policy(vehicle_class)

    result = compile_planned_task_mission(
        task,
        context,
        vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
        mission_id=f"{task.task_id}-{vehicle_class}",
    )

    assert result.vehicle_class == vehicle_class
    assert result.plan == plan_task(task, context, vehicle_class)
    assert result.estimate.task_id == task.task_id
    assert (
        result.compiled_mission.executable_mission.platform_plan_hash == result.plan.canonical_hash
    )
    assert (
        result.executable_mission_hash == result.compiled_mission.executable_mission.canonical_hash
    )
    assert result.wire_hash == result.compiled_mission.wire_hash


def test_planned_compilation_rejects_cross_bound_artifacts() -> None:
    task = _task()
    context = _context("multirotor")
    geometry, policy = _geometry_and_policy("multirotor")
    result = compile_planned_task_mission(
        task,
        context,
        "multirotor",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )

    with pytest.raises(ValueError, match="compiled mission is not bound"):
        type(result)(
            vehicle_class=result.vehicle_class,
            estimate=result.estimate,
            plan=plan_task(task, replace(context, snapshot_id="different-context"), "multirotor"),
            compiled_mission=result.compiled_mission,
        )


def test_planned_compilation_round_trips_wire_artifact_and_hashes() -> None:
    task = _task()
    context = _context("fixed-wing")
    geometry, policy = _geometry_and_policy("fixed-wing")
    result = compile_planned_task_mission(
        task,
        context,
        "fixed-wing",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )

    reopened = PlannedMissionCompilation.from_mapping(result.to_mapping())

    assert reopened == result
    assert reopened.compiled_mission.wire_items == result.compiled_mission.wire_items


def test_shared_mission_loader_accepts_bare_compiled_and_planner_documents(tmp_path) -> None:
    task = _task()
    context = _context("multirotor")
    geometry, policy = _geometry_and_policy("multirotor")
    result = compile_planned_task_mission(
        task,
        context,
        "multirotor",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )
    mission = result.compiled_mission.executable_mission
    documents = {
        "bare.json": mission.to_mapping(),
        "compiled.json": result.compiled_mission.to_mapping(),
        "planned.json": result.to_mapping(),
    }

    for name, document in documents.items():
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        assert load_executable_mission_document(path) == mission


def test_planned_compilation_rejects_tampered_declared_hash_and_unknown_field() -> None:
    task = _task()
    context = _context("vtol")
    geometry, policy = _geometry_and_policy("vtol")
    result = compile_planned_task_mission(
        task,
        context,
        "vtol",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )

    tampered_hash = result.to_mapping()
    tampered_hash["wire_hash"] = "0" * 64
    with pytest.raises(ValueError, match="wire_hash does not match"):
        PlannedMissionCompilation.from_mapping(tampered_hash)

    unknown_field = result.to_mapping()
    unknown_field["unexpected"] = True
    with pytest.raises(ValueError, match="unknown field"):
        PlannedMissionCompilation.from_mapping(unknown_field)


def test_runtime_compile_adapter_round_trips_geometry_and_policy_for_all_airframes() -> None:
    task = _task()
    for vehicle_class in ("vtol", "fixed-wing", "multirotor"):
        context = _context(vehicle_class)
        geometry, policy = _geometry_and_policy(vehicle_class)
        runtime = {
            "schema_version": MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
            "vehicle_class": vehicle_class,
            **geometry.to_mapping(),
            "safety_policy": policy.to_mapping(),
        }

        parsed_geometry, parsed_policy = parse_mission_compile_runtime(runtime, vehicle_class)
        assert parsed_geometry == geometry
        assert parsed_policy == policy
        result = compile_planned_task_mission_from_runtime(
            task,
            context,
            vehicle_class,
            runtime=runtime,
            airframe_profile_hash=PROFILE_HASH,
        )
        assert result.vehicle_class == vehicle_class
        assert result.wire_hash == result.compiled_mission.wire_hash


def test_runtime_compile_adapter_rejects_mismatched_vehicle_and_truth() -> None:
    geometry, policy = _geometry_and_policy("vtol")
    runtime = {
        "schema_version": MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
        "vehicle_class": "fixed-wing",
        **geometry.to_mapping(),
        "safety_policy": policy.to_mapping(),
    }
    with pytest.raises(ValueError, match="does not match"):
        parse_mission_compile_runtime(runtime, "vtol")

    truth_runtime = {
        **runtime,
        "vehicle_class": "vtol",
        "truth": {"target": "evaluator-only"},
    }
    with pytest.raises(ValueError, match="unknown field"):
        parse_mission_compile_runtime(truth_runtime, "vtol")


def test_mission_compile_cli_writes_reopenable_artifact(tmp_path) -> None:
    task = _task()
    context = _context("fixed-wing")
    geometry, policy = _geometry_and_policy("fixed-wing")
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "context.json"
    runtime_path = tmp_path / "runtime.json"
    output_path = tmp_path / "out" / "compiled.json"
    task_path.write_text(json.dumps(task.to_mapping()), encoding="utf-8")
    context_path.write_text(json.dumps(context.to_mapping()), encoding="utf-8")
    runtime_path.write_text(
        json.dumps(
            {
                "schema_version": MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
                "vehicle_class": "fixed-wing",
                **geometry.to_mapping(),
                "safety_policy": policy.to_mapping(),
            }
        ),
        encoding="utf-8",
    )

    assert (
        mission_compile_cli_main(
            [
                "--task",
                str(task_path),
                "--context",
                str(context_path),
                "--runtime",
                str(runtime_path),
                "--vehicle-class",
                "fixed_wing",
                "--airframe-profile-hash",
                PROFILE_HASH,
                "--mission-id",
                "cli-fixed-wing-001",
                "--out",
                str(output_path),
            ]
        )
        == 0
    )
    reopened = PlannedMissionCompilation.from_mapping(
        json.loads(output_path.read_text(encoding="utf-8"))
    )
    assert reopened.compiled_mission.executable_mission.mission_id == "cli-fixed-wing-001"
    assert reopened.vehicle_class == "fixed-wing"


def test_policy_mapping_rejects_unknown_fields() -> None:
    _geometry, policy = _geometry_and_policy("multirotor")
    raw = policy.to_mapping()
    raw["unexpected"] = True
    with pytest.raises(ValueError, match="unknown field"):
        Px4MultirotorSafetyPolicy.from_mapping(raw)
