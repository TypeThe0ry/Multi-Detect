"""NEW complete finite lifecycle execution using CP-v2 and V4 resource evidence.

Only the old normal Mission mode/ARM/one RTL commands are reused. No force ARM,
fixture issuer, arbitrary sender, time renewal, or new receive owner is added.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import timedelta
from pathlib import Path

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.mission_context_admission import ContextV2MissionAdmission
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import (
    canonical_json,
    require_bool,
    require_real,
    require_sha256,
    require_text,
    strict_object,
)
from multidetect.autonomy.sim_navigation_reference import _binding
from multidetect.mission_journal import MissionJournalIdentity
from multidetect.px4_external_mission import ExternalRuntimeAttestation
from scripts import onboard_teacher_mission_execution_v2 as v8
from scripts import onboard_teacher_mission_live_revalidation_v2 as live_api
from scripts import onboard_teacher_mission_transport_v2 as transfer
from scripts.onboard_teacher_sortie_artifact_ledger_v1 import (
    LedgerFileBinding,
    TeacherSortieArtifactLedger,
)

CONTRACT = "multidetect.teacher-owned-lifecycle-Mission-execution-v3"
_permit_mapping = v8.frozen._permit_mapping


@dataclass(frozen=True, slots=True)
class TeacherMissionExecutionIntentV3:
    transaction_id: str
    journal_identity_sha256: str
    start_permit_sha256: str
    context_snapshot_sha256: str
    context_admission_sha256: str
    airframe_profile_sha256: str
    runtime_attestation_sha256: str
    container_id: str
    image: str
    clock_id: str
    approved_at_s: float
    expires_at_s: float
    explicitly_authorized: bool
    return_abort_authorized: bool
    budget_sha256: str

    def __post_init__(self):
        for name in ("transaction_id", "image", "clock_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in (
            "journal_identity_sha256",
            "start_permit_sha256",
            "context_snapshot_sha256",
            "context_admission_sha256",
            "airframe_profile_sha256",
            "runtime_attestation_sha256",
            "container_id",
            "budget_sha256",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("approved_at_s", "expires_at_s"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        if not 0 < self.expires_at_s - self.approved_at_s <= 600:
            raise ValueError("NEW original lifecycle execution intent must fit ten minutes")
        for name in ("explicitly_authorized", "return_abort_authorized"):
            if require_bool(getattr(self, name), name) is not True:
                raise ValueError("Explicit bounded normal start/return declaration required")

    def to_mapping(self):
        return {
            "contract": CONTRACT,
            "scope": "owned-disposable-SIM-finite-sortie-normal-ARM",
            **asdict(self),
        }

    @classmethod
    def from_mapping(cls, raw):
        value = strict_object(
            raw,
            name="TeacherMissionExecutionIntentV3",
            required=frozenset({"contract", "scope", *(f.name for f in fields(cls))}),
        )
        if (
            value["contract"] != CONTRACT
            or value["scope"] != "owned-disposable-SIM-finite-sortie-normal-ARM"
        ):
            raise ValueError("NEW lifecycle execution intent scope differs")
        return cls(**{f.name: value[f.name] for f in fields(cls)})


class TeacherMissionExecutionV3(v8.TeacherMissionExecutionV2):
    def __init__(
        self,
        *,
        transport,
        permit,
        context,
        admission,
        runtime_attestation,
        intent,
        runtime_recheck,
        evidence_directory,
        artifact_root,
        send_timeout_s=1.0,
        maximum_observation_age_s=2.0,
        original_inputs,
        live_input_provider,
        artifact_ledger,
    ):
        if type(transport) is not transfer.TeacherMissionTransportV2:
            raise TypeError("Existing V6 Mission transport required")
        transport._main()
        if type(context) is not ContextSnapshot or type(admission) is not ContextV2MissionAdmission:
            raise TypeError("Typed source-qualified context and narrow SIM admission required")
        if type(runtime_attestation) is not ExternalRuntimeAttestation:
            raise TypeError("Typed current owned runtime attestation required")
        if type(intent) is not TeacherMissionExecutionIntentV3:
            raise TypeError("Typed scoped execution intent required")
        if not callable(runtime_recheck):
            raise TypeError("Current owned runtime reinspection callback required")
        if TeacherMissionExecutionIntentV3.from_mapping(intent.to_mapping()) != intent:
            raise ValueError("Execution intent is not canonical")
        checked = runtime_attestation.to_mapping()
        checked.pop("attestation_sha256")
        if ExternalRuntimeAttestation.from_mapping(checked) != runtime_attestation:
            raise ValueError("Runtime attestation is not canonical")
        if type(original_inputs) is not live_api.TeacherMissionOriginalInputs:
            raise TypeError("NEW typed original inputs with actual CP-v2 required")
        if (
            type(original_inputs.navigation_checkpoint)
            is not live_api.LiveTeacherNavigationCheckpoint
        ):
            raise TypeError("Exact original CP-v2 required; no historical capture proxy")
        if not callable(live_input_provider):
            raise TypeError("Main-owned actual CP-v2 current provider required")
        if type(artifact_ledger) is not TeacherSortieArtifactLedger:
            raise TypeError("Actual typed camera/native/evidence ledger required")
        if (
            artifact_ledger.budget != transport.budget
            or artifact_ledger._clock != transport._clock
            or intent.budget_sha256 != transport.budget.canonical_hash
            or intent.approved_at_s != transport.budget.started_at_s
            or intent.expires_at_s != transport.budget.deadline_s
            or context != original_inputs.draft.context
            or admission != original_inputs.draft.context_admission
            or original_inputs.navigation_checkpoint.budget != transport.budget
        ):
            raise ValueError("Original lifecycle plan/context/writer/intent/ledger differs")
        identity = MissionJournalIdentity.from_transaction(transport._transaction.transaction)
        live_api.validate_teacher_plan_source_admission(
            original_inputs, identity, transport._transaction.transaction._validation_report
        )
        self._original, self._provider = original_inputs, live_input_provider
        self._live_inputs, self._live_receipt = None, None
        self._live_phase, self._phase_started_at_s = None, None
        self._live_evidence = []
        self._ledger = artifact_ledger
        self.budget = transport.budget
        self._transport, self._permit = transport, permit
        self._context, self._admission, self._runtime = context, admission, runtime_attestation
        self.intent, self._recheck = intent, runtime_recheck
        self._owner = threading.get_ident()
        self._timeout = require_real(send_timeout_s, "send_timeout_s", minimum=0.01, maximum=2)
        self._age = require_real(
            maximum_observation_age_s, "observation age", minimum=0.01, maximum=2
        )
        self._identity = MissionJournalIdentity.from_transaction(transport._transaction.transaction)
        self._lock, self._active, self._sender_owner = threading.RLock(), None, None
        self._failure, self._closed, self._status = None, False, "ready"
        self._attempts, self._observations = [], []
        self._start_at, self._arm_at, self._rtl_at = None, None, None
        self._fresh_runtime_at = None
        self._latest = {}
        self._acks, self._recent = deque(), deque(maxlen=256)
        self._start_called = self._abort_called = False
        self._root = Path(artifact_root).resolve()
        self._directory = Path(evidence_directory).absolute()
        if (
            self._directory.exists()
            or not self._directory.parent.is_dir()
            or not self._directory.resolve().is_relative_to(self._root)
            or any(p.is_symlink() for p in (self._directory, *self._directory.parents))
        ):
            raise ValueError("Fresh contained execution evidence directory required")
        self._scope(starting=True)
        self._runtime_now()
        artifact_ledger.bind_execution_sender(transport._receiver)
        self._directory.mkdir(exist_ok=False)
        stat = self._directory.stat()
        self._directory_identity = (stat.st_dev, stat.st_ino)
        self._save(
            "execution-binding.json",
            {
                "contract": CONTRACT,
                "intent": intent.to_mapping(),
                "journal_identity": self._identity.to_mapping(),
                "permit": _permit_mapping(permit),
                "context": context.to_mapping(),
                "context_admission": admission.to_mapping(),
                "runtime_attestation": runtime_attestation.to_mapping(),
                "normal_ARM_param2": 0.0,
                "flight_permission_created": False,
            },
        )

        live_api._historical_original_sources(
            original_inputs, artifact_root=self._root, clock_id=self.intent.clock_id
        )

    def _verify_live_receipt(self, kind, now=None):
        if self._live_receipt is None or self._live_inputs is None or self._live_phase != kind:
            raise ValueError("Missing exact NEW phase source revalidation lease")
        document = self._live_receipt.to_mapping()
        self._check_live_writer(self._live_inputs)
        initial = super()._scope(starting=False) if now is None else now
        if not document["qualified_at_s"] <= initial < document["expires_at_s"]:
            raise ValueError("Live phase receipt is future or stale")
        reproduced = live_api._reproduce_teacher_mission_live_receipt(
            self._original,
            self._live_inputs,
            receipt=self._live_receipt,
            identity=self._identity,
            validation_report=self._transport._transaction.transaction._validation_report,
            artifact_root=self._root,
            phase=kind,
            phase_started_at_s=self._phase_started_at_s,
            # The readonly reproduction API uses each receipt/checkpoint's
            # OWN original time. It never calls a main-owned writer API from
            # the sender or creates a refreshed lease. Expiry/energy below
            # use actual current time before AND after source reads.
            clock_id=self.intent.clock_id,
        )
        if reproduced != self._live_receipt:
            raise ValueError("Current phase source recomputation differs")
        self._check_live_writer(self._live_inputs)
        checked_now = super()._scope(starting=False)
        budget = document["current_budget"]
        if not checked_now < document["expires_at_s"] or budget[
            "full_route_estimated_duration_s"
        ] > budget["available_above_fixed_return_reserve_s"] - (
            checked_now - document["qualified_at_s"]
        ):
            raise ValueError("Live phase lease/energy exhausted while reading sources")
        return checked_now

    def _prepare_phase(self, kind):
        self._main()
        if self._active is not None or len(self._live_evidence) >= 2:
            raise RuntimeError("One bounded main-owned mode/ARM revalidation per phase")
        self.poll(0)
        self._runtime_now()
        started = super()._scope(starting=False)
        inputs = self._provider(phase=kind, phase_started_at_s=started, execution=self)
        if type(inputs) is not live_api.TeacherMissionLiveInputs:
            raise TypeError("Provider must return typed NEW actual phase sources")
        self._check_live_writer(inputs)
        self._require_dynamic_sources(inputs)
        receipt = live_api.revalidate_teacher_mission_for_control(
            self._original,
            inputs,
            identity=self._identity,
            validation_report=self._transport._transaction.transaction._validation_report,
            artifact_root=self._root,
            phase=kind,
            phase_started_at_s=started,
            now_s=super()._scope(starting=False),
            clock_id=self.intent.clock_id,
        )
        self._live_inputs, self._live_receipt = inputs, receipt
        self._live_phase, self._phase_started_at_s = kind, started
        binding = self._save(f"live-{kind}.json", receipt.to_mapping())
        self._verify_live_receipt(kind)
        self._assert_start_capacity(kind)
        self._live_evidence.append(
            {"phase": kind, "receipt": binding, "sha256": receipt.canonical_hash}
        )

    def _scope(self, *, starting=False):
        now = super()._scope(starting=starting)
        if self._active is not None:
            phase = {
                "mission-mode": "mission_mode",
                "normal-arm": "normal_arm",
                "return-abort": "rtl_land",
            }.get(self._active.kind)
            if phase is None:
                raise ValueError("Only the original finite lifecycle controls permitted")
            self.budget.assert_phase(phase, now_s=now, clock_id=self.intent.clock_id)
            if starting and self._active.kind != "return-abort":
                # The main physical file audit already completed; these are
                # the exact permanently charged counters, not caller values.
                self._assert_start_capacity(self._active.kind, sender_only=True, now=now)
        return now

    def _check_live_writer(self, inputs):
        super()._check_live_writer(inputs)
        if inputs.navigation_checkpoint.budget != self.budget:
            raise ValueError("Current checkpoint changed the original lifecycle budget")

    def _require_dynamic_sources(self, inputs):
        checkpoint = inputs.navigation_checkpoint
        document = checkpoint.to_mapping()
        dynamic = [
            checkpoint.checkpoint_source,
            inputs.loaded_world_native_source,
            EvidenceBinding.from_mapping(document["input_source"]),
            EvidenceBinding.from_mapping(document["inputs"]["prefix_source"]),
        ]
        for source in dynamic:
            self._ledger.require_source(source, artifact_root=self._root, category="evidence")
        # The source-qualified camera JSON must not reference pixels stored
        # outside the same actual charged lifecycle camera allocation.
        for update in document["inputs"]["camera_updates"]:
            source = _binding(self._root, update["camera"], pointer="")
            self._ledger.require_source(source, artifact_root=self._root)
            camera = live_api._read_document(self._root, source, 128 * 1024, {})
            for binding in camera["persisted_payloads"].values():
                self._ledger.require_source(
                    _binding(self._root, binding, pointer=""),
                    artifact_root=self._root,
                    category="camera",
                )

    def _save(self, name, value):
        path = self._directory / name
        raw = (canonical_json(value) + "\n").encode("utf-8")
        reservation = self._ledger.reserve_write("evidence", path, maximum_bytes=len(raw))
        binding = super()._save(name, value)
        self._ledger.commit_write(
            reservation,
            LedgerFileBinding(binding["path"], binding["bytes"], binding["sha256"]),
        )
        return binding

    def _assert_start_capacity(self, kind, *, sender_only=False, now=None):
        capacity = (
            self._ledger.remaining_for_bound_sender() if sender_only else self._ledger.capacity()
        )
        capture = self._transport._capture
        now = self._scope(starting=False) if now is None else now
        if self._live_receipt is None:
            raise ValueError("No current complete route resource receipt")
        duration = self._live_receipt.to_mapping()["current_budget"][
            "full_route_estimated_duration_s"
        ]
        self.budget.assert_start_capacity(
            now_s=now,
            clock_id=self.intent.clock_id,
            route_duration_s=duration,
            used_records=len(capture._attempts),
            used_telemetry_bytes=capture._charged_bytes,
            next_control=kind,
            **capacity,
        )
        # A resource declaration cannot prolong the actual existing UTC Grant.
        core = self._transport._transaction.transaction
        core._validate_authorization(self._transport._utc())
        if core._expires_at < self._transport._utc() + timedelta(
            seconds=self.budget.remaining_s(now, clock_id=self.intent.clock_id)
        ):
            raise ValueError("Existing Grant cannot cover finite return/cleanup; no normal ARM")

    def _request(self, kind):
        if kind not in {"mission-mode", "normal-arm", "return-abort"}:
            raise ValueError("Only the three existing finite controls are allowed")
        if kind != "return-abort":
            # _prepare_phase is called by V8 after full current-source checks.
            self.budget.assert_phase(
                "mission_mode" if kind == "mission-mode" else "normal_arm",
                now_s=self._scope(starting=False),
                clock_id=self.intent.clock_id,
            )
        return super()._request(kind)

    def _wait(self, predicate, timeout_s, label):
        phase = (
            "rtl_land"
            if self._rtl_at is not None
            else "normal_arm"
            if self._arm_at is not None
            else "mission_mode"
        )
        timeout = require_real(timeout_s, "finite phase timeout", minimum=0.01, maximum=600)
        deadline = min(self._scope() + timeout, self.budget.phase_deadline_s(phase))
        while True:
            if predicate():
                return
            now = self._scope()
            if now >= deadline:
                raise RuntimeError(f"{label} fixed phase deadline; no acceptance claimed")
            self.poll(min(0.05, deadline - now))

    def monitor_until_landed(self):
        """One total deadline; observed land/disarm is not route completion."""
        self._main()
        if self._status != "armed-mission-observed":
            raise RuntimeError("Monitor requires the owned observed armed Mission")
        while True:
            now = self._scope(starting=False)
            heartbeat = self._latest_row("HEARTBEAT", after=self._arm_at)
            if self._heartbeat(heartbeat, armed=False) and self._ground(after=self._arm_at):
                self._record("landed-disarmed-observed", heartbeat)
                self._status = "landed-disarmed-observed"
                return self.to_mapping()
            if now >= self.budget.phase_deadline_s("route") and not self._abort_called:
                # The existing finite RTL requires fresh exact armed Mission.
                # It does not force disarm or issue commands in another mode.
                return self.abort_return(phase_timeout_s=self.budget.rtl_land_s)
            self.poll(min(0.05, self.budget.phase_deadline_s("rtl_land") - now))

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": CONTRACT,
            "budget": self.budget.to_mapping(),
            "budget_sha256": self.budget.canonical_hash,
            "old_execution_intent_renewed": False,
            "route_completion_claimed": False,
        }


__all__ = ["TeacherMissionExecutionIntentV3", "TeacherMissionExecutionV3"]
