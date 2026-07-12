from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass
from enum import IntEnum
from math import isfinite
from typing import TypeAlias
from uuid import UUID

from .domain import BoundingBox
from .operator_link import (
    OPERATOR_LINK_PROTOCOL_VERSION,
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)

MAX_TUNNEL_PAYLOAD_BYTES = 128
OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL = 42_000
AUTHENTICATION_TAG_BYTES = 16

_MAGIC = b"MD"
_HEADER = struct.Struct(">2sBBBBIQH")
_SELECTION = struct.Struct(">16s16sBIHHBHB4HBQ")
_ACK = struct.Struct(">16sBBI")
_TRACK = struct.Struct(">Q16sBIHHBBQB4HB16sBBQHhH")


class OperatorProtocolError(ValueError):
    """Raised when a TUNNEL application payload is malformed or unauthenticated."""


class WireMessageType(IntEnum):
    TARGET_SELECTION = 1
    SELECTION_ACK = 2
    TRACK_STATUS = 3


class SelectionAckReason(IntEnum):
    ACCEPTED = 0
    STALE = 1
    STREAM_MISMATCH = 2
    GEOMETRY_MISMATCH = 3
    SEQUENCE_REJECTED = 4
    FUTURE_TIMESTAMP = 5
    COMMAND_ID_CONFLICT = 6
    INVALID = 255


@dataclass(frozen=True, slots=True)
class SelectionAck:
    command_id: str
    accepted: bool
    reason: SelectionAckReason
    acknowledged_sequence: int

    def __post_init__(self) -> None:
        _uuid_bytes(self.command_id, field_name="command_id")
        if not 0 <= self.acknowledged_sequence <= 0xFFFFFFFF:
            raise ValueError("acknowledged_sequence must fit in an unsigned 32-bit integer")
        if self.accepted and self.reason is not SelectionAckReason.ACCEPTED:
            raise ValueError("an accepted acknowledgement must use the ACCEPTED reason")
        if not self.accepted and self.reason is SelectionAckReason.ACCEPTED:
            raise ValueError("a rejected acknowledgement cannot use the ACCEPTED reason")


OperatorMessage: TypeAlias = TargetSelectionCommand | SelectionAck | TrackStatusMessage


@dataclass(frozen=True, slots=True)
class DecodedOperatorPacket:
    message_type: WireMessageType
    sequence: int
    sent_at_s: float
    message: OperatorMessage


