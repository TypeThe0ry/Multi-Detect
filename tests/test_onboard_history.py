from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from test_onboard_agent import _context, _executor

from multidetect.autonomy.onboard_agent import (
    ModelBackedDecisionProvider,
    OnboardEventKind,
    OnboardModelInput,
    OnboardModelOutput,
    OnboardResponse,
    OnboardRuntimeEvent,
)
from multidetect.autonomy.onboard_history import (
    OnboardHistoryBuffer,
    OnboardHistoryError,
    OnboardInferenceScope,
)
from multidetect.autonomy.serialization import canonical_sha256


def _input(at_s: float = 10.0, event_id: str | None = None) -> OnboardModelInput:
    return OnboardModelInput(
        _context(at_s),
        OnboardRuntimeEvent(
            event_id=event_id or f"event-{at_s}",
            kind=OnboardEventKind.TARGET_LOST,
            observed_at_s=at_s,
            target_class="search_target",
            obstacle_distance_m=5_000.0,
        ),
    )


def _scope() -> OnboardInferenceScope:
    context = _context()
    return OnboardInferenceScope(
        session_id="11111111-1111-4111-8111-111111111111",
        prompt_sha256="a" * 64,
        aircraft_id="aircraft-1",
        selected_task_id="task-1",
        airframe_profile_id=context.airframe_profile_id,
        airframe_profile_sha256=context.airframe_profile_hash,
        vehicle_class="multirotor",
        home_sha256=context.home_position.canonical_hash,
        clock_epoch="declared-test-epoch-not-sensor-clock",
    )


def test_history_right_aligns_current_with_explicit_padding_dt_and_origin() -> None:
    history = OnboardHistoryBuffer()
    first = history.prepare(_input(), _scope())
    assert history.entries == ()  # prepare is not a successful inference
    assert first.valid_mask == (0.0,) * 7 + (1.0,)
    assert first.features[:-1] == ((0.0,) * 40,) * 7
    assert first.features[-1] == _input().feature_vector()
    assert first.dt_s == (0.0,) * 8
    assert first.reset_mask == (0.0,) * 7 + (1.0,)
    history.commit(first)
    second = history.prepare(_input(10.25), _scope())
    assert second.dt_s == (0.0,) * 7 + (0.25,)
    assert second.reset_mask == (0.0,) * 6 + (1.0, 0.0)
    assert second.to_mapping()["truth_free"] is True
    assert second.to_mapping()["training_ready"] is False
    assert second.canonical_hash != first.canonical_hash


def test_history_is_bounded_and_never_renames_search_target() -> None:
    history = OnboardHistoryBuffer()
    for index in range(20):
        model_input = _input(10.0 + index / 10)
        window = history.prepare(model_input, _scope())
        history.commit(window)
        assert len(history.entries) <= 8
        assert model_input.event.target_class == "search_target"
    assert history.entries[0].event_id == "event-11.2"
    assert window.valid_mask == (1.0,) * 8
    assert window.reset_mask == (0.0,) * 8
    assert window.dt_s[0] == pytest.approx(0.1)
    assert window.features[-1] == _input(11.9).feature_vector()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "22222222-2222-4222-8222-222222222222"),
        ("prompt_sha256", "c" * 64),
        ("aircraft_id", "aircraft-2"),
        ("selected_task_id", "task-2"),
        ("airframe_profile_id", "different-profile"),
        ("airframe_profile_sha256", "d" * 64),
        ("vehicle_class", "vtol"),
        ("home_sha256", "e" * 64),
        ("clock_epoch", "new-clock-epoch"),
    ],
)
def test_every_scope_boundary_requires_explicit_reset(field: str, value: str) -> None:
    history = OnboardHistoryBuffer()
    history.commit(history.prepare(_input(), _scope()))
    before = history.entries
    with pytest.raises(OnboardHistoryError, match="scope changed"):
        history.prepare(_input(10.1), replace(_scope(), **{field: value}))
    assert history.entries == before


@pytest.mark.parametrize("time", [9.0, 10.0, 15.01])
def test_regressed_repeated_or_expired_time_does_not_consume_input(time: float) -> None:
    history = OnboardHistoryBuffer()
    history.commit(history.prepare(_input(), _scope()))
    with pytest.raises(OnboardHistoryError, match="timestamp|expired"):
        history.prepare(_input(time, "new-event"), _scope())
    assert len(history.entries) == 1


