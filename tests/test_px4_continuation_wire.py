from __future__ import annotations

import base64
import hashlib
import inspect
import io
import socket
import threading
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pymavlink.dialects.v20 import common as mav
from test_onboard_continuation import _compile, _inputs

from multidetect.mission_journal import (
    DurableMissionJournal,
    MissionTransactionState,
    PersistentPx4MissionTransaction,
)
from multidetect.px4_continuation_wire import (
    PX4_AUTO_LOITER_CUSTOM_MODE,
    ContinuationWireError,
    ContinuationWireState,
    Px4ContinuationWireSession,
)
from multidetect.px4_external_mission import ExternalRuntimeAttestation
from multidetect.px4_mission import PX4_AUTO_MISSION_CUSTOM_MODE, Px4MissionTransaction
from multidetect.px4_mission_acceptance import _build_transaction_documents

NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = 10.0
        self.utc_calls = 0

    def __call__(self):
        return self.value

    def utc(self):
        self.utc_calls += 1
        return NOW + timedelta(microseconds=self.utc_calls)


def _packed(message, source=(1, 1)):
    writer = mav.MAVLink(io.BytesIO(), srcSystem=source[0], srcComponent=source[1])
    return mav.MAVLink(None).parse_char(message.pack(writer))


def _hb(mode=PX4_AUTO_LOITER_CUSTOM_MODE, base_mode=129, source=(1, 1)):
    return _packed(mav.MAVLink_heartbeat_message(2, 12, base_mode, mode, 4, 3), source)


def _air(landed=2):
    return _packed(mav.MAVLink_extended_sys_state_message(0, landed))


def _current(sequence=1):
    return _packed(mav.MAVLink_mission_current_message(sequence))


class Peer:
    """Actual packed/decoded MAVLink unit peer, not Gazebo or flight evidence."""

    def __init__(self, clock):
        self.clock = clock
        self.queue = deque()
        self.sends = []
        self.items = {}
        self.expected_count = 0
        self.cursor = 1
        self.mode = PX4_AUTO_MISSION_CUSTOM_MODE
        self.hook = None

    def state(self):
        self.queue.extend((_hb(self.mode), _air(), _current(self.cursor)))

    def send(self, name, args):
        self.sends.append((name, args))
        if self.hook:
            self.hook(name, args)
        if name == "set_mode_send":
            self.mode = args[2]
            self.state()
        elif name == "mission_count_send":
            self.items = {}
            self.expected_count = args[2]
            self.queue.append(_packed(mav.MAVLink_mission_request_int_message(250, 191, 0, 0)))
        elif name == "mission_item_int_send":
            self.items[args[2]] = args
            remaining = [
                sequence for sequence in range(self.expected_count) if sequence not in self.items
            ]
            if remaining:
                self.queue.append(
                    _packed(mav.MAVLink_mission_request_int_message(250, 191, remaining[0], 0))
                )
            else:
                self.queue.append(_packed(mav.MAVLink_mission_ack_message(250, 191, 0, 0)))
        elif name == "mission_request_list_send":
            self.queue.append(
                _packed(mav.MAVLink_mission_count_message(250, 191, len(self.items), 0))
            )
        elif name == "mission_request_int_send":
            fields = list(self.items[args[2]])
            fields[:2] = [250, 191]
            fields[5] = int(args[2] == self.cursor)
            self.queue.append(_packed(mav.MAVLink_mission_item_int_message(*fields)))
        elif name == "mission_set_current_send":
            self.cursor = args[2]
            self.queue.append(_current(self.cursor))

    def receive(self, timeout):
        self.clock.value += min(timeout, 0.005) if self.queue else timeout
        return self.queue.popleft() if self.queue else None


def _attestation():
    return ExternalRuntimeAttestation.from_mapping(
        {
            "container_id": "a" * 64,
            "image": "unit-peer@sha256:" + "b" * 64,
            "vehicle_class": "multirotor",
            "airframe_profile_id": "px4_multirotor_gazebo_v1",
            "scenario_id": "unit-wire-not-gazebo",
            "scenario_manifest_sha256": "c" * 64,
            "ownership_checks": {"unit_peer_only": True},
            "telemetry_qualified": True,
            "truth_free": True,
            "hardware_control_enabled": False,
        }
    )


