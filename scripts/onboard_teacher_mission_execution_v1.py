"""Finite normal-ARM teacher controls on the V6 single RX/send owner.

Caller admission, an exact committed Mission/start permit, and an explicit
owned-SIM intent are required. This module issues no grant or PASS. It never
uses force ARM, arbitrary commands, a second receive loop, or real hardware.
API intention/attempt/result files are fsynced before progression; original
telemetry is dual-committed before state/ACK evidence is consumed. An API
return is not proof of delivery, and armed is not proof of airborne flight.
"""

from __future__ import annotations

import copy
import hashlib
import queue
import threading
from collections import deque
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.mission_context_admission import ContextV2MissionAdmission
from multidetect.autonomy.serialization import (
    canonical_json,
    canonical_sha256,
    require_bool,
    require_real,
    require_sha256,
    require_text,
    strict_object,
)
from multidetect.mission_journal import MissionJournalIdentity, MissionTransactionState
from multidetect.px4_external_mission import ExternalRuntimeAttestation, _validated_gcs_identity
from multidetect.px4_mission import MissionStartPermit
from scripts import onboard_teacher_mission_transport_v1 as transfer
from scripts.onboard_teacher_gazebo_session_v4 import ReadOnlyDiagnosticSender
from scripts.onboard_teacher_sim_operator import GcsHeartbeatTicker

CONTRACT = "multidetect.teacher-owned-normal-Mission-execution-v1"
AUTO_RTL_CUSTOM_MODE = (5 << 24) | (4 << 16)
_ARM_ARGS = (1, 1, 400, 0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_RTL_ARGS = (1, 1, 20, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_REQUIRED_SENSORS = 1 | 2 | 32 | 33554432


def _permit_mapping(permit):
    if type(permit) is not MissionStartPermit:
        raise TypeError("Exact typed MissionStartPermit required")
    return {
        "transaction_id": require_text(permit.transaction_id, "permit transaction_id"),
        "executable_mission_hash": require_sha256(
            permit.executable_mission_hash, "permit executable_mission_hash"
        ),
        "protocol_hash": require_sha256(permit.protocol_hash, "permit protocol_hash"),
    }


@dataclass(frozen=True, slots=True)
class TeacherMissionExecutionIntent:
    """An explicit scoped caller declaration, not authentication or a grant."""

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
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in ("approved_at_s", "expires_at_s"):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0))
        if not 0 < self.expires_at_s - self.approved_at_s <= 60:
            raise ValueError("Execution intent must expire within 60 seconds")
        for name in ("explicitly_authorized", "return_abort_authorized"):
            if require_bool(getattr(self, name), name) is not True:
                raise ValueError("Explicit start and bounded return-abort authorization required")

    def to_mapping(self):
        return {"contract": CONTRACT, "scope": "owned-disposable-SIM-normal-ARM", **asdict(self)}

    @classmethod
    def from_mapping(cls, raw):
        value = strict_object(
            raw,
            name="TeacherMissionExecutionIntent",
            required=frozenset({"contract", "scope", *(f.name for f in fields(cls))}),
        )
        if value["contract"] != CONTRACT or value["scope"] != "owned-disposable-SIM-normal-ARM":
            raise ValueError("Execution intent contract/scope differs")
        return cls(**{f.name: value[f.name] for f in fields(cls)})


@dataclass(frozen=True, slots=True)
class _ExecutionCommand:
    kind: str
    requested_at_s: float
    deadline_s: float
    cancelled: threading.Event
    intent_receipt: dict


