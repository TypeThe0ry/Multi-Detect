from __future__ import annotations

import pytest

pytest.importorskip("pymavlink")

from pymavlink.dialects.v20 import common as mavlink2

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
from multidetect.operator_protocol import (
    OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
    OperatorProtocolError,
    OperatorTunnelCodec,
    SelectionAck,
    SelectionAckReason,
    WireMessageType,
)
from multidetect.operator_transport import SelectionCommandServer, SelectionRetryClient

KEY = b"operator-mavlink-test-key-at-least-32-bytes"
MAVLINK_KEY = b"M" * 32
GEOMETRY = VideoGeometry("camera-main", 1280, 720)
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
STATUS_ID = "33333333-3333-4333-8333-333333333333"
JETSON_ENDPOINT = OperatorMavlinkEndpoint(1, 191, 255, 190)
G20_ENDPOINT = OperatorMavlinkEndpoint(255, 190, 1, 191)


def _adapter(endpoint: OperatorMavlinkEndpoint) -> OperatorMavlinkTunnelAdapter:
    return OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,)),
        endpoint,
        signing_key=MAVLINK_KEY,
        signing_link_id=endpoint.local_component_id,
        initial_signing_timestamp=1_000_000 + endpoint.local_system_id,
    )


def _signed_frame(message, *, source_system: int, source_component: int) -> bytes:
    serializer = mavlink2.MAVLink(
        None,
        srcSystem=source_system,
        srcComponent=source_component,
    )
    serializer.signing.secret_key = MAVLINK_KEY
    serializer.signing.link_id = source_component
    serializer.signing.timestamp = 2_000_000 + source_system
    serializer.signing.sign_outgoing = True
    return bytes(message.pack(serializer, force_mavlink1=False))


def _selection() -> TargetSelectionCommand:
    return TargetSelectionCommand(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        sequence=7,
        action=SelectionAction.SELECT,
        geometry=GEOMETRY,
        issued_at_s=100.0,
        expires_at_s=103.0,
        bbox=BoundingBox(0.2, 0.2, 0.5, 0.5),
        displayed_frame_id="g20-frame-1",
    )


def test_g20_selection_is_packed_as_addressed_mavlink2_tunnel() -> None:
    frame = _adapter(G20_ENDPOINT).encode_selection(_selection())

    decoded = _adapter(JETSON_ENDPOINT).decode_frame(frame)

    assert frame[0] == mavlink2.PROTOCOL_MARKER_V2
    assert decoded.message_type is WireMessageType.TARGET_SELECTION
    assert isinstance(decoded.message, TargetSelectionCommand)
    assert decoded.message.command_id == COMMAND_ID


def test_jetson_ack_and_tracking_status_round_trip_to_g20() -> None:
    jetson = _adapter(JETSON_ENDPOINT)
    g20 = _adapter(G20_ENDPOINT)
    ack = SelectionAck(COMMAND_ID, True, SelectionAckReason.ACCEPTED, 7)
    status = TrackStatusMessage(
        status_id=STATUS_ID,
        selection_command_id=COMMAND_ID,
        sequence=8,
        geometry=GEOMETRY,
        state=TrackingState.TRACKING,
        target_id="track-1",
        bbox=BoundingBox(0.21, 0.2, 0.51, 0.5),
        label="flame",
        confidence=0.91,
        tracking_quality=0.88,
        source_frame_id="jetson-frame-1",
        source_captured_at_s=100.1,
        produced_at_s=100.2,
    )

    decoded_ack = g20.decode_frame(jetson.encode_ack(ack, sequence=20, sent_at_s=100.2))
    decoded_status = g20.decode_frame(jetson.encode_track_status(status))

    assert decoded_ack.message == ack
    assert decoded_status.message_type is WireMessageType.TRACK_STATUS
    assert isinstance(decoded_status.message, TrackStatusMessage)
    assert decoded_status.message.label == "flame"