def test_duplicate_input_is_idempotent_but_conflicting_or_stale_retry_is_rejected() -> None:
    history = OnboardHistoryBuffer()
    first = _input()
    history.commit(history.prepare(first, _scope()))
    duplicate = history.prepare(first, _scope())
    history.commit(duplicate)
    assert len(history.entries) == 1
    conflicting = replace(first, event=replace(first.event, target_confidence=0.8))
    with pytest.raises(OnboardHistoryError, match="conflicting"):
        history.prepare(conflicting, _scope())
    history.commit(history.prepare(_input(10.2), _scope()))
    with pytest.raises(OnboardHistoryError, match="stale event"):
        history.prepare(first, _scope())


def test_reset_records_scope_reason_and_discards_before_rebinding() -> None:
    history = OnboardHistoryBuffer()
    history.commit(history.prepare(_input(), _scope()))
    new_scope = replace(_scope(), selected_task_id="task-2")
    history.reset(reason="explicit_task_transition", scope=new_scope)
    assert history.entries == ()
    receipt = history.last_reset_receipt
    assert receipt["discarded_entries"] == 1
    assert receipt["previous_scope_sha256"] == _scope().canonical_hash
    assert receipt["next_scope_sha256"] == new_scope.canonical_hash
    receipt["reason"] = "mutated external copy"
    assert history.last_reset_receipt["reason"] == "explicit_task_transition"
    window = history.prepare(_input(20), new_scope)
    assert window.dt_s[-1] == 0
    assert window.reset_mask[-1] == 1


def test_stale_prepared_window_cannot_commit_after_reset() -> None:
    history = OnboardHistoryBuffer()
    pending = history.prepare(_input(), _scope())
    history.reset(reason="runtime_recovery")
    with pytest.raises(OnboardHistoryError, match="stale history"):
        history.commit(pending)


def test_history_transaction_is_buffer_bound_and_gap_limit_cannot_mutate() -> None:
    first = OnboardHistoryBuffer()
    second = OnboardHistoryBuffer()
    pending = first.prepare(_input(), _scope())
    with pytest.raises(OnboardHistoryError, match="stale history"):
        second.commit(pending)
    first.commit(pending)
    with pytest.raises(AttributeError):
        first.max_gap_s = float("nan")


def test_history_rejects_truth_dictionary_and_runtime_annotation_channels() -> None:
    history = OnboardHistoryBuffer()
    with pytest.raises(TypeError, match="OnboardModelInput"):
        history.prepare({"truth_target_position": [1, 2, 3]}, _scope())
    with pytest.raises(TypeError, match="annotation"):
        OnboardRuntimeEvent(
            event_id="truth",
            kind=OnboardEventKind.TARGET_MOVED,
            observed_at_s=10,
            annotation_mask_path="offline-mask.png",
        )
    assert history.entries == ()


def test_scope_context_and_nonfinite_features_are_rejected(monkeypatch) -> None:
    history = OnboardHistoryBuffer()
    wrong_context = replace(_input().context, airframe_profile_id="different")
    with pytest.raises(OnboardHistoryError, match="context"):
        history.prepare(replace(_input(), context=wrong_context), _scope())
    monkeypatch.setattr(OnboardModelInput, "feature_vector", lambda self: (float("nan"),) * 40)
    with pytest.raises(ValueError, match="finite"):
        history.prepare(_input(), _scope())
    assert history.entries == ()


def test_history_immutable_entries_and_window_reject_list_aliases() -> None:
    window = OnboardHistoryBuffer().prepare(_input(), _scope())
    with pytest.raises(OnboardHistoryError, match="immutable"):
        replace(window.entries[0], features=list(window.entries[0].features))
    with pytest.raises(OnboardHistoryError, match="immutable"):
        replace(window, entries=list(window.entries))


def test_scope_rejects_missing_uuid_or_noncanonical_vehicle() -> None:
    with pytest.raises(OnboardHistoryError, match="UUID"):
        replace(_scope(), session_id="not-a-session-uuid")
    with pytest.raises(OnboardHistoryError, match="canonical"):
        replace(_scope(), vehicle_class="copter")


class _ScopedRunner:
    def __init__(self) -> None:
        self.history = OnboardHistoryBuffer()
        self.inferences = 0

    def infer(self, model_input):
        raise AssertionError("unscoped history inference forbidden")

    def infer_scoped(self, model_input, scope):
        window = self.history.prepare(model_input, scope)
        self.inferences += 1
        self.history.commit(window)
        return OnboardModelOutput(OnboardResponse.HOLD, "history_test_hold", 0.9)

    def observe_scoped(self, model_input, scope):
        window = self.history.prepare(model_input, scope)
        changed = window.entries != self.history.entries
        self.history.commit(window)
        return changed


