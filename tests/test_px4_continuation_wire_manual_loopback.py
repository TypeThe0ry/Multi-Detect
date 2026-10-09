"""Owned kernel UDP test peers, not PX4/Gazebo dynamics or flight evidence."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager

import pytest
from test_px4_continuation_wire_loopback import (
    _child_transaction,
    _decoded_receipts,
    _LoopbackMissionPeer,
    _session,
)

from multidetect.mission_journal import MissionTransactionState
from multidetect.px4_continuation_wire import ContinuationWireError, ContinuationWireState
from multidetect.px4_external_mission import ExternalPx4MissionError


class _QueuedMissionPeer(_LoopbackMissionPeer):
    """Queue two real frames in ONE datagram; no inter-thread arrival race.

    The decoder returns REQUEST first and retains the following real packet.
    A true nonblocking drain must examine that buffered packet even when the
    kernel socket has become empty. This is an emulated protocol edge case.
    """

    def __init__(self, port, *, queued_kind):
        self.queued_kind = queued_kind
        self.queued_packet_sha256 = None
        super().__init__(port)

    def _pack(self, message):
        writer = self.connection.mav
        packet = bytes(message.pack(writer))
        writer.seq = (writer.seq + 1) % 256
        return packet

    def _handle(self, message):
        from pymavlink.dialects.v20 import common

        if message.get_type() == "MISSION_COUNT":
            assert (message.get_srcSystem(), message.get_srcComponent()) == (250, 191)
            self.received.append(
                (message.get_type(), message.to_dict(), bytes(message.get_msgbuf()))
            )
            assert message.mission_type == 0
            self.expected_count = int(message.count)
            self.items = {}
            request = self._pack(common.MAVLink_mission_request_int_message(250, 191, 0, 0))
            if self.queued_kind == "manual":
                self.mode = 0
                queued = self._pack(common.MAVLink_heartbeat_message(2, 12, 129, 0, 4, 3))
            else:
                assert self.queued_kind == "pipelined_request"
                queued = self._pack(common.MAVLink_mission_request_int_message(250, 191, 1, 0))
            self.queued_packet_sha256 = hashlib.sha256(queued).hexdigest()
            self.connection.write(request + queued)
            return
        if message.get_type() == "MISSION_ITEM_INT" and self.queued_kind == "pipelined_request":
            # Deliberately do not repeat the already-pipelined second request.
            # The all-send drain must not silently replay it as new permission.
            self.received.append(
                (message.get_type(), message.to_dict(), bytes(message.get_msgbuf()))
            )
            assert (message.get_srcSystem(), message.get_srcComponent()) == (250, 191)
            self.items[int(message.seq)] = message
            return
        super()._handle(message)


@contextmanager
def _queued_loopback(queued_kind):
    from pymavlink import mavutil
    from pymavlink.dialects.v20 import common

    gcs = mavutil.mavlink_connection("udpin:127.0.0.1:0", source_system=250, source_component=191)
    gcs.mav = common.MAVLink(gcs, srcSystem=250, srcComponent=191)
    gcs.mav.robust_parsing = True
    port = gcs.port.getsockname()[1]
    gcs.address = f"127.0.0.1:{port}"
    peer = _QueuedMissionPeer(port, queued_kind=queued_kind)
    try:
        assert gcs.recv_match(type="HEARTBEAT", blocking=True, timeout=2) is not None
        yield gcs, peer, f"udpin:127.0.0.1:{port}"
    finally:
        try:
            peer.close()
        finally:
            gcs.close()


def test_actual_udp_request_then_buffered_manual_prevents_all_item_writes(
    tmp_path, record_property
):
    with (
        _queued_loopback("manual") as (gcs, peer, endpoint),
        _child_transaction(tmp_path) as (child, _),
    ):
        session = _session(gcs, endpoint)
        session.pause_and_confirm(expected_generation=0)
        with pytest.raises(ContinuationWireError, match="mode proof"):
            session.install_authorized_child(child, expected_generation=0)
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert child.state is MissionTransactionState.UPLOADING
        assert not peer.items
        assert not any(kind == "MISSION_ITEM_INT" for kind, _, _ in peer.received)
        assert not any(kind == "MISSION_SET_CURRENT" for kind, _, _ in peer.received)
        assert [kind for kind, _, _ in peer.received].count("MISSION_COUNT") == 1
        raw = _decoded_receipts(session)
        drained = [
            receipt
            for receipt in raw["receipts"]
            if receipt["wire_sha256"] == peer.queued_packet_sha256
        ]
        assert len(drained) == 1
        assert drained[0]["receive_kind"] == "pre_send_drain"
        assert drained[0]["message_type"] == "HEARTBEAT"
        assert drained[0]["message"]["custom_mode"] == 0
        assert not any(entry["method"] == "mission_item_int_send" for entry in raw["sends"])
        assert raw["command_causality_proven"] is False
        assert raw["flight_acceptance_proven"] is False
        record_property("kernel_udp_session", json.dumps(raw, sort_keys=True))


def test_actual_udp_pipelined_request_is_evidenced_not_replayed_and_times_out(
    tmp_path, record_property
):
    with (
        _queued_loopback("pipelined_request") as (gcs, peer, endpoint),
        _child_transaction(tmp_path) as (child, _),
    ):
        session = _session(gcs, endpoint, timeout=0.3)
        session.pause_and_confirm(expected_generation=0)
        started = time.monotonic()
        with pytest.raises((ContinuationWireError, ExternalPx4MissionError)):
            session.install_authorized_child(child, expected_generation=0)
        assert time.monotonic() - started < 0.8
        assert session.state is ContinuationWireState.RECOVERY_REQUIRED
        assert child.state is MissionTransactionState.UPLOADING
        assert set(peer.items) == {0}
        assert not any(kind == "MISSION_SET_CURRENT" for kind, _, _ in peer.received)
        raw = _decoded_receipts(session)
        drained = [
            receipt
            for receipt in raw["receipts"]
            if receipt["wire_sha256"] == peer.queued_packet_sha256
        ]
        assert len(drained) == 1
        assert drained[0]["receive_kind"] == "pre_send_drain"
        assert drained[0]["message_type"] == "MISSION_REQUEST_INT"
        assert drained[0]["message"]["seq"] == 1
        assert raw["pipelined_protocol_packets_replayed"] is False
        assert raw["flight_acceptance_proven"] is False
        record_property("kernel_udp_session", json.dumps(raw, sort_keys=True))
