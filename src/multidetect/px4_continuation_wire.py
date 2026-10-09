"""Explicit, owned-SIM continuation wire phases; no connection or grant creation.

One host owns the connection, its receive pump, and the phase admission callback.
This one-shot session never arms, changes parameters, clears a Mission, or sends
an automatic fallback. Cancellation invalidates the generation and leaves the
vehicle for the host/operator to reconcile. A send already in progress cannot be
undone. The receive callback must honor its bounded timeout; it is not run under
the writer lock. These receipts are observed wire evidence, not flight authority.
"""

from __future__ import annotations

import base64
import hashlib
import re
import select
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from typing import Any

from .autonomy.onboard_continuation import CONTINUATION_ADAPTER_HASH, CONTINUATION_ADAPTER_ID
from .autonomy.serialization import canonical_json, require_real, require_text
from .compat import StrEnum
from .mission_journal import (
    MissionJournalIdentity,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from .px4_external_mission import (
    ExternalRuntimeAttestation,
    readback_mission_int,
    upload_mission_int,
)
from .px4_mission import MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, PX4_AUTO_MISSION_CUSTOM_MODE

PX4_AUTO_LOITER_CUSTOM_MODE = (4 << 16) | (3 << 24)
_ARMED_FLAG = 128
_ALLOWED_SENDS = frozenset(
    {
        "set_mode_send",
        "mission_count_send",
        "mission_item_int_send",
        "mission_request_list_send",
        "mission_request_int_send",
        "mission_set_current_send",
    }
)


class ContinuationWireError(RuntimeError):
    """A phase failed or no longer has host permission to send."""


class ContinuationWireState(StrEnum):
    READY = "ready"
    PAUSING = "pausing"
    PAUSED = "paused"
    INSTALLING = "installing"
    INSTALLED = "installed"
    SELECTING = "selecting"
    CURSOR_SELECTED = "cursor_selected"
    RESUMING = "resuming"
    RESUMED = "resumed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class ContinuationWireReceipt:
    generation: int
    phase: str
    observed_at_s: float
    clock_epoch: str
    source_system_id: int
    source_component_id: int
    message_type: str
    message_json: str
    wire_bytes: bytes
    receive_sequence: int
    receive_kind: str

    def to_mapping(self) -> dict[str, Any]:
        import json

        return {
            "generation": self.generation,
            "phase": self.phase,
            "observed_at_s": self.observed_at_s,
            "clock_epoch": self.clock_epoch,
            "source_system_id": self.source_system_id,
            "source_component_id": self.source_component_id,
            "message_type": self.message_type,
            "message": json.loads(self.message_json),
            "wire_bytes_base64": base64.b64encode(self.wire_bytes).decode("ascii"),
            "wire_bytes": len(self.wire_bytes),
            "wire_sha256": hashlib.sha256(self.wire_bytes).hexdigest(),
            "receive_sequence": self.receive_sequence,
            "receive_kind": self.receive_kind,
        }


class _GuardedMav:
    def __init__(self, session: Px4ContinuationWireSession, generation: int):
        self.session, self.generation = session, generation

    def __getattr__(self, name: str):
        if name not in _ALLOWED_SENDS:
            raise ContinuationWireError(f"wire method is forbidden: {name}")

        def send(*args):
            return self.session._send(self.generation, name, args)

        return send


class _FreshTransaction:
    """Only the narrow public methods called by the frozen wire primitives."""

    def __init__(self, session: Px4ContinuationWireSession, generation: int):
        self.session, self.generation = session, generation

    def __getattr__(self, name: str):
        if name not in {
            "begin_upload",
            "mission_item_for_request",
            "accept_upload_ack",
            "verify_readback",
            "commit",
            "permit_start",
        }:
            raise ContinuationWireError(f"transaction method is forbidden: {name}")

        def call(*args, **kwargs):
            with self.session._lock:
                self.session._guard(self.generation)
                now = self.session._utc_now()
                child = self.session._child
                assert child is not None
                # The core has no public expiry-only API. Check its existing
                # guard without rewriting the grant or restoring any state.
                child.transaction._check_expiry(now)
                kwargs["now_utc"] = now
                result = getattr(child, name)(*args, **kwargs)
                self.session._guard(self.generation)
                return result

        return call


class Px4ContinuationWireSession:
    """Four separately admitted phases on one already-owned loopback socket.

    ``phase_guard(phase, generation)`` must return exactly True before every
    write/transaction transition. It remains the host's responsibility to bind
    current/source admission, authoring evidence, clearance, and authorization.
    Constructor flags and runtime attestations do not manufacture these proofs.
    The receive pump must be the connection's sole reader. Direct external
    writes/reads must not run concurrently with this session.
    ``receive(0.0)`` MUST be a nonblocking read returning None only when the
    current reader/socket queue is empty. Before a new control/protocol request
    that queue is boundedly drained. This is a host queue cut, NOT proof that
    PX4 processed a particular command (mode telemetry has no command ID).
    For pymavlink this means ``recv_match(blocking=False)`` WITHOUT timeout=0;
    its zero timeout can expire before attempting a read.
    Every send, including ITEM responses, has this cut. This conservatively
    assumes PX4's one-request/one-response Mission protocol. Pipelined or
    duplicate queued protocol facts are kept as raw receipts but not replayed
    as requests; availability may require a bounded retry or host recovery.
    """

    def __init__(
        self,
        connection: Any,
        *,
        receive: Callable[[float], Any | None],
        host_clock: Callable[[], float],
        utc_clock: Callable[[], datetime],
        clock_epoch: str,
        endpoint: str,
        runtime_attestation: ExternalRuntimeAttestation,
        writes_enabled: bool = False,
        phase_guard: Callable[[str, int], bool] | None = None,
        phase_timeout_s: float = 3.0,
        maximum_fact_age_s: float = 1.0,
        total_timeout_s: float = 30.0,
    ) -> None:
        if type(writes_enabled) is not bool:
            raise ContinuationWireError("writes_enabled must be a boolean")
        if not all(callable(value) for value in (receive, host_clock, utc_clock)):
            raise ContinuationWireError("explicit receive and both clocks are required")
        if phase_guard is not None and not callable(phase_guard):
            raise ContinuationWireError("phase_guard must be callable")
        if writes_enabled and phase_guard is None:
            raise ContinuationWireError("enabled writes require host phase admission")
        match = re.fullmatch(r"udpin:127\.0\.0\.1:([0-9]{4,5})", endpoint)
        port = int(match[1]) if match else 0
        if not 1024 <= port <= 65535 or port == 14550:
            raise ContinuationWireError("owned loopback endpoint must not be protected 14550")
        sock = getattr(connection, "port", None)
        if (
            not isinstance(sock, socket.socket)
            or sock.family != socket.AF_INET
            or sock.type != socket.SOCK_DGRAM
            or sock.getsockname() != ("127.0.0.1", port)
            or getattr(connection, "address", None) != f"127.0.0.1:{port}"
            or getattr(connection, "udp_server", None) is not True
            or getattr(connection, "broadcast", None) is not False
        ):
            raise ContinuationWireError("actual connection is not the declared loopback UDP socket")
        mav = getattr(connection, "mav", None)
        if (getattr(mav, "srcSystem", None), getattr(mav, "srcComponent", None)) != (250, 191):
            raise ContinuationWireError("registered GCS sender must be 250/191")
        if not isinstance(runtime_attestation, ExternalRuntimeAttestation):
            raise ContinuationWireError("owned SIM runtime attestation is required")
        attestation = runtime_attestation.to_mapping()
        attestation.pop("attestation_sha256")
        ExternalRuntimeAttestation.from_mapping(attestation)
        if runtime_attestation.vehicle_class != "multirotor":
            raise ContinuationWireError("continuation wire is multirotor SIM only")
        self.connection, self.receive = connection, receive
        self._socket, self._sender, self._endpoint_port = sock, mav, port
        self.host_clock, self.utc_clock = host_clock, utc_clock
        self.clock_epoch = require_text(clock_epoch, "clock_epoch")
        self.phase_timeout_s = require_real(
            phase_timeout_s, "phase_timeout_s", minimum=0.01, maximum=30.0
        )
        self.maximum_fact_age_s = require_real(
            maximum_fact_age_s, "maximum_fact_age_s", minimum=0.001, maximum=1.0
        )
        self.total_timeout_s = require_real(
            total_timeout_s, "total_timeout_s", minimum=0.01, maximum=120.0
        )
        self.runtime_attestation = runtime_attestation
        self.writes_enabled, self.phase_guard = writes_enabled, phase_guard
        self._lock = RLock()
        self._generation = 0
        self._state = ContinuationWireState.READY
        self._phase = "ready"
        self._last_clock_s: float | None = None
        self._last_utc: datetime | None = None
        self._receipts: list[ContinuationWireReceipt] = []
        self._sends: list[dict[str, Any]] = []
        self._heartbeat: ContinuationWireReceipt | None = None
        self._in_air: ContinuationWireReceipt | None = None
        self._cursor: ContinuationWireReceipt | None = None
        self._child: PersistentPx4MissionTransaction | None = None
        self._child_identity_sha256: str | None = None
        self._child_grant_sha256: str | None = None
        self._permit = None
        self._recovery_reason: str | None = None
        self._phase_deadline_s: float | None = None
        self._session_deadline_s: float | None = None
        with self._lock:
            self._now()
            self._utc_now()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def state(self) -> ContinuationWireState:
        with self._lock:
            return self._state

    @property
    def receipts(self) -> tuple[ContinuationWireReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def cancel(self, *, expected_generation: int, reason: str = "host_cancelled") -> bool:
        with self._lock:
            if type(expected_generation) is not int or expected_generation != self._generation:
                return False
            self._fail(require_text(reason, "reason"))
            return True

    def _fail(self, reason: str) -> None:
        if self._state is not ContinuationWireState.RECOVERY_REQUIRED:
            self._generation += 1
            self._recovery_reason = reason
        self._state = ContinuationWireState.RECOVERY_REQUIRED

    def _now(self) -> float:
        now = require_real(self.host_clock(), "host clock", minimum=0)
        if self._last_clock_s is not None and now < self._last_clock_s:
            raise ContinuationWireError("host receive clock moved backwards")
        self._last_clock_s = now
        return now

    def _utc_now(self) -> datetime:
        now = self.utc_clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ContinuationWireError("UTC clock must return an aware datetime")
        now = now.astimezone(timezone.utc)
        if self._last_utc is not None and now < self._last_utc:
            raise ContinuationWireError("UTC authorization clock moved backwards")
        self._last_utc = now
        return now

    def _check_generation_and_deadline(self, generation: int) -> None:
        if (
            type(generation) is not int
            or generation != self._generation
            or self._state is ContinuationWireState.RECOVERY_REQUIRED
        ):
            raise ContinuationWireError("generation cancelled or recovery required")
        now = self._now()
        for deadline in (self._phase_deadline_s, self._session_deadline_s):
            if deadline is not None and now >= deadline:
                self._fail("absolute wire phase/session deadline expired")
                raise ContinuationWireError("absolute wire phase/session deadline expired")

    def _check_transport(self) -> None:
        if (
            self.connection.port is not self._socket
            or self.connection.mav is not self._sender
            or self._socket.getsockname() != ("127.0.0.1", self._endpoint_port)
            or self.connection.address != f"127.0.0.1:{self._endpoint_port}"
            or self.connection.udp_server is not True
            or self.connection.broadcast is not False
            or (self._sender.srcSystem, self._sender.srcComponent) != (250, 191)
        ):
            raise ContinuationWireError("actual transport/GCS identity drift")

    def _guard(self, generation: int, *, host_admission: bool = True) -> None:
        self._check_generation_and_deadline(generation)
        if not self.writes_enabled:
            raise ContinuationWireError("wire writes are disabled")
        self._check_transport()
        if host_admission and (
            self.phase_guard is None or self.phase_guard(self._phase, generation) is not True
        ):
            raise ContinuationWireError("host phase admission rejected")
        # A guard can synchronously revoke this session. RLock is deliberate.
        self._check_generation_and_deadline(generation)
        self._check_transport()
        if self._child is not None:
            if (
                self._child.journal.identity.canonical_hash != self._child_identity_sha256
                or MissionJournalIdentity.from_transaction(self._child.transaction).canonical_hash
                != self._child_identity_sha256
                or self._child.transaction._operator_grant.canonical_hash
                != self._child_grant_sha256
            ):
                raise ContinuationWireError("child transaction identity drift")
            if self._child.journal.recovery_required:
                raise ContinuationWireError("child journal requires recovery")
            self._child.transaction._check_expiry(self._utc_now())

    def _send(self, generation: int, name: str, args: tuple[Any, ...]):
        if name not in _ALLOWED_SENDS:
            raise ContinuationWireError("forbidden sender")
        self._drain_before_send(generation)
        with self._lock:
            self._guard(generation)
            self._require_flight_state(
                PX4_AUTO_MISSION_CUSTOM_MODE
                if self._phase == "pause"
                else PX4_AUTO_LOITER_CUSTOM_MODE
            )
            if self._phase == "resume":
                assert self._cursor is not None
                if self._cursor.to_mapping()["message"]["seq"] != 1:
                    raise ContinuationWireError("selected body cursor drifted before actual resume")
            sender = getattr(self.connection.mav, name, None)
            if not callable(sender):
                raise ContinuationWireError(f"missing MAVLink sender {name}")
            sent_utc = self._utc_now()
            entry = {
                "generation": generation,
                "phase": self._phase,
                "method": name,
                "args": list(args),
                "sent_at_s": self._now(),
                "sent_at_utc": sent_utc.isoformat(),
                "receive_cut_sequence": len(self._receipts),
                "call_completed": False,
            }
            self._check_generation_and_deadline(generation)
            self._check_transport()
            self._require_flight_state(
                PX4_AUTO_MISSION_CUSTOM_MODE
                if self._phase == "pause"
                else PX4_AUTO_LOITER_CUSTOM_MODE
            )
            if self._phase == "resume" and self._cursor.to_mapping()["message"]["seq"] != 1:
                raise ContinuationWireError("selected body cursor drifted at actual resume")
            if self._child is not None:
                self._child.transaction._check_expiry(sent_utc)
            self._sends.append(entry)
            result = sender(*args)
            entry["call_completed"] = True
            self._guard(generation)
            return result

    def _drain_before_send(self, generation: int) -> None:
        # Never hold the writer lock around even a nonblocking receive. The
        # callback owns the real reader; timeout zero is its explicit empty cut.
        for _ in range(64):
            with self._lock:
                self._guard(generation, host_admission=False)
            message = self.receive(0.0)
            with self._lock:
                self._guard(generation, host_admission=False)
                if message is None:
                    # A broken zero-timeout callback can return None before
                    # reading anything. Never call a still-readable owned UDP
                    # socket an empty cut. A concurrently arriving packet is
                    # drained on the next bounded iteration as well.
                    if select.select([self._socket], [], [], 0)[0]:
                        continue
                    return
                self._record_message(generation, message, receive_kind="pre_send_drain")
        raise ContinuationWireError("pre-send receive queue did not reach a bounded empty cut")

    def _receive(self, generation: int, timeout_s: float):
        with self._lock:
            self._guard(generation, host_admission=False)
            deadline = self._now() + timeout_s
        # Never hold the writer/CAS lock while waiting on the sole receive pump.
        message = None
        while message is None:
            with self._lock:
                self._guard(generation, host_admission=False)
                remaining = deadline - self._now()
            if remaining <= 0:
                return None
            message = self.receive(min(remaining, 0.1))
        with self._lock:
            self._guard(generation, host_admission=False)
            if message is None:
                return None
            return self._record_message(generation, message, receive_kind="phase_receive")

    def _record_message(self, generation: int, message: Any, *, receive_kind: str):
        with self._lock:
            self._guard(generation, host_admission=False)
            now = self._now()
            getter = getattr(message, "get_msgbuf", None)
            wire = getter() if callable(getter) else None
            if not isinstance(wire, (bytes, bytearray)) or not 1 <= len(wire) <= 4096:
                raise ContinuationWireError("actual original MAVLink bytes are required")
            # Reparse the actual packet, rather than trusting fake decoded fields
            # or a claimed source attached to unrelated packet bytes.
            from pymavlink.dialects.v20 import common

            decoded = common.MAVLink(None).parse_buffer(bytes(wire))
            if decoded is None or len(decoded) != 1:
                raise ContinuationWireError("receipt must contain exactly one MAVLink packet")
            actual = decoded[0]
            if canonical_json(actual.to_dict()) != canonical_json(message.to_dict()):
                raise ContinuationWireError("decoded fields differ from original wire bytes")
            source = (actual.get_srcSystem(), actual.get_srcComponent())
            if source != (message.get_srcSystem(), message.get_srcComponent()) or source != (1, 1):
                raise ContinuationWireError("actual PX4 source must be 1/1")
            receipt = ContinuationWireReceipt(
                generation,
                self._phase,
                now,
                self.clock_epoch,
                *source,
                actual.get_type(),
                canonical_json(actual.to_dict()),
                bytes(wire),
                len(self._receipts) + 1,
                receive_kind,
            )
            self._receipts.append(receipt)
            if len(self._receipts) > 8192:
                raise ContinuationWireError("bounded receipt capacity exceeded")
            if receipt.message_type == "HEARTBEAT":
                self._heartbeat = receipt
                if self._phase in {"install", "cursor"}:
                    self._require_heartbeat(PX4_AUTO_LOITER_CUSTOM_MODE, receipt)
            elif receipt.message_type == "EXTENDED_SYS_STATE":
                if actual.landed_state != 2:
                    raise ContinuationWireError("actual PX4 is no longer in air")
                self._in_air = receipt
            elif receipt.message_type == "MISSION_CURRENT":
                if type(actual.seq) is not int or not 0 <= actual.seq < 64:
                    raise ContinuationWireError("actual Mission cursor is outside the bound")
                self._cursor = receipt
            return actual

    def _require_heartbeat(self, mode: int, receipt: ContinuationWireReceipt) -> None:
        fields = receipt.to_mapping()["message"]
        if (
            fields.get("custom_mode") != mode
            or type(fields.get("base_mode")) is not int
            or fields["base_mode"] & (_ARMED_FLAG | MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
            != (_ARMED_FLAG | MAV_MODE_FLAG_CUSTOM_MODE_ENABLED)
            or fields.get("autopilot") != 12
            or fields.get("type") != 2
        ):
            raise ContinuationWireError("fresh armed PX4 multirotor exact mode proof required")

    def _require_flight_state(
        self, mode: int, *, earliest_s: float | None = None, after_sequence: int | None = None
    ) -> None:
        now = self._now()
        for fact in (self._heartbeat, self._in_air, self._cursor):
            if (
                fact is None
                or fact.generation != self._generation
                or not 0 <= now - fact.observed_at_s <= self.maximum_fact_age_s
                or (earliest_s is not None and fact.observed_at_s < earliest_s)
                or (after_sequence is not None and fact.receive_sequence <= after_sequence)
            ):
                raise ContinuationWireError(
                    "fresh heartbeat, IN_AIR and MISSION_CURRENT facts required"
                )
        assert self._heartbeat is not None
        self._require_heartbeat(mode, self._heartbeat)

    def _await_flight_state(
        self, generation: int, mode: int, earliest_s: float, *, after_sequence: int | None = None
    ) -> None:
        deadline = self._now() + self.phase_timeout_s
        while self._now() < deadline:
            self._receive(generation, max(0.001, deadline - self._now()))
            try:
                self._require_flight_state(
                    mode, earliest_s=earliest_s, after_sequence=after_sequence
                )
            except ContinuationWireError:
                continue
            return
        raise ContinuationWireError("mode/IN_AIR confirmation timed out")

    def _run_phase(self, generation: int, phase: str, before, during, after, operation):
        try:
            with self._lock:
                self._check_generation_and_deadline(generation)
                if self._state is not before:
                    raise ContinuationWireError(f"phase {phase} cannot run in {self._state.value}")
                self._phase = phase
                self._state = during
                now = self._now()
                if self._session_deadline_s is None:
                    self._session_deadline_s = now + self.total_timeout_s
                self._phase_deadline_s = min(now + self.phase_timeout_s, self._session_deadline_s)
                self._guard(generation)
                start_index = len(self._receipts)
            result = operation()
            with self._lock:
                self._guard(generation)
                self._state = after
                self._phase_deadline_s = None
                return {
                    "phase": phase,
                    "generation": generation,
                    "state": after.value,
                    "result": result,
                    "receipts": [item.to_mapping() for item in self._receipts[start_index:]],
                }
        except BaseException as exc:
            with self._lock:
                self._fail(f"{type(exc).__name__}: {exc}")
            raise

    def pause_and_confirm(self, *, expected_generation: int) -> dict[str, Any]:
        def pause():
            self._await_flight_state(expected_generation, PX4_AUTO_MISSION_CUSTOM_MODE, self._now())
            self._send(
                expected_generation,
                "set_mode_send",
                (1, MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, PX4_AUTO_LOITER_CUSTOM_MODE),
            )
            sent = self._sends[-1]["sent_at_s"]
            self._await_flight_state(
                expected_generation,
                PX4_AUTO_LOITER_CUSTOM_MODE,
                sent,
                after_sequence=self._sends[-1]["receive_cut_sequence"],
            )
            return {"mode": "AUTO.LOITER", "armed": True, "in_air": True}

        return self._run_phase(
            expected_generation,
            "pause",
            ContinuationWireState.READY,
            ContinuationWireState.PAUSING,
            ContinuationWireState.PAUSED,
            pause,
        )

    def install_authorized_child(
        self, transaction: PersistentPx4MissionTransaction, *, expected_generation: int
    ) -> dict[str, Any]:
        def install():
            if not isinstance(transaction, PersistentPx4MissionTransaction):
                raise ContinuationWireError("existing persistent authorized child is required")
            if (
                transaction.state is not MissionTransactionState.AUTHORIZED
                or transaction.journal.identity.airframe_adapter_id != CONTINUATION_ADAPTER_ID
                or transaction.journal.identity.mission_item_count != 3
                or transaction.transaction._compiled.executable_mission.airframe_adapter_hash
                != CONTINUATION_ADAPTER_HASH
            ):
                raise ContinuationWireError("authorized three-item continuation child required")
            with self._lock:
                self._child = transaction
                self._child_identity_sha256 = transaction.journal.identity.canonical_hash
                self._child_grant_sha256 = transaction.transaction._operator_grant.canonical_hash
            self._await_flight_state(expected_generation, PX4_AUTO_LOITER_CUSTOM_MODE, self._now())
            fresh = _FreshTransaction(self, expected_generation)
            plan = fresh.begin_upload()
            if [item.command for item in plan.items] != [16, 16, 19]:
                raise ContinuationWireError("child must be WAYPOINT/WAYPOINT/finite LOITER")
            wrapped = SimpleNamespace(mav=_GuardedMav(self, expected_generation))

            def receive(timeout):
                return self._receive(expected_generation, timeout)

            assert self._phase_deadline_s is not None
            upload = upload_mission_int(
                wrapped,
                fresh,
                plan,
                receive=receive,
                clock=self._now,
                message_timeout_s=min(1.5, self.phase_timeout_s),
                maximum_duration_s=max(0.001, self._phase_deadline_s - self._now()),
            )
            readback = readback_mission_int(
                wrapped,
                fresh,
                plan,
                receive=receive,
                clock=self._now,
                message_timeout_s=min(1.5, self.phase_timeout_s),
            )
            if tuple(readback["raw_execution_current_flags"]) not in {(1, 0, 0), (0, 1, 0)}:
                raise ContinuationWireError("readback requires one coherent anchor/body cursor")
            committed = fresh.commit()
            self._permit = fresh.permit_start()
            return {
                "upload": upload,
                "readback": readback,
                "committed_protocol_sha256": committed,
                "mission_sha256": self._permit.executable_mission_hash,
                "wire_sha256": self._permit.protocol_hash,
                "transaction_id": self._permit.transaction_id,
            }

        return self._run_phase(
            expected_generation,
            "install",
            ContinuationWireState.PAUSED,
            ContinuationWireState.INSTALLING,
            ContinuationWireState.INSTALLED,
            install,
        )

    def select_and_confirm_cursor(
        self, sequence: int = 1, *, expected_generation: int
    ) -> dict[str, Any]:
        def select():
            if type(sequence) is not int or sequence != 1:
                raise ContinuationWireError("only continuation body cursor 1 is allowed")
            self._await_flight_state(expected_generation, PX4_AUTO_LOITER_CUSTOM_MODE, self._now())
            self._send(expected_generation, "mission_set_current_send", (1, 1, sequence))
            sent = self._sends[-1]["sent_at_s"]
            deadline = self._now() + self.phase_timeout_s
            while self._now() < deadline:
                message = self._receive(expected_generation, max(0.001, deadline - self._now()))
                if message is not None and message.get_type() == "MISSION_CURRENT":
                    if message.seq != sequence:
                        raise ContinuationWireError("actual cursor differs from body cursor 1")
                    if self._receipts[-1].observed_at_s < sent:
                        raise ContinuationWireError("cursor confirmation predates its request")
                    if (
                        self._receipts[-1].receive_sequence
                        <= self._sends[-1]["receive_cut_sequence"]
                    ):
                        raise ContinuationWireError(
                            "cursor confirmation predates the host queue cut"
                        )
                    return {"sequence": sequence, "source": "actual MISSION_CURRENT"}
            raise ContinuationWireError("cursor confirmation timed out")

        return self._run_phase(
            expected_generation,
            "cursor",
            ContinuationWireState.INSTALLED,
            ContinuationWireState.SELECTING,
            ContinuationWireState.CURSOR_SELECTED,
            select,
        )

    def resume_and_confirm(self, *, expected_generation: int) -> dict[str, Any]:
        def resume():
            if self._permit is None:
                raise ContinuationWireError("child start permit is missing")
            self._await_flight_state(expected_generation, PX4_AUTO_LOITER_CUSTOM_MODE, self._now())
            assert self._cursor is not None
            if self._cursor.to_mapping()["message"]["seq"] != 1:
                raise ContinuationWireError("selected body cursor drifted before resume")
            self._send(expected_generation, "set_mode_send", self._permit.to_set_mode_args())
            sent = self._sends[-1]["sent_at_s"]
            self._await_flight_state(
                expected_generation,
                PX4_AUTO_MISSION_CUSTOM_MODE,
                sent,
                after_sequence=self._sends[-1]["receive_cut_sequence"],
            )
            assert self._cursor is not None
            if self._cursor.to_mapping()["message"]["seq"] != 1:
                raise ContinuationWireError("resumed Mission did not retain body cursor 1")
            return {"mode": "AUTO.MISSION", "armed": True, "in_air": True, "cursor": 1}

        return self._run_phase(
            expected_generation,
            "resume",
            ContinuationWireState.CURSOR_SELECTED,
            ContinuationWireState.RESUMING,
            ContinuationWireState.RESUMED,
            resume,
        )

    def to_mapping(self) -> dict[str, Any]:
        import json

        with self._lock:
            return {
                "schema_version": 1,
                "contract": "multidetect.px4-continuation-wire-session-v1",
                "state": self._state.value,
                "generation": self._generation,
                "clock_epoch": self.clock_epoch,
                "runtime_attestation_sha256": self.runtime_attestation.canonical_hash,
                "writes_enabled": self.writes_enabled,
                "recovery_reason": self._recovery_reason,
                "receipts": [item.to_mapping() for item in self._receipts],
                "sends": json.loads(canonical_json({"items": self._sends}))["items"],
                "hardware_control_enabled": False,
                "flight_acceptance_proven": False,
                "automatic_fallback_sent": False,
                "receive_time_origin": "host_sole_reader_dequeue_not_device_capture",
                "pre_send_cut": "bounded_nonblocking_queue_drain",
                "command_causality_proven": False,
                "pipelined_protocol_packets_replayed": False,
            }
