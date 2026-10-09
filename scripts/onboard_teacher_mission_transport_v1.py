"""Bounded teacher Mission transfer on the existing single RX/send owner.

Only upload/INT readback APIs are exposed. The caller supplies an already
authorized persistent transaction and explicit owned-SIM transfer intent. This
module never creates reports/grants, arms, selects modes, starts flight or
claims execution. Main-thread telemetry raw/projection commits precede every
returned protocol response. API return is not datagram delivery proof.
"""

from __future__ import annotations

import copy
import queue
import threading
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from types import SimpleNamespace

from multidetect.autonomy.serialization import (
    require_bool,
    require_int,
    require_real,
    require_sha256,
    require_text,
    strict_object,
)
from multidetect.mission_journal import (
    MissionJournalIdentity,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from multidetect.px4_external_mission import (
    ExternalRuntimeAttestation,
    _validated_gcs_identity,
)
from multidetect.px4_mission import PX4_MULTIROTOR_ADAPTER_ID, CompiledPx4Mission, MissionUploadPlan
from scripts.onboard_teacher_gazebo_session_v4 import (
    ConcurrentTelemetryReceiverV4,
    ReadOnlyDiagnosticSender,
)
from scripts.onboard_teacher_sim_operator import GcsHeartbeatTicker
from scripts.onboard_teacher_sim_operator_v2 import OwnedTeacherGazeboSessionV5
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3

CONTRACT = "multidetect.teacher-owned-Mission-transfer-v1"
GCS_IDENTITY = (255, 190)
_PROTOCOL_KINDS = frozenset(
    {
        "MISSION_REQUEST_INT",
        "MISSION_REQUEST",
        "MISSION_ACK",
        "MISSION_COUNT",
        "MISSION_ITEM_INT",
        "MISSION_ITEM",
    }
)
_MAX_SENDS = 512


@dataclass(frozen=True, slots=True)
class TeacherMissionTransferIntent:
    """Caller declaration for one transfer, not issuer authentication or a grant."""

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

    def __post_init__(self):
        for name in ("transaction_id", "image", "clock_id"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in (
            "container_id",
            "runtime_attestation_sha256",
            "executable_mission_sha256",
            "wire_sha256",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("approved_at_s", "expires_at_s"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        if not 0 < self.expires_at_s - self.approved_at_s <= 60:
            raise ValueError("Transfer intent must have a finite 60-second maximum lifetime")
        if require_bool(self.explicitly_authorized, "explicitly_authorized") is not True:
            raise ValueError("Explicit owned-SIM Mission transfer intent required")

    def to_mapping(self):
        return {
            "contract": CONTRACT,
            "scope": "owned-disposable-SIM-upload-readback-only",
            **asdict(self),
        }

    @classmethod
    def from_mapping(cls, raw):
        value = strict_object(
            raw,
            name="TeacherMissionTransferIntent",
            required=frozenset({"contract", "scope", *(f.name for f in fields(cls))}),
        )
        if (
            value["contract"] != CONTRACT
            or value["scope"] != "owned-disposable-SIM-upload-readback-only"
        ):
            raise ValueError("Wrong Mission transfer intent contract/scope")
        return cls(**{f.name: value[f.name] for f in fields(cls)})


@dataclass(frozen=True, slots=True)
class _SendCommand:
    method: str
    args: tuple
    state: MissionTransactionState
    requested_at_s: float
    deadline_s: float
    cancelled: threading.Event


class _ConcurrentReceiverV6(ConcurrentTelemetryReceiverV4):
    """Use the frozen receiver's command hook, never a second receive loop."""

    def request_mission(self, command, *, timeout_s):
        self._main()
        self.raise_if_failed()
        if (
            type(command) is not _SendCommand
            or self._thread is None
            or not self._thread.is_alive()
            or self._stop.is_set()
        ):
            raise RuntimeError("Live single RX/send owner and typed Mission command required")
        done, returned = threading.Event(), []
        try:
            self._commands.put_nowait(((command,), done, returned))
        except queue.Full as error:
            self._fail(error)
            command.cancelled.set()
            raise RuntimeError("Mission command queue exhausted; no retry") from error
        if not done.wait(timeout_s):
            command.cancelled.set()
            error = RuntimeError("Mission send timeout; delivery unknown, no retry")
            self._fail(error)
            raise error
        valid, result = returned[0]
        if not valid:
            raise result
        self.raise_if_failed()
        return result[0]


class _MavFacade:
    srcSystem, srcComponent = GCS_IDENTITY

    def __init__(self, transport):
        self._transport = transport

    def mission_count_send(self, *args):
        return self._transport._request("mission_count_send", args)

    def mission_item_int_send(self, *args):
        return self._transport._request("mission_item_int_send", args)

    def mission_request_list_send(self, *args):
        return self._transport._request("mission_request_list_send", args)

    def mission_request_int_send(self, *args):
        return self._transport._request("mission_request_int_send", args)


class _CommittedMessage:
    def __init__(self, row):
        self._fields = copy.deepcopy(row["message"])
        self.durable_receipt = copy.deepcopy(row)

    def get_type(self):
        return self._fields["message_type"]

    def get_srcSystem(self):
        return self._fields["source_system_id"]

    def get_srcComponent(self):
        return self._fields["source_component_id"]

    def __getattr__(self, name):
        if name not in self._fields:
            raise AttributeError(name)
        return self._fields[name]


class TeacherMissionTransport:
    """One bound transfer; main-thread consumer, RX-thread sender, no flight API."""

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
        remaining,
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
            type(capture) is not TeacherTelemetryCaptureV3
            or type(intent) is not TeacherMissionTransferIntent
        ):
            raise TypeError("Actual V3 journal capture and typed transfer intent required")
        if type(runtime_attestation) is not ExternalRuntimeAttestation:
            raise TypeError("Owned runtime attestation required")
        checked = runtime_attestation.to_mapping()
        checked.pop("attestation_sha256")
        if ExternalRuntimeAttestation.from_mapping(checked) != runtime_attestation:
            raise ValueError("Runtime attestation must be canonical")
        if CompiledPx4Mission.from_mapping(compiled.to_mapping()) != compiled:
            raise ValueError("Compiled Mission must re-open canonically")
        if TeacherMissionTransferIntent.from_mapping(intent.to_mapping()) != intent:
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
            or (capture.expected_system_id, capture.expected_component_id) != (1, 1)
            or clock_id != intent.clock_id
        ):
            raise ValueError("Mission transfer identity/scope/source binding mismatch")
        if (
            not callable(clock)
            or not callable(remaining)
            or (utc_clock is not None and not callable(utc_clock))
        ):
            raise TypeError("Actual observer/UTC clocks and owned deadline callback required")
        self._owner = threading.get_ident()
        receiver._main()
        capture._open()
        self._receiver, self._connection, self._compiled = receiver, connection, compiled
        self._transaction, self._capture, self.intent = transaction, capture, intent
        self._clock, self._remaining = clock, remaining
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

    def _main(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Mission transfer has one main-thread consumer")

    def _fail(self, error):
        with self._lock:
            self._failure = self._failure or f"{type(error).__name__}: {error}"[:1024]
        self._receiver._fail(error)

    def _check_live(self):
        with self._lock:
            if self._closed or self._failure is not None:
                raise RuntimeError("Mission transfer is closed or poisoned")
            now = require_real(self._clock(), "transfer clock", minimum=0)
            if self._last_at is not None and now < self._last_at:
                raise ValueError("Mission transfer observer clock regressed")
            self._last_at = now
            if not self.intent.approved_at_s <= now < self.intent.expires_at_s:
                raise ValueError("Mission transfer intent is future or expired")
            if require_real(self._remaining(), "owned remaining duration", minimum=0) <= 0:
                raise ValueError("Owned Mission transfer deadline exhausted")
            _validated_gcs_identity(self._connection, GCS_IDENTITY)
            self._receiver.raise_if_failed()
            if (
                self._receiver._stop.is_set()
                or self._receiver._thread is None
                or not self._receiver._thread.is_alive()
            ):
                raise RuntimeError("Live exclusive Mission RX/send owner required")
            self._transaction.transaction._validate_authorization(self._utc())
            if self._transaction.journal._closed:
                raise RuntimeError("Bound persistent Mission journal is closed")
            return now

    def _validate_send(self, method, args):
        state = self._transaction.state
        if (
            MissionJournalIdentity.from_transaction(self._transaction.transaction) != self._identity
            or self._transaction.journal.identity != self._identity
            or self._transaction.recovery.recovery_required
            or self._transaction.recovery.state is not state
            or self._compiled.wire_hash != self.intent.wire_sha256
            or self._compiled.executable_mission.canonical_hash
            != self.intent.executable_mission_sha256
        ):
            raise ValueError("Bound Mission transaction/compiled identity changed")
        plan = MissionUploadPlan(self._compiled.wire_items, self._compiled.wire_hash)
        if method == "mission_count_send":
            expected = plan.count_send_args()
            phase = MissionTransactionState.UPLOADING
        elif method == "mission_item_int_send":
            if (
                len(args) != len(plan.items[0].to_send_args())
                or type(args[2]) is not int
                or not 0 <= args[2] < plan.item_count
            ):
                raise ValueError("Mission item sequence/arity is outside compiled wire")
            expected = plan.items[args[2]].to_send_args()
            if args[2] not in self._transaction.recovery.requested_sequences:
                raise ValueError("Mission item lacks durable transaction request checkpoint")
            phase = MissionTransactionState.UPLOADING
        elif method == "mission_request_list_send":
            expected, phase = (1, 1, 0), MissionTransactionState.UPLOADED
        elif method == "mission_request_int_send":
            if len(args) != 4 or type(args[2]) is not int or not 0 <= args[2] < plan.item_count:
                raise ValueError("Readback sequence is outside compiled Mission")
            expected, phase = (1, 1, args[2], 0), MissionTransactionState.UPLOADED
        else:
            raise ValueError("Only four bounded Mission INT APIs permitted")
        if (
            state is not phase
            or len(args) != len(expected)
            or any(type(a) is not type(b) or a != b for a, b in zip(args, expected, strict=True))
        ):
            raise ValueError("Mission API phase or exact compiled arguments differ")
        return state

    def _check_capture_binding(self):
        self._main()
        self._capture._open()
        if (
            self._capture.transaction_id != self.intent.transaction_id
            or self._capture.clock_id != self.intent.clock_id
            or (self._capture.expected_system_id, self._capture.expected_component_id) != (1, 1)
        ):
            raise ValueError("Bound raw capture source/clock/transaction identity changed")

    def _request(self, method, args):
        self._main()
        try:
            now = self._check_live()
            self._check_capture_binding()
            phase = self._validate_send(method, args)
            if len(self._attempts) >= _MAX_SENDS:
                raise ValueError("Cumulative Mission send budget exhausted")
            command = _SendCommand(
                method,
                tuple(args),
                phase,
                now,
                min(now + self._timeout, self.intent.expires_at_s),
                threading.Event(),
            )
            with self._lock:
                if self._active_command is not None:
                    raise RuntimeError("Another Mission API is already in flight")
                self._active_command = command
            # Readback/transaction journal operations remain exclusively main-thread.
            self._receiver.request_mission(command, timeout_s=max(0.001, command.deadline_s - now))
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            with self._lock:
                self._active_command = None

    def _dispatch(self, commands):
        if (
            type(commands) is not tuple
            or len(commands) != 1
            or type(commands[0]) is not _SendCommand
        ):
            raise TypeError("One typed bound Mission send required")
        command = commands[0]
        owner = threading.get_ident()
        with self._lock:
            if owner == self._owner or (self._send_owner is not None and owner != self._send_owner):
                raise RuntimeError("Mission API requires the exclusive RX/send thread")
            if command is not self._active_command:
                raise RuntimeError("Mission command lacks main-thread durable validation")
            self._send_owner = owner
        now = self._check_live()
        if (
            command.cancelled.is_set()
            or now >= command.deadline_s
            or self._transaction.state is not command.state
        ):
            raise RuntimeError("Queued Mission command cancelled, late or wrong phase")
        evidence = {
            "API": command.method,
            "args": list(command.args),
            "API_owner_thread_id": owner,
            "requested_at_s": command.requested_at_s,
            "API_called_at_s": now,
            "API_attempted": True,
            "API_returned": False,
            "delivery_verified": False,
            "error": None,
            "transaction_id": self.intent.transaction_id,
        }
        with self._lock:
            self._attempts.append(evidence)
            self._last_send_at = now
        try:
            getattr(self._connection.mav, command.method)(*command.args)
            evidence["API_returned"] = True
            returned_at = self._check_live()
            evidence["API_returned_at_s"] = returned_at
            if command.cancelled.is_set() or returned_at >= command.deadline_s:
                raise RuntimeError("Mission API returned after timeout; delivery unknown")
            return [copy.deepcopy(evidence)]
        except BaseException as error:
            evidence["error"] = f"{type(error).__name__}: {error}"[:1024]
            self._fail(error)
            raise

    def _validate_protocol_fields(self, message):
        # The shared helper historically casts wire fields. This stricter new
        # adapter never turns a bool/string/fractional sequence into evidence.
        kind = message["message_type"]
        if require_int(message.get("mission_type"), "mission_type", minimum=0, maximum=255) != 0:
            raise ValueError("Only primary Mission responses accepted")
        if kind in {"MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ITEM_INT", "MISSION_ITEM"}:
            require_int(
                message.get("seq"),
                "Mission sequence",
                minimum=0,
                maximum=len(self._compiled.wire_items) - 1,
            )
        if kind == "MISSION_ACK":
            require_int(message.get("type"), "Mission ACK type", minimum=0, maximum=255)
        elif kind == "MISSION_COUNT":
            require_int(message.get("count"), "Mission count", minimum=0, maximum=64)
        elif kind == "MISSION_ITEM_INT":
            for name, minimum, maximum in (
                ("frame", 0, 255),
                ("command", 0, 65535),
                ("current", 0, 1),
                ("autocontinue", 0, 1),
                ("x", -(2**31), 2**31 - 1),
                ("y", -(2**31), 2**31 - 1),
            ):
                require_int(message.get(name), name, minimum=minimum, maximum=maximum)
            for name in ("param1", "param2", "param3", "param4", "z"):
                require_real(message.get(name), name)

    def receive(self, timeout_s):
        self._main()
        try:
            now = self._check_live()
            self._check_capture_binding()
            timeout = require_real(timeout_s, "protocol timeout_s", minimum=0, maximum=30)
            deadline = min(now + timeout, self.intent.expires_at_s)
            while True:
                while self._pending:
                    row = self._pending.popleft()
                    current = self._check_live()
                    if (
                        self._last_send_at is None
                        or row["received_at_s"] < self._last_send_at
                        or not 0 <= current - row["received_at_s"] <= self._response_age
                    ):
                        self._counts["stale_protocol_rows"] += 1
                        continue
                    self._counts["returned_protocol_rows"] += 1
                    return _CommittedMessage(row)
                current = self._check_live()
                if current >= deadline:
                    return None
                rows = self._receiver.read_batch(min(0.05, deadline - current))
                if not rows:
                    continue
                committed = self._capture.append_batch(rows)
                self._check_live()
                for row in committed:
                    self._counts["committed_rows"] += 1
                    if row["source_accepted"] is not True or (
                        row["message"]["source_system_id"],
                        row["message"]["source_component_id"],
                    ) != (1, 1):
                        self._counts["foreign_source_rows"] += 1
                        continue
                    if row["message"]["message_type"] not in _PROTOCOL_KINDS:
                        continue
                    target = (
                        row["message"].get("target_system"),
                        row["message"].get("target_component"),
                    )
                    if any(type(v) is not int or not 0 <= v <= 255 for v in target):
                        raise ValueError("Committed protocol target is malformed")
                    if target != GCS_IDENTITY:
                        self._counts["foreign_target_rows"] += 1
                        continue
                    self._validate_protocol_fields(row["message"])
                    if len(self._pending) >= 256:
                        raise ValueError("Committed Mission response queue exhausted")
                    self._pending.append(row)
        except BaseException as error:
            self._fail(error)
            raise

    def cancel(self):
        self._main()
        self._closed = True
        self._fail(RuntimeError("Mission transport explicitly cancelled"))

    def close(self):
        """Graceful stop; a normal lifecycle close is not a failure receipt."""
        self._main()
        with self._lock:
            self._closed = True
            if self._active_command is not None:
                self._active_command.cancelled.set()
        self._receiver._stop.set()

    def to_mapping(self):
        with self._lock:
            return {
                "contract": CONTRACT,
                "intent": self.intent.to_mapping(),
                "failure": self._failure,
                "closed": self._closed,
                "counts": dict(self._counts),
                "API_attempts": copy.deepcopy(self._attempts),
                "send_owner_thread_id": self._send_owner,
                "pending_protocol_rows": len(self._pending),
                "claims": {
                    name: False
                    for name in (
                        "grant_created",
                        "arm_sent",
                        "mode_change_sent",
                        "flight_started",
                        "flight_executed",
                        "physical_output_enabled",
                        "datagram_delivery_independently_verified",
                    )
                },
            }


class OwnedTeacherGazeboSessionV6(OwnedTeacherGazeboSessionV5):
    """Same owned lifecycle/camera/GCS/unique RX, plus explicit transfer binding."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mission_transport = None

    def start_receiver(self, queue_capacity=256):
        if (
            self.receiver is not None
            or threading.get_ident() != self._owner
            or not self.execute
            or self._closed
            or self.connection is None
        ):
            raise RuntimeError("Opened owned session and one receiver transfer required")
        self._gcs = GcsHeartbeatTicker(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )
        self._diagnostic_sender = ReadOnlyDiagnosticSender(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )

        def reader(timeout):
            if self.connection.recv_match != self._receive or self._closed:
                raise RuntimeError("Original transferred receive path differs")
            self._gcs.tick()
            message = self._receive(blocking=True, timeout=min(timeout, self._remaining()))
            self._remaining()
            return message

        def sender(requests):
            if requests and type(requests[0]) is _SendCommand:
                if self.mission_transport is None:
                    raise RuntimeError("Mission transfer not bound")
                return self.mission_transport._dispatch(requests)
            return self._diagnostic_sender(requests)

        self.receiver = _ConcurrentReceiverV6(
            reader, self.clock.monotonic, request_sender=sender, queue_capacity=queue_capacity
        ).start()
        return self.receiver

    def bind_mission_transport(
        self,
        *,
        transaction,
        compiled,
        capture,
        intent,
        runtime_attestation,
        utc_clock=None,
        **bounds,
    ):
        if (
            threading.get_ident() != self._owner
            or self.mission_transport is not None
            or self._closed
            or not self.execute
        ):
            raise RuntimeError("One main-thread opened-session Mission binding required")
        if type(runtime_attestation) is not ExternalRuntimeAttestation:
            raise TypeError("Caller source-qualified runtime attestation required")
        # Re-inspect current owned Docker identity, not a fixture issuer or a
        # new qualification. The caller owns observed-context qualification.
        if runtime_attestation != self.attest_qualified(
            telemetry_qualified=runtime_attestation.telemetry_qualified
        ):
            raise ValueError("Actual current owned runtime differs from supplied attestation")
        self.mission_transport = TeacherMissionTransport(
            receiver=self.receiver,
            connection=self.connection,
            compiled=compiled,
            transaction=transaction,
            capture=capture,
            intent=intent,
            runtime_attestation=runtime_attestation,
            clock=self.clock.monotonic,
            clock_id=self.clock.session_id,
            remaining=self._remaining,
            utc_clock=utc_clock,
            **bounds,
        )
        return self.mission_transport

    def _finish(self, primary):
        if self.mission_transport is not None and not self.mission_transport._closed:
            self.mission_transport.close()
        super()._finish(primary)

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": "multidetect.onboard-teacher-gazebo-session-v6",
            "mission_transport": None
            if self.mission_transport is None
            else self.mission_transport.to_mapping(),
        }


__all__ = ["TeacherMissionTransferIntent", "TeacherMissionTransport", "OwnedTeacherGazeboSessionV6"]
