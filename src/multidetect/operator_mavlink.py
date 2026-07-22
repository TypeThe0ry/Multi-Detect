from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecisionCommand,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    SafetyStatusMessage,
    SceneContextStatusMessage,
    TargetGeolocationStatusMessage,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackStatusMessage,
)
from .operator_protocol import (
    MAX_TUNNEL_PAYLOAD_BYTES,
    OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
    ApproachConfirmationAck,
    AuthorizationDecisionAck,
    DecodedOperatorPacket,
    OperatorProtocolError,
    OperatorTunnelCodec,
    PayloadTargetConfirmationAck,
    SelectionAck,
)


class OperatorMavlinkDependencyError(RuntimeError):
    """Raised when the optional MAVLink integration dependency is unavailable."""


_MAVLINK_SIGNING_EPOCH_UNIX_S = 1_420_070_400
_MAVLINK_SIGNING_TICKS_PER_SECOND = 100_000


def _current_signing_timestamp() -> int:
    return max(
        1,
        int((time.time() - _MAVLINK_SIGNING_EPOCH_UNIX_S) * _MAVLINK_SIGNING_TICKS_PER_SECOND),
    )


def _require_mavlink2() -> Any:
    try:
        from pymavlink.dialects.v20 import common as mavlink2
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise OperatorMavlinkDependencyError(
            "Install MAVLink support with: pip install -e '.[pixhawk]'"
        ) from exc
    return mavlink2


def _component_id(value: int, *, field_name: str) -> int:
    if not 1 <= value <= 255:
        raise ValueError(f"{field_name} must be in [1, 255]; broadcast IDs are not allowed")
    return value


@dataclass(frozen=True, slots=True)
class OperatorMavlinkEndpoint:
    """Explicit application-component addressing for direct G20/Jetson metadata traffic."""

    local_system_id: int
    local_component_id: int
    remote_system_id: int
    remote_component_id: int
    payload_type: int = OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL

    def __post_init__(self) -> None:
        for name, value in (
            ("local_system_id", self.local_system_id),
            ("local_component_id", self.local_component_id),
            ("remote_system_id", self.remote_system_id),
            ("remote_component_id", self.remote_component_id),
        ):
            _component_id(value, field_name=name)
        if not 32_768 <= self.payload_type <= 65_535:
            raise ValueError("operator TUNNEL payload_type must be in the experimental range")


@dataclass(frozen=True, slots=True)
class AuthenticatedMavlinkDatagram:
    """One fully authenticated MAVLink datagram from the configured operator endpoint."""

    message_id: int
    operator_payload: bytes | None
    is_heartbeat: bool


