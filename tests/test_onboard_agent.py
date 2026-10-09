from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    ModelBackedDecisionProvider,
    OnboardAIExecutor,
    OnboardDecision,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardFlightPlan,
    OnboardFlightPlanDispatch,
    OnboardMissionAdapter,
    OnboardMissionAdapterError,
    OnboardMissionEventLoop,
    OnboardModelInput,
    OnboardModelProposal,
    OnboardOnnxModelError,
    OnboardOnnxModelRunner,
    OnboardResponse,
    OnboardRuntimeEvent,
    OnboardTaskAction,
    OnboardTaskStateError,
    OnboardTaskStateMachine,
    PlanAction,
    PromptEnvelope,
    PromptResolutionError,
    PromptTaskResolver,
    PromptTaskTemplate,
    ResolvedTaskSpec,
    SharedTaskPolicy,
    TaskConstraints,
    TaskGeometry,
    TaskPolicyRuntimeBinding,
    TaskType,
)
from multidetect.autonomy.onboard_navigation import compile_model_flight_plan
from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.task_ranker import TaskRanker
from multidetect.autonomy.task_selection import TaskCandidate
from multidetect.px4_mission import (
    MultirotorMissionGeometry,
    Px4MultirotorSafetyPolicy,
)
from multidetect.simulator import SimulatorClosedLoop

PROFILE_HASH = "b" * 64
HOME = GeoPoint(1.3, 103.8)


def _task(task_id: str = "task-1") -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id=task_id,
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="corridor-1",
            direction="northbound",
            length_m=400.0,
            centerline=(HOME, GeoPoint(1.304, 103.8)),
            resolution_confidence=0.95,
            buffer_m=20.0,
        ),
        target_classes=("person",),
        on_detection=(PlanAction.OBSERVE_TARGET, PlanAction.CAPTURE_EVIDENCE),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def _context(at_s: float = 10.0) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id=f"ctx-{at_s}",
        captured_at_s=at_s,
        captured_at_utc=datetime.fromtimestamp(at_s).isoformat() + "Z",
        aircraft_position=HOME,
        home_position=HOME,
        airframe_profile_id="px4_multirotor_gazebo_v1",
        airframe_profile_hash=PROFILE_HASH,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1800.0,
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