def test_operator_endpoint_rejects_unrelated_mavlink_message() -> None:
    heartbeat = mavlink2.MAVLink_heartbeat_message(0, 0, 0, 0, 0, 3)
    frame = _signed_frame(heartbeat, source_system=255, source_component=190)

    with pytest.raises(OperatorProtocolError, match="only MAVLink TUNNEL"):
        _adapter(JETSON_ENDPOINT).decode_frame(frame)


def test_operator_endpoint_rejects_wrong_source_target_and_payload_type() -> None:
    valid_payload = OperatorTunnelCodec(
        hmac_key=KEY,
        geometries=(GEOMETRY,),
    ).encode_selection(_selection())

    def tunnel_frame(*, source: int, target: int, payload_type: int) -> bytes:
        message = mavlink2.MAVLink_tunnel_message(
            target,
            191,
            payload_type,
            len(valid_payload),
            list(valid_payload) + [0] * (128 - len(valid_payload)),
        )
        return _signed_frame(message, source_system=source, source_component=190)

    with pytest.raises(OperatorProtocolError, match="source system"):
        _adapter(JETSON_ENDPOINT).decode_frame(
            tunnel_frame(
                source=254,
                target=1,
                payload_type=OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
            )
        )
    with pytest.raises(OperatorProtocolError, match="target system"):
        _adapter(JETSON_ENDPOINT).decode_frame(
            tunnel_frame(
                source=255,
                target=2,
                payload_type=OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
            )
        )
    with pytest.raises(OperatorProtocolError, match="payload type"):
        _adapter(JETSON_ENDPOINT).decode_frame(
            tunnel_frame(source=255, target=1, payload_type=42_001)
        )


def test_operator_endpoint_rejects_broadcast_component_ids() -> None:
    with pytest.raises(ValueError, match="broadcast IDs"):
        OperatorMavlinkEndpoint(1, 191, 0, 190)


def test_operator_endpoint_rejects_tampering_and_exact_signed_frame_replay() -> None:
    sender = _adapter(G20_ENDPOINT)
    receiver = _adapter(JETSON_ENDPOINT)
    frame = sender.encode_selection(_selection())

    assert receiver.decode_frame(frame).message_type is WireMessageType.TARGET_SELECTION
    with pytest.raises(OperatorProtocolError, match="Invalid signature"):
        receiver.decode_frame(frame)

    tampered = bytearray(sender.encode_selection(_selection()))
    tampered[-1] ^= 0x01
    with pytest.raises(OperatorProtocolError, match="validation"):
        _adapter(JETSON_ENDPOINT).decode_frame(bytes(tampered))


def test_operator_adapter_requires_a_full_mavlink_signing_key() -> None:
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        OperatorMavlinkTunnelAdapter(
            OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,)),
            G20_ENDPOINT,
            signing_key=b"weak",
            signing_link_id=1,
            initial_signing_timestamp=1,
        )


def test_mavlink_wrapper_composes_with_bounded_selection_ack_transport() -> None:
    g20 = _adapter(G20_ENDPOINT)
    jetson = _adapter(JETSON_ENDPOINT)
    client = SelectionRetryClient(g20.codec, _selection())
    server = SelectionCommandServer(jetson.codec, SelectionCommandGuard(GEOMETRY))

    inner_selection = client.poll(now_s=100.0)
    assert inner_selection is not None
    air_frame = g20.wrap_authenticated_operator_payload(inner_selection)
    server_result = server.handle_selection(
        jetson.extract_authenticated_operator_payload(air_frame),
        received_at_s=100.1,
        acknowledgement_sequence=20,
    )
    ground_frame = jetson.wrap_authenticated_operator_payload(server_result.acknowledgement_payload)
    acknowledgement = client.handle_acknowledgement(
        g20.extract_authenticated_operator_payload(ground_frame)
    )

    assert acknowledgement.accepted is True
    assert client.completed is True
