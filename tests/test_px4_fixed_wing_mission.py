from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
)
from multidetect.px4_mission import (
    MAV_CMD_NAV_LAND,
    MAV_CMD_NAV_TAKEOFF,
    MAV_CMD_NAV_WAYPOINT,
    PX4_FIXED_WING_ADAPTER_ID,
    FixedWingMissionGeometry,
    Px4FixedWingSafetyPolicy,
    Px4MissionError,
    compile_fixed_wing_executable_mission,
)

HASH_A = "a" * 64
PROFILE_HASH = "b" * 64


def _context(**overrides: object) -> ContextSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "context-fw-001",
        "captured_at_s": 10.0,
        "captured_at_utc": "2026-08-28T00:00:00Z",
        "aircraft_position": GeoPoint(1.3000, 103.8000),
        "home_position": GeoPoint(1.3000, 103.8000),
        "airframe_profile_id": "px4_fixed_wing_gazebo_v1",
        "battery_remaining_pct": 90.0,
        "estimated_flight_time_s": 1800.0,
        "ground_speed_mps": 15.0,
        "airspeed_mps": 17.0,
        "wind_north_mps": 2.0,
        "wind_east_mps": -1.0,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 1.5,
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
        plan_id="river-fw-001",
        task_spec_hash=HASH_A,
        context_snapshot_hash=context.canonical_hash,
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.TRANSIT),
            PlanStep(2, PlanAction.SEARCH_CORRIDOR),
            PlanStep(3, PlanAction.RETURN),
            PlanStep(4, PlanAction.LAND),
        ),
    )


def _geometry(**overrides: object) -> FixedWingMissionGeometry:
    values: dict[str, object] = {
        "home": GeoPoint(1.3000, 103.8000),
        "waypoints": (
            GeoPoint(1.3010, 103.8000, 60.0, "relative_home"),
            GeoPoint(1.3015, 103.8010, 60.0, "relative_home"),
        ),
        "landing_approach": GeoPoint(1.3000, 103.8020, 40.0, "relative_home"),
        "landing": GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        "takeoff_altitude_m": 35.0,
        "acceptance_radius_m": 8.0,
    }
    values.update(overrides)
    return FixedWingMissionGeometry(**values)  # type: ignore[arg-type]


def _policy(**overrides: object) -> Px4FixedWingSafetyPolicy:
    values: dict[str, object] = {
        "geofence_center": GeoPoint(1.3000, 103.8000),
        "geofence_radius_m": 6_000.0,
        "allowed_airframe_profile_ids": ("px4_fixed_wing_gazebo_v1",),
    }
    values.update(overrides)
    return Px4FixedWingSafetyPolicy(**values)  # type: ignore[arg-type]


def test_fixed_wing_compile_emits_takeoff_waypoints_and_aligned_land() -> None:
    context = _context()
    compiled = compile_fixed_wing_executable_mission(
        platform_plan=_plan(context),
        context_snapshot=context,
        geometry=_geometry(),
        policy=_policy(),
        airframe_profile_hash=PROFILE_HASH,
    )

    mission = compiled.executable_mission
    assert mission.airframe_adapter_id == PX4_FIXED_WING_ADAPTER_ID
    assert [item.command.value for item in mission.items] == [
        "MAV_CMD_NAV_TAKEOFF",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_LAND",
    ]
    assert [item.command for item in compiled.wire_items] == [
        MAV_CMD_NAV_TAKEOFF,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_WAYPOINT,
        MAV_CMD_NAV_LAND,
    ]
    assert compiled.wire_hash == compiled.protocol_hash
    assert compiled.wire_items[-1].param4 == pytest.approx(270.0, abs=2.0)


