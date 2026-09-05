from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    MissionCommand,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
)
from multidetect.px4_mission import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_LOITER_TIME,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_NAV_WAYPOINT,
    PX4_MULTIROTOR_ADAPTER_HASH,
    PX4_MULTIROTOR_ADAPTER_ID,
    MultirotorMissionGeometry,
    Px4MissionError,
    Px4MultirotorSafetyPolicy,
    compile_executable_mission,
    compile_multirotor_executable_mission,
    normalize_px4_vehicle_class,
)

PROFILE_HASH = "b" * 64
TASK_HASH = "a" * 64


def _context(**overrides: object) -> ContextSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "context-multirotor-001",
        "captured_at_s": 10.0,
        "captured_at_utc": "2026-08-31T00:00:00Z",
        "aircraft_position": GeoPoint(1.3000, 103.8000),
        "home_position": GeoPoint(1.3000, 103.8000),
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "battery_remaining_pct": 90.0,
        "estimated_flight_time_s": 1_800.0,
        "ground_speed_mps": 0.0,
        "airspeed_mps": 0.0,
        "wind_north_mps": 2.0,
        "wind_east_mps": -1.0,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 1.0,
        "geofence_valid": True,
        "camera_available": True,
        "perception_available": True,
        "map_cache_available": True,
        "dem_available": True,
    }
    values.update(overrides)
    return ContextSnapshot(**values)  # type: ignore[arg-type]


def _plan(context: ContextSnapshot) -> PlatformNeutralPlan:
    return PlatformNeutralPlan(
        plan_id="urban-multirotor-001",
        task_spec_hash=TASK_HASH,
        context_snapshot_hash=context.canonical_hash,
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.TRANSIT),
            PlanStep(2, PlanAction.SEARCH_AREA),
            PlanStep(3, PlanAction.OBSERVE_TARGET),
            PlanStep(4, PlanAction.RETURN),
            PlanStep(5, PlanAction.LAND),
        ),
    )


def _geometry(**overrides: object) -> MultirotorMissionGeometry:
    values: dict[str, object] = {
        "home": GeoPoint(1.3000, 103.8000),
        "waypoints": (
            GeoPoint(1.3004, 103.8002, 35.0, "relative_home"),
            GeoPoint(1.3008, 103.8004, 40.0, "relative_home"),
        ),
        "takeoff_altitude_m": 30.0,
        "loiter_time_s": 15.0,
        "loiter_radius_m": 25.0,
        "acceptance_radius_m": 5.0,
        "landing": GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
    }
    values.update(overrides)
    return MultirotorMissionGeometry(**values)  # type: ignore[arg-type]


def _policy(**overrides: object) -> Px4MultirotorSafetyPolicy:
    values: dict[str, object] = {
        "geofence_center": GeoPoint(1.3000, 103.8000),
        "geofence_radius_m": 2_000.0,
        "allowed_airframe_profile_ids": ("px4_multirotor_gazebo_v1",),
        "minimum_relative_altitude_m": 10.0,
        "maximum_relative_altitude_m": 100.0,
        "maximum_mission_radius_m": 1_000.0,
        "maximum_loiter_radius_m": 100.0,
        "maximum_landing_offset_m": 10.0,
    }
    values.update(overrides)
    return Px4MultirotorSafetyPolicy(**values)  # type: ignore[arg-type]


def test_multirotor_compiler_uses_shared_int_transaction_path() -> None:
    context = _context()
    compiled = compile_multirotor_executable_mission(
        platform_plan=_plan(context),
        context_snapshot=context,
        geometry=_geometry(),
        policy=_policy(),
        airframe_profile_hash=PROFILE_HASH,
    )

    mission = compiled.executable_mission
    assert mission.airframe_adapter_id == PX4_MULTIROTOR_ADAPTER_ID
    assert mission.airframe_adapter_hash == PX4_MULTIROTOR_ADAPTER_HASH
    assert [item.command for item in mission.items] == [
        MissionCommand.TAKEOFF,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
        MissionCommand.LAND,
    ]
    assert [item.current for item in mission.items] == [1, 0, 0, 0, 0]
    assert mission.items[-2].param1 == 15.0
    assert mission.items[-2].param3 == 25.0
    assert [item.command for item in compiled.wire_items] == [
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_LOITER_TIME,
        MAV_CMD_NAV_LAND,
    ]
    assert compiled.wire_items[0].to_send_args()[:7] == (1, 1, 0, 6, 22, 1, 1)
    assert compiled.wire_hash == compiled.protocol_hash


