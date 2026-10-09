from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from test_onboard_agent import _context

from multidetect.autonomy.contracts import PromptEnvelope
from multidetect.autonomy.onboard_agent import (
    OnboardEventKind,
    OnboardResponse,
    OnboardRuntimeEvent,
)
from multidetect.autonomy.onboard_history import OnboardInferenceScope
from multidetect.autonomy.onboard_multimodal import (
    MissionEvidenceLedger,
    OnboardImageFrame,
    OnboardMultimodalInput,
    OnboardMultimodalScope,
)
from multidetect.autonomy.onboard_vlm_provider import (
    OnboardVlmProviderError,
    OnboardVlmReadyDecisionProvider,
    OnboardVlmReadyScheduler,
)
from multidetect.autonomy.onboard_vlm_runtime_contract import (
    RESPONSE_CONTRACT,
    OnboardVlmRuntimeRequest,
    parse_onboard_vlm_runtime_response,
)
from multidetect.autonomy.onboard_vlm_worker import OnboardVlmWorkerResult


class _Clock:
    def __init__(self, at_s=10.0):
        self.at_s = at_s

    def __call__(self):
        return self.at_s


class _FakeWorker:
    """No backend, process, GPU or transport; return the real typed result shape."""

    def __init__(self):
        self.state = "idle"
        self.generation = 0
        self.submissions = []
        self.cancellations = []
        self.result = None
        self.poll_calls = 0
        self.take_calls = 0

    def submit(self, request, *, observed_now_s):
        if self.state != "idle":
            raise RuntimeError("explicit prewarming required")
        self.generation += 1
        self.submissions.append((request, observed_now_s, self.generation))
        self.state = "pending"
        return self.generation

    def poll(self):
        self.poll_calls += 1
        return self.result if self.state == "ready" else None

    def take_result(self):
        self.take_calls += 1
        result, self.result = self.result, None
        self.state = "idle"
        return result

    def cancel(self, reason):
        self.cancellations.append(reason)
        self.result = None
        self.state = "cancelled"

    def finish(self, request, clock, output=None):
        raw = _raw(request, output)
        elapsed = (clock.at_s - request.model_input.requested_at_s) * 1000
        response = parse_onboard_vlm_runtime_response(
            raw,
            request,
            current_scope=request.model_input.scope,
            completed_at_s=clock.at_s,
            inference_latency_ms=elapsed,
        )
        self.result = OnboardVlmWorkerResult(
            self.generation,
            request.canonical_hash,
            response,
            raw,
            clock.at_s,
            elapsed,
            '{"fixture":true}',
        )
        self.state = "ready"


def _prompt():
    return PromptEnvelope(
        "fixture-observe",
        "检查当前画面；没有定位证据就不要声称GPS位置。",
        "zh",
        "2026-10-07T00:00:00Z",
        "fixture-operator",
        "fixture-aircraft",
    )


def _scope(context=None, prompt=None):
    context, prompt = context or _context(), prompt or _prompt()
    return OnboardInferenceScope(
        "11111111-1111-4111-8111-111111111111",
        prompt.canonical_hash,
        prompt.aircraft_id,
        "task-1",
        context.airframe_profile_id,
        context.airframe_profile_hash,
        "multirotor",
        context.home_position.canonical_hash,
        "fixture-epoch-not-sensor-clock",
    )


def _source(
    at_s=10.0, *, context=None, event=None, scope=None, prompt=None, pixels=b"\x01\x02\x03"
):
    context, prompt = context or _context(at_s), prompt or _prompt()
    scope = scope or _scope(context, prompt)
    event = event or OnboardRuntimeEvent(
        event_id=f"fixture-event-{at_s}",
        kind=OnboardEventKind.TARGET_LOST,
        observed_at_s=at_s,
        detail="fixture observation, not model/action gold",
        obstacle_distance_m=None,
    )
    frame = OnboardImageFrame(
        scope,
        f"frame-{at_s}",
        at_s,
        pixels,
        hashlib.sha256(pixels).hexdigest(),
        1,
        1,
        context=context,
    )
    model_input = OnboardMultimodalInput(
        scope,
        prompt,
        (frame,),
        MissionEvidenceLedger(scope, frames=(frame,)),
        at_s,
        at_s + 1.0,
    )
    return OnboardVlmRuntimeRequest(model_input, source_event=event), context, event, scope