def _provider(runner) -> ModelBackedDecisionProvider:
    return ModelBackedDecisionProvider(
        runner,
        model_id="development-history",
        model_version="v1-dev",
        model_sha256="f" * 64,
        provider="test-no-transport",
    )


def test_provider_and_executor_use_scoped_path_without_changing_output_or_safety() -> None:
    executor, candidates, runtimes = _executor()
    runner = _ScopedRunner()
    executor.decision_provider = _provider(runner)
    executor.prepare(_context(), candidates, runtimes)
    assert executor.observe_model_input(_context(10.1), _input(10.1).event)
    assert runner.inferences == 0
    decision = executor.step(_context(10.2), _input(10.2).event)
    assert decision.response is OnboardResponse.HOLD
    assert runner.inferences == 1
    assert len(runner.history.entries) == 2
    scope = runner.history.scope
    assert scope.session_id == executor.session_id
    assert scope.prompt_sha256 == executor.prompt.canonical_hash
    assert scope.aircraft_id == executor.prompt.aircraft_id
    assert scope.clock_epoch == executor.session_id
    assert scope.selected_task_id == "task-1"
    assert decision.selected_task_id is None  # HOLD never binds a movement Mission
    assert scope.home_sha256 == _context().home_position.canonical_hash


def test_reusing_provider_in_another_executor_is_fail_closed_not_cross_prompt_memory() -> None:
    runner = _ScopedRunner()
    provider = _provider(runner)
    for index in range(2):
        executor, candidates, runtimes = _executor()
        executor.decision_provider = provider
        executor.prepare(_context(), candidates, runtimes)
        decision = executor.step(_context(10.1), _input(10.1).event)
        assert decision.response is OnboardResponse.HOLD
        if index == 1:
            assert decision.reason == "model_provider_fault"
    assert runner.inferences == 1
    assert len(runner.history.entries) == 1


def test_legacy_runner_propose_for_session_falls_back_and_observe_is_noop() -> None:
    class LegacyRunner:
        def infer(self, model_input):
            return {"response": "hold", "reason": "legacy", "confidence": 0.9}

    provider = _provider(LegacyRunner())
    proposal = provider.propose_for_session(_context(), _input().event, _scope())
    assert proposal.reason == "legacy"
    assert provider.observe_for_session(_context(), _input().event, _scope()) is False


def test_executor_rejects_home_or_clock_regression_before_observation() -> None:
    executor, candidates, runtimes = _executor()
    runner = _ScopedRunner()
    executor.decision_provider = _provider(runner)
    executor.prepare(_context(), candidates, runtimes)
    executor.observe_model_input(_context(10.1), _input(10.1).event)
    with pytest.raises(ValueError, match="regressed"):
        executor.observe_model_input(_context(9), _input(9).event)
    context = replace(
        _context(10.2), home_position=replace(_context().home_position, latitude_deg=2.0)
    )
    with pytest.raises(ValueError, match="home position changed"):
        executor.observe_model_input(context, _input(10.2).event)
    assert len(runner.history.entries) == 1


def test_suppressed_search_samples_history_without_another_mission_or_inference() -> None:
    from test_onboard_realtime_bridge import _bridge
    from test_onboard_realtime_bridge import _context as bridge_context

    from multidetect.autonomy.realtime_bridge import (
        OnboardAutonomousSortie,
        OnboardPerceptionObservation,
    )

    bridge, legacy, _, _ = _bridge()

    class CollectingRunner(_ScopedRunner):
        def infer_scoped(self, model_input, scope):
            window = self.history.prepare(model_input, scope)
            output = legacy.infer(model_input)
            self.history.commit(window)
            self.inferences += 1
            return output

    runner = CollectingRunner()
    bridge.event_loop.adapter.executor.decision_provider = _provider(runner)
    sortie = OnboardAutonomousSortie(bridge)
    sortie.prepare(bridge_context(10.0))
    sortie.ingest_telemetry(bridge_context(11.0, altitude_m=30.0))
    observation = OnboardPerceptionObservation(
        observation_id="first-loss",
        observed_at_s=12.0,
        source="offline-rgb-test",
        target_visible=False,
        target_class_match=False,
        obstacle_distance_m=5_000.0,
    )
    first = sortie.ingest_perception(bridge_context(12.0), observation)
    second = sortie.ingest_perception(
        bridge_context(12.1), replace(observation, observation_id="second-loss", observed_at_s=12.1)
    )
    assert second is first
    assert len(bridge.revisions) == 2  # prepare + one model-authored Mission
    assert runner.inferences == 1
    assert len(runner.history.entries) == 2
    assert runner.history.entries[-1].input_sha256 == canonical_sha256(
        OnboardModelInput(
            bridge_context(12.1),
            replace(
                observation, observation_id="second-loss", observed_at_s=12.1
            ).to_runtime_event(),
        ).to_mapping()
    )
    bridge.event_loop.close()
    with pytest.raises(Exception, match="closed"):
        sortie.ingest_perception(
            bridge_context(12.2),
            replace(observation, observation_id="third-loss", observed_at_s=12.2),
        )


