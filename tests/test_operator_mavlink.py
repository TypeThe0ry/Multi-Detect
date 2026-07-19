from __future__ import annotations

import pytest

pytest.importorskip("pymavlink")

from pymavlink.dialects.v20 import common as mavlink2

from multidetect import operator_mavlink as operator_mavlink_module
from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    RuleCheck,
    Verdict,
)
from multidetect.operator_link import (
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    SafetyStatusMessage,
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
from multidetect.patrol_advisory import AdvisoryValidity, PatrolPhase, ReturnObserveDirection
from multidetect.unified_tracking import UnifiedTrackState

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


def test_jetson_mission_status_round_trip_to_g20_is_advisory_only() -> None:
    jetson = _adapter(JETSON_ENDPOINT)
    g20 = _adapter(G20_ENDPOINT)
    status = MissionStatusMessage(
        status_id=STATUS_ID,
        sequence=9,
        mission_id="fire-fixed-wing-demo",
        phase=MissionPhase.DEPLOYMENT_READY,
        authorization_state=AuthorizationDisplayState.APPROVED,
        release_window=DeploymentWindowStatus.READY,
        safety_allowed=True,
        remaining_payload_count=3,
        total_payload_count=4,
        target_id="track-1",
        active_payload_slot_id="payload-1",
        target_confidence=0.91,
        relative_bearing_deg=-2.0,
        estimated_range_m=62.8,
        cross_track_error_m=0.5,
        along_track_error_m=0.1,
        release_lead_distance_m=62.7,
        produced_at_s=100.2,
    )

    decoded = g20.decode_frame(jetson.encode_mission_status(status))

    assert decoded.message_type is WireMessageType.MISSION_STATUS
    assert isinstance(decoded.message, MissionStatusMessage)
    assert decoded.message.authorization_state is AuthorizationDisplayState.APPROVED
    assert decoded.message.advisory_only is True
    assert decoded.message.physical_release_enabled is False


def test_jetson_safety_status_round_trip_to_g20_is_explanatory_only() -> None:
    status = SafetyStatusMessage(
        status_id="55555555-5555-4555-8555-555555555555",
        sequence=10,
        mission_id="fire-fixed-wing-demo",
        target_id="track-1",
        ruleset_version="rules-v1",
        checks=(
            RuleCheck("target.confirmed_track", Verdict.PASS, "confirmed"),
            RuleCheck("navigation.geofence_health", Verdict.UNKNOWN, "unknown"),
        ),
        produced_at_s=100.3,
    )

    decoded = _adapter(G20_ENDPOINT).decode_frame(
        _adapter(JETSON_ENDPOINT).encode_safety_status(status)
    )

    assert decoded.message_type is WireMessageType.SAFETY_STATUS
    assert isinstance(decoded.message, SafetyStatusMessage)
    assert decoded.message.pass_count == 1
    assert decoded.message.unknown_count == 1
    assert decoded.message.advisory_only is True
    assert decoded.message.physical_release_enabled is False


def test_jetson_patrol_status_round_trip_to_g20_is_advisory_only() -> None:
    status = PatrolStatusMessage(
        status_id="66666666-6666-4666-8666-666666666666",
        sequence=11,
        mission_id="fire-patrol-demo",
        phase=PatrolPhase.LOST,
        primary_target_id="track-vehicle-1",
        target_state=UnifiedTrackState.LOST,
        bbox=BoundingBox(0.1, 0.2, 0.3, 0.5),
        label="car",
        confidence=0.86,
        tracking_quality=0.2,
        total_track_count=10,
        locked_track_count=2,
        source_frame_id="jetson-frame-8",
        source_captured_at_s=100.2,
        produced_at_s=100.3,
        return_direction=ReturnObserveDirection.LEFT,
        return_validity=AdvisoryValidity.VALID,
        return_evidence_age_s=0.4,
        estimated_minimum_turn_radius_m=75.0,
    )

    decoded = _adapter(G20_ENDPOINT).decode_frame(
        _adapter(JETSON_ENDPOINT).encode_patrol_status(status)
    )

    assert decoded.message_type is WireMessageType.PATROL_STATUS
    assert isinstance(decoded.message, PatrolStatusMessage)
    assert decoded.message.total_track_count == 10
    assert decoded.message.return_direction is ReturnObserveDirection.LEFT
    assert decoded.message.advisory_only is True
    assert decoded.message.flight_control_enabled is False


def test_operator_endpoint_rejects_unrelated_mavlink_message() -> None:
    heartbeat = mavlink2.MAVLink_heartbeat_message(0, 0, 0, 0, 0, 3)
    frame = _signed_frame(heartbeat, source_system=255, source_component=190)

    with pytest.raises(OperatorProtocolError, match="only MAVLink TUNNEL"):
        _adapter(JETSON_ENDPOINT).decode_frame(frame)


def test_operator_endpoint_authenticates_signed_heartbeat_for_peer_discovery() -> None:
    heartbeat = mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 3, 3)
    frame = _signed_frame(heartbeat, source_system=255, source_component=190)

    datagram = _adapter(JETSON_ENDPOINT).decode_authenticated_datagram(
        frame,
        ignore_unrelated_message=True,
    )

    assert datagram.is_heartbeat is True
    assert datagram.message_id == mavlink2.MAVLINK_MSG_ID_HEARTBEAT
    assert datagram.operator_payload is None


def test_operator_peer_discovery_rejects_unsigned_or_wrong_source_heartbeat() -> None:
    heartbeat = mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 3, 3)
    unsigned_serializer = mavlink2.MAVLink(None, srcSystem=255, srcComponent=190)
    unsigned = bytes(heartbeat.pack(unsigned_serializer, force_mavlink1=False))
    wrong_source = _signed_frame(heartbeat, source_system=254, source_component=190)

    with pytest.raises(OperatorProtocolError, match="unsigned|Invalid signature"):
        _adapter(JETSON_ENDPOINT).decode_authenticated_datagram(
            unsigned,
            ignore_unrelated_message=True,
        )
    with pytest.raises(OperatorProtocolError, match="source system"):
        _adapter(JETSON_ENDPOINT).decode_authenticated_datagram(
            wrong_source,
            ignore_unrelated_message=True,
        )


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


def test_outgoing_signing_timestamp_catches_up_to_wall_clock(monkeypatch) -> None:
    wall_time_s = operator_mavlink_module._MAVLINK_SIGNING_EPOCH_UNIX_S + 12_345
    monkeypatch.setattr(operator_mavlink_module.time, "time", lambda: wall_time_s)
    sender = OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,)),
        G20_ENDPOINT,
        signing_key=MAVLINK_KEY,
        signing_link_id=G20_ENDPOINT.local_component_id,
        initial_signing_timestamp=1,
    )

    first = sender.encode_selection(_selection())
    second = sender.encode_selection(_selection())
    expected = 12_345 * operator_mavlink_module._MAVLINK_SIGNING_TICKS_PER_SECOND

    assert int.from_bytes(first[-12:-6], "little") == expected
    assert int.from_bytes(second[-12:-6], "little") == expected + 1


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