def _raw(request, output=None):
    return json.dumps(
        {
            "schema_version": 2,
            "contract": RESPONSE_CONTRACT,
            "frame_ids": [request.model_input.frames[-1].frame_id],
            "evidence_ids": [],
            "output": output
            or {
                "response": "hold",
                "reason": "fixture only, evidence unknown",
                "confidence": 0.5,
                "task_action": None,
                "flight_plan": None,
            },
        },
        ensure_ascii=False,
        allow_nan=False,
    )


def _provider():
    worker, clock = _FakeWorker(), _Clock()
    provider = OnboardVlmReadyDecisionProvider(
        worker,
        model_id="fixture-vlm-not-inference",
        model_version="fixture-v2",
        model_sha256="a" * 64,
        clock=clock,
    )
    return provider, worker, clock


def _arm(provider, worker, clock, source=None):
    source = source or _source()
    request, context, event, scope = source
    provider.submit_source(request, context, event)
    clock.at_s = request.model_input.requested_at_s + 0.05
    worker.finish(request, clock)
    assert provider.ready_if_bound(context, event, scope)
    return source


def test_pending_never_polls_from_proposal_or_returns_fake_hold():
    provider, worker, _clock = _provider()
    request, context, event, scope = _source()
    assert provider.submit_source(request, context, event) == 1
    assert not provider.ready_if_bound(context, event, scope)
    polls = worker.poll_calls
    with pytest.raises(OnboardVlmProviderError, match="no armed"):
        provider.propose_for_session(context, event, scope)
    assert worker.poll_calls == polls
    assert worker.take_calls == 0
    assert provider.evidence()["consumed_request_count"] == 0


def test_real_result_type_exact_raw_binding_and_single_consumption():
    provider, worker, clock = _provider()
    request, context, event, scope = _arm(provider, worker, clock)
    polls = worker.poll_calls
    proposal = provider.propose_for_session(context, event, scope)
    assert proposal.response is OnboardResponse.HOLD
    assert proposal.model_id == "fixture-vlm-not-inference"
    assert proposal.inference_latency_ms == pytest.approx(50.0)
    assert worker.poll_calls == polls  # the real worker take_result may do its own bounded poll
    assert worker.take_calls == 1
    assert (
        provider.evidence()["audit"][-1]["raw_response_sha256"]
        == hashlib.sha256(_raw(request).encode()).hexdigest()
    )
    with pytest.raises(OnboardVlmProviderError):
        provider.propose_for_session(context, event, scope)
    with pytest.raises(OnboardVlmProviderError, match="already been consumed"):
        provider.submit_source(request, context, event)


@pytest.mark.parametrize("change", ["context", "event", "scope", "receipt"])
def test_current_binding_mismatch_does_not_arm_poll_consume_or_relabel(change):
    provider, worker, clock = _provider()
    source = _source()
    request, context, event, scope = source
    provider.submit_source(request, context, event)
    clock.at_s = 10.05
    worker.finish(request, clock)
    kwargs = {"context": context, "event": event, "scope": scope, "receipt_revision": 0}
    if change == "context":
        kwargs["context"] = replace(context, battery_remaining_pct=80)
    elif change == "event":
        kwargs["event"] = replace(event, detail="new fact, same time")
    elif change == "scope":
        kwargs["scope"] = replace(scope, selected_task_id="other-task")
    else:
        kwargs["receipt_revision"] = 1
    assert not provider.ready_if_bound(**kwargs)
    assert worker.poll_calls == 0 and worker.take_calls == 0
    assert provider.evidence()["audit"][-1]["status"] == "current_source_mismatch_not_admitted"
    assert provider.ready_if_bound(context, event, scope)