@pytest.mark.parametrize(
    ("value", "expected"),
    (("x500", "multirotor"), ("multicopter", "multirotor"), ("multi_rotor", "multirotor")),
)
def test_px4_vehicle_dispatcher_normalizes_simulator_aliases(value: str, expected: str) -> None:
    assert normalize_px4_vehicle_class(value) == expected
    context = _context()
    compiled = compile_executable_mission(
        vehicle_class=value,
        platform_plan=_plan(context),
        context_snapshot=context,
        geometry=_geometry(),
        policy=_policy(),
        airframe_profile_hash=PROFILE_HASH,
    )
    assert compiled.executable_mission.airframe_adapter_id == PX4_MULTIROTOR_ADAPTER_ID


def test_px4_vehicle_dispatcher_rejects_cross_vehicle_geometry() -> None:
    context = _context()
    with pytest.raises(Px4MissionError, match="VtolMissionGeometry"):
        compile_executable_mission(
            vehicle_class="vtol",
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=_geometry(),
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_px4_vehicle_dispatcher_rejects_unknown_class() -> None:
    context = _context()
    with pytest.raises(Px4MissionError, match="vehicle_class"):
        compile_executable_mission(
            vehicle_class="hoverboard",
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=_geometry(),
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_multirotor_runtime_geometry_is_deterministic_and_rejects_truth() -> None:
    context = _context()
    kwargs = {
        "platform_plan": _plan(context),
        "context_snapshot": context,
        "geometry": _geometry(),
        "policy": _policy(),
        "airframe_profile_hash": PROFILE_HASH,
    }
    first = compile_multirotor_executable_mission(**kwargs)
    second = compile_multirotor_executable_mission(**kwargs)
    assert first.executable_mission.canonical_hash == second.executable_mission.canonical_hash
    assert first.wire_hash == second.wire_hash

    raw = {
        "home_position": {"latitude_deg": 1.3, "longitude_deg": 103.8},
        "mission_geometry": {
            "waypoints": [
                {
                    "latitude_deg": 1.3004,
                    "longitude_deg": 103.8002,
                    "altitude_m": 35.0,
                    "altitude_reference": "relative_home",
                }
            ],
            "takeoff_altitude_m": 30.0,
            "loiter_time_s": 10.0,
            "loiter_radius_m": 20.0,
            "acceptance_radius_m": 5.0,
        },
    }
    parsed = MultirotorMissionGeometry.from_runtime_mapping(raw)
    assert parsed.waypoints[0].altitude_m == 35.0
    with pytest.raises(Px4MissionError, match="evaluator-only"):
        MultirotorMissionGeometry.from_runtime_mapping({**raw, "truth": {"secret": True}})


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("geofence", "invalid geofence"),
        ("airframe", "airframe profile"),
        ("altitude", "altitude"),
        ("loiter", "loiter radius"),
        ("landing", "landing point is too far"),
        ("plan", "platform plan must end"),
    ),
)
def test_multirotor_preflight_fails_closed(mutation: str, message: str) -> None:
    context = _context()
    plan = _plan(context)
    geometry = _geometry()
    policy = _policy()
    if mutation == "geofence":
        context = _context(geofence_valid=False)
        plan = _plan(context)
    elif mutation == "airframe":
        context = _context(airframe_profile_id="px4_fixed_wing_gazebo_v1")
        plan = _plan(context)
    elif mutation == "altitude":
        geometry = replace(geometry, takeoff_altitude_m=150.0)
    elif mutation == "loiter":
        geometry = replace(geometry, loiter_radius_m=120.0)
    elif mutation == "landing":
        geometry = replace(
            geometry,
            landing=GeoPoint(1.3010, 103.8000, 0.0, "relative_home"),
        )
        policy = _policy(maximum_landing_offset_m=100.0)
    elif mutation == "plan":
        plan = replace(
            plan,
            steps=(PlanStep(0, PlanAction.TAKEOFF), PlanStep(1, PlanAction.RETURN)),
        )

    with pytest.raises(Px4MissionError, match=message):
        compile_multirotor_executable_mission(
            platform_plan=plan,
            context_snapshot=context,
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=PROFILE_HASH,
        )
