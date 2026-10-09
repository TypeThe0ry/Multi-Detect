"""CPU queues and actual temporary journals only; no Docker/PX4/flight proof."""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_onboard_teacher_sim_operator as session_fixtures

from multidetect.clock import HighResolutionSystemClock
from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from multidetect.px4_external_mission import (
    ExternalPx4MissionError,
    ExternalRuntimeAttestation,
    readback_mission_int,
    upload_mission_int,
)
from multidetect.px4_mission import Px4MissionTransaction, compile_executable_mission
from multidetect.px4_mission_acceptance import _build_transaction_documents
from scripts import onboard_teacher_mission_transport_v1 as api
from scripts.onboard_teacher_gazebo_session_v4 import DiagnosticRequest, ReadOnlyDiagnosticSender
from scripts.onboard_teacher_telemetry_capture_v3 import TeacherTelemetryCaptureV3
from scripts.run_px4_airframe_transaction_acceptance import _compile_fixture

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
CLOCK_ID = "CPU-fixture-single-monotonic-domain"


class Clock:
    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset


class RawMessage:
    def __init__(self, kind, *, source=(1, 1), target=(255, 190), **fields):
        self.kind, self.source = kind, source
        self.fields = {"mavpackettype": kind, **fields}
        if target is not None:
            self.fields.update(target_system=target[0], target_component=target[1], mission_type=0)
        self.packet = b"cpu-standin-original-packet:" + json.dumps(self.fields).encode()
        self.decode_threads = []
        self.before_decode = None

    def get_msgbuf(self):
        return self.packet

    def _decode(self):
        self.decode_threads.append(threading.get_ident())
        if self.before_decode:
            self.before_decode()

    def get_type(self):
        self._decode()
        return self.kind

    def get_srcSystem(self):
        self._decode()
        return self.source[0]

    def get_srcComponent(self):
        self._decode()
        return self.source[1]

    def get_seq(self):
        self._decode()
        return 7

    def to_dict(self):
        self._decode()
        return copy.deepcopy(self.fields)


class Mav:
    def __init__(self):
        self.srcSystem, self.srcComponent = 255, 190
        self.calls = []
        self.on_call = None

    def _send(self, method, args):
        self.calls.append((method, args, threading.get_ident()))
        if self.on_call:
            self.on_call(method, args)

    def mission_count_send(self, *args):
        self._send("mission_count_send", args)

    def mission_item_int_send(self, *args):
        self._send("mission_item_int_send", args)

    def mission_request_list_send(self, *args):
        self._send("mission_request_list_send", args)

    def mission_request_int_send(self, *args):
        self._send("mission_request_int_send", args)

    def heartbeat_send(self, *args):
        self._send("heartbeat_send", args)

    def command_long_send(self, *args):
        self._send("command_long_send", args)

    def param_request_read_send(self, *args):
        self._send("param_request_read_send", args)


class Connection:
    def __init__(self):
        self.mav = Mav()
        self.source_system, self.source_component = 255, 190
        self.inbox, self.read_threads = queue.Queue(), []
        self.closed = False

    def recv_match(self, *, blocking, timeout):
        assert blocking is True and 0 < timeout <= 0.05
        self.read_threads.append(threading.get_ident())
        try:
            return self.inbox.get(timeout=min(timeout, 0.005))
        except queue.Empty:
            return None

    def close(self):
        self.closed = True


def attestation():
    return ExternalRuntimeAttestation.from_mapping(
        {
            "container_id": "1" * 64,
            "image": "cpu-fixture/px4@sha256:" + "2" * 64,
            "vehicle_class": "multirotor",
            "airframe_profile_id": "px4_multirotor_gazebo_v1",
            "scenario_id": "CPU-only-no-runtime",
            "scenario_manifest_sha256": "3" * 64,
            "ownership_checks": {"CPU_fixture_only": True},
            "telemetry_qualified": True,
            "truth_free": True,
            "hardware_control_enabled": False,
        }
    )