@pytest.mark.parametrize("change", ["context", "event", "scope"])
def test_armed_proposal_rejects_different_current_context_event_or_scope(change):
    provider, worker, clock = _provider()
    _request, context, event, scope = _arm(provider, worker, clock)
    if change == "context":
        context = replace(context, snapshot_id="new-context")
    elif change == "event":
        event = replace(event, event_id="new-event")
    else:
        scope = replace(scope, clock_epoch="new-epoch")
    with pytest.raises(OnboardVlmProviderError, match="no armed"):
        provider.propose_for_session(context, event, scope)
    assert worker.take_calls == 0


def test_inference_scope_and_explicit_multimodal_scope_are_exact_field_equivalents():
    provider, worker, clock = _provider()
    request, context, event, scope = _source()
    multimodal = OnboardMultimodalScope.from_inference_scope(scope)
    request, context, event, _ = _source(scope=multimodal)
    _arm(provider, worker, clock, (request, context, event, multimodal))
    assert provider.propose_for_session(context, event, scope).response is OnboardResponse.HOLD


def test_ordinary_rgb_preserves_inflight_and_replaces_only_latest_queue():
    provider, worker, clock = _provider()
    first = _source()
    provider.submit_source(*first[:3])
    assert provider.submit_source(*first[:3]) == 1
    clock.at_s = 10.1
    second = _source(10.1, pixels=b"\x02\x03\x04")
    assert provider.submit_source(*second[:3]) is None
    clock.at_s = 10.2
    third = _source(10.2, pixels=b"\x03\x04\x05")
    assert provider.submit_source(*third[:3]) is None
    assert provider.advance_latest() is None
    assert len(worker.submissions) == 1 and not worker.cancellations
    assert provider.evidence()["queued_request_sha256"] == third[0].canonical_hash
    worker.finish(first[0], clock)
    assert provider.ready_if_bound(first[1], first[2], first[3])
    provider.propose_for_session(first[1], first[2], first[3])
    assert provider.advance_latest() == 2
    assert len(worker.submissions) == 2
    assert worker.submissions[-1][0] is third[0]


@pytest.mark.parametrize("change", ["scope", "receipt"])
def test_scope_or_receipt_change_cancels_and_requires_explicit_outer_prewarm(change):
    provider, worker, clock = _provider()
    first = _source()
    provider.submit_source(*first[:3])
    clock.at_s = 10.1
    scope = replace(first[3], selected_task_id="new-task") if change == "scope" else first[3]
    second = _source(10.1, scope=scope)
    revision = 1 if change == "receipt" else 0
    assert provider.submit_source(*second[:3], receipt_revision=revision) is None
    assert worker.cancellations == ["scope_or_receipt_changed"]
    assert len(worker.submissions) == 1 and provider.state == "queued"
    assert not provider.ready_if_bound(first[1], first[2], first[3])
    with pytest.raises(RuntimeError, match="prewarming"):
        provider.advance_latest()
    assert provider.evidence()["queued_request_sha256"] == second[0].canonical_hash
    worker.state = "idle"  # explicit caller prewarming, never done by provider
    assert provider.advance_latest() == 2


def test_receipt_facts_cannot_change_silently_under_same_revision():
    provider, _worker, clock = _provider()
    first = _source()
    provider.submit_source(*first[:3])
    clock.at_s = 10.1
    request, context, event, _scope_value = _source(10.1)
    request = replace(request, host_reviewed_receipt_sources=("fixture-reviewed-source",))
    with pytest.raises(OnboardVlmProviderError, match="without a receipt revision"):
        provider.submit_source(request, context, event)


@pytest.mark.parametrize("bad", [True, -1, 0.5])
def test_bad_receipt_revision_is_not_a_truthy_counter(bad):
    provider, worker, _clock = _provider()
    with pytest.raises(ValueError):
        provider.submit_source(*_source()[:3], receipt_revision=bad)
    assert not worker.submissions


def test_queued_time_regression_and_reused_event_identity_fail_closed():
    provider, worker, clock = _provider()
    first = _source()
    provider.submit_source(*first[:3])
    clock.at_s = 10.2
    second = _source(10.2)
    provider.submit_source(*second[:3])
    with pytest.raises(OnboardVlmProviderError, match="time regressed"):
        provider.submit_source(*_source(10.1)[:3])
    third = _source(
        10.2, event=replace(second[2], detail="another payload"), pixels=b"\x03\x04\x05"
    )
    with pytest.raises(OnboardVlmProviderError, match="event ID reused"):
        provider.submit_source(*third[:3])
    assert not worker.cancellations