def _executor() -> tuple[OnboardAIExecutor, list[TaskCandidate], dict[str, object]]:
    ranker = TaskRanker((0, 0, 0), "onboard-agent-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="onboard-task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    prompt = PromptEnvelope(
        prompt_id="prompt-1",
        text="沿走廊搜索人员并记录证据",
        language="zh-CN",
        issued_at="2026-10-03T00:00:00Z",
        operator_id="operator-1",
        aircraft_id="aircraft-1",
    )
    candidates = [TaskCandidate(_task())]
    runtime = _runtime()
    return (
        OnboardAIExecutor(
            prompt,
            SharedTaskPolicy(ranker, artifact),
            "multirotor",
            PROFILE_HASH,
            allow_deterministic_baseline=True,
        ),
        candidates,
        {"task-1": runtime},
    )


class _ModelDecisionProvider:
    def __init__(
        self,
        response: OnboardResponse,
        *,
        confidence: float = 0.87,
        flight_plan: OnboardFlightPlan | None = None,
        task_action: OnboardTaskAction | None = None,
    ) -> None:
        self.response = response
        self.confidence = confidence
        self.flight_plan = flight_plan
        self.task_action = task_action
        self.calls: list[tuple[str, OnboardEventKind]] = []

    def propose(self, context: ContextSnapshot, event: OnboardRuntimeEvent) -> OnboardModelProposal:
        self.calls.append((context.snapshot_id, event.kind))
        return OnboardModelProposal(
            response=self.response,
            reason=f"model_{self.response.value}",
            confidence=self.confidence,
            model_id="onboard-policy",
            model_version="0.3.0-pre.8-test",
            model_sha256="a" * 64,
            provider="test-provider",
            inference_latency_ms=4.2,
            flight_plan=self.flight_plan,
            task_action=self.task_action,
        )


class _ModelRunner:
    def __init__(self, output: object) -> None:
        self.output = output
        self.inputs: list[OnboardModelInput] = []

    def infer(self, model_input: OnboardModelInput) -> object:
        self.inputs.append(model_input)
        return self.output


class _SequenceModelProvider:
    def __init__(self, proposals: list[OnboardModelProposal]) -> None:
        self._proposals = iter(proposals)

    def propose(self, context: ContextSnapshot, event: OnboardRuntimeEvent) -> OnboardModelProposal:
        return next(self._proposals)


class _TensorInfo:
    def __init__(self, name: str, shape: object = None) -> None:
        self.name = name
        self.shape = shape


class _OnnxSession:
    def __init__(self, output: object, *, input_width: int | None = None) -> None:
        self.output = output
        self.input_width = input_width
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def get_inputs(self) -> list[_TensorInfo]:
        shape = None if self.input_width is None else (1, self.input_width)
        return [_TensorInfo("features", shape)]

    def get_outputs(self) -> list[_TensorInfo]:
        return [_TensorInfo("logits")]

    def run(self, names: list[str], feeds: dict[str, object]) -> list[object]:
        self.calls.append((names, feeds))
        return [self.output]


def test_model_backed_provider_adapts_runner_output_and_binds_identity() -> None:
    runner = _ModelRunner(
        {
            "response": "replan",
            "reason": "model_target_moved",
            "confidence": 0.91,
        }
    )
    ticks = iter((10.0, 10.0125))
    provider = ModelBackedDecisionProvider(
        runner,
        model_id="onboard-policy",
        model_version="0.3.0-pre.8-test",
        model_sha256="c" * 64,
        provider="onnxruntime:CUDAExecutionProvider",
        clock=lambda: next(ticks),
    )

    context = _context(12.0)
    event = OnboardRuntimeEvent("model-runner", OnboardEventKind.TARGET_MOVED, 12.0)
    proposal = provider.propose(context, event)

    assert proposal.response is OnboardResponse.REPLAN
    assert proposal.reason == "model_target_moved"
    assert proposal.confidence == pytest.approx(0.91)
    assert proposal.model_sha256 == "c" * 64
    assert proposal.provider == "onnxruntime:CUDAExecutionProvider"
    assert proposal.inference_latency_ms == pytest.approx(12.5)
    assert len(runner.inputs) == 1
    assert runner.inputs[0].context is context
    assert runner.inputs[0].event is event
    assert runner.inputs[0].to_mapping()["truth_free"] is True
    assert len(runner.inputs[0].canonical_hash) == 64


def test_onnx_runner_uses_shared_feature_vector_and_decodes_logits() -> None:
    import numpy as np

    session = _OnnxSession(
        np.asarray(
            [[4.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]],
            dtype=np.float32,
        )
    )
    runner = OnboardOnnxModelRunner(session)
    model_input = OnboardModelInput(
        _context(12.0), OnboardRuntimeEvent("onnx-event", OnboardEventKind.OBSTACLE_RISK, 12.0)
    )

    output = runner.infer(model_input)

    assert output.response is OnboardResponse.HOLD
    assert output.reason == "model_plan_hold_position"
    assert output.confidence > 0.9
    assert output.flight_plan is not None
    assert output.flight_plan.action.value == "hold_position"
    assert session.calls[0][0] == ["logits"]
    features = session.calls[0][1]["features"]
    assert tuple(features.shape) == (1, 40)
    assert tuple(features[0]) == pytest.approx(model_input.feature_vector())


def test_onnx_runner_keeps_r3_feature_width_compatible() -> None:
    import numpy as np

    session = _OnnxSession(np.asarray([[4.0, *([0.0] * 10)]], dtype=np.float32), input_width=38)
    runner = OnboardOnnxModelRunner(session)
    model_input = OnboardModelInput(
        _context(12.0),
        OnboardRuntimeEvent(
            "legacy-input",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            target_class="vehicle",
            target_color="green",
            target_class_match=True,
            target_color_match=False,
        ),
    )
    runner.infer(model_input)
    features = session.calls[0][1]["features"]
    assert tuple(features.shape) == (1, 38)
    assert tuple(features[0]) == pytest.approx(model_input.legacy_feature_vector())


def test_structured_target_attributes_are_in_model_input_and_receipt() -> None:
    event = OnboardRuntimeEvent(
        "green-rejected",
        OnboardEventKind.TARGET_MOVED,
        12.0,
        target_class="Vehicle",
        target_color="Green",
        target_class_match=True,
        target_color_match=False,
    )
    model_input = OnboardModelInput(_context(12.0), event)
    mapping = model_input.to_mapping()
    assert mapping["event"]["target_class"] == "vehicle"
    assert mapping["event"]["target_color"] == "green"
    names = mapping["feature_names"]
    values = mapping["features"]
    assert values[names.index("target_class_match")] == 1.0
    assert values[names.index("target_color_match")] == 0.0


def test_camera_relative_bearing_is_not_consumed_as_ned_model_geometry() -> None:
    event = OnboardRuntimeEvent(
        "uncalibrated-bearing",
        OnboardEventKind.TARGET_MOVED,
        12.0,
        target_bearing_deg=45.0,
        target_distance_m=20.0,
        target_geometry_frame="camera_relative",
    )
    model_input = OnboardModelInput(_context(12.0), event)
    names = model_input.to_mapping()["feature_names"]
    values = model_input.feature_vector()
    assert values[names.index("target_bearing_fraction")] == 0.0
    assert event.to_mapping()["target_geometry_frame"] == "camera_relative"

    with pytest.raises(ValueError, match="camera_relative geometry"):
        OnboardRuntimeEvent(
            "invalid-frame",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            target_geometry_frame="camera_relative",
            target_north_offset_m=1.0,
            target_east_offset_m=0.0,
        )


def test_record_location_rejects_camera_relative_geometry_without_gps_receipt() -> None:
    state = OnboardTaskStateMachine()
    takeoff_context = replace(
        _context(),
        aircraft_position=GeoPoint(HOME.latitude_deg, HOME.longitude_deg, 20.0, "relative_home"),
    )
    assert state.mark_takeoff_complete(takeoff_context) is True

    selected_binding = TaskPolicyRuntimeBinding(
        vehicle_class="multirotor",
        context_sha256=takeoff_context.canonical_hash,
        airframe_profile_hash=PROFILE_HASH,
        action="select",
        reason="selected",
        selected_task_id="task-1",
        policy_artifact_sha256="1" * 64,
        ranker_sha256="2" * 64,
        plan_sha256="3" * 64,
        executable_mission_sha256="4" * 64,
        wire_hash="5" * 64,
        planner_revalidated=True,
        mission_bound=True,
    )
    match_event = OnboardRuntimeEvent(
        "match-before-record",
        OnboardEventKind.TARGET_MOVED,
        12.0,
        target_confidence=0.95,
        target_match_score=0.95,
        target_class="vehicle",
        target_color="red",
        target_class_match=True,
        target_color_match=True,
        target_geometry_frame="ned",
        target_north_offset_m=5.0,
        target_east_offset_m=2.0,
    )
    match_decision = OnboardDecision(
        prompt_sha256="6" * 64,
        context_sha256=takeoff_context.canonical_hash,
        event_sha256=match_event.canonical_hash,
        response=OnboardResponse.REPLAN,
        reason="model_match",
        binding=selected_binding,
        model_proposal=OnboardModelProposal(
            response=OnboardResponse.REPLAN,
            reason="model_match",
            confidence=0.9,
            model_id="onboard-policy",
            model_version="test",
            model_sha256="7" * 64,
            provider="test-provider",
            inference_latency_ms=1.0,
            flight_plan=OnboardFlightPlan(OnboardFlightAction.MOVE_RELATIVE),
            task_action=OnboardTaskAction.MATCH_TARGET,
        ),
    )
    assert (
        state.validate(takeoff_context, match_event, match_decision)
        is OnboardTaskAction.MATCH_TARGET
    )
    state.commit(takeoff_context, match_event, match_decision, OnboardTaskAction.MATCH_TARGET)

    record_context = _context(14.0)
    record_event = OnboardRuntimeEvent(
        "record-camera-relative",
        OnboardEventKind.TARGET_MOVED,
        14.0,
        target_confidence=0.99,
        target_match_score=0.99,
        target_class="vehicle",
        target_color="red",
        target_class_match=True,
        target_color_match=True,
        target_bearing_deg=45.0,
        target_distance_m=20.0,
        target_geometry_frame="camera_relative",
    )
    record_decision = OnboardDecision(
        prompt_sha256="6" * 64,
        context_sha256=record_context.canonical_hash,
        event_sha256=record_event.canonical_hash,
        response=OnboardResponse.RETURN,
        reason="model_record",
        binding=TaskPolicyRuntimeBinding.from_decision(
            context=record_context,
            airframe_profile_hash=PROFILE_HASH,
            vehicle_class="multirotor",
            decision=None,
            selected=None,
            policy_artifact_sha256="1" * 64,
            ranker_sha256="2" * 64,
            reason="model_record",
        ),
        model_proposal=OnboardModelProposal(
            response=OnboardResponse.RETURN,
            reason="model_record",
            confidence=0.9,
            model_id="onboard-policy",
            model_version="test",
            model_sha256="7" * 64,
            provider="test-provider",
            inference_latency_ms=1.0,
            flight_plan=OnboardFlightPlan(OnboardFlightAction.RETURN_HOME),
            task_action=OnboardTaskAction.RECORD_LOCATION,
        ),
    )
    with pytest.raises(
        OnboardTaskStateError,
        match="calibrated NED offsets",
    ):
        state.validate(record_context, record_event, record_decision)
    assert state.receipts[-1].phase is OnboardTaskAction.MATCH_TARGET
    assert state.receipts[-1].target_position is None


def test_onnx_runner_decodes_task_step_and_navigation_plan() -> None:
    import numpy as np

    # 9 task logits + 7 flight logits + north/east/altitude/speed regressions.
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 1] = 5.0  # search
    logits[0, 9 + 1] = 5.0  # move_relative
    logits[0, 16] = 0.25  # north offset
    logits[0, 17] = -0.5  # east offset (left/right search is signed)
    session = _OnnxSession(logits)
    runner = OnboardOnnxModelRunner(session)

    output = runner.infer(
        OnboardModelInput(
            _context(12.0),
            OnboardRuntimeEvent(
                "red-car-search",
                OnboardEventKind.TARGET_MOVED,
                12.0,
                target_bearing_deg=-45.0,
                target_distance_m=80.0,
                target_confidence=0.62,
            ),
        )
    )

    assert output.task_action is OnboardTaskAction.SEARCH
    assert output.response is OnboardResponse.REPLAN
    assert output.reason == "model_task_search_plan_move_relative"
    assert output.flight_plan is not None
    assert output.flight_plan.action.value == "move_relative"
    assert output.flight_plan.north_offset_m > 0.0
    assert output.flight_plan.east_offset_m < 0.0


def test_onnx_runner_masks_incompatible_navigation_logit_using_shared_task_contract() -> None:
    import numpy as np

    # The independent heads can temporarily disagree during fine-tuning.  A
    # MATCH_TARGET step must never turn into RETURN_HOME; keep the same model
    # and select the highest-scoring action from that step's allowed set.
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 3] = 6.0  # match_target task head
    logits[0, 9 + 5] = 8.0  # raw RETURN_HOME (incompatible with MATCH_TARGET)
    logits[0, 9 + 4] = 5.0  # FOLLOW_TARGET (compatible fallback)
    output = OnboardOnnxModelRunner(_OnnxSession(logits)).infer(
        OnboardModelInput(
            _context(12.0),
            OnboardRuntimeEvent(
                "match-target-compatibility",
                OnboardEventKind.TARGET_MOVED,
                12.0,
                target_class="vehicle",
                target_color="red",
                target_class_match=True,
                target_color_match=True,
            ),
        )
    )

    assert output.task_action is OnboardTaskAction.MATCH_TARGET
    assert output.flight_plan is not None
    assert output.flight_plan.action is OnboardFlightAction.FOLLOW_TARGET
    assert output.reason.endswith("_compatibility_masked")


