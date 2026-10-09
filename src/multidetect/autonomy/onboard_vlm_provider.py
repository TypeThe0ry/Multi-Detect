"""Source-bound, ready-only VLM handoff to the existing development executor.

Generation happens in an injected asynchronous worker, never under the Mission
event-loop lock. Pending is not a HOLD decision. This first handoff intentionally
requires the exact source context/event: source-versus-newer-execution-state
admission, arbitrary-Prompt task resolution and autonomous flight are not proved.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Protocol

from .contracts import ContextSnapshot
from .onboard_adapter import OnboardMissionEventLoop, OnboardMissionRevision
from .onboard_agent import OnboardFlightAction, OnboardModelProposal, OnboardRuntimeEvent
from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardMultimodalScope, Scope
from .onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    OnboardVlmRuntimeResponse,
    parse_onboard_vlm_runtime_response,
)
from .serialization import canonical_sha256, require_int, require_real, require_sha256, require_text


class OnboardVlmProviderError(ValueError):
    """A pending, retired or differently sourced answer cannot be a proposal."""


class VlmWorkerResult(Protocol):
    generation: int
    request_sha256: str
    response: OnboardVlmRuntimeResponse
    raw_output: str
    completed_at_s: float
    inference_latency_ms: float


class VlmProcessWorker(Protocol):
    """The worker owns processes/deadlines; all parent operations are bounded."""

    @property
    def state(self) -> str: ...

    def submit(self, request: OnboardVlmRuntimeRequest, *, observed_now_s: float) -> int: ...

    def poll(self) -> VlmWorkerResult | None: ...

    def take_result(self) -> VlmWorkerResult | None: ...

    def cancel(self, reason: str) -> None: ...


def _scope(scope: Scope) -> OnboardMultimodalScope:
    if isinstance(scope, OnboardInferenceScope):
        return OnboardMultimodalScope.from_inference_scope(scope)
    if isinstance(scope, OnboardMultimodalScope):
        return scope
    raise TypeError("scope must be a typed onboard inference/multimodal scope")


@dataclass(frozen=True, slots=True)
class _Source:
    request: OnboardVlmRuntimeRequest
    context: ContextSnapshot
    event: OnboardRuntimeEvent
    receipt_revision: int
    receipts_sha256: str
    context_sha256: str
    event_sha256: str
    scope_sha256: str

    @property
    def identity(self) -> tuple[str, str, str, str, int]:
        return (
            self.request.canonical_hash,
            self.context_sha256,
            self.event_sha256,
            self.scope_sha256,
            self.receipt_revision,
        )


@dataclass(frozen=True, slots=True)
class _Armed:
    source: _Source
    generation: int
    response: OnboardVlmRuntimeResponse
    raw_output: str


class OnboardVlmReadyDecisionProvider:
    """Async source submission plus exact-source, single-use ready proposals.

    Ordinary observations queue only the latest request without cancelling an
    in-flight inference. Scope or receipt revision changes retire it immediately.
    ``advance_latest`` starts a queued request only after the old one is retired;
    call it outside the executor. No provider method waits for generation.
    """

    def __init__(
        self,
        worker: VlmProcessWorker,
        *,
        model_id: str,
        model_version: str,
        model_sha256: str,
        provider: str = "development-vlm-ready-only",
        clock: Callable[[], float] = monotonic,
    ) -> None:
        for method in ("submit", "poll", "take_result", "cancel"):
            if not callable(getattr(worker, method, None)):
                raise TypeError(f"worker must expose {method}")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.worker = worker
        self.model_id = require_text(model_id, "model_id")
        self.model_version = require_text(model_version, "model_version")
        self.model_sha256 = require_sha256(model_sha256, "model_sha256")
        self.provider = require_text(provider, "provider")
        self._clock = clock
        self._lock = RLock()
        self._active: _Source | None = None
        self._generation: int | None = None
        self._queued: _Source | None = None
        self._armed: _Armed | None = None
        self._consumed: set[str] = set()
        self._audit: deque[dict[str, object]] = deque(maxlen=128)

    def _now(self) -> float:
        return require_real(self._clock(), "observed host clock", minimum=0)

    def _record(self, status: str, source: _Source | None, **fields: object) -> None:
        self._audit.append(
            {
                "status": status,
                "request_sha256": None if source is None else source.request.canonical_hash,
                "context_sha256": None if source is None else source.context_sha256,
                "event_sha256": None if source is None else source.event_sha256,
                "scope_sha256": None if source is None else source.scope_sha256,
                "receipt_revision": None if source is None else source.receipt_revision,
                "source_event_provided_to_model": source is not None
                and getattr(source.request, "source_event", None) is not None,
                "generation": self._generation,
                **fields,
            }
        )

    def _source(
        self,
        request: OnboardVlmRuntimeRequest,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        receipt_revision: int,
    ) -> _Source:
        if not isinstance(request, OnboardVlmRuntimeRequest):
            raise TypeError("request must be OnboardVlmRuntimeRequest")
        if not isinstance(context, ContextSnapshot) or not isinstance(event, OnboardRuntimeEvent):
            raise TypeError("source requires typed ContextSnapshot and OnboardRuntimeEvent")
        request.validate_binding()
        require_int(receipt_revision, "receipt_revision", minimum=0)
        scope = _scope(request.model_input.scope)
        scope.validate_context(context)
        if scope.vehicle_class is None:
            raise OnboardVlmProviderError("executor source requires known vehicle class")
        frame = request.model_input.frames[-1]
        if frame.context is None or frame.context.canonical_hash != context.canonical_hash:
            raise OnboardVlmProviderError(
                "current RGB must carry the exact provided source context"
            )
        if event.observed_at_s != context.captured_at_s:
            raise OnboardVlmProviderError(
                "source event and current context times must exactly agree"
            )
        source_event = request.source_event
        if source_event is None:
            raise OnboardVlmProviderError("provider requires a source event supplied to the model")
        if source_event.canonical_hash != event.canonical_hash:
            raise OnboardVlmProviderError(
                "request source event differs from submitted source event"
            )
        request.model_input.validate_freshness(self._now())
        receipts_hash = canonical_sha256(
            {
                "receipts": [item.to_mapping() for item in request.model_input.ledger.receipts],
                "reviewed_sources": list(request.host_reviewed_receipt_sources),
            }
        )
        return _Source(
            request,
            context,
            event,
            receipt_revision,
            receipts_hash,
            context.canonical_hash,
            event.canonical_hash,
            scope.canonical_hash,
        )

    def _start(self, source: _Source) -> int:
        source.request.validate_binding()
        now = self._now()
        source.request.model_input.validate_freshness(now)
        if source.request.canonical_hash in self._consumed:
            raise OnboardVlmProviderError("request has already been consumed")
        generation = require_int(
            self.worker.submit(source.request, observed_now_s=now), "worker generation", minimum=0
        )
        self._active, self._generation = source, generation
        self._armed = None
        self._record("submitted", source)
        return generation

    def _retire(self, reason: str, *, cancel: bool) -> None:
        source = self._active
        if cancel:
            self.worker.cancel(reason)
        self._record(reason, source)
        self._active, self._generation, self._armed = None, None, None

    def submit_source(
        self,
        request: OnboardVlmRuntimeRequest,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        receipt_revision: int = 0,
    ) -> int | None:
        """Return its worker generation, or None when only the latest is queued."""
        with self._lock:
            source = self._source(request, context, event, receipt_revision)
            if source.request.canonical_hash in self._consumed:
                raise OnboardVlmProviderError("request has already been consumed")
            active = self._active
            if active is None:
                self._queued = source
                if self.worker.state != "idle":
                    self._record("source_waits_for_explicit_prewarm", source)
                    return None
                generation = self._start(source)
                self._queued = None
                return generation
            if source.identity == active.identity:
                self._record("duplicate_submission", source)
                return self._generation
            if source.request.canonical_hash == active.request.canonical_hash:
                raise OnboardVlmProviderError("same request hash cannot acquire different source")
            if (
                _scope(source.request.model_input.scope) == _scope(active.request.model_input.scope)
                and source.receipt_revision < active.receipt_revision
            ):
                raise OnboardVlmProviderError("receipt revision regressed within source scope")
            if (
                _scope(source.request.model_input.scope) != _scope(active.request.model_input.scope)
                or source.receipt_revision != active.receipt_revision
            ):
                self._queued = None
                self._retire("scope_or_receipt_changed", cancel=True)
                # cancel retires an owned process. The caller must explicitly
                # prewarm before advance_latest; never auto-create a GPU worker.
                self._queued = source
                self._record("replacement_waits_for_explicit_prewarm", source)
                return None
            if source.receipts_sha256 != active.receipts_sha256:
                raise OnboardVlmProviderError("receipt facts changed without a receipt revision")
            newest = self._queued or active
            if source.context.captured_at_s < newest.context.captured_at_s:
                raise OnboardVlmProviderError("queued source observation time regressed")
            if source.event.event_id in {newest.event.event_id, active.event.event_id}:
                raise OnboardVlmProviderError("source event ID reused with different request")
            self._queued = source
            self._record("latest_queued_without_cancel", source)
            return None

    def advance_latest(self) -> int | None:
        """Submit the bounded latest queue outside the executor; never wait."""
        with self._lock:
            if self._active is not None:
                return None
            source = self._queued
            if source is None:
                return None
            generation = self._start(source)
            self._queued = None
            return generation

    def cancel(self, reason: str) -> None:
        with self._lock:
            self._queued = None
            self._retire(require_text(reason, "cancel reason"), cancel=True)

    def _matches(
        self,
        source: _Source,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        scope: Scope,
        receipt_revision: int,
    ) -> bool:
        if not isinstance(context, ContextSnapshot) or not isinstance(event, OnboardRuntimeEvent):
            raise TypeError("current requires typed context/event")
        require_int(receipt_revision, "receipt_revision", minimum=0)
        return (
            context.canonical_hash == source.context_sha256
            and event.canonical_hash == source.event_sha256
            and _scope(scope).canonical_hash == source.scope_sha256
            and receipt_revision == source.receipt_revision
        )

    def _validated_result(
        self, result: VlmWorkerResult, source: _Source
    ) -> OnboardVlmRuntimeResponse:
        if (
            type(result.generation) is not int
            or result.generation != self._generation
            or result.request_sha256 != source.request.canonical_hash
        ):
            raise OnboardVlmProviderError("worker result generation/request differs from source")
        if not isinstance(result.response, OnboardVlmRuntimeResponse):
            raise OnboardVlmProviderError("worker result must contain typed runtime response")
        if not isinstance(result.raw_output, str):
            raise OnboardVlmProviderError("worker raw output must be original JSON text")
        now = self._now()
        completed = require_real(result.completed_at_s, "completed_at_s", maximum=now)
        parsed = parse_onboard_vlm_runtime_response(
            result.raw_output,
            source.request,
            current_scope=source.request.model_input.scope,
            completed_at_s=completed,
            inference_latency_ms=result.inference_latency_ms,
        )
        source.request.model_input.validate_freshness(now)
        if parsed != result.response:
            raise OnboardVlmProviderError("worker typed response differs from exact raw parsing")
        plan = parsed.output.flight_plan
        if (
            source.event.obstacle_distance_m == 5000.0
            and plan is not None
            and plan.action
            in {
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            }
        ):
            raise OnboardVlmProviderError("legacy 5000 obstacle sentinel cannot admit movement")
        return parsed

    def ready_if_bound(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        scope: Scope,
        receipt_revision: int = 0,
    ) -> bool:
        """Arm only a finished, fresh answer for the exact source; mismatch is False."""
        with self._lock:
            source = self._active
            if source is None:
                return False
            if self.worker.state in {"failed", "timed_out", "cancelled", "closed"}:
                self._retire(f"worker_{self.worker.state}", cancel=False)
                return False
            if not self._matches(source, context, event, scope, receipt_revision):
                self._record("current_source_mismatch_not_admitted", source)
                return False
            try:
                source.request.validate_binding()
                source.request.model_input.validate_freshness(self._now())
                result = self.worker.poll()
                if result is None:
                    if self.worker.state in {"failed", "timed_out", "cancelled", "closed"}:
                        self._retire(f"worker_{self.worker.state}", cancel=False)
                    return False
                self._validated_result(result, source)
                self._armed = _Armed(source, result.generation, result.response, result.raw_output)
                self._record("ready_armed", source)
                return True
            except Exception:
                self._retire("ready_validation_failed", cancel=True)
                raise

    def propose_for_session(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        scope: OnboardInferenceScope,
    ) -> OnboardModelProposal:
        """Consume an armed exact-source answer only; no poll/submit/generation here."""
        with self._lock:
            armed = self._armed
            if armed is None or not self._matches(
                armed.source, context, event, scope, armed.source.receipt_revision
            ):
                raise OnboardVlmProviderError(
                    "no armed answer bound to current context/event/scope"
                )
            try:
                now = self._now()
                armed.source.request.model_input.validate_freshness(now)
                if armed.response.completed_at_s > now:
                    raise OnboardVlmProviderError("host clock predates armed answer completion")
                result = self.worker.take_result()
                if result is None:
                    raise OnboardVlmProviderError("armed worker result is no longer available")
                if (
                    type(result.generation) is not int
                    or result.generation != armed.generation
                    or result.request_sha256 != armed.source.request.canonical_hash
                    or result.response != armed.response
                    or result.raw_output != armed.raw_output
                    or result.completed_at_s != armed.response.completed_at_s
                    or result.inference_latency_ms != armed.response.inference_latency_ms
                ):
                    raise OnboardVlmProviderError("armed answer changed before single consumption")
                if len(self._consumed) >= 4096:
                    raise OnboardVlmProviderError("development consumption registry is full")
                self._consumed.add(armed.source.request.canonical_hash)
                output = armed.response.output
                proposal = OnboardModelProposal(
                    output.response,
                    output.reason,
                    output.confidence,
                    self.model_id,
                    self.model_version,
                    self.model_sha256,
                    self.provider,
                    armed.response.inference_latency_ms,
                    output.flight_plan,
                    output.task_action,
                )
                self._record(
                    "consumed",
                    armed.source,
                    raw_response_sha256=armed.response.raw_response_sha256,
                )
                self._active, self._generation, self._armed = None, None, None
                return proposal
            except Exception:
                self._retire("consume_validation_failed", cancel=True)
                raise

    def propose(self, context: ContextSnapshot, event: OnboardRuntimeEvent) -> OnboardModelProposal:
        raise OnboardVlmProviderError("VLM ready-only provider requires explicit session scope")

    @property
    def state(self) -> str:
        with self._lock:
            if self._armed is not None:
                return "armed"
            if self._active is not None:
                return self.worker.state
            return "queued" if self._queued is not None else "idle"

    def evidence(self) -> dict[str, object]:
        with self._lock:
            return {
                "contract": "multidetect.onboard-vlm-ready-provider-development-v1",
                "state": self.state,
                "active_request_sha256": None
                if self._active is None
                else self._active.request.canonical_hash,
                "queued_request_sha256": None
                if self._queued is None
                else self._queued.request.canonical_hash,
                "consumed_request_count": len(self._consumed),
                "audit": [dict(item) for item in self._audit],
                "source_vs_current_async_admission": False,
                "arbitrary_prompt_resolution_verified": False,
                "execution_authority": False,
                "actual_model_inference_verified": False,
                "autonomous_flight_verified": False,
                "hardware_acceptance": False,
                "candidate_promoted": False,
            }


class OnboardVlmReadyScheduler:
    """Do not touch the Mission adapter while an exact-source answer is pending."""

    def __init__(
        self,
        event_loop: OnboardMissionEventLoop,
        provider: OnboardVlmReadyDecisionProvider,
    ) -> None:
        if not isinstance(event_loop, OnboardMissionEventLoop):
            raise TypeError("event_loop must be the existing OnboardMissionEventLoop")
        if not isinstance(provider, OnboardVlmReadyDecisionProvider):
            raise TypeError("provider must be OnboardVlmReadyDecisionProvider")
        if event_loop.adapter.executor.decision_provider is not provider:
            raise OnboardVlmProviderError("event loop must use this exact ready-only provider")
        self.event_loop, self.provider = event_loop, provider

    def submit_if_ready(
        self,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        *,
        receipt_revision: int = 0,
        mission_id: str | None = None,
    ) -> OnboardMissionRevision | None:
        scope = self.event_loop.adapter.executor.model_session_scope(context)
        if not self.provider.ready_if_bound(context, event, scope, receipt_revision):
            return None
        return self.event_loop.submit(context, event, mission_id=mission_id)
