from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .operator_link import TargetSelectionCommand, TrackStatusMessage
from .operator_protocol import (
    MAX_TUNNEL_PAYLOAD_BYTES,
    OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL,
    DecodedOperatorPacket,
    OperatorProtocolError,
    OperatorTunnelCodec,
    SelectionAck,
)


class OperatorMavlinkDependencyError(RuntimeError):
    """Raised when the optional MAVLink integration dependency is unavailable."""


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

    def decode_frame(self, frame: bytes) -> DecodedOperatorPacket:
        return self.codec.decode(self.extract_authenticated_operator_payload(frame))

    def extract_authenticated_operator_payload(self, frame: bytes) -> bytes:
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
        if message.get_msgId() != mavlink2.MAVLINK_MSG_ID_TUNNEL:
            raise OperatorProtocolError("operator endpoint accepts only MAVLink TUNNEL messages")
        if not message.get_signed():
            raise OperatorProtocolError("unsigned MAVLink operator frames are not allowed")
        if message.get_srcSystem() != self.endpoint.remote_system_id:
            raise OperatorProtocolError("MAVLink TUNNEL source system does not match")
        if message.get_srcComponent() != self.endpoint.remote_component_id:
            raise OperatorProtocolError("MAVLink TUNNEL source component does not match")
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
        return payload

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
        return bytes(message.pack(self._serializer, force_mavlink1=False))


__all__ = [
    "OperatorMavlinkDependencyError",
    "OperatorMavlinkEndpoint",
    "OperatorMavlinkTunnelAdapter",
]
