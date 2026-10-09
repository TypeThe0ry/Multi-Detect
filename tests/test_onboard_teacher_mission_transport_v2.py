"""CPU protocol queues/journals; fixture issuer is isolated, never real authority."""

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_onboard_teacher_mission_transport_v1 as fixture
from test_onboard_teacher_sortie_budget_v1 import budget as budget_fixture

from multidetect.mission_journal import MissionTransactionState
from multidetect.px4_external_mission import readback_mission_int, upload_mission_int
from scripts import onboard_teacher_mission_transport_v2 as api
from scripts.onboard_teacher_telemetry_capture_v4 import TeacherTelemetryCaptureV4

budget = budget_fixture
transaction = fixture.transaction


@pytest.fixture
def harness(tmp_path, transaction, budget):
    clock, connection, runtime = fixture.Clock(), fixture.Connection(), fixture.attestation()
    budget = replace(budget, started_at_s=clock(), clock_id=fixture.CLOCK_ID)
    cap = TeacherTelemetryCaptureV4(
        tmp_path / "v4",
        budget=budget,
        clock=clock,
        clock_id=budget.clock_id,
        transaction_id=transaction.persistent.journal.identity.transaction_id,
        local_origin_id="CPU-only-not-flight",
    )
    holder = {}
    receiver = api.frozen._ConcurrentReceiverV6(
        lambda timeout: connection.recv_match(blocking=True, timeout=timeout),
        clock,
        request_sender=lambda commands: holder["transport"]._dispatch(commands),
        queue_capacity=budget.queue_capacity,
    ).start()
    intent = api.TeacherMissionTransferIntentV2(
        transaction_id=cap.transaction_id,
        container_id=runtime.container_id,
        image=runtime.image,
        runtime_attestation_sha256=runtime.canonical_hash,
        executable_mission_sha256=transaction.compiled.executable_mission.canonical_hash,
        wire_sha256=transaction.compiled.wire_hash,
        clock_id=budget.clock_id,
        approved_at_s=budget.started_at_s,
        expires_at_s=budget.deadline_s,
        explicitly_authorized=True,
        budget_sha256=budget.canonical_hash,
    )
    inputs = dict(
        receiver=receiver,
        connection=connection,
        compiled=transaction.compiled,
        transaction=transaction.persistent,
        capture=cap,
        intent=intent,
        runtime_attestation=runtime,
        clock=clock,
        clock_id=budget.clock_id,
        budget=budget,
        utc_clock=lambda: fixture.NOW,
    )

    def bind(**changes):
        value = api.TeacherMissionTransportV2(**{**inputs, **changes})
        holder["transport"] = value
        return value

    h = SimpleNamespace(
        **transaction.__dict__,
        clock=clock,
        connection=connection,
        receiver=receiver,
        capture=cap,
        runtime=runtime,
        intent=intent,
        budget=budget,
        inputs=inputs,
        bind=bind,
        messages=[],
    )

    def enqueue(kind, **kwargs):
        message = fixture.RawMessage(kind, **kwargs)

        def before_decode():
            assert cap._commits["raw"]["verified"] > 0
            assert threading.get_ident() == receiver._owner

        message.before_decode = before_decode
        h.messages.append(message)
        connection.inbox.put(message)
        return message

    h.enqueue = enqueue
    try:
        yield h
    finally:
        if holder.get("transport"):
            holder["transport"].close()
        receiver.stop()
        if not cap._closed:
            cap.abort("CPU-finished")


