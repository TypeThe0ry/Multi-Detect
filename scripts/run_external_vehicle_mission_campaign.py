"""Run a current-parity external PX4 Mission transaction for one airframe.

This wrapper builds the same public Mission compiler inputs used by the
vehicle-neutral acceptance tests, then hands them to the existing external
Docker/PX4 Mission transport.  It does not arm, change mode, start flight or
enable physical output.  The resulting evidence includes the current
``release-parity.json`` binding so it cannot be mistaken for a source-rebound
historical report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# ruff: noqa: E402 -- repository-local import bootstrap before package imports
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_SOURCE = REPOSITORY_ROOT / "src"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if REPOSITORY_SOURCE.is_dir() and str(REPOSITORY_SOURCE) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_SOURCE))

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.mission_campaign import select_and_compile_task
from multidetect.autonomy.policy_artifact import load_policy_artifact
from multidetect.autonomy.policy_contract import SharedTaskPolicy
from multidetect.autonomy.task_ranker import load_task_ranker
from multidetect.autonomy.task_selection import TaskCandidate
from multidetect.px4_mission import (
    FixedWingMissionGeometry,
    MultirotorMissionGeometry,
    Px4FixedWingSafetyPolicy,
    Px4MissionSafetyPolicy,
    Px4MultirotorSafetyPolicy,
    VtolMissionGeometry,
)
from multidetect.simulator.stack import airframe_profile_hash as stable_airframe_profile_hash
from scripts.run_external_px4_mission_transaction import (
    build_mission_transaction_runner,
)
from scripts.run_simulator_docker_acceptance import (
    PINNED_IMAGE,
    build_docker_simulator_plan,
    run_acceptance,
)

PROFILE_HASH = "b" * 64
VEHICLE_DATA: dict[str, dict[str, Any]] = {
    "fixed-wing": {
        "scenario": "configs/scenarios/coastal_patrol_fixed_wing_001.scenario.json",
        "home": GeoPoint(1.2905, 103.8505),
        "profile": "px4_fixed_wing_gazebo_v1",
        "actions": (
            PlanAction.TAKEOFF,
            PlanAction.TRANSIT,
            PlanAction.SEARCH_CORRIDOR,
            PlanAction.RETURN,
            PlanAction.LAND,
        ),
    },
    "multirotor": {
        "scenario": "configs/scenarios/urban_inspection_multirotor_001.scenario.json",
        "home": GeoPoint(1.3521, 103.8198),
        "profile": "px4_multirotor_gazebo_v1",
        "actions": (
            PlanAction.TAKEOFF,
            PlanAction.TRANSIT,
            PlanAction.SEARCH_AREA,
            PlanAction.OBSERVE_TARGET,
            PlanAction.RETURN,
            PlanAction.LAND,
        ),
    },
    "vtol": {
        "scenario": "configs/scenarios/river_rescue_vtol_001.scenario.json",
        "home": GeoPoint(1.2900, 103.8500),
        "profile": "px4_vtol_gazebo_v1",
        "actions": (
            PlanAction.TAKEOFF,
            PlanAction.TRANSIT,
            PlanAction.SEARCH_CORRIDOR,
            PlanAction.OBSERVE_TARGET,
            PlanAction.RETURN,
            PlanAction.LAND,
        ),
    },
}


def _context(vehicle_class: str) -> ContextSnapshot:
    item = VEHICLE_DATA[vehicle_class]
    cruise = vehicle_class == "fixed-wing"
    home = item["home"]
    return ContextSnapshot(
        snapshot_id=f"external-{vehicle_class}-current-parity-context",
        captured_at_s=10.0,
        captured_at_utc="2026-09-27T00:00:00Z",
        aircraft_position=home,
        home_position=home,
        airframe_profile_id=item["profile"],
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
        ground_speed_mps=15.0 if cruise else (8.0 if vehicle_class == "vtol" else 0.0),
        airspeed_mps=17.0 if cruise else (12.0 if vehicle_class == "vtol" else 0.0),
        wind_north_mps=2.0 if cruise else 1.0,
        wind_east_mps=-0.8 if cruise else -0.4,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.5 if cruise else 1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _geometry_and_policy(
    vehicle_class: str, context: ContextSnapshot
) -> tuple[FixedWingMissionGeometry | MultirotorMissionGeometry | VtolMissionGeometry, object]:
    home = context.home_position
    if vehicle_class == "fixed-wing":
        geometry = FixedWingMissionGeometry(
            home=home,
            waypoints=(
                GeoPoint(home.latitude_deg + 0.0010, home.longitude_deg, 60.0, "relative_home"),
                GeoPoint(
                    home.latitude_deg + 0.0015,
                    home.longitude_deg + 0.0010,
                    60.0,
                    "relative_home",
                ),
            ),
            landing_approach=GeoPoint(
                home.latitude_deg,
                home.longitude_deg + 0.0020,
                40.0,
                "relative_home",
            ),
            landing=GeoPoint(home.latitude_deg, home.longitude_deg, 0.0, "relative_home"),
            takeoff_altitude_m=35.0,
            acceptance_radius_m=8.0,
        )
        return geometry, Px4FixedWingSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=6_000.0,
            allowed_airframe_profile_ids=(context.airframe_profile_id,),
        )
    if vehicle_class == "vtol":
        geometry = VtolMissionGeometry(
            home=home,
            waypoints=(
                GeoPoint(
                    home.latitude_deg + 0.0005,
                    home.longitude_deg + 0.0002,
                    45.0,
                    "relative_home",
                ),
                GeoPoint(
                    home.latitude_deg + 0.0010,
                    home.longitude_deg + 0.0005,
                    55.0,
                    "relative_home",
                ),
            ),
            takeoff_altitude_m=35.0,
            loiter_time_s=20.0,
            loiter_radius_m=35.0,
            acceptance_radius_m=8.0,
            landing=GeoPoint(home.latitude_deg, home.longitude_deg, 0.0, "relative_home"),
        )
        return geometry, Px4MissionSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=4_000.0,
            allowed_airframe_profile_ids=(context.airframe_profile_id,),
            minimum_relative_altitude_m=10.0,
            maximum_relative_altitude_m=150.0,
            maximum_mission_radius_m=2_000.0,
            maximum_loiter_radius_m=120.0,
            maximum_landing_offset_m=15.0,
        )
    geometry = MultirotorMissionGeometry(
        home=home,
        waypoints=(
            GeoPoint(
                home.latitude_deg + 0.0004,
                home.longitude_deg + 0.0002,
                35.0,
                "relative_home",
            ),
            GeoPoint(
                home.latitude_deg + 0.0008,
                home.longitude_deg + 0.0004,
                40.0,
                "relative_home",
            ),
        ),
        takeoff_altitude_m=30.0,
        loiter_time_s=15.0,
        loiter_radius_m=25.0,
        acceptance_radius_m=5.0,
        landing=GeoPoint(home.latitude_deg, home.longitude_deg, 0.0, "relative_home"),
    )
    return geometry, Px4MultirotorSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=2_000.0,
        allowed_airframe_profile_ids=(context.airframe_profile_id,),
        minimum_relative_altitude_m=10.0,
        maximum_relative_altitude_m=100.0,
        maximum_mission_radius_m=1_000.0,
        maximum_loiter_radius_m=100.0,
        maximum_landing_offset_m=10.0,
    )


def _task(context: ContextSnapshot) -> ResolvedTaskSpec:
    home = context.home_position
    return ResolvedTaskSpec(
        task_id=f"external-{context.airframe_profile_id}-task",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="external-current-parity-corridor",
            direction="northbound",
            length_m=1_000.0,
            buffer_m=30.0,
            centerline=(home, GeoPoint(home.latitude_deg + 0.004, home.longitude_deg)),
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


def _runtime_airframe_profile_hash(
    *, root: Path, vehicle_class: str, evidence_path: Path
) -> str:
    """Derive the stable airframe identity from the exact external plan."""
    container_name = f"multidetect-{vehicle_class.replace('-', '')}-current-parity"
    scenario = root / VEHICLE_DATA[vehicle_class]["scenario"]
    plan = build_docker_simulator_plan(
        scenario,
        repository_root=root,
        job_dir=evidence_path.parent / f"{container_name}-job",
        container_name=container_name,
        control_port=14540,
        telemetry_port=14541,
        image=PINNED_IMAGE,
        dry_run=True,
    )
    return stable_airframe_profile_hash(
        {
            "schema_version": 1,
            "airframe_profile_id": plan.airframe_profile_id,
            "vehicle_model": plan.vehicle_model,
            "model_name": plan.model_name,
            "stack_plan_sha256": plan.stack_plan_sha256,
        }
    )


def _inputs(
    vehicle_class: str,
    run_directory: Path,
    *,
    shared_policy: SharedTaskPolicy | None = None,
    airframe_profile_hash: str = PROFILE_HASH,
) -> tuple[Path, Path, Path]:
    context = _context(vehicle_class)
    geometry, policy = _geometry_and_policy(vehicle_class, context)
    task = _task(context)
    runtime = {"schema_version": 1, **geometry.to_mapping(), "safety_policy": policy.to_mapping()}
    binding = select_and_compile_task(
        (TaskCandidate(task, priority=1),),
        context,
        vehicle_class,
        runtimes={task.task_id: runtime},
        airframe_profile_hash=airframe_profile_hash,
        mission_id=f"external-{vehicle_class}-current-parity-mission",
        shared_policy=shared_policy,
    )
    if (
        binding.selected_task_id != task.task_id
        or binding.compilation is None
        or not binding.planner_revalidated
    ):
        raise RuntimeError("vehicle-neutral task binding did not produce a safe Mission")
    run_directory.mkdir(parents=True, exist_ok=True)
    context_path = run_directory / "context.json"
    compiled_path = run_directory / "compiled-mission.json"
    runtime_path = run_directory / "runtime.json"
    context_path.write_text(
        json.dumps(context.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    compiled_path.write_text(
        json.dumps(binding.compilation.to_mapping(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    runtime_path.write_text(
        json.dumps(runtime, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (run_directory / "task-binding.json").write_text(
        json.dumps(binding.to_mapping(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return compiled_path, context_path, runtime_path


def _parity_binding(root: Path) -> dict[str, Any]:
    path = root / "configs/release-parity.json"
    raw = path.read_bytes()
    document = json.loads(raw)
    return {
        "path": "configs/release-parity.json",
        "bytes": len(raw),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "hashes": document["parity"],
        "release_label": document["release_label"],
        "verified": True,
    }


def run_current_parity_campaign(
    *,
    root: Path,
    vehicle_class: str,
    evidence_path: Path,
    run_directory: Path,
    shared_policy: SharedTaskPolicy | None = None,
    sensor_frame_count: int = 0,
    sensor_width: int = 1280,
    sensor_height: int = 960,
    policy_artifact_path: Path | None = None,
    ranker_path: Path | None = None,
) -> dict[str, Any]:
    if vehicle_class not in VEHICLE_DATA:
        raise ValueError(f"unsupported vehicle class: {vehicle_class}")
    if (
        isinstance(sensor_frame_count, bool)
        or not isinstance(sensor_frame_count, int)
        or sensor_frame_count < 0
    ):
        raise ValueError("sensor_frame_count must be a non-negative integer")
    if (
        isinstance(sensor_width, bool)
        or not isinstance(sensor_width, int)
        or sensor_width <= 0
        or isinstance(sensor_height, bool)
        or not isinstance(sensor_height, int)
        or sensor_height <= 0
    ):
        raise ValueError("sensor dimensions must be positive integers")
    scenario = root / VEHICLE_DATA[vehicle_class]["scenario"]
    expected_airframe_profile_hash = _runtime_airframe_profile_hash(
        root=root, vehicle_class=vehicle_class, evidence_path=evidence_path
    )
    if (
        shared_policy is not None
        and shared_policy.artifact.airframe_profile_hash != expected_airframe_profile_hash
    ):
        raise ValueError("shared policy airframe profile does not match external runtime plan")
    compiled, context, runtime = _inputs(
        vehicle_class,
        run_directory,
        shared_policy=shared_policy,
        airframe_profile_hash=expected_airframe_profile_hash,
    )
    transaction_id = f"external-{vehicle_class}-current-parity"
    hook = build_mission_transaction_runner(
        compiled_path=compiled,
        context_path=context,
        runtime_path=runtime,
        vehicle_class=vehicle_class,
        journal_directory=run_directory / "journals",
        transaction_id=transaction_id,
    )
    payload = run_acceptance(
        scenario,
        evidence_path,
        execute=True,
        container_name=f"multidetect-{vehicle_class.replace('-', '')}-current-parity",
        mission_transaction_runner=hook,
        sensor_frame_count=sensor_frame_count,
        sensor_width=sensor_width,
        sensor_height=sensor_height,
    )
    sensor_transport = payload.get("sensor_transport")
    if sensor_frame_count > 0 and (
        not isinstance(sensor_transport, dict)
        or sensor_transport.get("airframe_profile_sha256")
        != expected_airframe_profile_hash
    ):
        raise RuntimeError("external sensor transport airframe identity mismatch")
    binding_path = run_directory / "task-binding.json"
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if (
        not isinstance(binding, dict)
        or binding.get("vehicle_class") != vehicle_class
        or binding.get("mission_bound") is not True
        or binding.get("planner_revalidated") is not True
        or binding.get("policy_applied_to_actuator") is not False
        or binding.get("truth_free") is not True
    ):
        raise RuntimeError("vehicle-neutral task binding evidence is invalid")
    payload["task_binding"] = binding
    payload["task_binding_path"] = str(binding_path)
    payload["release_parity"] = _parity_binding(root)
    payload["current_release_parity_bound"] = True
    payload["shared_policy_used"] = shared_policy is not None
    payload["ranker_sha256"] = (
        shared_policy.ranker.canonical_hash if shared_policy is not None else None
    )
    payload["policy_artifact_sha256"] = (
        shared_policy.artifact_sha256 if shared_policy is not None else None
    )
    payload["shared_policy_mission_sensor_frames_requested"] = sensor_frame_count
    payload["external_airframe_profile_sha256"] = expected_airframe_profile_hash
    container_runtime = payload.get("container_runtime")
    mission_transaction = payload.get("mission_transaction")
    mission_attestation = (
        mission_transaction.get("runtime_attestation")
        if isinstance(mission_transaction, dict)
        else None
    )
    runtime_container_id = (
        container_runtime.get("container_id")
        if isinstance(container_runtime, dict)
        else None
    )
    mission_container_id = (
        mission_attestation.get("container_id")
        if isinstance(mission_attestation, dict)
        else None
    )
    if sensor_frame_count > 0:
        if not isinstance(runtime_container_id, str) or not isinstance(
            mission_container_id, str
        ):
            raise RuntimeError("external sensor/Mission container identity is missing")
        payload["sensor_runtime_binding"] = {
            "container_id": runtime_container_id,
            "mission_container_id": mission_container_id,
            "same_container": runtime_container_id == mission_container_id,
            "truth_free": True,
        }
        if runtime_container_id != mission_container_id:
            raise RuntimeError("external sensor and Mission used different containers")
    if policy_artifact_path is not None:
        payload["policy_artifact"] = str(policy_artifact_path)
    if ranker_path is not None:
        payload["ranker"] = str(ranker_path)
    evidence_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--vehicle-class", choices=tuple(VEHICLE_DATA), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--ranker", type=Path)
    parser.add_argument("--ranker-file-sha256")
    parser.add_argument("--policy-artifact", type=Path)
    parser.add_argument("--policy-artifact-file-sha256")
    parser.add_argument(
        "--sensor-frames",
        type=int,
        default=0,
        help="capture real Gazebo sensor frames in the same container before Mission transaction",
    )
    parser.add_argument("--sensor-width", type=int, default=1280)
    parser.add_argument("--sensor-height", type=int, default=960)
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    evidence = args.evidence if args.evidence.is_absolute() else root / args.evidence
    run_directory = (
        args.run_directory
        if args.run_directory.is_absolute()
        else root / args.run_directory
    )
    if evidence.exists():
        parser.error(f"refusing to overwrite: {evidence}")
    ranker_args = (args.ranker, args.ranker_file_sha256)
    policy_args = (args.policy_artifact, args.policy_artifact_file_sha256)
    if (ranker_args[0] is None) != (ranker_args[1] is None):
        parser.error("--ranker and --ranker-file-sha256 must be paired")
    if (policy_args[0] is None) != (policy_args[1] is None):
        parser.error(
            "--policy-artifact and --policy-artifact-file-sha256 must be paired"
        )
    if any(value is not None for value in ranker_args) and all(
        value is None for value in policy_args
    ):
        parser.error("a ranker input requires an explicit policy artifact and hash")
    if any(value is not None for value in policy_args) and any(
        value is None for value in ranker_args
    ):
        parser.error("a policy artifact requires an explicit ranker and hash")
    shared_policy = None
    if all(value is not None for value in ranker_args + policy_args):
        try:
            ranker = load_task_ranker(
                args.ranker,
                expected_sha256=args.ranker_file_sha256,
            )
            artifact = load_policy_artifact(
                args.policy_artifact,
                expected_sha256=args.policy_artifact_file_sha256,
                ranker=ranker,
            )
            shared_policy = SharedTaskPolicy(ranker, artifact)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(str(exc))
    payload = run_current_parity_campaign(
        root=root,
        vehicle_class=args.vehicle_class,
        evidence_path=evidence,
        run_directory=run_directory,
        shared_policy=shared_policy,
        sensor_frame_count=args.sensor_frames,
        sensor_width=args.sensor_width,
        sensor_height=args.sensor_height,
        policy_artifact_path=(
            args.policy_artifact.resolve() if args.policy_artifact is not None else None
        ),
        ranker_path=args.ranker.resolve() if args.ranker is not None else None,
    )
    print(
        json.dumps(
            {
                "vehicle_class": args.vehicle_class,
                "gate_passed": payload.get("gate_passed"),
                "mission_transaction_gate_passed": payload.get("mission_transaction_gate_passed"),
                "current_release_parity_bound": payload.get("current_release_parity_bound"),
                "shared_policy_used": payload.get("shared_policy_used"),
                "policy_artifact_sha256": payload.get("policy_artifact_sha256"),
                "sensor_transport_gate_passed": payload.get("sensor_transport_gate_passed"),
                "sensor_frames_requested": payload.get(
                    "shared_policy_mission_sensor_frames_requested"
                ),
                "evidence": str(evidence),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if payload.get("gate_passed") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