@pytest.mark.parametrize("state", ["failed", "timed_out", "cancelled", "closed"])
def test_terminal_worker_does_not_create_fallback_proposal(state):
    provider, worker, _clock = _provider()
    request, context, event, scope = _source()
    provider.submit_source(request, context, event)
    worker.state = state
    assert not provider.ready_if_bound(context, event, scope)
    with pytest.raises(OnboardVlmProviderError):
        provider.propose_for_session(context, event, scope)
    assert provider.state == "idle" and worker.take_calls == 0


@pytest.mark.parametrize(
    "field",
    [
        "generation",
        "request_sha256",
        "response",
        "raw_output",
        "completed_at_s",
        "inference_latency_ms",
    ],
)
def test_foreign_tampered_or_nonfinite_worker_result_never_arms(field):
    provider, worker, clock = _provider()
    request, context, event, scope = _source()
    provider.submit_source(request, context, event)
    clock.at_s = 10.05
    worker.finish(request, clock)
    changes = {
        "generation": 2,
        "request_sha256": "f" * 64,
        "response": replace(
            worker.result.response,
            output=replace(worker.result.response.output, reason="tampered typed output"),
        ),
        "raw_output": _raw(request).replace("fixture only", "different raw"),
        "completed_at_s": float("nan"),
        "inference_latency_ms": float("inf"),
    }
    worker.result = replace(worker.result, **{field: changes[field]})
    with pytest.raises(ValueError):
        provider.ready_if_bound(context, event, scope)
    assert worker.cancellations == ["ready_validation_failed"]
    assert worker.take_calls == 0 and provider.state == "idle"


def test_changed_result_after_arming_is_rejected_on_take():
    provider, worker, clock = _provider()
    _request, context, event, scope = _arm(provider, worker, clock)
    worker.result = replace(worker.result, raw_output="another response")
    with pytest.raises(OnboardVlmProviderError, match="changed before"):
        provider.propose_for_session(context, event, scope)
    assert provider.evidence()["consumed_request_count"] == 0


@pytest.mark.parametrize("armed", [False, True])
def test_host_source_expiry_before_arm_or_consumption_is_rejected(armed):
    provider, worker, clock = _provider()
    source = _source()
    if armed:
        _arm(provider, worker, clock, source)
    else:
        provider.submit_source(*source[:3])
    clock.at_s = 11.01
    with pytest.raises(ValueError):
        if armed:
            provider.propose_for_session(source[1], source[2], source[3])
        else:
            provider.ready_if_bound(source[1], source[2], source[3])
    assert provider.evidence()["consumed_request_count"] == 0


def test_source_context_unknown_or_mismatched_event_cannot_enter_executor_provider():
    provider, worker, _clock = _provider()
    request, context, event, _scope_value = _source()
    frame = replace(request.model_input.frames[-1], context=None)
    model_input = replace(
        request.model_input,
        frames=(frame,),
        ledger=MissionEvidenceLedger(frame.scope, frames=(frame,)),
    )
    unknown = OnboardVlmRuntimeRequest(model_input)
    with pytest.raises(OnboardVlmProviderError, match="exact provided source context"):
        provider.submit_source(unknown, context, event)
    with pytest.raises(OnboardVlmProviderError, match="source event differs"):
        provider.submit_source(request, context, replace(event, detail="wrong source"))
    assert not worker.submissions


def test_context_event_clock_mismatch_is_not_repaired():
    provider, worker, _clock = _provider()
    request, context, event, _scope_value = _source()
    with pytest.raises(OnboardVlmProviderError, match="times must exactly agree"):
        provider.submit_source(request, context, replace(event, observed_at_s=9.9))
    assert not worker.submissions


def test_execution_provider_rejects_event_not_supplied_in_the_model_request():
    provider, worker, _clock = _provider()
    request, context, event, _scope_value = _source()
    request = replace(request, source_event=None)
    with pytest.raises(OnboardVlmProviderError, match="source event supplied to the model"):
        provider.submit_source(request, context, event)
    assert not worker.submissions


