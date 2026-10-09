"""CPU fixtures only: explicit sender identity, not runtime or flight authorization."""

from collections import deque
from datetime import datetime, timezone

import pytest

from multidetect.mission_journal import DurableMissionJournal, PersistentPx4MissionTransaction
from multidetect.px4_external_mission import (
    ExternalPx4MissionError,
    readback_mission_int,
    upload_mission_int,
)
from multidetect.px4_mission import (
    MissionTransactionState,
    Px4MissionTransaction,
    compile_executable_mission,
)
from multidetect.px4_mission_acceptance import _build_transaction_documents
from scripts.run_px4_airframe_transaction_acceptance import _compile_fixture

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)


class Message:
    def __init__(self, kind, *, target=(255, 190), **fields):
        self.kind = kind
        self.target_system, self.target_component = target
        self.mission_type = 0
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self):
        return self.kind

    def get_srcSystem(self):
        return 1

    def get_srcComponent(self):
        return 1


class MavRecorder:
    def __init__(self, identity):
        self.calls = []
        if identity is not None:
            self.srcSystem, self.srcComponent = identity

    def mission_count_send(self, *args):
        self.calls.append(("count", args))

    def mission_item_int_send(self, *args):
        self.calls.append(("item", args))

    def mission_request_list_send(self, *args):
        self.calls.append(("list", args))

    def mission_request_int_send(self, *args):
        self.calls.append(("request", args))


class Connection:
    def __init__(self, messages=(), *, identity=(255, 190), source=None):
        self.mav = MavRecorder(identity)
        self.messages = deque(messages)
        self.received = 0
        if source is not None:
            self.source_system, self.source_component = source

    def recv_match(self, **kwargs):
        self.received += 1
        return self.messages.popleft() if self.messages else None


@pytest.fixture
def transaction_and_plan(tmp_path):
    context, plan, geometry, policy = _compile_fixture("multirotor")
    compiled = compile_executable_mission(
        vehicle_class="multirotor",
        platform_plan=plan,
        context_snapshot=context,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash="b" * 64,
    )
    # Fixture grants never enter the actual teacher execution path.
    report, bindings, grant = _build_transaction_documents(
        vehicle_class="multirotor",
        compiled=compiled,
        context_snapshot=context,
        platform_plan=plan,
        policy=policy,
        transaction_id="gcs-identity-fixture",
        now_utc=NOW,
    )
    transaction = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=NOW,
    )
    journal = DurableMissionJournal.create(
        tmp_path / "cpu-fixture.transaction.jsonl", identity=transaction, now_utc=NOW
    )
    persistent = PersistentPx4MissionTransaction(transaction, journal)
    try:
        yield persistent, persistent.begin_upload(now_utc=NOW)
    finally:
        persistent.close()


def upload_messages(plan, target):
    return [
        *(Message("MISSION_REQUEST_INT", target=target, seq=i) for i in range(plan.item_count)),
        Message("MISSION_ACK", target=target, type=0),
    ]


def readback_messages(plan, target):
    messages = [Message("MISSION_COUNT", target=target, count=plan.item_count)]
    for item in plan.items:
        fields = {
            name: getattr(item, name)
            for name in (
                "frame",
                "command",
                "current",
                "autocontinue",
                "param1",
                "param2",
                "param3",
                "param4",
                "x",
                "y",
                "z",
            )
        }
        messages.append(Message("MISSION_ITEM_INT", target=target, seq=item.sequence, **fields))
    return messages


@pytest.mark.parametrize("identity", [(250, 191), (255, 190)])
def test_bound_gcs_upload_readback_preserves_protocol_hash(transaction_and_plan, identity):
    transaction, plan = transaction_and_plan
    connection = Connection(upload_messages(plan, identity), identity=identity, source=identity)
    upload = upload_mission_int(connection, transaction, plan, now_utc=NOW, gcs_identity=identity)
    assert upload["request_sequences"] == list(range(plan.item_count))
    connection.messages.extend(readback_messages(plan, identity))
    readback = readback_mission_int(
        connection, transaction, plan, now_utc=NOW, gcs_identity=identity
    )
    assert readback["readback_protocol_sha256"] == plan.protocol_hash
    assert transaction.state is MissionTransactionState.READBACK_VERIFIED
    assert all(args[:2] == (1, 1) for _, args in connection.mav.calls)


def test_legacy_default_with_no_source_metadata_is_unchanged(transaction_and_plan):
    transaction, plan = transaction_and_plan
    connection = Connection(upload_messages(plan, (250, 191)), identity=None)
    upload_mission_int(connection, transaction, plan, now_utc=NOW)
    connection.messages.extend(readback_messages(plan, (250, 191)))
    assert readback_mission_int(connection, transaction, plan, now_utc=NOW)["hash_match"]


@pytest.mark.parametrize("method", [upload_mission_int, readback_mission_int])
def test_dynamic_send_recorder_cannot_supply_explicit_source_metadata(transaction_and_plan, method):
    class DynamicRecorder(MavRecorder):
        def __getattr__(self, name):
            return lambda *args: self.calls.append((name, args))

    transaction, plan = transaction_and_plan
    connection = Connection(identity=None)
    connection.mav = DynamicRecorder(None)
    with pytest.raises(ExternalPx4MissionError, match="requires actual MAVLink source"):
        method(connection, transaction, plan, now_utc=NOW, gcs_identity=(255, 190))
    assert connection.mav.calls == []
    assert connection.received == 0


