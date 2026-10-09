"""Ready-only development seam for source-bound intent at a newer observation.

This module does not acquire images, qualify route sensors or own PX4 authority.
Worker operations and the first canonical compilation happen outside the existing
Mission loop lock. Commit rechecks the actual adapter and both immutable snapshots;
pending is not a fabricated HOLD. The selected registered task stays fixed in this
first version, so this is not an arbitrary-Prompt task resolver.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from time import monotonic

from .contracts import ContextSnapshot, ResolvedTaskSpec
from .onboard_adapter import OnboardMissionEventLoop, OnboardMissionRevision
from .onboard_agent import OnboardModelProposal, OnboardRuntimeEvent
from .onboard_history import OnboardInferenceScope
from .onboard_multimodal import OnboardMultimodalScope, Scope
from .onboard_vlm_admission import (
    ExecutionAdmission,
    ExecutionAdmissionPolicy,
    ExecutionSnapshot,
    RouteClearanceEvidence,
    SourceAnchoredIntent,
    admit_anchored_intent,
)
from .onboard_vlm_provider import VlmProcessWorker
from .onboard_vlm_runtime_contract import OnboardVlmRuntimeRequest
from .onboard_vlm_worker import OnboardVlmWorkerResult
from .prompt_resolution import _thaw_json, runtime_sha256
from .serialization import (
    canonical_json,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
)


class OnboardVlmExecutionError(ValueError):
    """A stale, pending, retired or differently bound result cannot execute."""


class OnboardVlmExecutionDeferred(BaseException):
    """Host-only cancellation of an uncommitted attempt, not a model fault.

    The legacy executor converts ordinary Exceptions into a safety decision.
    This dedicated availability signal must instead unwind before any decision
    or Mission state is committed; the scheduler catches this exact type.
    """


_DEFAULT_POLICY = ExecutionAdmissionPolicy()


def _scope(value: Scope) -> OnboardMultimodalScope:
    if isinstance(value, OnboardInferenceScope):
        return OnboardMultimodalScope.from_inference_scope(value)
    if isinstance(value, OnboardMultimodalScope):
        return value
    raise TypeError("scope must be a typed onboard scope")


def _identity(snapshot: ExecutionSnapshot) -> tuple[str, str, int, int, str]:
    return (
        _scope(snapshot.scope).canonical_hash,
        snapshot.mission_sha256,
        snapshot.mission_revision,
        snapshot.receipt_revision,
        snapshot.policy_artifact_sha256,
    )


def capture_execution_snapshot(
    event_loop: OnboardMissionEventLoop,
    context: ContextSnapshot,
    event: OnboardRuntimeEvent,
    *,
    clearance: RouteClearanceEvidence | None = None,
) -> ExecutionSnapshot:
    """Read actual adapter identity, not the loop's possibly stale last revision.

    Callers provide the original contemporaneous observation/event and, if known,
    independently qualified clearance. No timestamp or sensor fact is invented.
    Receipt revision is the task-state receipt count, not a collector revision.
    """
    if not isinstance(event_loop, OnboardMissionEventLoop):
        raise TypeError("event_loop must be OnboardMissionEventLoop")
    with event_loop._lock:
        adapter = event_loop.adapter
        if (
            event_loop.closed
            or not adapter.prepared
            or adapter.faulted
            or adapter.safety_engaged
            or adapter.current_mission is None
            or not adapter.history
        ):
            raise OnboardVlmExecutionError("adapter is not an open prepared execution session")
        shared_policy = adapter.executor.shared_policy
        if shared_policy.artifact.ranker_sha256 != shared_policy.ranker.canonical_hash:
            raise OnboardVlmExecutionError(
                "shared policy artifact no longer binds the actual ranker"
            )
        return ExecutionSnapshot(
            context,
            event,
            adapter.executor.model_session_scope(context),
            adapter.current_mission.canonical_hash,
            adapter.history[-1].sequence,
            len(adapter.task_state.receipts),
            clearance,
            policy_artifact_sha256=shared_policy.artifact_sha256,
        )


@dataclass(frozen=True, slots=True)
class _Submission:
    request: OnboardVlmRuntimeRequest
    source: ExecutionSnapshot
    task: ResolvedTaskSpec
    runtime_json: str


class OnboardVlmExecutionProvider:
    """Async inference plus an armed, already-admitted single-use proposal.

    A completed answer remains anchored at its original state. Ordinary newer
    frames disarm only the current admission, not the worker or the anchor.
    Scope/Mission/task-receipt changes retire it. Explicit worker prewarming is
    owned by the caller; cancellation never silently starts another GPU process.
    """

    def __init__(
        self,
        worker: VlmProcessWorker,
        *,
        model_id: str,
        model_version: str,
        model_sha256: str,
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
        self._model_identity = (self.model_id, self.model_version, self.model_sha256)
        self._clock = clock
        self._lock = RLock()
        self._active: _Submission | None = None
        self._generation: int | None = None
        self._queued: _Submission | None = None
        self._anchor: SourceAnchoredIntent | None = None
        self._armed: ExecutionAdmission | None = None
        self._armed_binding_sha256: str | None = None
        self._retired: set[str] = set()
        self._received: set[tuple[str, int]] = set()
        self._audit: deque[dict[str, object]] = deque(maxlen=128)

    def now(self) -> float:
        return require_real(self._clock(), "observed host clock", minimum=0)

    def _verify_model_identity(self) -> None:
        if (self.model_id, self.model_version, self.model_sha256) != self._model_identity:
            raise OnboardVlmExecutionError(
                "host model identity changed during this provider session"
            )

    def _record(self, status: str, **fields: object) -> None:
        self._audit.append({"status": status, **fields})

    def _tombstone(self, request_sha256: str) -> None:
        if request_sha256 not in self._retired and len(self._retired) >= 4096:
            raise OnboardVlmExecutionError(
                "session consumption registry is full; new session required"
            )
        self._retired.add(request_sha256)

    def _retire_locked(self, reason: str) -> bool:
        had_active = self._active is not None
        for source in (self._active, self._queued):
            if source is not None:
                self._tombstone(source.request.canonical_hash)
        if self._anchor is not None:
            self._tombstone(self._anchor.request.canonical_hash)
        self._record("retired", reason=reason)
        self._active = self._queued = None
        self._generation = None
        self._anchor = self._armed = None
        return had_active

    def retire(self, reason: str) -> None:
        """Call outside the Mission loop lock: cancellation can retire a process."""
        reason = require_text(reason, "retirement reason")
        with self._lock:
            cancel = self._retire_locked(reason)
        if cancel:
            self.worker.cancel(reason)

    def observe_snapshot(self, current: ExecutionSnapshot) -> None:
        if not isinstance(current, ExecutionSnapshot):
            raise TypeError("current must be ExecutionSnapshot")
        with self._lock:
            previous = (
                self._active.source
                if self._active
                else (
                    self._anchor.source
                    if self._anchor
                    else (self._queued.source if self._queued else None)
                )
            )
            cancel = previous is not None and _identity(previous) != _identity(current)
            if cancel:
                cancel = self._retire_locked("scope/Mission/receipt changed")
            elif (
                self._armed is not None
                and self._armed.current.canonical_hash != current.canonical_hash
            ):
                self._armed = None
                self._record("disarmed_new_observation", execution_sha256=current.canonical_hash)
        if cancel:
            self.worker.cancel("scope/Mission/receipt changed")

    def _submission(self, request, source, task, runtime_json) -> _Submission:
        self._verify_model_identity()
        if not isinstance(request, OnboardVlmRuntimeRequest) or not isinstance(
            source, ExecutionSnapshot
        ):
            raise TypeError("submission requires typed request and source snapshot")
        if not isinstance(task, ResolvedTaskSpec):
            raise TypeError("task must be ResolvedTaskSpec")
        request.validate_binding()
        request.model_input.validate_freshness(self.now())
        frame = request.model_input.frames[-1]
        if (
            frame.context is None
            or frame.context.canonical_hash != source.context.canonical_hash
            or _scope(request.model_input.scope) != _scope(source.scope)
            or request.source_event is None
            or request.source_event.canonical_hash != source.event.canonical_hash
            or _scope(source.scope).selected_task_id != task.task_id
        ):
            raise OnboardVlmExecutionError(
                "request differs from real source context/event/scope/task"
            )
        value = strict_json_loads(runtime_json, name="source runtime")
        if not isinstance(value, dict):
            raise OnboardVlmExecutionError("runtime must be a JSON object")
        return _Submission(request, source, task, canonical_json(value))

    def submit_source(self, request, source, task, runtime_json) -> int | None:
        """Submit outside the core lock, or keep only the latest ordinary frame."""
        submission = self._submission(request, source, task, runtime_json)
        with self._lock:
            if request.canonical_hash in self._retired:
                raise OnboardVlmExecutionError("source is permanently retired/consumed")
            previous = self._active if self._active else None
            if previous is None and self._anchor is not None:
                previous = _Submission(
                    self._anchor.request,
                    self._anchor.source,
                    self._anchor.task,
                    self._anchor.runtime_json,
                )
            changed = previous is not None and _identity(previous.source) != _identity(source)
            cancel = self._retire_locked("new source identity") if changed else False
            if previous is not None and not changed:
                if previous.request.canonical_hash == request.canonical_hash:
                    return self._generation if self._active else self._anchor.expected_generation
                latest = self._queued.source if self._queued is not None else previous.source
                if source.context.captured_at_s < latest.context.captured_at_s:
                    raise OnboardVlmExecutionError("queued source observation timestamp regressed")
                if self._queued is not None:
                    if self._queued.request.canonical_hash == request.canonical_hash:
                        return None
                    self._tombstone(self._queued.request.canonical_hash)
                self._queued = submission
                self._record("queued_latest", request_sha256=request.canonical_hash)
                return None
            # A retired process must be explicitly prewarmed by the outer owner.
            if cancel or self.worker.state != "idle":
                self._queued = submission
        if cancel:
            self.worker.cancel("new source identity")
        if cancel or self.worker.state != "idle":
            return None
        return self._start(submission)

    def _start(self, submission: _Submission) -> int:
        with self._lock:
            if submission.request.canonical_hash in self._retired:
                raise OnboardVlmExecutionError("source was retired before worker submit")
            if self._active is not None or self._anchor is not None:
                raise OnboardVlmExecutionError("another source is already active")
            if (
                self._queued is not None
                and self._queued.request.canonical_hash != submission.request.canonical_hash
            ):
                self._tombstone(self._queued.request.canonical_hash)
            self._active, self._generation = submission, None
            self._queued = None
        try:
            generation = self.worker.submit(submission.request, observed_now_s=self.now())
            generation = require_int(generation, "worker generation", minimum=1)
        except Exception:
            self.retire("worker submission failed")
            raise
        with self._lock:
            if self._active is not submission or submission.request.canonical_hash in self._retired:
                raise OnboardVlmExecutionError("submission retired during worker submit")
            self._generation = generation
            self._record(
                "submitted",
                request_sha256=submission.request.canonical_hash,
                source_snapshot_sha256=submission.source.canonical_hash,
                generation=generation,
            )
        return generation

    def advance_latest(self) -> int | None:
        """After explicit prewarming, submit the queued latest request, no wait."""
        with self._lock:
            queued = self._queued
            if self._active is not None or self._anchor is not None or queued is None:
                return None
        if self.worker.state != "idle":
            return None
        queued.request.model_input.validate_freshness(self.now())
        return self._start(queued)

    def poll_source(self) -> SourceAnchoredIntent | None:
        """Take and reparse the exact result outside the Mission loop lock."""
        with self._lock:
            if self._anchor is not None:
                return self._anchor
            active, generation = self._active, self._generation
        if active is None or generation is None:
            return None
        try:
            self._verify_model_identity()
        except OnboardVlmExecutionError:
            self.retire("host model identity changed")
            raise
        result = self.worker.poll()
        if result is None:
            if self.worker.state in {"failed", "timed_out", "cancelled", "closed"}:
                self.retire(f"worker {self.worker.state}")
            return None
        try:
            if not isinstance(result, OnboardVlmWorkerResult):
                raise TypeError("worker must return the frozen typed result")
            anchor = SourceAnchoredIntent(
                active.request,
                result,
                active.source,
                active.task,
                active.runtime_json,
                self.model_id,
                self.model_version,
                self.model_sha256,
                generation,
            )
            taken = self.worker.take_result()
            if taken != result:
                raise OnboardVlmExecutionError("worker result changed between poll and single take")
            with self._lock:
                key = (active.request.canonical_hash, generation)
                if self._active is not active or key in self._received or key[0] in self._retired:
                    raise OnboardVlmExecutionError("late or already consumed worker result")
                self._received.add(key)
                self._active, self._generation = None, None
                self._anchor = anchor
                self._record("source_ready", source_anchor_sha256=anchor.canonical_hash)
            return anchor
        except Exception:
            self.retire("invalid/late worker result")
            raise

    def prepare_admission(self, current, *, now_s, mission_id, policy) -> ExecutionAdmission | None:
        with self._lock:
            anchor = self._anchor
        if anchor is None:
            return None
        if self.expire_ready_source(now_s=now_s, policy=policy):
            raise OnboardVlmExecutionError("source inference deadline/history expired")
        return admit_anchored_intent(
            anchor, current, now_s=now_s, mission_id=mission_id, policy=policy
        )

    def expire_ready_source(self, *, now_s: float, policy: ExecutionAdmissionPolicy) -> bool:
        """Tombstone true ready expiry, preserving a valid fresh latest request.

        This operation never cancels or prewarms a worker: the completed result
        was already taken. Temporary current-state/clearance failures are not
        source expiry. A caller must explicitly advance the surviving queue.
        """
        require_real(now_s, "observed host clock", minimum=0)
        if not isinstance(policy, ExecutionAdmissionPolicy):
            raise TypeError("policy must be ExecutionAdmissionPolicy")
        with self._lock:
            anchor = self._anchor
            if anchor is None:
                return False
            model_input = anchor.request.model_input
            expired = (
                now_s > model_input.deadline_at_s
                or any(
                    now_s - frame.captured_at_s > model_input.max_frame_age_s
                    for frame in model_input.frames
                )
                or now_s - anchor.source.context.captured_at_s > policy.max_source_age_s
            )
            if not expired:
                return False
            self._tombstone(anchor.request.canonical_hash)
            self._anchor = self._armed = None
            queued = self._queued
            if queued is not None:
                try:
                    queued.request.validate_binding()
                    queued.request.model_input.validate_freshness(now_s)
                    if (
                        _identity(queued.source) != _identity(anchor.source)
                        or now_s - queued.source.context.captured_at_s > policy.max_source_age_s
                    ):
                        raise OnboardVlmExecutionError("latest queue changed source identity")
                except (ValueError, TypeError):
                    self._tombstone(queued.request.canonical_hash)
                    self._queued = None
            self._record(
                "source_expired",
                request_sha256=anchor.request.canonical_hash,
                source_snapshot_sha256=anchor.source.canonical_hash,
                kept_latest_request_sha256=(
                    None if self._queued is None else self._queued.request.canonical_hash
                ),
            )
            return True

    def arm(self, admission: ExecutionAdmission) -> None:
        self._verify_model_identity()
        if not isinstance(admission, ExecutionAdmission):
            raise TypeError("admission must be ExecutionAdmission")
        # Public callers cannot arm a dataclass replacement containing an
        # uncompiled plan. This is bounded CPU verification, never inference.
        admission.validate_binding(now_s=self.now())
        with self._lock:
            if (
                self._anchor is not admission.anchor
                or admission.anchor.request.canonical_hash in self._retired
            ):
                raise OnboardVlmExecutionError("only the current unconsumed anchor can be armed")
            self._armed = admission
            self._armed_binding_sha256 = admission.canonical_hash

    def propose_for_session(
        self, context: ContextSnapshot, event: OnboardRuntimeEvent, scope: Scope
    ) -> OnboardModelProposal:
        """Pure ready consumption: no worker, waiting or compilation in executor."""
        with self._lock:
            self._verify_model_identity()
            admission = self._armed
            if admission is None:
                raise OnboardVlmExecutionDeferred("no armed admission is currently available")
            if (
                context.canonical_hash != admission.current.context.canonical_hash
                or event.canonical_hash != admission.current.event.canonical_hash
                or _scope(scope) != _scope(admission.current.scope)
            ):
                raise OnboardVlmExecutionError("no armed admission for exact execution snapshot")
            now = self.now()
            # Invalid immutable bytes/identity remain ordinary faults. Only
            # host-owned availability/time transitions use the neutral signal.
            admission.anchor.request.validate_binding()
            result = admission.anchor.result
            if (
                admission.canonical_hash != self._armed_binding_sha256
                or result.generation != admission.anchor.expected_generation
                or result.request_sha256 != admission.anchor.request.canonical_hash
                or hashlib.sha256(result.raw_output.encode("utf-8")).hexdigest()
                != result.response.raw_response_sha256
            ):
                raise OnboardVlmExecutionError("armed original result/admission binding changed")
            if self.expire_ready_source(now_s=now, policy=admission.policy):
                raise OnboardVlmExecutionDeferred("source inference deadline/history expired")
            try:
                admission.anchor.request.model_input.validate_freshness(now)
                if not (
                    admission.anchor.result.completed_at_s <= now
                    and admission.admitted_at_s <= now
                    and admission.current.context.captured_at_s <= now
                    and 0
                    <= now - admission.anchor.source.context.captured_at_s
                    <= admission.policy.max_source_age_s
                ):
                    raise ValueError("armed admission clock regressed")
            except ValueError as error:
                self._retire_locked("armed admission clock regressed")
                raise OnboardVlmExecutionDeferred(str(error)) from error
            clearance = admission.current.clearance
            if (
                now - admission.current.context.captured_at_s > admission.policy.max_current_age_s
                or clearance is None
                or not 0 <= now - clearance.observed_at_s <= admission.policy.max_current_age_s
            ):
                self._armed = None
                self._record("disarmed_execution_facts_expired")
                raise OnboardVlmExecutionDeferred("execution context/clearance expired or unknown")
            proposal = admission.proposal()
            self._tombstone(admission.anchor.request.canonical_hash)
            self._record(
                "consumed",
                admission_sha256=admission.canonical_hash,
                source_snapshot_sha256=admission.anchor.source.canonical_hash,
                execution_snapshot_sha256=admission.current.canonical_hash,
            )
            self._anchor = self._armed = None
            return proposal

    def propose(self, context, event):
        raise OnboardVlmExecutionError("session scope and armed admission are required")

    def evidence(self) -> dict[str, object]:
        with self._lock:
            return {
                "contract": "multidetect.onboard-vlm-execution-scheduler-development-v1",
                "pending": self._active is not None,
                "source_ready": self._anchor is not None,
                "armed": self._armed is not None,
                "queued_latest": self._queued is not None,
                "retired_count": len(self._retired),
                "audit": list(self._audit),
                "source_timestamps_rewritten": False,
                "model_saw_execution_context": False,
                "route_clearance_producer_implemented": False,
                "collector_receipt_join_implemented": False,
                "arbitrary_prompt_resolution_verified": False,
                "actual_inference_verified": False,
                "autonomous_flight_verified": False,
                "execution_authority": False,
                "candidate_promoted": False,
            }


class OnboardVlmExecutionScheduler:
    """CAS the latest owned observation and actual adapter before old dispatch.

    All ingress owners must serialize adapter mutations through this loop. Direct
    adapter mutations are detected by actual Mission/history/receipt comparisons,
    not mistaken for a still-current wrapper head. Old transport callbacks retain
    their existing ownership and latency; this scheduler adds no GPU wait there.
    """

    def __init__(
        self,
        event_loop: OnboardMissionEventLoop,
        provider: OnboardVlmExecutionProvider,
        *,
        policy: ExecutionAdmissionPolicy = _DEFAULT_POLICY,
    ) -> None:
        if not isinstance(event_loop, OnboardMissionEventLoop) or not isinstance(
            provider, OnboardVlmExecutionProvider
        ):
            raise TypeError("scheduler requires the typed loop and execution provider")
        if not isinstance(policy, ExecutionAdmissionPolicy):
            raise TypeError("policy must be ExecutionAdmissionPolicy")
        self.event_loop, self.provider, self.policy = event_loop, provider, policy
        self._head: ExecutionSnapshot | None = None
        self._head_version = 0
        self.last_execution_admission: ExecutionAdmission | None = None

    def _actual_matches(self, snapshot: ExecutionSnapshot) -> None:
        actual = capture_execution_snapshot(
            self.event_loop, snapshot.context, snapshot.event, clearance=snapshot.clearance
        )
        if actual.canonical_hash != snapshot.canonical_hash:
            raise OnboardVlmExecutionError("actual adapter Mission/scope/receipt revision changed")
        if self.event_loop.adapter.executor.decision_provider is not self.provider:
            raise OnboardVlmExecutionError(
                "execution provider is not installed in existing executor"
            )

    def _task_runtime(self, snapshot: ExecutionSnapshot):
        adapter = self.event_loop.adapter
        candidates, runtimes = adapter._candidates, adapter._runtimes
        selected = _scope(snapshot.scope).selected_task_id
        if candidates is None or len(candidates) != 1 or candidates[0].task.task_id != selected:
            raise OnboardVlmExecutionError("first version requires one unchanged registered task")
        if runtimes is None or set(runtimes) != {selected}:
            raise OnboardVlmExecutionError("first version requires only that task's runtime")
        resolution = adapter.executor._prompt_resolution
        if resolution is not None and (
            resolution.prompt_sha256 != adapter.executor.prompt.canonical_hash
            or len(resolution.candidates) != 1
            or resolution.candidates[0].task.canonical_hash != candidates[0].task.canonical_hash
            or set(resolution.runtimes) != {selected}
            or runtime_sha256(resolution.runtimes[selected]) != runtime_sha256(runtimes[selected])
            or resolution.runtime_hashes[selected] != runtime_sha256(resolution.runtimes[selected])
        ):
            raise OnboardVlmExecutionError(
                "registered Prompt resolution could change selected task/runtime"
            )
        return candidates[0].task, canonical_json(_thaw_json(runtimes[selected]))

    def update_current(self, current: ExecutionSnapshot) -> None:
        if not isinstance(current, ExecutionSnapshot):
            raise TypeError("current must be ExecutionSnapshot")
        with self.event_loop._lock:
            self._actual_matches(current)
            if (
                self._head is not None
                and _identity(self._head) == _identity(current)
                and current.context.captured_at_s < self._head.context.captured_at_s
            ):
                raise OnboardVlmExecutionError("current observation timestamp regressed")
            self._head, self._head_version = current, self._head_version + 1
        self.provider.observe_snapshot(current)

    def submit_source(
        self, request: OnboardVlmRuntimeRequest, source: ExecutionSnapshot
    ) -> int | None:
        with self.event_loop._lock:
            self._actual_matches(source)
            task, runtime = self._task_runtime(source)
        return self.provider.submit_source(request, source, task, runtime)

    def submit_if_ready(self, *, mission_id: str) -> OnboardMissionRevision | None:
        """Return a committed revision, or None for pending/neutral unavailability.

        The host-only BaseException signal is internal cancellation plumbing
        across the frozen executor; callers never need to catch it. Ordinary
        binding/backend faults retain their existing exception/safety behavior.
        """
        mission_id = require_text(mission_id, "mission_id")
        try:
            with self.event_loop._lock:
                current, version = self._head, self._head_version
                if current is None:
                    return None
                self._actual_matches(current)
                task, runtime = self._task_runtime(current)
                policy = self.policy
        except Exception:
            self.provider.retire("actual execution session changed before admission")
            raise
        self.provider.poll_source()
        expected = self.provider.prepare_admission(
            current, now_s=self.provider.now(), mission_id=mission_id, policy=policy
        )
        if expected is None:
            return None
        failure = False
        deferred = False
        try:
            with self.event_loop._lock:
                if self._head_version != version or self._head is not current:
                    return None  # New ordinary observation: re-admit next turn, no stale submit.
                self._actual_matches(current)
                actual_task, actual_runtime = self._task_runtime(current)
                if (
                    actual_task.canonical_hash != task.canonical_hash
                    or actual_runtime != runtime
                    or self.policy != policy
                ):
                    raise OnboardVlmExecutionError(
                        "task/runtime/admission policy changed during preparation"
                    )
                if (
                    task.canonical_hash != expected.anchor.task.canonical_hash
                    or runtime != expected.anchor.runtime_json
                ):
                    raise OnboardVlmExecutionError(
                        "source task/runtime differs from actual adapter"
                    )
                checked = admit_anchored_intent(
                    expected.anchor,
                    current,
                    now_s=self.provider.now(),
                    mission_id=mission_id,
                    policy=policy,
                )
                if (
                    checked.effective_plan.canonical_hash != expected.effective_plan.canonical_hash
                    or checked.compilation.plan.canonical_hash
                    != expected.compilation.plan.canonical_hash
                    or checked.compilation.wire_hash != expected.compilation.wire_hash
                    or checked.compilation.executable_mission_hash
                    != expected.compilation.executable_mission_hash
                ):
                    raise OnboardVlmExecutionError(
                        "locked revalidation differs from expected canonical compilation"
                    )
                self.provider.arm(checked)
                revision = self.event_loop.submit(
                    current.context, current.event, mission_id=mission_id
                )
                if (
                    not revision.flight_plan_dispatched
                    or revision.mission is None
                    or revision.mission.canonical_hash
                    != checked.compilation.executable_mission_hash
                    or self.event_loop.adapter.executor.active_task_id != task.task_id
                ):
                    raise OnboardVlmExecutionError(
                        "existing executor did not dispatch the exact admitted Mission"
                    )
                self._head = capture_execution_snapshot(
                    self.event_loop, current.context, current.event, clearance=current.clearance
                )
                self._head_version += 1
                self.last_execution_admission = checked
                return revision
        except OnboardVlmExecutionDeferred:
            failure = True
            deferred = True
            return None
        except Exception:
            failure = True
            raise
        finally:
            if failure and not deferred:
                if not self.provider.expire_ready_source(now_s=self.provider.now(), policy=policy):
                    self.provider.retire("execution commit failed; source cannot resurrect")
            elif self.last_execution_admission is not None and self._head is not current:
                # This runs after releasing the core lock; a queued old-Mission
                # observation cannot revive after the successful Mission change.
                self.provider.observe_snapshot(self._head)