class OperatorMavlinkTunnelAdapter:
    """Packs operator metadata into signed MAVLink2 TUNNEL frames; it performs no I/O."""

    def __init__(
        self,
        codec: OperatorTunnelCodec,
        endpoint: OperatorMavlinkEndpoint,
        *,
        signing_key: bytes,
        signing_link_id: int,
        initial_signing_timestamp: int,
    ) -> None:
        if len(signing_key) != 32:
            raise ValueError("MAVLink2 signing key must contain exactly 32 bytes")
        if not 0 <= signing_link_id <= 255:
            raise ValueError("MAVLink2 signing link ID must be in [0, 255]")
        if not 0 <= initial_signing_timestamp < 1 << 48:
            raise ValueError("MAVLink2 signing timestamp must fit in 48 bits")
        self.codec = codec
        self.endpoint = endpoint
        mavlink2 = _require_mavlink2()
        self._serializer = mavlink2.MAVLink(
            None,
            srcSystem=endpoint.local_system_id,
            srcComponent=endpoint.local_component_id,
        )
        self._serializer.signing.secret_key = bytes(signing_key)
        self._serializer.signing.link_id = signing_link_id
        self._serializer.signing.timestamp = initial_signing_timestamp
        self._serializer.signing.sign_outgoing = True
        self._parser = mavlink2.MAVLink(None)
        self._parser.signing.secret_key = bytes(signing_key)

    def encode_selection(self, command: TargetSelectionCommand) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_selection(command))

    def encode_ack(self, ack: SelectionAck, *, sequence: int, sent_at_s: float) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_ack(ack, sequence=sequence, sent_at_s=sent_at_s)
        )

    def encode_track_status(self, status: TrackStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_track_status(status))

    def encode_mission_status(self, status: MissionStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_mission_status(status))

    def encode_safety_status(self, status: SafetyStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_safety_status(status))

    def encode_patrol_status(self, status: PatrolStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_patrol_status(status))

    def encode_range_status(self, status: RangeStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_range_status(status))

    def encode_target_geolocation_status(self, status: TargetGeolocationStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_target_geolocation_status(status)
        )

    def encode_release_status(self, status: ReleaseStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_release_status(status))

    def encode_approach_challenge(self, status: ApproachChallengeStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_approach_challenge(status)
        )

    def encode_approach_confirmation(self, command: ApproachConfirmationCommand) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_approach_confirmation(command)
        )

    def encode_approach_ack(
        self,
        acknowledgement: ApproachConfirmationAck,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_approach_ack(
                acknowledgement,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        )

    def encode_approach_status(self, status: ApproachStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(self.codec.encode_approach_status(status))

    def encode_target_pool_status(self, status: TargetPoolStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_target_pool_status(status)
        )

    def encode_scene_context_status(self, status: SceneContextStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_scene_context_status(status)
        )

    def encode_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage,
    ) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_payload_target_challenge(status)
        )

    def encode_payload_target_confirmation(
        self,
        command: PayloadTargetConfirmationCommand,
    ) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_payload_target_confirmation(command)
        )

    def encode_payload_target_ack(
        self,
        acknowledgement: PayloadTargetConfirmationAck,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_payload_target_ack(
                acknowledgement,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        )

    def encode_payload_target_status(self, status: PayloadTargetStatusMessage) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_payload_target_status(status)
        )

    def encode_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
    ) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_authorization_challenge(status)
        )

    def encode_authorization_decision(self, command: AuthorizationDecisionCommand) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_authorization_decision(command)
        )

    def encode_authorization_ack(
        self,
        acknowledgement: AuthorizationDecisionAck,
        *,
        sequence: int,
        sent_at_s: float,
    ) -> bytes:
        return self.wrap_authenticated_operator_payload(
            self.codec.encode_authorization_ack(
                acknowledgement,
                sequence=sequence,
                sent_at_s=sent_at_s,
            )
        )

    def decode_frame(self, frame: bytes) -> DecodedOperatorPacket:
        payload = self.extract_authenticated_operator_payload(frame)
        if payload is None:
            # The strict decode path never opts into unrelated-message filtering.
            raise OperatorProtocolError("operator MAVLink payload is unavailable")
        return self.codec.decode(payload)

    def extract_authenticated_operator_payload(
        self,
        frame: bytes,
        *,
        ignore_unrelated_message: bool = False,
    ) -> bytes | None:
        return self.decode_authenticated_datagram(
            frame,
            ignore_unrelated_message=ignore_unrelated_message,
        ).operator_payload

    def decode_authenticated_datagram(
        self,
        frame: bytes,
        *,
        ignore_unrelated_message: bool = False,
    ) -> AuthenticatedMavlinkDatagram:
        if not frame:
            raise OperatorProtocolError("MAVLink frame is empty")
        mavlink2 = _require_mavlink2()
        try:
            messages = self._parser.parse_buffer(frame) or []
        except Exception as exc:
            raise OperatorProtocolError(
                f"MAVLink frame failed signature, CRC or framing validation: {exc}"
            ) from exc
        if len(messages) != 1:
            raise OperatorProtocolError("exactly one MAVLink message is required per frame")
        message = messages[0]
        if len(message.get_msgbuf()) != len(frame):
            raise OperatorProtocolError("MAVLink frame contains trailing or unrelated bytes")
        message_id = int(message.get_msgId())
        # Authentication and endpoint identity are checked before an unrelated
        # message may be ignored. This lets the UDP server use QGC's normal
        # signed HEARTBEAT as peer discovery without accepting an unsigned or
        # spoofed source as a metadata recipient.
        if not message.get_signed():
            raise OperatorProtocolError("unsigned MAVLink operator frames are not allowed")
        if message.get_srcSystem() != self.endpoint.remote_system_id:
            raise OperatorProtocolError("MAVLink operator source system does not match")
        if message.get_srcComponent() != self.endpoint.remote_component_id:
            raise OperatorProtocolError("MAVLink operator source component does not match")
        if message_id != mavlink2.MAVLINK_MSG_ID_TUNNEL:
            if ignore_unrelated_message:
                return AuthenticatedMavlinkDatagram(
                    message_id=message_id,
                    operator_payload=None,
                    is_heartbeat=message_id == mavlink2.MAVLINK_MSG_ID_HEARTBEAT,
                )
            raise OperatorProtocolError("operator endpoint accepts only MAVLink TUNNEL messages")
        if message.target_system != self.endpoint.local_system_id:
            raise OperatorProtocolError("MAVLink TUNNEL target system does not match")
        if message.target_component != self.endpoint.local_component_id:
            raise OperatorProtocolError("MAVLink TUNNEL target component does not match")
        if message.payload_type != self.endpoint.payload_type:
            raise OperatorProtocolError("MAVLink TUNNEL payload type does not match")
        payload_length = int(message.payload_length)
        if not 1 <= payload_length <= MAX_TUNNEL_PAYLOAD_BYTES:
            raise OperatorProtocolError("MAVLink TUNNEL payload length is invalid")
        payload = bytes(message.payload[:payload_length])
        # Authenticate and structurally validate before returning the application payload.
        self.codec.decode(payload)
        return AuthenticatedMavlinkDatagram(
            message_id=message_id,
            operator_payload=payload,
            is_heartbeat=False,
        )

    def wrap_authenticated_operator_payload(self, payload: bytes) -> bytes:
        if not 1 <= len(payload) <= MAX_TUNNEL_PAYLOAD_BYTES:
            raise ValueError("operator payload does not fit MAVLink TUNNEL")
        # Refuse to tunnel arbitrary bytes or a payload signed under another codec key.
        self.codec.decode(payload)
        mavlink2 = _require_mavlink2()
        padded = list(payload) + [0] * (MAX_TUNNEL_PAYLOAD_BYTES - len(payload))
        message = mavlink2.MAVLink_tunnel_message(
            self.endpoint.remote_system_id,
            self.endpoint.remote_component_id,
            self.endpoint.payload_type,
            len(payload),
            padded,
        )
        # pymavlink only increments its signing timestamp by one per packet. A
        # long-running, low-rate process would therefore drift far behind wall
        # time and a newly connected QGC would reject the stream as stale. Catch
        # up before every signature while retaining pymavlink's monotonic value
        # if the clock moves backwards.
        self._serializer.signing.timestamp = max(
            self._serializer.signing.timestamp,
            _current_signing_timestamp(),
        )
        return bytes(message.pack(self._serializer, force_mavlink1=False))


__all__ = [
    "AuthenticatedMavlinkDatagram",
    "OperatorMavlinkDependencyError",
    "OperatorMavlinkEndpoint",
    "OperatorMavlinkTunnelAdapter",
]
