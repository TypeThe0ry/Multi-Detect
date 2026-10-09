"""Run the shared journalized PX4 Mission path for VTOL, fixed-wing and multirotor.

This is a deterministic software/SIM gate.  It compiles runtime-channel
fixtures with the public ``compile_executable_mission`` dispatcher and then
drives every airframe through the same upload, ``MISSION_REQUEST_INT``, ACK,
readback, commit and start-permit transaction.  No MAVLink socket or physical
output is opened.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# ruff: noqa: E402 -- repository-local CLI bootstrap before package imports
_REPOSITORY_SOURCE = Path(__file__).resolve().parents[1] / "src"
if _REPOSITORY_SOURCE.is_dir() and str(_REPOSITORY_SOURCE) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_SOURCE))

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
)
from multidetect.product_version import PRODUCT_VERSION
from multidetect.px4_mission import (
    FixedWingMissionGeometry,
    MultirotorMissionGeometry,
    Px4FixedWingSafetyPolicy,
    Px4MissionSafetyPolicy,
    Px4MultirotorSafetyPolicy,
    VtolMissionGeometry,
    compile_executable_mission,
)
from multidetect.px4_mission_acceptance import (
    run_px4_mission_transaction_acceptance,
    verify_px4_mission_transaction_acceptance,
)

_PROFILE_HASH = "b" * 64
_HOME = GeoPoint(1.3000, 103.8000)
_NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)
_VEHICLE_CLASSES = ("vtol", "fixed-wing", "multirotor")


def _context(vehicle_class: str) -> ContextSnapshot:
    profile = {
        "vtol": "px4_vtol_gazebo_v1",
        "fixed-wing": "px4_fixed_wing_gazebo_v1",
        "multirotor": "px4_multirotor_gazebo_v1",
    }[vehicle_class]
    cruise = vehicle_class == "fixed-wing"
    return ContextSnapshot(
        snapshot_id=f"{vehicle_class}-transaction-context-001",
        captured_at_s=10.0,
        captured_at_utc="2026-09-01T08:00:00Z",
        aircraft_position=_HOME,
        home_position=_HOME,
        airframe_profile_id=profile,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
        ground_speed_mps=15.0 if cruise else 0.0,
        airspeed_mps=17.0 if cruise else 0.0,
        wind_north_mps=2.0,
        wind_east_mps=-1.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0 if vehicle_class != "fixed-wing" else 1.5,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _plan(vehicle_class: str, context: ContextSnapshot) -> PlatformNeutralPlan:
    actions = {
        "vtol": (
            PlanAction.TAKEOFF,
            PlanAction.TRANSIT,
            PlanAction.SEARCH_CORRIDOR,
            PlanAction.LAND,
        ),
        "fixed-wing": (
            PlanAction.TAKEOFF,
            PlanAction.TRANSIT,
            PlanAction.SEARCH_CORRIDOR,
            PlanAction.RETURN,
            PlanAction.LAND,
        ),
        "multirotor": (
            PlanAction.TAKEOFF,
            PlanAction.TRANSIT,
            PlanAction.SEARCH_AREA,
            PlanAction.OBSERVE_TARGET,
            PlanAction.RETURN,
            PlanAction.LAND,
        ),
    }[vehicle_class]
    return PlatformNeutralPlan(
        plan_id=f"{vehicle_class}-shared-transaction-plan-001",
        task_spec_hash="a" * 64,
        context_snapshot_hash=context.canonical_hash,
        steps=tuple(PlanStep(index, action) for index, action in enumerate(actions)),
    )


def _geometry_and_policy(
    vehicle_class: str,
    context: ContextSnapshot,
) -> tuple[
    VtolMissionGeometry | FixedWingMissionGeometry | MultirotorMissionGeometry,
    Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
]:
    if vehicle_class == "vtol":
        geometry = VtolMissionGeometry(
            home=_HOME,
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
            geofence_center=_HOME,
            geofence_radius_m=2_000.0,
            allowed_airframe_profile_ids=(context.airframe_profile_id,),
            minimum_relative_altitude_m=10.0,
            maximum_relative_altitude_m=120.0,
            maximum_mission_radius_m=1_000.0,
            maximum_loiter_radius_m=100.0,
            maximum_loiter_time_s=60.0,
            maximum_landing_offset_m=10.0,
            home_match_tolerance_m=2.0,
        )
        return geometry, policy
    if vehicle_class == "fixed-wing":
        geometry = FixedWingMissionGeometry(
            home=_HOME,
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
            geofence_center=_HOME,
            geofence_radius_m=6_000.0,
            allowed_airframe_profile_ids=(context.airframe_profile_id,),
        )
        return geometry, policy
    geometry = MultirotorMissionGeometry(
        home=_HOME,
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
        geofence_center=_HOME,
        geofence_radius_m=2_000.0,
        allowed_airframe_profile_ids=(context.airframe_profile_id,),
        minimum_relative_altitude_m=10.0,
        maximum_relative_altitude_m=100.0,
        maximum_mission_radius_m=1_000.0,
        maximum_loiter_radius_m=100.0,
        maximum_landing_offset_m=10.0,
    )
    return geometry, policy


def _compile_fixture(
    vehicle_class: str,
) -> tuple[
    ContextSnapshot,
    PlatformNeutralPlan,
    VtolMissionGeometry | FixedWingMissionGeometry | MultirotorMissionGeometry,
    Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
]:
    context = _context(vehicle_class)
    plan = _plan(vehicle_class, context)
    geometry, policy = _geometry_and_policy(vehicle_class, context)
    return context, plan, geometry, policy


def run(
    *,
    journal_directory: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    """Run all three airframe fixtures and return one cross-airframe document."""

    resolved_run_id = run_id or f"local-{uuid4().hex[:12]}"
    journal_root = (
        Path(journal_directory).expanduser().resolve()
        if journal_directory is not None
        else Path.cwd() / "artifacts" / "evaluation" / f"px4-airframe-transaction-{resolved_run_id}"
    )
    journal_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, object]] = []
    for vehicle_class in _VEHICLE_CLASSES:
        context, plan, geometry, policy = _compile_fixture(vehicle_class)
        compiled = compile_executable_mission(
            vehicle_class=vehicle_class,
            platform_plan=plan,
            context_snapshot=context,
            geometry=geometry,
            policy=policy,
            airframe_profile_hash=_PROFILE_HASH,
            mission_id=f"{vehicle_class}-shared-transaction-mission-{resolved_run_id}",
        )
        entries.append(
            run_px4_mission_transaction_acceptance(
                vehicle_class=vehicle_class,
                compiled=compiled,
                context_snapshot=context,
                platform_plan=plan,
                policy=policy,
                journal_path=journal_root / f"{vehicle_class}.transaction.jsonl",
                now_utc=_NOW,
                transaction_id=f"transaction-{resolved_run_id}-{vehicle_class}",
            )
        )
    document: dict[str, object] = {
        "schema": "multidetect.px4-airframe-mission-transaction-acceptance",
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "release_label": PRODUCT_VERSION.release_label,
        "numeric_version": PRODUCT_VERSION.numeric,
        "run_id": resolved_run_id,
        "runtime_mode": "SIM",
        "truth_free": True,
        "physical_output_enabled": False,
        "shared_dispatcher": "compile_executable_mission",
        "shared_transport": "in_process_mission_item_int",
        "entries": entries,
    }
    verification = verify_px4_mission_transaction_acceptance(document)
    document["gate_passed"] = bool(verification["gate_passed"])
    document["checks"] = verification["checks"]
    document["verification"] = verification
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        help="write evidence JSON; journals are placed beside it in a unique directory",
    )
    args = parser.parse_args(argv)
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    journal_directory = (
        args.out.expanduser().resolve().parent / f"{args.out.stem}.journals.{run_id}"
        if args.out is not None
        else None
    )
    try:
        payload = run(journal_directory=journal_directory, run_id=run_id)
    except Exception as exc:  # pragma: no cover - CLI boundary
        print(
            json.dumps(
                {
                    "schema": "multidetect.px4-airframe-mission-transaction-acceptance",
                    "schema_version": 1,
                    "gate_passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 2
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.out is not None:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
        print(
            json.dumps(
                {
                    "event": "px4_airframe_transaction_acceptance_finished",
                    "gate_passed": payload["gate_passed"],
                    "run_id": payload["run_id"],
                    "evidence_path": str(output),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(encoded, end="")
    return 0 if payload["gate_passed"] is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
