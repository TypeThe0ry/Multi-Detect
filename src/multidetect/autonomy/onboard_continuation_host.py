"""One original-parent continuation, orchestrated through measured wire phases.

The host supplies fresh receiver/context/route evidence and an independently
authorized, persistent child transaction. This module creates neither trust
nor grants, containers, sockets, arming or fallback authority. It is deliberately
not a repeated-parent chain, aircraft watchdog, or whole-sortie dispatcher.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, is_dataclass
from hashlib import sha256
from pathlib import Path, PosixPath, PurePath, PurePosixPath, PureWindowsPath, WindowsPath
from threading import RLock

from .. import px4_mission
from ..mission_journal import PersistentPx4MissionTransaction
from ..mission_planning import parse_mission_compile_runtime
from ..px4_continuation_wire import ContinuationWireState, Px4ContinuationWireSession
from ..px4_mission_feedback import PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
from . import (
    contracts,
    onboard_agent,
    onboard_continuation,
    onboard_continuation_admission,
    onboard_continuation_binding,
    onboard_evidence_collector,
    onboard_history,
    onboard_multimodal,
    onboard_vlm_admission,
    onboard_vlm_runtime_contract,
    onboard_vlm_worker,
    planner,
)
from .onboard_continuation_admission import OnboardContinuationAdmission
from .onboard_continuation_paused_admission import (
    ContinuationPauseAttempt,
    PausedContinuationAdmission,
    begin_pause_attempt,
)
from .serialization import (
    canonical_json,
    canonical_sha256,
    require_real,
    require_text,
    strict_json_loads,
)


class ContinuationHostError(RuntimeError):
    """A host dependency is missing, mismatched or no longer usable."""


# The audited stored-field graph is deliberately finite. A new runner/receipt
# type needs an explicit review, not duck typing through an IO-bearing getter.
_IDENTITY_DATACLASSES = frozenset(
    (
        contracts.ContextSnapshot,
        contracts.ExecutableMission,
        contracts.GeoPoint,
        contracts.MissionItem,
        contracts.PlanStep,
        contracts.PlatformNeutralPlan,
        contracts.PromptEnvelope,
        contracts.ResolvedTaskSpec,
        contracts.TaskConstraints,
        contracts.TaskGeometry,
        onboard_agent.OnboardFlightPlan,
        onboard_agent.OnboardModelOutput,
        onboard_agent.OnboardRuntimeEvent,
        onboard_continuation.ContinuationBudget,
        onboard_continuation.ContinuationCompilation,
        onboard_continuation.ContinuationCompilePolicy,
        onboard_continuation.ContinuationParentBinding,
        onboard_continuation_admission.ContinuationAuthoringEvidence,
        onboard_continuation_admission.OnboardContinuationAdmission,
        onboard_continuation_binding.ContinuationParentEvidence,
        onboard_continuation_binding.ContinuationReceivedFact,
        onboard_evidence_collector.OnboardMissionTelemetryBinding,
        onboard_evidence_collector.OnboardTelemetryObservation,
        onboard_history.OnboardInferenceScope,
        onboard_multimodal.MissionEvidenceLedger,
        onboard_multimodal.OnboardImageFrame,
        onboard_multimodal.OnboardMultimodalInput,
        onboard_multimodal.OnboardEvidenceReceipt,
        onboard_multimodal.OnboardMultimodalScope,
        onboard_vlm_admission.ExecutionAdmissionPolicy,
        onboard_vlm_admission.ExecutionSnapshot,
        onboard_vlm_admission.RouteClearanceEvidence,
        onboard_vlm_admission.SourceAnchoredIntent,
        onboard_vlm_runtime_contract.OnboardVlmRuntimeRequest,
        onboard_vlm_runtime_contract.OnboardVlmRuntimeResponse,
        onboard_vlm_worker.OnboardVlmWorkerResult,
        planner.PlannerPolicy,
        planner.PlanningEstimate,
        px4_mission.CompiledPx4Mission,
        px4_mission.Px4MissionItemInt,
    )
)
_IDENTITY_ENUMS = frozenset(
    (
        contracts.MissionCommand,
        contracts.MissionFrame,
        contracts.PlanAction,
        contracts.TaskType,
        onboard_agent.OnboardEventKind,
        onboard_agent.OnboardFlightAction,
        onboard_agent.OnboardResponse,
        onboard_agent.OnboardTaskAction,
    )
)
_IDENTITY_PATHS = frozenset(
    (Path, PosixPath, WindowsPath, PurePath, PurePosixPath, PureWindowsPath)
)


def _memory_identity(value):
    """Closed immutable field graph, not JSON serialization or an authorization.

    Avoid IO-bearing canonical getters on every packet. Include every stored
    dataclass field (including original bytes), exact types and paths. Unknown
    or mutable nodes fail closed; phase boundaries still run the deep verifier.
    """
    kind = type(value)
    if kind in {type(None), bool, int, float, str, bytes} or kind in _IDENTITY_ENUMS:
        return kind, value
    if kind in _IDENTITY_PATHS:
        return kind, str(value)
    if kind is tuple:
        return kind, tuple(_memory_identity(item) for item in value)
    if kind in _IDENTITY_DATACLASSES and is_dataclass(value):
        stored_fields = fields(value)
        stored_slots = {
            name
            for base in kind.__mro__
            for name in base.__dict__.get("__slots__", ())
            if name not in {"__weakref__"}
        }
        if (
            not value.__dataclass_params__.frozen
            or hasattr(value, "__dict__")
            or stored_slots != {item.name for item in stored_fields}
        ):
            raise ContinuationHostError("mutable admission node cannot be cached")
        return kind, tuple(
            (item.name, _memory_identity(getattr(value, item.name))) for item in stored_fields
        )
    raise ContinuationHostError("unknown admission node cannot be cached")


def _paused_identity(paused):
    if type(paused) is not PausedContinuationAdmission or hasattr(paused, "__dict__"):
        raise ContinuationHostError("unknown paused admission root cannot be cached")
    return _memory_identity(
        tuple(getattr(paused, item.name) for item in fields(paused) if item.name != "attempt")
    )


def _check_original_journal(binding):
    # Hash the bounded exact original bytes, not a repeatedly replayed journal.
    # Structural checks were performed by the phase's full parent verifier.
    with binding.journal_path.open("rb") as stream:
        payload = stream.read(256 * 1024 + 1)
    if (
        len(payload) != binding.journal_bytes
        or sha256(payload).hexdigest() != binding.journal_sha256
    ):
        raise ContinuationHostError("original parent journal bytes changed")


class OriginalParentContinuationHost:
    """Bind the source token, post-pause admission and actual wire transaction.

    Construct this first and pass ``host.phase_guard`` to the wire session.
    ``current_gate`` belongs to the exclusive host writer and must check its
    live task, scope, Mission head, permissions, resources and safety at each
    phase/send boundary; returning True is not self-authentication. Ordinary
    receive/RGB changes are not, by themselves, task cancellation.

    This initial implementation conservatively expires the admitted HOLD
    context instead of silently refreshing its timestamp. A slow compilation
    or transfer therefore fails closed; live route re-joining is not claimed.
    """

    def __init__(
        self,
        original: OnboardContinuationAdmission,
        *,
        attempt_id: str,
        generation: int,
        host_clock: Callable[[], float],
        current_gate: Callable[[str, int], bool],
    ) -> None:
        if not callable(host_clock) or not callable(current_gate):
            raise TypeError("host clock and independent current gate are required")
        self.host_clock, self.current_gate = host_clock, current_gate
        self.attempt = begin_pause_attempt(
            original,
            attempt_id=attempt_id,
            generation=generation,
            now_s=require_real(host_clock(), "actual host time", minimum=0),
        )
        self._lock = RLock()
        self._wire: Px4ContinuationWireSession | None = None
        self._paused: PausedContinuationAdmission | None = None
        self._started = False
        self._cancel_reason: str | None = None
        self._last_clock_s = self.attempt.validated_at_s
        self._paused_sha256: str | None = None
        self._original_memory_identity = _memory_identity(original)
        self._paused_memory_identity = None
        self._hold_fact_times: tuple[float, ...] = ()
        self._full_validation_phase: str | None = None
        self._events: list[str] = []

    def _now(self) -> float:
        now = require_real(self.host_clock(), "actual host time", minimum=self._last_clock_s)
        self._last_clock_s = now
        return now

    def _bind_paused(self, paused: PausedContinuationAdmission) -> None:
        """Internal validated attachment; caller holds the host writer lock."""
        identity = _paused_identity(paused)
        if paused.attempt is not self.attempt:
            raise ContinuationHostError("post-pause token changed")
        paused.validate_binding(now_s=self._now())
        self._paused = paused
        self._paused_sha256 = paused.canonical_hash
        self._paused_memory_identity = identity
        proof = paused.hold_parent_evidence
        self._hold_fact_times = tuple(
            fact.to_mapping()["observed_at_s"]
            for fact in (*proof.telemetry, *proof.navigation_facts)
        )

    def phase_guard(self, phase: str, generation: int) -> bool:
        with self._lock:
            if (
                not self._started
                or self._cancel_reason is not None
                or generation != self.attempt.generation
            ):
                return False
            self.attempt.validate_active_state(
                generation=generation, consumed_sha256=self._paused_sha256
            )
            if _memory_identity(self.attempt.original) != self._original_memory_identity:
                raise ContinuationHostError("original immutable admission changed")
            _check_original_journal(self.attempt.original.parent_evidence.mission_binding)
            now = self._now()
            if self._paused is not None:
                if (
                    self._paused.attempt is not self.attempt
                    or _paused_identity(self._paused) != self._paused_memory_identity
                ):
                    raise ContinuationHostError("admitted source/current artifact changed")
                # Full compilation/route/journal revalidation once per phase;
                # immutable identity, cancellation and absolute freshness stay
                # checked at every send guard, without recompiling for
                # each telemetry packet inside the protocol receive loop.
                if phase != self._full_validation_phase:
                    self._paused.validate_binding(now_s=now)
                    self._full_validation_phase = phase
                # Phase caching cannot extend a raw fact's original lifetime.
                # These are the immutable receive-envelope times, not the time
                # of parsing or the latest heartbeat in the wire session.
                _check_original_journal(self._paused.hold_parent_evidence.mission_binding)
                if self.current_gate(phase, generation) is not True:
                    return False
                # Hashing/full validation can consume a meaningful part of the
                # short budget. Re-sample after that work, before authorizing.
                now = self._now()
                if any(
                    not 0 <= now - stamp <= PX4_MISSION_FEEDBACK_CANONICAL_MAX_AGE_S
                    for stamp in self._hold_fact_times
                ):
                    raise ContinuationHostError("admitted original HOLD facts expired/future")
                original = self.attempt.original
                original.anchor.request.model_input.validate_freshness(now)
                current = self._paused.current
                compilation = self._paused.compilation
                if not (
                    self._paused.admitted_at_s <= now
                    and 0
                    <= now - current.context.captured_at_s
                    <= original.policy.max_current_age_s
                    and 0
                    <= now - current.clearance.observed_at_s
                    <= original.policy.max_current_age_s
                    and 0
                    <= now - original.anchor.source.context.captured_at_s
                    <= original.policy.max_source_age_s
                    and 0
                    <= now - compilation.current_context.captured_at_s
                    <= compilation.policy.maximum_context_age_s
                    and 0
                    <= now - compilation.parent.observed_at_s
                    <= compilation.policy.maximum_parent_age_s
                ):
                    raise ContinuationHostError("admitted source/current/clearance expired")
            elif phase not in {"ready", "pause"}:
                return False
            else:
                if self.current_gate(phase, generation) is not True:
                    return False
                now = self._now()
                original = self.attempt.original
                original.anchor.request.model_input.validate_freshness(now)
                if not (
                    0
                    <= now - original.anchor.source.context.captured_at_s
                    <= original.policy.max_source_age_s
                    and 0
                    <= now - original.current.context.captured_at_s
                    <= min(
                        original.policy.max_current_age_s,
                        original.compilation.policy.maximum_context_age_s,
                    )
                    and 0
                    <= now - original.compilation.parent.observed_at_s
                    <= original.compilation.policy.maximum_parent_age_s
                ):
                    raise ContinuationHostError("pre-pause original admission expired")
            # A callback can retire a token too. Its cancellation must win even
            # when it happens on this same thread during current_gate.
            self.attempt.validate_active_state(
                generation=generation, consumed_sha256=self._paused_sha256
            )
            return True

    def cancel(self, reason: str) -> None:
        # Never hold the host lock while taking the wire writer lock: the wire
        # invokes phase_guard under its own lock. Cancellation of the token is
        # visible at the next guard even if a write is already in progress.
        with self._lock:
            if self._cancel_reason is None:
                self._cancel_reason = require_text(reason, "cancellation reason")
                self.attempt.cancel(self._cancel_reason)
            wire = self._wire
        if wire is not None:
            wire.cancel(expected_generation=self.attempt.generation, reason=self._cancel_reason)

    def execute(
        self,
        wire: Px4ContinuationWireSession,
        *,
        admit_after_pause: Callable[[ContinuationPauseAttempt, dict], PausedContinuationAdmission],
        authorize_child: Callable[[PausedContinuationAdmission], PersistentPx4MissionTransaction],
    ) -> dict:
        """Pause first, then request a newly compiled/admitted child from host.

        The callbacks must not send, fake mode facts, normalize HOLD into AUTO,
        refresh original source times, or manufacture operator authorization.
        The caller owns and closes the returned persistent transaction.
        """
        if not isinstance(wire, Px4ContinuationWireSession):
            raise TypeError("wire must be an owned continuation session")
        if not callable(admit_after_pause) or not callable(authorize_child):
            raise TypeError("post-pause admission and child authorization are required")
        if (
            wire.generation != self.attempt.generation
            or wire.phase_guard != self.phase_guard
            or wire.state is not ContinuationWireState.READY
            or wire.clock_epoch != self.attempt.original.current.scope.clock_epoch
            or wire.host_clock != self.host_clock
        ):
            raise ContinuationHostError("wire generation/guard/clock/epoch/initial state differs")
        with self._lock:
            if self._started:
                raise ContinuationHostError("host attempt is one-shot")
            self.attempt.validate_active()
            self._wire = wire
            self._started = True
        try:
            pause = wire.pause_and_confirm(expected_generation=self.attempt.generation)
            with self._lock:
                self._events.append(canonical_json(pause))
            paused = admit_after_pause(self.attempt, pause)
            if (
                type(paused) is not PausedContinuationAdmission
                or paused.attempt is not self.attempt
            ):
                raise ContinuationHostError("post-pause admission belongs to another host attempt")
            with self._lock:
                self._bind_paused(paused)
            child = authorize_child(paused)
            if not isinstance(child, PersistentPx4MissionTransaction):
                raise ContinuationHostError("independently authorized persistent child required")
            identity = child.journal.identity
            compilation = paused.compilation
            scope = paused.current.scope
            _, safety = parse_mission_compile_runtime(
                strict_json_loads(compilation.runtime_json), "multirotor"
            )
            if (
                identity.executable_mission_hash
                != compilation.compiled_mission.executable_mission.canonical_hash
                or identity.protocol_hash != compilation.wire_hash
                or identity.bindings.context_snapshot_hash != paused.current.context.canonical_hash
                or identity.bindings.plan_hash != compilation.plan.canonical_hash
                or identity.bindings.task_spec_hash
                != self.attempt.original.anchor.task.canonical_hash
                or identity.bindings.prompt_hash != scope.prompt_sha256
                or identity.bindings.airframe_profile_hash != scope.airframe_profile_sha256
                or identity.bindings.geofence_hash != safety.geofence_hash
                or identity.bindings.vehicle_identity_hash
                != paused.hold_parent_evidence.mission_binding.vehicle_identity_sha256
            ):
                raise ContinuationHostError("authorized child does not bind exact admitted lineage")
            for operation in (
                lambda: wire.install_authorized_child(
                    child, expected_generation=self.attempt.generation
                ),
                lambda: wire.select_and_confirm_cursor(expected_generation=self.attempt.generation),
                lambda: wire.resume_and_confirm(expected_generation=self.attempt.generation),
            ):
                event = canonical_json(operation())
                with self._lock:
                    self._events.append(event)
            if wire.state is not ContinuationWireState.RESUMED:
                raise ContinuationHostError("receiver has not confirmed Mission resume")
            return self.to_mapping()
        except BaseException as error:
            self.cancel(f"{type(error).__name__}: {error}")
            raise

    def to_mapping(self) -> dict:
        with self._lock:
            wire = self._wire
            attempt = self.attempt.to_mapping()
            paused_sha = None if self._paused is None else self._paused.canonical_hash
            cancel_reason = self._cancel_reason
            events = [strict_json_loads(item) for item in self._events]
        # Never nest host -> wire locks; the send path locks wire -> host.
        wire_mapping = None if wire is None else wire.to_mapping()
        result = {
            "contract": "multidetect.original-parent-continuation-host-development-v1",
            "attempt": attempt,
            "paused_admission_sha256": paused_sha,
            "cancel_reason": cancel_reason,
            "events": events,
            "wire": wire_mapping,
            "receiver_confirmed_resume": wire_mapping is not None
            and wire_mapping["state"] == ContinuationWireState.RESUMED.value,
            "execution_authority_created": False,
            "source_authentication_proven": False,
            "actual_flight_verified": False,
            "repeated_parent_chain_implemented": False,
            "aircraft_watchdog_verified": False,
            "whole_sortie_dispatcher_connected": False,
        }
        return {**result, "receipt_sha256": canonical_sha256(result)}


__all__ = ["ContinuationHostError", "OriginalParentContinuationHost"]