def test_onnx_runner_does_not_jump_from_observed_search_back_to_takeoff() -> None:
    import numpy as np

    # A noisy task head may prefer TAKEOFF after a long sequence of green
    # distractor frames.  The live perception phase is already SEARCH, so the
    # same shared model must stay within SEARCH/INSPECT/MATCH transitions and
    # must not trigger an airborne safety-mode request.
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 0] = 9.0  # raw TAKEOFF task logit
    logits[0, 1] = 2.0  # SEARCH task logit (allowed fallback)
    logits[0, 9 + 1] = 5.0  # MOVE_RELATIVE
    output = OnboardOnnxModelRunner(_OnnxSession(logits)).infer(
        OnboardModelInput(
            _context(12.0),
            OnboardRuntimeEvent(
                "green-search-phase",
                OnboardEventKind.TARGET_MOVED,
                12.0,
                target_class="vehicle",
                target_color="green",
                target_class_match=True,
                target_color_match=False,
                target_confidence=0.90,
                target_match_score=0.0,
                task_phase=OnboardTaskAction.SEARCH,
            ),
        )
    )

    assert output.task_action is OnboardTaskAction.SEARCH
    assert output.flight_plan is not None
    assert output.flight_plan.action is OnboardFlightAction.MOVE_RELATIVE
    assert output.reason.endswith("_task_phase_masked")


def test_onnx_runner_honors_qualified_recording_phase_without_emitting_transport() -> None:
    import numpy as np

    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 5] = 7.0  # raw return_home task logit
    logits[0, 9 + 5] = 6.0
    output = OnboardOnnxModelRunner(_OnnxSession(logits)).infer(
        OnboardModelInput(
            _context(12.0),
            OnboardRuntimeEvent(
                "qualified-location",
                OnboardEventKind.TARGET_MOVED,
                12.0,
                target_class="vehicle",
                target_color="red",
                target_class_match=True,
                target_color_match=True,
                target_bearing_deg=15.0,
                target_distance_m=55.0,
                target_north_offset_m=52.0,
                target_east_offset_m=14.0,
                target_confidence=0.99,
                target_match_score=0.99,
                task_phase=OnboardTaskAction.RECORD_LOCATION,
            ),
        )
    )

    assert output.task_action is OnboardTaskAction.RECORD_LOCATION
    assert output.flight_plan is not None
    assert output.flight_plan.action is OnboardFlightAction.RETURN_HOME
    assert output.response is OnboardResponse.RETURN
    assert output.reason.endswith("_task_phase_masked")


def test_onnx_runner_allows_model_safety_transition_to_interrupt_search_phase() -> None:
    import numpy as np

    # A health event must expose the model's HOLD/RETURN/LAND decision even
    # while the perception phase is SEARCH.  The safety adapter still owns the
    # final PX4/MAVLink decision; this test only covers semantic model masking.
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 1] = 8.0  # noisy SEARCH preference
    logits[0, 7] = 6.0  # HOLD semantic proposal
    logits[0, 9 + 0] = 7.0  # HOLD_POSITION
    output = OnboardOnnxModelRunner(_OnnxSession(logits)).infer(
        OnboardModelInput(
            _context(12.0),
            OnboardRuntimeEvent(
                "gps-degraded-interrupt",
                OnboardEventKind.GPS_DEGRADED,
                12.0,
                severity=60,
                task_phase=OnboardTaskAction.SEARCH,
            ),
        )
    )

    assert output.task_action is OnboardTaskAction.HOLD
    assert output.flight_plan is not None
    assert output.flight_plan.action is OnboardFlightAction.HOLD_POSITION


def test_onnx_runner_rejects_wrong_output_shape() -> None:
    import numpy as np

    runner = OnboardOnnxModelRunner(_OnnxSession(np.zeros((1, 3), dtype=np.float32)))
    model_input = OnboardModelInput(
        _context(12.0), OnboardRuntimeEvent("onnx-invalid", OnboardEventKind.TARGET_LOST, 12.0)
    )
    with pytest.raises(OnboardOnnxModelError, match="output must have shape"):
        runner.infer(model_input)


def test_model_backed_provider_invalid_backend_output_fails_closed_in_executor() -> None:
    baseline, candidates, runtimes = _executor()
    runner = _ModelRunner({"response": "not-a-response", "reason": "bad", "confidence": 0.5})
    provider = ModelBackedDecisionProvider(
        runner,
        model_id="onboard-policy",
        model_version="0.3.0-pre.8-test",
        model_sha256="d" * 64,
        provider="test-runner",
        clock=lambda: 1.0,
    )
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )
    executor.prepare(_context(), candidates, runtimes)

    decision = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("invalid-model", OnboardEventKind.BATTERY_LOW, 12.0),
        candidates,
        runtimes,
    )

    assert decision.response is OnboardResponse.HOLD
    assert decision.reason == "model_provider_fault"
    assert decision.model_proposal is None


def test_executor_does_not_reactivate_fixed_event_map_without_model() -> None:
    baseline, candidates, runtimes = _executor()
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
    )
    executor.prepare(_context(), candidates, runtimes)

    decision = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("unconfigured-model", OnboardEventKind.BATTERY_LOW, 12.0),
        candidates,
        runtimes,
    )

    assert decision.response is OnboardResponse.HOLD
    assert decision.reason == "model_provider_unconfigured"
    assert decision.to_mapping()["decision_source"] == "safety_baseline"


def test_prompt_prepare_and_target_replan_share_compiled_policy_path() -> None:
    executor, candidates, runtimes = _executor()
    initial = executor.prepare(_context(), candidates, runtimes)
    assert initial.response is OnboardResponse.EXECUTE
    assert initial.selected_task_id == "task-1"
    assert initial.binding.mission_bound is True
    assert initial.binding.planner_revalidated is True
    assert initial.to_mapping()["mission_started"] is False
    assert initial.to_mapping()["policy_applied_to_actuator"] is False

    replanned = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-1", OnboardEventKind.TARGET_MOVED, 12.0, detail="target moved"),
        candidates,
        runtimes,
    )
    assert replanned.response is OnboardResponse.REPLAN
    assert replanned.selected_task_id == "task-1"
    assert replanned.binding.mission_bound is True
    assert replanned.event_sha256 is not None
    assert replanned.prompt_sha256 == executor.prompt.canonical_hash


def test_model_provider_owns_event_choice_and_can_select_hold() -> None:
    baseline, candidates, runtimes = _executor()
    provider = _ModelDecisionProvider(OnboardResponse.HOLD)
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )
    executor.prepare(_context(), candidates, runtimes)

    decision = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("model-battery", OnboardEventKind.BATTERY_LOW, 12.0),
        candidates,
        runtimes,
    )

    assert decision.response is OnboardResponse.HOLD
    assert decision.reason == "model_hold"
    assert decision.model_proposal is not None
    assert decision.to_mapping()["decision_source"] == "model"
    assert provider.calls == [("ctx-12.0", OnboardEventKind.BATTERY_LOW)]


def test_model_provider_can_replan_any_event_and_return_is_vetoed_without_gps() -> None:
    baseline, candidates, runtimes = _executor()
    replanner = _ModelDecisionProvider(OnboardResponse.REPLAN)
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=replanner,
    )
    executor.prepare(_context(), candidates, runtimes)
    replanned = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("model-obstacle", OnboardEventKind.OBSTACLE_RISK, 12.0),
        candidates,
        runtimes,
    )
    assert replanned.response is OnboardResponse.REPLAN
    assert replanned.binding.mission_bound is True
    assert replanned.model_proposal is not None

    returner = _ModelDecisionProvider(OnboardResponse.RETURN)
    unsafe_executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=returner,
    )
    unsafe_executor.prepare(_context(), candidates, runtimes)
    vetoed = unsafe_executor.step(
        replace(_context(12.0), gps_fix_type=2),
        OnboardRuntimeEvent("model-return-veto", OnboardEventKind.BATTERY_LOW, 12.0),
        candidates,
        runtimes,
    )
    assert vetoed.response is OnboardResponse.HOLD
    assert vetoed.reason == "model_return_vetoed_by_safety_envelope"
    assert vetoed.model_proposal is not None


