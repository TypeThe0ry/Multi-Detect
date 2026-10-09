"""Replay the prompt-to-task onboard policy chain in deterministic software.

This runner uses the same ONNX runner, adapter-owned WGS-84 navigation
compiler and task receipt state machine that a future Gazebo/PX4 or Jetson
adapter will call.  It deliberately does not open a vehicle transport and
therefore cannot be interpreted as external Gazebo, HIL or real-flight proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import (  # noqa: E402
    ONBOARD_MODEL_FEATURE_NAMES,
    ContextSnapshot,
    GeoPoint,
    ModelBackedDecisionProvider,
    OnboardAIExecutor,
    OnboardAutonomousSortie,
    OnboardFlightPlanDispatch,
    OnboardMissionAdapter,
    OnboardMissionEventLoop,
    OnboardOnnxModelRunner,
    OnboardPerceptionObservation,
    OnboardRealtimeModelBridge,
    OnboardTaskAction,
    PlanAction,
    PromptEnvelope,
    ResolvedTaskSpec,
    SharedTaskPolicy,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    load_onboard_model_reference,
)
from multidetect.autonomy.onboard_navigation import compile_model_flight_plan  # noqa: E402
from multidetect.autonomy.policy_artifact import PolicyArtifact  # noqa: E402
from multidetect.autonomy.task_ranker import TaskRanker  # noqa: E402
from multidetect.autonomy.task_selection import TaskCandidate  # noqa: E402
from multidetect.px4_mission import (  # noqa: E402
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
)

PROFILE_HASH = "b" * 64
HOME = GeoPoint(1.300000, 103.800000)
CURRENT_MODEL_REFERENCE = (
    ROOT / "configs" / "training" / "onboard-policy-current-reference-v1.json"
)
CURRENT_MODEL_ID = "onboard-policy-r33-balanced-bearing-3d-dev"
CURRENT_MODEL_VERSION = (
    "0.3.0-pre.8-onboard-policy-r33-balanced-bearing-3d-dev"
)


def _task() -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="neutral-target-search",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="search-corridor",
            direction="northbound",
            length_m=400.0,
            centerline=(HOME, GeoPoint(1.304000, 103.800000)),
            resolution_confidence=0.95,
            buffer_m=20.0,
        ),
        target_classes=("vehicle",),
        on_detection=(
            PlanAction.OBSERVE_TARGET,
            PlanAction.CAPTURE_EVIDENCE,
            PlanAction.REPORT_GEOLOCATION,
        ),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def _runtime() -> dict[str, object]:
    geometry = MultirotorMissionGeometry(
        home=HOME,
        waypoints=(
            GeoPoint(1.300400, 103.800200, 35.0, "relative_home"),
            GeoPoint(1.300800, 103.800400, 40.0, "relative_home"),
        ),
        takeoff_altitude_m=30.0,
        loiter_time_s=15.0,
        loiter_radius_m=25.0,
        acceptance_radius_m=5.0,
        landing=GeoPoint(1.300000, 103.800000, 0.0, "relative_home"),
    )
    policy = Px4MultirotorSafetyPolicy(
        geofence_center=HOME,
        geofence_radius_m=2_000.0,
        allowed_airframe_profile_ids=("px4_multirotor_gazebo_v1",),
        minimum_relative_altitude_m=10.0,
        maximum_relative_altitude_m=120.0,
        maximum_mission_radius_m=1_000.0,
        maximum_loiter_radius_m=100.0,
        maximum_landing_offset_m=10.0,
    )
    return {
        "schema_version": 1,
        "vehicle_class": "multirotor",
        **geometry.to_mapping(),
        "safety_policy": policy.to_mapping(),
    }


def _context(at_s: float, *, altitude_m: float = 0.0) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id=f"chain-context-{at_s:.1f}",
        captured_at_s=at_s,
        captured_at_utc=(
            datetime.fromtimestamp(at_s, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        aircraft_position=GeoPoint(
            HOME.latitude_deg, HOME.longitude_deg, altitude_m, "relative_home"
        ),
        home_position=HOME,
        airframe_profile_id="px4_multirotor_gazebo_v1",
        airframe_profile_hash=PROFILE_HASH,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
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


def run_chain(
    model_path: Path,
    *,
    output_path: Path,
    model_id: str = CURRENT_MODEL_ID,
    model_version: str = CURRENT_MODEL_VERSION,
) -> dict[str, object]:
    model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
    runner = OnboardOnnxModelRunner.from_path(
        model_path,
        providers=("CPUExecutionProvider",),
    )
    ranker = TaskRanker((0, 0, 0), "onboard-chain-baseline")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="onboard-task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash=hashlib.sha256(b"multidetect.mission-compile-runtime-v1").hexdigest(),
        safety_policy_hash=hashlib.sha256(b"multidetect.safety-policy-v1").hexdigest(),
    )
    prompt = PromptEnvelope(
        prompt_id="neutral-target-prompt",
        text="搜索任务目标，确认后记录位置并返航",
        language="zh-CN",
        issued_at="2026-10-04T00:00:00Z",
        operator_id="software-sim",
        aircraft_id="sim-x500",
    )
    candidate = TaskCandidate(_task())
    candidates = (candidate,)
    runtimes = {candidate.task.task_id: _runtime()}
    provider = ModelBackedDecisionProvider(
        runner,
        model_id=model_id,
        model_version=model_version,
        model_sha256=model_sha256,
        provider="onnxruntime:CPUExecutionProvider",
    )
    executor = OnboardAIExecutor(
        prompt,
        SharedTaskPolicy(ranker, artifact),
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )

    safety_requests: list[object] = []

    def dispatch(context, plan, _decision, mission_id):
        compilation = compile_model_flight_plan(
            candidate.task,
            context,
            "multirotor",
            runtime=runtimes[candidate.task.task_id],
            flight_plan=plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        mission = compilation.compiled_mission.executable_mission
        return OnboardFlightPlanDispatch(
            mission=mission,
            flight_plan_sha256=plan.canonical_hash,
            mission_sha256=compilation.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=_decision.selected_task_id,
        )

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="neutral-target-chain",
        flight_plan_dispatcher=dispatch,
        safety_dispatcher=safety_requests.append,
    )
    bridge = OnboardRealtimeModelBridge(
        OnboardMissionEventLoop(adapter), candidates=candidates, runtimes=runtimes
    )
    started_missions: list[str] = []
    return_requests: list[str] = []
    reports: list[object] = []
    sortie = OnboardAutonomousSortie(
        bridge,
        mission_start_dispatcher=lambda _context, mission, _revision: started_missions.append(
            mission.mission_id
        ),
        return_request_dispatcher=lambda _context, revision: return_requests.append(
            revision.decision.reason
        ),
        report_dispatcher=reports.append,
    )
    initial = sortie.prepare(_context(10.0), mission_id="neutral-target-chain-initial")
    sortie.ingest_telemetry(_context(11.0, altitude_m=12.0))
    candidate_mismatch = OnboardPerceptionObservation(
        observation_id="search-nonmatching-candidate",
        observed_at_s=12.0,
        source="software-sim-neutral-3d-perception-fixture",
        target_visible=True,
        target_bearing_deg=-80.0,
        target_distance_m=80.0,
        target_confidence=0.65,
        target_match_score=0.10,
        target_class="candidate_object",
        target_color=None,
        target_class_match=False,
        target_color_match=False,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
        detail="candidate object rejected by task class; continue search",
    )
    target_match = OnboardPerceptionObservation(
        observation_id="task-target-match",
        observed_at_s=14.0,
        source="software-sim-neutral-3d-perception-fixture",
        target_visible=True,
        target_bearing_deg=15.0,
        target_distance_m=55.0,
        target_confidence=0.96,
        target_match_score=0.97,
        target_class="vehicle",
        target_color=None,
        target_class_match=True,
        target_color_match=None,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
        detail="task target matched and track confirmed",
    )
    record = OnboardPerceptionObservation(
        observation_id="record-target-gps",
        observed_at_s=16.0,
        source="software-sim-neutral-3d-perception-fixture",
        target_visible=True,
        target_bearing_deg=15.0,
        target_distance_m=55.0,
        target_north_offset_m=52.0,
        target_east_offset_m=14.0,
        target_confidence=0.99,
        target_match_score=0.99,
        target_class="vehicle",
        target_color=None,
        target_class_match=True,
        target_color_match=None,
        obstacle_distance_m=5_000.0,
        # The calibrated N/E solution closes the semantic recording phase;
        # the sortie method still enforces that the previous phase was
        # MATCH_TARGET before accepting this observation.
        task_phase=OnboardTaskAction.RECORD_LOCATION,
        detail="qualified GPS target position recorded",
    )
    revisions = [
        sortie.ingest_perception(_context(12.0), candidate_mismatch),
        sortie.ingest_perception(_context(14.0), target_match),
        sortie.record_target_location(_context(16.0), record),
    ]
    returned, reported = sortie.complete_return_and_report(_context(18.0))
    revisions.extend((returned, reported))
    events = tuple(revision.event for revision in revisions)
    if any(event is None for event in events):  # pragma: no cover - defensive
        raise RuntimeError("autonomous sortie produced a revision without an event")
    events = tuple(event for event in events if event is not None)
    receipts = [receipt.to_mapping() for receipt in adapter.task_state.receipts]
    result = {
        "schema_version": 1,
        "contract": "multidetect.onboard-policy-chain-evidence",
        "prompt": prompt.to_mapping(),
        "model": {
            "path": str(model_path),
            "model_id": provider.model_id,
            "model_version": provider.model_version,
            "model_sha256": model_sha256,
            "feature_width": len(ONBOARD_MODEL_FEATURE_NAMES),
            "provider": provider.provider,
            "synthetic_bootstrap": True,
        },
        "sequence": [
            {
                "event_id": event.event_id,
                "decision": revision.decision.to_mapping(),
                "mission_id": None if revision.mission is None else revision.mission.mission_id,
                "mission_sha256": None
                if revision.mission is None
                else revision.mission.canonical_hash,
                "task_receipt": (
                    None if revision.task_receipt is None else revision.task_receipt.to_mapping()
                ),
            }
            for event, revision in zip(events, revisions, strict=True)
        ],
        "initial_mission_sha256": None
        if initial.mission is None
        else initial.mission.canonical_hash,
        "task_receipts": receipts,
        "phase_sequence": [receipt["phase"] for receipt in receipts],
        "movement_intents": [
            {
                "event_id": event.event_id,
                "action": (
                    None
                    if revision.decision.model_proposal is None
                    or revision.decision.model_proposal.flight_plan is None
                    else revision.decision.model_proposal.flight_plan.action.value
                ),
                "north_offset_m": (
                    None
                    if revision.decision.model_proposal is None
                    or revision.decision.model_proposal.flight_plan is None
                    else revision.decision.model_proposal.flight_plan.north_offset_m
                ),
                "east_offset_m": (
                    None
                    if revision.decision.model_proposal is None
                    or revision.decision.model_proposal.flight_plan is None
                    else revision.decision.model_proposal.flight_plan.east_offset_m
                ),
                "altitude_m": (
                    None
                    if revision.decision.model_proposal is None
                    or revision.decision.model_proposal.flight_plan is None
                    else revision.decision.model_proposal.flight_plan.altitude_m
                ),
                "speed_mps": (
                    None
                    if revision.decision.model_proposal is None
                    or revision.decision.model_proposal.flight_plan is None
                    else revision.decision.model_proposal.flight_plan.speed_mps
                ),
                "mission_id": None if revision.mission is None else revision.mission.mission_id,
                "mission_sha256": (
                    None if revision.mission is None else revision.mission.canonical_hash
                ),
                "planner_revalidated": revision.decision.planner_revalidated,
                "speed_applied_to_mission_wire": False,
                "policy_applied_to_actuator": False,
            }
            for event, revision in zip(events, revisions, strict=True)
        ],
        "planner_revalidated": all(
            revision.decision.planner_revalidated
            for revision in revisions
            if revision.mission is not None
        ),
            "movement_mission_count": sum(revision.mission is not None for revision in revisions),
        "safety_request_count": len(safety_requests),
        "autonomous_sortie": sortie.status.to_mapping(),
        "mission_start_count": len(started_missions),
        "return_request_count": len(return_requests),
        "report_count": len(reports),
        "policy_applied_to_actuator": False,
        "truth_free": True,
        "software_sim_only": True,
        "external_gazebo_evidence": False,
        "hil_real_evidence": False,
        "jetson_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="explicit ONNX path; omitted means the hash-bound current reference",
    )
    parser.add_argument(
        "--model-reference",
        type=Path,
        help=(
            "load the hash-bound selected OnboardAI reference; this overrides "
            "--model and supplies the model identity"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs/evidence/onboard-policy-chain-20261004.json",
    )
    parser.add_argument("--model-id", default=CURRENT_MODEL_ID)
    parser.add_argument("--model-version", default=CURRENT_MODEL_VERSION)
    args = parser.parse_args()
    model_path = args.model
    model_id = args.model_id
    model_version = args.model_version
    if args.model_reference is not None:
        reference = load_onboard_model_reference(args.model_reference, repository_root=ROOT)
        model_path = reference.model_path
        model_id = reference.model_id
        model_version = reference.model_version
    elif model_path is None:
        reference = load_onboard_model_reference(
            CURRENT_MODEL_REFERENCE,
            repository_root=ROOT,
        )
        model_path = reference.model_path
        model_id = reference.model_id
        model_version = reference.model_version
    result = run_chain(
        model_path,
        output_path=args.output,
        model_id=model_id,
        model_version=model_version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
