from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    ModelBackedDecisionProvider,
    OnboardAIExecutor,
    OnboardAutonomousSortie,
    OnboardFlightPlanDispatch,
    OnboardMissionAdapter,
    OnboardMissionEventLoop,
    OnboardModelInput,
    OnboardPerceptionObservation,
    OnboardRealtimeModelBridge,
    OnboardResponse,
    OnboardTaskAction,
    PlanAction,
    PolicyArtifact,
    PromptEnvelope,
    ResolvedTaskSpec,
    SharedTaskPolicy,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.onboard_navigation import compile_model_flight_plan
from multidetect.autonomy.task_ranker import TaskRanker
from multidetect.autonomy.task_selection import TaskCandidate
from multidetect.px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy

PROFILE_HASH = "b" * 64
HOME = GeoPoint(1.3, 103.8)


def _context(at_s: float, *, altitude_m: float = 0.0) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id=f"external-context-{at_s:.1f}",
        captured_at_s=at_s,
        captured_at_utc=datetime.fromtimestamp(at_s, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
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


def _task() -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="red-car-external",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="external-corridor",
            direction="northbound",
            length_m=400.0,
            centerline=(HOME, GeoPoint(1.304, 103.8)),
            resolution_confidence=0.95,
            buffer_m=20.0,
        ),
        target_classes=("vehicle", "red"),
        on_detection=(PlanAction.OBSERVE_TARGET, PlanAction.CAPTURE_EVIDENCE),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def _runtime() -> dict[str, object]:
    geometry = MultirotorMissionGeometry(
        home=HOME,
        waypoints=(
            GeoPoint(1.3004, 103.8002, 35.0, "relative_home"),
            GeoPoint(1.3008, 103.8004, 40.0, "relative_home"),
        ),
        takeoff_altitude_m=30.0,
        loiter_time_s=15.0,
        loiter_radius_m=25.0,
        acceptance_radius_m=5.0,
        landing=GeoPoint(1.3, 103.8, 0.0, "relative_home"),
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


class _SequenceRunner:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self.outputs = iter(outputs)
        self.inputs: list[OnboardModelInput] = []

    def infer(self, model_input: OnboardModelInput) -> dict[str, object]:
        self.inputs.append(model_input)
        return next(self.outputs)


def _bridge() -> tuple[
    OnboardRealtimeModelBridge,
    _SequenceRunner,
    tuple[TaskCandidate, ...],
    dict[str, object],
]:
    candidate = TaskCandidate(_task())
    candidates = (candidate,)
    runtimes = {candidate.task.task_id: _runtime()}
    ranker = TaskRanker((0, 0, 0), "external-bridge-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="external-bridge-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    prompt = PromptEnvelope(
        prompt_id="external-red-car",
        text="帮我找到最近的红车",
        language="zh-CN",
        issued_at="2026-10-04T00:00:00Z",
        operator_id="external-sim",
        aircraft_id="gazebo-x500",
    )
    runner = _SequenceRunner(
        [
            {
                "response": "replan",
                "reason": "model_task_search_plan_search",
                "confidence": 0.91,
                "flight_plan": {
                    "action": "search",
                    "north_offset_m": 0.0,
                    "east_offset_m": -25.0,
                    "altitude_m": 35.0,
                    "speed_mps": 8.0,
                    "duration_s": 5.0,
                    "reference_id": "green-mismatch-search",
                },
                "task_action": "search",
            },
            {
                "response": "replan",
                "reason": "model_task_match_target_plan_follow_target",
                "confidence": 0.94,
                "flight_plan": {
                    "action": "follow_target",
                    "north_offset_m": 20.0,
                    "east_offset_m": 5.0,
                    "altitude_m": 35.0,
                    "speed_mps": 6.0,
                    "duration_s": 5.0,
                    "reference_id": "red-match-follow",
                },
                "task_action": "match_target",
            },
        ]
    )
    provider = ModelBackedDecisionProvider(
        runner,
        model_id="onboard-policy-r4",
        model_version="0.3.0-pre.8-onboard-policy-r4",
        model_sha256="a" * 64,
        provider="test-shared-onnx",
    )
    executor = OnboardAIExecutor(
        prompt,
        SharedTaskPolicy(ranker, artifact),
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )

    def dispatch(context, plan, decision, mission_id):
        compilation = compile_model_flight_plan(
            candidate.task,
            context,
            "multirotor",
            runtime=runtimes[candidate.task.task_id],
            flight_plan=plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            mission=compilation.compiled_mission.executable_mission,
            flight_plan_sha256=plan.canonical_hash,
            mission_sha256=compilation.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=decision.selected_task_id,
        )

    event_loop = OnboardMissionEventLoop(
        OnboardMissionAdapter(
            executor,
            mission_id_prefix="external-model",
            flight_plan_dispatcher=dispatch,
            safety_dispatcher=lambda _decision: None,
        )
    )
    return (
        OnboardRealtimeModelBridge(
            event_loop,
            candidates=candidates,
            runtimes=runtimes,
        ),
        runner,
        candidates,
        runtimes,
    )


def test_realtime_bridge_exposes_compiled_mission_to_transport_dispatcher() -> None:
    candidate = TaskCandidate(_task())
    candidates = (candidate,)
    runtimes = {candidate.task.task_id: _runtime()}
    ranker = TaskRanker((0, 0, 0), "transport-dispatch-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="transport-dispatch-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    runner = _SequenceRunner(
        [
            {
                "response": "replan",
                "reason": "transport-test",
                "confidence": 0.9,
                "flight_plan": {
                    "action": "search",
                    "north_offset_m": 0.0,
                    "east_offset_m": -20.0,
                    "altitude_m": 35.0,
                    "speed_mps": 8.0,
                    "duration_s": 5.0,
                    "reference_id": "transport-test",
                },
                "task_action": "search",
            }
        ]
    )
    provider = ModelBackedDecisionProvider(
        runner,
        model_id="onboard-policy-r4",
        model_version="0.3.0-pre.8-onboard-policy-r4",
        model_sha256="a" * 64,
        provider="test-shared-onnx",
    )
    executor = OnboardAIExecutor(
        PromptEnvelope(
            prompt_id="transport-prompt",
            text="帮我找到最近的红车",
            language="zh-CN",
            issued_at="2026-10-04T00:00:00Z",
            operator_id="test",
            aircraft_id="sim",
        ),
        SharedTaskPolicy(ranker, artifact),
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )
    received: list[tuple[ContextSnapshot, object | None, object | None]] = []

    def dispatcher(
        context: ContextSnapshot,
        mission: object | None,
        compilation: object | None,
    ) -> None:
        received.append((context, mission, compilation))

    def dispatch(context, plan, decision, mission_id):
        compilation = compile_model_flight_plan(
            candidate.task,
            context,
            "multirotor",
            runtime=runtimes[candidate.task.task_id],
            flight_plan=plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            mission=compilation.compiled_mission.executable_mission,
            flight_plan_sha256=plan.canonical_hash,
            mission_sha256=compilation.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=decision.selected_task_id,
        )

    bridge = OnboardRealtimeModelBridge(
        OnboardMissionEventLoop(
            OnboardMissionAdapter(
                executor,
                mission_transaction_dispatcher=dispatcher,
                flight_plan_dispatcher=dispatch,
            )
        ),
        candidates=candidates,
        runtimes=runtimes,
    )
    revision = bridge.prepare(_context(10.0), mission_id="transport-initial")

    assert revision.mission is not None
    assert len(received) == 1
    assert received[0][0] == _context(10.0)
    assert received[0][1] is revision.mission
    assert received[0][2] is not None


def test_realtime_bridge_maps_structured_green_then_red_observations_to_shared_model() -> None:
    bridge, runner, _candidates, _runtimes = _bridge()
    initial = bridge.prepare(_context(10.0), mission_id="external-model-initial")
    green = bridge.submit(
        _context(12.0),
        OnboardPerceptionObservation(
            observation_id="frame-green",
            observed_at_s=12.0,
            source="gazebo-rgb-detector-tracker",
            target_visible=True,
            target_class="vehicle",
            target_color="green",
            target_class_match=True,
            target_color_match=False,
            target_bearing_deg=-80.0,
            target_distance_m=80.0,
            target_confidence=0.91,
            target_match_score=0.12,
            obstacle_distance_m=5_000.0,
            task_phase=OnboardTaskAction.SEARCH,
            detail="green vehicle rejected; model searches left",
        ),
    )
    red = bridge.submit(
        _context(14.0),
        OnboardPerceptionObservation(
            observation_id="frame-red",
            observed_at_s=14.0,
            source="gazebo-rgb-detector-tracker",
            target_visible=True,
            target_class="vehicle",
            target_color="red",
            target_class_match=True,
            target_color_match=True,
            target_bearing_deg=15.0,
            target_distance_m=55.0,
            target_confidence=0.97,
            target_match_score=0.98,
            obstacle_distance_m=5_000.0,
            task_phase=OnboardTaskAction.MATCH_TARGET,
            detail="red vehicle matched; follow target",
        ),
    )

    assert initial.mission is not None
    assert green.decision.response is OnboardResponse.REPLAN
    assert red.decision.response is OnboardResponse.REPLAN
    assert green.decision.model_proposal is not None
    assert red.decision.model_proposal is not None
    assert green.decision.model_proposal.model_id == "onboard-policy-r4"
    assert green.decision.planner_revalidated is True
    assert red.decision.binding.wire_hash is not None
    assert [item.event.event_id for item in bridge.revisions[1:] if item.event is not None] == [
        "frame-green",
        "frame-red",
    ]
    assert len(runner.inputs) == 2
    assert all(item.to_mapping()["truth_free"] is True for item in runner.inputs)
    assert bridge.evidence()["policy_applied_to_actuator"] is False


def test_realtime_bridge_requires_live_altitude_before_search_phase() -> None:
    bridge, _runner, _candidates, _runtimes = _bridge()
    bridge.prepare(_context(10.0), mission_id="external-model-initial")

    with pytest.raises(ValueError, match="minimum relative altitude"):
        bridge.mark_takeoff_complete(_context(12.0, altitude_m=5.0))

    assert bridge.event_loop.adapter.task_state.phase is OnboardTaskAction.TAKEOFF
    assert bridge.mark_takeoff_complete(_context(14.0, altitude_m=12.0)) is True
    assert bridge.mark_takeoff_complete(_context(15.0, altitude_m=12.0)) is False
    assert bridge.event_loop.adapter.task_state.phase is OnboardTaskAction.SEARCH
    evidence = bridge.evidence()
    assert evidence["event_loop"]["task_phase"] == "search"
    assert evidence["context_sha256"][-1] == _context(14.0, altitude_m=12.0).canonical_hash


def test_autonomous_sortie_runs_red_car_search_to_report_without_operator_steps() -> None:
    candidate = TaskCandidate(_task())
    candidates = (candidate,)
    runtimes = {candidate.task.task_id: _runtime()}
    ranker = TaskRanker((0, 0, 0), "autonomous-sortie-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="autonomous-sortie-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    runner = _SequenceRunner(
        [
            {
                "response": "replan",
                "reason": "green_rejected_search_left",
                "confidence": 0.91,
                "flight_plan": {
                    "action": "search",
                    "north_offset_m": 0.0,
                    "east_offset_m": -25.0,
                    "altitude_m": 35.0,
                    "speed_mps": 8.0,
                    "duration_s": 5.0,
                    "reference_id": "search-left",
                },
                "task_action": "search",
            },
            {
                "response": "replan",
                "reason": "red_confirmed_follow",
                "confidence": 0.94,
                "flight_plan": {
                    "action": "follow_target",
                    "north_offset_m": 20.0,
                    "east_offset_m": 5.0,
                    "altitude_m": 35.0,
                    "speed_mps": 6.0,
                    "duration_s": 5.0,
                    "reference_id": "red-follow",
                },
                "task_action": "match_target",
            },
            {
                "response": "return",
                "reason": "red_location_recorded_return",
                "confidence": 0.95,
                "flight_plan": {
                    "action": "return_home",
                    "north_offset_m": 0.0,
                    "east_offset_m": 0.0,
                    "altitude_m": 35.0,
                    "speed_mps": 8.0,
                    "duration_s": None,
                    "reference_id": "return-after-gps",
                },
                "task_action": "record_location",
            },
            {
                "response": "return",
                "reason": "home_arrival_confirmed",
                "confidence": 0.95,
                "flight_plan": {
                    "action": "return_home",
                    "north_offset_m": 0.0,
                    "east_offset_m": 0.0,
                    "altitude_m": 35.0,
                    "speed_mps": 8.0,
                    "duration_s": None,
                    "reference_id": "return-confirmed",
                },
                "task_action": "return_home",
            },
            {
                "response": "hold",
                "reason": "task_report_ready",
                "confidence": 0.95,
                "flight_plan": {
                    "action": "hold_position",
                    "north_offset_m": 0.0,
                    "east_offset_m": 0.0,
                    "altitude_m": 35.0,
                    "speed_mps": 0.0,
                    "duration_s": None,
                    "reference_id": "report",
                },
                "task_action": "report",
            },
        ]
    )
    provider = ModelBackedDecisionProvider(
        runner,
        model_id="onboard-policy-r4",
        model_version="0.3.0-pre.8-onboard-policy-r4",
        model_sha256="a" * 64,
        provider="test-shared-onnx",
    )
    executor = OnboardAIExecutor(
        PromptEnvelope(
            prompt_id="autonomous-red-car",
            text="帮我找到最近的红车",
            language="zh-CN",
            issued_at="2026-10-04T00:00:00Z",
            operator_id="test",
            aircraft_id="sim",
        ),
        SharedTaskPolicy(ranker, artifact),
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )

    def dispatch(context, plan, decision, mission_id):
        compilation = compile_model_flight_plan(
            candidate.task,
            context,
            "multirotor",
            runtime=runtimes[candidate.task.task_id],
            flight_plan=plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            mission=compilation.compiled_mission.executable_mission,
            flight_plan_sha256=plan.canonical_hash,
            mission_sha256=compilation.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=decision.selected_task_id,
        )

    bridge = OnboardRealtimeModelBridge(
        OnboardMissionEventLoop(
            OnboardMissionAdapter(
                executor,
                mission_id_prefix="autonomous-sortie",
                flight_plan_dispatcher=dispatch,
                safety_dispatcher=lambda _decision: None,
            )
        ),
        candidates=candidates,
        runtimes=runtimes,
    )
    started: list[str] = []
    return_requests: list[str] = []
    reports: list[object] = []
    sortie = OnboardAutonomousSortie(
        bridge,
        mission_start_dispatcher=lambda _context, mission, _revision: started.append(
            mission.mission_id
        ),
        return_request_dispatcher=lambda _context, revision: return_requests.append(
            revision.decision.reason
        ),
        report_dispatcher=reports.append,
    )

    initial = sortie.prepare(_context(10.0), mission_id="autonomous-initial")
    assert initial.mission is not None
    assert len(started) == 1
    assert sortie.ingest_telemetry(_context(11.0, altitude_m=12.0)) is True
    green = OnboardPerceptionObservation(
        observation_id="sortie-green",
        observed_at_s=12.0,
        source="gazebo-rgb-depth",
        target_visible=True,
        target_class="vehicle",
        target_color="green",
        target_class_match=True,
        target_color_match=False,
        target_bearing_deg=-80.0,
        target_distance_m=80.0,
        target_confidence=0.90,
        target_match_score=0.12,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
    )
    red = OnboardPerceptionObservation(
        observation_id="sortie-red",
        observed_at_s=14.0,
        source="gazebo-rgb-depth",
        target_visible=True,
        target_class="vehicle",
        target_color="red",
        target_class_match=True,
        target_color_match=True,
        target_bearing_deg=15.0,
        target_distance_m=55.0,
        target_north_offset_m=50.0,
        target_east_offset_m=12.0,
        target_confidence=0.97,
        target_match_score=0.98,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
    )
    first_green_revision = sortie.ingest_perception(_context(12.0), green)
    assert first_green_revision.task_receipt is not None
    duplicate_green_revision = sortie.ingest_perception(
        _context(12.1),
        replace(green, observation_id="sortie-green-duplicate", observed_at_s=12.1),
    )
    assert duplicate_green_revision is first_green_revision
    # The second camera tick is the same coarse view.  It must not upload a
    # second identical Mission while PX4 is still executing the search leg.
    assert len(runner.inputs) == 1
    lost_green_revision = sortie.ingest_perception(
        _context(12.2),
        replace(
            green,
            observation_id="sortie-green-temporary-loss",
            observed_at_s=12.2,
            target_visible=False,
            target_class=None,
            target_color=None,
            target_class_match=False,
            target_color_match=False,
            target_bearing_deg=None,
            target_distance_m=None,
            target_confidence=0.0,
            target_match_score=0.0,
        ),
    )
    assert lost_green_revision is first_green_revision
    assert len(runner.inputs) == 1
    matched, recorded = sortie.confirm_and_record_target_location(_context(14.0), red)
    assert matched.task_receipt is not None
    assert matched.task_receipt.phase is OnboardTaskAction.MATCH_TARGET
    assert recorded.task_receipt is not None
    assert recorded.task_receipt.target_position is not None
    returned, reported = sortie.complete_return_and_report(_context(18.0))
    assert returned.task_receipt is not None
    assert returned.task_receipt.phase is OnboardTaskAction.RETURN_HOME
    assert reported.task_receipt is not None
    assert reported.task_receipt.phase is OnboardTaskAction.REPORT
    assert sortie.status.report_committed is True
    assert sortie.status.target_position is not None
    assert len(return_requests) == 1
    assert len(reports) == 1
    assert len(started) == 3


def test_neutral_target_match_is_not_suppressed_as_duplicate_search() -> None:
    observation = OnboardPerceptionObservation(
        observation_id="neutral-target",
        observed_at_s=1.0,
        source="gazebo-rgb-depth",
        target_visible=True,
        target_class="vehicle",
        target_color=None,
        target_class_match=True,
        target_color_match=None,
        target_bearing_deg=15.0,
        target_distance_m=55.0,
        target_confidence=0.92,
        target_match_score=0.90,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
    )

    assert OnboardAutonomousSortie._search_signature(observation) is None


def test_realtime_bridge_preserves_event_idempotency_and_rejects_changed_payload() -> None:
    bridge, _runner, _candidates, _runtimes = _bridge()
    bridge.prepare(_context(10.0), mission_id="external-model-initial")
    observation = OnboardPerceptionObservation(
        observation_id="frame-one",
        observed_at_s=12.0,
        source="jetson-detector-tracker",
        target_visible=True,
        target_class="vehicle",
        target_color="red",
        target_class_match=True,
        target_color_match=True,
        target_bearing_deg=5.0,
        target_distance_m=50.0,
        target_confidence=0.95,
        target_match_score=0.96,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
    )
    first = bridge.submit(_context(12.0), observation)
    retry = bridge.submit(_context(13.0), observation)
    assert retry is first
    assert len(bridge.observations) == 1
    changed = OnboardPerceptionObservation(
        observation_id="frame-one",
        observed_at_s=12.0,
        source="jetson-detector-tracker",
        target_visible=True,
        target_class="vehicle",
        target_color="green",
        target_class_match=True,
        target_color_match=False,
        target_bearing_deg=5.0,
        target_distance_m=50.0,
        target_confidence=0.95,
        target_match_score=0.10,
        obstacle_distance_m=5_000.0,
        task_phase=OnboardTaskAction.SEARCH,
    )
    with pytest.raises(RuntimeError, match="event ID collision"):
        bridge.submit(_context(14.0), changed)
    assert bridge.event_loop.adapter.faulted is True