@pytest.mark.parametrize("target", [("255", 190), (255.9, 190), (255, 190.9), (True, 190)])
def test_invalid_packet_targets_are_not_coerced_to_current_gcs(transaction_and_plan, target):
    transaction, plan = transaction_and_plan
    connection = Connection([Message("MISSION_REQUEST_INT", target=target, seq=0)])
    with pytest.raises(ExternalPx4MissionError, match="invalid target identity"):
        upload_mission_int(connection, transaction, plan, now_utc=NOW, gcs_identity=(255, 190))
    assert transaction.state is MissionTransactionState.UPLOADING
    assert [name for name, _ in connection.mav.calls] == ["count"]


def test_foreign_component_and_broadcast_cannot_advance_upload_or_readback(transaction_and_plan):
    transaction, plan = transaction_and_plan
    targets = [(255, 191), (250, 191), (0, 0), (255, 0), (0, 190)]
    ignored = [
        message
        for target in targets
        for message in (
            Message("MISSION_REQUEST_INT", target=target, seq=99),
            Message("MISSION_ACK", target=target, type=1),
        )
    ]
    connection = Connection([*ignored, *upload_messages(plan, (255, 190))])
    upload = upload_mission_int(connection, transaction, plan, now_utc=NOW, gcs_identity=(255, 190))
    assert upload["request_sequences"] == list(range(plan.item_count))
    assert upload["premature_ack_count"] == 0
    assert len(connection.mav.calls) == 1 + plan.item_count
    valid = readback_messages(plan, (255, 190))
    connection.messages.extend(
        [*(Message("MISSION_COUNT", target=t, count=-1) for t in targets), valid[0]]
    )
    for message in valid[1:]:
        connection.messages.extend(
            [*(Message("MISSION_ITEM_INT", target=t, seq=99) for t in targets), message]
        )
    readback = readback_mission_int(
        connection, transaction, plan, now_utc=NOW, gcs_identity=(255, 190)
    )
    assert readback["retry_count"] == len(targets) * plan.item_count
    assert readback["hash_match"]


@pytest.mark.parametrize("target", [(255, 191), (0, 0), (255, 0), (0, 190)])
def test_only_foreign_or_broadcast_upload_packets_never_complete(transaction_and_plan, target):
    transaction, plan = transaction_and_plan
    connection = Connection(upload_messages(plan, target))
    with pytest.raises(ExternalPx4MissionError, match="timed out"):
        upload_mission_int(
            connection,
            transaction,
            plan,
            now_utc=NOW,
            gcs_identity=(255, 190),
            maximum_idle_retries=0,
        )
    assert transaction.state is MissionTransactionState.UPLOADING
    assert [name for name, _ in connection.mav.calls] == ["count"]


@pytest.mark.parametrize("method", [upload_mission_int, readback_mission_int])
@pytest.mark.parametrize(
    "identity",
    [
        None,
        (),
        (255,),
        (255, 190, 1),
        [255, 190],
        (True, 190),
        (255, False),
        (0, 190),
        (255, 0),
        (256, 190),
        (255, 256),
        (255.0, 190),
        ("255", 190),
    ],
)
def test_invalid_requested_identity_rejects_before_any_io(transaction_and_plan, method, identity):
    transaction, plan = transaction_and_plan
    connection = Connection()
    with pytest.raises(ExternalPx4MissionError, match="nonzero byte integers"):
        method(connection, transaction, plan, now_utc=NOW, gcs_identity=identity)
    assert connection.mav.calls == []
    assert connection.received == 0


@pytest.mark.parametrize("method", [upload_mission_int, readback_mission_int])
@pytest.mark.parametrize(
    ("identity", "source", "expected"),
    [
        ((255, 191), None, (255, 190)),
        ((255.0, 190), None, (255, 190)),
        (("255", 190), None, (255, 190)),
        ((255, True), None, (255, 190)),
        (None, None, (255, 190)),
        ((255, 190), (255, 191), (255, 190)),
        ((255, 190), (255, 190.0), (255, 190)),
        ((255, 190), (255, "190"), (255, 190)),
        ((255, 190), None, (250, 191)),
        ((250, 191), (255, 190), (250, 191)),
    ],
)
def test_mismatched_or_untyped_sender_identity_rejects_before_any_io(
    transaction_and_plan, method, identity, source, expected
):
    transaction, plan = transaction_and_plan
    connection = Connection(identity=identity, source=source)
    with pytest.raises(ExternalPx4MissionError, match="GCS identity"):
        method(connection, transaction, plan, now_utc=NOW, gcs_identity=expected)
    assert connection.mav.calls == []
    assert connection.received == 0


@pytest.mark.parametrize("method", [upload_mission_int, readback_mission_int])
@pytest.mark.parametrize("owner", ["mav", "connection"])
def test_partial_source_identity_is_not_accepted(transaction_and_plan, method, owner):
    transaction, plan = transaction_and_plan
    connection = Connection(source=(255, 190))
    if owner == "mav":
        del connection.mav.srcComponent
    else:
        del connection.source_component
    with pytest.raises(ExternalPx4MissionError, match="GCS identity"):
        method(connection, transaction, plan, now_utc=NOW, gcs_identity=(255, 190))
    assert connection.mav.calls == []
    assert connection.received == 0