class TeacherMissionExecution:
    """One main consumer and existing RX sender, with three finite APIs only."""

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
    ):
        if type(transport) is not transfer.TeacherMissionTransport:
            raise TypeError("Existing V6 Mission transport required")
        transport._main()
        if type(context) is not ContextSnapshot or type(admission) is not ContextV2MissionAdmission:
            raise TypeError("Typed source-qualified context and narrow SIM admission required")
        if type(runtime_attestation) is not ExternalRuntimeAttestation:
            raise TypeError("Typed current owned runtime attestation required")
        if type(intent) is not TeacherMissionExecutionIntent:
            raise TypeError("Typed scoped execution intent required")
        if not callable(runtime_recheck):
            raise TypeError("Current owned runtime reinspection callback required")
        if TeacherMissionExecutionIntent.from_mapping(intent.to_mapping()) != intent:
            raise ValueError("Execution intent is not canonical")
        checked = runtime_attestation.to_mapping()
        checked.pop("attestation_sha256")
        if ExternalRuntimeAttestation.from_mapping(checked) != runtime_attestation:
            raise ValueError("Runtime attestation is not canonical")
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

    def _main(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Execution has one main-thread consumer")

    def _save(self, name, value):
        import os

        stat = self._directory.stat()
        if (
            self._directory.is_symlink()
            or self._directory.resolve() != self._directory
            or (stat.st_dev, stat.st_ino) != self._directory_identity
        ):
            raise ValueError("Execution evidence directory identity changed")
        raw = (canonical_json(value) + "\n").encode("utf-8")
        if len(raw) > 64 * 1024:
            raise ValueError("Execution evidence record byte cap exceeded")
        path = self._directory / name
        with path.open("xb") as stream:
            if stream.write(raw) != len(raw):
                raise OSError("Short execution evidence write")
            stream.flush()
            os.fsync(stream.fileno())
        before = path.stat()
        checked = path.read_bytes()
        after = path.stat()
        if (
            path.is_symlink()
            or checked != raw
            or (before.st_size, before.st_mtime_ns, before.st_dev, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
        ):
            raise ValueError("Execution evidence bytes/identity differ after fsync")
        return {
            "path": path.as_posix(),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def _fail(self, error):
        with self._lock:
            self._failure = self._failure or f"{type(error).__name__}: {error}"[:1024]
        self._transport._fail(error)

    def _scope(self, *, starting=False):
        if self._closed or self._failure is not None:
            raise RuntimeError("Execution is closed or poisoned")
        now = self._transport._check_live()
        tx = self._transport._transaction
        identity = MissionJournalIdentity.from_transaction(tx.transaction)
        if (
            tx.state is not MissionTransactionState.START_PERMITTED
            or tx.recovery.recovery_required
            or tx.recovery.state is not MissionTransactionState.START_PERMITTED
            or tx.journal.identity != self._identity
            or identity != self._identity
            or tx.start_permit_from_checkpoint() != self._permit
            or self._identity.canonical_hash != self.intent.journal_identity_sha256
            or canonical_sha256(_permit_mapping(self._permit)) != self.intent.start_permit_sha256
            or self.intent.transaction_id != identity.transaction_id
            or self._context.canonical_hash != identity.bindings.context_snapshot_hash
            or self._context.canonical_hash != self.intent.context_snapshot_sha256
            or self._context.airframe_profile_hash != identity.bindings.airframe_profile_hash
            or self._context.airframe_profile_hash != self.intent.airframe_profile_sha256
            or self._context.schema_version != 2
            or self._context.provenance.runtime_kind != "SIM"
            or self._runtime.vehicle_class != "multirotor"
            or self._runtime.airframe_profile_id != self._context.airframe_profile_id
            or self._runtime.container_id != self.intent.container_id
            or self._runtime.image != self.intent.image
            or self._runtime.canonical_hash != self.intent.runtime_attestation_sha256
            or self._runtime.canonical_hash != self._transport.intent.runtime_attestation_sha256
            or self._admission.canonical_hash != self.intent.context_admission_sha256
            or self._admission.context_snapshot_sha256 != self._context.canonical_hash
            or self._admission.runtime_attestation_sha256 != self._runtime.canonical_hash
            or self._admission.airframe_profile_sha256 != self._context.airframe_profile_hash
            or self._admission.airframe_profile_id != self._context.airframe_profile_id
            or self._admission.task_spec_sha256 != identity.bindings.task_spec_hash
            or self._admission.observed_at_s != self._context.captured_at_s
            or self._admission.runtime_kind != "SIM"
            or self._admission.vehicle_class != "multirotor"
            or self.intent.clock_id != self._transport.intent.clock_id
            or self._admission.clock_id != self.intent.clock_id
            or not self.intent.approved_at_s <= now < self.intent.expires_at_s
        ):
            raise ValueError("Execution persistent permit/context/runtime/intent binding differs")
        if starting and (
            not self._admission.qualified_at_s <= now < self._admission.expires_at_s
            or not 0 <= now - self._context.captured_at_s <= self._age
        ):
            raise ValueError("Execution starting admission/context is future or stale")
        _validated_gcs_identity(self._transport._connection, (255, 190))
        return now

    def _runtime_now(self):
        self._main()
        if self._recheck() != self._runtime:
            raise ValueError("Current exact owned runtime reinspection differs")
        self._fresh_runtime_at = self._scope()

    def _request(self, kind):
        self._main()
        try:
            starting = kind != "return-abort"
            now = self._scope(starting=starting)
            self._transport._check_capture_binding()
            self._runtime_now()
            if len(self._attempts) >= 3:
                raise ValueError("Finite execution API budget exhausted")
            number = len(self._attempts)
            receipt = self._save(
                f"control-{number:02d}.intent.json",
                {
                    "kind": kind,
                    "requested_at_s": now,
                    "clock_id": self.intent.clock_id,
                    "execution_intent_sha256": canonical_sha256(self.intent.to_mapping()),
                    "journal_identity_sha256": self._identity.canonical_hash,
                    "delivery_verified": False,
                },
            )
            command = _ExecutionCommand(
                kind,
                now,
                min(now + self._timeout, self.intent.expires_at_s),
                threading.Event(),
                receipt,
            )
            done, returned = threading.Event(), []
            with self._lock:
                if self._active is not None:
                    raise RuntimeError("Another execution API is already pending")
                self._active = command
            try:
                self._transport._receiver._commands.put_nowait(((command,), done, returned))
            except queue.Full as error:
                raise RuntimeError("Single execution command queue exhausted") from error
            if not done.wait(max(0.001, command.deadline_s - now)):
                command.cancelled.set()
                raise RuntimeError("Execution API timeout; delivery unknown, no retry")
            valid, value = returned[0]
            if not valid:
                raise value
            self._scope()
            return value[0]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            with self._lock:
                self._active = None

    def _dispatch(self, commands):
        if (
            type(commands) is not tuple
            or len(commands) != 1
            or type(commands[0]) is not _ExecutionCommand
        ):
            raise TypeError("One typed finite execution command required")
        command = commands[0]
        owner = threading.get_ident()
        with self._lock:
            if owner == self._owner or (
                self._sender_owner is not None and owner != self._sender_owner
            ):
                raise RuntimeError("Execution sender is the exclusive RX thread")
            self._sender_owner = owner
            if command is not self._active or command.cancelled.is_set():
                raise RuntimeError("Execution command was not minted by the active main consumer")
        number, evidence = len(self._attempts), None
        try:
            now = self._scope(starting=command.kind != "return-abort")
            if (
                now >= command.deadline_s
                or self._fresh_runtime_at is None
                or not 0 <= now - self._fresh_runtime_at <= self._age
            ):
                raise ValueError("Execution command/runtime check is stale")
            if command.kind == "mission-mode" and self._status == "ready":
                method, args = "set_mode_send", self._permit.to_set_mode_args()
            elif command.kind == "normal-arm" and self._status == "mode-observed":
                method, args = "command_long_send", _ARM_ARGS
            elif command.kind == "return-abort" and self._status == "armed-mission-observed":
                method, args = "command_long_send", _RTL_ARGS
            else:
                raise ValueError("Finite execution kind/phase differs")
            evidence = {
                "kind": command.kind,
                "method": method,
                "args": list(args),
                "API_owner_thread_id": owner,
                "API_entry_intended_at_s": now,
                "API_attempted_at_s": None,
                "API_attempted": False,
                "API_returned": False,
                "delivery_verified": False,
                "clock_id": self.intent.clock_id,
                "intention_receipt": command.intent_receipt,
                "error": None,
            }
            evidence["attempt_receipt"] = self._save(f"control-{number:02d}.attempt.json", evidence)
            with self._lock:
                self._attempts.append(evidence)
            entered_at = self._scope(starting=command.kind != "return-abort")
            if command.cancelled.is_set() or entered_at >= command.deadline_s:
                raise RuntimeError("Execution expired before API; no send")
            sender = getattr(self._transport._connection.mav, method)
            # The durable pre-call record is an entry INTENTION. Only crossing
            # the API boundary sets attempted; neither means datagram delivery.
            evidence["API_attempted"] = True
            evidence["API_attempted_at_s"] = entered_at
            sender(*args)
            evidence["API_returned"] = True
            evidence["API_returned_at_s"] = self._scope()
            if command.cancelled.is_set() or evidence["API_returned_at_s"] >= command.deadline_s:
                raise RuntimeError("Execution API returned late; delivery unknown")
            evidence["result_receipt"] = self._save(f"control-{number:02d}.result.json", evidence)
            return [copy.deepcopy(evidence)]
        except BaseException as error:
            if evidence is not None:
                evidence["error"] = f"{type(error).__name__}: {error}"[:1024]
                try:
                    evidence["failure_receipt"] = self._save(
                        f"control-{number:02d}.failure.json", evidence
                    )
                except BaseException as secondary:
                    error.add_note(f"Execution failure receipt error: {secondary}")
            self._fail(error)
            raise

    def poll(self, timeout_s=0.02):
        """Commit original bytes/projection before returning source-qualified rows."""
        self._main()
        try:
            self._scope()
            self._transport._check_capture_binding()
            duration = require_real(timeout_s, "poll timeout", minimum=0, maximum=1)
            batch = self._transport._receiver.read_batch(duration)
            rows = self._transport._capture.append_batch(batch) if batch else []
            now = self._scope()
            admitted = []
            for row in rows:
                message = row["message"]
                if (
                    row["source_accepted"] is not True
                    or (message["source_system_id"], message["source_component_id"]) != (1, 1)
                    or not 0 <= now - row["received_at_s"] <= self._age
                ):
                    continue
                self._latest[message["message_type"]] = row
                self._recent.append(row)
                if message["message_type"] == "COMMAND_ACK":
                    if len(self._acks) >= 128:
                        raise ValueError("Finite pending execution ACK budget exhausted")
                    self._acks.append(row)
                admitted.append(copy.deepcopy(row))
            return admitted
        except BaseException as error:
            self._fail(error)
            raise

    def _latest_row(self, kind, *, after=None):
        row = self._transport._capture.latest_message(
            kind, now_s=self._scope(), maximum_age_s=self._age
        )
        if row is None or (after is not None and row["received_at_s"] < after):
            return None
        if row["source_accepted"] is not True or (
            row["message"]["source_system_id"],
            row["message"]["source_component_id"],
        ) != (1, 1):
            return None
        return row

    def _matching_heartbeat(self, *, armed, mode=None, after=None):
        current = self._scope()
        candidates = list(self._recent)
        latest = self._latest_row("HEARTBEAT", after=after)
        if latest is not None:
            candidates.append(latest)
        for row in reversed(candidates):
            if (
                row["message"]["message_type"] == "HEARTBEAT"
                and (after is None or row["received_at_s"] >= after)
                and 0 <= current - row["received_at_s"] <= self._age
                and self._heartbeat(row, armed=armed, mode=mode)
            ):
                return row
        return None

    @staticmethod
    def _heartbeat(row, *, armed, mode=None):
        if row is None:
            return False
        m = row["message"]
        if any(
            type(m.get(k)) is not int for k in ("base_mode", "custom_mode", "type", "autopilot")
        ):
            raise ValueError("Malformed source-qualified HEARTBEAT")
        return (
            0 <= m["base_mode"] <= 255
            and 0 <= m["custom_mode"] < 2**32
            and m["type"] == 2
            and m["autopilot"] == 12
            and bool(m["base_mode"] & 128) is armed
            and (mode is None or (m["base_mode"] & 1 and m["custom_mode"] == mode))
        )

    def _ground(self, *, after=None):
        row = self._latest_row("EXTENDED_SYS_STATE", after=after)
        if row is None:
            return False
        m = row["message"]
        if type(m.get("landed_state")) is not int or type(m.get("vtol_state")) is not int:
            raise ValueError("Malformed grounded-state observation")
        return m["landed_state"] == 1 and m["vtol_state"] == 0

    def _sensors(self, *, after=None):
        row = self._latest_row("SYS_STATUS", after=after)
        if row is None:
            return False
        m = row["message"]
        for name in (
            "onboard_control_sensors_present",
            "onboard_control_sensors_enabled",
            "onboard_control_sensors_health",
        ):
            v = m.get(name)
            if type(v) is not int or not 0 <= v < 2**32:
                raise ValueError("Malformed sensor-health bitfield")
            if v & _REQUIRED_SENSORS != _REQUIRED_SENSORS:
                return False
        # At pinned PX4 83c4f4e, SYS_STATUS.hpp:126-127 derives bit28 from
        # can_arm_mode_flags for the CURRENT nav_state, not from heading_good.
        # start() requires a new exact Mission HB and post-mode SYS_STATUS.
        return bool(m["onboard_control_sensors_health"] & (1 << 28))

    def _wait(self, predicate, timeout_s, label):
        timeout = require_real(timeout_s, "phase timeout", minimum=0.01, maximum=15)
        deadline = min(self._scope() + timeout, self.intent.expires_at_s)
        while True:
            if predicate():
                return
            now = self._scope()
            if now >= deadline:
                raise RuntimeError(f"{label} observation timeout; no acceptance claimed")
            self.poll(min(0.05, deadline - now))

    def _ack(self, command, after, timeout_s):
        def accepted():
            while self._acks:
                row = self._acks.popleft()
                if (
                    row["received_at_s"] < after
                    or not 0 <= self._scope() - row["received_at_s"] <= self._age
                ):
                    continue
                m = row["message"]
                if any(
                    type(m.get(k)) is not int
                    for k in ("command", "result", "target_system", "target_component")
                ):
                    raise ValueError("Malformed COMMAND_ACK")
                if (m["target_system"], m["target_component"]) != (255, 190) or m[
                    "command"
                ] != command:
                    continue
                if m["result"] != 0:
                    raise RuntimeError(
                        f"PX4 rejected normal command {command}: result={m['result']}"
                    )
                self._record("command-ACK-observed", row)
                return True
            return False

        self._wait(accepted, timeout_s, f"command {command} ACK")

    def _record(self, state, row):
        number = len(self._observations)
        if number >= 16:
            raise ValueError("Finite execution observation budget exhausted")
        receipt = self._save(
            f"state-{number:02d}.json",
            {
                "state": state,
                "observed_row": row,
                "journal_identity_sha256": self._identity.canonical_hash,
                "flight_execution_claimed": False,
            },
        )
        self._observations.append({"state": state, "receipt": receipt})

    def start(self, *, phase_timeout_s=5.0):
        """Set exact permitted Mission mode, normal ARM, then observe armed state.

        The immutable planning context/admission must still be within its
        original 2 s window at both send boundaries. Fresh control telemetry
        does NOT refresh that window. Slow upload or mode switching therefore
        fails closed pending a separate, explicit live revalidation contract.
        """
        self._main()
        try:
            if self._start_called:
                raise RuntimeError("Teacher start is single-use; no rearm/retry")
            self._start_called = True
            self._scope(starting=True)
            self.poll(0)
            if not (
                self._heartbeat(self._latest_row("HEARTBEAT"), armed=False)
                and self._ground()
                and self._sensors()
            ):
                raise ValueError("Fresh disarmed grounded MC and prearm/sensor health required")
            mode = self._request("mission-mode")
            self._start_at = mode["API_attempted_at_s"]
            exact_mode = self._permit.to_set_mode_args()[2]
            self._wait(
                lambda: (
                    self._heartbeat(
                        self._latest_row("HEARTBEAT", after=self._start_at),
                        armed=False,
                        mode=exact_mode,
                    )
                    and self._ground()
                    and self._sensors(after=self._start_at)
                ),
                phase_timeout_s,
                "exact AUTO.MISSION grounded prearm",
            )
            self._record("mode-observed", self._latest_row("HEARTBEAT", after=self._start_at))
            self._status = "mode-observed"
            arm = self._request("normal-arm")
            self._arm_at = arm["API_attempted_at_s"]
            self._ack(400, self._arm_at, phase_timeout_s)
            self._wait(
                lambda: self._heartbeat(
                    self._latest_row("HEARTBEAT", after=self._arm_at),
                    armed=True,
                    mode=exact_mode,
                ),
                phase_timeout_s,
                "armed AUTO.MISSION",
            )
            self._record(
                "armed-mission-observed",
                self._latest_row("HEARTBEAT", after=self._arm_at),
            )
            self._status = "armed-mission-observed"
            return self.to_mapping()
        except BaseException as error:
            self._fail(error)
            raise

    def abort_return(self, *, phase_timeout_s=5.0):
        """One normal RTL command for the exact active Mission; observe convergence."""
        self._main()
        try:
            if self._abort_called or self._status != "armed-mission-observed":
                raise RuntimeError("Return abort requires one current active teacher Mission")
            self._abort_called = True
            if not self._heartbeat(
                self._latest_row("HEARTBEAT", after=self._arm_at),
                armed=True,
                mode=self._permit.to_set_mode_args()[2],
            ):
                raise ValueError("Return abort requires a fresh armed exact Mission observation")
            returned = self._request("return-abort")
            self._rtl_at = returned["API_attempted_at_s"]
            self._ack(20, self._rtl_at, phase_timeout_s)
            self._wait(
                lambda: (
                    self._matching_heartbeat(
                        after=self._rtl_at, armed=True, mode=AUTO_RTL_CUSTOM_MODE
                    )
                    is not None
                ),
                phase_timeout_s,
                "RTL mode",
            )
            self._record(
                "RTL-mode-observed",
                self._matching_heartbeat(after=self._rtl_at, armed=True, mode=AUTO_RTL_CUSTOM_MODE),
            )
            self._wait(
                lambda: (
                    self._heartbeat(self._latest_row("HEARTBEAT", after=self._rtl_at), armed=False)
                    and self._ground(after=self._rtl_at)
                ),
                phase_timeout_s,
                "landed and disarmed after RTL",
            )
            self._record(
                "return-abort-landed-disarmed-observed",
                self._latest_row("HEARTBEAT", after=self._rtl_at),
            )
            self._status = "return-abort-landed-disarmed-observed"
            return self.to_mapping()
        except BaseException as error:
            self._fail(error)
            raise

    def cancel(self):
        self._main()
        with self._lock:
            if self._active is not None:
                self._active.cancelled.set()
            self._closed = True
        self._fail(RuntimeError("Teacher execution explicitly cancelled"))

    def close(self):
        """Stop this producer only; V7 owned lifecycle joins the unique RX owner."""
        self._main()
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active.cancelled.set()

    def to_mapping(self):
        with self._lock:
            return {
                "contract": CONTRACT,
                "status": self._status,
                "closed": self._closed,
                "error": self._failure,
                "intent": self.intent.to_mapping(),
                "API_attempts": copy.deepcopy(self._attempts),
                "observations": copy.deepcopy(self._observations),
                "armed_state_observed": self._arm_at is not None
                and any(r["state"] == "armed-mission-observed" for r in self._observations),
                "claims": {
                    name: False
                    for name in (
                        "flight_authority_created",
                        "force_ARM_used",
                        "datagram_delivery_verified",
                        "airborne_flight_proven",
                        "mission_completion_proven",
                        "external_SIM_executed",
                        "teacher_expert_verified",
                        "hardware_control_enabled",
                        "optimizer_performed",
                        "final_release_allowed",
                    )
                },
            }


class OwnedTeacherGazeboSessionV7(transfer.OwnedTeacherGazeboSessionV6):
    """V6 transfer/camera lifecycle with an additional finite execution queue hook."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mission_execution = None

    def start_receiver(self, queue_capacity=256):
        if (
            self.receiver is not None
            or threading.get_ident() != self._owner
            or not self.execute
            or self._closed
            or self.connection is None
        ):
            raise RuntimeError("Opened owned V7 session and one receiver required")
        self._gcs = GcsHeartbeatTicker(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )
        self._diagnostic_sender = ReadOnlyDiagnosticSender(
            self.connection, self.clock.monotonic, self.clock.session_id, self._remaining
        )

        def reader(timeout):
            if self.connection.recv_match != self._receive or self._closed:
                raise RuntimeError("Original V7 transferred receive path differs")
            self._gcs.tick()
            message = self._receive(blocking=True, timeout=min(timeout, self._remaining()))
            self._remaining()
            return message

        def sender(commands):
            if commands and type(commands[0]) is _ExecutionCommand:
                if self.mission_execution is None:
                    raise RuntimeError("Finite execution not bound")
                return self.mission_execution._dispatch(commands)
            if commands and type(commands[0]) is transfer._SendCommand:
                if self.mission_transport is None:
                    raise RuntimeError("Mission transfer not bound")
                return self.mission_transport._dispatch(commands)
            return self._diagnostic_sender(commands)

        self.receiver = transfer._ConcurrentReceiverV6(
            reader, self.clock.monotonic, request_sender=sender, queue_capacity=queue_capacity
        ).start()
        return self.receiver

    def bind_mission_execution(self, **inputs):
        if (
            threading.get_ident() != self._owner
            or self.mission_execution is not None
            or self.mission_transport is None
            or not self.execute
            or self._closed
        ):
            raise RuntimeError("One opened owned session/transport execution binding required")
        if "transport" in inputs or "runtime_recheck" in inputs:
            raise ValueError(
                "V7 itself binds the original transport and actual runtime reinspection"
            )
        self.mission_execution = TeacherMissionExecution(
            transport=self.mission_transport,
            # Reinspect actual owned runtime; retain the CALLER'S original
            # qualification declaration. This callback is not a new issuer.
            runtime_recheck=lambda: self.attest_qualified(
                telemetry_qualified=inputs["runtime_attestation"].telemetry_qualified
            ),
            **inputs,
        )
        return self.mission_execution

    def _finish(self, primary):
        if self.mission_execution is not None and not self.mission_execution._closed:
            self.mission_execution.close()
        super()._finish(primary)

    def to_mapping(self):
        return {
            **super().to_mapping(),
            "contract": "multidetect.onboard-teacher-gazebo-session-v7",
            "mission_execution": None
            if self.mission_execution is None
            else self.mission_execution.to_mapping(),
        }


__all__ = [
    "TeacherMissionExecutionIntent",
    "TeacherMissionExecution",
    "OwnedTeacherGazeboSessionV7",
]
