"""Small MAVLink frame parser with optional dialect decoding for target routing."""

from __future__ import annotations

from dataclasses import dataclass

try:
    from pymavlink.dialects.v20 import common as _mavlink_common
except ImportError:  # The raw-frame fallback keeps the relay usable without the optional dialect.
    _mavlink_common = None

_mavlink_decoder = _mavlink_common.MAVLink(None) if _mavlink_common is not None else None

MAVLINK_V1_STX = 0xFE
MAVLINK_V2_STX = 0xFD


@dataclass(frozen=True, slots=True)
class MavlinkFrame:
    raw: bytes
    version: int
    system_id: int
    component_id: int
    message_id: int
    payload: bytes

    @property
    def is_heartbeat(self) -> bool:
        return self.message_id == 0

    @property
    def target_system(self) -> int | None:
        decoded = _decoded_target_system(self.raw)
        if decoded is not None:
            return decoded
        offset = _TARGET_SYSTEM_OFFSETS.get(self.message_id)
        if offset is None or len(self.payload) <= offset:
            return None
        return self.payload[offset]


_TARGET_SYSTEM_OFFSETS = {
    11: 4,   # SET_MODE
    20: 2,   # PARAM_REQUEST_READ
    21: 0,   # PARAM_REQUEST_LIST
    23: 4,   # PARAM_SET
    39: 32,  # MISSION_ITEM
    40: 2,   # MISSION_REQUEST
    41: 2,   # MISSION_SET_CURRENT
    43: 0,   # MISSION_REQUEST_LIST
    44: 2,   # MISSION_COUNT
    45: 0,   # MISSION_CLEAR_ALL
    47: 0,   # MISSION_ACK
    48: 12,  # SET_GPS_GLOBAL_ORIGIN
    49: 4,   # MISSION_REQUEST_PARTIAL_LIST
    51: 2,   # MISSION_REQUEST_INT
    52: 4,   # MISSION_WRITE_PARTIAL_LIST
    66: 2,   # REQUEST_DATA_STREAM
    69: 10,  # MANUAL_CONTROL
    70: 16,  # RC_CHANNELS_OVERRIDE
    73: 32,  # MISSION_ITEM_INT
    75: 30,  # COMMAND_INT
    76: 30,  # COMMAND_LONG
    82: 36,  # SET_ATTITUDE_TARGET
    84: 50,  # SET_POSITION_TARGET_LOCAL_NED
    86: 50,  # SET_POSITION_TARGET_GLOBAL_INT
    139: 37, # SET_ACTUATOR_CONTROL_TARGET
    282: 32, # GIMBAL_MANAGER_SET_ATTITUDE
    284: 30, # GIMBAL_DEVICE_SET_ATTITUDE
    287: 20, # GIMBAL_MANAGER_SET_PITCHYAW
    288: 20, # GIMBAL_MANAGER_SET_MANUAL_CONTROL
}


def _decoded_target_system(raw: bytes) -> int | None:
    if _mavlink_decoder is None:
        return None
    try:
        message = _mavlink_decoder.decode(bytearray(raw))
    except Exception:
        return None
    target = getattr(message, "target_system", None)
    return int(target) if target is not None else None


class MavlinkFrameDecoder:
    """Incrementally split MAVLink 1 and 2 byte streams into complete frames."""

    def __init__(self, *, max_buffer: int = 1_048_576) -> None:
        self.buffer = bytearray()
        self.max_buffer = max_buffer

    def feed(self, data: bytes) -> list[MavlinkFrame]:
        self.buffer.extend(data)
        if len(self.buffer) > self.max_buffer:
            # A stream without a valid marker should not retain unbounded data.
            del self.buffer[: len(self.buffer) - self.max_buffer]
        frames: list[MavlinkFrame] = []
        while self.buffer:
            marker = next((i for i, byte in enumerate(self.buffer) if byte in (MAVLINK_V1_STX, MAVLINK_V2_STX)), -1)
            if marker < 0:
                self.buffer.clear()
                break
            if marker:
                del self.buffer[:marker]
            stx = self.buffer[0]
            if stx == MAVLINK_V1_STX:
                if len(self.buffer) < 6:
                    break
                payload_len = self.buffer[1]
                frame_len = 8 + payload_len
                if len(self.buffer) < frame_len:
                    break
                raw = bytes(self.buffer[:frame_len])
                frames.append(MavlinkFrame(raw, 1, raw[3], raw[4], raw[5], raw[6:-2]))
            else:
                if len(self.buffer) < 10:
                    break
                payload_len = self.buffer[1]
                signed = bool(self.buffer[2] & 1)
                frame_len = 12 + payload_len + (13 if signed else 0)
                if len(self.buffer) < frame_len:
                    break
                raw = bytes(self.buffer[:frame_len])
                message_id = raw[7] | (raw[8] << 8) | (raw[9] << 16)
                frames.append(MavlinkFrame(raw, 2, raw[5], raw[6], message_id, raw[10 : 10 + payload_len]))
            del self.buffer[:frame_len]
        return frames


def parse_datagram(data: bytes) -> list[MavlinkFrame]:
    return MavlinkFrameDecoder().feed(data)


def route_frames(frames: list[MavlinkFrame], known_sysids: set[int]) -> dict[int, bytes]:
    """Group GCS frames by target, keeping mixed-target TCP batches isolated."""
    routed: dict[int, list[bytes]] = {}
    for frame in frames:
        target = frame.target_system
        targets = known_sysids if frame.is_heartbeat or target == 0 else ({target} if target in known_sysids else set())
        for sysid in targets:
            routed.setdefault(sysid, []).append(frame.raw)
    return {sysid: b"".join(packet_list) for sysid, packet_list in routed.items()}


def build_mavlink2_frame(
    message_id: int,
    payload: bytes,
    *,
    system_id: int = 255,
    component_id: int = 190,
    sequence: int = 0,
) -> bytes:
    """Build an unsigned MAVLink 2 test frame; checksum is intentionally opaque to the router."""
    if not 0 <= message_id < 1 << 24 or len(payload) > 255:
        raise ValueError("invalid MAVLink 2 message id or payload size")
    header = bytes((MAVLINK_V2_STX, len(payload), 0, 0, sequence & 0xFF,
                    system_id & 0xFF, component_id & 0xFF,
                    message_id & 0xFF, (message_id >> 8) & 0xFF, (message_id >> 16) & 0xFF))
    return header + payload + b"\x00\x00"


__all__ = ["MavlinkFrame", "MavlinkFrameDecoder", "build_mavlink2_frame", "parse_datagram", "route_frames"]
