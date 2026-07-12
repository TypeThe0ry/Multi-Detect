from __future__ import annotations

import threading
import time

from multidetect.domain import BoundingBox
from multidetect.operator_link import (
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_mavlink import (
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import OperatorTunnelCodec
from multidetect.operator_udp import (
    UdpOperatorSelectionClient,
    UdpOperatorSelectionServer,
    UdpOperatorSessionClient,
)

APP_KEY = b"operator-udp-application-key-at-least-32-bytes"
MAVLINK_KEY = b"U" * 32
GEOMETRY = VideoGeometry("camera-main", 1280, 720)


def _adapter(endpoint: OperatorMavlinkEndpoint) -> OperatorMavlinkTunnelAdapter:
    return OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=APP_KEY, geometries=(GEOMETRY,)),
        endpoint,
        signing_key=MAVLINK_KEY,
        signing_link_id=endpoint.local_component_id,
        initial_signing_timestamp=3_000_000 + endpoint.local_system_id,
    )


def test_real_localhost_udp_selection_and_ack_round_trip() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    g20 = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))
    received = []

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
        receive_timeout_s=2.0,
    ) as server:
        worker = threading.Thread(target=lambda: received.append(server.serve_once()))
        worker.start()
        issued_at_s = time.time()
        receipt = UdpOperatorSelectionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=g20,
        ).deliver(
            TargetSelectionCommand(
                command_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=issued_at_s,
                expires_at_s=issued_at_s + 3.0,
                bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
                displayed_frame_id="g20-frame-500",
            )
        )
        worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert len(received) == 1
    server_result, peer = received[0]
    assert server_result.acceptance.allowed is True
    assert peer[0] == "127.0.0.1"
    assert receipt.acknowledgement.accepted is True
    assert receipt.attempts == 1
    assert receipt.elapsed_s < 2.0


def test_background_server_delivers_selection_and_returns_tracking_status() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    g20 = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
    ) as server:
        server.start_background()
        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=g20,
        ) as client:
            issued_at_s = time.time()
            command = TargetSelectionCommand(
                command_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=issued_at_s,
                expires_at_s=issued_at_s + 3.0,
                bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
            )
            assert client.deliver(command).acknowledgement.accepted is True

            deadline = time.monotonic() + 1.0
            queued = server.poll_selection()
            while queued is None and time.monotonic() < deadline:
                time.sleep(0.005)
                queued = server.poll_selection()
            assert queued is not None
            received_command, peer = queued
            assert received_command.command_id == command.command_id
            server.publish_track_status(
                TrackStatusMessage(
                    status_id="33333333-3333-4333-8333-333333333333",
                    selection_command_id=command.command_id,
                    sequence=1,
                    geometry=GEOMETRY,
                    state=TrackingState.TRACKING,
                    target_id="track-42",
                    bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
                    label="flame",
                    confidence=0.91,
                    tracking_quality=0.87,
                    source_frame_id="jetson-frame-700",
                    source_captured_at_s=issued_at_s + 0.01,
                    produced_at_s=issued_at_s + 0.02,
                ),
                peer=peer,
            )
            status = client.receive_track_status(timeout_s=1.0)

    assert status.state is TrackingState.TRACKING
    assert status.selection_command_id == command.command_id
    assert status.label == "flame"