def test_prompt_catalog_resolves_once_then_enters_shared_prepare_path() -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes, aircraft_ids=("aircraft-1",))},
    )
    resolution = resolver.resolve(executor.prompt)
    assert resolution.prompt_sha256 == executor.prompt.canonical_hash
    assert resolution.canonical_hash
    assert resolution.to_mapping()["truth_free"] is True
    prepared = executor.prepare_from_prompt(_context(), resolver)
    assert prepared.response is OnboardResponse.EXECUTE
    assert prepared.selected_task_id == "task-1"
    assert prepared.resolver_id == "catalog-v1"
    assert prepared.resolution_sha256 == resolution.canonical_hash
    assert prepared.to_mapping()["resolver_id"] == "catalog-v1"

    unknown = replace(executor.prompt, prompt_id="unregistered")
    unknown_executor = OnboardAIExecutor(
        unknown, executor.shared_policy, "multirotor", PROFILE_HASH
    )
    with pytest.raises(PromptResolutionError, match="not registered"):
        unknown_executor.prepare_from_prompt(_context(), resolver)

    wrong_aircraft = replace(executor.prompt, aircraft_id="aircraft-2")
    wrong_executor = OnboardAIExecutor(
        wrong_aircraft, executor.shared_policy, "multirotor", PROFILE_HASH
    )
    with pytest.raises(PromptResolutionError, match="aircraft"):
        wrong_executor.prepare_from_prompt(_context(), resolver)


def test_prompt_replan_cannot_escape_resolved_catalog() -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes, aircraft_ids=("aircraft-1",))},
    )
    executor.prepare_from_prompt(_context(), resolver)
    alternate = [TaskCandidate(_task("task-2"))]
    alternate_runtime = {"task-2": _runtime()}
    with pytest.raises(ValueError, match="outside the resolved prompt catalog"):
        executor.step(
            _context(12.0),
            OnboardRuntimeEvent("event-escape", OnboardEventKind.TARGET_MOVED, 12.0),
            alternate,
            alternate_runtime,
        )


def test_prompt_runtime_is_frozen_and_hash_bound() -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes)},
    )
    resolution = resolver.resolve(executor.prompt)
    original_hash = resolution.runtime_hashes["task-1"]
    resolution_hash = resolution.canonical_hash
    runtimes["task-1"]["mission_geometry"]["loiter_time_s"] = 999.0
    assert resolution.runtime_hashes["task-1"] == original_hash
    assert resolution.runtimes["task-1"]["mission_geometry"]["loiter_time_s"] == 15.0
    assert resolution.canonical_hash == resolution_hash
    with pytest.raises(TypeError):
        resolution.runtimes["task-1"]["mission_geometry"]["loiter_time_s"] = 999.0

    prepared = executor.prepare_from_prompt(_context(), resolver)
    replanned = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-frozen", OnboardEventKind.TARGET_MOVED, 12.0),
        resolution.candidates,
        resolution.runtimes,
    )
    assert prepared.resolution_sha256 == resolution_hash
    assert replanned.resolution_sha256 == resolution_hash
    assert replanned.response is OnboardResponse.REPLAN
    assert replanned.selected_task_id == "task-1"
    assert replanned.binding.mission_bound is True
    assert replanned.planner_revalidated is True


def test_prompt_replan_rejects_changed_runtime_for_the_same_task_id() -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes)},
    )
    initial = executor.prepare_from_prompt(_context(), resolver)
    changed_runtime = _runtime()
    changed_runtime["mission_geometry"]["loiter_time_s"] = 20.0
    with pytest.raises(ValueError, match="runtime is outside the resolved prompt catalog"):
        executor.step(
            _context(12.0),
            OnboardRuntimeEvent("event-runtime-change", OnboardEventKind.TARGET_MOVED, 12.0),
            candidates,
            {"task-1": changed_runtime},
        )

    retry = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-runtime-retry", OnboardEventKind.TARGET_MOVED, 12.0),
        candidates,
        runtimes,
    )
    assert retry.response is OnboardResponse.REPLAN
    assert retry.resolution_sha256 == initial.resolution_sha256


@pytest.mark.parametrize("prepare_again", ["direct", "from_prompt"])
def test_prompt_session_cannot_be_prepared_again(prepare_again: str) -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes)},
    )
    initial = executor.prepare_from_prompt(_context(), resolver)
    alternate_candidates = [TaskCandidate(_task("task-2"))]
    alternate_runtimes = {"task-2": _runtime()}
    alternate_resolver = PromptTaskResolver(
        "catalog-v2",
        {"prompt-1": PromptTaskTemplate(tuple(alternate_candidates), alternate_runtimes)},
    )
    with pytest.raises(ValueError, match="already prepared"):
        if prepare_again == "direct":
            executor.prepare(_context(11.0), alternate_candidates, alternate_runtimes)
        else:
            executor.prepare_from_prompt(_context(11.0), alternate_resolver)

    with pytest.raises(ValueError, match="outside the resolved prompt catalog"):
        executor.step(
            _context(12.0),
            OnboardRuntimeEvent("event-after-reprepare", OnboardEventKind.TARGET_MOVED, 12.0),
            alternate_candidates,
            alternate_runtimes,
        )
    retry = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-after-reprepare-retry", OnboardEventKind.TARGET_MOVED, 12.0),
        candidates,
        runtimes,
    )
    assert retry.resolution_sha256 == initial.resolution_sha256
    assert retry.selected_task_id == "task-1"


def test_prompt_step_defaults_to_frozen_resolution() -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes)},
    )
    initial = executor.prepare_from_prompt(_context(), resolver)
    runtimes["task-1"]["mission_geometry"]["loiter_time_s"] = 999.0
    candidates[:] = [TaskCandidate(_task("task-2"))]
    replanned = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-default-inputs", OnboardEventKind.TARGET_MOVED, 12.0),
    )
    assert replanned.response is OnboardResponse.REPLAN
    assert replanned.selected_task_id == "task-1"
    assert replanned.planner_revalidated is True
    assert replanned.resolution_sha256 == initial.resolution_sha256


@pytest.mark.parametrize(
    ("kind", "response"),
    [
        (OnboardEventKind.GPS_DEGRADED, OnboardResponse.RETURN),
        (OnboardEventKind.BATTERY_LOW, OnboardResponse.RETURN),
        (OnboardEventKind.OBSTACLE_RISK, OnboardResponse.HOLD),
        (OnboardEventKind.INFERENCE_FAULT, OnboardResponse.RETURN),
    ],
)
def test_prompt_safety_response_does_not_require_valid_task_inputs(
    kind: OnboardEventKind, response: OnboardResponse
) -> None:
    executor, candidates, runtimes = _executor()
    resolver = PromptTaskResolver(
        "catalog-v1",
        {"prompt-1": PromptTaskTemplate(tuple(candidates), runtimes)},
    )
    initial = executor.prepare_from_prompt(_context(), resolver)
    decision = executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-invalid-task-inputs", kind, 12.0),
        [None],
        {},
    )
    assert decision.response is response
    assert decision.binding.mission_bound is False
    assert decision.binding.policy_applied_to_actuator is False
    assert decision.resolution_sha256 == initial.resolution_sha256


@pytest.mark.parametrize("phase", ["prepare", "step"])
def test_onboard_agent_rejects_context_without_airframe_hash(phase: str) -> None:
    executor, candidates, runtimes = _executor()
    context = replace(_context(12.0), airframe_profile_hash=None)
    if phase == "step":
        executor.prepare(_context(), candidates, runtimes)
    with pytest.raises(ValueError, match="context airframe profile mismatch"):
        if phase == "prepare":
            executor.prepare(context, candidates, runtimes)
        else:
            executor.step(
                context,
                OnboardRuntimeEvent("event-missing-profile", OnboardEventKind.TARGET_MOVED, 12.0),
                candidates,
                runtimes,
            )