class OperatorTunnelCodec:
    """Authenticated compact payload codec carried inside a MAVLink2 TUNNEL message."""

    def __init__(self, *, hmac_key: bytes, geometries: tuple[VideoGeometry, ...]) -> None:
        if len(hmac_key) < 32:
            raise ValueError("operator-link HMAC key must contain at least 32 bytes")
        if not geometries:
            raise ValueError("at least one video geometry must be registered")
        self._hmac_key = bytes(hmac_key)
        self._geometries: dict[int, VideoGeometry] = {}
        for geometry in geometries:
            stream_hash = _hash32(geometry.stream_id)
            existing = self._geometries.get(stream_hash)
            if existing is not None and existing.stream_id != geometry.stream_id:
                raise ValueError("video stream hash collision")
            self._geometries[stream_hash] = geometry

    def encode_selection(self, command: TargetSelectionCommand) -> bytes:
        geometry = command.geometry
        self._require_registered_stream(geometry.stream_id)
        action = {
            SelectionAction.SELECT: 1,
            SelectionAction.SWITCH: 2,
            SelectionAction.CANCEL: 3,
        }[command.action]
        ttl_ms = _bounded_uint(
            round((command.expires_at_s - command.issued_at_s) * 1000.0),
            bits=16,
            field_name="selection TTL milliseconds",
        )
        if command.bbox is None:
            bbox_present = 0
            bbox = (0, 0, 0, 0)
        else:
            bbox_present = 1
            bbox = _encode_bbox(command.bbox)
        frame_present = int(command.displayed_frame_id is not None)
        frame_hash = _hash64(command.displayed_frame_id) if command.displayed_frame_id else 0
        body = _SELECTION.pack(
            _uuid_bytes(command.command_id, field_name="command_id"),
            _uuid_bytes(command.session_id, field_name="session_id"),
            action,
            _hash32(geometry.stream_id),
            geometry.width,
            geometry.height,
            _rotation_code(geometry.rotation_degrees),
            ttl_ms,
            bbox_present,
            *bbox,
            frame_present,
            frame_hash,
        )
        return self._encode_frame(
            WireMessageType.TARGET_SELECTION,
            sequence=command.sequence,
            sent_at_s=command.issued_at_s,
            body=body,
        )

    def encode_ack(self, ack: SelectionAck, *, sequence: int, sent_at_s: float) -> bytes:
        body = _ACK.pack(
            _uuid_bytes(ack.command_id, field_name="command_id"),
            int(ack.accepted),
            int(ack.reason),
            ack.acknowledged_sequence,
        )
        return self._encode_frame(
            WireMessageType.SELECTION_ACK,
            sequence=sequence,
            sent_at_s=sent_at_s,
            body=body,
        )

    def encode_track_status(self, status: TrackStatusMessage) -> bytes:
        geometry = status.geometry
        self._require_registered_stream(geometry.stream_id)
        state = {
            TrackingState.INITIALIZING: 1,
            TrackingState.TRACKING: 2,
            TrackingState.LOST: 3,
            TrackingState.CANCELLED: 4,
            TrackingState.REJECTED: 5,
        }[status.state]
        target_present = int(status.target_id is not None)
        target_hash = _hash64(status.target_id) if status.target_id else 0
        if status.bbox is None:
            bbox_present = 0
            bbox = (0, 0, 0, 0)
        else:
            bbox_present = 1
            bbox = _encode_bbox(status.bbox)
        label = (status.label or "").encode("utf-8")
        if len(label) > 16:
            raise ValueError("tracking label cannot exceed 16 UTF-8 bytes on the wire")
        confidence = _encode_ratio(status.confidence)
        quality = _encode_ratio(status.tracking_quality)
        source_age_ms = _bounded_uint(
            round((status.produced_at_s - status.source_captured_at_s) * 1000.0),
            bits=16,
            field_name="source frame age milliseconds",
        )
        bearing = _encode_bearing(status.relative_bearing_deg)
        distance = _encode_distance(status.estimated_range_m)
        body = _TRACK.pack(
            _hash64(status.status_id),
            _uuid_bytes(status.selection_command_id, field_name="selection_command_id"),
            state,
            _hash32(geometry.stream_id),
            geometry.width,
            geometry.height,
            _rotation_code(geometry.rotation_degrees),
            target_present,
            target_hash,
            bbox_present,
            *bbox,
            len(label),
            label.ljust(16, b"\0"),
            confidence,
            quality,
            _hash64(status.source_frame_id),
            source_age_ms,
            bearing,
            distance,
        )
        return self._encode_frame(
            WireMessageType.TRACK_STATUS,
            sequence=status.sequence,
            sent_at_s=status.produced_at_s,
            body=body,
        )

    def decode(self, payload: bytes) -> DecodedOperatorPacket:
        if len(payload) < _HEADER.size + AUTHENTICATION_TAG_BYTES:
            raise OperatorProtocolError("operator-link payload is truncated")
        if len(payload) > MAX_TUNNEL_PAYLOAD_BYTES:
            raise OperatorProtocolError("operator-link payload exceeds MAVLink TUNNEL capacity")
        try:
            magic, version, type_value, flags, reserved, sequence, sent_ms, body_length = (
                _HEADER.unpack_from(payload)
            )
        except struct.error as exc:  # pragma: no cover - guarded by the length check
            raise OperatorProtocolError("operator-link header is malformed") from exc
        if magic != _MAGIC:
            raise OperatorProtocolError("operator-link magic does not match")
        if version != OPERATOR_LINK_PROTOCOL_VERSION:
            raise OperatorProtocolError("unsupported operator-link wire version")
        if flags != 0 or reserved != 0:
            raise OperatorProtocolError("unsupported operator-link flags")
        expected_length = _HEADER.size + body_length + AUTHENTICATION_TAG_BYTES
        if len(payload) != expected_length:
            raise OperatorProtocolError("operator-link body length does not match the frame")
        signed = payload[:-AUTHENTICATION_TAG_BYTES]
        supplied_tag = payload[-AUTHENTICATION_TAG_BYTES:]
        expected_tag = hmac.digest(self._hmac_key, signed, "sha256")[:AUTHENTICATION_TAG_BYTES]
        if not hmac.compare_digest(supplied_tag, expected_tag):
            raise OperatorProtocolError("operator-link authentication failed")
        try:
            message_type = WireMessageType(type_value)
        except ValueError as exc:
            raise OperatorProtocolError("unknown operator-link message type") from exc
        body = payload[_HEADER.size : -AUTHENTICATION_TAG_BYTES]
        sent_at_s = sent_ms / 1000.0
        if message_type is WireMessageType.TARGET_SELECTION:
            message = self._decode_selection(body, sequence=sequence, sent_at_s=sent_at_s)
        elif message_type is WireMessageType.SELECTION_ACK:
            message = self._decode_ack(body)
        else:
            message = self._decode_track(body, sequence=sequence, sent_at_s=sent_at_s)
        return DecodedOperatorPacket(message_type, sequence, sent_at_s, message)

    def _encode_frame(
        self,
        message_type: WireMessageType,
        *,
        sequence: int,
        sent_at_s: float,
        body: bytes,
    ) -> bytes:
        sequence = _bounded_uint(sequence, bits=32, field_name="wire sequence")
        sent_ms = _timestamp_ms(sent_at_s)
        header = _HEADER.pack(
            _MAGIC,
            OPERATOR_LINK_PROTOCOL_VERSION,
            int(message_type),
            0,
            0,
            sequence,
            sent_ms,
            len(body),
        )
        signed = header + body
        tag = hmac.digest(self._hmac_key, signed, "sha256")[:AUTHENTICATION_TAG_BYTES]
        frame = signed + tag
        if len(frame) > MAX_TUNNEL_PAYLOAD_BYTES:
            raise ValueError("encoded operator-link frame exceeds MAVLink TUNNEL capacity")
        return frame

    def _decode_selection(
        self, body: bytes, *, sequence: int, sent_at_s: float
    ) -> TargetSelectionCommand:
        if len(body) != _SELECTION.size:
            raise OperatorProtocolError("target-selection body has an invalid size")
        (
            command_id,
            session_id,
            action_value,
            stream_hash,
            width,
            height,
            rotation,
            ttl_ms,
            bbox_present,
            x1,
            y1,
            x2,
            y2,
            frame_present,
            frame_hash,
        ) = _SELECTION.unpack(body)
        action = {
            1: SelectionAction.SELECT,
            2: SelectionAction.SWITCH,
            3: SelectionAction.CANCEL,
        }.get(action_value)
        if action is None:
            raise OperatorProtocolError("target-selection action is invalid")
        geometry = self._wire_geometry(stream_hash, width, height, rotation)
        if bbox_present not in {0, 1} or frame_present not in {0, 1}:
            raise OperatorProtocolError("target-selection presence flag is invalid")
        bbox = _decode_bbox((x1, y1, x2, y2)) if bbox_present else None
        frame_id = _hashed_identifier(frame_hash) if frame_present else None
        try:
            return TargetSelectionCommand(
                command_id=str(UUID(bytes=command_id)),
                session_id=str(UUID(bytes=session_id)),
                sequence=sequence,
                action=action,
                geometry=geometry,
                issued_at_s=sent_at_s,
                expires_at_s=sent_at_s + ttl_ms / 1000.0,
                bbox=bbox,
                displayed_frame_id=frame_id,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid target-selection content: {exc}") from exc

    def _decode_ack(self, body: bytes) -> SelectionAck:
        if len(body) != _ACK.size:
            raise OperatorProtocolError("selection-ack body has an invalid size")
        command_id, accepted, reason_value, acknowledged_sequence = _ACK.unpack(body)
        if accepted not in {0, 1}:
            raise OperatorProtocolError("selection-ack accepted flag is invalid")
        try:
            reason = SelectionAckReason(reason_value)
            return SelectionAck(
                command_id=str(UUID(bytes=command_id)),
                accepted=bool(accepted),
                reason=reason,
                acknowledged_sequence=acknowledged_sequence,
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid selection acknowledgement: {exc}") from exc

    def _decode_track(self, body: bytes, *, sequence: int, sent_at_s: float) -> TrackStatusMessage:
        if len(body) != _TRACK.size:
            raise OperatorProtocolError("track-status body has an invalid size")
        (
            status_id,
            command_id,
            state_value,
            stream_hash,
            width,
            height,
            rotation,
            target_present,
            target_hash,
            bbox_present,
            x1,
            y1,
            x2,
            y2,
            label_length,
            label_bytes,
            confidence,
            quality,
            frame_hash,
            frame_age_ms,
            bearing,
            distance,
        ) = _TRACK.unpack(body)
        state = {
            1: TrackingState.INITIALIZING,
            2: TrackingState.TRACKING,
            3: TrackingState.LOST,
            4: TrackingState.CANCELLED,
            5: TrackingState.REJECTED,
        }.get(state_value)
        if state is None:
            raise OperatorProtocolError("track-status state is invalid")
        if target_present not in {0, 1} or bbox_present not in {0, 1}:
            raise OperatorProtocolError("track-status presence flag is invalid")
        if label_length > len(label_bytes):
            raise OperatorProtocolError("track-status label length is invalid")
        try:
            label = label_bytes[:label_length].decode("utf-8") or None
        except UnicodeDecodeError as exc:
            raise OperatorProtocolError("track-status label is not valid UTF-8") from exc
        geometry = self._wire_geometry(stream_hash, width, height, rotation)
        target_id = _hashed_identifier(target_hash) if target_present else None
        bbox = _decode_bbox((x1, y1, x2, y2)) if bbox_present else None
        try:
            return TrackStatusMessage(
                status_id=_hashed_identifier(status_id),
                selection_command_id=str(UUID(bytes=command_id)),
                sequence=sequence,
                geometry=geometry,
                state=state,
                target_id=target_id,
                bbox=bbox,
                label=label,
                confidence=_decode_ratio(confidence),
                tracking_quality=_decode_ratio(quality),
                source_frame_id=_hashed_identifier(frame_hash),
                source_captured_at_s=sent_at_s - frame_age_ms / 1000.0,
                produced_at_s=sent_at_s,
                relative_bearing_deg=_decode_bearing(bearing),
                estimated_range_m=_decode_distance(distance),
            )
        except ValueError as exc:
            raise OperatorProtocolError(f"invalid track-status content: {exc}") from exc

    def _require_registered_stream(self, stream_id: str) -> None:
        registered = self._geometries.get(_hash32(stream_id))
        if registered is None or registered.stream_id != stream_id:
            raise ValueError("video stream is not registered with the operator-link codec")

    def _wire_geometry(
        self, stream_hash: int, width: int, height: int, rotation_code: int
    ) -> VideoGeometry:
        registered = self._geometries.get(stream_hash)
        if registered is None:
            raise OperatorProtocolError("operator-link stream is not registered")
        try:
            rotation = {0: 0, 1: 90, 2: 180, 3: 270}[rotation_code]
            return VideoGeometry(registered.stream_id, width, height, rotation)
        except (KeyError, ValueError) as exc:
            raise OperatorProtocolError("operator-link video geometry is invalid") from exc


def _uuid_bytes(value: str, *, field_name: str) -> bytes:
    try:
        return UUID(value).bytes
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID for wire transport") from exc


def _hash32(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:4], "big")


def _hash64(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def _hashed_identifier(value: int) -> str:
    return f"hash64:{value:016x}"


def _rotation_code(rotation_degrees: int) -> int:
    return {0: 0, 90: 1, 180: 2, 270: 3}[rotation_degrees]


def _timestamp_ms(value: float) -> int:
    if not isfinite(value) or value < 0.0:
        raise ValueError("wire timestamp must be finite and non-negative")
    return _bounded_uint(round(value * 1000.0), bits=64, field_name="wire timestamp")


def _bounded_uint(value: int, *, bits: int, field_name: str) -> int:
    if not 0 <= value <= (1 << bits) - 1:
        raise ValueError(f"{field_name} does not fit in uint{bits}")
    return value


def _encode_bbox(bbox: BoundingBox) -> tuple[int, int, int, int]:
    return tuple(round(value * 65535.0) for value in (bbox.x1, bbox.y1, bbox.x2, bbox.y2))


def _decode_bbox(values: tuple[int, int, int, int]) -> BoundingBox:
    try:
        return BoundingBox(*(value / 65535.0 for value in values))
    except ValueError as exc:
        raise OperatorProtocolError(f"invalid wire bounding box: {exc}") from exc


def _encode_ratio(value: float | None) -> int:
    return 255 if value is None else round(value * 254.0)


def _decode_ratio(value: int) -> float | None:
    return None if value == 255 else value / 254.0


def _encode_bearing(value: float | None) -> int:
    return -32768 if value is None else round(value * 100.0)


def _decode_bearing(value: int) -> float | None:
    return None if value == -32768 else value / 100.0


def _encode_distance(value: float | None) -> int:
    if value is None:
        return 0xFFFF
    return _bounded_uint(round(value * 10.0), bits=16, field_name="estimated range decimetres")


def _decode_distance(value: int) -> float | None:
    return None if value == 0xFFFF else value / 10.0


__all__ = [
    "AUTHENTICATION_TAG_BYTES",
    "DecodedOperatorPacket",
    "MAX_TUNNEL_PAYLOAD_BYTES",
    "OPERATOR_TUNNEL_PAYLOAD_TYPE_EXPERIMENTAL",
    "OperatorProtocolError",
    "OperatorTunnelCodec",
    "SelectionAck",
    "SelectionAckReason",
    "WireMessageType",
]
