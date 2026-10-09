"""Real kernel UDP / MAVLink codec integration, NOT PX4 or Gazebo flight.

The independent peer below emulates protocol replies on ephemeral loopback
ports. Its mode and position are test data, not simulated dynamics or telemetry
from an aircraft. No Docker attestation fixture is treated as an inspected
container. This test exercises actual sends, receives, CRCs and raw bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from test_mission_execution_adapter import _runtime_attestation
from test_onboard_continuation import _compile

from multidetect.mission_journal import DurableMissionJournal, PersistentPx4MissionTransaction
from multidetect.mission_planning import parse_mission_compile_runtime
from multidetect.px4_continuation_wire import (
    PX4_AUTO_LOITER_CUSTOM_MODE,
    ContinuationWireError,
    ContinuationWireState,
    Px4ContinuationWireSession,
)
from multidetect.px4_external_mission import ExternalPx4MissionError, ExternalRuntimeAttestation
from multidetect.px4_mission import PX4_AUTO_MISSION_CUSTOM_MODE, Px4MissionTransaction
from multidetect.px4_mission_acceptance import _build_transaction_documents


class _LoopbackMissionPeer:
    """An independent MAVLink2-speaking test peer, never a flight controller."""

    def __init__(self, gcs_port, *, fault=None, navigation=None):
        from pymavlink import mavutil
        from pymavlink.dialects.v20 import common

        self.connection = mavutil.mavlink_connection(
            f"udpout:127.0.0.1:{gcs_port}", source_system=1, source_component=1
        )
        self.connection.mav = common.MAVLink(self.connection, srcSystem=1, srcComponent=1)
        self.connection.mav.robust_parsing = True
        self.fault = fault
        self.navigation = navigation
        self.mode = PX4_AUTO_MISSION_CUSTOM_MODE
        self.cursor = 1
        self.items = {}
        self.expected_count = None
        self.received = []
        self.error = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="owned-mavlink-test-peer")
        self._thread.start()

    def _telemetry(self):
        mav = self.connection.mav
        if self.fault == "silent_hold" and self.mode == PX4_AUTO_LOITER_CUSTOM_MODE:
            return
        mav.heartbeat_send(2, 12, 129, self.mode, 4)
        mav.extended_sys_state_send(0, 2)
        mav.mission_current_send(self.cursor)
        if self.navigation is not None:
            home = self.navigation["home"]
            position = self.navigation["hold_position"]
            relative_alt = round(position.altitude_m * 1000)
            boot_ms = round(time.monotonic() * 1000) % (2**32)
            mav.home_position_send(
                round(home.latitude_deg * 1e7),
                round(home.longitude_deg * 1e7),
                0,
                0,
                0,
                0,
                [1, 0, 0, 0],
                0,
                0,
                0,
                boot_ms * 1000,
            )
            mav.param_value_send(b"MIS_TKO_LAND_REQ", 0.0, 6, 1, 0)
            mav.gps_raw_int_send(
                boot_ms * 1000,
                3,
                round(position.latitude_deg * 1e7),
                round(position.longitude_deg * 1e7),
                relative_alt,
                100,
                100,
                0,
                0,
                12,
                h_acc=1000,
            )
            mav.global_position_int_send(
                boot_ms,
                round(position.latitude_deg * 1e7),
                round(position.longitude_deg * 1e7),
                relative_alt,
                relative_alt,
                0,
                0,
                0,
                0,
            )

    def _handle(self, message):
        self.received.append((message.get_type(), message.to_dict(), bytes(message.get_msgbuf())))
        assert (message.get_srcSystem(), message.get_srcComponent()) == (250, 191)
        kind = message.get_type()
        mav = self.connection.mav
        if kind == "SET_MODE":
            assert message.target_system == 1
            if self.fault != "ignore_mode":
                self.mode = int(message.custom_mode)
            self._telemetry()
        elif kind == "MISSION_COUNT":
            assert message.mission_type == 0
            self.expected_count = int(message.count)
            self.items = {}
            mav.mission_request_int_send(250, 191, 0, 0)
        elif kind == "MISSION_ITEM_INT":
            assert message.target_system == 1 and message.target_component == 1
            self.items[int(message.seq)] = message
            remaining = [i for i in range(self.expected_count) if i not in self.items]
            if remaining:
                mav.mission_request_int_send(250, 191, remaining[0], 0)
            elif self.fault != "lost_final_ack":
                mav.mission_ack_send(250, 191, 0, 0)
        elif kind == "MISSION_REQUEST_LIST":
            mav.mission_count_send(250, 191, len(self.items), 0)
        elif kind == "MISSION_REQUEST_INT":
            item = self.items[int(message.seq)]
            mav.mission_item_int_send(
                250,
                191,
                item.seq,
                item.frame,
                item.command,
                int(item.seq == self.cursor),
                item.autocontinue,
                item.param1,
                item.param2,
                item.param3,
                item.param4,
                item.x + int(self.fault == "changed_readback" and item.seq == 1),
                item.y,
                item.z,
                0,
            )
        elif kind == "MISSION_SET_CURRENT":
            self.cursor = int(message.seq)
            self._telemetry()
        else:
            raise AssertionError(f"unexpected or forbidden write: {kind}")

    def _run(self):
        try:
            next_telemetry = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_telemetry:
                    self._telemetry()
                    next_telemetry = now + 0.04
                message = self.connection.recv_match(blocking=True, timeout=0.02)
                if message is not None:
                    self._handle(message)
        except Exception as error:
            self.error = error

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self.connection.close()
        assert not self._thread.is_alive(), "owned peer thread did not stop"
        if self.error is not None:
            raise self.error


@contextmanager
def _loopback(*, fault=None, navigation=None):
    from pymavlink import mavutil
    from pymavlink.dialects.v20 import common

    gcs = mavutil.mavlink_connection("udpin:127.0.0.1:0", source_system=250, source_component=191)
    gcs.mav = common.MAVLink(gcs, srcSystem=250, srcComponent=191)
    gcs.mav.robust_parsing = True
    port = gcs.port.getsockname()[1]
    # The connection was bound with :0. Publish only the actually owned port.
    gcs.address = f"127.0.0.1:{port}"
    peer = _LoopbackMissionPeer(port, fault=fault, navigation=navigation)
    try:
        assert gcs.recv_match(type="HEARTBEAT", blocking=True, timeout=2) is not None
        yield gcs, peer, f"udpin:127.0.0.1:{port}"
    finally:
        try:
            peer.close()
        finally:
            gcs.close()


def _session(
    gcs,
    endpoint,
    *,
    phase_guard=None,
    timeout=1.5,
    host_clock=time.monotonic,
    clock_epoch="owned-kernel-udp-test-only",
    receive=None,
):
    def nonblocking_safe_receive(duration):
        if duration <= 0:
            # pymavlink timeout=0 may expire before examining queued packets.
            return gcs.recv_match(blocking=False)
        return gcs.recv_match(blocking=True, timeout=duration)

    return Px4ContinuationWireSession(
        gcs,
        receive=receive or nonblocking_safe_receive,
        host_clock=host_clock,
        utc_clock=lambda: datetime.now(timezone.utc),
        clock_epoch=clock_epoch,
        endpoint=endpoint,
        runtime_attestation=ExternalRuntimeAttestation.from_mapping(
            _runtime_attestation("multirotor")
        ),
        writes_enabled=True,
        phase_guard=phase_guard or (lambda phase, generation: True),
        phase_timeout_s=timeout,
    )


@contextmanager
def _child_transaction(tmp_path, *, admission=None, bindings_changes=None):
    compilation = _compile() if admission is None else admission.compilation
    now = datetime.now(timezone.utc)
    _, safety = parse_mission_compile_runtime(compilation.to_mapping()["runtime"], "multirotor")
    report, bindings, grant = _build_transaction_documents(
        vehicle_class="multirotor",
        compiled=compilation.compiled_mission,
        context_snapshot=compilation.current_context,
        platform_plan=compilation.plan,
        policy=safety,
        transaction_id="kernel-loopback-emulated-mission-only",
        now_utc=now,
        transport="kernel_udp_mavlink_test_peer_not_px4",
        runtime_provenance_source="test-only-owned-loopback-emulated-replies",
    )
    if admission is not None or bindings_changes:
        from dataclasses import replace

        if admission is not None:
            bindings = replace(
                bindings,
                prompt_hash=admission.current.scope.prompt_sha256,
                task_spec_hash=admission.attempt.original.anchor.task.canonical_hash,
                vehicle_identity_hash=admission.hold_parent_evidence.mission_binding.vehicle_identity_sha256,
            )
        bindings = replace(bindings, **(bindings_changes or {}))
        grant = replace(
            grant,
            **{
                name: getattr(bindings, name)
                for name in (
                    "prompt_hash",
                    "task_spec_hash",
                    "plan_hash",
                    "context_snapshot_hash",
                    "airframe_profile_hash",
                    "geofence_hash",
                    "vehicle_identity_hash",
                    "runtime_descriptor_hash",
                    "runtime_provenance_hash",
                )
            },
        )
    transaction = Px4MissionTransaction(
        transaction_id=grant.transaction_id,
        compiled=compilation.compiled_mission,
        validation_report=report,
        operator_grant=grant,
        bindings=bindings,
        now_utc=now,
    )
    with DurableMissionJournal.create(
        tmp_path / "actual-host-journal.jsonl", transaction, now_utc=now
    ) as journal:
        persistent = PersistentPx4MissionTransaction(transaction, journal)
        yield persistent, compilation


def _decoded_receipts(session):
    from pymavlink.dialects.v20 import common

    raw = session.to_mapping()
    for receipt in session.receipts:
        item = receipt.to_mapping()
        packet = base64.b64decode(item["wire_bytes_base64"], validate=True)
        assert packet == receipt.wire_bytes
        assert hashlib.sha256(packet).hexdigest() == item["wire_sha256"]
        decoded = common.MAVLink(None).parse_char(packet)
        assert decoded is not None and decoded.get_type() != "BAD_DATA"
        assert (decoded.get_srcSystem(), decoded.get_srcComponent()) == (1, 1)
        assert decoded.get_type() == item["message_type"]
    return raw


def test_kernel_udp_peer_full_phases_preserve_actual_packet_bytes(tmp_path, record_property):
    with _loopback() as (gcs, peer, endpoint), _child_transaction(tmp_path) as (child, compilation):
        session = _session(gcs, endpoint)
        generation = session.generation
        session.pause_and_confirm(expected_generation=generation)
        assert session.state is ContinuationWireState.PAUSED
        session.install_authorized_child(child, expected_generation=generation)
        session.select_and_confirm_cursor(expected_generation=generation)
        session.resume_and_confirm(expected_generation=generation)
        assert session.state is ContinuationWireState.RESUMED
        raw = _decoded_receipts(session)
        record_property("kernel_udp_session", json.dumps(raw, ensure_ascii=True, sort_keys=True))
        assert raw["flight_acceptance_proven"] is False
        assert [peer.items[i].command for i in range(3)] == [16, 16, 19]
        assert peer.items[1].x == compilation.compiled_mission.wire_items[1].x
        modes = [fields["custom_mode"] for kind, fields, _ in peer.received if kind == "SET_MODE"]
        assert modes == [PX4_AUTO_LOITER_CUSTOM_MODE, PX4_AUTO_MISSION_CUSTOM_MODE]
        assert [
            fields["seq"] for kind, fields, _ in peer.received if kind == "MISSION_SET_CURRENT"
        ] == [1]


@pytest.mark.parametrize("fault", ["lost_final_ack", "changed_readback"])
def test_real_udp_upload_fault_never_sends_resume(tmp_path, fault, record_property):
    with (
        _loopback(fault=fault) as (gcs, peer, endpoint),
        _child_transaction(tmp_path) as (child, _),
    ):
        session = _session(gcs, endpoint, timeout=0.4)
        generation = session.generation
        session.pause_and_confirm(expected_generation=generation)
        with pytest.raises((ContinuationWireError, ExternalPx4MissionError)):
            session.install_authorized_child(child, expected_generation=generation)
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        _decoded_receipts(session)
        record_property("kernel_udp_session", json.dumps(session.to_mapping(), sort_keys=True))
        assert not any(
            kind == "SET_MODE" and fields["custom_mode"] == PX4_AUTO_MISSION_CUSTOM_MODE
            for kind, fields, _ in peer.received
        )


def test_real_udp_cancel_after_installed_blocks_cursor_and_late_resume(tmp_path, record_property):
    with _loopback() as (gcs, peer, endpoint), _child_transaction(tmp_path) as (child, _):
        session = _session(gcs, endpoint)
        generation = session.generation
        session.pause_and_confirm(expected_generation=generation)
        session.install_authorized_child(child, expected_generation=generation)
        assert session.cancel(expected_generation=generation)
        with pytest.raises(ContinuationWireError):
            session.select_and_confirm_cursor(expected_generation=generation)
        with pytest.raises(ContinuationWireError):
            session.resume_and_confirm(expected_generation=generation)
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        record_property("kernel_udp_session", json.dumps(session.to_mapping(), sort_keys=True))
        assert not any(kind == "MISSION_SET_CURRENT" for kind, _, _ in peer.received)
        assert [
            fields["custom_mode"] for kind, fields, _ in peer.received if kind == "SET_MODE"
        ] == [PX4_AUTO_LOITER_CUSTOM_MODE]


def test_real_udp_pause_without_fresh_hold_is_not_confirmation(record_property):
    with _loopback(fault="silent_hold") as (gcs, peer, endpoint):
        session = _session(gcs, endpoint, timeout=0.15)
        with pytest.raises(ContinuationWireError):
            session.pause_and_confirm(expected_generation=session.generation)
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        record_property("kernel_udp_session", json.dumps(session.to_mapping(), sort_keys=True))
        assert not any(kind == "MISSION_COUNT" for kind, _, _ in peer.received)


def test_prequeued_real_udp_hold_is_not_a_reply_when_mode_write_is_ignored(record_property):
    with _loopback(fault="ignore_mode") as (gcs, peer, endpoint):
        # Deliberately place old HOLD packets in the actual kernel queue. The
        # independent peer continues AUTO and will not apply the mode command.
        peer.connection.mav.heartbeat_send(2, 12, 129, PX4_AUTO_LOITER_CUSTOM_MODE, 4)
        peer.connection.mav.extended_sys_state_send(0, 2)
        peer.connection.mav.mission_current_send(1)
        session = _session(gcs, endpoint, timeout=0.2)
        with pytest.raises(ContinuationWireError):
            session.pause_and_confirm(expected_generation=session.generation)
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert not any(kind == "MISSION_COUNT" for kind, _, _ in peer.received)
        raw = _decoded_receipts(session)
        assert raw["command_causality_proven"] is False
        record_property("kernel_udp_session", json.dumps(raw, sort_keys=True))


@pytest.mark.parametrize("cliff", ["final_upload_ack", "last_readback_item", "resume_guard"])
def test_real_udp_cancellation_cliffs_do_not_resurrect_old_generation(
    tmp_path, cliff, record_property
):
    with _loopback() as (gcs, peer, endpoint), _child_transaction(tmp_path) as (child, _):
        session = None
        revoked = False

        def phase_guard(phase, generation):
            nonlocal revoked
            if cliff == "resume_guard" and phase == "resume":
                revoked = True
                session.cancel(expected_generation=generation, reason="kernel_test_resume_cliff")
                return False
            return True

        session = _session(gcs, endpoint, phase_guard=phase_guard)
        generation = session.generation

        def receive(duration):
            nonlocal revoked
            message = (
                gcs.recv_match(blocking=False)
                if duration <= 0
                else gcs.recv_match(blocking=True, timeout=duration)
            )
            if message is not None and (
                (cliff == "final_upload_ack" and message.get_type() == "MISSION_ACK")
                or (
                    cliff == "last_readback_item"
                    and message.get_type() == "MISSION_ITEM_INT"
                    and message.seq == 2
                )
            ):
                revoked = True
                session.cancel(expected_generation=generation, reason="kernel_test_receive_cliff")
            return message

        session.receive = receive
        session.pause_and_confirm(expected_generation=generation)
        with pytest.raises(ContinuationWireError):
            session.install_authorized_child(child, expected_generation=generation)
            session.select_and_confirm_cursor(expected_generation=generation)
            session.resume_and_confirm(expected_generation=generation)
        assert revoked
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert session.generation != generation
        assert not any(
            entry["method"] == "set_mode_send" and entry["args"][-1] == PX4_AUTO_MISSION_CUSTOM_MODE
            for entry in session.to_mapping()["sends"]
        )
        assert not any(
            kind == "SET_MODE" and fields["custom_mode"] == PX4_AUTO_MISSION_CUSTOM_MODE
            for kind, fields, _ in peer.received
        )
        record_property("kernel_udp_session", json.dumps(session.to_mapping(), sort_keys=True))
