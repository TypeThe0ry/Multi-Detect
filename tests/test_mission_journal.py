from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionJournalCorruptionError,
    MissionJournalError,
    MissionJournalIdentity,
    MissionJournalRecord,
    MissionJournalRecoveryRequired,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from multidetect.px4_mission import (
    MissionTransactionBindings,
    Px4MissionError,
    Px4MissionTransaction,
)

NOW = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


def _hash(letter: str) -> str:
    return letter * 64


def _identity(*, transaction_id: str = "tx-journal-001") -> MissionJournalIdentity:
    bindings = MissionTransactionBindings(
        prompt_hash=_hash("a"),
        task_spec_hash=_hash("b"),
        plan_hash=_hash("c"),
        context_snapshot_hash=_hash("d"),
        airframe_profile_hash=_hash("e"),
        geofence_hash=_hash("f"),
        vehicle_identity_hash=_hash("1"),
        runtime_descriptor_hash=_hash("2"),
        runtime_provenance_hash=_hash("3"),
    )
    return MissionJournalIdentity(
        transaction_id=transaction_id,
        executable_mission_hash=_hash("4"),
        protocol_hash=_hash("5"),
        validation_report_hash=_hash("6"),
        bindings=bindings,
        mission_item_count=3,
        airframe_adapter_id="px4-vtol-mission-int-v1",
    )


def _advance_to_uploading(journal: DurableMissionJournal) -> None:
    journal.record_transition(
        MissionTransactionState.UPLOADING,
        payload={"protocol_hash": _hash("5"), "item_count": 3},
        now_utc=NOW,
    )


def test_journal_round_trip_and_explicit_resume_plan(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), journal_id="journal-001", now_utc=NOW)
    _advance_to_uploading(journal)
    journal.record_item_request(2, now_utc=NOW)
    journal.record_item_request(0, now_utc=NOW)
    journal.close()

    reopened = DurableMissionJournal.open(path)
    recovery = reopened.recover()
    assert recovery.state is MissionTransactionState.UPLOADING
    assert recovery.requested_sequences == (0, 2)
    assert recovery.resume_plan.missing_sequences == (1,)
    assert recovery.resume_plan.next_action == "request_missing_items"
    assert recovery.resume_plan.requires_readback is True
    assert recovery.resume_plan.auto_start_allowed is False
    assert recovery.recovery_required is False
    assert len(recovery.records) == 4
    reopened.close()


def test_journal_rejects_invalid_transition_and_binding(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), now_utc=NOW)
    with pytest.raises(MissionJournalError, match="does not permit"):
        journal.record_transition(MissionTransactionState.COMMITTED)
    journal.close()

    with pytest.raises(MissionJournalError, match="identity"):
        DurableMissionJournal(path, identity=_identity(transaction_id="other"), create=False)


def test_hash_chain_tampering_is_not_recoverable(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), now_utc=NOW)
    _advance_to_uploading(journal)
    journal.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    document = json.loads(lines[1])
    document["payload"]["item_count"] = 99
    lines[1] = json.dumps(document, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(MissionJournalCorruptionError, match="hash mismatch"):
        DurableMissionJournal.open(path)


def test_partial_tail_requires_review_and_never_auto_resumes(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), now_utc=NOW)
    _advance_to_uploading(journal)
    journal.close()
    with path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"record_type":"event"')

    reopened = DurableMissionJournal.open(path)
    recovery = reopened.recover()
    assert recovery.recovery_required is True
    assert recovery.resume_plan.next_action == "repair_journal_tail"
    with pytest.raises(MissionJournalRecoveryRequired):
        reopened.record_item_request(1)
    reopened.close()


def test_external_append_after_open_is_not_silently_extended(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), now_utc=NOW)
    with path.open("ab") as handle:
        handle.write(b"external-tail")
    with pytest.raises(MissionJournalRecoveryRequired, match="changed after opening"):
        journal.record_transition(MissionTransactionState.UPLOADING)
    journal.close()


def test_journal_timestamps_are_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), now_utc=NOW)
    with pytest.raises(MissionJournalError, match="timestamp cannot move backwards"):
        journal.record_transition(
            MissionTransactionState.UPLOADING,
            payload={"protocol_hash": _hash("5"), "item_count": 3},
            now_utc=NOW.replace(hour=7),
        )
    journal.close()