def test_executor_session_ids_are_unique_without_claiming_a_sensor_clock() -> None:
    first, _, _ = _executor()
    second, _, _ = _executor()
    assert first.session_id != second.session_id
    assert len(first.session_id) == len(str(uuid4()))


def _task_switch_runtime():
    import numpy as np
    from test_onboard_agent import _task
    from test_onboard_history_onnx import _Session

    from multidetect.autonomy.onboard_adapter import (
        OnboardFlightPlanDispatch,
        OnboardMissionAdapter,
        OnboardMissionEventLoop,
    )
    from multidetect.autonomy.onboard_history_onnx import OnboardHistoryOnnxModelRunner
    from multidetect.autonomy.onboard_navigation import compile_model_flight_plan
    from multidetect.autonomy.task_selection import TaskCandidate

    executor, initial_candidates, initial_runtimes = _executor()
    task1 = initial_candidates[0].task
    task2 = _task("task-2")
    tasks = {task1.task_id: task1, task2.task_id: task2}
    runtimes = {
        task1.task_id: initial_runtimes[task1.task_id],
        task2.task_id: initial_runtimes[task1.task_id],
    }
    session = _Session()
    raw = np.zeros((1, 20), dtype=np.float32)
    raw[0, 1] = 12.0  # SEARCH fixture
    raw[0, 12] = 12.0
    raw[0, 16] = 0.03
    session.result = [raw]
    runner = OnboardHistoryOnnxModelRunner(session)
    executor.decision_provider = _provider(runner)
    installed = []

    def dispatch(context, plan, decision, mission_id):
        compiled = compile_model_flight_plan(
            tasks[decision.selected_task_id],
            context,
            executor.vehicle_class,
            runtime=runtimes[decision.selected_task_id],
            flight_plan=plan,
            airframe_profile_hash=executor.airframe_profile_hash,
            mission_id=mission_id,
        )
        return OnboardFlightPlanDispatch(
            mission=compiled.compiled_mission.executable_mission,
            flight_plan_sha256=plan.canonical_hash,
            mission_sha256=compiled.executable_mission_hash,
            planner_revalidated=True,
            plan_sha256=compiled.plan.canonical_hash,
            wire_hash=compiled.wire_hash,
            compilation=compiled,
            selected_task_id=decision.selected_task_id,
        )

    adapter = OnboardMissionAdapter(
        executor,
        mission_installer=installed.append,
        flight_plan_dispatcher=dispatch,
        safety_dispatcher=lambda decision: None,
    )
    loop = OnboardMissionEventLoop(adapter)
    loop.prepare(_context(), candidates=initial_candidates, runtimes=initial_runtimes)
    takeoff_context = replace(
        _context(10.1),
        aircraft_position=replace(
            _context().aircraft_position, altitude_m=30.0, altitude_reference="relative_home"
        ),
    )
    loop.mark_takeoff_complete(takeoff_context)
    task2_candidates = [TaskCandidate(task2)]
    return (
        loop,
        runner,
        session,
        installed,
        task2_candidates,
        {task2.task_id: runtimes[task2.task_id]},
    )