def test_cancel_drops_armed_and_queued_sources_without_consumption():
    provider, worker, clock = _provider()
    source = _arm(provider, worker, clock)
    clock.at_s = 10.1
    provider.submit_source(*_source(10.1)[:3])
    provider.cancel("explicit_session_stop")
    assert provider.state == "idle"
    assert provider.advance_latest() is None
    assert not provider.ready_if_bound(source[1], source[2], source[3])
    assert provider.evidence()["consumed_request_count"] == 0


def test_unscoped_proposal_and_permission_claims_are_explicitly_rejected():
    provider, worker, _clock = _provider()
    _request, context, event, _scope_value = _source()
    with pytest.raises(OnboardVlmProviderError, match="explicit session scope"):
        provider.propose(context, event)
    evidence = provider.evidence()
    for key in (
        "source_vs_current_async_admission",
        "arbitrary_prompt_resolution_verified",
        "execution_authority",
        "actual_model_inference_verified",
        "autonomous_flight_verified",
        "hardware_acceptance",
        "candidate_promoted",
    ):
        assert evidence[key] is False
    evidence["audit"].append({"status": "forged"})
    assert not provider.evidence()["audit"]
    assert not worker.submissions


def test_existing_software_event_loop_has_no_pending_side_effect_and_one_causal_ready_replan():
    # Existing planner/compiler/event-loop fixture, not a model or PX4 run.
    from test_onboard_realtime_bridge import _bridge
    from test_onboard_realtime_bridge import _context as bridge_context

    bridge, old_runner, _candidates, _runtimes = _bridge()
    bridge.prepare(bridge_context(1.0))
    context = bridge_context(2.0, altitude_m=35.0)
    assert bridge.mark_takeoff_complete(context)
    provider, worker, clock = _provider()
    clock.at_s = 2.0
    executor = bridge.event_loop.adapter.executor
    executor.decision_provider = provider
    scheduler = OnboardVlmReadyScheduler(bridge.event_loop, provider)
    event = OnboardRuntimeEvent(
        event_id="fixture-new-vlm-source",
        kind=OnboardEventKind.TARGET_LOST,
        observed_at_s=2.0,
        detail="fixture measured free-space; no physical sensor claim",
        obstacle_distance_m=100.0,
    )
    scope = executor.model_session_scope(context)
    request, context, event, _ = _source(
        2.0,
        context=context,
        event=event,
        scope=scope,
        prompt=executor.prompt,
    )
    provider.submit_source(request, context, event)
    initial_mission = bridge.event_loop.adapter.current_mission
    initial_history = bridge.event_loop.adapter.history
    assert scheduler.submit_if_ready(context, event) is None
    assert bridge.event_loop.adapter.current_mission is initial_mission
    assert bridge.event_loop.adapter.history == initial_history
    assert bridge.event_loop.processed_event_count == 0
    assert not bridge.event_loop.adapter.safety_engaged
    clock.at_s = 2.05
    worker.finish(
        request,
        clock,
        {
            "response": "replan",
            "reason": "fixture ready-only learned seam, not inference",
            "confidence": 0.95,
            "task_action": "search",
            "flight_plan": {
                "action": "search",
                "north_offset_m": 0.0,
                "east_offset_m": -25.0,
                "altitude_m": 35.0,
                "speed_mps": None,
                "duration_s": None,
                "reference_id": None,
            },
        },
    )
    revision = scheduler.submit_if_ready(context, event, mission_id="fixture-vlm-ready-once")
    assert revision.decision.response is OnboardResponse.REPLAN
    assert revision.decision.model_proposal.model_id == provider.model_id
    assert revision.flight_plan_dispatched and revision.mission is not None
    assert revision.decision.planner_revalidated
    assert bridge.event_loop.processed_event_count == 1
    assert provider.evidence()["consumed_request_count"] == 1
    assert not old_runner.inputs
    assert scheduler.submit_if_ready(context, event) is None
    assert bridge.event_loop.processed_event_count == 1
    assert provider.evidence()["actual_model_inference_verified"] is False