@pytest.mark.parametrize(
    ("kind", "response"),
    [
        (OnboardEventKind.GPS_DEGRADED, OnboardResponse.RETURN),
        (OnboardEventKind.BATTERY_LOW, OnboardResponse.RETURN),
        (OnboardEventKind.OBSTACLE_RISK, OnboardResponse.HOLD),
        (OnboardEventKind.INFERENCE_FAULT, OnboardResponse.RETURN),
    ],
)
def test_safety_events_never_bind_a_new_mission(
    kind: OnboardEventKind, response: OnboardResponse
) -> None:
    executor, candidates, runtimes = _executor()
    executor.prepare(_context(), candidates, runtimes)
    decision = executor.step(
        _context(11.0),
        OnboardRuntimeEvent(f"{kind.value}-1", kind, 11.0, severity=80),
        candidates,
        runtimes,
    )
    assert decision.response is response
    assert decision.binding.mission_bound is False
    assert decision.binding.mission_started is False
    assert decision.binding.policy_applied_to_actuator is False


def test_onboard_agent_rejects_regressed_context() -> None:
    executor, candidates, runtimes = _executor()
    executor.prepare(_context(10.0), candidates, runtimes)
    with pytest.raises(ValueError, match="timestamp regressed"):
        executor.step(
            _context(9.0),
            OnboardRuntimeEvent("event-1", OnboardEventKind.TARGET_LOST, 10.0),
            candidates,
            runtimes,
        )


def test_onboard_agent_rejects_event_newer_than_its_context() -> None:
    executor, candidates, runtimes = _executor()
    executor.prepare(_context(10.0), candidates, runtimes)
    with pytest.raises(ValueError, match="event is newer than the current context"):
        executor.step(
            _context(12.0),
            OnboardRuntimeEvent("event-future", OnboardEventKind.TARGET_MOVED, 13.0),
            candidates,
            runtimes,
        )


def test_onboard_agent_rejects_event_timestamp_reordering() -> None:
    executor, candidates, runtimes = _executor()
    executor.prepare(_context(10.0), candidates, runtimes)
    executor.step(
        _context(12.0),
        OnboardRuntimeEvent("event-later", OnboardEventKind.TARGET_MOVED, 12.0),
        candidates,
        runtimes,
    )
    with pytest.raises(ValueError, match="event predates the current context"):
        executor.step(
            _context(13.0),
            OnboardRuntimeEvent("event-earlier", OnboardEventKind.TARGET_MOVED, 11.0),
            candidates,
            runtimes,
        )


def test_onboard_adapter_replaces_mission_with_event_revision() -> None:
    executor, candidates, runtimes = _executor()
    installed = []
    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-event",
        mission_installer=installed.append,
    )

    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    replanned = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent("target-moved-1", OnboardEventKind.TARGET_MOVED, 12.0),
    )

    assert initial.mission is not None
    assert initial.mission.mission_id == "sim-event-initial"
    assert replanned.mission is not None
    assert replanned.mission.mission_id == "sim-event-event-1"
    assert replanned.mission_replaced is True
    assert replanned.mission_cleared is False
    assert replanned.decision.response is OnboardResponse.REPLAN
    assert replanned.decision.event_sha256 is not None
    assert adapter.current_mission == replanned.mission
    assert [item.mission_id for item in installed] == [
        "sim-event-initial",
        "sim-event-event-1",
    ]
    assert [item.sequence for item in adapter.history] == [0, 1]


def test_onboard_adapter_dispatches_model_navigation_plan_to_mission_seam() -> None:
    baseline, candidates, runtimes = _executor()
    provider = _ModelDecisionProvider(
        OnboardResponse.REPLAN,
        flight_plan=OnboardFlightPlan(
            OnboardFlightAction.MOVE_RELATIVE,
            north_offset_m=12.0,
            east_offset_m=-8.0,
            altitude_m=35.0,
            speed_mps=8.0,
        ),
        task_action=OnboardTaskAction.SEARCH,
    )
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )
    installed = []
    dispatches = []
    compilations = []

    def dispatch(context, plan, decision, mission_id):
        dispatches.append((context, plan, decision, mission_id))
        compilation = compile_model_flight_plan(
            candidates[0].task,
            context,
            "multirotor",
            runtime=runtimes["task-1"],
            flight_plan=plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        compilations.append(compilation)
        mission = compilation.compiled_mission.executable_mission
        return OnboardFlightPlanDispatch(
            mission=mission,
            flight_plan_sha256=plan.canonical_hash,
            mission_sha256=mission.canonical_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=decision.selected_task_id,
        )

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-model-plan",
        mission_installer=installed.append,
        flight_plan_dispatcher=dispatch,
    )
    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    revision = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent(
            "red-car-search",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            obstacle_distance_m=5000.0,
        ),
    )

    assert revision.flight_plan_dispatched is True
    assert revision.mission_replaced is True
    assert revision.mission is not None
    assert revision.mission.mission_id == "sim-model-plan-event-1"
    assert dispatches[0][1].east_offset_m == -8.0
    assert dispatches[0][2].model_proposal is not None
    assert revision.mission.items[1].latitude_deg != initial.mission.items[1].latitude_deg
    assert revision.mission.items[1].longitude_deg != initial.mission.items[1].longitude_deg
    assert revision.decision.binding.executable_mission_sha256 == revision.mission.canonical_hash
    assert revision.decision.binding.plan_sha256 == compilations[0].plan.canonical_hash
    assert revision.decision.binding.wire_hash == compilations[0].wire_hash
    assert revision.decision.binding.plan_sha256 != initial.decision.binding.plan_sha256
    assert revision.task_receipt is not None
    assert revision.task_receipt.phase is OnboardTaskAction.SEARCH
    assert adapter.task_state.phase is OnboardTaskAction.SEARCH
    assert installed == [initial.mission, revision.mission]


def test_calibrated_target_geometry_replaces_follow_regression_before_compile() -> None:
    baseline, candidates, runtimes = _executor()
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_ModelDecisionProvider(
            OnboardResponse.REPLAN,
            flight_plan=OnboardFlightPlan(
                OnboardFlightAction.FOLLOW_TARGET,
                north_offset_m=1.0,
                east_offset_m=1.0,
            ),
            task_action=OnboardTaskAction.SEARCH,
        ),
    )
    dispatches = []

    def dispatch(context, plan, decision, mission_id):
        dispatches.append(plan)
        compilation = compile_model_flight_plan(
            candidates[0].task,
            context,
            "multirotor",
            runtime=runtimes["task-1"],
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

    adapter = OnboardMissionAdapter(executor, flight_plan_dispatcher=dispatch)
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    revision = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent(
            "calibrated-follow",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            target_geometry_frame="ned",
            target_north_offset_m=42.0,
            target_east_offset_m=-18.0,
            target_distance_m=45.7,
            target_confidence=0.95,
            target_match_score=0.90,
            target_class_match=True,
            obstacle_distance_m=5000.0,
        ),
    )

    assert revision.mission is not None
    assert dispatches[0].north_offset_m == pytest.approx(42.0)
    assert dispatches[0].east_offset_m == pytest.approx(-18.0)
    assert dispatches[0].reference_id == "perception:calibrated-follow"
    assert revision.decision.model_proposal is not None
    assert revision.decision.model_proposal.flight_plan == dispatches[0]


def test_uncalibrated_camera_geometry_vetoes_absolute_follow_navigation() -> None:
    baseline, candidates, runtimes = _executor()
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_ModelDecisionProvider(
            OnboardResponse.REPLAN,
            flight_plan=OnboardFlightPlan(
                OnboardFlightAction.FOLLOW_TARGET,
                north_offset_m=8.0,
                east_offset_m=-8.0,
            ),
            task_action=OnboardTaskAction.SEARCH,
        ),
    )
    adapter = OnboardMissionAdapter(executor)
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    revision = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent(
            "uncalibrated-follow",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            target_geometry_frame="camera_relative",
            target_bearing_deg=20.0,
            target_distance_m=30.0,
            obstacle_distance_m=5000.0,
        ),
    )
    assert revision.decision.response is OnboardResponse.HOLD
    assert revision.decision.reason == "model_navigation_vetoed_uncalibrated_geometry"
    assert revision.mission is None