def test_fixed_wing_compile_is_deterministic_and_binds_plan_hash() -> None:
    context = _context()
    kwargs = {
        "platform_plan": _plan(context),
        "context_snapshot": context,
        "geometry": _geometry(),
        "policy": _policy(),
        "airframe_profile_hash": PROFILE_HASH,
    }
    first = compile_fixed_wing_executable_mission(**kwargs)
    second = compile_fixed_wing_executable_mission(**kwargs)
    assert first.executable_mission.canonical_hash == second.executable_mission.canonical_hash
    assert first.wire_hash == second.wire_hash
    assert first.executable_mission.platform_plan_hash == kwargs["platform_plan"].canonical_hash


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("geofence_valid", False, "invalid geofence"),
        ("gps_fix_type", 2, "GPS fix"),
        ("gps_horizontal_accuracy_m", 50.0, "GPS accuracy"),
        ("battery_remaining_pct", 20.0, "battery"),
        ("estimated_flight_time_s", 10.0, "energy"),
    ),
)
def test_fixed_wing_preflight_fails_closed(field: str, value: object, message: str) -> None:
    context = _context(**{field: value})
    with pytest.raises(Px4MissionError, match=message):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=_geometry(),
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_fixed_wing_rejects_crosswind_and_tailwind_on_approach() -> None:
    context = _context(wind_north_mps=0.0, wind_east_mps=-20.0)
    with pytest.raises(Px4MissionError, match="landing tailwind|landing crosswind"):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=_geometry(),
            policy=_policy(maximum_tailwind_mps=1.0, maximum_crosswind_mps=1.0),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_fixed_wing_rejects_short_approach_and_bad_glide_slope() -> None:
    context = _context()
    short = _geometry(
        landing_approach=GeoPoint(1.3000, 103.8010, 40.0, "relative_home")
    )
    with pytest.raises(Px4MissionError, match="approach distance"):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=short,
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )

    steep = _geometry(
        landing_approach=GeoPoint(1.3000, 103.8015, 120.0, "relative_home")
    )
    with pytest.raises(Px4MissionError, match="glide slope"):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=steep,
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_fixed_wing_rejects_hairpin_turn_and_misaligned_heading() -> None:
    context = _context()
    hairpin = _geometry(
        waypoints=(
            GeoPoint(1.3010, 103.8000, 60.0, "relative_home"),
            GeoPoint(1.3000, 103.8000, 60.0, "relative_home"),
        )
    )
    with pytest.raises(Px4MissionError, match="turn angle|turn-radius"):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=hairpin,
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )

    with pytest.raises(Px4MissionError, match="landing heading"):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=_geometry(landing_heading_deg=90.0),
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_fixed_wing_runtime_mapping_requires_approach_and_rejects_truth() -> None:
    raw = {
        "home_position": {"latitude_deg": 1.3, "longitude_deg": 103.8},
        "mission_geometry": {
            "waypoints": [
                {
                    "latitude_deg": 1.301,
                    "longitude_deg": 103.8,
                    "altitude_m": 60.0,
                    "altitude_reference": "relative_home",
                }
            ],
            "landing_approach": {
                "latitude_deg": 1.3,
                "longitude_deg": 103.802,
                "altitude_m": 40.0,
                "altitude_reference": "relative_home",
            },
            "landing": {
                "latitude_deg": 1.3,
                "longitude_deg": 103.8,
                "altitude_m": 0.0,
                "altitude_reference": "relative_home",
            },
            "takeoff_altitude_m": 35.0,
            "acceptance_radius_m": 8.0,
        },
    }
    geometry = FixedWingMissionGeometry.from_runtime_mapping(raw)
    assert geometry.landing_approach.altitude_m == 40.0
    with pytest.raises(Px4MissionError, match="evaluator-only"):
        FixedWingMissionGeometry.from_runtime_mapping({**raw, "truth": {"target": "secret"}})


def test_fixed_wing_plan_requires_return_or_divert() -> None:
    context = _context()
    plan = replace(
        _plan(context),
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.SEARCH_CORRIDOR),
            PlanStep(2, PlanAction.LAND),
        ),
    )
    with pytest.raises(Px4MissionError, match="RETURN or DIVERT"):
        compile_fixed_wing_executable_mission(
            platform_plan=plan,
            context_snapshot=context,
            geometry=_geometry(),
            policy=_policy(),
            airframe_profile_hash=PROFILE_HASH,
        )


def test_fixed_wing_policy_rejects_impossible_alignment_limit() -> None:
    with pytest.raises(Px4MissionError, match="landing_alignment"):
        _policy(maximum_landing_alignment_deg=91.0)


def test_fixed_wing_rejects_landing_far_from_home() -> None:
    context = _context()
    geometry = _geometry(
        landing=GeoPoint(1.3050, 103.8000, 0.0, "relative_home"),
    )
    with pytest.raises(Px4MissionError, match="landing point is too far"):
        compile_fixed_wing_executable_mission(
            platform_plan=_plan(context),
            context_snapshot=context,
            geometry=geometry,
            policy=_policy(maximum_landing_offset_m=100.0),
            airframe_profile_hash=PROFILE_HASH,
        )