@pytest.fixture
def rig():
    clock = Clock()
    peer = Peer(clock)
    peer.state()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sender = SimpleNamespace(srcSystem=250, srcComponent=191)
    for name in (
        "set_mode_send",
        "mission_count_send",
        "mission_item_int_send",
        "mission_request_list_send",
        "mission_request_int_send",
        "mission_set_current_send",
    ):
        setattr(sender, name, lambda *args, name=name: peer.send(name, args))
    connection = SimpleNamespace(
        mav=sender, port=sock, address=f"127.0.0.1:{port}", udp_server=True, broadcast=False
    )
    arguments = dict(
        receive=peer.receive,
        host_clock=clock,
        utc_clock=clock.utc,
        clock_epoch="unit-host-monotonic",
        endpoint=f"udpin:127.0.0.1:{port}",
        runtime_attestation=_attestation(),
        phase_guard=lambda phase, generation: True,
    )
    session = Px4ContinuationWireSession(connection, writes_enabled=True, **arguments)
    yield session, peer, clock, connection, arguments
    sock.close()


def _transaction(tmp_path, *, expires_at=None):
    compilation = _compile()
    from multidetect.mission_planning import parse_mission_compile_runtime

    _, policy = parse_mission_compile_runtime(_inputs()[3], "multirotor")
    report, bindings, grant = _build_transaction_documents(
        vehicle_class="multirotor",
        compiled=compilation.compiled_mission,
        context_snapshot=compilation.current_context,
        platform_plan=compilation.plan,
        policy=policy,
        transaction_id="unit-child",
        now_utc=NOW,
    )
    if expires_at:
        grant = replace(grant, expires_at=expires_at)
    transaction = Px4MissionTransaction(
        transaction_id="unit-child",
        compiled=compilation.compiled_mission,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=NOW,
    )
    journal = DurableMissionJournal.create(
        tmp_path / "child.jsonl", identity=transaction, now_utc=NOW
    )
    return PersistentPx4MissionTransaction(transaction, journal)


def _install(session, peer, transaction):
    session.pause_and_confirm(expected_generation=0)
    peer.state()
    return session.install_authorized_child(transaction, expected_generation=0)


def test_actual_packed_four_phases_and_original_receipts(rig, tmp_path):
    session, peer, clock, _, _ = rig
    child = _transaction(tmp_path)
    try:
        pause = session.pause_and_confirm(expected_generation=0)
        assert [r["message_type"] for r in pause["receipts"][-3:]] == [
            "HEARTBEAT",
            "EXTENDED_SYS_STATE",
            "MISSION_CURRENT",
        ]
        peer.state()
        install = session.install_authorized_child(child, expected_generation=0)
        assert install["result"]["readback"]["raw_execution_current_flags"] == [0, 1, 0]
        assert child.state is MissionTransactionState.START_PERMITTED
        peer.state()
        session.select_and_confirm_cursor(expected_generation=0)
        peer.state()
        session.resume_and_confirm(expected_generation=0)
        assert session.state is ContinuationWireState.RESUMED
        assert peer.sends[-1] == ("set_mode_send", (1, 1, PX4_AUTO_MISSION_CUSTOM_MODE))
        assert clock.utc_calls > 25
        for receipt in session.receipts:
            mapping = receipt.to_mapping()
            raw = base64.b64decode(mapping["wire_bytes_base64"], validate=True)
            assert raw == receipt.wire_bytes
            assert hashlib.sha256(raw).hexdigest() == mapping["wire_sha256"]
            actual = mav.MAVLink(None).parse_char(raw)
            assert actual.to_dict() == mapping["message"]
            assert mapping["source_system_id"] == actual.get_srcSystem() == 1
        summary = session.to_mapping()
        assert not summary["flight_acceptance_proven"]
        assert not summary["hardware_control_enabled"]
        assert not summary["automatic_fallback_sent"]
    finally:
        child.close()