def intent_for(transaction, compiled, runtime, clock, clock_id=CLOCK_ID):
    at = clock()
    return api.TeacherMissionTransferIntent(
        transaction_id=transaction.journal.identity.transaction_id,
        container_id=runtime.container_id,
        image=runtime.image,
        runtime_attestation_sha256=runtime.canonical_hash,
        executable_mission_sha256=compiled.executable_mission.canonical_hash,
        wire_sha256=compiled.wire_hash,
        clock_id=clock_id,
        approved_at_s=at,
        expires_at_s=at + 10,
        explicitly_authorized=True,
    )


@pytest.fixture
def transaction(tmp_path, request):
    vehicle_class = getattr(request, "param", "multirotor")
    context, platform, geometry, policy = _compile_fixture(vehicle_class)
    compiled = compile_executable_mission(
        vehicle_class=vehicle_class,
        platform_plan=platform,
        context_snapshot=context,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash="b" * 64,
    )
    # Issuer fixture deliberately isolated here. Production module imports no
    # issuer/factory and cannot manufacture this fixture grant or report.
    report, bindings, grant = _build_transaction_documents(
        vehicle_class=vehicle_class,
        compiled=compiled,
        context_snapshot=context,
        platform_plan=platform,
        policy=policy,
        transaction_id="CPU-teacher-transfer-fixture",
        now_utc=NOW,
    )
    core = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compiled,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=NOW,
    )
    journal = DurableMissionJournal.create(tmp_path / "mission.jsonl", identity=core, now_utc=NOW)
    persistent = PersistentPx4MissionTransaction(core, journal)
    try:
        yield SimpleNamespace(persistent=persistent, compiled=compiled)
    finally:
        persistent.close()


@pytest.fixture
def harness(tmp_path, transaction):
    clock, connection, runtime = Clock(), Connection(), attestation()
    cap = TeacherTelemetryCaptureV3(
        tmp_path / "telemetry",
        clock=clock,
        clock_id=CLOCK_ID,
        transaction_id=transaction.persistent.journal.identity.transaction_id,
        local_origin_id="CPU-unqualified-origin-no-flight",
    )
    holder = {}
    diagnostics = ReadOnlyDiagnosticSender(connection, clock, CLOCK_ID, lambda: 10)

    def dispatch(commands):
        if commands and type(commands[0]) is api._SendCommand:
            return holder["transport"]._dispatch(commands)
        return diagnostics(commands)

    receiver = api._ConcurrentReceiverV6(
        lambda timeout: connection.recv_match(blocking=True, timeout=timeout),
        clock,
        request_sender=dispatch,
    ).start()
    intent = intent_for(transaction.persistent, transaction.compiled, runtime, clock)
    inputs = dict(
        receiver=receiver,
        connection=connection,
        compiled=transaction.compiled,
        transaction=transaction.persistent,
        capture=cap,
        intent=intent,
        runtime_attestation=runtime,
        clock=clock,
        clock_id=CLOCK_ID,
        remaining=lambda: 10,
        utc_clock=lambda: NOW,
    )

    def bind(**overrides):
        transport = api.TeacherMissionTransport(**{**inputs, **overrides})
        holder["transport"] = transport
        return transport

    h = SimpleNamespace(
        **transaction.__dict__,
        receiver=receiver,
        connection=connection,
        clock=clock,
        capture=cap,
        runtime=runtime,
        intent=intent,
        bind=bind,
        inputs=inputs,
        messages=[],
    )

    def enqueue(kind, **kwargs):
        message = RawMessage(kind, **kwargs)

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
        current = holder.get("transport")
        if current:
            current.close()
        receiver.stop()
        if not cap._closed:
            cap.abort("CPU-fixture-finished")


def begin(h):
    return h.persistent.begin_upload(now_utc=NOW)