def test_active_task_local_replan_survives_full_route_budget_exhaustion() -> None:
    """A short model search step remains valid after the prompt route no longer fits."""

    baseline, candidates, runtimes = _executor()
    provider = _ModelDecisionProvider(
        OnboardResponse.REPLAN,
        flight_plan=OnboardFlightPlan(
            OnboardFlightAction.SEARCH,
            north_offset_m=12.0,
            east_offset_m=-8.0,
            altitude_m=35.0,
            speed_mps=8.0,
        ),
        task_action=OnboardTaskAction.SEARCH,
    )
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )
    installed = []

    def dispatch(context, plan, decision, mission_id):
        compilation = compile_model_flight_plan(
            candidates[0].task,
            context,
            "multirotor",
            runtime=runtimes["task-1"],
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
            selected_task_id=decision.selected_task_id,
        )

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="active-task-budget",
        mission_installer=installed.append,
        flight_plan_dispatcher=dispatch,
    )
    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    low_energy = replace(
        _context(12.0),
        battery_remaining_pct=51.0,
        estimated_flight_time_s=918.0,
    )
    revision = adapter.handle_event(
        low_energy,
        OnboardRuntimeEvent(
            "local-search-after-budget",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            obstacle_distance_m=5_000.0,
        ),
    )

    assert initial.mission is not None
    assert revision.mission is not None
    assert revision.decision.response is OnboardResponse.REPLAN
    assert revision.decision.selected_task_id == "task-1"
    assert revision.decision.reason == "active_task_local_replan_after_route_budget"
    assert revision.decision.planner_revalidated is True
    assert revision.flight_plan_dispatched is True
    assert len(installed) == 2


def test_search_replan_accepts_candidate_confidence_below_general_replan_gate() -> None:
    """A weak reacquisition proposal may keep searching, but is never a match."""

    baseline, candidates, runtimes = _executor()
    provider = _ModelDecisionProvider(
        OnboardResponse.REPLAN,
        confidence=0.30,
        flight_plan=OnboardFlightPlan(
            OnboardFlightAction.MOVE_RELATIVE,
            north_offset_m=10.0,
            east_offset_m=-10.0,
            altitude_m=40.0,
            speed_mps=8.0,
        ),
        task_action=OnboardTaskAction.SEARCH,
    )
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=provider,
    )

    executor.prepare(_context(12.0), candidates, runtimes)
    decision = executor.step(
        _context(13.0),
        OnboardRuntimeEvent(
            "search-loss",
            OnboardEventKind.TARGET_LOST,
            13.0,
            obstacle_distance_m=5_000.0,
        ),
        candidates,
        runtimes,
        mission_id="search-replan-low-confidence",
    )

    assert decision.response is OnboardResponse.REPLAN
    assert decision.model_proposal is not None
    assert decision.model_proposal.confidence == pytest.approx(0.30)
    assert decision.model_proposal.task_action is OnboardTaskAction.SEARCH
    assert decision.binding.mission_bound is True
    assert decision.binding.policy_applied_to_actuator is False


@pytest.mark.parametrize("invalid_provenance", ["missing_task", "wrong_task", "stale_intent"])
def test_onboard_adapter_rejects_misbound_model_navigation_before_install(
    invalid_provenance: str,
) -> None:
    baseline, candidates, runtimes = _executor()
    plan = OnboardFlightPlan(OnboardFlightAction.MOVE_RELATIVE, east_offset_m=-8.0)
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_ModelDecisionProvider(
            OnboardResponse.REPLAN,
            flight_plan=plan,
            task_action=OnboardTaskAction.SEARCH,
        ),
    )
    installed = []

    def dispatch(context, requested_plan, decision, mission_id):
        compiled_plan = (
            replace(requested_plan, east_offset_m=8.0)
            if invalid_provenance == "stale_intent"
            else requested_plan
        )
        compilation = compile_model_flight_plan(
            candidates[0].task,
            context,
            "multirotor",
            runtime=runtimes["task-1"],
            flight_plan=compiled_plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            mission=compilation.compiled_mission.executable_mission,
            flight_plan_sha256=requested_plan.canonical_hash,
            mission_sha256=compilation.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compilation.plan.canonical_hash,
            wire_hash=compilation.wire_hash,
            compilation=compilation,
            selected_task_id=(
                None
                if invalid_provenance == "missing_task"
                else "wrong-task"
                if invalid_provenance == "wrong_task"
                else decision.selected_task_id
            ),
        )

    adapter = OnboardMissionAdapter(
        executor, mission_installer=installed.append, flight_plan_dispatcher=dispatch
    )
    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    with pytest.raises(OnboardMissionAdapterError, match="Mission replacement failed"):
        adapter.handle_event(
            _context(12.0),
            OnboardRuntimeEvent(
                "misbound-model",
                OnboardEventKind.TARGET_MOVED,
                12.0,
                obstacle_distance_m=5000.0,
            ),
        )
    assert adapter.faulted is True
    assert installed == [initial.mission]
    assert adapter.task_state.receipts == ()


def test_onboard_task_state_records_match_gps_return_and_report_sequence() -> None:
    baseline, candidates, runtimes = _executor()
    plans = (
        OnboardFlightPlan(OnboardFlightAction.MOVE_RELATIVE, north_offset_m=12.0),
        OnboardFlightPlan(OnboardFlightAction.FOLLOW_TARGET, east_offset_m=-8.0),
        OnboardFlightPlan(OnboardFlightAction.RETURN_HOME),
        OnboardFlightPlan(OnboardFlightAction.RETURN_HOME),
        OnboardFlightPlan(OnboardFlightAction.HOLD_POSITION),
    )
    actions = (
        OnboardTaskAction.SEARCH,
        OnboardTaskAction.MATCH_TARGET,
        OnboardTaskAction.RECORD_LOCATION,
        OnboardTaskAction.RETURN_HOME,
        OnboardTaskAction.REPORT,
    )
    proposals = [
        OnboardModelProposal(
            response=(
                OnboardResponse.REPLAN
                if index < 2
                else OnboardResponse.RETURN
                if index in {2, 3}
                else OnboardResponse.HOLD
            ),
            reason=f"model_{action.value}",
            confidence=0.9,
            model_id="onboard-policy",
            model_version="0.3.0-pre.8-test",
            model_sha256="a" * 64,
            provider="test-provider",
            inference_latency_ms=1.0,
            flight_plan=plan,
            task_action=action,
        )
        for index, (action, plan) in enumerate(zip(actions, plans, strict=True))
    ]
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_SequenceModelProvider(proposals),
    )
    installed = []
    safety_requests = []

    def dispatch_model(context, plan, decision, mission_id):
        compilation = compile_model_flight_plan(
            candidates[0].task,
            context,
            "multirotor",
            runtime=runtimes["task-1"],
            flight_plan=plan,
            airframe_profile_hash=PROFILE_HASH,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            compilation.compiled_mission.executable_mission,
            plan.canonical_hash,
            compilation.executable_mission_hash,
            True,
            compilation.plan.canonical_hash,
            compilation.wire_hash,
            compilation,
            decision.selected_task_id,
        )

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-task-sequence",
        mission_installer=installed.append,
        safety_dispatcher=safety_requests.append,
        flight_plan_dispatcher=dispatch_model,
    )
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent(
            "search",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            target_confidence=0.6,
            target_match_score=0.1,
            obstacle_distance_m=5000.0,
        ),
    )
    adapter.handle_event(
        _context(14.0),
        OnboardRuntimeEvent(
            "match",
            OnboardEventKind.TARGET_MOVED,
            14.0,
            target_confidence=0.95,
            target_match_score=0.98,
            obstacle_distance_m=5000.0,
        ),
    )
    final = adapter.handle_event(
        _context(16.0),
        OnboardRuntimeEvent(
            "record",
            OnboardEventKind.TARGET_MOVED,
            16.0,
            target_confidence=0.99,
            target_match_score=0.99,
            target_bearing_deg=30.0,
            target_distance_m=40.0,
            target_geometry_frame="ned",
            target_north_offset_m=34.6410161514,
            target_east_offset_m=20.0,
            obstacle_distance_m=5000.0,
        ),
    )
    returned = adapter.handle_event(
        _context(18.0),
        OnboardRuntimeEvent(
            "return-home",
            OnboardEventKind.MISSION_COMPLETE,
            18.0,
            route_progress_fraction=1.0,
            obstacle_distance_m=5000.0,
            task_phase=OnboardTaskAction.RECORD_LOCATION,
        ),
    )
    report = adapter.handle_event(
        _context(20.0),
        OnboardRuntimeEvent(
            "report",
            OnboardEventKind.MISSION_COMPLETE,
            20.0,
            route_progress_fraction=1.0,
            obstacle_distance_m=5000.0,
            task_phase=OnboardTaskAction.RETURN_HOME,
        ),
    )
    assert [receipt.phase for receipt in adapter.task_state.receipts] == [
        OnboardTaskAction.SEARCH,
        OnboardTaskAction.MATCH_TARGET,
        OnboardTaskAction.RECORD_LOCATION,
        OnboardTaskAction.RETURN_HOME,
        OnboardTaskAction.REPORT,
    ]
    assert final.task_receipt is not None
    assert final.task_receipt.target_position is not None
    assert final.decision.response is OnboardResponse.RETURN
    assert returned.task_receipt is not None
    assert returned.task_receipt.phase is OnboardTaskAction.RETURN_HOME
    assert adapter.safety_engaged is True
    assert report.task_receipt is not None
    assert report.task_receipt.phase is OnboardTaskAction.REPORT
    assert len(safety_requests) == 3


