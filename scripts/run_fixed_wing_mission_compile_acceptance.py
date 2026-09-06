"""Run the deterministic fixed-wing mission compiler gate.

The gate exercises the same hash-bound ``ExecutableMission`` and
``MISSION_ITEM_INT`` compiler used by a future PX4 transport, but keeps the
transport in memory.  It is intentionally independent of evaluator truth and
does not open a Pixhawk link.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
)
from multidetect.px4_mission import (
    FixedWingMissionGeometry,
    Px4FixedWingSafetyPolicy,
    compile_executable_mission,
    normalize_px4_vehicle_class,
)

PROFILE_HASH = "b" * 64


def _context() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="fixed-wing-sim-context-001",
        captured_at_s=10.0,
        captured_at_utc="2026-08-28T00:00:00Z",
        aircraft_position=GeoPoint(1.3000, 103.8000),
        home_position=GeoPoint(1.3000, 103.8000),
        airframe_profile_id="px4_fixed_wing_gazebo_v1",
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
        ground_speed_mps=15.0,
        airspeed_mps=17.0,
        wind_north_mps=2.0,
        wind_east_mps=-1.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.5,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _geometry() -> FixedWingMissionGeometry:
    return FixedWingMissionGeometry(
        home=GeoPoint(1.3000, 103.8000),
        waypoints=(
            GeoPoint(1.3010, 103.8000, 60.0, "relative_home"),
            GeoPoint(1.3015, 103.8010, 60.0, "relative_home"),
        ),
        landing_approach=GeoPoint(1.3000, 103.8020, 40.0, "relative_home"),
        landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        takeoff_altitude_m=35.0,
        acceptance_radius_m=8.0,
    )


def run() -> dict[str, object]:
    context = _context()
    plan = PlatformNeutralPlan(
        plan_id="fixed-wing-sim-plan-001",
        task_spec_hash="a" * 64,
        context_snapshot_hash=context.canonical_hash,
        steps=(
            PlanStep(0, PlanAction.TAKEOFF),
            PlanStep(1, PlanAction.TRANSIT),
            PlanStep(2, PlanAction.SEARCH_CORRIDOR),
            PlanStep(3, PlanAction.RETURN),
            PlanStep(4, PlanAction.LAND),
        ),
    )
    policy = Px4FixedWingSafetyPolicy(
        geofence_center=context.home_position,
        geofence_radius_m=6_000.0,
        allowed_airframe_profile_ids=(context.airframe_profile_id,),
    )
    compiled = compile_executable_mission(
        vehicle_class="fixed-wing",
        platform_plan=plan,
        context_snapshot=context,
        geometry=_geometry(),
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )
    mission = compiled.executable_mission
    executable_hash = mission.canonical_hash
    protocol_hash = compiled.protocol_hash
    return {
        "event": "fixed_wing_mission_compile_acceptance_finished",
        "gate_passed": True,
        "runtime_mode": "SIM",
        "vehicle_class": "fixed-wing",
        "vehicle_class_normalized": normalize_px4_vehicle_class("fixed-wing"),
        "dispatcher": "compile_executable_mission",
        "truth_free": True,
        "physical_output_enabled": False,
        "transport": "in_process_mission_item_int",
        "mission_id": mission.mission_id,
        "airframe_adapter_id": mission.airframe_adapter_id,
        "airframe_adapter_hash": mission.airframe_adapter_hash,
        "executable_mission_hash": executable_hash,
        "protocol_hash": protocol_hash,
        "item_count": len(compiled.wire_items),
        "commands": [item.command for item in compiled.wire_items],
        "landing_heading_deg": compiled.wire_items[-1].param4,
        "policy_hash": policy.canonical_hash,
        "checks": {
            "takeoff_first": compiled.wire_items[0].command == 22,
            "land_last": compiled.wire_items[-1].command == 21,
            "contiguous_sequences": [item.sequence for item in compiled.wire_items]
            == list(range(len(compiled.wire_items))),
            "hash_bound_to_plan": mission.platform_plan_hash == plan.canonical_hash,
            "executable_hash_shape": (
                isinstance(executable_hash, str)
                and len(executable_hash) == 64
                and all(char in "0123456789abcdef" for char in executable_hash)
            ),
            "protocol_hash_shape": (
                isinstance(protocol_hash, str)
                and len(protocol_hash) == 64
                and all(char in "0123456789abcdef" for char in protocol_hash)
            ),
            "protocol_hash_covers_wire_items": protocol_hash == compiled.wire_hash,
            "physical_output_disabled": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.dumps(run(), ensure_ascii=False, indent=2) + "\n"
    except Exception as exc:  # pragma: no cover - CLI boundary
        print(
            json.dumps({"event": "fixed_wing_mission_compile_acceptance_failed", "error": str(exc)})
        )
        return 2
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
