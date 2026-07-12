from __future__ import annotations

import random

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_protocol import (
    MAX_TUNNEL_PAYLOAD_BYTES,
    OperatorProtocolError,
    OperatorTunnelCodec,
    SelectionAck,
    SelectionAckReason,
    WireMessageType,
)

KEY = b"operator-link-unit-test-key-32-bytes-minimum"
GEOMETRY = VideoGeometry("camera-main", 1280, 720)
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
STATUS_ID = "33333333-3333-4333-8333-333333333333"


def _codec() -> OperatorTunnelCodec:
    return OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,))


def _selection(**overrides: object) -> TargetSelectionCommand:
    values: dict[str, object] = {
        "command_id": COMMAND_ID,
        "session_id": SESSION_ID,
        "sequence": 7,
        "action": SelectionAction.SELECT,
        "geometry": GEOMETRY,
        "issued_at_s": 1_000.125,
        "expires_at_s": 1_003.125,
        "bbox": BoundingBox(0.32, 0.21, 0.61, 0.72),
        "displayed_frame_id": "g20-frame-500",
    }
    values.update(overrides)
    return TargetSelectionCommand(**values)


def test_selection_round_trip_fits_tunnel_payload() -> None:
    codec = _codec()

    encoded = codec.encode_selection(_selection())
    decoded = codec.decode(encoded)

    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.TARGET_SELECTION
    assert decoded.sequence == 7
    command = decoded.message
    assert isinstance(command, TargetSelectionCommand)
    assert command.command_id == COMMAND_ID
    assert command.session_id == SESSION_ID
    assert command.geometry == GEOMETRY
    assert command.bbox is not None
    assert command.bbox.x1 == pytest.approx(0.32, abs=1 / 65535)
    assert command.bbox.y2 == pytest.approx(0.72, abs=1 / 65535)
    assert command.displayed_frame_id is not None
    assert command.displayed_frame_id.startswith("hash64:")
    assert command.issued_at_s == pytest.approx(1_000.125)
    assert command.expires_at_s == pytest.approx(1_003.125)


def test_cancel_round_trip_has_no_bbox() -> None:
    decoded = _codec().decode(
        _codec().encode_selection(
            _selection(action=SelectionAction.CANCEL, bbox=None, displayed_frame_id=None)
        )
    )

    command = decoded.message
    assert isinstance(command, TargetSelectionCommand)
    assert command.action is SelectionAction.CANCEL
    assert command.bbox is None
    assert command.displayed_frame_id is None


def test_acknowledgement_round_trip_is_correlated() -> None:
    codec = _codec()
    ack = SelectionAck(COMMAND_ID, True, SelectionAckReason.ACCEPTED, 7)

    decoded = codec.decode(codec.encode_ack(ack, sequence=12, sent_at_s=1_000.25))

    assert decoded.message_type is WireMessageType.SELECTION_ACK
    assert decoded.sequence == 12
    assert decoded.message == ack


def test_track_status_round_trip_fits_worst_case_tunnel_frame() -> None:
    codec = _codec()
    status = TrackStatusMessage(
        status_id=STATUS_ID,
        selection_command_id=COMMAND_ID,
        sequence=99,
        geometry=GEOMETRY,
        state=TrackingState.TRACKING,
        target_id="tracker-target-123",
        bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
        label="smolder_area",
        confidence=0.91,
        tracking_quality=0.87,
        source_frame_id="jetson-frame-700",
        source_captured_at_s=1_000.2,
        produced_at_s=1_000.25,
        relative_bearing_deg=-4.2,
        estimated_range_m=82.0,
    )

    encoded = codec.encode_track_status(status)
    decoded = codec.decode(encoded)

    assert len(encoded) == 121
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.TRACK_STATUS
    message = decoded.message
    assert isinstance(message, TrackStatusMessage)
    assert message.status_id.startswith("hash64:")
    assert message.selection_command_id == COMMAND_ID
    assert message.geometry == GEOMETRY
    assert message.target_id is not None and message.target_id.startswith("hash64:")
    assert message.source_frame_id.startswith("hash64:")
    assert message.confidence == pytest.approx(0.91, abs=1 / 254)
    assert message.tracking_quality == pytest.approx(0.87, abs=1 / 254)
    assert message.relative_bearing_deg == pytest.approx(-4.2)
    assert message.estimated_range_m == pytest.approx(82.0)


def test_authentication_rejects_single_byte_tampering() -> None:
    encoded = bytearray(_codec().encode_selection(_selection()))
    encoded[25] ^= 0x01

    with pytest.raises(OperatorProtocolError, match="authentication"):
        _codec().decode(bytes(encoded))


def test_every_single_byte_mutation_of_selection_frame_is_rejected() -> None:
    encoded = _codec().encode_selection(_selection())

    for index in range(len(encoded)):
        mutated = bytearray(encoded)
        mutated[index] ^= 0x01
        with pytest.raises(OperatorProtocolError):
            _codec().decode(bytes(mutated))


def test_deterministic_malformed_packet_fuzz_cannot_reach_decoder_output() -> None:
    generator = random.Random(20260712)

    for _ in range(256):
        malformed = generator.randbytes(generator.randrange(MAX_TUNNEL_PAYLOAD_BYTES + 1))
        with pytest.raises(OperatorProtocolError):
            _codec().decode(malformed)


def test_decoder_rejects_unregistered_stream_even_with_valid_authentication() -> None:
    encoded = _codec().encode_selection(_selection())
    other = OperatorTunnelCodec(
        hmac_key=KEY,
        geometries=(VideoGeometry("camera-secondary", 1280, 720),),
    )

    with pytest.raises(OperatorProtocolError, match="stream is not registered"):
        other.decode(encoded)


def test_codec_requires_real_key_material_and_wire_uuids() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        OperatorTunnelCodec(hmac_key=b"demo", geometries=(GEOMETRY,))
    with pytest.raises(ValueError, match="must be a UUID"):
        _codec().encode_selection(_selection(command_id="selection-1"))


def test_track_label_and_frame_age_must_fit_compact_wire_contract() -> None:
    base = dict(
        status_id=STATUS_ID,
        selection_command_id=COMMAND_ID,
        sequence=99,
        geometry=GEOMETRY,
        state=TrackingState.TRACKING,
        target_id="target",
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        confidence=0.9,
        tracking_quality=0.8,
        source_frame_id="frame",
        produced_at_s=100.0,
    )
    with pytest.raises(ValueError, match="16 UTF-8 bytes"):
        _codec().encode_track_status(
            TrackStatusMessage(
                **base,
                label="label-is-far-too-long",
                source_captured_at_s=99.9,
            )
        )
    with pytest.raises(ValueError, match="uint16"):
        _codec().encode_track_status(
            TrackStatusMessage(
                **base,
                label="flame",
                source_captured_at_s=1.0,
            )
        )
