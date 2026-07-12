from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_link import (
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    VideoGeometry,
)
from multidetect.operator_protocol import (
    OperatorProtocolError,
    OperatorTunnelCodec,
    SelectionAck,
    SelectionAckReason,
)
from multidetect.operator_transport import (
    SelectionCommandServer,
    SelectionDeliveryTimeout,
    SelectionRetryClient,
)

KEY = b"operator-transport-test-key-at-least-32-bytes"
GEOMETRY = VideoGeometry("camera-main", 1280, 720)
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"


def _codec() -> OperatorTunnelCodec:
    return OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,))


def _command(**overrides: object) -> TargetSelectionCommand:
    values: dict[str, object] = {
        "command_id": COMMAND_ID,
        "session_id": SESSION_ID,
        "sequence": 7,
        "action": SelectionAction.SELECT,
        "geometry": GEOMETRY,
        "issued_at_s": 100.0,
        "expires_at_s": 103.0,
        "bbox": BoundingBox(0.2, 0.2, 0.5, 0.5),
        "displayed_frame_id": "g20-frame-1",
    }
    values.update(overrides)
    return TargetSelectionCommand(**values)


def test_selection_retry_client_and_server_complete_correlated_exchange() -> None:
    codec = _codec()
    command = _command()
    client = SelectionRetryClient(codec, command)
    server = SelectionCommandServer(codec, SelectionCommandGuard(GEOMETRY))

    payload = client.poll(now_s=100.0)
    assert payload is not None
    result = server.handle_selection(
        payload,
        received_at_s=100.1,
        acknowledgement_sequence=20,
    )
    acknowledgement = client.handle_acknowledgement(result.acknowledgement_payload)

    assert result.acceptance.allowed is True
    assert result.duplicate is False
    assert acknowledgement.accepted is True
    assert acknowledgement.acknowledged_sequence == command.sequence
    assert client.completed is True
    assert client.poll(now_s=100.2) is None


def test_server_returns_same_decision_for_identical_retransmission() -> None:
    codec = _codec()
    payload = codec.encode_selection(_command())
    server = SelectionCommandServer(codec, SelectionCommandGuard(GEOMETRY))

    first = server.handle_selection(
        payload,
        received_at_s=100.1,
        acknowledgement_sequence=20,
    )
    repeated = server.handle_selection(
        payload,
        received_at_s=100.2,
        acknowledgement_sequence=21,
    )

    assert first.acceptance.allowed is True
    assert repeated.acceptance == first.acceptance
    assert repeated.duplicate is True


def test_server_rejects_command_id_reused_with_different_signed_content() -> None:
    codec = _codec()
    server = SelectionCommandServer(codec, SelectionCommandGuard(GEOMETRY))
    original = codec.encode_selection(_command())
    changed = codec.encode_selection(_command(bbox=BoundingBox(0.3, 0.3, 0.6, 0.6)))
    server.handle_selection(original, received_at_s=100.1, acknowledgement_sequence=20)

    conflict = server.handle_selection(
        changed,
        received_at_s=100.2,
        acknowledgement_sequence=21,
    )
    decoded_ack = codec.decode(conflict.acknowledgement_payload).message

    assert conflict.acceptance.allowed is False
    assert conflict.duplicate is True
    assert isinstance(decoded_ack, SelectionAck)
    assert decoded_ack.reason is SelectionAckReason.COMMAND_ID_CONFLICT


def test_selection_retry_budget_is_bounded_and_uses_identical_payload() -> None:
    client = SelectionRetryClient(
        _codec(),
        _command(),
        retry_interval_s=0.25,
        maximum_attempts=3,
    )

    attempts = [client.poll(now_s=value) for value in (100.0, 100.25, 100.5)]

    assert attempts[0] == attempts[1] == attempts[2]
    with pytest.raises(SelectionDeliveryTimeout, match="retry budget"):
        client.poll(now_s=100.75)


def test_selection_client_rejects_ack_for_another_command() -> None:
    codec = _codec()
    client = SelectionRetryClient(codec, _command())
    wrong_ack = SelectionAck(
        "33333333-3333-4333-8333-333333333333",
        True,
        SelectionAckReason.ACCEPTED,
        7,
    )
    payload = codec.encode_ack(wrong_ack, sequence=30, sent_at_s=100.1)

    with pytest.raises(OperatorProtocolError, match="command ID does not match"):
        client.handle_acknowledgement(payload)
