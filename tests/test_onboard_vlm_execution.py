"""Software fixtures only: no VLM, real range sensor, PX4 or flight evidence."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_onboard_realtime_bridge import _bridge, _context
from test_onboard_vlm_admission import POLICY, _clearance, _current
from test_onboard_vlm_provider import _Clock, _FakeWorker, _source

from multidetect.autonomy.onboard_agent import (
    ModelBackedDecisionProvider,
    OnboardEventKind,
    OnboardRuntimeEvent,
)
from multidetect.autonomy.onboard_vlm_admission import (
    ExecutionAdmissionPolicy,
    OnboardVlmAdmissionError,
)
from multidetect.autonomy.onboard_vlm_execution import (
    OnboardVlmExecutionDeferred,
    OnboardVlmExecutionError,
    OnboardVlmExecutionProvider,
    OnboardVlmExecutionScheduler,
    capture_execution_snapshot,
)
from multidetect.autonomy.policy_contract import PolicyArtifact, SharedTaskPolicy
from multidetect.autonomy.prompt_resolution import PromptTaskResolution
from multidetect.autonomy.serialization import canonical_json
from multidetect.autonomy.task_ranker import TaskRanker


class _OutsideCoreWorker(_FakeWorker):
    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.operations = []

    def _outside(self, operation):
        assert not self.loop._lock._is_owned(), f"{operation} held Mission loop lock"
        self.operations.append(operation)

    def submit(self, request, *, observed_now_s):
        self._outside("submit")
        return super().submit(request, observed_now_s=observed_now_s)

    def poll(self):
        self._outside("poll")
        return super().poll()

    def take_result(self):
        self._outside("take_result")
        return super().take_result()

    def cancel(self, reason):
        self._outside("cancel")
        return super().cancel(reason)


def _fixture():
    bridge, runner, candidates, runtimes = _bridge()
    bridge.prepare(_context(1.0))
    context = _context(2.0, altitude_m=35.0)
    assert bridge.mark_takeoff_complete(context)
    loop = bridge.event_loop
    worker, clock = _OutsideCoreWorker(loop), _Clock(2.0)
    provider = OnboardVlmExecutionProvider(
        worker,
        model_id="fixture-vlm",
        model_version="fixture-v2",
        model_sha256="a" * 64,
        clock=clock,
    )
    loop.adapter.executor.decision_provider = provider
    scheduler = OnboardVlmExecutionScheduler(loop, provider, policy=POLICY)
    event = OnboardRuntimeEvent(
        "fixture-source",
        OnboardEventKind.TARGET_LOST,
        2.0,
        detail="software fixture; not actual range/visual/action evidence",
        obstacle_distance_m=100.0,
    )
    source = capture_execution_snapshot(loop, context, event)
    request, _, _, _ = _source(
        2.0,
        context=context,
        event=event,
        scope=source.scope,
        prompt=loop.adapter.executor.prompt,
    )
    scheduler.update_current(source)
    return bridge, runner, candidates, runtimes, worker, clock, provider, scheduler, source, request


def _finish(worker, clock, request, *, north=20.0, east=0.0):
    clock.at_s = 2.05
    worker.finish(
        request,
        clock,
        {
            "response": "replan",
            "reason": "software fixture; no VLM/flight claim",
            "confidence": 0.95,
            "task_action": "search",
            "flight_plan": {
                "action": "search",
                "north_offset_m": north,
                "east_offset_m": east,
                "altitude_m": 35.0,
                "speed_mps": None,
                "duration_s": None,
                "reference_id": None,
            },
        },
    )


def _ready():
    fixture = _fixture()
    _, _, _, _, worker, clock, provider, scheduler, source, request = fixture
    assert scheduler.submit_source(request, source) == 1
    _finish(worker, clock, request)
    anchor = provider.poll_source()
    current = _current(anchor)
    scheduler.update_current(current)
    clock.at_s = 2.2
    return fixture, anchor, current


def test_pending_never_calls_adapter_or_fake_hold():
    bridge, _, _, _, worker, _, provider, scheduler, source, request = _fixture()
    before = (
        bridge.event_loop.adapter.current_mission,
        bridge.event_loop.adapter.history,
        bridge.event_loop.adapter.task_state.receipts,
    )
    scheduler.submit_source(request, source)
    assert scheduler.submit_if_ready(mission_id="fixture-pending") is None
    assert before == (
        bridge.event_loop.adapter.current_mission,
        bridge.event_loop.adapter.history,
        bridge.event_loop.adapter.task_state.receipts,
    )
    assert bridge.event_loop.processed_event_count == 0
    assert worker.take_calls == 0
    assert provider.evidence()["pending"]


def test_two_snapshots_causally_dispatch_exact_old_goal_at_new_position_once():
    fixture, anchor, current = _ready()
    bridge, runner, _, _, worker, _, provider, scheduler, source, request = fixture
    original = (
        source.canonical_hash,
        request.canonical_hash,
        anchor.result.raw_output,
        anchor.result.response.to_mapping(),
    )
    before_receipts = len(bridge.event_loop.adapter.task_state.receipts)
    revision = scheduler.submit_if_ready(mission_id="fixture-rebased")
    assert revision.flight_plan_dispatched and revision.mission_replaced
    assert revision.event is current.event
    assert revision.event.observed_at_s == 2.1
    assert runner.inputs == []
    admission = scheduler.last_execution_admission
    assert admission.anchor is anchor and admission.current is current
    assert admission.effective_plan.north_offset_m == pytest.approx(17, abs=0.001)
    assert admission.effective_plan.east_offset_m == pytest.approx(-4, abs=0.001)
    assert revision.mission.canonical_hash == admission.compilation.executable_mission_hash
    assert admission.compilation.plan.context_snapshot_hash == current.context.canonical_hash
    assert original == (
        source.canonical_hash,
        request.canonical_hash,
        anchor.result.raw_output,
        anchor.result.response.to_mapping(),
    )
    assert len(bridge.event_loop.adapter.task_state.receipts) == before_receipts + 1
    assert bridge.event_loop.processed_event_count == 1
    assert worker.operations == ["submit", "poll", "take_result"]
    assert scheduler.submit_if_ready(mission_id="fixture-repeat") is None
    with pytest.raises(OnboardVlmExecutionError, match="retired/consumed"):
        provider.submit_source(request, source, anchor.task, anchor.runtime_json)
    audit = provider.evidence()["audit"]
    consumed = next(row for row in audit if row["status"] == "consumed")
    assert consumed["source_snapshot_sha256"] == source.canonical_hash
    assert consumed["execution_snapshot_sha256"] == current.canonical_hash


def test_unarmed_or_different_execution_context_cannot_propose():
    fixture, anchor, current = _ready()
    _, _, _, _, worker, _, provider, _, _, _ = fixture
    with pytest.raises(OnboardVlmExecutionDeferred, match="no armed"):
        provider.propose_for_session(current.context, current.event, current.scope)
    admission = provider.prepare_admission(current, now_s=2.2, mission_id="fixture", policy=POLICY)
    provider.arm(admission)
    with pytest.raises(OnboardVlmExecutionError, match="no armed"):
        provider.propose_for_session(
            anchor.source.context, anchor.source.event, anchor.source.scope
        )
    before = (worker.poll_calls, worker.take_calls)
    assert (
        provider.propose_for_session(current.context, current.event, current.scope).flight_plan
        == admission.effective_plan
    )
    assert before == (worker.poll_calls, worker.take_calls)
    with pytest.raises(OnboardVlmExecutionDeferred, match="no armed"):
        provider.propose_for_session(current.context, current.event, current.scope)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": OnboardEventKind.OBSTACLE_RISK},
        {"kind": OnboardEventKind.GPS_DEGRADED},
        {"kind": OnboardEventKind.BATTERY_LOW},
        {"kind": OnboardEventKind.INFERENCE_FAULT},
        {"kind": OnboardEventKind.MISSION_COMPLETE},
        {"obstacle_distance_m": None},
        {"obstacle_distance_m": 5000.0},
        {"obstacle_distance_m": 4.9},
    ],
)
def test_current_unsafe_event_rejects_without_consumption_or_adapter(changes):
    fixture, anchor, _ = _ready()
    bridge, _, _, _, _, _, provider, scheduler, _, _ = fixture
    current = _current(anchor, event_changes=changes)
    scheduler.update_current(current)
    with pytest.raises(OnboardVlmAdmissionError):
        scheduler.submit_if_ready(mission_id="fixture-unsafe")
    assert bridge.event_loop.processed_event_count == 0
    assert provider.evidence()["source_ready"]


def test_unknown_clearance_and_default_untrusted_source_reject():
    fixture, _, current = _ready()
    bridge, _, _, _, _, _, _, scheduler, _, _ = fixture
    scheduler.update_current(replace(current, clearance=None))
    with pytest.raises(OnboardVlmAdmissionError, match="clearance"):
        scheduler.submit_if_ready(mission_id="fixture-no-clearance")
    scheduler.update_current(current)
    scheduler.policy = ExecutionAdmissionPolicy()
    with pytest.raises(OnboardVlmAdmissionError, match="clearance"):
        scheduler.submit_if_ready(mission_id="fixture-untrusted")
    assert bridge.event_loop.processed_event_count == 0


def test_new_frame_queues_latest_without_cancelling_pending_worker():
    _, _, candidates, runtimes, worker, clock, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    newer_context = replace(source.context, captured_at_s=2.1, snapshot_id="fixture-new")
    newer_event = replace(source.event, observed_at_s=2.1, event_id="fixture-new-event")
    newer = replace(source, context=newer_context, event=newer_event)
    newer_request, _, _, _ = _source(
        2.1,
        context=newer_context,
        event=newer_event,
        scope=source.scope,
        prompt=request.model_input.prompt,
    )
    clock.at_s = 2.1
    scheduler.update_current(newer)
    assert (
        provider.submit_source(
            newer_request,
            newer,
            candidates[0].task,
            canonical_json(runtimes[candidates[0].task.task_id]),
        )
        is None
    )
    assert worker.cancellations == [] and len(worker.submissions) == 1
    assert provider.evidence()["queued_latest"]
    assert (
        provider.submit_source(
            newer_request,
            newer,
            candidates[0].task,
            canonical_json(runtimes[candidates[0].task.task_id]),
        )
        is None
    )  # queued retry is idempotent, not tombstoned as superseded.
    assert scheduler.submit_if_ready(mission_id="fixture-pending-new") is None


@pytest.mark.parametrize(
    "field,value", [("mission_sha256", "f" * 64), ("mission_revision", 9), ("receipt_revision", 9)]
)
def test_identity_change_retires_pending_and_late_result_never_revives(field, value):
    _, _, _, _, worker, clock, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    provider.observe_snapshot(replace(source, **{field: value}))
    assert worker.cancellations == ["scope/Mission/receipt changed"]
    _finish(worker, clock, request)
    assert provider.poll_source() is None
    assert not provider.evidence()["source_ready"]
    with pytest.raises(OnboardVlmExecutionError, match="retired"):
        scheduler.submit_source(request, source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 8),
        ("request_sha256", "f" * 64),
        ("raw_output", '{"schema_version":2,"confidence":1e309}'),
    ],
)
def test_changed_worker_identity_or_raw_rejects_and_tombstones(field, value):
    _, _, _, _, worker, clock, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    _finish(worker, clock, request)
    worker.result = replace(worker.result, **{field: value})
    with pytest.raises((ValueError, TypeError)):
        provider.poll_source()
    assert worker.cancellations
    assert not provider.evidence()["source_ready"]


def test_take_result_change_rejects_without_core_lock(monkeypatch):
    _, _, _, _, worker, clock, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    _finish(worker, clock, request)
    old_take = worker.take_result

    def changed_take():
        return replace(old_take(), generation=99)

    monkeypatch.setattr(worker, "take_result", changed_take)
    with pytest.raises(OnboardVlmExecutionError, match="changed between"):
        provider.poll_source()


def test_latest_head_cas_does_not_submit_old_observation(monkeypatch):
    fixture, anchor, _ = _ready()
    bridge, _, _, _, worker, clock, provider, scheduler, _, _ = fixture
    newer = _current(anchor, north=4, east=4, at_s=2.15)
    old_prepare = provider.prepare_admission

    def advance_head(*args, **kwargs):
        assert not bridge.event_loop._lock._is_owned()
        result = old_prepare(*args, **kwargs)
        scheduler.update_current(newer)
        return result

    monkeypatch.setattr(provider, "prepare_admission", advance_head)
    assert scheduler.submit_if_ready(mission_id="fixture-cas-old") is None
    assert bridge.event_loop.processed_event_count == 0
    assert worker.cancellations == [] and provider.evidence()["source_ready"]
    monkeypatch.setattr(provider, "prepare_admission", old_prepare)
    clock.at_s = 2.25
    assert scheduler.submit_if_ready(mission_id="fixture-cas-new").event is newer.event


def test_deadline_rechecked_after_unlocked_compilation(monkeypatch):
    fixture, _, _ = _ready()
    bridge, _, _, _, _, clock, provider, scheduler, _, _ = fixture
    old_prepare = provider.prepare_admission

    def expired(*args, **kwargs):
        result = old_prepare(*args, **kwargs)
        clock.at_s = 3.1
        return result

    monkeypatch.setattr(provider, "prepare_admission", expired)
    with pytest.raises(ValueError):
        scheduler.submit_if_ready(mission_id="fixture-expired")
    assert bridge.event_loop.processed_event_count == 0
    assert not provider.evidence()["source_ready"]


@pytest.mark.parametrize("mutation", ["runtime", "task", "policy"])
def test_runtime_task_policy_cas_changes_reject(monkeypatch, mutation):
    fixture, _, _ = _ready()
    bridge, _, _, _, _, _, provider, scheduler, _, _ = fixture
    old_prepare = provider.prepare_admission

    def changed(*args, **kwargs):
        result = old_prepare(*args, **kwargs)
        adapter = bridge.event_loop.adapter
        if mutation == "runtime":
            task_id = adapter._candidates[0].task.task_id
            adapter._runtimes[task_id]["takeoff_altitude_m"] = 36.0
        elif mutation == "task":
            adapter._candidates = ()
        else:
            scheduler.policy = ExecutionAdmissionPolicy(
                max_current_age_s=0.5, trusted_clearance_source_ids=("fixture-segment-clearance",)
            )
        return result

    monkeypatch.setattr(provider, "prepare_admission", changed)
    with pytest.raises(OnboardVlmExecutionError):
        scheduler.submit_if_ready(mission_id="fixture-cas-config")
    assert bridge.event_loop.processed_event_count == 0


def test_external_adapter_revision_not_hidden_by_stale_loop_head(monkeypatch):
    fixture, _, _ = _ready()
    bridge, _, _, _, _, _, provider, scheduler, source, _ = fixture
    old_prepare = provider.prepare_admission

    def changed_adapter(*args, **kwargs):
        result = old_prepare(*args, **kwargs)
        # Simulate an ingress owner bypassing loop bookkeeping: actual history
        # and receipt count change while last_revision remains the old revision.
        adapter = bridge.event_loop.adapter
        adapter._history.append(replace(adapter.history[-1], sequence=source.mission_revision + 1))
        return result

    monkeypatch.setattr(provider, "prepare_admission", changed_adapter)
    with pytest.raises(OnboardVlmExecutionError, match="actual adapter"):
        scheduler.submit_if_ready(mission_id="fixture-external")
    assert bridge.event_loop.last_revision.sequence == source.mission_revision
    actual = capture_execution_snapshot(bridge.event_loop, source.context, source.event)
    assert actual.mission_revision == source.mission_revision + 1
    assert bridge.event_loop.processed_event_count == 0


def test_transport_failure_consumed_source_cannot_resurrect():
    fixture, _, _ = _ready()
    bridge, _, candidates, runtimes, _, _, provider, scheduler, source, request = fixture

    def fail(_mission):
        raise RuntimeError("fixture transport failure")

    bridge.event_loop.adapter.mission_installer = fail
    with pytest.raises(RuntimeError, match="fixture transport failure"):
        scheduler.submit_if_ready(mission_id="fixture-failed-upload")
    assert bridge.event_loop.adapter.faulted
    assert not provider.evidence()["source_ready"]
    assert not provider.evidence()["armed"]
    with pytest.raises(OnboardVlmExecutionError, match="retired"):
        provider.submit_source(
            request,
            source,
            candidates[0].task,
            canonical_json(runtimes[candidates[0].task.task_id]),
        )


def test_source_event_missing_or_changed_refuses_worker_submit():
    _, _, _, _, worker, _, _, scheduler, source, request = _fixture()
    for candidate in (
        replace(request, source_event=None),
        replace(request, source_event=replace(source.event, event_id="different")),
    ):
        with pytest.raises(OnboardVlmExecutionError, match="real source"):
            scheduler.submit_source(candidate, source)
    assert worker.submissions == []


def test_current_timestamp_regression_and_wrong_actual_identity_reject():
    fixture, _, current = _ready()
    _, _, _, _, _, _, _, scheduler, source, _ = fixture
    with pytest.raises(OnboardVlmExecutionError, match="regressed"):
        scheduler.update_current(source)
    with pytest.raises(OnboardVlmExecutionError, match="actual adapter"):
        scheduler.update_current(replace(current, receipt_revision=99))


def test_evidence_stays_development_only_and_json_finite():
    _, _, _, _, _, _, provider, _, _, _ = _fixture()
    evidence = json.loads(canonical_json(provider.evidence()))
    for key in (
        "route_clearance_producer_implemented",
        "collector_receipt_join_implemented",
        "arbitrary_prompt_resolution_verified",
        "actual_inference_verified",
        "autonomous_flight_verified",
        "execution_authority",
        "candidate_promoted",
        "source_timestamps_rewritten",
        "model_saw_execution_context",
    ):
        assert evidence[key] is False


def test_public_arm_cannot_bypass_canonical_compiler_with_replaced_plan():
    fixture, _, current = _ready()
    _, _, _, _, _, _, provider, _, _, _ = fixture
    admission = provider.prepare_admission(current, now_s=2.2, mission_id="fixture", policy=POLICY)
    forged = replace(
        admission, effective_plan=replace(admission.effective_plan, north_offset_m=90.0)
    )
    with pytest.raises(OnboardVlmAdmissionError, match="canonical revalidation"):
        provider.arm(forged)
    assert not provider.evidence()["armed"]


@pytest.mark.parametrize("at_s", [2.04, 2.19, 3.01])
def test_armed_clock_regression_or_expiry_never_consumes_a_proposal(at_s):
    fixture, _, current = _ready()
    _, _, _, _, worker, clock, provider, _, _, _ = fixture
    admission = provider.prepare_admission(current, now_s=2.2, mission_id="fixture", policy=POLICY)
    provider.arm(admission)
    before = worker.operations[:]
    clock.at_s = at_s
    with pytest.raises(OnboardVlmExecutionDeferred):
        provider.propose_for_session(current.context, current.event, current.scope)
    assert worker.operations == before
    assert not provider.evidence()["armed"] and not provider.evidence()["source_ready"]
    clock.at_s = 2.2
    with pytest.raises(OnboardVlmExecutionDeferred, match="no armed"):
        provider.propose_for_session(current.context, current.event, current.scope)


def test_actual_direct_adapter_call_changes_identity_without_loop_bookkeeping():
    fixture = _fixture()
    bridge, runner, _, _, worker, _, provider, scheduler, source, request = fixture
    scheduler.submit_source(request, source)
    loop = bridge.event_loop
    loop.adapter.executor.decision_provider = ModelBackedDecisionProvider(
        runner,
        model_id="fixture-existing",
        model_version="fixture-existing",
        model_sha256="a" * 64,
        provider="fixture-existing-no-gpu",
    )
    context = replace(source.context, captured_at_s=2.05, snapshot_id="fixture-direct-context")
    event = replace(source.event, observed_at_s=2.05, event_id="fixture-direct-event")
    direct = loop.adapter.handle_event(context, event, mission_id="fixture-direct")
    loop.adapter.executor.decision_provider = provider
    assert direct.sequence == source.mission_revision + 1
    assert loop.last_revision.sequence == source.mission_revision
    assert loop.processed_event_count == 0
    actual = capture_execution_snapshot(loop, context, event)
    assert actual.mission_sha256 == direct.mission.canonical_hash
    assert actual.mission_revision == direct.sequence
    assert actual.receipt_revision == len(loop.adapter.task_state.receipts)
    with pytest.raises(OnboardVlmExecutionError, match="actual adapter"):
        scheduler.submit_if_ready(mission_id="fixture-stale-wrapper")
    assert worker.cancellations and not provider.evidence()["pending"]


@pytest.mark.parametrize("state", ["failed", "timed_out", "cancelled", "closed"])
def test_terminal_worker_state_retires_source_without_adapter(state):
    bridge, _, _, _, worker, _, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    worker.state = state
    assert scheduler.submit_if_ready(mission_id="fixture-terminal") is None
    assert not provider.evidence()["pending"]
    assert bridge.event_loop.processed_event_count == 0


def test_completed_answer_disarms_on_new_ordinary_frame_but_reuses_original_anchor():
    fixture, anchor, current = _ready()
    _, _, _, _, worker, _, provider, scheduler, _, _ = fixture
    admission = provider.prepare_admission(current, now_s=2.2, mission_id="fixture", policy=POLICY)
    provider.arm(admission)
    newer = _current(anchor, at_s=2.15, north=4.0, east=5.0)
    scheduler.update_current(newer)
    assert not provider.evidence()["armed"]
    assert provider.poll_source() is anchor
    assert worker.cancellations == []
    assert scheduler.submit_if_ready(mission_id="fixture-newer-ordinary").event is newer.event


def test_scope_change_cancels_pending_and_requires_explicit_rewarming():
    _, _, candidates, runtimes, worker, clock, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    new_scope = replace(source.scope, clock_epoch="fixture-new-clock-epoch")
    changed = replace(source, scope=new_scope)
    new_request, _, _, _ = _source(
        2.0,
        context=source.context,
        event=source.event,
        scope=new_scope,
        prompt=request.model_input.prompt,
    )
    runtime = canonical_json(runtimes[candidates[0].task.task_id])
    assert provider.submit_source(new_request, changed, candidates[0].task, runtime) is None
    assert worker.state == "cancelled"
    assert provider.advance_latest() is None
    assert len(worker.submissions) == 1
    worker.state = "idle"  # fixture stand-in for an explicit outer prewarm.
    clock.at_s = 2.1
    assert provider.advance_latest() == 2
    assert worker.submissions[-1][0] is new_request


def test_source_submission_retired_during_worker_submit_cannot_publish_active(monkeypatch):
    _, _, _, _, worker, _, provider, scheduler, source, request = _fixture()
    original_submit = worker.submit

    def retired_during_submit(*args, **kwargs):
        generation = original_submit(*args, **kwargs)
        provider.observe_snapshot(replace(source, mission_revision=99))
        return generation

    monkeypatch.setattr(worker, "submit", retired_during_submit)
    with pytest.raises(OnboardVlmExecutionError, match="retired during"):
        scheduler.submit_source(request, source)
    assert worker.cancellations
    assert not provider.evidence()["pending"]


def test_successful_dispatch_retires_queued_old_mission_source():
    fixture, anchor, current = _ready()
    _, _, _, _, worker, clock, provider, scheduler, _, request = fixture
    newer_request, _, _, _ = _source(
        current.context.captured_at_s,
        context=current.context,
        event=current.event,
        scope=current.scope,
        prompt=request.model_input.prompt,
    )
    assert provider.submit_source(newer_request, current, anchor.task, anchor.runtime_json) is None
    assert provider.evidence()["queued_latest"]
    scheduler.submit_if_ready(mission_id="fixture-consume-with-queued")
    assert not provider.evidence()["queued_latest"]
    clock.at_s = 2.25
    with pytest.raises(OnboardVlmExecutionError, match="retired"):
        provider.submit_source(newer_request, current, anchor.task, anchor.runtime_json)
    assert provider.advance_latest() is None and len(worker.submissions) == 1


def test_ordinary_queue_rejects_backward_observation_order():
    fixture = _fixture()
    _, _, candidates, runtimes, _, clock, provider, scheduler, source, request = fixture
    scheduler.submit_source(request, source)
    task, runtime = candidates[0].task, canonical_json(runtimes[candidates[0].task.task_id])

    def observation(at_s):
        context = replace(source.context, captured_at_s=at_s, snapshot_id=f"fixture-{at_s}")
        event = replace(source.event, observed_at_s=at_s, event_id=f"fixture-{at_s}")
        snapshot = replace(source, context=context, event=event)
        new_request, _, _, _ = _source(
            at_s,
            context=context,
            event=event,
            scope=source.scope,
            prompt=request.model_input.prompt,
        )
        return new_request, snapshot

    clock.at_s = 2.2
    newest, snapshot = observation(2.2)
    provider.submit_source(newest, snapshot, task, runtime)
    older, older_snapshot = observation(2.1)
    with pytest.raises(OnboardVlmExecutionError, match="timestamp regressed"):
        provider.submit_source(older, older_snapshot, task, runtime)


def test_shared_policy_switch_without_mission_change_retires_source():
    fixture, _, current = _ready()
    bridge, _, _, _, _, _, provider, scheduler, _, _ = fixture
    executor = bridge.event_loop.adapter.executor
    old_policy = executor.shared_policy
    ranker = TaskRanker((1.0, 0.0, 0.0), "fixture-new-ranker")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="fixture-new-policy",
        policy_version="fixture-new-version",
        airframe_profile_hash=executor.airframe_profile_hash,
        mission_contract_hash=old_policy.artifact.mission_contract_hash,
        safety_policy_hash=old_policy.artifact.safety_policy_hash,
    )
    executor.shared_policy = SharedTaskPolicy(ranker, artifact)
    actual = capture_execution_snapshot(
        bridge.event_loop, current.context, current.event, clearance=current.clearance
    )
    assert actual.policy_artifact_sha256 != current.policy_artifact_sha256
    assert actual.mission_sha256 == current.mission_sha256
    assert actual.receipt_revision == current.receipt_revision
    with pytest.raises(OnboardVlmExecutionError, match="actual adapter"):
        scheduler.submit_if_ready(mission_id="fixture-changed-policy")
    assert not provider.evidence()["source_ready"]
    assert bridge.event_loop.processed_event_count == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "fixture-wrong-model"),
        ("model_version", "fixture-wrong-version"),
        ("model_sha256", "f" * 64),
    ],
)
def test_pending_model_identity_cannot_be_relabelled(field, value):
    _, _, _, _, worker, clock, provider, scheduler, source, request = _fixture()
    scheduler.submit_source(request, source)
    _finish(worker, clock, request)
    setattr(provider, field, value)
    with pytest.raises(OnboardVlmExecutionError, match="model identity changed"):
        provider.poll_source()
    assert not provider.evidence()["pending"] and not provider.evidence()["source_ready"]
    assert worker.cancellations


def test_actual_ranker_tamper_cannot_hide_behind_unchanged_artifact_hash():
    fixture, _, current = _ready()
    bridge, _, _, _, _, _, provider, scheduler, _, _ = fixture
    policy = bridge.event_loop.adapter.executor.shared_policy
    original_hash = policy.artifact_sha256
    object.__setattr__(policy.ranker, "coefficients", (1.0, 0.0, 0.0))
    assert policy.artifact_sha256 == original_hash == current.policy_artifact_sha256
    with pytest.raises(OnboardVlmExecutionError, match="actual ranker"):
        scheduler.submit_if_ready(mission_id="fixture-ranker-tamper")
    assert not provider.evidence()["source_ready"]
    assert bridge.event_loop.processed_event_count == 0


def test_private_prompt_resolution_cannot_replace_single_registered_task(monkeypatch):
    fixture, _, _ = _ready()
    bridge, _, candidates, runtimes, _, _, provider, scheduler, _, _ = fixture
    executor = bridge.event_loop.adapter.executor
    original = provider.prepare_admission

    def replace_resolution(*args, **kwargs):
        admission = original(*args, **kwargs)
        extra_task = replace(candidates[0].task, task_id="fixture-other-task")
        extra_candidate = replace(candidates[0], task=extra_task)
        executor._prompt_resolution = PromptTaskResolution(
            executor.prompt.canonical_hash,
            "fixture-new-catalog",
            (candidates[0], extra_candidate),
            {**runtimes, extra_task.task_id: runtimes[candidates[0].task.task_id]},
        )
        return admission

    monkeypatch.setattr(provider, "prepare_admission", replace_resolution)
    with pytest.raises(OnboardVlmExecutionError, match="Prompt resolution"):
        scheduler.submit_if_ready(mission_id="fixture-private-catalog-cas")
    assert bridge.event_loop.processed_event_count == 0


def test_single_registered_prompt_resolution_keeps_same_task_and_frozen_runtime():
    fixture, _, _ = _ready()
    bridge, _, candidates, runtimes, _, _, _, scheduler, _, _ = fixture
    executor = bridge.event_loop.adapter.executor
    executor._prompt_resolution = PromptTaskResolution(
        executor.prompt.canonical_hash,
        "fixture-one-approved-task",
        candidates,
        runtimes,
    )
    revision = scheduler.submit_if_ready(mission_id="fixture-one-approved-task")
    assert revision.flight_plan_dispatched
    assert revision.decision.selected_task_id == candidates[0].task.task_id


def _fresh_queue(fixture, anchor, *, at_s=3.0):
    _, _, _, _, _, clock, provider, scheduler, _, request = fixture
    current = _current(anchor, at_s=at_s)
    fresh_request, _, _, _ = _source(
        at_s,
        context=current.context,
        event=current.event,
        scope=current.scope,
        prompt=request.model_input.prompt,
    )
    clock.at_s = at_s
    scheduler.update_current(current)
    assert provider.submit_source(fresh_request, current, anchor.task, anchor.runtime_json) is None
    return fresh_request, current


@pytest.mark.parametrize("expiry", ["deadline", "history_age", "source_policy_age"])
def test_expired_ready_retires_only_old_source_and_fresh_queue_can_advance(expiry):
    fixture = _fixture()
    bridge, _, _, _, worker, clock, provider, scheduler, source, request = fixture
    if expiry == "history_age":
        request = replace(
            request,
            model_input=replace(request.model_input, deadline_at_s=10.0, max_frame_age_s=0.5),
        )
    elif expiry == "source_policy_age":
        request = replace(request, model_input=replace(request.model_input, deadline_at_s=10.0))
        scheduler.policy = replace(POLICY, max_source_age_s=0.5)
    scheduler.submit_source(request, source)
    _finish(worker, clock, request)
    old_result = worker.result
    anchor = provider.poll_source()
    fresh_request, _ = _fresh_queue(fixture, anchor)
    clock.at_s = 3.1
    with pytest.raises(OnboardVlmExecutionError, match="deadline/history expired"):
        scheduler.submit_if_ready(mission_id="fixture-expired-ready")
    assert not provider.evidence()["source_ready"]
    assert provider.evidence()["queued_latest"]
    assert worker.state == "idle" and worker.cancellations == []
    assert bridge.event_loop.processed_event_count == 0
    assert provider.poll_source() is None
    assert any(
        row.get("kept_latest_request_sha256") == fresh_request.canonical_hash
        for row in provider.evidence()["audit"]
        if row["status"] == "source_expired"
    )
    worker.result = old_result  # A stray old answer cannot recreate an anchor.
    assert provider.poll_source() is None
    # A later clock restoration cannot resurrect the tombstoned source.
    clock.at_s = 2.2
    with pytest.raises(OnboardVlmExecutionError, match="retired"):
        provider.submit_source(request, source, anchor.task, anchor.runtime_json)
    assert old_result.request_sha256 == request.canonical_hash
    clock.at_s = 3.1
    assert provider.advance_latest() == 2
    assert worker.submissions[-1][0] is fresh_request
    assert scheduler.submit_if_ready(mission_id="fixture-fresh-pending") is None
    assert bridge.event_loop.processed_event_count == 0
    clock.at_s = 3.15
    _finish_output = anchor.result.response.to_mapping()["output"]
    worker.finish(fresh_request, clock, _finish_output)
    new_anchor = provider.poll_source()
    new_current = _current(new_anchor, at_s=3.2, north=0.0, east=0.0)
    scheduler.update_current(new_current)
    clock.at_s = 3.25
    revision = scheduler.submit_if_ready(mission_id="fixture-fresh-ready")
    assert revision.flight_plan_dispatched and bridge.event_loop.processed_event_count == 1
    assert scheduler.last_execution_admission.anchor.request is fresh_request


def test_temporary_clearance_unknown_preserves_ready_anchor_and_fresh_queue():
    fixture, anchor, _ = _ready()
    bridge, _, _, _, worker, clock, provider, scheduler, _, _ = fixture
    _fresh_queue(fixture, anchor, at_s=2.3)
    current = _current(anchor, at_s=2.3)
    scheduler.update_current(replace(current, clearance=None))
    clock.at_s = 2.4
    with pytest.raises(OnboardVlmAdmissionError, match="clearance"):
        scheduler.submit_if_ready(mission_id="fixture-clearance-pending")
    assert provider.evidence()["source_ready"] and provider.evidence()["queued_latest"]
    assert provider.poll_source() is anchor and worker.cancellations == []
    assert bridge.event_loop.processed_event_count == 0


def test_expiry_during_locked_revalidation_keeps_fresh_queue(monkeypatch):
    fixture, anchor, _ = _ready()
    bridge, _, _, _, worker, clock, provider, scheduler, _, _ = fixture
    _fresh_queue(fixture, anchor, at_s=2.8)
    original = provider.prepare_admission

    def expires_after_prepare(*args, **kwargs):
        admission = original(*args, **kwargs)
        clock.at_s = 3.1
        return admission

    monkeypatch.setattr(provider, "prepare_admission", expires_after_prepare)
    with pytest.raises(ValueError):
        scheduler.submit_if_ready(mission_id="fixture-locked-expiry")
    assert not provider.evidence()["source_ready"] and provider.evidence()["queued_latest"]
    assert worker.cancellations == [] and bridge.event_loop.processed_event_count == 0
    assert provider.advance_latest() == 2


def test_consume_rechecks_clearance_time_without_compiling_or_killing_queue(monkeypatch):
    fixture, anchor, current = _ready()
    _, _, _, _, worker, clock, provider, _, _, request = fixture
    latest = _current(anchor, at_s=2.15)
    fresh_request, _, _, _ = _source(
        2.15,
        context=latest.context,
        event=latest.event,
        scope=latest.scope,
        prompt=request.model_input.prompt,
    )
    provider.submit_source(fresh_request, latest, anchor.task, anchor.runtime_json)
    clearance = _clearance(
        current.context,
        current.event,
        anchor.target,
        wire_hash=current.clearance.wire_hash,
        observed_at_s=1.25,
    )
    current = replace(current, clearance=clearance)
    admission = provider.prepare_admission(current, now_s=2.2, mission_id="fixture", policy=POLICY)
    provider.arm(admission)
    clock.at_s = 2.3  # clearance age 1.05, while source/current/deadline are valid.
    before = worker.operations[:]

    def must_not_compile(*_args, **_kwargs):
        raise AssertionError("proposal consumption must not compile")

    monkeypatch.setattr(
        "multidetect.autonomy.onboard_vlm_execution.admit_anchored_intent", must_not_compile
    )
    with pytest.raises(OnboardVlmExecutionDeferred, match="clearance"):
        provider.propose_for_session(current.context, current.event, current.scope)
    assert worker.operations == before and worker.cancellations == []
    assert not provider.evidence()["armed"]
    assert provider.evidence()["source_ready"] and provider.evidence()["queued_latest"]


def test_expired_latest_queue_is_not_preserved_as_fresh():
    fixture, anchor, _ = _ready()
    bridge, _, _, _, worker, clock, provider, scheduler, _, _ = fixture
    _fresh_queue(fixture, anchor, at_s=2.3)
    clock.at_s = 3.5
    with pytest.raises(OnboardVlmExecutionError, match="deadline/history expired"):
        scheduler.submit_if_ready(mission_id="fixture-both-expired")
    assert not provider.evidence()["source_ready"] and not provider.evidence()["queued_latest"]
    assert provider.advance_latest() is None
    assert worker.state == "idle" and worker.cancellations == []
    assert bridge.event_loop.processed_event_count == 0


@pytest.mark.parametrize(
    "cause", ["clearance", "deadline", "history_age", "policy_age", "new_observation"]
)
def test_actual_legacy_executor_unwinds_host_unavailability_before_any_commit(monkeypatch, cause):
    fixture = _fixture()
    bridge, _, _, _, worker, clock, provider, scheduler, source, request = fixture
    if cause in {"history_age", "policy_age"}:
        request = replace(
            request,
            model_input=replace(
                request.model_input,
                deadline_at_s=10.0,
                max_frame_age_s=(0.5 if cause == "history_age" else 5.0),
            ),
        )
    if cause == "policy_age":
        scheduler.policy = replace(POLICY, max_source_age_s=0.5)
    scheduler.submit_source(request, source)
    _finish(worker, clock, request)
    anchor = provider.poll_source()
    before_s, consume_s = (
        (3.0, 3.1)
        if cause == "deadline"
        else ((2.4, 2.6) if cause in {"history_age", "policy_age"} else (2.2, 2.3))
    )
    queue_s = before_s if cause != "clearance" else 2.1
    fresh_request, current = _fresh_queue(fixture, anchor, at_s=queue_s)
    if cause == "clearance":
        current = replace(
            current,
            clearance=_clearance(
                current.context,
                current.event,
                anchor.target,
                wire_hash=current.clearance.wire_hash,
                observed_at_s=1.25,
            ),
        )
        scheduler.update_current(current)
    clock.at_s = before_s
    adapter = bridge.event_loop.adapter
    executor = adapter.executor
    original_state = (
        adapter.current_mission,
        adapter.history,
        adapter.task_state.receipts,
        adapter.task_state.phase,
        bridge.event_loop.processed_event_count,
        adapter.safety_engaged,
        executor._last_context,
        executor._last_event_at_s,
    )
    original_propose = provider.propose_for_session

    def becomes_unavailable(*args):
        assert bridge.event_loop._lock._is_owned()
        clock.at_s = consume_s
        if cause == "new_observation":
            provider.observe_snapshot(_current(anchor, at_s=consume_s))
        return original_propose(*args)

    monkeypatch.setattr(provider, "propose_for_session", becomes_unavailable)
    assert scheduler.submit_if_ready(mission_id="fixture-real-core-unavailable") is None
    assert not bridge.event_loop._lock._is_owned()
    assert original_state == (
        adapter.current_mission,
        adapter.history,
        adapter.task_state.receipts,
        adapter.task_state.phase,
        bridge.event_loop.processed_event_count,
        adapter.safety_engaged,
        executor._last_context,
        executor._last_event_at_s,
    )
    assert provider.evidence()["queued_latest"] and worker.cancellations == []
    monkeypatch.setattr(provider, "propose_for_session", original_propose)
    if cause in {"deadline", "history_age", "policy_age"}:
        assert not provider.evidence()["source_ready"]
        assert provider.advance_latest() == 2
        clock.at_s = consume_s + 0.05
        worker.finish(fresh_request, clock, anchor.result.response.to_mapping()["output"])
        anchor = provider.poll_source()
    # Fresh independently bound execution facts, without retiming the old raw.
    next_at = consume_s + 0.1
    next_current = _current(anchor, at_s=next_at, north=0.0, east=0.0)
    scheduler.update_current(next_current)
    clock.at_s = next_at + 0.01
    assert scheduler.submit_if_ready(mission_id="fixture-real-core-fresh").flight_plan_dispatched
    assert bridge.event_loop.processed_event_count == 1


@pytest.mark.parametrize(
    "fault", ["runtime_error", "model_identity", "image_bytes", "raw_bytes", "generation"]
)
def test_actual_legacy_executor_keeps_real_fault_as_safety_hold(monkeypatch, fault):
    fixture, anchor, _ = _ready()
    bridge, _, _, _, _, _, provider, scheduler, _, _ = fixture
    original_propose = provider.propose_for_session

    def genuine_fault(*args):
        if fault == "runtime_error":
            raise RuntimeError("fixture genuine provider failure")
        if fault == "model_identity":
            provider.model_sha256 = "f" * 64
        elif fault == "image_bytes":
            object.__setattr__(
                anchor.request.model_input.frames[-1], "image_bytes", b"\x09\x08\x07"
            )
        elif fault == "raw_bytes":
            object.__setattr__(anchor.result, "raw_output", "{}")
        else:
            object.__setattr__(anchor.result, "generation", 99)
        return original_propose(*args)

    monkeypatch.setattr(provider, "propose_for_session", genuine_fault)
    with pytest.raises(OnboardVlmExecutionError, match="did not dispatch"):
        scheduler.submit_if_ready(mission_id="fixture-real-core-fault")
    adapter = bridge.event_loop.adapter
    assert adapter.current_mission is None and adapter.safety_engaged
    assert adapter.history[-1].decision.reason == "model_provider_fault"
    assert bridge.event_loop.processed_event_count == 1
    assert not provider.evidence()["source_ready"]
