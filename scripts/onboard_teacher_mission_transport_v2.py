"""NEW lifecycle-scoped Mission INT transport; no new authority or receiver."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from types import SimpleNamespace

from multidetect.autonomy.serialization import (
    require_bool,
    require_real,
    require_sha256,
    require_text,
    strict_object,
)
from multidetect.mission_journal import MissionJournalIdentity, PersistentPx4MissionTransaction
from multidetect.px4_external_mission import ExternalRuntimeAttestation
from multidetect.px4_mission import PX4_MULTIROTOR_ADAPTER_ID, CompiledPx4Mission
from scripts import onboard_teacher_mission_transport_v1 as frozen
from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget
from scripts.onboard_teacher_telemetry_capture_v4 import TeacherTelemetryCaptureV4

_ConcurrentReceiverV6 = frozen._ConcurrentReceiverV6
_MavFacade = frozen._MavFacade
CONTRACT = "multidetect.teacher-owned-Mission-transfer-v2"


@dataclass(frozen=True, slots=True)
class TeacherMissionTransferIntentV2:
    transaction_id: str
    container_id: str
    image: str
    runtime_attestation_sha256: str
    executable_mission_sha256: str
    wire_sha256: str
    clock_id: str
    approved_at_s: float
    expires_at_s: float
    explicitly_authorized: bool
    budget_sha256: str

    def __post_init__(self):
        for name in ("transaction_id", "image", "clock_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in (
            "container_id",
            "runtime_attestation_sha256",
            "executable_mission_sha256",
            "wire_sha256",
            "budget_sha256",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("approved_at_s", "expires_at_s"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        if not 0 < self.expires_at_s - self.approved_at_s <= 600:
            raise ValueError("NEW once-bound lifecycle intent must fit ten minutes")
        if require_bool(self.explicitly_authorized, "explicitly_authorized") is not True:
            raise ValueError("Explicit owned-SIM transfer declaration required; not a Grant")

    def to_mapping(self):
        return {
            "contract": CONTRACT,
            "scope": "owned-disposable-SIM-lifecycle-upload-readback-only",
            **asdict(self),
        }

    @classmethod
    def from_mapping(cls, raw):
        value = strict_object(
            raw,
            name="TeacherMissionTransferIntentV2",
            required=frozenset({"contract", "scope", *(f.name for f in fields(cls))}),
        )
        if (
            value["contract"] != CONTRACT
            or value["scope"] != "owned-disposable-SIM-lifecycle-upload-readback-only"
        ):
            raise ValueError("NEW lifecycle transfer intent contract differs")
        return cls(**{f.name: value[f.name] for f in fields(cls)})


class TeacherMissionTransportV2(frozen.TeacherMissionTransport):
    def __init__(
        self,
        *,
        receiver,
        connection,
        compiled,
        transaction,
        capture,
        intent,
        runtime_attestation,
        clock,
        clock_id,
        budget,
        utc_clock=None,
        send_timeout_s=1.0,
        response_maximum_age_s=2.0,
    ):
        if type(receiver) is not _ConcurrentReceiverV6:
            raise TypeError("Teacher V6 single receiver required")
        if (
            type(transaction) is not PersistentPx4MissionTransaction
            or type(compiled) is not CompiledPx4Mission
        ):
            raise TypeError("Bound persistent Mission transaction and compiled wire required")
        if (
            type(capture) is not TeacherTelemetryCaptureV4
            or type(intent) is not TeacherMissionTransferIntentV2
        ):
            raise TypeError("Actual V4 journal capture and NEW lifecycle transfer intent required")
        if type(budget) is not TeacherSortieLifecycleBudget:
            raise TypeError("Exact original lifecycle budget required")
        if receiver.capacity != budget.queue_capacity or receiver.clock != clock:
            raise ValueError("Original lifecycle queue capacity/clock differs")
        if type(runtime_attestation) is not ExternalRuntimeAttestation:
            raise TypeError("Owned runtime attestation required")
        checked = runtime_attestation.to_mapping()
        checked.pop("attestation_sha256")
        if ExternalRuntimeAttestation.from_mapping(checked) != runtime_attestation:
            raise ValueError("Runtime attestation must be canonical")
        if CompiledPx4Mission.from_mapping(compiled.to_mapping()) != compiled:
            raise ValueError("Compiled Mission must re-open canonically")
        if TeacherMissionTransferIntentV2.from_mapping(intent.to_mapping()) != intent:
            raise ValueError("Mission transfer intent must be canonical")
        identity = MissionJournalIdentity.from_transaction(transaction.transaction)
        if identity != transaction.journal.identity or transaction.recovery.recovery_required:
            raise ValueError("Persistent transaction journal identity/recovery differs")
        if (
            identity.transaction_id != intent.transaction_id
            or identity.executable_mission_hash != compiled.executable_mission.canonical_hash
            or identity.protocol_hash != compiled.wire_hash
            or intent.executable_mission_sha256 != identity.executable_mission_hash
            or intent.wire_sha256 != identity.protocol_hash
            or runtime_attestation.canonical_hash != intent.runtime_attestation_sha256
            or runtime_attestation.container_id != intent.container_id
            or runtime_attestation.image != intent.image
            or runtime_attestation.vehicle_class != "multirotor"
            or identity.airframe_adapter_id != PX4_MULTIROTOR_ADAPTER_ID
            or capture.transaction_id != intent.transaction_id
            or capture.clock_id != intent.clock_id
            or type(budget) is not TeacherSortieLifecycleBudget
            or capture.budget != budget
            or capture._bound_clock != clock
            or budget.clock_id != clock_id
            or intent.budget_sha256 != budget.canonical_hash
            or intent.approved_at_s != budget.started_at_s
            or intent.expires_at_s != budget.deadline_s
            or (capture.expected_system_id, capture.expected_component_id) != (1, 1)
            or clock_id != intent.clock_id
        ):
            raise ValueError("Mission transfer identity/scope/source binding mismatch")
        if not callable(clock) or (utc_clock is not None and not callable(utc_clock)):
            raise TypeError("Actual observer/UTC clocks and owned deadline callback required")
        self._owner = threading.get_ident()
        receiver._main()
        capture._open()
        self._receiver, self._connection, self._compiled = receiver, connection, compiled
        self._transaction, self._capture, self.intent = transaction, capture, intent
        self.budget = budget
        self._budget_sha256 = budget.canonical_hash
        self._clock = clock
        self._remaining = lambda: budget.remaining_s(clock(), clock_id=clock_id)
        self._utc = utc_clock or (lambda: datetime.now(timezone.utc))
        self._timeout = require_real(send_timeout_s, "send_timeout_s", minimum=0.01, maximum=2)
        self._response_age = require_real(
            response_maximum_age_s, "response_maximum_age_s", minimum=0.01, maximum=2
        )
        self._pending, self._attempts, self._lock = deque(), [], threading.RLock()
        self._failure, self._closed, self._last_at = None, False, None
        self._send_owner, self._last_send_at = None, None
        self._active_command = None
        self._identity = identity
        self._counts = {
            "committed_rows": 0,
            "foreign_source_rows": 0,
            "foreign_target_rows": 0,
            "stale_protocol_rows": 0,
            "returned_protocol_rows": 0,
        }
        self.connection = SimpleNamespace(
            source_system=255, source_component=190, mav=_MavFacade(self)
        )
        self._check_live()

    def _check_live(self):
        now = super()._check_live()
        if (
            self.budget.canonical_hash != self._budget_sha256
            or self._capture.budget != self.budget
            or self._capture._bound_clock != self._clock
        ):
            raise ValueError("Original lifecycle writer/budget/clock changed")
        self.budget.assert_phase("rtl_land", now_s=now, clock_id=self.intent.clock_id)
        return now

    def _validate_send(self, method, args):
        self.budget.assert_phase(
            "transfer", now_s=self._check_live(), clock_id=self.intent.clock_id
        )
        return super()._validate_send(method, args)

    def _dispatch(self, commands):
        # RX rechecks the fixed transfer deadline after queue latency.
        self.budget.assert_phase(
            "transfer", now_s=self._check_live(), clock_id=self.intent.clock_id
        )
        return super()._dispatch(commands)

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": CONTRACT,
            "budget": self.budget.to_mapping(),
            "budget_sha256": self._budget_sha256,
            "old_intent_renewed": False,
        }


__all__ = ["TeacherMissionTransferIntentV2", "TeacherMissionTransportV2"]
