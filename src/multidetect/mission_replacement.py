"""Fail-closed transaction for replacing an in-flight PX4 Mission.

The ordinary :class:`Px4MissionTransaction` deliberately becomes terminal at
``START_PERMITTED``.  Replanning after a realtime OnboardAI event therefore
must not mutate that transaction or append ordinary events to its journal.
This module owns a separate, parent-bound transaction which records the pause,
replacement upload/readback/commit and explicit resume sequence.

The transport is injected.  A SIM, HIL or PX4 adapter can implement the same
small callback surface, while this module remains free of MAVLink, mode and
actuator commands.  A transport callback must prove its step in the returned
mapping; an exception or an unconfirmed step is fail-closed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    require_aware_timestamp,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .compat import StrEnum


class MissionReplacementError(RuntimeError):
    """Raised when a replacement cannot be proven safe to continue."""


class MissionReplacementState(StrEnum):
    AUTHORIZED = "authorized"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    UPLOADING = "uploading"
    UPLOAD_CONFIRMED = "upload_confirmed"
    READBACK_VERIFIED = "readback_verified"
    COMMIT_REQUESTED = "commit_requested"
    COMMITTED = "committed"
    CURSOR_REQUESTED = "cursor_requested"
    CURSOR_CONFIRMED = "cursor_confirmed"
    RESUME_REQUESTED = "resume_requested"
    RESUMED = "resumed"
    HOLD_REQUIRED = "hold_required"
    RETURN_REQUIRED = "return_required"
    RECOVERY_REQUIRED = "recovery_required"
    DENIED = "denied"


_TRANSITIONS: dict[MissionReplacementState, frozenset[MissionReplacementState]] = {
    MissionReplacementState.AUTHORIZED: frozenset({MissionReplacementState.PAUSE_REQUESTED}),
    MissionReplacementState.PAUSE_REQUESTED: frozenset(
        {MissionReplacementState.PAUSED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.PAUSED: frozenset(
        {MissionReplacementState.UPLOADING, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.UPLOADING: frozenset(
        {MissionReplacementState.UPLOAD_CONFIRMED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.UPLOAD_CONFIRMED: frozenset(
        {MissionReplacementState.READBACK_VERIFIED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.READBACK_VERIFIED: frozenset(
        {MissionReplacementState.COMMIT_REQUESTED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.COMMIT_REQUESTED: frozenset(
        {MissionReplacementState.COMMITTED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.COMMITTED: frozenset(
        {MissionReplacementState.CURSOR_REQUESTED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.CURSOR_REQUESTED: frozenset(
        {MissionReplacementState.CURSOR_CONFIRMED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.CURSOR_CONFIRMED: frozenset(
        {MissionReplacementState.RESUME_REQUESTED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.RESUME_REQUESTED: frozenset(
        {MissionReplacementState.RESUMED, MissionReplacementState.HOLD_REQUIRED,
         MissionReplacementState.RETURN_REQUIRED, MissionReplacementState.RECOVERY_REQUIRED,
         MissionReplacementState.DENIED}
    ),
    MissionReplacementState.RESUMED: frozenset(),
    MissionReplacementState.HOLD_REQUIRED: frozenset(),
    MissionReplacementState.RETURN_REQUIRED: frozenset(),
    MissionReplacementState.DENIED: frozenset(),
    MissionReplacementState.RECOVERY_REQUIRED: frozenset(),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionReplacementError(f"{name} must be a mapping")
    return dict(value)


def _evidence(value: object, name: str) -> dict[str, Any]:
    result = _object(value, name)
    confirmed = result.get("confirmed")
    if confirmed is not True:
        raise MissionReplacementError(f"{name}.confirmed must be true")
    return result


@dataclass(frozen=True, slots=True)
class MissionReplacementBinding:
    """Hash identity connecting an event and a new Mission to its parent."""

    replacement_id: str
    parent_transaction_id: str
    parent_mission_sha256: str
    event_sha256: str
    context_sha256: str
    new_mission_sha256: str
    new_protocol_sha256: str
    airframe_profile_sha256: str
    parent_journal_sha256: str
    observed_current_sequence: int
    resume_sequence: int
    source_system: int
    source_component: int
    reason: str
    parent_protocol_sha256: str | None = None
    policy_sha256: str | None = None
    ranker_sha256: str | None = None
    planner_sha256: str | None = None
    safety_policy_sha256: str | None = None
    release_parity_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("replacement_id", "parent_transaction_id", "reason"):
            object.__setattr__(self, name, require_text(getattr(self, name), name))
        for name in (
            "parent_mission_sha256",
            "event_sha256",
            "context_sha256",
            "new_mission_sha256",
            "new_protocol_sha256",
            "airframe_profile_sha256",
            "parent_journal_sha256",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        for name in (
            "parent_protocol_sha256",
            "policy_sha256",
            "ranker_sha256",
            "planner_sha256",
            "safety_policy_sha256",
            "release_parity_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_sha256(value, name))
        for name in (
            "observed_current_sequence",
            "resume_sequence",
            "source_system",
            "source_component",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.source_system != 1 or self.source_component != 1:
            raise ValueError("replacement transport identity must be PX4 1/1")
        if self.resume_sequence == 0:
            raise ValueError("resume_sequence cannot target the takeoff anchor")

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "contract": "multidetect.mission-replacement-binding",
            "replacement_id": self.replacement_id,
            "parent_transaction_id": self.parent_transaction_id,
            "parent_mission_sha256": self.parent_mission_sha256,
            "event_sha256": self.event_sha256,
            "context_sha256": self.context_sha256,
            "new_mission_sha256": self.new_mission_sha256,
            "new_protocol_sha256": self.new_protocol_sha256,
            "airframe_profile_sha256": self.airframe_profile_sha256,
            "parent_journal_sha256": self.parent_journal_sha256,
            "observed_current_sequence": self.observed_current_sequence,
            "resume_sequence": self.resume_sequence,
            "source_system": self.source_system,
            "source_component": self.source_component,
            "reason": self.reason,
            "parent_protocol_sha256": self.parent_protocol_sha256,
            "policy_sha256": self.policy_sha256,
            "ranker_sha256": self.ranker_sha256,
            "planner_sha256": self.planner_sha256,
            "safety_policy_sha256": self.safety_policy_sha256,
            "release_parity_sha256": self.release_parity_sha256,
            "truth_free": True,
        }


class MissionReplacementTransport(Protocol):
    """Adapter-owned pause/upload/readback/commit/resume callbacks."""

    def request_pause(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def confirm_paused(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def upload_replacement(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def verify_readback(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def commit_replacement(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def request_resume_cursor(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def confirm_resume_cursor(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def request_resume(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def confirm_resumed(self, binding: MissionReplacementBinding) -> Mapping[str, Any]: ...

    def fallback(self, binding: MissionReplacementBinding, mode: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MissionReplacementRecord:
    sequence: int
    state: MissionReplacementState
    event_type: str
    occurred_at: str
    payload: Mapping[str, Any]
    previous_record_sha256: str | None
    record_sha256: str

    @classmethod
    def build(
        cls,
        *,
        sequence: int,
        state: MissionReplacementState,
        event_type: str,
        payload: Mapping[str, Any],
        previous_record_sha256: str | None,
    ) -> MissionReplacementRecord:
        value = {
            "schema_version": 1,
            "contract": "multidetect.mission-replacement-record",
            "sequence": sequence,
            "state": state.value,
            "event_type": event_type,
            "occurred_at": _utc_now(),
            "payload": payload,
            "previous_record_sha256": previous_record_sha256,
        }
        return cls(
            sequence=sequence,
            state=state,
            event_type=event_type,
            occurred_at=value["occurred_at"],
            payload=dict(payload),
            previous_record_sha256=previous_record_sha256,
            record_sha256=canonical_sha256(value),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "contract": "multidetect.mission-replacement-record",
            "sequence": self.sequence,
            "state": self.state.value,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "payload": json.loads(canonical_json(self.payload)),
            "previous_record_sha256": self.previous_record_sha256,
            "record_sha256": self.record_sha256,
        }


class MissionReplacementJournal:
    """Hash-chained append-only replacement journal."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        binding: MissionReplacementBinding | None = None,
    ) -> None:
        self.path = None if path is None else Path(path).expanduser().resolve()
        self._binding = binding
        self._records: list[MissionReplacementRecord] = []
        self._recovery_required = False
        self._recovery_reason: str | None = None
        self._lock = RLock()
        if self.path is not None and self.path.exists():
            self._load()
        if self._binding is not None:
            self._validate_binding_header()

    @property
    def records(self) -> tuple[MissionReplacementRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def state(self) -> MissionReplacementState:
        if not self._records:
            raise MissionReplacementError("replacement journal has no records")
        return self._records[-1].state

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    @property
    def recovery_reason(self) -> str | None:
        return self._recovery_reason

    def bind(self, binding: MissionReplacementBinding) -> None:
        """Bind an empty or already-loaded journal to one replacement identity."""

        if self._binding is not None and self._binding.canonical_hash != binding.canonical_hash:
            raise MissionReplacementError("replacement journal binding mismatch")
        self._binding = binding
        self._validate_binding_header()

    def _validate_binding_header(self) -> None:
        if not self._records or self._binding is None:
            return
        first = self._records[0]
        payload_binding = first.payload.get("binding")
        payload_hash = first.payload.get("binding_sha256")
        if not isinstance(payload_binding, Mapping) or payload_hash != self._binding.canonical_hash:
            raise MissionReplacementError("replacement journal binding header is missing")
        if canonical_sha256(dict(payload_binding)) != self._binding.canonical_hash:
            raise MissionReplacementError("replacement journal binding header mismatch")

    def append(
        self,
        state: MissionReplacementState,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> MissionReplacementRecord:
        with self._lock:
            previous = self._records[-1] if self._records else None
            if previous is None:
                if (
                    state is not MissionReplacementState.AUTHORIZED
                    or event_type != "replacement_authorized"
                ):
                    raise MissionReplacementError(
                        "replacement journal must begin with replacement_authorized"
                    )
                if self._binding is not None:
                    value = _object(payload, "payload")
                    if value.get("binding_sha256") != self._binding.canonical_hash:
                        raise MissionReplacementError("replacement journal binding header mismatch")
                    binding_value = value.get("binding")
                    if (
                        not isinstance(binding_value, Mapping)
                        or canonical_sha256(dict(binding_value))
                        != self._binding.canonical_hash
                    ):
                        raise MissionReplacementError("replacement journal binding header mismatch")
            if previous is not None and state not in _TRANSITIONS[previous.state]:
                raise MissionReplacementError(
                    f"replacement state {previous.state.value} cannot transition to {state.value}"
                )
            record = MissionReplacementRecord.build(
                sequence=len(self._records),
                state=state,
                event_type=require_text(event_type, "event_type"),
                payload=_object(payload, "payload"),
                previous_record_sha256=(None if previous is None else previous.record_sha256),
            )
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                encoded = (
                    json.dumps(record.to_mapping(), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                with self.path.open("ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            self._records.append(record)
            return record

    def _load(self) -> None:
        try:
            raw_bytes = self.path.read_bytes() if self.path is not None else b""
            lines_with_endings = raw_bytes.splitlines(keepends=True)
            lines = []
            if lines_with_endings and not lines_with_endings[-1].endswith((b"\n", b"\r")):
                self._recovery_required = True
                self._recovery_reason = "replacement journal has an incomplete final record"
                lines_with_endings = lines_with_endings[:-1]
            lines = [line.rstrip(b"\r\n") for line in lines_with_endings]
            records: list[MissionReplacementRecord] = []
            previous_hash = None
            for index, line in enumerate(lines):
                raw = strict_json_loads(line, name="replacement journal record")
                raw = dict(strict_object(
                    raw,
                    name="replacement journal record",
                    required=frozenset({
                        "schema_version", "contract", "sequence", "state", "event_type",
                        "occurred_at", "payload", "previous_record_sha256", "record_sha256",
                    }),
                ))
                require_schema_version(raw.get("schema_version"))
                if raw.get("contract") != "multidetect.mission-replacement-record":
                    raise AutonomyContractError("replacement journal record contract mismatch")
                state = MissionReplacementState(require_text(raw.get("state"), "state"))
                if require_int(raw.get("sequence"), "sequence", minimum=0) != index:
                    raise MissionReplacementError("replacement journal sequence is not contiguous")
                if raw.get("previous_record_sha256") != previous_hash:
                    raise MissionReplacementError("replacement journal hash chain is broken")
                expected = dict(raw)
                record_hash = expected.pop("record_sha256", None)
                if record_hash != canonical_sha256(expected):
                    raise MissionReplacementError("replacement journal record hash mismatch")
                record = MissionReplacementRecord(
                    sequence=index,
                    state=state,
                    event_type=require_text(raw.get("event_type"), "event_type"),
                    occurred_at=require_aware_timestamp(raw.get("occurred_at"), "occurred_at"),
                    payload=_object(raw.get("payload"), "payload"),
                    previous_record_sha256=raw.get("previous_record_sha256"),
                    record_sha256=require_sha256(record_hash, "record_sha256"),
                )
                if records and state not in _TRANSITIONS[records[-1].state]:
                    raise MissionReplacementError("replacement journal contains invalid transition")
                records.append(record)
                previous_hash = record.record_sha256
            self._records = records
            self._validate_binding_header()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            AutonomyContractError,
            MissionReplacementError,
        ) as exc:
            raise MissionReplacementError("replacement journal is corrupt") from exc

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "contract": "multidetect.mission-replacement-journal",
            "path": None if self.path is None else str(self.path),
            "state": self.state.value if self._records else None,
            "record_count": len(self._records),
            "records": [record.to_mapping() for record in self._records],
            "binding": None if self._binding is None else self._binding.to_mapping(),
            "binding_sha256": None if self._binding is None else self._binding.canonical_hash,
            "recovery_required": self._recovery_required,
            "recovery_reason": self._recovery_reason,
            "truth_free": True,
        }


class MissionReplacementTransaction:
    """Execute one parent-bound in-flight Mission replacement."""

    def __init__(
        self,
        binding: MissionReplacementBinding,
        *,
        parent_state: str | Enum,
        transport: MissionReplacementTransport,
        journal: MissionReplacementJournal | None = None,
        fallback_mode: str = "return",
    ) -> None:
        if not isinstance(binding, MissionReplacementBinding):
            raise TypeError("binding must be a MissionReplacementBinding")
        parent_value = parent_state.value if isinstance(parent_state, Enum) else parent_state
        if parent_value != "start_permitted":
            raise MissionReplacementError(
                "replacement requires the parent Mission to be start_permitted"
            )
        if not all(
            callable(getattr(transport, name, None))
            for name in (
                "request_pause",
                "confirm_paused",
                "upload_replacement",
                "verify_readback",
                "commit_replacement",
                "request_resume_cursor",
                "confirm_resume_cursor",
                "request_resume",
                "confirm_resumed",
                "fallback",
            )
        ):
            raise TypeError("transport does not implement the replacement contract")
        if fallback_mode not in {"hold", "return"}:
            raise ValueError("fallback_mode must be hold or return")
        self.binding = binding
        self.transport = transport
        self.journal = journal or MissionReplacementJournal()
        self.journal.bind(binding)
        self.fallback_mode = fallback_mode
        self._executed = False
        self._in_memory_recovery = False
        self._in_memory_recovery_reason: str | None = None

    @property
    def state(self) -> MissionReplacementState:
        if self._in_memory_recovery or self.journal.recovery_required:
            return MissionReplacementState.RECOVERY_REQUIRED
        return self.journal.state

    @property
    def terminal(self) -> bool:
        return self.state in {
            MissionReplacementState.RESUMED,
            MissionReplacementState.HOLD_REQUIRED,
            MissionReplacementState.RETURN_REQUIRED,
            MissionReplacementState.RECOVERY_REQUIRED,
            MissionReplacementState.DENIED,
        }

    def execute(self) -> dict[str, Any]:
        if self._executed or self.journal.records or self.journal.recovery_required:
            raise MissionReplacementError(
                "replacement transaction is already started or requires recovery"
            )
        self._executed = True
        self.journal.append(
            MissionReplacementState.AUTHORIZED,
            "replacement_authorized",
            {"binding": self.binding.to_mapping(), "binding_sha256": self.binding.canonical_hash},
        )
        try:
            self._intent(
                MissionReplacementState.PAUSE_REQUESTED,
                "pause_requested",
            )
            _evidence(self.transport.request_pause(self.binding), "pause_requested")
            self._confirm(
                MissionReplacementState.PAUSED,
                "pause_confirmed",
                self.transport.confirm_paused(self.binding),
            )
            self._intent(
                MissionReplacementState.UPLOADING,
                "replacement_upload_started",
            )
            self._confirm(
                MissionReplacementState.UPLOAD_CONFIRMED,
                "replacement_upload_confirmed",
                self.transport.upload_replacement(self.binding),
            )
            self._confirm(
                MissionReplacementState.READBACK_VERIFIED,
                "replacement_readback_verified",
                self.transport.verify_readback(self.binding),
            )
            self._intent(
                MissionReplacementState.COMMIT_REQUESTED,
                "replacement_commit_started",
            )
            self._confirm(
                MissionReplacementState.COMMITTED,
                "replacement_committed",
                self.transport.commit_replacement(self.binding),
            )
            self._intent(
                MissionReplacementState.CURSOR_REQUESTED,
                "resume_cursor_requested",
            )
            _evidence(
                self.transport.request_resume_cursor(self.binding),
                "resume_cursor_requested",
            )
            cursor_evidence = self.transport.confirm_resume_cursor(self.binding)
            if cursor_evidence.get("sequence") != self.binding.resume_sequence:
                raise MissionReplacementError("resume cursor confirmation does not match binding")
            if cursor_evidence.get("mission_sha256") != self.binding.new_mission_sha256:
                raise MissionReplacementError("resume cursor is not bound to replacement Mission")
            self._require_source(cursor_evidence, "resume cursor")
            command = require_text(
                cursor_evidence.get("mission_item_command"), "mission_item_command"
            )
            if command in {
                "MAV_CMD_NAV_TAKEOFF",
                "MAV_CMD_DO_VTOL_TRANSITION",
                "MAV_CMD_NAV_LAND",
                "MAV_CMD_NAV_RETURN_TO_LAUNCH",
                "MAV_CMD_NAV_RTL",
            }:
                raise MissionReplacementError("resume cursor targets a forbidden handoff command")
            self._confirm(
                MissionReplacementState.CURSOR_CONFIRMED,
                "resume_cursor_confirmed",
                cursor_evidence,
            )
            self._intent(
                MissionReplacementState.RESUME_REQUESTED,
                "resume_requested",
            )
            _evidence(self.transport.request_resume(self.binding), "resume_requested")
            resumed = self.transport.confirm_resumed(self.binding)
            if resumed.get("mission_sha256") != self.binding.new_mission_sha256:
                raise MissionReplacementError(
                    "resume confirmation is not bound to replacement Mission"
                )
            if resumed.get("source_system") != self.binding.source_system or (
                resumed.get("source_component") != self.binding.source_component
            ):
                raise MissionReplacementError("resume confirmation source identity mismatch")
            if resumed.get("auto_mission") is not True:
                raise MissionReplacementError("resume confirmation lacks exact AUTO.MISSION proof")
            self._confirm(
                MissionReplacementState.RESUMED,
                "replacement_resumed",
                resumed,
            )
        except Exception as exc:  # noqa: BLE001 - preserve fail-closed receipt
            return self._fail_closed(exc)
        return self.receipt()

    def _intent(
        self,
        state: MissionReplacementState,
        event_type: str,
    ) -> None:
        self.journal.append(
            state,
            event_type,
            {"intent": True, "binding_sha256": self.binding.canonical_hash},
        )

    def _confirm(
        self,
        state: MissionReplacementState,
        event_type: str,
        evidence: Mapping[str, Any],
    ) -> None:
        self.journal.append(state, event_type, _evidence(evidence, event_type))

    def _require_source(self, evidence: Mapping[str, Any], name: str) -> None:
        if evidence.get("source_system") != self.binding.source_system or evidence.get(
            "source_component"
        ) != self.binding.source_component:
            raise MissionReplacementError(f"{name} source identity mismatch")

    def _fail_closed(self, error: Exception) -> dict[str, Any]:
        failure = f"{type(error).__name__}: {error}"
        try:
            evidence = self.transport.fallback(self.binding, self.fallback_mode)
            evidence = _evidence(evidence, f"fallback_{self.fallback_mode}")
            self._require_source(evidence, f"fallback_{self.fallback_mode}")
            state = (
                MissionReplacementState.HOLD_REQUIRED
                if self.fallback_mode == "hold"
                else MissionReplacementState.RETURN_REQUIRED
            )
            self.journal.append(
                state,
                f"fallback_{self.fallback_mode}",
                {**evidence, "failure": failure},
            )
        except Exception as fallback_error:  # noqa: BLE001
            self._append_recovery(
                failure,
                f"{type(fallback_error).__name__}: {fallback_error}",
            )
        return self.receipt()

    def _append_recovery(self, failure: str, fallback_failure: str) -> None:
        payload = {
            "failure": failure,
            "fallback_failure": fallback_failure,
            "write_uncertain": True,
            "fallback_attempted": True,
            "fallback_confirmed": False,
        }
        try:
            self.journal.append(
                MissionReplacementState.RECOVERY_REQUIRED,
                "replacement_recovery_required",
                payload,
            )
        except Exception as journal_error:  # noqa: BLE001 - preserve in-memory uncertainty
            self._in_memory_recovery = True
            self._in_memory_recovery_reason = (
                f"{type(journal_error).__name__}: {journal_error}"
            )

    def recover(self) -> dict[str, Any]:
        """Describe recovery without issuing transport commands."""

        if self._in_memory_recovery:
            state = MissionReplacementState.RECOVERY_REQUIRED
        elif not self.journal.records:
            state = None
        else:
            state = self.state
        if state is None:
            return {
                "recovery_required": False,
                "ready": True,
                "state": None,
                "next_action": "execute",
                "transport_called": False,
            }
        if state in {
            MissionReplacementState.RESUMED,
            MissionReplacementState.HOLD_REQUIRED,
            MissionReplacementState.RETURN_REQUIRED,
            MissionReplacementState.RECOVERY_REQUIRED,
            MissionReplacementState.DENIED,
        }:
            return {
                "recovery_required": state is MissionReplacementState.RECOVERY_REQUIRED,
                "ready": False,
                "state": state.value,
                "next_action": "none",
                "transport_called": False,
            }
        if state is MissionReplacementState.AUTHORIZED:
            next_action = "request_pause"
        elif state is MissionReplacementState.PAUSE_REQUESTED:
            next_action = "confirm_paused_or_fallback"
        elif state is MissionReplacementState.PAUSED:
            next_action = "start_upload_or_fallback"
        elif state in {
            MissionReplacementState.UPLOADING,
            MissionReplacementState.UPLOAD_CONFIRMED,
        }:
            next_action = "verify_readback_or_fallback"
        elif state in {
            MissionReplacementState.READBACK_VERIFIED,
            MissionReplacementState.COMMIT_REQUESTED,
        }:
            next_action = "commit_or_recover"
        elif state in {MissionReplacementState.COMMITTED, MissionReplacementState.CURSOR_REQUESTED}:
            next_action = "confirm_cursor_or_recover"
        elif state is MissionReplacementState.CURSOR_CONFIRMED:
            next_action = "resume_or_recover"
        else:
            next_action = "manual_review"
        return {
            "recovery_required": True,
            "ready": False,
            "state": state.value,
            "next_action": next_action,
            "transport_called": False,
            "write_uncertain": state is not MissionReplacementState.AUTHORIZED,
        }

    def receipt(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "contract": "multidetect.mission-replacement-receipt",
            "binding": self.binding.to_mapping(),
            "binding_sha256": self.binding.canonical_hash,
            "parent_state_required": "start_permitted",
            "state": self.state.value,
            "terminal": self.terminal,
            "records": [record.to_mapping() for record in self.journal.records],
            "journal": self.journal.to_mapping(),
            "recovery_required": self.state is MissionReplacementState.RECOVERY_REQUIRED,
            "recovery_reason": self._in_memory_recovery_reason or self.journal.recovery_reason,
            "mission_replacement_used": True,
            "policy_applied_to_actuator": False,
            "truth_free": True,
        }


__all__ = [
    "MissionReplacementBinding",
    "MissionReplacementError",
    "MissionReplacementJournal",
    "MissionReplacementRecord",
    "MissionReplacementState",
    "MissionReplacementTransaction",
    "MissionReplacementTransport",
]