def test_successful_task_switch_resets_after_transport_and_receipt_and_next_frame_works() -> None:
    loop, runner, session, installed, candidates, runtimes = _task_switch_runtime()
    first = loop.submit(_context(10.2), _input(10.2).event)
    assert first.decision.selected_task_id == "task-1"
    old_scope = runner.history.scope
    calls = []

    # Instance methods are slotted; capture ordering through the public provider seam.
    class ResetRecordingProvider(ModelBackedDecisionProvider):
        def reset_for_session(self, scope, *, reason):
            calls.append((len(installed), len(loop.adapter.task_state.receipts), scope, reason))
            return super().reset_for_session(scope, reason=reason)

    loop.adapter.executor.decision_provider = ResetRecordingProvider(
        runner,
        model_id="development-history",
        model_version="v1-dev",
        model_sha256="f" * 64,
        provider="test-no-transport",
    )
    second = loop.submit(
        _context(10.3), _input(10.3).event, candidates=candidates, runtimes=runtimes
    )
    assert second.decision.selected_task_id == "task-2"
    assert len(calls) == 1
    assert calls[0][0] == len(installed) == 3
    assert calls[0][1] == len(loop.adapter.task_state.receipts) == 2
    assert calls[0][3] == "committed_task_transition"
    scope = runner.history.scope
    assert scope == replace(old_scope, selected_task_id="task-2")
    assert runner.history.entries == ()
    receipt = runner.history.last_reset_receipt
    assert receipt["previous_scope_sha256"] == old_scope.canonical_hash
    assert receipt["next_scope_sha256"] == scope.canonical_hash
    assert receipt["reason"] == "committed_task_transition"
    assert receipt["discarded_entries"] == 2
    third = loop.submit(
        _context(10.4), _input(10.4).event, candidates=candidates, runtimes=runtimes
    )
    assert third.decision.reason != "model_provider_fault"
    assert third.decision.selected_task_id == "task-2"
    assert len(calls) == 1  # same-task replan keeps its real history
    assert runner.history.entries[-1].reset
    assert session.calls[-1][1]["reset_mask"][0, -1] == 1.0


@pytest.mark.parametrize("failure", ["compiler", "installer", "receipt"])
def test_failed_task_switch_does_not_reset_history(failure: str, monkeypatch) -> None:
    from multidetect.autonomy.onboard_adapter import OnboardMissionAdapterError
    from multidetect.autonomy.onboard_task_state import (
        OnboardTaskStateError,
        OnboardTaskStateMachine,
    )

    loop, runner, _, _, candidates, runtimes = _task_switch_runtime()
    loop.submit(_context(10.2), _input(10.2).event)
    old_scope = runner.history.scope
    if failure == "compiler":
        loop.adapter.flight_plan_dispatcher = lambda *args: (_ for _ in ()).throw(
            RuntimeError("compile fixture")
        )
    elif failure == "installer":
        loop.adapter.mission_installer = lambda mission: (_ for _ in ()).throw(
            RuntimeError("install fixture")
        )
    else:

        def fail_commit(*args):
            raise OnboardTaskStateError("receipt fixture")

        monkeypatch.setattr(OnboardTaskStateMachine, "commit", fail_commit)
    with pytest.raises(OnboardMissionAdapterError):
        loop.submit(_context(10.3), _input(10.3).event, candidates=candidates, runtimes=runtimes)
    assert loop.adapter.faulted
    assert runner.history.scope == old_scope
    assert runner.history.last_reset_receipt is None
    assert len(loop.adapter.history) == 2  # prepare + first task, no new successful revision


@pytest.mark.parametrize("reset_failure", ["raise", "false"])
def test_history_reset_failure_after_committed_transport_latches_recovery_without_retry(
    reset_failure: str,
) -> None:
    from multidetect.autonomy.onboard_adapter import OnboardMissionAdapterError

    loop, runner, _, installed, candidates, runtimes = _task_switch_runtime()
    loop.submit(_context(10.2), _input(10.2).event)

    class FailingResetProvider(ModelBackedDecisionProvider):
        def reset_for_session(self, scope, *, reason):
            if reset_failure == "raise":
                raise RuntimeError("reset failure fixture")
            return False

    loop.adapter.executor.decision_provider = FailingResetProvider(
        runner,
        model_id="development-history",
        model_version="v1-dev",
        model_sha256="f" * 64,
        provider="test-no-transport",
    )
    with pytest.raises(OnboardMissionAdapterError, match="committed.*reset failed"):
        loop.submit(_context(10.3), _input(10.3).event, candidates=candidates, runtimes=runtimes)
    assert len(installed) == 3  # transaction happened; never claim it was rolled back
    assert len(loop.adapter.history) == 2
    assert loop.adapter.faulted
    with pytest.raises(OnboardMissionAdapterError, match="faulted"):
        loop.submit(_context(10.4), _input(10.4).event, candidates=candidates, runtimes=runtimes)
    assert len(installed) == 3  # no second install after failed reset


def test_standalone_executor_task_selection_does_not_fake_a_transaction_reset() -> None:
    loop, runner, _, installed, candidates, runtimes = _task_switch_runtime()
    loop.submit(_context(10.2), _input(10.2).event)
    old_scope = runner.history.scope
    decision = loop.adapter.executor.step(_context(10.3), _input(10.3).event, candidates, runtimes)
    assert decision.selected_task_id == "task-2"
    assert runner.history.scope == old_scope
    assert runner.history.last_reset_receipt is None
    assert len(installed) == 2