def test_default_disabled_never_sends(rig):
    _, peer, _, connection, arguments = rig
    session = Px4ContinuationWireSession(connection, **arguments)
    with pytest.raises(ContinuationWireError, match="disabled"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "udpin:127.0.0.1:14550"},
        {"endpoint": "udpout:127.0.0.1:49000"},
        {"endpoint": "udpin:0.0.0.0:49000"},
        {"endpoint": "COM5"},
        {"host_clock": None},
        {"utc_clock": None},
        {"phase_guard": None, "writes_enabled": True},
        {"writes_enabled": 1},
        {"maximum_fact_age_s": 2},
        {"runtime_attestation": None},
    ],
)
def test_constructor_fail_closed(rig, change):
    _, peer, _, connection, arguments = rig
    with pytest.raises((ContinuationWireError, ValueError)):
        Px4ContinuationWireSession(connection, **(arguments | change))
    assert not peer.sends


@pytest.mark.parametrize("mutation", ["address", "source", "hardware", "profile", "not_socket"])
def test_actual_connection_and_runtime_checked(rig, mutation):
    _, peer, _, connection, arguments = rig
    if mutation == "address":
        connection.address = "10.0.0.1:12345"
    elif mutation == "source":
        connection.mav.srcSystem = 255
    elif mutation == "not_socket":
        connection.port = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 9999))
    else:
        arguments["runtime_attestation"] = replace(
            _attestation(),
            **{
                "hardware_control_enabled" if mutation == "hardware" else "vehicle_class": True
                if mutation == "hardware"
                else "vtol"
            },
        )
    with pytest.raises((ContinuationWireError, ValueError, RuntimeError)):
        Px4ContinuationWireSession(connection, **arguments)
    assert not peer.sends


@pytest.mark.parametrize(
    "bad",
    [
        "wrong_source",
        "unarmed",
        "manual",
        "landed",
        "no_current",
        "no_raw",
        "raw_mismatch",
        "bad_crc",
    ],
)
def test_pause_requires_actual_complete_fresh_facts(rig, bad):
    session, peer, clock, _, _ = rig
    peer.send = lambda name, args: peer.sends.append((name, args))
    # Existing send closures resolve peer.send dynamically.
    if bad == "wrong_source":
        peer.queue.extend((_hb(source=(2, 1)), _air(), _current()))
    elif bad == "unarmed":
        peer.queue.extend((_hb(base_mode=1), _air(), _current()))
    elif bad == "manual":
        peer.queue.extend((_hb(mode=0), _air(), _current()))
    elif bad == "landed":
        peer.queue.extend((_hb(), _air(1), _current()))
    elif bad == "no_current":
        peer.queue.extend((_hb(), _air()))
    elif bad == "no_raw":
        peer.queue.append(SimpleNamespace(get_msgbuf=lambda: b""))
    else:
        message = _hb()
        if bad == "raw_mismatch":
            message.custom_mode = 0
        else:
            message._msgbuf[-1] ^= 1
        peer.queue.append(message)
    with pytest.raises((ContinuationWireError, mav.MAVError)):
        session.pause_and_confirm(expected_generation=0)
    assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    assert not peer.sends
    assert clock.value <= 13.11


def test_cancel_during_receive_is_nonblocking_and_stops_future_sends(rig):
    session, peer, _, _, _ = rig
    entered, release = threading.Event(), threading.Event()
    errors = []

    def receive(timeout):
        entered.set()
        assert release.wait(2)
        return peer.queue.popleft()

    session.receive = receive

    def run():
        try:
            session.pause_and_confirm(expected_generation=0)
        except ContinuationWireError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2)
        assert session.cancel(expected_generation=0)
        assert session.generation == 1
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and errors
    with pytest.raises(ContinuationWireError):
        session.resume_and_confirm(expected_generation=0)
    assert not peer.sends