def test_onboard_adapter_rejects_model_navigation_plan_without_dispatcher() -> None:
    baseline, candidates, runtimes = _executor()
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_ModelDecisionProvider(
            OnboardResponse.REPLAN,
            flight_plan=OnboardFlightPlan(OnboardFlightAction.SEARCH, east_offset_m=5.0),
        ),
    )
    adapter = OnboardMissionAdapter(executor, mission_id_prefix="sim-model-no-dispatch")
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    with pytest.raises(OnboardMissionAdapterError, match="Mission replacement failed"):
        adapter.handle_event(
            _context(12.0),
            OnboardRuntimeEvent(
                "red-car-no-dispatch",
                OnboardEventKind.TARGET_MOVED,
                12.0,
                obstacle_distance_m=5000.0,
            ),
        )
    assert adapter.faulted is True


def test_onboard_safety_veto_precedes_task_phase_validation_and_clears_mission() -> None:
    baseline, candidates, runtimes = _executor()
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_ModelDecisionProvider(
            OnboardResponse.REPLAN,
            flight_plan=OnboardFlightPlan(OnboardFlightAction.MOVE_RELATIVE, east_offset_m=5.0),
            task_action=OnboardTaskAction.SEARCH,
        ),
    )
    installed = []
    safety = []
    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-safety-veto",
        mission_installer=installed.append,
        safety_dispatcher=safety.append,
    )
    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    revision = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent(
            "unknown-obstacle",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            obstacle_distance_m=None,
        ),
    )
    assert revision.decision.response is OnboardResponse.HOLD
    assert revision.task_receipt is None
    assert revision.safety_dispatched is True
    assert adapter.safety_engaged is True
    assert installed == [initial.mission, None]
    assert len(safety) == 1


def test_onboard_model_return_without_dispatcher_is_terminal() -> None:
    baseline, candidates, runtimes = _executor()
    executor = OnboardAIExecutor(
        baseline.prompt,
        baseline.shared_policy,
        "multirotor",
        PROFILE_HASH,
        decision_provider=_ModelDecisionProvider(
            OnboardResponse.RETURN,
            flight_plan=OnboardFlightPlan(OnboardFlightAction.RETURN_HOME),
            task_action=OnboardTaskAction.RECORD_LOCATION,
        ),
    )
    adapter = OnboardMissionAdapter(executor, mission_id_prefix="sim-return-no-ack")
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    revision = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent(
            "record-no-ack",
            OnboardEventKind.TARGET_MOVED,
            12.0,
            target_confidence=0.99,
            target_match_score=0.99,
            target_bearing_deg=10.0,
            target_distance_m=20.0,
            obstacle_distance_m=5000.0,
        ),
    )
    assert revision.decision.response is OnboardResponse.RETURN
    assert revision.task_receipt is None
    assert adapter.safety_engaged is True
    with pytest.raises(OnboardMissionAdapterError, match="safety response engaged"):
        adapter.handle_event(
            _context(14.0),
            OnboardRuntimeEvent(
                "report-after-no-ack",
                OnboardEventKind.MISSION_COMPLETE,
                14.0,
                route_progress_fraction=1.0,
                obstacle_distance_m=5000.0,
            ),
        )


def test_onboard_adapter_safety_event_clears_mission_and_dispatches() -> None:
    executor, candidates, runtimes = _executor()
    installed = []
    safety = []
    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-safety",
        mission_installer=installed.append,
        safety_dispatcher=safety.append,
    )
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)

    result = adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent("gps-degraded-1", OnboardEventKind.GPS_DEGRADED, 12.0),
    )

    assert result.decision.response is OnboardResponse.RETURN
    assert result.mission is None
    assert result.mission_replaced is False
    assert result.mission_cleared is True
    assert result.safety_dispatched is True
    assert adapter.current_mission is None
    assert installed[-1] is None
    assert safety == [result.decision]
    assert result.decision.to_mapping()["policy_applied_to_actuator"] is False


def test_onboard_adapter_records_initial_policy_abstain_without_installing() -> None:
    executor, candidates, runtimes = _executor()
    installed = []
    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-abstain",
        mission_installer=installed.append,
    )

    initial = adapter.prepare(
        replace(_context(), gps_fix_type=0),
        candidates=candidates,
        runtimes=runtimes,
    )

    assert initial.decision.response is OnboardResponse.HOLD
    assert initial.mission is None
    assert initial.mission_cleared is False
    assert adapter.prepared is True
    assert adapter.current_mission is None
    assert adapter.history == (initial,)
    assert installed == []


def test_onboard_adapter_same_event_id_is_idempotent() -> None:
    executor, candidates, runtimes = _executor()
    adapter = OnboardMissionAdapter(executor, mission_id_prefix="sim-dedup")
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    event = OnboardRuntimeEvent("target-moved-dedup", OnboardEventKind.TARGET_MOVED, 12.0)
    adapter.handle_event(_context(12.0), event)

    retry = adapter.handle_event(_context(13.0), event)
    assert retry is adapter.history[-1]
    assert [revision.sequence for revision in adapter.history] == [0, 1]


def test_onboard_adapter_dispatches_safety_before_clearing_mission() -> None:
    executor, candidates, runtimes = _executor()
    order = []

    def install(mission):
        order.append("install" if mission is not None else "clear")

    def dispatch(_decision):
        order.append("dispatch")

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-order",
        mission_installer=install,
        safety_dispatcher=dispatch,
    )
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent("gps-order", OnboardEventKind.GPS_DEGRADED, 12.0),
    )

    assert order == ["install", "dispatch", "clear"]


def test_onboard_adapter_marks_safety_dispatch_failure_unknown() -> None:
    executor, candidates, runtimes = _executor()
    installed = []

    def dispatch(_decision):
        raise RuntimeError("safe mode not acknowledged")

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-dispatch-fail",
        mission_installer=installed.append,
        safety_dispatcher=dispatch,
    )
    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)

    with pytest.raises(OnboardMissionAdapterError, match="safety Mission transaction failed"):
        adapter.handle_event(
            _context(12.0),
            OnboardRuntimeEvent("gps-dispatch-fail", OnboardEventKind.GPS_DEGRADED, 12.0),
        )

    assert adapter.current_mission is None
    assert installed == [initial.mission]
    assert [revision.sequence for revision in adapter.history] == [0]
    assert adapter.faulted is True


def test_onboard_adapter_marks_replan_install_failure_unknown() -> None:
    executor, candidates, runtimes = _executor()
    calls = []

    def install(mission):
        calls.append(mission)
        if len(calls) == 2:
            raise RuntimeError("PX4 transaction failed")

    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-replan-fail",
        mission_installer=install,
    )
    initial = adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    with pytest.raises(OnboardMissionAdapterError, match="Mission replacement failed"):
        adapter.handle_event(
            _context(12.0),
            OnboardRuntimeEvent("target-install-fail", OnboardEventKind.TARGET_MOVED, 12.0),
        )

    assert adapter.faulted is True
    assert adapter.current_mission is None
    assert adapter.history == (initial,)
    with pytest.raises(OnboardMissionAdapterError, match="faulted"):
        adapter.handle_event(
            _context(13.0),
            OnboardRuntimeEvent("target-install-fail-retry", OnboardEventKind.TARGET_MOVED, 13.0),
        )