def test_repair_quarantines_tail_and_adds_auditable_ack(tmp_path: Path) -> None:
    path = tmp_path / "mission.jsonl"
    journal = DurableMissionJournal.create(path, _identity(), now_utc=NOW)
    _advance_to_uploading(journal)
    journal.close()
    tail = b"partial-tail"
    with path.open("ab") as handle:
        handle.write(tail)

    reopened = DurableMissionJournal.open(path)
    quarantine = tmp_path / "tail.bin"
    repaired = reopened.repair_truncated_tail(
        operator_note="power loss reviewed",
        quarantine_path=quarantine,
        now_utc=NOW,
    )
    assert repaired.event_type == "journal_recovery_acknowledged"
    assert repaired.to_state is MissionTransactionState.UPLOADING
    assert quarantine.read_bytes() == tail
    assert reopened.recovery_required is False
    assert reopened.records[-1].payload["quarantined_sha256"] == hashlib.sha256(tail).hexdigest()
    reopened.close()

    checked = DurableMissionJournal.open(path)
    assert checked.recovery_required is False
    assert checked.records[-1].event_type == "journal_recovery_acknowledged"
    checked.close()


def test_record_hash_is_deterministic_and_round_trips() -> None:
    record = MissionJournalRecord.build(
        record_type="header",
        journal_id="journal-001",
        sequence=0,
        transaction_id="tx",
        identity_hash=_hash("a"),
        event_type="journal_opened",
        from_state=None,
        to_state=MissionTransactionState.AUTHORIZED,
        occurred_at="2026-09-01T08:00:00Z",
        payload={"format": "multidetect.px4-mission-journal.v1", "identity": {}},
        previous_record_hash=None,
    )
    assert MissionJournalRecord.from_mapping(record.to_mapping()) == record
    assert record.record_hash == canonical_sha256(record._hash_input())


def test_persistent_adapter_records_the_existing_px4_transaction(tmp_path: Path) -> None:
    # Reuse the canonical transaction fixture from the PX4 mission tests so
    # this test exercises the real compiler, grant, and readback gate.
    from test_px4_mission import _compiled, _transaction_documents

    context, plan, compiled = _compiled()
    report, bindings, grant = _transaction_documents(compiled, context, plan)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    transaction = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now,
    )
    journal = DurableMissionJournal.create(
        tmp_path / "transaction.jsonl",
        identity=transaction,
        now_utc=now,
    )
    persistent = PersistentPx4MissionTransaction(transaction, journal)
    upload = persistent.begin_upload(now_utc=now)
    for sequence in range(upload.item_count):
        assert (
            persistent.mission_item_for_request(sequence, now_utc=now)
            == upload.items[sequence]
        )
    persistent.accept_upload_ack(now_utc=now)
    assert persistent.verify_readback(compiled.wire_items, now_utc=now) == compiled.wire_hash
    assert persistent.commit(now_utc=now) == compiled.wire_hash
    permit = persistent.permit_start(now_utc=now)
    assert permit.protocol_hash == compiled.wire_hash
    assert persistent.state is MissionTransactionState.START_PERMITTED
    assert journal.resume_plan().auto_start_allowed is False
    persistent.close()


def test_persistent_adapter_explicit_resume_restores_checkpoint_without_new_events(
    tmp_path: Path,
) -> None:
    from test_px4_mission import _compiled, _transaction_documents

    context, plan, compiled = _compiled()
    report, bindings, grant = _transaction_documents(compiled, context, plan)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)

    def make_transaction() -> Px4MissionTransaction:
        return Px4MissionTransaction(
            transaction_id=grant.transaction_id,
            compiled=compiled,
            validation_report=report,
            operator_grant=grant,
            bindings=bindings,
            now_utc=now,
        )

    path = tmp_path / "resume.jsonl"
    first = make_transaction()
    journal = DurableMissionJournal.create(path, identity=first, now_utc=now)
    running = PersistentPx4MissionTransaction(first, journal)
    upload = running.begin_upload(now_utc=now)
    running.mission_item_for_request(0)
    running.close()

    reopened = DurableMissionJournal.open(path)
    resumed = PersistentPx4MissionTransaction.resume(make_transaction(), reopened)
    assert resumed.state is MissionTransactionState.UPLOADING
    assert resumed.recovery.resume_plan.missing_sequences == tuple(range(1, upload.item_count))
    # A resume only restores the checkpoint.  It does not synthesize an ACK or
    # permit AUTO.MISSION; the caller must continue the normal core methods.
    with pytest.raises(Px4MissionError):
        resumed.permit_start(now_utc=now)
    resumed.close()
