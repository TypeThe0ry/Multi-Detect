"""Receive-only, host-owned causal evidence for development VLM memory.

This module never sends a command, installs a Mission, or accepts a caller's
``completed`` flag. Proposal JSON is reparsed; acceptance is an exact adapter
revision; vehicle execution requires a committed/read-back Mission journal and
fresh, source-bound AUTO.MISSION/armed/in-air/Mission feedback. Completion is
derived by the existing receive-only outcome monitor. These facts describe the
bound observation session, not hardware acceptance or autonomous capability.

Telemetry inputs are original strict-JSON *host receive receipts*, not invented
sensor timestamps. Optional wire bytes are retained verbatim as base64. A hash
provides integrity, not authentication: the host must own the named receiver and
verify its source before calling the collector. No UTC/boot/monotonic conversion
or nearest-timestamp join is performed. Scope changes require a new collector.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING

from ..mission_journal import DurableMissionJournal, MissionTransactionState
from ..px4_mission import MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, PX4_AUTO_MISSION_CUSTOM_MODE
from ..px4_mission_feedback import (
    Px4MissionFeedbackConfig,
    Px4MissionFeedbackTracker,
)
from .contracts import ContextSnapshot, ExecutableMission, ResolvedTaskSpec
from .mission_outcome import MissionOutcomeMonitor
from .onboard_adapter import OnboardMissionRevision
from .onboard_agent import OnboardRuntimeEvent
from .onboard_multimodal import (
    RECEIPT_CONTRACT,
    MissionEvidenceLedger,
    OnboardEvidenceReceipt,
    OnboardImageFrame,
    OnboardMultimodalScope,
    Scope,
)
from .onboard_vlm_runtime_contract import (
    OnboardVlmRuntimeRequest,
    OnboardVlmRuntimeResponse,
    parse_onboard_vlm_runtime_response,
)
from .serialization import (
    canonical_json,
    canonical_sha256,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

if TYPE_CHECKING:
    from .onboard_vlm_admission import ExecutionAdmission

TELEMETRY_CONTRACT = "multidetect.onboard-received-vehicle-observation-v1"
PROPOSAL_SOURCE_ID = "host-strict-vlm-response-v2"
_MAX_SOURCE_BYTES = 256 * 1024


class OnboardEvidenceCollectorError(ValueError):
    """Missing, mismatched, stale, or non-causal host evidence."""


def _original(payload: bytes, expected_sha256: str, name: str) -> object:
    if type(payload) is not bytes or not 0 < len(payload) <= _MAX_SOURCE_BYTES:
        raise OnboardEvidenceCollectorError(f"{name} must be bounded immutable original bytes")
    require_sha256(expected_sha256, f"{name}.sha256")
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise OnboardEvidenceCollectorError(f"{name} original bytes/hash mismatch")
    raw = strict_json_loads(payload, name=name)
    canonical_sha256(raw)
    return raw


def _encoded(payload: bytes) -> dict[str, object]:
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "original_bytes_base64": base64.b64encode(payload).decode("ascii"),
    }


@dataclass(frozen=True, slots=True)
class OnboardAcceptanceTrace:
    """Host-supplied revision after adapter commit, never a model assertion."""

    proposal_id: str
    revision: OnboardMissionRevision
    context: ContextSnapshot
    event: OnboardRuntimeEvent
    source_receipt_bytes: bytes
    source_receipt_sha256: str
    observed_at_s: float
    clock_epoch: str
    source_id: str = "host-onboard-adapter-v1"
    execution_admission: ExecutionAdmission | None = None

    def __post_init__(self) -> None:
        require_sha256(self.proposal_id, "proposal_id")
        if not isinstance(self.revision, OnboardMissionRevision):
            raise TypeError("revision must be OnboardMissionRevision")
        if not isinstance(self.context, ContextSnapshot) or not isinstance(
            self.event, OnboardRuntimeEvent
        ):
            raise TypeError("acceptance needs actual context/event")
        raw = _original(self.source_receipt_bytes, self.source_receipt_sha256, "revision")
        if canonical_sha256(raw) != canonical_sha256(self.revision.to_mapping()):
            raise OnboardEvidenceCollectorError("original revision differs from typed revision")
        require_real(self.observed_at_s, "observed_at_s", minimum=self.event.observed_at_s)
        require_text(self.clock_epoch, "clock_epoch")
        require_text(self.source_id, "source_id")
        if self.execution_admission is not None:
            from .onboard_vlm_admission import ExecutionAdmission

            if not isinstance(self.execution_admission, ExecutionAdmission):
                raise TypeError("execution_admission must be typed ExecutionAdmission")


@dataclass(frozen=True, slots=True)
class OnboardMissionTelemetryBinding:
    """Explicit active Mission/readback identity supplied by its owning adapter.

    The journal is reopened and its exact bytes retained. Merely binding it
    does not create an executed receipt. A test/in-process journal is not proof
    of a real vehicle; authenticity of the registered receiver remains host-owned.
    """

    mission: ExecutableMission
    task: ResolvedTaskSpec
    journal_path: Path
    journal_bytes: int
    journal_sha256: str
    vehicle_identity_sha256: str
    bound_at_s: float
    clock_epoch: str

    def __post_init__(self) -> None:
        if not isinstance(self.mission, ExecutableMission) or not isinstance(
            self.task, ResolvedTaskSpec
        ):
            raise TypeError("binding needs actual Mission and task")
        if not isinstance(self.journal_path, Path):
            raise TypeError("journal_path must be Path")
        require_int(self.journal_bytes, "journal_bytes", minimum=1, maximum=_MAX_SOURCE_BYTES)
        require_sha256(self.journal_sha256, "journal_sha256")
        require_sha256(self.vehicle_identity_sha256, "vehicle_identity_sha256")
        require_real(self.bound_at_s, "bound_at_s", minimum=0)
        require_text(self.clock_epoch, "clock_epoch")


@dataclass(frozen=True, slots=True)
class OnboardTelemetryObservation:
    """Exact receive-envelope bytes with explicit host time and Mission binding."""

    source_receipt_bytes: bytes
    source_receipt_sha256: str

    def __post_init__(self) -> None:
        self.to_mapping()

    def to_mapping(self) -> dict:
        raw = strict_object(
            _original(self.source_receipt_bytes, self.source_receipt_sha256, "telemetry"),
            name="telemetry receive receipt",
            required=frozenset(
                {
                    "schema_version",
                    "contract",
                    "message_id",
                    "observed_at_s",
                    "clock_epoch",
                    "source_id",
                    "source_system_id",
                    "source_component_id",
                    "mission_sha256",
                    "transaction_id",
                    "message",
                }
            ),
            optional=frozenset({"wire_bytes_base64", "wire_bytes", "wire_sha256"}),
        )
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
            raise OnboardEvidenceCollectorError("unsupported telemetry schema")
        if raw["contract"] != TELEMETRY_CONTRACT:
            raise OnboardEvidenceCollectorError("unsupported telemetry contract")
        for name in ("message_id", "clock_epoch", "source_id", "transaction_id"):
            require_text(raw[name], name)
        require_sha256(raw["mission_sha256"], "mission_sha256")
        require_real(raw["observed_at_s"], "observed_at_s", minimum=0)
        for name in ("source_system_id", "source_component_id"):
            require_int(raw[name], name, minimum=1, maximum=255)
        body = raw["message"]
        if not isinstance(body, dict):
            raise OnboardEvidenceCollectorError("telemetry message must be object")
        kind = body.get("message_type")
        fields = {
            "HEARTBEAT": frozenset({"base_mode", "custom_mode"}),
            "EXTENDED_SYS_STATE": frozenset({"landed_state"}),
            "MISSION_CURRENT": frozenset({"seq"}),
            "MISSION_ITEM_REACHED": frozenset({"seq"}),
        }.get(kind)
        if fields is None:
            raise OnboardEvidenceCollectorError("unsupported receive-only telemetry type")
        strict_object(body, name="message", required=fields | {"message_type"})
        for name in fields:
            upper = 255 if name == "base_mode" else 4 if name == "landed_state" else 2**32 - 1
            require_int(body[name], name, minimum=0, maximum=upper)
        wire = {"wire_bytes_base64", "wire_bytes", "wire_sha256"} & raw.keys()
        if wire:
            if len(wire) != 3 or not isinstance(raw["wire_bytes_base64"], str):
                raise OnboardEvidenceCollectorError("wire byte provenance must be complete")
            try:
                packet = base64.b64decode(raw["wire_bytes_base64"], validate=True)
            except ValueError as exc:
                raise OnboardEvidenceCollectorError("wire bytes are not valid base64") from exc
            size = require_int(raw["wire_bytes"], "wire_bytes", minimum=1, maximum=4096)
            if len(packet) != size or hashlib.sha256(packet).hexdigest() != require_sha256(
                raw["wire_sha256"], "wire_sha256"
            ):
                raise OnboardEvidenceCollectorError("wire bytes/hash mismatch")
        return dict(raw)


class _ReceiptClock:
    """Host processing clock; original receiver timestamps stay in their receipts."""

    def __init__(self, at_s: float) -> None:
        self.at_s = at_s

    def monotonic(self) -> float:
        return self.at_s

    def utc_now(self):
        raise OnboardEvidenceCollectorError("receive receipts do not provide a UTC conversion")


@dataclass(slots=True)
class _Vehicle:
    binding: OnboardMissionTelemetryBinding
    journal_original: bytes
    identity: dict
    clock: _ReceiptClock
    tracker: Px4MissionFeedbackTracker
    monitor: MissionOutcomeMonitor
    last_source_at_s: float
    last_processing_at_s: float
    events: list[OnboardTelemetryObservation] = field(default_factory=list)
    processing_times: list[float] = field(default_factory=list)
    messages: dict[str, OnboardTelemetryObservation] = field(default_factory=dict)
    latest: dict[str, dict] = field(default_factory=dict)
    pending_reached: dict[int, tuple[dict, float]] = field(default_factory=dict)
    faulted: bool = False


@dataclass(slots=True)
class _Proposal:
    request: OnboardVlmRuntimeRequest
    response: OnboardVlmRuntimeResponse
    acceptance: OnboardAcceptanceTrace | None = None
    vehicle: _Vehicle | None = None


class OnboardEvidenceCollector:
    """Bounded mission-scoped facts; all methods are receive-only and locked."""

    def __init__(
        self,
        scope: Scope,
        *,
        expected_system_id: int,
        expected_component_id: int,
        vehicle_identity_sha256: str | None = None,
        acceptance_source_id: str = "host-onboard-adapter-v1",
        telemetry_source_id: str = "host-vehicle-receiver-v1",
    ) -> None:
        # Reuse the frozen ledger's scope type check without normalizing/relabeling.
        MissionEvidenceLedger(scope)
        self.scope = scope
        self.expected_system_id = require_int(expected_system_id, "system", minimum=1, maximum=255)
        self.expected_component_id = require_int(
            expected_component_id, "component", minimum=1, maximum=255
        )
        self.vehicle_identity_sha256 = (
            None
            if vehicle_identity_sha256 is None
            else require_sha256(vehicle_identity_sha256, "vehicle_identity_sha256")
        )
        self.acceptance_source_id = require_text(acceptance_source_id, "acceptance_source_id")
        self.telemetry_source_id = require_text(telemetry_source_id, "telemetry_source_id")
        self._sources = (PROPOSAL_SOURCE_ID, self.acceptance_source_id, self.telemetry_source_id)
        if len(set(self._sources)) != 3:
            raise OnboardEvidenceCollectorError("registered source IDs must differ")
        self._frames: list[OnboardImageFrame] = []
        self._receipts: list[OnboardEvidenceReceipt] = []
        self._proposals: dict[str, _Proposal] = {}
        self._stages: dict[tuple[str, str], OnboardEvidenceReceipt] = {}
        self._revision = 0
        self._last_adapter_sequence = -1
        self._current_mission_sha256: str | None = None
        self._lock = RLock()

    @property
    def receipt_revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> MissionEvidenceLedger:
        with self._lock:
            return MissionEvidenceLedger(
                self.scope, tuple(self._frames), tuple(self._receipts), self._sources
            )

    def add_observation(self, frame: OnboardImageFrame) -> bool:
        with self._lock:
            if not isinstance(frame, OnboardImageFrame) or frame.scope != self.scope:
                raise OnboardEvidenceCollectorError("observation crosses collector scope")
            for prior in self._frames:
                if prior.frame_id == frame.frame_id:
                    if prior == frame:
                        return False
                    raise OnboardEvidenceCollectorError("frame ID collision")
            if (
                len(self._frames) >= 64
                or sum(len(item.image_bytes) for item in self._frames) + len(frame.image_bytes)
                > 16 * 1024 * 1024
            ):
                raise OnboardEvidenceCollectorError("bounded observation registry is full")
            MissionEvidenceLedger(
                self.scope, (*self._frames, frame), tuple(self._receipts), self._sources
            )
            self._frames.append(frame)
            return True

    def _append(self, proposal_id, stage, observed_at_s, source_id, source_bytes, frame_ids):
        key = (proposal_id, stage)
        existing = self._stages.get(key)
        if existing is not None:
            if (
                existing.source_receipt_bytes == source_bytes
                and existing.observed_at_s == observed_at_s
                and existing.source_id == source_id
                and existing.frame_ids == frame_ids
            ):
                return existing
            raise OnboardEvidenceCollectorError("stage already consumed with different evidence")
        if (
            len(self._receipts) >= 64
            or sum(
                len(item.receipt_bytes) + len(item.source_receipt_bytes) for item in self._receipts
            )
            + len(source_bytes)
            + 2048
            > _MAX_SOURCE_BYTES
        ):
            raise OnboardEvidenceCollectorError("bounded receipt registry is full")
        prior = next(
            (item for item in reversed(self._receipts) if item.proposal_id == proposal_id), None
        )
        evidence_id = canonical_sha256(
            {
                "scope": self.scope.canonical_hash,
                "proposal": proposal_id,
                "stage": stage,
                "source": hashlib.sha256(source_bytes).hexdigest(),
            }
        )
        wrapper = {
            "schema_version": 1,
            "contract": RECEIPT_CONTRACT,
            "evidence_id": evidence_id,
            "scope_sha256": self.scope.canonical_hash,
            "stage": stage,
            "observed_at_s": observed_at_s,
            "source_id": source_id,
            "proposal_id": proposal_id,
            "prior_evidence_id": None if prior is None else prior.evidence_id,
            "frame_ids": list(frame_ids),
            "authority": "model_proposal" if stage == "proposed" else "executor_receipt",
            "source_receipt_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "success": True,
        }
        encoded = canonical_json(wrapper).encode()
        receipt = OnboardEvidenceReceipt(
            evidence_id,
            self.scope,
            stage,
            observed_at_s,
            source_id,
            encoded,
            hashlib.sha256(encoded).hexdigest(),
            proposal_id,
            source_bytes,
            wrapper["prior_evidence_id"],
            frame_ids,
        )
        MissionEvidenceLedger(
            self.scope, tuple(self._frames), (*self._receipts, receipt), self._sources
        )
        self._receipts.append(receipt)
        self._stages[key] = receipt
        self._revision += 1
        return receipt

    def record_proposal(
        self,
        request: OnboardVlmRuntimeRequest,
        raw_response: str | bytes,
        *,
        current_scope: Scope,
        completed_at_s: float,
        inference_latency_ms: float,
    ) -> OnboardEvidenceReceipt:
        with self._lock:
            if not isinstance(request, OnboardVlmRuntimeRequest):
                raise TypeError("proposal requires an immutable runtime request")
            if request.model_input.scope != self.scope or current_scope != self.scope:
                raise OnboardEvidenceCollectorError("proposal crosses collector scope")
            response = parse_onboard_vlm_runtime_response(
                raw_response,
                request,
                current_scope=current_scope,
                completed_at_s=completed_at_s,
                inference_latency_ms=inference_latency_ms,
            )
            proposal_id = request.canonical_hash
            prior = self._proposals.get(proposal_id)
            if prior is not None:
                if prior.response == response:
                    return self._stages[proposal_id, "proposed"]
                raise OnboardEvidenceCollectorError("request reused for a different response")
            # Inference may finish while newer RGB is arriving. The source memory
            # must be an exact immutable prefix, not a relabelled latest snapshot.
            source_ledger = request.model_input.ledger
            current_ledger = self.snapshot()
            if (
                source_ledger.scope != current_ledger.scope
                or source_ledger.trusted_receipt_sources != current_ledger.trusted_receipt_sources
                or source_ledger.frames != current_ledger.frames[: len(source_ledger.frames)]
                or source_ledger.receipts != current_ledger.receipts[: len(source_ledger.receipts)]
            ):
                raise OnboardEvidenceCollectorError(
                    "request ledger is not an exact collector prefix"
                )
            receipt = self._append(
                proposal_id,
                "proposed",
                completed_at_s,
                PROPOSAL_SOURCE_ID,
                response.raw_response_bytes,
                response.frame_ids,
            )
            self._proposals[proposal_id] = _Proposal(request, response)
            return receipt

    def record_acceptance(self, trace: OnboardAcceptanceTrace) -> OnboardEvidenceReceipt:
        with self._lock:
            if not isinstance(trace, OnboardAcceptanceTrace):
                raise TypeError("acceptance requires OnboardAcceptanceTrace")
            raw = _original(trace.source_receipt_bytes, trace.source_receipt_sha256, "revision")
            if canonical_sha256(raw) != canonical_sha256(trace.revision.to_mapping()):
                raise OnboardEvidenceCollectorError("original revision drifted from typed revision")
            state = self._proposals.get(trace.proposal_id)
            if state is None:
                raise OnboardEvidenceCollectorError("acceptance has no strict model proposal")
            if (
                trace.clock_epoch != self.scope.clock_epoch
                or trace.source_id != self.acceptance_source_id
            ):
                raise OnboardEvidenceCollectorError("acceptance clock/source mismatch")
            if state.acceptance is not None:
                if state.acceptance == trace:
                    # A receipt retry is not a new commit: never restore an older
                    # active Mission after another proposal has been accepted.
                    return self._stages[trace.proposal_id, "accepted"]
                raise OnboardEvidenceCollectorError(
                    "accepted stage already consumed with different trace"
                )
            self.scope.validate_context(trace.context)
            original_context = state.request.model_input.frames[-1].context
            state.request.validate_binding()
            if trace.observed_at_s < state.response.completed_at_s:
                raise OnboardEvidenceCollectorError("acceptance predates model response")
            admission = trace.execution_admission
            effective_proposal = None
            if admission is None:
                if original_context is None or original_context != trace.context:
                    raise OnboardEvidenceCollectorError(
                        "acceptance does not match actual input context"
                    )
                state.request.model_input.validate_freshness(trace.observed_at_s)
                if state.request.source_event is None or state.request.source_event != trace.event:
                    raise OnboardEvidenceCollectorError("acceptance needs the exact input event")
                source_bytes = trace.source_receipt_bytes
            else:
                from .onboard_vlm_admission import (
                    ExecutionAdmission,
                    ExecutionSnapshot,
                    SourceAnchoredIntent,
                )

                if not isinstance(admission, ExecutionAdmission):
                    raise TypeError("execution_admission must be typed ExecutionAdmission")
                if not isinstance(admission.anchor, SourceAnchoredIntent) or not isinstance(
                    admission.current, ExecutionSnapshot
                ):
                    raise TypeError("admission requires typed original anchor/current")
                normalized_scope = (
                    self.scope
                    if isinstance(self.scope, OnboardMultimodalScope)
                    else OnboardMultimodalScope.from_inference_scope(self.scope)
                )
                current_scope = admission.current.scope
                if not isinstance(current_scope, OnboardMultimodalScope):
                    current_scope = OnboardMultimodalScope.from_inference_scope(current_scope)
                anchor = admission.anchor
                if (
                    anchor.request != state.request
                    or anchor.result.response != state.response
                    or anchor.result.raw_output.encode() != state.response.raw_response_bytes
                    or anchor.source.context != original_context
                    or anchor.source.event != state.request.source_event
                    or current_scope != normalized_scope
                    or admission.current.context != trace.context
                    or admission.current.event != trace.event
                    or trace.observed_at_s < admission.admitted_at_s
                ):
                    raise OnboardEvidenceCollectorError(
                        "admission differs from recorded source/current"
                    )
                # This reopens raw parsing, clearance policy, geodetic transform and compiler.
                admission.validate_binding(now_s=trace.observed_at_s)
                effective_proposal = admission.proposal()
                compilation = admission.compilation
                if (
                    trace.revision.decision.model_proposal != effective_proposal
                    or trace.revision.mission != compilation.compiled_mission.executable_mission
                    or trace.revision.decision.binding.plan_sha256
                    != compilation.plan.canonical_hash
                    or trace.revision.decision.binding.wire_hash != compilation.wire_hash
                    or trace.revision.decision.binding.executable_mission_sha256
                    != compilation.executable_mission_hash
                ):
                    raise OnboardEvidenceCollectorError(
                        "admission effective plan/Mission/wire mismatch"
                    )
                source_bytes = canonical_json(
                    {
                        "schema_version": 1,
                        "contract": "multidetect.onboard-admitted-revision-evidence-v1",
                        "original_revision": _encoded(trace.source_receipt_bytes),
                        "original_model_response": _encoded(state.response.raw_response_bytes),
                        "execution_admission": admission.to_mapping(),
                        "effective_plan_origin": "host_geodetic_reprojection",
                        "original_model_output_rewritten": False,
                        "execution_authority": False,
                    }
                ).encode()
            revision = trace.revision
            decision = revision.decision
            output = state.response.output
            proposal = decision.model_proposal
            expected_plan = (
                output.flight_plan if effective_proposal is None else effective_proposal.flight_plan
            )
            if (
                revision.event != trace.event
                or decision.prompt_sha256 != self.scope.prompt_sha256
                or decision.context_sha256 != trace.context.canonical_hash
                or decision.event_sha256 != trace.event.canonical_hash
                or (
                    decision.selected_task_id != self.scope.selected_task_id
                    and not (revision.mission is None and decision.selected_task_id is None)
                )
                or proposal is None
                or decision.response != output.response
                or (
                    proposal.response,
                    proposal.reason,
                    proposal.confidence,
                    proposal.task_action,
                    proposal.flight_plan,
                )
                != (
                    output.response,
                    output.reason,
                    output.confidence,
                    output.task_action,
                    expected_plan,
                )
                or decision.binding.vehicle_class != self.scope.vehicle_class
                or decision.binding.airframe_profile_hash != self.scope.airframe_profile_sha256
            ):
                raise OnboardEvidenceCollectorError(
                    "adapter revision does not accept this proposal"
                )
            if output.task_action is not None and (
                revision.task_receipt is None
                or revision.task_receipt.phase != output.task_action
                or revision.task_receipt.event_sha256 != trace.event.canonical_hash
                or revision.task_receipt.context_sha256 != trace.context.canonical_hash
                or revision.task_receipt.model_sha256 != proposal.model_sha256
                or revision.task_receipt.model_id != proposal.model_id
                or revision.task_receipt.provider != proposal.provider
            ):
                raise OnboardEvidenceCollectorError("accepted task phase lacks its actual receipt")
            if revision.mission is not None and (
                revision.mission.canonical_hash != decision.binding.executable_mission_sha256
                or revision.mission.airframe_profile_hash != self.scope.airframe_profile_sha256
                or revision.mission.platform_plan_hash != decision.binding.plan_sha256
            ):
                raise OnboardEvidenceCollectorError("accepted Mission identity mismatch")
            if state.acceptance is None and revision.sequence <= self._last_adapter_sequence:
                raise OnboardEvidenceCollectorError("adapter revision sequence is not newer")
            receipt = self._append(
                trace.proposal_id,
                "accepted",
                trace.observed_at_s,
                trace.source_id,
                source_bytes,
                state.response.frame_ids,
            )
            state.acceptance = trace
            self._last_adapter_sequence = max(self._last_adapter_sequence, revision.sequence)
            self._current_mission_sha256 = (
                None if revision.mission is None else revision.mission.canonical_hash
            )
            return receipt

    def bind_vehicle_mission(
        self, proposal_id: str, binding: OnboardMissionTelemetryBinding
    ) -> None:
        with self._lock:
            if not isinstance(binding, OnboardMissionTelemetryBinding):
                raise TypeError("typed Mission telemetry binding required; Permit is not execution")
            state = self._proposals.get(proposal_id)
            if state is None or state.acceptance is None:
                raise OnboardEvidenceCollectorError("vehicle binding needs accepted proposal")
            if state.vehicle is not None:
                if state.vehicle.binding == binding:
                    return
                raise OnboardEvidenceCollectorError("active Mission binding cannot be replaced")
            scope = self.scope
            trace = state.acceptance
            if (
                binding.clock_epoch != scope.clock_epoch
                or binding.bound_at_s < trace.observed_at_s
                or binding.mission != trace.revision.mission
                or binding.task.task_id != scope.selected_task_id
                or self.vehicle_identity_sha256 is None
                or binding.vehicle_identity_sha256 != self.vehicle_identity_sha256
                or binding.mission.canonical_hash != self._current_mission_sha256
            ):
                raise OnboardEvidenceCollectorError(
                    "vehicle binding scope/Mission/identity mismatch"
                )
            original = binding.journal_path.read_bytes()
            if (
                len(original) != binding.journal_bytes
                or hashlib.sha256(original).hexdigest() != binding.journal_sha256
            ):
                raise OnboardEvidenceCollectorError("journal original bytes/hash mismatch")
            with DurableMissionJournal.open(binding.journal_path) as journal:
                identity = journal.identity
                bindings = identity.bindings
                if (
                    journal.recovery_required
                    or journal.state
                    not in {
                        MissionTransactionState.COMMITTED,
                        MissionTransactionState.START_PERMITTED,
                    }
                    or identity.executable_mission_hash != binding.mission.canonical_hash
                    or identity.airframe_adapter_id != binding.mission.airframe_adapter_id
                    or identity.mission_item_count != len(binding.mission.items)
                    or bindings.prompt_hash != scope.prompt_sha256
                    or bindings.task_spec_hash != binding.task.canonical_hash
                    or bindings.context_snapshot_hash != trace.context.canonical_hash
                    or bindings.plan_hash != binding.mission.platform_plan_hash
                    or bindings.airframe_profile_hash != scope.airframe_profile_sha256
                    or bindings.vehicle_identity_hash != binding.vehicle_identity_sha256
                ):
                    raise OnboardEvidenceCollectorError(
                        "journal lacks matching committed readback identity"
                    )
                identity_map = identity.to_mapping()
            if binding.journal_path.read_bytes() != original:
                raise OnboardEvidenceCollectorError("journal changed during read-only verification")
            clock = _ReceiptClock(binding.bound_at_s)
            config = Px4MissionFeedbackConfig(
                expected_system_id=self.expected_system_id,
                expected_component_id=self.expected_component_id,
            )
            state.vehicle = _Vehicle(
                binding,
                original,
                identity_map,
                clock,
                Px4MissionFeedbackTracker(binding.mission, config=config),
                MissionOutcomeMonitor(
                    binding.mission,
                    clock,
                    timeout_s=binding.task.constraints.maximum_duration_s,
                    expected_system_id=self.expected_system_id,
                    expected_component_id=self.expected_component_id,
                ),
                binding.bound_at_s,
                binding.bound_at_s,
            )

    def ingest_vehicle_message(
        self,
        proposal_id: str,
        observation: OnboardTelemetryObservation,
        *,
        now_s: float,
    ) -> OnboardEvidenceReceipt | None:
        with self._lock:
            state = self._proposals.get(proposal_id)
            if state is None or state.vehicle is None:
                raise OnboardEvidenceCollectorError(
                    "telemetry lacks committed active Mission binding"
                )
            if not isinstance(observation, OnboardTelemetryObservation):
                raise TypeError("original typed telemetry receive receipt required")
            vehicle = state.vehicle
            if vehicle.faulted:
                raise OnboardEvidenceCollectorError("vehicle evidence session is faulted")
            if vehicle.binding.mission.canonical_hash != self._current_mission_sha256:
                vehicle.faulted = True
                raise OnboardEvidenceCollectorError(
                    "Mission was superseded; old lineage cannot complete"
                )
            raw = observation.to_mapping()
            stamp = raw["observed_at_s"]
            now = require_real(now_s, "now_s", minimum=stamp)
            age = vehicle.tracker.config.maximum_age_s
            if now < vehicle.last_processing_at_s:
                vehicle.faulted = True
                raise OnboardEvidenceCollectorError("telemetry processing clock regressed")
            key = raw["message_id"]
            existing = vehicle.messages.get(key)
            if existing is not None:
                if existing == observation:
                    # A retry advances only the host clock, never source freshness
                    # or the outcome monitor's lifecycle/feedback observations.
                    vehicle.last_processing_at_s = now
                    vehicle.clock.at_s = now
                    return None
                vehicle.faulted = True
                raise OnboardEvidenceCollectorError("telemetry message ID collision")
            if (proposal_id, "completed") in self._stages:
                raise OnboardEvidenceCollectorError("completed lineage is closed")
            if (
                raw["clock_epoch"] != self.scope.clock_epoch
                or raw["source_id"] != self.telemetry_source_id
                or (raw["source_system_id"], raw["source_component_id"])
                != (self.expected_system_id, self.expected_component_id)
                or raw["mission_sha256"] != vehicle.binding.mission.canonical_hash
                or raw["transaction_id"] != vehicle.identity["transaction_id"]
                or stamp < vehicle.last_source_at_s
                or now - stamp > age
                or any(now - pending[1] >= age for pending in vehicle.pending_reached.values())
                or len(vehicle.events) >= 256
                or sum(len(item.source_receipt_bytes) for item in vehicle.events)
                + len(observation.source_receipt_bytes)
                > 96 * 1024
            ):
                vehicle.faulted = True
                raise OnboardEvidenceCollectorError(
                    "telemetry source/clock/Mission/order/bound mismatch"
                )
            # Receiver and processor are independently monotonic. Queue delay
            # must not compare a new source stamp against the previous host now.
            vehicle.clock.at_s = now
            message = {
                **raw["message"],
                "source_system_id": raw["source_system_id"],
                "source_component_id": raw["source_component_id"],
            }
            kind = message["message_type"]
            reconcile_start = len(vehicle.monitor.reconciliations)
            accepted = vehicle.monitor.ingest_message(message)
            if not accepted:
                vehicle.faulted = True
                raise OnboardEvidenceCollectorError(
                    "canonical outcome monitor rejected source message"
                )
            if kind.startswith("MISSION_"):
                current = vehicle.tracker.snapshot(now_s=now).current_sequence
                if (
                    kind == "MISSION_ITEM_REACHED"
                    and current is not None
                    and message["seq"] > current
                ):
                    vehicle.pending_reached.setdefault(message["seq"], (message, stamp))
                elif not vehicle.tracker.ingest_message(message, observed_at_s=now):
                    vehicle.faulted = True
                    raise OnboardEvidenceCollectorError(
                        "canonical Mission tracker rejected feedback"
                    )
                for item in vehicle.monitor.reconciliations[reconcile_start:]:
                    pending = vehicle.pending_reached.pop(item["sequence"], None)
                    if (
                        pending is None
                        or not item["accepted"]
                        or not vehicle.tracker.ingest_message(
                            pending[0], observed_at_s=item["confirmed_at_s"]
                        )
                    ):
                        vehicle.faulted = True
                        raise OnboardEvidenceCollectorError(
                            "Mission feedback reconciliation failed"
                        )
            vehicle.events.append(observation)
            vehicle.processing_times.append(now)
            vehicle.messages[key] = observation
            vehicle.latest[kind] = raw
            vehicle.last_source_at_s = stamp
            vehicle.last_processing_at_s = now
            outcome = vehicle.monitor.poll()
            if outcome.status in {"failed", "timeout"}:
                vehicle.faulted = True
                raise OnboardEvidenceCollectorError(
                    f"canonical outcome is {outcome.status}: {outcome.reason}"
                )
            feedback = vehicle.tracker.snapshot(now_s=now)
            stage = None
            heartbeat = vehicle.latest.get("HEARTBEAT")
            lifecycle = vehicle.latest.get("EXTENDED_SYS_STATE")
            latest_feedback_source = max(
                (
                    item["observed_at_s"]
                    for name, item in vehicle.latest.items()
                    if name.startswith("MISSION_")
                ),
                default=None,
            )
            source_samples_fresh = (
                heartbeat is not None
                and lifecycle is not None
                and latest_feedback_source is not None
                and now - heartbeat["observed_at_s"] <= age
                and now - lifecycle["observed_at_s"] <= age
                and now - latest_feedback_source <= age
            )
            if (proposal_id, "executed") not in self._stages:
                if (
                    feedback.qualified
                    and source_samples_fresh
                    and heartbeat["message"]["base_mode"] & 128
                    and heartbeat["message"]["base_mode"] & MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
                    and heartbeat["message"]["custom_mode"] == PX4_AUTO_MISSION_CUSTOM_MODE
                    and lifecycle["message"]["landed_state"] == 2
                ):
                    stage = "executed"
            elif outcome.status == "completed":
                if not source_samples_fresh:
                    vehicle.faulted = True
                    raise OnboardEvidenceCollectorError(
                        "processing completion lacks fresh original source samples"
                    )
                stage = "completed"
            if stage is None:
                return None
            proof = {
                "schema_version": 1,
                "contract": "multidetect.onboard-observed-Mission-proof-v1",
                "stage": stage,
                "scope_sha256": self.scope.canonical_hash,
                "clock_epoch": self.scope.clock_epoch,
                "observed_at_s": now,
                "monitor_clock_semantics": "host_processing_time_s",
                "last_source_observed_at_s": vehicle.last_source_at_s,
                "last_processed_at_s": vehicle.last_processing_at_s,
                "journal_identity": vehicle.identity,
                "journal_original": _encoded(vehicle.journal_original),
                "received_observations": [
                    {
                        **_encoded(item.source_receipt_bytes),
                        "source_observed_at_s": item.to_mapping()["observed_at_s"],
                        "processed_at_s": processed,
                    }
                    for item, processed in zip(
                        vehicle.events, vehicle.processing_times, strict=True
                    )
                ],
                "feedback": feedback.to_mapping(),
                "reconciliations": list(vehicle.monitor.reconciliations),
                "outcome": {
                    "mission_hash": outcome.mission_hash,
                    "status": outcome.status,
                    "reason": outcome.reason,
                    "elapsed_s": outcome.elapsed_s,
                },
                "execution_meaning": "observed_airborne_Mission_not_arrival_or_actuator_proof",
                "source_authentication": "host_owned_receiver_not_proven_by_hash",
                "execution_authority": False,
                "truth_free": True,
            }
            try:
                return self._append(
                    proposal_id,
                    stage,
                    now,
                    self.telemetry_source_id,
                    canonical_json(proof).encode(),
                    state.response.frame_ids,
                )
            except Exception:
                # Receive monitor state has advanced. Never retry it as an untouched session.
                vehicle.faulted = True
                raise


__all__ = [
    "OnboardAcceptanceTrace",
    "OnboardEvidenceCollector",
    "OnboardEvidenceCollectorError",
    "OnboardMissionTelemetryBinding",
    "OnboardTelemetryObservation",
    "PROPOSAL_SOURCE_ID",
    "TELEMETRY_CONTRACT",
]