def test_onboard_adapter_same_event_retry_is_idempotent() -> None:
    executor, candidates, runtimes = _executor()
    installed = []
    adapter = OnboardMissionAdapter(
        executor,
        mission_id_prefix="sim-idempotent",
        mission_installer=installed.append,
    )
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    event = OnboardRuntimeEvent("target-idempotent", OnboardEventKind.TARGET_MOVED, 12.0)
    first = adapter.handle_event(_context(12.0), event)
    second = adapter.handle_event(_context(13.0), event)

    assert second is first
    assert len(installed) == 2
    assert [revision.sequence for revision in adapter.history] == [0, 1]


def test_onboard_adapter_same_event_id_with_changed_payload_fails_closed() -> None:
    executor, candidates, runtimes = _executor()
    adapter = OnboardMissionAdapter(executor, mission_id_prefix="sim-collision")
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent("target-collision", OnboardEventKind.TARGET_MOVED, 12.0, detail="a"),
    )

    with pytest.raises(OnboardMissionAdapterError, match="event ID collision"):
        adapter.handle_event(
            _context(13.0),
            OnboardRuntimeEvent(
                "target-collision", OnboardEventKind.TARGET_MOVED, 13.0, detail="b"
            ),
        )
    assert adapter.faulted is True


def test_onboard_adapter_safety_response_requires_new_session() -> None:
    executor, candidates, runtimes = _executor()
    adapter = OnboardMissionAdapter(executor, mission_id_prefix="sim-terminal")
    adapter.prepare(_context(), candidates=candidates, runtimes=runtimes)
    adapter.handle_event(
        _context(12.0),
        OnboardRuntimeEvent("gps-terminal", OnboardEventKind.GPS_DEGRADED, 12.0),
    )

    assert adapter.safety_engaged is True
    with pytest.raises(OnboardMissionAdapterError, match="safety response engaged"):
        adapter.handle_event(
            _context(13.0),
            OnboardRuntimeEvent("target-after-safety", OnboardEventKind.TARGET_MOVED, 13.0),
        )


def test_onboard_event_loop_serializes_replan_and_idempotent_retry() -> None:
    executor, candidates, runtimes = _executor()
    installed = []
    loop = OnboardMissionEventLoop(
        OnboardMissionAdapter(
            executor,
            mission_id_prefix="sim-loop",
            mission_installer=installed.append,
        )
    )
    initial = loop.prepare(_context(), candidates=candidates, runtimes=runtimes)
    event = OnboardRuntimeEvent("loop-target-moved", OnboardEventKind.TARGET_MOVED, 12.0)
    replanned = loop.submit(_context(12.0), event)
    retry = loop.dispatch(_context(13.0), event)

    assert initial.mission is not None
    assert replanned.mission is not None
    assert replanned.mission_replaced is True
    assert retry is replanned
    assert loop.processed_event_count == 1
    assert [mission.mission_id for mission in installed] == [
        "sim-loop-initial",
        "sim-loop-event-1",
    ]
    evidence = loop.evidence()
    assert evidence["processed_event_count"] == 1
    assert evidence["current_mission_id"] == "sim-loop-event-1"
    assert evidence["policy_applied_to_actuator"] is False

    loop.close()
    with pytest.raises(OnboardMissionAdapterError, match="event loop is closed"):
        loop.submit(
            _context(14.0),
            OnboardRuntimeEvent("loop-after-close", OnboardEventKind.TARGET_MOVED, 14.0),
        )


def test_onboard_event_loop_old_retry_does_not_rewind_latest_sim_observer() -> None:
    executor, candidates, runtimes = _executor()
    loop = OnboardMissionEventLoop(
        OnboardMissionAdapter(executor, mission_id_prefix="sim-loop-order")
    )
    sim = SimulatorClosedLoop("multirotor", dt_s=0.1)
    try:
        sim.bind_onboard_event_loop(loop, _context(), candidates=candidates, runtimes=runtimes)
        first = OnboardRuntimeEvent("order-first", OnboardEventKind.TARGET_MOVED, 12.0)
        second = OnboardRuntimeEvent("order-second", OnboardEventKind.TARGET_MOVED, 13.0)
        sim.handle_onboard_event(_context(12.0), first)
        latest = sim.handle_onboard_event(_context(13.0), second)

        retry = sim.handle_onboard_event(_context(14.0), first)

        assert retry is not latest
        assert retry.sequence == 1
        assert loop.last_revision is latest
        assert loop.last_revision.sequence == 2
        assert sim.mission_tracker is not None
        assert sim.mission_tracker.mission.mission_id == "sim-loop-order-event-2"
    finally:
        sim.close()


def test_onboard_event_loop_old_retry_after_safety_does_not_restore_observer() -> None:
    executor, candidates, runtimes = _executor()
    loop = OnboardMissionEventLoop(
        OnboardMissionAdapter(executor, mission_id_prefix="sim-loop-safety")
    )
    sim = SimulatorClosedLoop("multirotor", dt_s=0.1)
    try:
        sim.bind_onboard_event_loop(loop, _context(), candidates=candidates, runtimes=runtimes)
        event = OnboardRuntimeEvent("safety-order", OnboardEventKind.TARGET_MOVED, 12.0)
        sim.handle_onboard_event(_context(12.0), event)
        sim.handle_onboard_event(
            _context(13.0),
            OnboardRuntimeEvent("safety-stop", OnboardEventKind.GPS_DEGRADED, 13.0),
        )
        assert sim.mission_progress is None

        retry = sim.handle_onboard_event(_context(14.0), event)

        assert retry.sequence == 1
        assert loop.last_revision is not retry
        assert loop.last_revision is not None
        assert loop.last_revision.sequence == 2
        assert sim.mission_progress is None
    finally:
        sim.close()


def test_simulator_clears_observer_when_onboard_install_fails() -> None:
    executor, candidates, runtimes = _executor()
    calls = []

    def install(mission):
        calls.append(mission)
        if len(calls) == 2:
            raise RuntimeError("PX4 transaction failed")

    loop = OnboardMissionEventLoop(
        OnboardMissionAdapter(
            executor,
            mission_id_prefix="sim-loop-fault",
            mission_installer=install,
        )
    )
    sim = SimulatorClosedLoop("multirotor", dt_s=0.1)
    try:
        sim.bind_onboard_event_loop(loop, _context(), candidates=candidates, runtimes=runtimes)
        with pytest.raises(OnboardMissionAdapterError, match="Mission replacement failed"):
            sim.handle_onboard_event(
                _context(12.0),
                OnboardRuntimeEvent("fault-event", OnboardEventKind.TARGET_MOVED, 12.0),
            )
        assert loop.adapter.faulted is True
        assert sim.mission_progress is None
    finally:
        sim.close()


def test_simulator_closed_loop_binds_shared_onboard_replan_without_starting_mission() -> None:
    executor, candidates, runtimes = _executor()
    loop = OnboardMissionEventLoop(OnboardMissionAdapter(executor, mission_id_prefix="sim-bridge"))
    sim = SimulatorClosedLoop("multirotor", dt_s=0.1)
    try:
        initial = sim.bind_onboard_event_loop(
            loop,
            _context(),
            candidates=candidates,
            runtimes=runtimes,
        )
        replanned = sim.handle_onboard_event(
            _context(12.0),
            OnboardRuntimeEvent("bridge-target-moved", OnboardEventKind.TARGET_MOVED, 12.0),
        )
        assert initial.mission is not None
        assert replanned.mission is not None
        assert replanned.mission_replaced is True
        assert replanned.decision.planner_revalidated is True
        assert replanned.decision.to_mapping()["mission_started"] is False
        assert sim.mission_progress is None
        evidence = sim.evidence()
        assert evidence["onboard_event_loop"]["current_mission_id"] == ("sim-bridge-event-1")
        assert evidence["onboard_event_loop"]["policy_applied_to_actuator"] is False
    finally:
        sim.close()