def test_scheduler_does_not_install_itself_or_replace_other_providers():
    from test_onboard_realtime_bridge import _bridge

    provider, _worker, _clock = _provider()
    bridge, old_runner, _candidates, _runtimes = _bridge()
    original = bridge.event_loop.adapter.executor.decision_provider
    with pytest.raises(OnboardVlmProviderError, match="this exact"):
        OnboardVlmReadyScheduler(bridge.event_loop, provider)
    assert bridge.event_loop.adapter.executor.decision_provider is original
    assert not old_runner.inputs


def test_worker_starting_queues_source_without_implicit_start_or_inference():
    provider, worker, _clock = _provider()
    source = _source()
    worker.state = "starting"
    assert provider.submit_source(*source[:3]) is None
    assert provider.state == "queued" and not worker.submissions
    worker.state = "idle"
    assert provider.advance_latest() == 1
    assert worker.submissions[0][0] is source[0]


def test_receipt_revision_regression_cannot_erase_inflight_newer_memory():
    provider, worker, clock = _provider()
    first = _source()
    provider.submit_source(*first[:3], receipt_revision=2)
    clock.at_s = 10.1
    with pytest.raises(OnboardVlmProviderError, match="receipt revision regressed"):
        provider.submit_source(*_source(10.1)[:3], receipt_revision=1)
    assert not worker.cancellations and len(worker.submissions) == 1


def test_cancel_queued_startup_retires_worker_not_just_source_queue():
    provider, worker, _clock = _provider()
    worker.state = "starting"
    provider.submit_source(*_source()[:3])
    provider.cancel("stop_during_prewarm")
    assert worker.cancellations == ["stop_during_prewarm"]
    assert provider.state == "idle"
    assert provider.advance_latest() is None


def test_clock_regression_after_arming_cannot_consume_future_completion():
    provider, worker, clock = _provider()
    _request, context, event, scope = _arm(provider, worker, clock)
    clock.at_s = 10.01
    with pytest.raises(OnboardVlmProviderError, match="predates armed answer completion"):
        provider.propose_for_session(context, event, scope)
    assert worker.take_calls == 0
    assert provider.evidence()["consumed_request_count"] == 0


def test_terminal_worker_retires_even_when_new_current_cannot_admit_old_source():
    provider, worker, clock = _provider()
    provider.submit_source(*_source()[:3])
    clock.at_s = 10.1
    newest = _source(10.1)
    provider.submit_source(*newest[:3])
    worker.state = "timed_out"
    assert not provider.ready_if_bound(newest[1], newest[2], newest[3])
    assert provider.state == "queued"
    worker.state = "idle"
    assert provider.advance_latest() == 2


def test_lost_ready_result_between_arm_and_take_never_yields_proposal():
    provider, worker, clock = _provider()
    _request, context, event, scope = _arm(provider, worker, clock)
    worker.result = None
    worker.state = "timed_out"
    with pytest.raises(OnboardVlmProviderError, match="no longer available"):
        provider.propose_for_session(context, event, scope)
    assert provider.evidence()["consumed_request_count"] == 0


@pytest.mark.parametrize("movement", [True, False])
def test_legacy_unmeasured_obstacle_sentinel_blocks_movement_but_not_hold(movement):
    provider, worker, clock = _provider()
    event = replace(_source()[2], obstacle_distance_m=5000.0)
    request, context, event, scope = _source(event=event)
    provider.submit_source(request, context, event)
    clock.at_s = 10.05
    output = (
        {
            "response": "replan",
            "reason": "fixture search",
            "confidence": 0.95,
            "task_action": "search",
            "flight_plan": {
                "action": "search",
                "north_offset_m": 0.0,
                "east_offset_m": -25.0,
                "altitude_m": 35.0,
                "speed_mps": None,
                "duration_s": None,
                "reference_id": None,
            },
        }
        if movement
        else None
    )
    worker.finish(request, clock, output)
    if movement:
        with pytest.raises(OnboardVlmProviderError, match="sentinel cannot admit movement"):
            provider.ready_if_bound(context, event, scope)
        assert worker.take_calls == 0
    else:
        assert provider.ready_if_bound(context, event, scope)
        assert provider.propose_for_session(context, event, scope).response is OnboardResponse.HOLD