def test_new_upload_readback_real_single_RX_interleaving(harness):
    h = harness
    transport, plan = h.bind(), fixture.begin(h)
    fixture.automatic_px4(h, plan, interleave=True)
    upload = upload_mission_int(
        transport.connection,
        h.persistent,
        plan,
        receive=transport.receive,
        clock=h.clock,
        now_utc=fixture.NOW,
        gcs_identity=(255, 190),
        message_timeout_s=0.1,
    )
    readback = readback_mission_int(
        transport.connection,
        h.persistent,
        plan,
        receive=transport.receive,
        clock=h.clock,
        now_utc=fixture.NOW,
        gcs_identity=(255, 190),
        message_timeout_s=0.1,
    )
    assert upload["request_sequences"] == list(range(plan.item_count))
    assert (
        readback["hash_match"] and h.persistent.state is MissionTransactionState.READBACK_VERIFIED
    )
    assert transport.to_mapping()["counts"]["foreign_target_rows"] == 2
    assert transport.to_mapping()["counts"]["foreign_source_rows"] == 2
    assert len(set(h.connection.read_threads)) == 1
    assert {t for _, _, t in h.connection.mav.calls} == set(h.connection.read_threads)
    assert all(set(m.decode_threads) == {threading.get_ident()} for m in h.messages)
    assert not any(transport.to_mapping()["claims"].values())


@pytest.mark.parametrize(
    "field,value",
    [
        ("budget_sha256", "0" * 64),
        ("approved_at_s", 1),
        ("wire_sha256", "0" * 64),
        ("container_id", "0" * 64),
    ],
)
def test_binding_mismatch_refuses_before_anysend(harness, field, value):
    h = harness
    with pytest.raises(ValueError):
        h.bind(intent=replace(h.intent, **{field: value}))
    assert not h.connection.mav.calls


def test_legacy_contract_not_admitted_even_with_samewire(harness):
    h = harness
    old = fixture.intent_for(h.persistent, h.compiled, h.runtime, h.clock)
    with pytest.raises(TypeError, match="NEW lifecycle"):
        h.bind(intent=old)


def test_fixed_transfer_deadline_not_lifecycle_extension(harness):
    h = harness
    transport, plan = h.bind(), fixture.begin(h)
    h.clock.offset += 51
    with pytest.raises(ValueError, match="phase deadline"):
        transport.connection.mav.mission_count_send(*plan.count_send_args())
    assert not h.connection.mav.calls and transport.to_mapping()["failure"]


def test_budget_constructor_reads_dont_renew_60second_softlimit(harness):
    h = harness
    transport = h.bind()
    h.clock.offset += 70
    assert transport._check_live() < h.budget.deadline_s
    assert h.intent.expires_at_s - h.intent.approved_at_s == 235
    assert transport.to_mapping()["old_intent_renewed"] is False


def test_actualUTCgrant_expiry_stillrefuses(harness):
    from datetime import timedelta

    h = harness
    with pytest.raises(Exception, match="expired"):
        h.bind(utc_clock=lambda: fixture.NOW + timedelta(days=1))
    assert not h.connection.mav.calls


def test_ledger_only_exact_real_RX_may_write_and_mainmay_audit(harness, tmp_path):
    import hashlib

    from scripts.onboard_teacher_sortie_artifact_ledger_v1 import (
        LedgerFileBinding,
        TeacherSortieArtifactLedger,
    )

    h = harness
    transport, plan = h.bind(), fixture.begin(h)
    ledger = TeacherSortieArtifactLedger(
        budget=h.budget,
        clock=h.clock,
        camera_directory=tmp_path / "camera-ledger",
        evidence_directory=tmp_path / "evidence-ledger",
    )
    try:
        ledger.bind_execution_sender(h.receiver)
        with pytest.raises(ValueError, match="One actual"):
            ledger.bind_execution_sender(h.receiver)
        writes = []

        def send_scope(method, args):
            assert threading.get_ident() == h.receiver._thread.ident
            path = ledger.evidence_directory / "actual-RX-api-intention.json"
            reservation = ledger.reserve_write("evidence", path, maximum_bytes=100)
            path.write_bytes(b"CPU entry intention, not delivery proof")
            ledger.commit_write(
                reservation,
                LedgerFileBinding(
                    path.as_posix(),
                    path.stat().st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                ),
            )
            with pytest.raises(RuntimeError, match="Only main"):
                ledger.capacity()
            writes.append(path)

        h.connection.mav.on_call = send_scope
        transport.connection.mav.mission_count_send(*plan.count_send_args())
        assert len(writes) == 1 and ledger.capacity()["remaining_evidence_bytes"] > 0
        assert ledger._pending == {}
    finally:
        ledger.close()