def response_for_item(item):
    names = (
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
    return {"seq": item.sequence, **{name: getattr(item, name) for name in names}}


def automatic_px4(h, plan, *, interleave=False):
    def respond(method, args):
        if method == "mission_count_send":
            if interleave:
                h.enqueue("MISSION_REQUEST_INT", source=(2, 1), seq=0)
                h.enqueue("MISSION_REQUEST_INT", source=(1, 42), seq=0)
                h.enqueue("MISSION_REQUEST_INT", target=(0, 0), seq=0)
                h.enqueue("MISSION_REQUEST_INT", target=(255, 191), seq=0)
                h.enqueue("HEARTBEAT", target=None, autopilot=12, type=2)
            h.enqueue("MISSION_REQUEST_INT", seq=0)
        elif method == "mission_item_int_send":
            seq = args[2] + 1
            if seq < plan.item_count:
                h.enqueue("MISSION_REQUEST_INT", seq=seq)
            else:
                h.enqueue("MISSION_ACK", type=0)
        elif method == "mission_request_list_send":
            h.enqueue("MISSION_COUNT", count=plan.item_count)
        elif method == "mission_request_int_send":
            h.enqueue("MISSION_ITEM_INT", **response_for_item(plan.items[args[2]]))

    h.connection.mav.on_call = respond


def wait_until(predicate):
    deadline = time.monotonic() + 1
    while not predicate():
        assert time.monotonic() < deadline, "CPU thread failed to finish"
        time.sleep(0.001)


def test_actual_single_rx_upload_readback_with_interleaving_and_dual_commit(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.receiver.request_diagnostics([DiagnosticRequest("parameter-read", "BAT1_CAPACITY")])
    automatic_px4(h, plan, interleave=True)
    upload = upload_mission_int(
        transport.connection,
        h.persistent,
        plan,
        receive=transport.receive,
        clock=h.clock,
        now_utc=NOW,
        gcs_identity=(255, 190),
        message_timeout_s=0.1,
    )
    readback = readback_mission_int(
        transport.connection,
        h.persistent,
        plan,
        receive=transport.receive,
        clock=h.clock,
        now_utc=NOW,
        gcs_identity=(255, 190),
        message_timeout_s=0.1,
    )
    assert upload["request_sequences"] == list(range(plan.item_count))
    assert (
        readback["hash_match"] is True
        and readback["readback_protocol_sha256"] == plan.protocol_hash
    )
    assert h.persistent.state is MissionTransactionState.READBACK_VERIFIED
    report = transport.to_mapping()
    assert report["counts"]["foreign_source_rows"] == 2
    assert report["counts"]["foreign_target_rows"] == 2
    assert report["counts"]["returned_protocol_rows"] == 2 * plan.item_count + 2
    assert h.capture._commits["raw"]["verified"] == h.capture._commits["projection"]["verified"]
    assert all(
        m.decode_threads and set(m.decode_threads) == {threading.get_ident()} for m in h.messages
    )
    owners = {owner for _, _, owner in h.connection.mav.calls}
    assert owners == set(h.connection.read_threads) and threading.get_ident() not in owners
    assert len(owners) == 1
    assert not any(report["claims"].values())
    assert all(
        a["API_returned"] is True and a["delivery_verified"] is False
        for a in report["API_attempts"]
    )
    assert not hasattr(transport.connection, "recv_match")
    assert not hasattr(transport.connection.mav, "command_long_send")
    transport.close()
    h.receiver.stop()
    assert h.receiver.to_mapping()["thread_exited"] is True
    assert h.receiver.to_mapping()["failure"] is None


def test_response_receipt_contains_exact_original_packet_before_return(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.connection.mav.on_call = lambda *_: h.enqueue("MISSION_REQUEST_INT", seq=0)
    transport.connection.mav.mission_count_send(*plan.count_send_args())
    message = transport.receive(0.2)
    assert message.get_type() == "MISSION_REQUEST_INT"
    receipt = message.durable_receipt
    assert receipt["source_accepted"] is True
    assert (
        receipt["received_at_s"]
        <= receipt["raw_committed_at_s"]
        <= receipt["projection_committed_at_s"]
    )
    raw = receipt["raw_record"]
    with Path(raw["path"]).open("rb") as stream:
        stream.seek(raw["packet_offset"])
        packet = stream.read(raw["packet_bytes"])
    assert packet == h.messages[0].packet
    assert hashlib.sha256(packet).hexdigest() == raw["packet_sha256"]
    projection = receipt["projection_record"]
    with Path(projection["path"]).open("rb") as stream:
        stream.seek(projection["offset"])
        row = stream.read(projection["bytes"])
    assert hashlib.sha256(row).hexdigest() == projection["sha256"]
    assert json.loads(row)["row"]["transaction_id"] == h.intent.transaction_id
    assert h.persistent.recovery.requested_sequences == ()  # Consumer has not advanced transaction.


@pytest.mark.parametrize(
    "field,value",
    [
        ("explicitly_authorized", False),
        ("explicitly_authorized", 1),
        ("approved_at_s", True),
        ("expires_at_s", float("inf")),
        ("clock_id", ""),
        ("container_id", "unowned"),
    ],
)
def test_intent_invalid_primitives_reject(field, value, harness):
    with pytest.raises((TypeError, ValueError)):
        replace(harness.intent, **{field: value})
    assert harness.connection.mav.calls == []


def test_intent_strict_roundtrip_scope_and_lifetime(harness):
    intent = harness.intent
    assert api.TeacherMissionTransferIntent.from_mapping(intent.to_mapping()) == intent
    for change in (
        {"scope": "REAL"},
        {"contract": "other"},
        {"flight_start": True},
        {"expires_at_s": intent.approved_at_s + 61},
    ):
        with pytest.raises(ValueError):
            api.TeacherMissionTransferIntent.from_mapping({**intent.to_mapping(), **change})


@pytest.mark.parametrize(
    "field,value",
    [
        ("transaction_id", "different-tx"),
        ("wire_sha256", "f" * 64),
        ("executable_mission_sha256", "f" * 64),
        ("runtime_attestation_sha256", "f" * 64),
        ("container_id", "f" * 64),
        ("image", "other/px4@sha256:" + "4" * 64),
        ("clock_id", "different-domain"),
    ],
)
def test_bind_rejects_mismatched_transaction_wire_scope_or_identity(harness, field, value):
    with pytest.raises(ValueError, match="binding mismatch"):
        harness.bind(intent=replace(harness.intent, **{field: value}))
    assert harness.connection.mav.calls == []


@pytest.mark.parametrize("value", [None, {}, object()])
def test_missing_bound_persistent_transaction_rejects_before_send(harness, value):
    with pytest.raises(TypeError, match="persistent Mission transaction"):
        harness.bind(transaction=value)
    assert harness.connection.mav.calls == []


@pytest.mark.parametrize("transaction", ["fixed-wing", "vtol"], indirect=True)
def test_multirotor_owned_transport_rejects_other_airframe_wire(harness):
    with pytest.raises(ValueError, match="scope/source binding mismatch"):
        harness.bind()
    assert harness.connection.mav.calls == []


@pytest.mark.parametrize("identity", [(255, 191), (250, 191), (True, 190), (255, "190")])
def test_actual_mav_source_must_be_exact_strict_255_190(harness, identity):
    harness.connection.mav.srcSystem, harness.connection.mav.srcComponent = identity
    with pytest.raises(ExternalPx4MissionError, match="GCS identity"):
        harness.bind()
    assert harness.connection.mav.calls == []


def test_identity_mutation_after_binding_poisons_before_send(harness):
    transport, plan = harness.bind(), begin(harness)
    harness.connection.source_component = 191
    with pytest.raises(ExternalPx4MissionError, match="connection source"):
        transport.connection.mav.mission_count_send(*plan.count_send_args())
    assert harness.connection.mav.calls == []
    assert transport.to_mapping()["failure"] is not None


@pytest.mark.parametrize(
    "kind",
    ["item-no-checkpoint", "item-bad-wire", "count-bool", "count-other", "readback-uploading"],
)
def test_only_exact_compiled_arguments_with_durable_phase_and_checkpoint_send(harness, kind):
    h = harness
    transport, plan = h.bind(), begin(h)
    mav = transport.connection.mav
    if kind.startswith("item"):
        args = list(plan.items[0].to_send_args())
        if kind == "item-bad-wire":
            h.persistent.mission_item_for_request(0, now_utc=NOW)
            args[4] = 400  # ARM is not the compiled Mission command.
        method = mav.mission_item_int_send
    elif kind.startswith("count"):
        args = list(plan.count_send_args())
        args[0] = True if kind == "count-bool" else 2
        method = mav.mission_count_send
    else:
        args, method = (1, 1, 0), mav.mission_request_list_send
    with pytest.raises(ValueError):
        method(*args)
    assert h.connection.mav.calls == []
    assert h.receiver.to_mapping()["failure"] is not None


def test_synthetic_private_command_cannot_bypass_main_durable_validation(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    at = h.clock()
    command = api._SendCommand(
        "mission_count_send",
        plan.count_send_args(),
        h.persistent.state,
        at,
        at + 1,
        threading.Event(),
    )
    with pytest.raises(RuntimeError, match="durable validation"):
        h.receiver.request_mission(command, timeout_s=0.2)
    assert h.connection.mav.calls == [] and transport.to_mapping()["API_attempts"] == []


@pytest.mark.parametrize("source", [(2, 1), (1, 42)])
def test_foreign_source_durable_but_never_response_or_transaction_advance(harness, source):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.connection.mav.on_call = lambda *_: h.enqueue("MISSION_REQUEST_INT", source=source, seq=0)
    transport.connection.mav.mission_count_send(*plan.count_send_args())
    assert transport.receive(0.03) is None
    assert h.persistent.recovery.requested_sequences == ()
    assert transport.to_mapping()["counts"]["foreign_source_rows"] == 1
    assert (
        h.capture._commits["raw"]["verified"] == h.capture._commits["projection"]["verified"] == 1
    )


@pytest.mark.parametrize("target", [(0, 0), (255, 191), (250, 191)])
def test_broadcast_or_other_gcs_never_returns_response(harness, target):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.connection.mav.on_call = lambda *_: h.enqueue("MISSION_REQUEST_INT", target=target, seq=0)
    transport.connection.mav.mission_count_send(*plan.count_send_args())
    assert transport.receive(0.03) is None
    assert h.persistent.recovery.requested_sequences == ()
    assert transport.to_mapping()["counts"]["foreign_target_rows"] == 1


@pytest.mark.parametrize(
    "field,value",
    [("seq", True), ("seq", "0"), ("seq", 0.5), ("target_system", True), ("mission_type", True)],
)
def test_source_qualified_malformed_raw_protocol_fails_after_raw_commit(harness, field, value):
    h = harness
    transport, plan = h.bind(), begin(h)

    def response(*_):
        message = h.enqueue("MISSION_REQUEST_INT", seq=0)
        message.fields[field] = value
        message.packet = json.dumps(message.fields).encode()

    h.connection.mav.on_call = response
    transport.connection.mav.mission_count_send(*plan.count_send_args())
    with pytest.raises((ValueError, TypeError)):
        transport.receive(0.2)
    assert (
        h.capture._commits["raw"]["verified"] == h.capture._commits["projection"]["verified"] == 1
    )
    assert h.persistent.recovery.requested_sequences == ()
    assert transport.to_mapping()["counts"]["returned_protocol_rows"] == 0


def test_stale_pre_send_packet_durable_but_not_relabelled_as_current(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.enqueue("MISSION_REQUEST_INT", seq=0)
    wait_until(lambda: h.receiver._queue.qsize() == 1)
    transport.connection.mav.mission_count_send(*plan.count_send_args())
    assert transport.receive(0.03) is None
    assert transport.to_mapping()["counts"]["stale_protocol_rows"] == 1
    assert h.persistent.recovery.requested_sequences == ()


def test_stale_protocol_after_durable_commit_not_returned(harness):
    h = harness
    transport, plan = h.bind(response_maximum_age_s=0.02), begin(h)
    h.connection.mav.on_call = lambda *_: h.enqueue("MISSION_REQUEST_INT", seq=0)
    transport.connection.mav.mission_count_send(*plan.count_send_args())
    wait_until(lambda: h.receiver._queue.qsize() == 1)
    h.clock.offset += 0.1
    assert transport.receive(0.03) is None
    assert transport.to_mapping()["counts"]["stale_protocol_rows"] == 1


@pytest.mark.parametrize(
    "field,value",
    [("expected_component_id", 42), ("transaction_id", "other"), ("clock_id", "other-domain")],
)
def test_capture_binding_cannot_be_changed_to_accept_foreign_or_other_transaction(
    harness, field, value
):
    transport = harness.bind()
    setattr(harness.capture, field, value)
    with pytest.raises(ValueError, match="raw capture"):
        transport.receive(0)
    assert transport.to_mapping()["counts"]["returned_protocol_rows"] == 0


def test_sealed_raw_capture_cannot_continue_delivering_protocol(harness):
    transport = harness.bind()
    harness.capture.seal()
    with pytest.raises(RuntimeError, match="closed"):
        transport.receive(0)


def test_expired_intent_and_expired_grant_refuse_before_first_send(harness):
    h = harness
    h.clock.offset += 11
    with pytest.raises(ValueError, match="expired"):
        h.bind()
    h.clock.offset = 0
    with pytest.raises(ValueError, match="operator grant is expired"):
        h.bind(utc_clock=lambda: NOW + timedelta(days=1))
    assert h.connection.mav.calls == []


def test_cancel_poisoned_and_main_thread_ownership(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    errors = []

    def foreign_consumer():
        for action in (
            lambda: transport.receive(0),
            lambda: transport.cancel(),
            lambda: transport.connection.mav.mission_count_send(*plan.count_send_args()),
        ):
            try:
                action()
            except RuntimeError as error:
                errors.append(str(error))

    thread = threading.Thread(target=foreign_consumer)
    thread.start()
    thread.join(1)
    assert not thread.is_alive() and len(errors) == 3
    assert h.connection.mav.calls == []
    transport.cancel()
    with pytest.raises(RuntimeError, match="closed or poisoned"):
        transport.connection.mav.mission_count_send(*plan.count_send_args())
    h.receiver.stop()
    assert h.receiver.to_mapping()["thread_exited"] is True


def test_stopped_receiver_never_issues_new_send_or_returns_old_protocol(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.receiver.stop()
    with pytest.raises(RuntimeError, match="Live exclusive"):
        transport.connection.mav.mission_count_send(*plan.count_send_args())
    with pytest.raises(RuntimeError, match="closed or poisoned"):
        transport.receive(0)
    assert h.connection.mav.calls == []
    assert transport.to_mapping()["counts"]["returned_protocol_rows"] == 0


def test_api_timeout_is_unknown_and_no_retry_thread_exits_when_api_releases(harness):
    h = harness
    transport, plan = h.bind(send_timeout_s=0.02), begin(h)
    entered, release = threading.Event(), threading.Event()

    def blocked(*_):
        entered.set()
        assert release.wait(1)

    h.connection.mav.on_call = blocked
    try:
        with pytest.raises(RuntimeError, match="timeout; delivery unknown"):
            transport.connection.mav.mission_count_send(*plan.count_send_args())
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="closed or poisoned"):
            transport.connection.mav.mission_count_send(*plan.count_send_args())
        assert len(h.connection.mav.calls) == 1
    finally:
        release.set()
        h.receiver.stop()
    attempts = transport.to_mapping()["API_attempts"]
    assert len(attempts) == 1 and attempts[0]["delivery_verified"] is False
    assert attempts[0]["error"] is not None
    assert h.receiver.to_mapping()["thread_exited"] is True


@pytest.mark.parametrize("queue_exhausted", [False, True])
def test_queued_command_timeout_or_full_never_sends_after_cancel(harness, queue_exhausted):
    h = harness
    transport, plan = h.bind(send_timeout_s=0.02), begin(h)
    entered, release = threading.Event(), threading.Event()

    def blocked_read(*, blocking, timeout):
        assert blocking is True and timeout > 0
        entered.set()
        assert release.wait(1)
        return None

    h.connection.recv_match = blocked_read
    wait_until(entered.is_set)
    if queue_exhausted:
        h.receiver._commands.put_nowait((("CPU-occupied-no-send",), threading.Event(), []))
    try:
        with pytest.raises(RuntimeError, match="queue exhausted" if queue_exhausted else "timeout"):
            transport.connection.mav.mission_count_send(*plan.count_send_args())
    finally:
        release.set()
        h.receiver.stop()
    assert h.connection.mav.calls == []
    assert transport.to_mapping()["API_attempts"] == []
    assert h.receiver.to_mapping()["thread_exited"] is True


def test_telemetry_queue_overflow_is_not_a_silent_drop_or_send_success(harness):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.receiver._queue = queue.Queue(1)
    h.enqueue("HEARTBEAT", target=None, type=2, autopilot=12)
    wait_until(lambda: h.receiver.to_mapping()["queue_overflow"])
    with pytest.raises(RuntimeError, match="capacity exhausted"):
        transport.connection.mav.mission_count_send(*plan.count_send_args())
    assert h.connection.mav.calls == []
    assert h.persistent.recovery.requested_sequences == ()
    h.receiver.stop()
    assert h.receiver.to_mapping()["thread_exited"] is True


def test_raw_fsync_failure_never_delivers_protocol_or_advances_transaction(harness, monkeypatch):
    h = harness
    transport, plan = h.bind(), begin(h)
    h.connection.mav.on_call = lambda *_: h.enqueue("MISSION_REQUEST_INT", seq=0)
    transport.connection.mav.mission_count_send(*plan.count_send_args())

    def fail(*_):
        raise OSError("CPU injected fsync failure")

    monkeypatch.setattr("scripts.onboard_teacher_telemetry_capture_v3.os.fsync", fail)
    with pytest.raises(OSError, match="fsync failure"):
        transport.receive(0.2)
    assert h.capture._commits["raw"]["verified"] == 0
    assert h.persistent.recovery.requested_sequences == ()
    assert transport.to_mapping()["counts"]["returned_protocol_rows"] == 0


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return session_fixtures.template.__wrapped__(tmp_path_factory)


@pytest.fixture
def session_plan(tmp_path, template):
    return session_fixtures.plan.__wrapped__(tmp_path, template)


def test_v6_reuses_owned_v5_lifecycle_and_unique_rx_main_bind_cleanup(
    session_plan, transaction, tmp_path
):
    clock, connection = HighResolutionSystemClock(), Connection()
    docker = session_fixtures.helpers.NativeDocker(session_plan)
    backend = session_fixtures.helpers.Backend(session_plan, clock)
    calls = []

    def connect(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return connection

    session = api.OwnedTeacherGazeboSessionV6(
        session_plan,
        execute=True,
        clock=clock,
        runner=docker,
        connection_factory=connect,
        camera_factory=lambda *_: backend,
        port_probe=lambda _: None,
    )
    cap = TeacherTelemetryCaptureV3(
        tmp_path / "v6-telemetry",
        clock=clock.monotonic,
        clock_id=clock.session_id,
        transaction_id=transaction.persistent.journal.identity.transaction_id,
        local_origin_id="CPU-unqualified-origin",
    )
    try:
        with session:
            receiver = session.start_receiver()
            runtime = session.attest_qualified(
                telemetry_qualified=True
            )  # CPU Docker stand-in only.
            intent = intent_for(
                transaction.persistent,
                transaction.compiled,
                runtime,
                clock.monotonic,
                clock.session_id,
            )
            transport = session.bind_mission_transport(
                transaction=transaction.persistent,
                compiled=transaction.compiled,
                capture=cap,
                intent=intent,
                runtime_attestation=runtime,
                utc_clock=lambda: NOW,
            )
            plan = transaction.persistent.begin_upload(now_utc=NOW)
            transport.connection.mav.mission_count_send(*plan.count_send_args())
            with pytest.raises(RuntimeError, match="One main-thread"):
                session.bind_mission_transport(
                    transaction=None,
                    compiled=None,
                    capture=None,
                    intent=None,
                    runtime_attestation=None,
                )
        report = session.to_mapping()
        assert report["contract"] == "multidetect.onboard-teacher-gazebo-session-v6"
        assert receiver.to_mapping()["thread_exited"] is True
        assert receiver.to_mapping()["failure"] is None
        assert connection.closed is True and docker.current is None
        assert calls[0][1]["source_system"] == 255 and calls[0][1]["source_component"] == 190
        assert len(set(connection.read_threads)) == 1
        assert {owner for _, _, owner in connection.mav.calls} == set(connection.read_threads)
        assert not any(report["mission_transport"]["claims"].values())
    finally:
        if not cap._closed:
            cap.abort("CPU-V6-fixture-finished")