def test_guard_revocation_inside_reentrant_lock_prevents_even_first_send(rig):
    session, peer, _, _, _ = rig

    def revoke(phase, generation):
        session.cancel(expected_generation=generation)
        return True

    session.phase_guard = revoke
    with pytest.raises(ContinuationWireError, match="cancelled"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends


@pytest.mark.parametrize("phase", ["install", "cursor", "resume"])
def test_cancellation_in_send_never_emits_followup_or_fallback(rig, tmp_path, phase):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        session.pause_and_confirm(expected_generation=0)
        if phase != "install":
            peer.state()
            session.install_authorized_child(child, expected_generation=0)
        if phase == "resume":
            peer.state()
            session.select_and_confirm_cursor(expected_generation=0)
        peer.state()
        peer.hook = lambda name, args: session.cancel(expected_generation=0)
        count = len(peer.sends)
        method = {
            "install": lambda: session.install_authorized_child(child, expected_generation=0),
            "cursor": lambda: session.select_and_confirm_cursor(expected_generation=0),
            "resume": lambda: session.resume_and_confirm(expected_generation=0),
        }[phase]
        with pytest.raises(ContinuationWireError, match="cancelled"):
            method()
        assert len(peer.sends) == count + 1
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert not session.to_mapping()["automatic_fallback_sent"]
    finally:
        child.close()


@pytest.mark.parametrize("clock_kind", ["host", "utc"])
def test_clock_rollback_no_write(rig, clock_kind):
    session, peer, clock, _, _ = rig
    if clock_kind == "host":
        clock.value = 9
    else:
        session.utc_clock = lambda: NOW - timedelta(seconds=1)
    with pytest.raises(ContinuationWireError, match="backwards"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends


def test_expired_authorization_before_resume_sends_nothing(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        _install(session, peer, child)
        peer.state()
        session.select_and_confirm_cursor(expected_generation=0)
        count = len(peer.sends)
        session.utc_clock = lambda: NOW + timedelta(hours=1)
        peer.state()
        with pytest.raises(ValueError, match="expired"):
            session.resume_and_confirm(expected_generation=0)
        assert len(peer.sends) == count
    finally:
        child.close()


@pytest.mark.parametrize("sequence", [0, 2, 3, True])
def test_cursor_only_body_one(rig, tmp_path, sequence):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        _install(session, peer, child)
        count = len(peer.sends)
        with pytest.raises(ContinuationWireError, match="cursor 1"):
            session.select_and_confirm_cursor(sequence, expected_generation=0)
        assert len(peer.sends) == count
    finally:
        child.close()


def test_manual_mode_during_install_is_not_overwritten(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        session.pause_and_confirm(expected_generation=0)
        peer.queue.extend((_hb(mode=0), _air(), _current()))
        with pytest.raises(ContinuationWireError, match="mode proof"):
            session.install_authorized_child(child, expected_generation=0)
        assert len(peer.sends) == 1
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    finally:
        child.close()


def test_missing_ack_or_readback_does_not_commit_or_resume(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        session.pause_and_confirm(expected_generation=0)
        peer.state()
        original = peer.send

        def drop(name, args):
            original(name, args)
            if name == "mission_item_int_send" and len(peer.items) == 3:
                peer.queue.clear()

        peer.send = drop
        with pytest.raises(RuntimeError):
            session.install_authorized_child(child, expected_generation=0)
        assert child.state is MissionTransactionState.UPLOADING
        assert not any(name == "mission_set_current_send" for name, _ in peer.sends)
        assert [args[-1] for name, args in peer.sends if name == "set_mode_send"] == [
            PX4_AUTO_LOITER_CUSTOM_MODE
        ]
    finally:
        child.close()


def test_each_transaction_transition_gets_fresh_utc(rig, tmp_path, monkeypatch):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    observed = []
    for name in (
        "begin_upload",
        "mission_item_for_request",
        "accept_upload_ack",
        "verify_readback",
        "commit",
        "permit_start",
    ):
        method = getattr(child, name)

        def record(*args, name=name, method=method, **kwargs):
            observed.append((name, kwargs["now_utc"]))
            return method(*args, **kwargs)

        monkeypatch.setattr(child, name, record)
    try:
        _install(session, peer, child)
        assert [name for name, _ in observed] == ["begin_upload"] + [
            "mission_item_for_request"
        ] * 3 + ["accept_upload_ack", "verify_readback", "commit", "permit_start"]
        stamps = [stamp for _, stamp in observed]
        assert stamps == sorted(set(stamps))
    finally:
        child.close()


def test_guard_receives_actual_phase_and_can_refuse_without_send(rig):
    session, peer, _, _, _ = rig
    phases = []

    def deny(phase, generation):
        phases.append((phase, generation))
        return False

    session.phase_guard = deny
    with pytest.raises(ContinuationWireError, match="admission rejected"):
        session.pause_and_confirm(expected_generation=0)
    assert phases == [("pause", 0)] and not peer.sends
    assert session.state is ContinuationWireState.RECOVERY_REQUIRED


def test_admission_callback_cannot_run_past_deadline_then_send(rig):
    session, peer, clock, _, _ = rig

    def slow(phase, generation):
        clock.value += 4
        return True

    session.phase_guard = slow
    with pytest.raises(ContinuationWireError, match="deadline"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends


def test_first_pause_absolute_budget_includes_between_phase_planning(rig, tmp_path):
    session, peer, clock, _, _ = rig
    session.total_timeout_s = 0.05
    session.pause_and_confirm(expected_generation=0)
    clock.value += 0.05
    child = _transaction(tmp_path)
    try:
        peer.state()
        with pytest.raises(ContinuationWireError, match="deadline"):
            session.install_authorized_child(child, expected_generation=0)
        assert len(peer.sends) == 1
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    finally:
        child.close()


def test_lost_ack_is_bounded_by_entire_install_phase(rig, tmp_path):
    session, peer, clock, _, _ = rig
    session.phase_timeout_s = 0.15
    child = _transaction(tmp_path)
    try:
        session.pause_and_confirm(expected_generation=0)
        peer.state()
        original = peer.send

        def no_ack(name, args):
            original(name, args)
            if name == "mission_item_int_send" and len(peer.items) == 3:
                peer.queue.clear()

        peer.send = no_ack
        start = clock.value
        with pytest.raises(ContinuationWireError, match="deadline"):
            session.install_authorized_child(child, expected_generation=0)
        assert clock.value - start < 0.26
        assert child.state is MissionTransactionState.UPLOADING
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    finally:
        child.close()


@pytest.mark.parametrize("flags", [(0, 0, 0), (1, 1, 0), (0, 0, 1)])
def test_incoherent_readback_current_is_not_committed(rig, tmp_path, flags):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        session.pause_and_confirm(expected_generation=0)
        peer.state()
        original = peer.send

        def bad_flags(name, args):
            original(name, args)
            if name == "mission_request_int_send":
                message = peer.queue.pop()
                message.current = flags[args[2]]
                # Build a real matching packet, not a decoded-field spoof.
                fields = [
                    message.to_dict()[field]
                    for field in mav.MAVLink_mission_item_int_message.fieldnames
                ]
                peer.queue.append(_packed(mav.MAVLink_mission_item_int_message(*fields)))

        peer.send = bad_flags
        with pytest.raises(ContinuationWireError, match="coherent"):
            session.install_authorized_child(child, expected_generation=0)
        assert child.state is MissionTransactionState.READBACK_VERIFIED
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert not any(name == "mission_set_current_send" for name, _ in peer.sends)
    finally:
        child.close()


def test_commit_failure_after_upload_is_uncertain_no_resume(rig, tmp_path, monkeypatch):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)

    def fail(**kwargs):
        raise OSError("durable commit unavailable")

    monkeypatch.setattr(child, "commit", fail)
    try:
        with pytest.raises(OSError, match="commit"):
            _install(session, peer, child)
        assert len(peer.items) == 3
        assert child.state is MissionTransactionState.READBACK_VERIFIED
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert not any(name == "mission_set_current_send" for name, _ in peer.sends)
        assert [name for name, _ in peer.sends].count("set_mode_send") == 1
    finally:
        child.close()


def test_gcs_sender_identity_drift_is_rechecked_before_send(rig):
    session, peer, _, connection, _ = rig
    connection.mav.srcSystem = 255
    with pytest.raises(ContinuationWireError, match="identity drift"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends


def test_cancelled_reason_is_not_overwritten_by_late_callback(rig):
    session, _, _, _, _ = rig
    assert session.cancel(expected_generation=0, reason="operator selected manual")
    with pytest.raises(ContinuationWireError):
        session.pause_and_confirm(expected_generation=0)
    assert session.to_mapping()["recovery_reason"] == "operator selected manual"


def test_no_default_lifecycle_or_child_creation(rig):
    session, peer, _, _, _ = rig
    session.pause_and_confirm(expected_generation=0)
    with pytest.raises(ContinuationWireError, match="existing persistent"):
        session.install_authorized_child(None, expected_generation=0)
    assert len(peer.sends) == 1


def test_prequeued_hold_from_silent_peer_never_confirms_pause(rig):
    session, peer, _, _, _ = rig
    peer.queue.clear()
    peer.queue.extend((_hb(), _air(), _current()))
    peer.send = lambda name, args: peer.sends.append((name, args))
    with pytest.raises(ContinuationWireError):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends
    assert session.state is ContinuationWireState.RECOVERY_REQUIRED


def test_auto_mission_preflight_then_old_hold_is_drained_not_postcommand(rig):
    session, peer, _, _, _ = rig
    peer.queue.extend((_hb(), _air(), _current()))
    peer.send = lambda name, args: peer.sends.append((name, args))
    with pytest.raises(ContinuationWireError, match="mode proof"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends
    drained = [x for x in session.receipts if x.receive_kind == "pre_send_drain"]
    assert [x.message_type for x in drained] == [
        "HEARTBEAT",
        "EXTENDED_SYS_STATE",
        "MISSION_CURRENT",
    ]
    assert [x.receive_sequence for x in session.receipts] == list(range(1, 7))
    assert session.to_mapping()["command_causality_proven"] is False


def test_silent_peer_after_real_pause_send_does_not_use_preflight(rig):
    session, peer, _, _, _ = rig
    peer.send = lambda name, args: peer.sends.append((name, args))
    with pytest.raises(ContinuationWireError):
        session.pause_and_confirm(expected_generation=0)
    assert [x[0] for x in peer.sends] == ["set_mode_send"]
    assert len(session.receipts) == 3
    assert session.to_mapping()["sends"][0]["receive_cut_sequence"] == 3
    assert session.state is ContinuationWireState.RECOVERY_REQUIRED


def test_prequeued_current_cannot_confirm_silent_cursor_send(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        _install(session, peer, child)
        peer.state()
        peer.queue.append(_current())
        original = peer.send

        def silent(name, args):
            if name == "mission_set_current_send":
                peer.sends.append((name, args))
            else:
                original(name, args)

        peer.send = silent
        with pytest.raises(ContinuationWireError):
            session.select_and_confirm_cursor(expected_generation=0)
        assert any(x.receive_kind == "pre_send_drain" for x in session.receipts)
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert [name for name, _ in peer.sends].count("set_mode_send") == 1
    finally:
        child.close()


def test_resume_drain_detects_prequeued_mode_change_before_write(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        _install(session, peer, child)
        peer.state()
        session.select_and_confirm_cursor(expected_generation=0)
        peer.state()
        peer.queue.extend((_hb(PX4_AUTO_MISSION_CUSTOM_MODE), _air(), _current()))
        count = len(peer.sends)
        with pytest.raises(ContinuationWireError, match="mode proof"):
            session.resume_and_confirm(expected_generation=0)
        assert len(peer.sends) == count
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    finally:
        child.close()


def test_nonempty_drain_is_bounded_and_no_control_write(rig):
    session, peer, _, _, _ = rig
    peer.queue.extend(_current() for _ in range(70))
    with pytest.raises(ContinuationWireError, match="bounded empty"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends
    assert len([x for x in session.receipts if x.receive_kind == "pre_send_drain"]) == 64


def test_authorization_expiring_at_exact_presend_utc_never_resumes(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        _install(session, peer, child)
        peer.state()
        session.select_and_confirm_cursor(expected_generation=0)
        peer.state()
        count = len(peer.sends)
        recorded_count = len(session.to_mapping()["sends"])
        direct_send_samples = []

        def cliff():
            frame = inspect.currentframe()
            try:
                direct_send = frame.f_back.f_back.f_code.co_name == "_send"
            finally:
                del frame
            if direct_send:
                direct_send_samples.append(True)
                return NOW + timedelta(hours=1)
            return NOW + timedelta(seconds=1)

        session.utc_clock = cliff
        with pytest.raises(ValueError, match="expired"):
            session.resume_and_confirm(expected_generation=0)
        assert direct_send_samples == [True]
        assert len(peer.sends) == count
        assert len(session.to_mapping()["sends"]) == recorded_count
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    finally:
        child.close()


def test_packet_reads_do_not_repeat_expensive_host_admission(rig):
    session, peer, _, _, _ = rig
    callbacks = []
    peer.queue.extend(_current() for _ in range(20))
    session.phase_guard = lambda phase, generation: callbacks.append((phase, generation)) or True
    session.pause_and_confirm(expected_generation=0)
    assert callbacks == [("pause", 0)] * 4
    assert len([x for x in session.receipts if x.receive_kind == "pre_send_drain"]) == 20


def test_false_none_with_readable_actual_socket_cannot_seal_empty_cut(rig):
    session, peer, _, connection, _ = rig
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sender.sendto(bytes(_hb().get_msgbuf()), connection.port.getsockname())
        original = session.receive
        session.receive = lambda timeout: None if timeout == 0 else original(timeout)
        with pytest.raises(ContinuationWireError, match="bounded empty"):
            session.pause_and_confirm(expected_generation=0)
        assert not peer.sends
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
    finally:
        sender.close()


def test_actual_sender_failure_keeps_uncertain_attempt_and_never_fallback(rig):
    session, peer, _, connection, _ = rig

    def failure(*args):
        raise OSError("owned socket send failure")

    connection.mav.set_mode_send = failure
    with pytest.raises(OSError, match="socket send"):
        session.pause_and_confirm(expected_generation=0)
    assert not peer.sends
    report = session.to_mapping()
    assert report["state"] == "recovery_required"
    assert len(report["sends"]) == 1
    assert report["sends"][0]["call_completed"] is False
    assert report["automatic_fallback_sent"] is False


def test_manual_heartbeat_queued_after_request_prevents_actual_item_write(rig, tmp_path):
    session, peer, _, _, _ = rig
    child = _transaction(tmp_path)
    try:
        session.pause_and_confirm(expected_generation=0)
        peer.state()
        original = peer.send

        def manual_after_request(name, args):
            original(name, args)
            if name == "mission_count_send":
                peer.queue.append(_hb(mode=0))

        peer.send = manual_after_request
        with pytest.raises(ContinuationWireError, match="mode proof"):
            session.install_authorized_child(child, expected_generation=0)
        assert [name for name, _ in peer.sends].count("mission_count_send") == 1
        assert not any(name == "mission_item_int_send" for name, _ in peer.sends)
        assert not peer.items
        assert child.state is MissionTransactionState.UPLOADING
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert session.receipts[-1].message_type == "HEARTBEAT"
        assert session.receipts[-1].receive_kind == "pre_send_drain"
        assert [name for name, _ in peer.sends].count("set_mode_send") == 1
    finally:
        child.close()
