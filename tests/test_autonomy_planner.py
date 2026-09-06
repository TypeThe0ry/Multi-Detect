from __future__ import annotations

import json
from dataclasses import replace

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    PlannerError,
    PlanningEstimate,
    PlatformNeutralPlan,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    estimate_task,
    normalize_vehicle_class,
    plan_task,
)
from multidetect.autonomy.planner_cli import main as planner_cli_main


def _context(**changes: object) -> ContextSnapshot:
    context = ContextSnapshot(
        snapshot_id="planner-context-001",
        captured_at_s=10.0,
        captured_at_utc="2026-09-04T00:00:00Z",
        aircraft_position=GeoPoint(1.3, 103.8),
        home_position=GeoPoint(1.3, 103.8),
        airframe_profile_id="px4_vtol_gazebo_v1",
        battery_remaining_pct=86.0,
        estimated_flight_time_s=2100.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.4,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )
    return replace(context, **changes)


def _task(
    task_type: TaskType = TaskType.CORRIDOR_VISUAL_SEARCH,
    *,
    on_detection: tuple[PlanAction, ...] = (
        PlanAction.OBSERVE_TARGET,
        PlanAction.CAPTURE_EVIDENCE,
        PlanAction.REPORT_GEOLOCATION,
    ),
    **changes: object,
) -> ResolvedTaskSpec:
    task = ResolvedTaskSpec(
        task_id="river-search-001",
        task_type=task_type,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="river-north-001",
            direction="northbound",
            length_m=2_000.0,
            buffer_m=35.0,
            centerline=(GeoPoint(1.3, 103.8), GeoPoint(1.3085, 103.8012)),
            resolution_confidence=0.94,
        ),
        target_classes=("person_in_water", "red_life_jacket"),
        on_detection=on_detection,
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )
    return replace(task, **changes)


@pytest.mark.parametrize(
    ("task_type", "primary_action", "on_detection"),
    (
        (TaskType.AREA_SEARCH, PlanAction.SEARCH_AREA, (PlanAction.OBSERVE_TARGET,)),
        (
            TaskType.CORRIDOR_VISUAL_SEARCH,
            PlanAction.SEARCH_CORRIDOR,
            (PlanAction.OBSERVE_TARGET, PlanAction.CAPTURE_EVIDENCE),
        ),
        (TaskType.TARGET_OBSERVATION, PlanAction.OBSERVE_TARGET, (PlanAction.CAPTURE_EVIDENCE,)),
        (TaskType.SURVEY, PlanAction.SURVEY, ()),
    ),
)
def test_plan_covers_all_task_types(
    task_type: TaskType,
    primary_action: PlanAction,
    on_detection: tuple[PlanAction, ...],
) -> None:
    plan = plan_task(_task(task_type, on_detection=on_detection), _context(), "vtol")

    assert tuple(step.sequence for step in plan.steps) == tuple(range(len(plan.steps)))
    assert [step.action for step in plan.steps] == [
        PlanAction.TAKEOFF,
        PlanAction.TRANSIT,
        primary_action,
        *on_detection,
        PlanAction.RETURN,
        PlanAction.LAND,
    ]
    assert plan.plan_id == "river-search-001:platform-neutral-v1"
    assert plan.task_spec_hash == _task(task_type, on_detection=on_detection).canonical_hash


def test_plan_is_byte_identical_across_airframe_classes() -> None:
    task = _task()
    context = _context()
    plans = [plan_task(task, context, vehicle) for vehicle in ("vtol", "fixed-wing", "multirotor")]

    assert plans[0].steps == plans[1].steps == plans[2].steps
    assert plans[0].canonical_hash == plans[1].canonical_hash == plans[2].canonical_hash


def test_estimate_uses_wgs84_polyline_and_adverse_wind() -> None:
    calm = estimate_task(_task(), _context(), "fixed-wing")
    windy = estimate_task(_task(), _context(wind_north_mps=2.0, wind_east_mps=2.0), "fixed-wing")

    assert calm.task_distance_m >= 2_000.0
    assert calm.total_distance_m == pytest.approx(
        calm.outbound_distance_m + calm.task_distance_m + calm.return_distance_m
    )
    understated_task = replace(
        _task(),
        geometry=replace(_task().geometry, length_m=100.0),
    )
    understated_estimate = estimate_task(understated_task, _context(), "fixed-wing")
    plan = plan_task(understated_task, _context(), "fixed-wing")
    task_step = next(step for step in plan.steps if step.action is PlanAction.SEARCH_CORRIDOR)
    assert understated_estimate.task_distance_m > understated_task.geometry.length_m
    assert task_step.quantity == pytest.approx(understated_estimate.task_distance_m)
    assert windy.cruise_speed_mps < calm.cruise_speed_mps
    assert windy.estimated_duration_s > calm.estimated_duration_s


