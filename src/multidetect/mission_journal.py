"""Durable, fail-closed checkpoints for the PX4 mission transaction.

The PX4 mission compiler and :class:`~multidetect.px4_mission.Px4MissionTransaction`
are deliberately pure and in-memory.  That is useful for deterministic replay,
but it leaves a dangerous gap when a process dies after a MAVLink write and before
the corresponding acknowledgement or readback.  This module adds an append-only
JSONL journal at the transaction boundary without changing the mission compiler,
wire format, or the QGC path.

Every committed line is hash chained and fsynced.  A journal with a truncated
tail, a broken hash chain, a skipped state, or a binding mismatch is never treated
as a successful transaction.  Recovery returns a plan for an operator/runtime
coordinator; it never automatically starts a vehicle or reuses an ambiguous ACK.
``PersistentPx4MissionTransaction`` is a thin adapter which records the existing
transaction methods and can explicitly restore a clean journal checkpoint by
reusing the exact same PX4 transaction implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_json,
    canonical_sha256,
    optional_sha256,
    require_aware_timestamp,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .px4_mission import (
    MAV_MISSION_TYPE_MISSION,
    MissionStartPermit,
    MissionTransactionBindings,
    MissionTransactionState,
    MissionUploadPlan,
    Px4MissionError,
    Px4MissionItemInt,
    Px4MissionTransaction,
)

MISSION_JOURNAL_SCHEMA_VERSION = 1
MISSION_JOURNAL_FORMAT = "multidetect.px4-mission-journal.v1"
MISSION_JOURNAL_RECORD_TYPES = frozenset({"header", "event"})

_STATE_TRANSITIONS: dict[MissionTransactionState, frozenset[MissionTransactionState]] = {
    MissionTransactionState.AUTHORIZED: frozenset(
        {MissionTransactionState.UPLOADING, MissionTransactionState.DENIED}
    ),
    MissionTransactionState.UPLOADING: frozenset(
        {MissionTransactionState.UPLOADED, MissionTransactionState.DENIED}
    ),
    MissionTransactionState.UPLOADED: frozenset(
        {MissionTransactionState.READBACK_VERIFIED, MissionTransactionState.DENIED}
    ),
    MissionTransactionState.READBACK_VERIFIED: frozenset(
        {MissionTransactionState.COMMITTED, MissionTransactionState.DENIED}
    ),
    MissionTransactionState.COMMITTED: frozenset(
        {MissionTransactionState.START_PERMITTED, MissionTransactionState.DENIED}
    ),
    MissionTransactionState.START_PERMITTED: frozenset(),
    MissionTransactionState.DENIED: frozenset(),
}

_TRANSITION_EVENTS = {
    MissionTransactionState.AUTHORIZED: "journal_opened",
    MissionTransactionState.UPLOADING: "upload_started",
    MissionTransactionState.UPLOADED: "upload_acknowledged",
    MissionTransactionState.READBACK_VERIFIED: "readback_verified",
    MissionTransactionState.COMMITTED: "mission_committed",
    MissionTransactionState.START_PERMITTED: "start_permitted",
    MissionTransactionState.DENIED: "transaction_denied",
}
_SAME_STATE_EVENTS = frozenset(
    {"mission_item_requested", "journal_recovery_acknowledged"}
)


class MissionJournalError(AutonomyContractError):
    """Base error for malformed, mismatched, or unusable mission journals."""


class MissionJournalCorruptionError(MissionJournalError):
    """Raised when a committed journal record is malformed or tampered with."""


class MissionJournalRecoveryRequired(MissionJournalError):
    """Raised when a human/runtime coordinator must inspect an interrupted journal."""


def _aware_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MissionJournalError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime | None, name: str = "now_utc") -> str:
    current = datetime.now(timezone.utc) if value is None else _aware_datetime(value, name)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str, name: str = "timestamp") -> datetime:
    text = require_aware_timestamp(value, name)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def _state(value: object, name: str) -> MissionTransactionState:
    if isinstance(value, MissionTransactionState):
        return value
    if not isinstance(value, str):
        raise MissionJournalError(f"{name} must be a mission transaction state")
    try:
        return MissionTransactionState(value.strip().lower())
    except ValueError as exc:
        raise MissionJournalError(f"{name} is not a valid mission transaction state") from exc


def _json_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MissionJournalError(f"{name} must be an object")
    try:
        # canonical_json performs strict finite-number and string-key checks and
        # also detaches nested mutable values through a JSON round-trip.
        return json.loads(canonical_json(value))
    except (AutonomyContractError, TypeError, ValueError) as exc:
        raise MissionJournalError(f"{name} is not strict JSON: {exc}") from exc


def _reject_json_constant(value: str) -> None:
    raise MissionJournalCorruptionError(f"journal contains non-finite JSON number {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MissionJournalCorruptionError(f"journal contains duplicate key {key!r}")
        result[key] = value
    return result


def _decode_record(line: bytes, *, line_number: int) -> Mapping[str, Any]:
    try:
        value = json.loads(
            line.decode("utf-8-sig"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except MissionJournalCorruptionError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MissionJournalCorruptionError(
            f"journal line {line_number} is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise MissionJournalCorruptionError(f"journal line {line_number} must be an object")
    return value


def _bindings_to_mapping(bindings: MissionTransactionBindings) -> dict[str, str]:
    return {
        "prompt_hash": bindings.prompt_hash,
        "task_spec_hash": bindings.task_spec_hash,
        "plan_hash": bindings.plan_hash,
        "context_snapshot_hash": bindings.context_snapshot_hash,
        "airframe_profile_hash": bindings.airframe_profile_hash,
        "geofence_hash": bindings.geofence_hash,
        "vehicle_identity_hash": bindings.vehicle_identity_hash,
        "runtime_descriptor_hash": bindings.runtime_descriptor_hash,
        "runtime_provenance_hash": bindings.runtime_provenance_hash,
    }


def _bindings_from_mapping(value: object) -> MissionTransactionBindings:
    raw = strict_object(
        value,
        name="identity.bindings",
        required=frozenset(
            {
                "prompt_hash",
                "task_spec_hash",
                "plan_hash",
                "context_snapshot_hash",
                "airframe_profile_hash",
                "geofence_hash",
                "vehicle_identity_hash",
                "runtime_descriptor_hash",
                "runtime_provenance_hash",
            }
        ),
    )
    try:
        return MissionTransactionBindings(
            prompt_hash=require_sha256(raw["prompt_hash"], "identity.bindings.prompt_hash"),
            task_spec_hash=require_sha256(
                raw["task_spec_hash"], "identity.bindings.task_spec_hash"
            ),
            plan_hash=require_sha256(raw["plan_hash"], "identity.bindings.plan_hash"),
            context_snapshot_hash=require_sha256(
                raw["context_snapshot_hash"], "identity.bindings.context_snapshot_hash"
            ),
            airframe_profile_hash=require_sha256(
                raw["airframe_profile_hash"], "identity.bindings.airframe_profile_hash"
            ),
            geofence_hash=require_sha256(raw["geofence_hash"], "identity.bindings.geofence_hash"),
            vehicle_identity_hash=require_sha256(
                raw["vehicle_identity_hash"], "identity.bindings.vehicle_identity_hash"
            ),
            runtime_descriptor_hash=require_sha256(
                raw["runtime_descriptor_hash"], "identity.bindings.runtime_descriptor_hash"
            ),
            runtime_provenance_hash=require_sha256(
                raw["runtime_provenance_hash"], "identity.bindings.runtime_provenance_hash"
            ),
        )
    except (AutonomyContractError, TypeError, ValueError) as exc:
        raise MissionJournalError(f"identity.bindings is invalid: {exc}") from exc


@dataclass(frozen=True, slots=True)
class MissionJournalIdentity:
    """Canonical identity of one compiled PX4 transaction."""

    transaction_id: str
    executable_mission_hash: str
    protocol_hash: str
    validation_report_hash: str
    bindings: MissionTransactionBindings
    mission_item_count: int
    airframe_adapter_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "transaction_id",
            require_text(self.transaction_id, "transaction_id"),
        )
        for name in (
            "executable_mission_hash",
            "protocol_hash",
            "validation_report_hash",
        ):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        if not isinstance(self.bindings, MissionTransactionBindings):
            raise MissionJournalError("bindings has the wrong type")
        object.__setattr__(
            self,
            "mission_item_count",
            require_int(
                self.mission_item_count,
                "mission_item_count",
                minimum=1,
                maximum=64,
            ),
        )
        object.__setattr__(
            self,
            "airframe_adapter_id",
            require_text(self.airframe_adapter_id, "airframe_adapter_id"),
        )

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "executable_mission_hash": self.executable_mission_hash,
            "protocol_hash": self.protocol_hash,
            "validation_report_hash": self.validation_report_hash,
            "bindings": _bindings_to_mapping(self.bindings),
            "mission_item_count": self.mission_item_count,
            "airframe_adapter_id": self.airframe_adapter_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> MissionJournalIdentity:
        raw = strict_object(
            value,
            name="identity",
            required=frozenset(
                {
                    "transaction_id",
                    "executable_mission_hash",
                    "protocol_hash",
                    "validation_report_hash",
                    "bindings",
                    "mission_item_count",
                    "airframe_adapter_id",
                }
            ),
        )
        try:
            return cls(
                transaction_id=require_text(raw["transaction_id"], "identity.transaction_id"),
                executable_mission_hash=require_sha256(
                    raw["executable_mission_hash"], "identity.executable_mission_hash"
                ),
                protocol_hash=require_sha256(raw["protocol_hash"], "identity.protocol_hash"),
                validation_report_hash=require_sha256(
                    raw["validation_report_hash"], "identity.validation_report_hash"
                ),
                bindings=_bindings_from_mapping(raw["bindings"]),
                mission_item_count=require_int(
                    raw["mission_item_count"], "identity.mission_item_count", minimum=1, maximum=64
                ),
                airframe_adapter_id=require_text(
                    raw["airframe_adapter_id"], "identity.airframe_adapter_id"
                ),
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalError(f"invalid mission journal identity: {exc}") from exc

    @classmethod
    def from_transaction(cls, transaction: Px4MissionTransaction) -> MissionJournalIdentity:
        """Derive identity without changing the core transaction API.

        The transaction remains intentionally headless.  This adapter lives in
        the same package and reads its immutable construction inputs so SIM,
        HIL, and REAL callers can all use the exact same identity bindings.
        """

        if not isinstance(transaction, Px4MissionTransaction):
            raise MissionJournalError("transaction has the wrong type")
        compiled = getattr(transaction, "_compiled", None)
        validation_report = getattr(transaction, "_validation_report", None)
        bindings = getattr(transaction, "_bindings", None)
        transaction_id = getattr(transaction, "_transaction_id", None)
        if compiled is None or validation_report is None or bindings is None:
            raise MissionJournalError("transaction does not expose complete PX4 bindings")
        mission = compiled.executable_mission
        return cls(
            transaction_id=transaction_id,
            executable_mission_hash=mission.canonical_hash,
            protocol_hash=compiled.wire_hash,
            validation_report_hash=validation_report.canonical_hash,
            bindings=bindings,
            mission_item_count=len(compiled.wire_items),
            airframe_adapter_id=mission.airframe_adapter_id,
        )


@dataclass(frozen=True, slots=True)
class MissionJournalRecord:
    """One hash-chained, committed journal line."""

    schema_version: int
    record_type: str
    journal_id: str
    sequence: int
    transaction_id: str
    identity_hash: str
    event_type: str
    from_state: MissionTransactionState | None
    to_state: MissionTransactionState
    occurred_at: str
    payload: Mapping[str, Any]
    previous_record_hash: str | None
    record_hash: str

    def __post_init__(self) -> None:
        require_schema_version(self.schema_version, expected=MISSION_JOURNAL_SCHEMA_VERSION)
        record_type = require_text(self.record_type, "record_type")
        if record_type not in MISSION_JOURNAL_RECORD_TYPES:
            raise MissionJournalError(f"record_type {record_type!r} is not supported")
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "journal_id", require_text(self.journal_id, "journal_id"))
        object.__setattr__(self, "sequence", require_int(self.sequence, "sequence", minimum=0))
        object.__setattr__(
            self, "transaction_id", require_text(self.transaction_id, "transaction_id")
        )
        object.__setattr__(
            self,
            "identity_hash",
            require_sha256(self.identity_hash, "identity_hash"),
        )
        object.__setattr__(self, "event_type", require_text(self.event_type, "event_type"))
        if self.from_state is not None:
            object.__setattr__(self, "from_state", _state(self.from_state, "from_state"))
        object.__setattr__(self, "to_state", _state(self.to_state, "to_state"))
        object.__setattr__(
            self, "occurred_at", require_aware_timestamp(self.occurred_at, "occurred_at")
        )
        object.__setattr__(self, "payload", _json_object(self.payload, "payload"))
        object.__setattr__(
            self,
            "previous_record_hash",
            optional_sha256(self.previous_record_hash, "previous_record_hash"),
        )
        object.__setattr__(self, "record_hash", require_sha256(self.record_hash, "record_hash"))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "journal_id": self.journal_id,
            "sequence": self.sequence,
            "transaction_id": self.transaction_id,
            "identity_hash": self.identity_hash,
            "event_type": self.event_type,
            "from_state": self.from_state.value if self.from_state is not None else None,
            "to_state": self.to_state.value,
            "occurred_at": self.occurred_at,
            "payload": json.loads(canonical_json(self.payload)),
            "previous_record_hash": self.previous_record_hash,
            "record_hash": self.record_hash,
        }

    def _hash_input(self) -> dict[str, Any]:
        value = self.to_mapping()
        value.pop("record_hash")
        return value

    @classmethod
    def build(
        cls,
        *,
        record_type: str,
        journal_id: str,
        sequence: int,
        transaction_id: str,
        identity_hash: str,
        event_type: str,
        from_state: MissionTransactionState | None,
        to_state: MissionTransactionState,
        occurred_at: str,
        payload: Mapping[str, Any],
        previous_record_hash: str | None,
    ) -> MissionJournalRecord:
        candidate = cls(
            schema_version=MISSION_JOURNAL_SCHEMA_VERSION,
            record_type=record_type,
            journal_id=journal_id,
            sequence=sequence,
            transaction_id=transaction_id,
            identity_hash=identity_hash,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            occurred_at=occurred_at,
            payload=payload,
            previous_record_hash=previous_record_hash,
            record_hash="0" * 64,
        )
        return cls(
            schema_version=candidate.schema_version,
            record_type=candidate.record_type,
            journal_id=candidate.journal_id,
            sequence=candidate.sequence,
            transaction_id=candidate.transaction_id,
            identity_hash=candidate.identity_hash,
            event_type=candidate.event_type,
            from_state=candidate.from_state,
            to_state=candidate.to_state,
            occurred_at=candidate.occurred_at,
            payload=candidate.payload,
            previous_record_hash=candidate.previous_record_hash,
            record_hash=canonical_sha256(candidate._hash_input()),
        )

    @classmethod
    def from_mapping(cls, value: object, *, line_number: int = 1) -> MissionJournalRecord:
        try:
            raw = strict_object(
                value,
                name=f"journal line {line_number}",
                required=frozenset(
                    {
                        "schema_version",
                        "record_type",
                        "journal_id",
                        "sequence",
                        "transaction_id",
                        "identity_hash",
                        "event_type",
                        "from_state",
                        "to_state",
                        "occurred_at",
                        "payload",
                        "previous_record_hash",
                        "record_hash",
                    }
                ),
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalCorruptionError(
                f"journal line {line_number} violates the record contract: {exc}"
            ) from exc
        try:
            record = cls(
                schema_version=require_schema_version(
                    raw["schema_version"], expected=MISSION_JOURNAL_SCHEMA_VERSION
                ),
                record_type=require_text(raw["record_type"], "record_type"),
                journal_id=require_text(raw["journal_id"], "journal_id"),
                sequence=require_int(raw["sequence"], "sequence", minimum=0),
                transaction_id=require_text(raw["transaction_id"], "transaction_id"),
                identity_hash=require_sha256(raw["identity_hash"], "identity_hash"),
                event_type=require_text(raw["event_type"], "event_type"),
                from_state=(
                    None
                    if raw["from_state"] is None
                    else _state(raw["from_state"], "from_state")
                ),
                to_state=_state(raw["to_state"], "to_state"),
                occurred_at=require_aware_timestamp(raw["occurred_at"], "occurred_at"),
                payload=_json_object(raw["payload"], "payload"),
                previous_record_hash=optional_sha256(
                    raw["previous_record_hash"], "previous_record_hash"
                ),
                record_hash=require_sha256(raw["record_hash"], "record_hash"),
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalCorruptionError(
                f"journal line {line_number} violates the record contract: {exc}"
            ) from exc
        expected_hash = canonical_sha256(record._hash_input())
        if expected_hash != record.record_hash:
            raise MissionJournalCorruptionError(
                f"journal line {line_number} record hash mismatch"
            )
        return record

    @property
    def state(self) -> MissionTransactionState:
        return self.to_state


@dataclass(frozen=True, slots=True)
class MissionResumePlan:
    """Explicit next action after a clean journal checkpoint."""

    state: MissionTransactionState
    requested_sequences: tuple[int, ...]
    missing_sequences: tuple[int, ...]
    next_action: str
    requires_operator_action: bool
    auto_start_allowed: bool
    requires_readback: bool
    recovery_required: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "requested_sequences": list(self.requested_sequences),
            "missing_sequences": list(self.missing_sequences),
            "next_action": self.next_action,
            "requires_operator_action": self.requires_operator_action,
            "auto_start_allowed": self.auto_start_allowed,
            "requires_readback": self.requires_readback,
            "recovery_required": self.recovery_required,
        }


@dataclass(frozen=True, slots=True)
class MissionJournalRecovery:
    """Immutable recovery view returned by :meth:`DurableMissionJournal.recover`."""

    path: Path
    identity: MissionJournalIdentity
    journal_id: str
    records: tuple[MissionJournalRecord, ...]
    requested_sequences: tuple[int, ...]
    recovery_required: bool
    recovery_reason: str | None

    @property
    def state(self) -> MissionTransactionState:
        return self.records[-1].to_state

    @property
    def last_sequence(self) -> int:
        return self.records[-1].sequence

    @property
    def resume_plan(self) -> MissionResumePlan:
        return _make_resume_plan(
            self.state,
            requested_sequences=self.requested_sequences,
            mission_item_count=self.identity.mission_item_count,
            recovery_required=self.recovery_required,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": MISSION_JOURNAL_SCHEMA_VERSION,
            "path": str(self.path),
            "journal_id": self.journal_id,
            "identity": self.identity.to_mapping(),
            "identity_hash": self.identity.canonical_hash,
            "state": self.state.value,
            "last_sequence": self.last_sequence,
            "record_count": len(self.records),
            "requested_sequences": list(self.requested_sequences),
            "recovery_required": self.recovery_required,
            "recovery_reason": self.recovery_reason,
            "resume_plan": self.resume_plan.to_mapping(),
        }


def _make_resume_plan(
    state: MissionTransactionState,
    *,
    requested_sequences: tuple[int, ...],
    mission_item_count: int,
    recovery_required: bool,
) -> MissionResumePlan:
    missing = (
        tuple(index for index in range(mission_item_count) if index not in requested_sequences)
        if state is MissionTransactionState.UPLOADING
        else ()
    )
    if recovery_required:
        action = "repair_journal_tail"
    elif state is MissionTransactionState.AUTHORIZED:
        action = "begin_upload"
    elif state is MissionTransactionState.UPLOADING:
        action = "request_missing_items" if missing else "await_upload_ack"
    elif state is MissionTransactionState.UPLOADED:
        action = "verify_readback"
    elif state is MissionTransactionState.READBACK_VERIFIED:
        action = "commit"
    elif state is MissionTransactionState.COMMITTED:
        action = "permit_start"
    elif state is MissionTransactionState.START_PERMITTED:
        action = "start_permit_already_issued"
    else:
        action = "terminal_denied"
    return MissionResumePlan(
        state=state,
        requested_sequences=requested_sequences,
        missing_sequences=missing,
        next_action=action,
        requires_operator_action=True,
        # A journal is evidence of what happened, never an implicit command to
        # enter AUTO.MISSION after a restart.
        auto_start_allowed=False,
        requires_readback=state
        in {
            MissionTransactionState.UPLOADED,
            MissionTransactionState.UPLOADING,
        },
        recovery_required=recovery_required,
    )


def _identity_from_value(
    value: MissionJournalIdentity | Px4MissionTransaction,
) -> MissionJournalIdentity:
    if isinstance(value, MissionJournalIdentity):
        return value
    if isinstance(value, Px4MissionTransaction):
        return MissionJournalIdentity.from_transaction(value)
    raise MissionJournalError("identity must be MissionJournalIdentity or Px4MissionTransaction")


def _encode_record(record: MissionJournalRecord) -> bytes:
    encoded = json.dumps(
        record.to_mapping(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    return encoded.encode("utf-8")


class DurableMissionJournal:
    """Thread-safe append-only mission transaction journal.

    ``create=True`` creates a header only when a complete transaction identity
    is supplied.  Existing files are never silently repaired or truncated.
    Call :meth:`repair_truncated_tail` after an explicit operator review when a
    process died while appending a line.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        identity: MissionJournalIdentity | Px4MissionTransaction | None = None,
        journal_id: str | None = None,
        now_utc: datetime | None = None,
        create: bool = True,
    ) -> None:
        self._path = Path(path).expanduser().resolve()
        if self._path.exists() and self._path.is_dir():
            raise MissionJournalError(f"journal path is a directory: {self._path}")
        self._lock = RLock()
        self._closed = False
        self._identity: MissionJournalIdentity | None = (
            _identity_from_value(identity) if identity is not None else None
        )
        self._requested_sequences: set[int] = set()
        self._records: list[MissionJournalRecord] = []
        self._stream = None
        self._committed_bytes = 0
        self._prefix_sha256 = ""
        self._recovery_required = False
        self._recovery_reason: str | None = None
        if journal_id is not None:
            journal_id = require_text(journal_id, "journal_id")
        self._journal_id = journal_id

        if self._path.exists():
            if self._path.stat().st_size == 0:
                if not create or self._identity is None:
                    raise MissionJournalCorruptionError(
                        "empty mission journal has no committed header"
                    )
                self._create_header(now_utc=now_utc)
            else:
                self._load()
                self._check_identity()
        elif create:
            if self._identity is None:
                raise MissionJournalError("identity is required when creating a mission journal")
            self._create_header(now_utc=now_utc)
        else:
            raise MissionJournalError(f"mission journal does not exist: {self._path}")

    @classmethod
    def create(
        cls,
        path: str | Path,
        identity: MissionJournalIdentity | Px4MissionTransaction,
        *,
        journal_id: str | None = None,
        now_utc: datetime | None = None,
    ) -> DurableMissionJournal:
        return cls(
            path,
            identity=identity,
            journal_id=journal_id,
            now_utc=now_utc,
            create=True,
        )

    @classmethod
    def open(cls, path: str | Path) -> DurableMissionJournal:
        return cls(path, create=False)

    load = open

    @property
    def path(self) -> Path:
        return self._path

    @property
    def identity(self) -> MissionJournalIdentity:
        if self._identity is None:  # pragma: no cover - guarded by load/create
            raise MissionJournalError("journal identity is unavailable")
        return self._identity

    @property
    def journal_id(self) -> str:
        return self._journal_id or ""

    @property
    def records(self) -> tuple[MissionJournalRecord, ...]:
        with self._lock:
            return tuple(self._records)

    @property
    def state(self) -> MissionTransactionState:
        with self._lock:
            if not self._records:
                raise MissionJournalError("journal has no committed records")
            return self._records[-1].to_state

    @property
    def requested_sequences(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(sorted(self._requested_sequences))

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    @property
    def recovery_reason(self) -> str | None:
        return self._recovery_reason

    def recover(self) -> MissionJournalRecovery:
        with self._lock:
            return MissionJournalRecovery(
                path=self._path,
                identity=self.identity,
                journal_id=self.journal_id,
                records=tuple(self._records),
                requested_sequences=tuple(sorted(self._requested_sequences)),
                recovery_required=self._recovery_required,
                recovery_reason=self._recovery_reason,
            )

    inspect = recover

    def resume_plan(self) -> MissionResumePlan:
        return self.recover().resume_plan

    def _check_identity(self) -> None:
        if self._identity is None:
            return
        if self._records[0].identity_hash != self._identity.canonical_hash:
            raise MissionJournalCorruptionError("journal header identity hash mismatch")
        header_payload = self._records[0].payload
        if header_payload.get("identity") != self._identity.to_mapping():
            raise MissionJournalError("journal identity binding mismatch")

    def _create_header(self, *, now_utc: datetime | None) -> None:
        assert self._identity is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_id = self._journal_id or uuid4().hex
        header = MissionJournalRecord.build(
            record_type="header",
            journal_id=self._journal_id,
            sequence=0,
            transaction_id=self._identity.transaction_id,
            identity_hash=self._identity.canonical_hash,
            event_type="journal_opened",
            from_state=None,
            to_state=MissionTransactionState.AUTHORIZED,
            occurred_at=_timestamp(now_utc),
            payload={"format": MISSION_JOURNAL_FORMAT, "identity": self._identity.to_mapping()},
            previous_record_hash=None,
        )
        try:
            with self._path.open("xb") as handle:
                payload = _encode_record(header)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise MissionJournalError(
                f"mission journal appeared while creating: {self._path}"
            ) from exc
        self._records = [header]
        encoded = _encode_record(header)
        self._committed_bytes = len(encoded)
        self._prefix_sha256 = hashlib.sha256(encoded).hexdigest()
        self._open_stream()

    def _open_stream(self) -> None:
        if self._recovery_required:
            return
        self._stream = self._path.open("a", encoding="utf-8", newline="\n")

    def _load(self) -> None:
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise MissionJournalError(f"cannot read mission journal: {exc}") from exc
        chunks = raw.splitlines(keepends=True)
        tail = b""
        if chunks and not chunks[-1].endswith(b"\n"):
            tail = chunks.pop()
        committed_bytes = sum(len(chunk) for chunk in chunks)
        if not chunks:
            raise MissionJournalCorruptionError(
                "mission journal has no committed newline-terminated header"
            )
        records = [
            MissionJournalRecord.from_mapping(
                _decode_record(chunk.rstrip(b"\r\n"), line_number=index)
            )
            for index, chunk in enumerate(chunks, start=1)
        ]
        self._records = records
        self._committed_bytes = committed_bytes
        self._prefix_sha256 = hashlib.sha256(raw[:committed_bytes]).hexdigest()
        self._identity = self._identity or self._identity_from_header(records[0])
        self._journal_id = self._journal_id or records[0].journal_id
        self._validate_records()
        if tail:
            self._recovery_required = True
            self._recovery_reason = (
                "journal has an uncommitted or truncated trailing record; "
                "operator review is required before any resume"
            )
        else:
            self._open_stream()

    @staticmethod
    def _identity_from_header(header: MissionJournalRecord) -> MissionJournalIdentity:
        if header.record_type != "header":
            raise MissionJournalCorruptionError("first mission journal record must be a header")
        try:
            payload = strict_object(
                header.payload,
                name="journal header payload",
                required=frozenset({"format", "identity"}),
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalCorruptionError(
                f"journal header payload is invalid: {exc}"
            ) from exc
        if payload["format"] != MISSION_JOURNAL_FORMAT:
            raise MissionJournalCorruptionError("unsupported mission journal format")
        identity = MissionJournalIdentity.from_mapping(payload["identity"])
        if header.identity_hash != identity.canonical_hash:
            raise MissionJournalCorruptionError("journal header identity hash mismatch")
        return identity

    def _validate_records(self) -> None:
        if not self._records:
            raise MissionJournalCorruptionError("mission journal has no records")
        identity = self._identity_from_header(self._records[0])
        if self._identity is not None and identity.to_mapping() != self._identity.to_mapping():
            raise MissionJournalError("mission journal identity does not match requested identity")
        self._identity = identity
        header = self._records[0]
        if (
            header.sequence != 0
            or header.from_state is not None
            or header.to_state is not MissionTransactionState.AUTHORIZED
            or header.event_type != "journal_opened"
            or header.previous_record_hash is not None
        ):
            raise MissionJournalCorruptionError("mission journal header lifecycle is invalid")
        if self._journal_id != header.journal_id:
            raise MissionJournalCorruptionError("mission journal id mismatch")
        self._requested_sequences.clear()
        for expected_sequence, record in enumerate(self._records):
            if record.sequence != expected_sequence:
                raise MissionJournalCorruptionError("mission journal sequence is not contiguous")
            if record.journal_id != self._journal_id:
                raise MissionJournalCorruptionError("mission journal record id mismatch")
            if record.transaction_id != self._identity.transaction_id:
                raise MissionJournalCorruptionError("mission journal transaction id mismatch")
            if record.identity_hash != self._identity.canonical_hash:
                raise MissionJournalCorruptionError("mission journal record identity mismatch")
            if expected_sequence == 0:
                continue
            previous = self._records[expected_sequence - 1]
            if record.record_type != "event" or record.previous_record_hash != previous.record_hash:
                raise MissionJournalCorruptionError("mission journal hash chain is broken")
            self._validate_event(previous, record)

    def _validate_event(
        self,
        previous: MissionJournalRecord,
        record: MissionJournalRecord,
    ) -> None:
        if previous.to_state in {
            MissionTransactionState.DENIED,
            MissionTransactionState.START_PERMITTED,
        }:
            if (
                record.from_state is previous.to_state
                and record.to_state is previous.to_state
                and record.event_type == "journal_recovery_acknowledged"
            ):
                self._validate_recovery_ack_payload(record.payload)
                return
            raise MissionJournalCorruptionError("journal contains an event after a terminal state")
        if record.from_state is not previous.to_state:
            raise MissionJournalCorruptionError(
                "journal event from_state does not match checkpoint"
            )
        if record.from_state is record.to_state:
            if record.event_type not in _SAME_STATE_EVENTS:
                raise MissionJournalCorruptionError("journal contains an unknown same-state event")
            if record.to_state is not MissionTransactionState.UPLOADING:
                if record.event_type != "journal_recovery_acknowledged":
                    raise MissionJournalCorruptionError(
                        "same-state event is outside uploading state"
                    )
            if record.event_type == "mission_item_requested":
                self._validate_item_request_payload(record.payload)
                self._requested_sequences.add(int(record.payload["sequence"]))
            else:
                self._validate_recovery_ack_payload(record.payload)
            return
        if record.to_state not in _STATE_TRANSITIONS[record.from_state]:
            raise MissionJournalCorruptionError("journal state transition is not allowed")
        if record.event_type != _TRANSITION_EVENTS[record.to_state]:
            raise MissionJournalCorruptionError("journal transition event name is not canonical")
        self._validate_transition_payload(record.to_state, record.payload)
        if record.to_state is MissionTransactionState.DENIED:
            try:
                payload = strict_object(
                    record.payload,
                    name="transaction_denied payload",
                    required=frozenset({"reason"}),
                )
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"transaction_denied payload is invalid: {exc}"
                ) from exc
            try:
                require_text(payload["reason"], "transaction_denied.reason")
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"transaction_denied.reason is invalid: {exc}"
                ) from exc

    def _validate_transition_payload(
        self,
        to_state: MissionTransactionState,
        payload: Mapping[str, Any],
    ) -> None:
        if to_state is MissionTransactionState.UPLOADING:
            try:
                raw = strict_object(
                    payload,
                    name="upload_started payload",
                    required=frozenset({"protocol_hash", "item_count"}),
                )
                protocol_hash = require_sha256(
                    raw["protocol_hash"], "upload_started.protocol_hash"
                )
                item_count = require_int(
                    raw["item_count"],
                    "upload_started.item_count",
                    minimum=1,
                    maximum=self.identity.mission_item_count,
                )
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"upload_started payload is invalid: {exc}"
                ) from exc
            if (
                protocol_hash != self.identity.protocol_hash
                or item_count != self.identity.mission_item_count
            ):
                raise MissionJournalCorruptionError(
                    "upload_started payload does not match identity"
                )
        elif to_state is MissionTransactionState.UPLOADED:
            try:
                raw = strict_object(
                    payload,
                    name="upload_acknowledged payload",
                    required=frozenset(
                        {"ack_type", "mission_type", "source_system", "source_component"}
                    ),
                )
                ack_type = require_int(raw["ack_type"], "upload_acknowledged.ack_type")
                mission_type = require_int(
                    raw["mission_type"], "upload_acknowledged.mission_type"
                )
                source_system = require_int(
                    raw["source_system"], "upload_acknowledged.source_system"
                )
                source_component = require_int(
                    raw["source_component"], "upload_acknowledged.source_component"
                )
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"upload_acknowledged payload is invalid: {exc}"
                ) from exc
            if (ack_type, mission_type, source_system, source_component) != (0, 0, 1, 1):
                raise MissionJournalCorruptionError(
                    "upload acknowledgement payload is not PX4 1/1 PASS"
                )
        elif to_state is MissionTransactionState.READBACK_VERIFIED:
            try:
                raw = strict_object(
                    payload,
                    name="readback_verified payload",
                    required=frozenset({"protocol_hash", "item_count"}),
                )
                protocol_hash = require_sha256(
                    raw["protocol_hash"], "readback_verified.protocol_hash"
                )
                item_count = require_int(
                    raw["item_count"],
                    "readback_verified.item_count",
                    minimum=1,
                    maximum=self.identity.mission_item_count,
                )
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"readback_verified payload is invalid: {exc}"
                ) from exc
            if (
                protocol_hash != self.identity.protocol_hash
                or item_count != self.identity.mission_item_count
            ):
                raise MissionJournalCorruptionError("readback payload does not match identity")
        elif to_state is MissionTransactionState.COMMITTED:
            try:
                raw = strict_object(
                    payload,
                    name="mission_committed payload",
                    required=frozenset({"protocol_hash"}),
                )
                protocol_hash = require_sha256(
                    raw["protocol_hash"], "mission_committed.protocol_hash"
                )
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"mission_committed payload is invalid: {exc}"
                ) from exc
            if protocol_hash != self.identity.protocol_hash:
                raise MissionJournalCorruptionError("commit payload does not match identity")
        elif to_state is MissionTransactionState.START_PERMITTED:
            try:
                raw = strict_object(
                    payload,
                    name="start_permitted payload",
                    required=frozenset(
                        {"transaction_id", "executable_mission_hash", "protocol_hash"}
                    ),
                )
                transaction_id = require_text(
                    raw["transaction_id"], "start_permitted.transaction_id"
                )
                executable_hash = require_sha256(
                    raw["executable_mission_hash"],
                    "start_permitted.executable_mission_hash",
                )
                protocol_hash = require_sha256(
                    raw["protocol_hash"], "start_permitted.protocol_hash"
                )
            except (AutonomyContractError, TypeError, ValueError) as exc:
                raise MissionJournalCorruptionError(
                    f"start_permitted payload is invalid: {exc}"
                ) from exc
            if (
                transaction_id != self.identity.transaction_id
                or executable_hash != self.identity.executable_mission_hash
                or protocol_hash != self.identity.protocol_hash
            ):
                raise MissionJournalCorruptionError("start permit payload does not match identity")

    def _validate_item_request_payload(self, payload: Mapping[str, Any]) -> None:
        try:
            raw = strict_object(
                payload,
                name="mission_item_requested payload",
                required=frozenset({"sequence", "request_message_type"}),
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalCorruptionError(
                f"mission_item_requested payload is invalid: {exc}"
            ) from exc
        try:
            sequence = require_int(
                raw["sequence"],
                "mission_item_requested.sequence",
                minimum=0,
                maximum=self.identity.mission_item_count - 1,
            )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalCorruptionError(
                f"mission_item_requested.sequence is invalid: {exc}"
            ) from exc
        if raw["request_message_type"] != "MISSION_REQUEST_INT":
            raise MissionJournalCorruptionError("journal contains a legacy mission request")
        # Keep this assignment explicit for callers that only use validation as
        # a side effect while rebuilding the requested sequence set.
        _ = sequence

    @staticmethod
    def _validate_recovery_ack_payload(payload: Mapping[str, Any]) -> None:
        try:
            raw = strict_object(
                payload,
                name="journal_recovery_acknowledged payload",
                required=frozenset(
                    {"operator_note", "quarantined_bytes", "quarantined_sha256"}
                ),
                optional=frozenset({"quarantine_path"}),
            )
            require_text(
                raw["operator_note"],
                "journal_recovery_acknowledged.operator_note",
            )
            require_int(
                raw["quarantined_bytes"],
                "journal_recovery_acknowledged.quarantined_bytes",
                minimum=0,
            )
            require_sha256(
                raw["quarantined_sha256"],
                "journal_recovery_acknowledged.quarantined_sha256",
            )
            if "quarantine_path" in raw:
                require_text(
                    raw["quarantine_path"],
                    "journal_recovery_acknowledged.quarantine_path",
                )
        except (AutonomyContractError, TypeError, ValueError) as exc:
            raise MissionJournalCorruptionError(
                f"journal_recovery_acknowledged payload is invalid: {exc}"
            ) from exc

    def _ensure_writable(self) -> None:
        if self._closed:
            raise MissionJournalError("mission journal is closed")
        if self._recovery_required:
            raise MissionJournalRecoveryRequired(
                self._recovery_reason or "journal recovery required"
            )
        if self._stream is None:
            self._open_stream()

    def _ensure_prefix_unchanged(self) -> None:
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise MissionJournalRecoveryRequired(
                f"cannot re-read mission journal before append: {exc}"
            ) from exc
        if len(raw) != self._committed_bytes:
            self._recovery_required = True
            self._recovery_reason = (
                "journal changed after opening; an uncommitted tail or external write "
                "requires review"
            )
            raise MissionJournalRecoveryRequired(self._recovery_reason)
        if hashlib.sha256(raw).hexdigest() != self._prefix_sha256:
            raise MissionJournalCorruptionError("journal committed prefix changed after opening")

    def _append(
        self,
        *,
        event_type: str,
        from_state: MissionTransactionState,
        to_state: MissionTransactionState,
        payload: Mapping[str, Any],
        now_utc: datetime | None,
    ) -> MissionJournalRecord:
        self._ensure_writable()
        self._ensure_prefix_unchanged()
        if self._records[-1].to_state is not from_state:
            raise MissionJournalError("journal append from_state does not match current state")
        if from_state is to_state:
            if event_type == "mission_item_requested":
                self._validate_item_request_payload(payload)
            elif event_type == "journal_recovery_acknowledged":
                self._validate_recovery_ack_payload(payload)
            else:
                raise MissionJournalError("unknown same-state journal event")
        else:
            if to_state not in _STATE_TRANSITIONS[from_state]:
                raise MissionJournalError("journal state transition is not allowed")
            if event_type != _TRANSITION_EVENTS[to_state]:
                raise MissionJournalError("journal transition event name is not canonical")
            self._validate_transition_payload(to_state, payload)
            if to_state is MissionTransactionState.DENIED:
                try:
                    denial = strict_object(
                        payload,
                        name="transaction_denied payload",
                        required=frozenset({"reason"}),
                    )
                    require_text(denial["reason"], "transaction_denied.reason")
                except (AutonomyContractError, TypeError, ValueError) as exc:
                    raise MissionJournalError(
                        f"transaction_denied payload is invalid: {exc}"
                    ) from exc
        occurred_at = _timestamp(now_utc)
        previous_timestamp = _parse_timestamp(
            self._records[-1].occurred_at, "previous occurred_at"
        )
        if _parse_timestamp(occurred_at) < previous_timestamp:
            if now_utc is None:
                # A caller that omits a clock (for example a deterministic
                # simulator) inherits the last committed timestamp instead of
                # introducing a wall-clock jump backwards.
                occurred_at = self._records[-1].occurred_at
            else:
                raise MissionJournalError("journal timestamp cannot move backwards")
        record = MissionJournalRecord.build(
            record_type="event",
            journal_id=self.journal_id,
            sequence=len(self._records),
            transaction_id=self.identity.transaction_id,
            identity_hash=self.identity.canonical_hash,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            occurred_at=occurred_at,
            payload=payload,
            previous_record_hash=self._records[-1].record_hash,
        )
        encoded = _encode_record(record)
        try:
            assert self._stream is not None
            self._stream.write(encoded.decode("utf-8"))
            self._stream.flush()
            os.fsync(self._stream.fileno())
        except (OSError, UnicodeError) as exc:
            # The write may have reached the filesystem only partially.  Do not
            # expose a permit; force a fresh load/review on the next operation.
            self._recovery_required = True
            self._recovery_reason = f"journal append failed and may be partial: {exc}"
            raise MissionJournalRecoveryRequired(self._recovery_reason) from exc
        self._records.append(record)
        self._committed_bytes += len(encoded)
        self._prefix_sha256 = hashlib.sha256(
            self._path.read_bytes()[: self._committed_bytes]
        ).hexdigest()
        if event_type == "mission_item_requested":
            self._requested_sequences.add(int(record.payload["sequence"]))
        return record

    def record_transition(
        self,
        to_state: MissionTransactionState,
        *,
        event_type: str | None = None,
        payload: Mapping[str, Any] | None = None,
        now_utc: datetime | None = None,
        expected_state: MissionTransactionState | None = None,
    ) -> MissionJournalRecord:
        """Append one canonical state transition after the core call succeeds."""

        with self._lock:
            current = self.state
            expected = (
                None
                if expected_state is None
                else _state(expected_state, "expected_state")
            )
            if expected is not None and current is not expected:
                raise MissionJournalError(
                    f"journal state {current.value} does not match expected {expected.value}"
                )
            target = _state(to_state, "to_state")
            if target is current or target not in _STATE_TRANSITIONS[current]:
                raise MissionJournalError(
                    f"journal state {current.value} does not permit {target.value}"
                )
            canonical_event = _TRANSITION_EVENTS[target]
            if (
                event_type is not None
                and require_text(event_type, "event_type") != canonical_event
            ):
                raise MissionJournalError(
                    "event_type does not match the canonical state transition"
                )
            return self._append(
                event_type=canonical_event,
                from_state=current,
                to_state=target,
                payload={} if payload is None else payload,
                now_utc=now_utc,
            )

    append_transition = record_transition

    def record_item_request(
        self,
        sequence: int,
        *,
        request_message_type: str = "MISSION_REQUEST_INT",
        now_utc: datetime | None = None,
    ) -> MissionJournalRecord:
        with self._lock:
            if self.state is not MissionTransactionState.UPLOADING:
                raise MissionJournalError("mission item requests require the uploading state")
            sequence = require_int(
                sequence,
                "sequence",
                minimum=0,
                maximum=self.identity.mission_item_count - 1,
            )
            if request_message_type != "MISSION_REQUEST_INT":
                raise MissionJournalError("legacy MISSION_REQUEST is not accepted")
            return self._append(
                event_type="mission_item_requested",
                from_state=MissionTransactionState.UPLOADING,
                to_state=MissionTransactionState.UPLOADING,
                payload={
                    "sequence": sequence,
                    "request_message_type": request_message_type,
                },
                now_utc=now_utc,
            )

    append_item_request = record_item_request

    def record_denied(
        self,
        reason: str,
        *,
        now_utc: datetime | None = None,
    ) -> MissionJournalRecord:
        with self._lock:
            return self.record_transition(
                MissionTransactionState.DENIED,
                payload={"reason": require_text(reason, "reason")},
                now_utc=now_utc,
            )

    def repair_truncated_tail(
        self,
        *,
        operator_note: str,
        quarantine_path: str | Path | None = None,
        now_utc: datetime | None = None,
    ) -> MissionJournalRecord:
        """Quarantine an uncommitted tail, then append an explicit repair event.

        This is intentionally a separate, opt-in mutation.  The original tail
        is copied to a sibling ``.recovery-tail`` file (or the caller-selected
        path) before the journal is truncated to its last newline-terminated
        record.
        """

        with self._lock:
            if not self._recovery_required:
                raise MissionJournalError("journal does not require tail recovery")
            note = require_text(operator_note, "operator_note")
            raw = self._path.read_bytes()
            tail = raw[self._committed_bytes :]
            destination = (
                Path(quarantine_path).expanduser().resolve()
                if quarantine_path is not None
                else self._path.with_name(self._path.name + ".recovery-tail")
            )
            if destination == self._path:
                raise MissionJournalError("quarantine_path cannot overwrite the mission journal")
            if tail:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise MissionJournalError(f"quarantine path already exists: {destination}")
                with destination.open("xb") as handle:
                    handle.write(tail)
                    handle.flush()
                    os.fsync(handle.fileno())
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            with self._path.open("rb+") as handle:
                handle.truncate(self._committed_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            self._recovery_required = False
            self._recovery_reason = None
            self._open_stream()
            payload: dict[str, Any] = {
                "operator_note": note,
                "quarantined_bytes": len(tail),
                "quarantined_sha256": hashlib.sha256(tail).hexdigest(),
            }
            if tail:
                payload["quarantine_path"] = str(destination)
            return self._append(
                event_type="journal_recovery_acknowledged",
                from_state=self.state,
                to_state=self.state,
                payload=payload,
                now_utc=now_utc,
            )

    acknowledge_recovery = repair_truncated_tail

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()

    def __enter__(self) -> DurableMissionJournal:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()


class PersistentPx4MissionTransaction:
    """Record an existing :class:`Px4MissionTransaction` in a durable journal."""

    def __init__(
        self,
        transaction: Px4MissionTransaction,
        journal: DurableMissionJournal,
        *,
        resume: bool = False,
    ) -> None:
        if not isinstance(transaction, Px4MissionTransaction):
            raise MissionJournalError("transaction has the wrong type")
        if not isinstance(journal, DurableMissionJournal):
            raise MissionJournalError("journal has the wrong type")
        identity = MissionJournalIdentity.from_transaction(transaction)
        if identity.to_mapping() != journal.identity.to_mapping():
            raise MissionJournalError("transaction and mission journal bindings do not match")
        if journal.recovery_required:
            raise MissionJournalRecoveryRequired(
                journal.recovery_reason or "journal recovery required"
            )
        if journal.state is not transaction.state:
            if not resume:
                raise MissionJournalRecoveryRequired(
                    "journal checkpoint is ahead of the new in-memory transaction; "
                    "call PersistentPx4MissionTransaction.resume explicitly"
                )
            self._restore_checkpoint(transaction, journal)
        self._transaction = transaction
        self._journal = journal
        self._require_sync()

    @staticmethod
    def _restore_checkpoint(
        transaction: Px4MissionTransaction,
        journal: DurableMissionJournal,
    ) -> None:
        recovery = journal.recover()
        if recovery.recovery_required:
            raise MissionJournalRecoveryRequired(
                recovery.recovery_reason or "journal recovery required"
            )
        if recovery.state is MissionTransactionState.DENIED:
            raise MissionJournalRecoveryRequired("a denied mission transaction is terminal")
        # A newly constructed core transaction has already validated the same
        # operator grant and all hashes.  Restoring only the checkpointed state
        # and requested item set avoids a second authorization or a synthetic
        # network ACK/readback.  All subsequent calls still execute the exact
        # Px4MissionTransaction methods.
        if transaction.state is not MissionTransactionState.AUTHORIZED:
            raise MissionJournalError(
                "resume requires a freshly constructed authorized transaction"
            )
        transaction._state = recovery.state  # type: ignore[attr-defined]
        transaction._requested_sequences = set(  # type: ignore[attr-defined]
            recovery.requested_sequences
        )

    @classmethod
    def resume(
        cls,
        transaction: Px4MissionTransaction,
        journal: DurableMissionJournal,
    ) -> PersistentPx4MissionTransaction:
        return cls(transaction, journal, resume=True)

    @property
    def transaction(self) -> Px4MissionTransaction:
        return self._transaction

    @property
    def journal(self) -> DurableMissionJournal:
        return self._journal

    @property
    def state(self) -> MissionTransactionState:
        return self._transaction.state

    @property
    def denial_reason(self) -> str | None:
        return self._transaction.denial_reason

    @property
    def recovery(self) -> MissionJournalRecovery:
        return self._journal.recover()

    def _require_sync(self) -> None:
        if self._transaction.state is not self._journal.state:
            raise MissionJournalRecoveryRequired(
                "in-memory transaction and mission journal states diverged"
            )

    def _record_denial(self, error: Px4MissionError) -> None:
        if (
            self._transaction.state is MissionTransactionState.DENIED
            and self._journal.state is not MissionTransactionState.DENIED
        ):
            try:
                self._journal.record_denied(str(error))
            except MissionJournalError as journal_error:
                raise MissionJournalRecoveryRequired(
                    f"transaction denied but denial journal append failed: {journal_error}"
                ) from journal_error

    def begin_upload(self, *, now_utc: datetime | None = None) -> MissionUploadPlan:
        self._require_sync()
        try:
            plan = self._transaction.begin_upload(now_utc=now_utc)
        except Px4MissionError as exc:
            self._record_denial(exc)
            raise
        self._journal.record_transition(
            MissionTransactionState.UPLOADING,
            payload={"protocol_hash": plan.protocol_hash, "item_count": plan.item_count},
            now_utc=now_utc,
        )
        self._require_sync()
        return plan

    def mission_item_for_request(
        self,
        sequence: int,
        *,
        request_message_type: str = "MISSION_REQUEST_INT",
        now_utc: datetime | None = None,
    ) -> Px4MissionItemInt:
        self._require_sync()
        try:
            item = self._transaction.mission_item_for_request(
                sequence,
                request_message_type=request_message_type,
            )
        except Px4MissionError as exc:
            self._record_denial(exc)
            raise
        self._journal.record_item_request(
            sequence,
            request_message_type=request_message_type,
            now_utc=now_utc,
        )
        self._require_sync()
        return item

    def accept_upload_ack(
        self,
        *,
        ack_type: int = 0,
        mission_type: int = MAV_MISSION_TYPE_MISSION,
        source_system: int = 1,
        source_component: int = 1,
        now_utc: datetime | None = None,
    ) -> None:
        self._require_sync()
        try:
            self._transaction.accept_upload_ack(
                ack_type=ack_type,
                mission_type=mission_type,
                source_system=source_system,
                source_component=source_component,
                now_utc=now_utc,
            )
        except Px4MissionError as exc:
            self._record_denial(exc)
            raise
        self._journal.record_transition(
            MissionTransactionState.UPLOADED,
            payload={
                "ack_type": ack_type,
                "mission_type": mission_type,
                "source_system": source_system,
                "source_component": source_component,
            },
            now_utc=now_utc,
        )
        self._require_sync()

    def verify_readback(
        self,
        items: Any,
        *,
        source_system: int = 1,
        source_component: int = 1,
        now_utc: datetime | None = None,
    ) -> str:
        self._require_sync()
        items_tuple = tuple(items)
        try:
            actual_hash = self._transaction.verify_readback(
                items_tuple,
                source_system=source_system,
                source_component=source_component,
                now_utc=now_utc,
            )
        except Px4MissionError as exc:
            self._record_denial(exc)
            raise
        self._journal.record_transition(
            MissionTransactionState.READBACK_VERIFIED,
            payload={"protocol_hash": actual_hash, "item_count": len(items_tuple)},
            now_utc=now_utc,
        )
        self._require_sync()
        return actual_hash

    def commit(self, *, now_utc: datetime | None = None) -> str:
        self._require_sync()
        try:
            protocol_hash = self._transaction.commit(now_utc=now_utc)
        except Px4MissionError as exc:
            self._record_denial(exc)
            raise
        self._journal.record_transition(
            MissionTransactionState.COMMITTED,
            payload={"protocol_hash": protocol_hash},
            now_utc=now_utc,
        )
        self._require_sync()
        return protocol_hash

    def permit_start(self, *, now_utc: datetime | None = None) -> MissionStartPermit:
        self._require_sync()
        try:
            permit = self._transaction.permit_start(now_utc=now_utc)
        except Px4MissionError as exc:
            self._record_denial(exc)
            raise
        self._journal.record_transition(
            MissionTransactionState.START_PERMITTED,
            payload={
                "transaction_id": permit.transaction_id,
                "executable_mission_hash": permit.executable_mission_hash,
                "protocol_hash": permit.protocol_hash,
            },
            now_utc=now_utc,
        )
        self._require_sync()
        return permit

    def start_permit_from_checkpoint(self) -> MissionStartPermit:
        """Return the recorded permit without emitting a new mode command.

        The caller still has to make an explicit, fresh vehicle-side decision;
        this helper only reconstructs the immutable evidence object.
        """

        self._require_sync()
        if self.state is not MissionTransactionState.START_PERMITTED:
            raise MissionJournalError("a start permit has not been journaled")
        payload = self._journal.records[-1].payload
        return MissionStartPermit(
            transaction_id=require_text(payload.get("transaction_id"), "transaction_id"),
            executable_mission_hash=require_sha256(
                payload.get("executable_mission_hash"), "executable_mission_hash"
            ),
            protocol_hash=require_sha256(payload.get("protocol_hash"), "protocol_hash"),
        )

    def close(self) -> None:
        self._journal.close()


# Descriptive aliases for callers that prefer transaction-journal terminology.
MissionTransactionJournal = DurableMissionJournal
PersistentMissionTransaction = PersistentPx4MissionTransaction


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a hash-bound PX4 mission transaction journal. "
            "A truncated tail returns exit code 3 until explicitly repaired."
        )
    )
    parser.add_argument("journal", type=Path)
    parser.add_argument("--repair-tail", action="store_true")
    parser.add_argument(
        "--operator-note",
        default="operator reviewed the uncommitted mission journal tail",
    )
    parser.add_argument("--quarantine-path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        journal = DurableMissionJournal.open(args.journal)
        if args.repair_tail:
            journal.repair_truncated_tail(
                operator_note=args.operator_note,
                quarantine_path=args.quarantine_path,
            )
        report = journal.recover().to_mapping()
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        if report["recovery_required"]:
            return 3
        return 0
    except (MissionJournalError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


__all__ = [
    "DurableMissionJournal",
    "MISSION_JOURNAL_FORMAT",
    "MISSION_JOURNAL_SCHEMA_VERSION",
    "MissionJournalCorruptionError",
    "MissionJournalError",
    "MissionJournalIdentity",
    "MissionJournalRecord",
    "MissionJournalRecovery",
    "MissionJournalRecoveryRequired",
    "MissionResumePlan",
    "MissionTransactionJournal",
    "PersistentMissionTransaction",
    "PersistentPx4MissionTransaction",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI wrapper
    raise SystemExit(main())