@pytest.mark.parametrize(
    ("token", "expected"),
    (
        ("VTOL", "vtol"),
        ("fixed_wing", "fixed-wing"),
        ("airplane", "fixed-wing"),
        ("x500", "multirotor"),
    ),
)
def test_vehicle_aliases_are_shared_and_strict(token: str, expected: str) -> None:
    assert normalize_vehicle_class(token) == expected

    with pytest.raises(PlannerError, match="vehicle_class"):
        normalize_vehicle_class("unknown-airframe")


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"gps_fix_type": 2}, "GPS fix"),
        ({"gps_horizontal_accuracy_m": 7.0}, "GPS horizontal accuracy"),
        ({"geofence_valid": False}, "geofence"),
        ({"camera_available": False}, "camera"),
        ({"perception_available": False}, "perception"),
        ({"map_cache_available": False}, "map cache"),
        ({"dem_available": False}, "DEM"),
        ({"battery_remaining_pct": 45.0}, "battery"),
        ({"estimated_flight_time_s": 0.0}, "estimated flight time"),
    ),
)
def test_planner_gates_fail_closed(changes: dict[str, object], message: str) -> None:
    with pytest.raises(PlannerError, match=message):
        plan_task(_task(), _context(**changes), "multirotor")


def test_planner_rejects_unresolved_low_confidence_and_bad_actions() -> None:
    with pytest.raises(PlannerError, match="unresolved assumptions"):
        plan_task(_task(unresolved_assumptions=("choose feature",)), _context(), "vtol")

    low_confidence = replace(
        _task(),
        geometry=replace(_task().geometry, resolution_confidence=0.2),
    )
    with pytest.raises(PlannerError, match="geometry confidence"):
        plan_task(low_confidence, _context(), "vtol")

    bad_action = _task(on_detection=(PlanAction.RETURN,))
    with pytest.raises(PlannerError, match="on_detection action"):
        plan_task(bad_action, _context(), "vtol")

    duplicate_action = _task(on_detection=(PlanAction.OBSERVE_TARGET, PlanAction.OBSERVE_TARGET))
    with pytest.raises(PlannerError, match="unique"):
        plan_task(duplicate_action, _context(), "vtol")


def test_planner_enforces_time_and_energy_budget() -> None:
    with pytest.raises(PlannerError, match="maximum_duration"):
        plan_task(
            replace(
                _task(),
                constraints=TaskConstraints(40.0, 100.0, True),
            ),
            _context(),
            "fixed-wing",
        )

    with pytest.raises(PlannerError, match="energy budget"):
        plan_task(_task(), _context(battery_remaining_pct=51.0), "multirotor")


def test_land_completion_does_not_add_return_step() -> None:
    task = replace(_task(), completion_action=PlanAction.LAND)
    plan = plan_task(task, _context(), "multirotor")
    estimate = estimate_task(task, _context(), "multirotor")

    assert plan.steps[-1].action is PlanAction.LAND
    assert all(step.action is not PlanAction.RETURN for step in plan.steps)
    assert estimate.return_distance_m == 0.0


def test_planner_cli_writes_schema_compatible_plan_and_estimate(tmp_path) -> None:
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "context.json"
    plan_path = tmp_path / "out" / "plan.json"
    estimate_path = tmp_path / "out" / "estimate.json"
    task_path.write_text(
        json.dumps(_task().to_mapping(), ensure_ascii=False),
        encoding="utf-8",
    )
    context_path.write_text(
        json.dumps(_context().to_mapping()),
        encoding="utf-8",
    )

    assert (
        planner_cli_main(
            [
                "--task",
                str(task_path),
                "--context",
                str(context_path),
                "--vehicle-class",
                "fixed_wing",
                "--out",
                str(plan_path),
                "--estimate-out",
                str(estimate_path),
            ]
        )
        == 0
    )
    plan_mapping = json.loads(plan_path.read_text(encoding="utf-8"))
    estimate_mapping = json.loads(estimate_path.read_text(encoding="utf-8"))
    parsed_plan = PlatformNeutralPlan.from_mapping(plan_mapping)
    parsed_estimate = PlanningEstimate.from_mapping(estimate_mapping)
    assert parsed_plan.plan_id == "river-search-001:platform-neutral-v1"
    assert plan_mapping["task_spec_hash"] == _task().canonical_hash
    assert parsed_estimate.vehicle_class == "fixed-wing"


def test_planning_estimate_rejects_unknown_fields() -> None:
    estimate = estimate_task(_task(), _context(), "fixed-wing")
    raw = estimate.to_mapping()
    raw["unexpected"] = True
    with pytest.raises(ValueError, match="unknown field"):
        PlanningEstimate.from_mapping(raw)


def test_planner_cli_returns_nonzero_for_stale_context(tmp_path, capsys) -> None:
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "context.json"
    task_path.write_text(
        json.dumps(_task().to_mapping(), ensure_ascii=False),
        encoding="utf-8",
    )
    context_path.write_text(
        json.dumps(_context(gps_fix_type=2).to_mapping()),
        encoding="utf-8",
    )

    assert (
        planner_cli_main(
            [
                "--task",
                str(task_path),
                "--context",
                str(context_path),
                "--vehicle-class",
                "vtol",
            ]
        )
        == 2
    )
    assert "GPS fix" in capsys.readouterr().err
