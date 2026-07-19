from __future__ import annotations

import argparse
import json
import select
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Final

MAVLINK_V1_MAGIC: Final = 0xFE
MAVLINK_V2_MAGIC: Final = 0xFD
MAVLINK_V2_SIGNED_FLAG: Final = 0x01

MSG_HEARTBEAT: Final = 0
MSG_SYSTEM_TIME: Final = 2
MSG_PING: Final = 4
MSG_PARAM_REQUEST_READ: Final = 20
MSG_PARAM_REQUEST_LIST: Final = 21
MSG_PARAM_SET: Final = 23
MSG_MISSION_REQUEST: Final = 40
MSG_MISSION_REQUEST_LIST: Final = 43
MSG_MISSION_ACK: Final = 47
MSG_MISSION_REQUEST_INT: Final = 51
MSG_REQUEST_DATA_STREAM: Final = 66
MSG_COMMAND_LONG: Final = 76
MSG_FILE_TRANSFER_PROTOCOL: Final = 110
MSG_TIMESYNC: Final = 111
MSG_AUTOPILOT_VERSION_REQUEST: Final = 183
MSG_PARAM_EXT_REQUEST_READ: Final = 320
MSG_PARAM_EXT_REQUEST_LIST: Final = 321

MAV_CMD_SET_MESSAGE_INTERVAL: Final = 511
MAV_CMD_RUN_PREARM_CHECKS: Final = 401
MAV_CMD_REQUEST_MESSAGE: Final = 512
MAV_CMD_REQUEST_PROTOCOL_VERSION: Final = 519
MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES: Final = 520

READ_ONLY_QGC_MESSAGE_IDS: Final = frozenset(
    {
        MSG_HEARTBEAT,
        MSG_PING,
        MSG_PARAM_REQUEST_READ,
        MSG_PARAM_REQUEST_LIST,
        MSG_MISSION_REQUEST,
        MSG_MISSION_REQUEST_LIST,
        MSG_MISSION_ACK,
        MSG_MISSION_REQUEST_INT,
        MSG_REQUEST_DATA_STREAM,
        MSG_TIMESYNC,
        MSG_AUTOPILOT_VERSION_REQUEST,
        MSG_PARAM_EXT_REQUEST_READ,
        MSG_PARAM_EXT_REQUEST_LIST,
    }
)
READ_ONLY_COMMAND_LONG_IDS: Final = frozenset(
    {
        MAV_CMD_RUN_PREARM_CHECKS,
        MAV_CMD_SET_MESSAGE_INTERVAL,
        MAV_CMD_REQUEST_MESSAGE,
        MAV_CMD_REQUEST_PROTOCOL_VERSION,
        MAV_CMD_REQUEST_AUTOPILOT_CAPABILITIES,
    }
)

# MAVLink FTP opcode values from common.xml/MAVLinkFTP.h. Session cleanup changes
# only transient protocol state; none of these operations can create, modify,
# rename, truncate, or remove an onboard file.
FTP_OPCODE_NONE: Final = 0
FTP_OPCODE_TERMINATE_SESSION: Final = 1
FTP_OPCODE_RESET_SESSIONS: Final = 2
FTP_OPCODE_LIST_DIRECTORY: Final = 3
FTP_OPCODE_OPEN_FILE_RO: Final = 4
FTP_OPCODE_READ_FILE: Final = 5
FTP_OPCODE_CALC_FILE_CRC32: Final = 14
FTP_OPCODE_BURST_READ_FILE: Final = 15
FTP_OPCODE_LIST_DIRECTORY_WITH_TIME: Final = 16
READ_ONLY_FTP_OPCODES: Final = frozenset(
    {
        FTP_OPCODE_NONE,
        FTP_OPCODE_TERMINATE_SESSION,
        FTP_OPCODE_RESET_SESSIONS,
        FTP_OPCODE_LIST_DIRECTORY,
        FTP_OPCODE_OPEN_FILE_RO,
        FTP_OPCODE_READ_FILE,
        FTP_OPCODE_CALC_FILE_CRC32,
        FTP_OPCODE_BURST_READ_FILE,
        FTP_OPCODE_LIST_DIRECTORY_WITH_TIME,
    }
)


@dataclass(frozen=True, slots=True)
class MavlinkFrame:
    encoded: bytes
    version: int
    message_id: int
    source_system_id: int
    source_component_id: int
    payload: bytes

    @property
    def command_long_id(self) -> int | None:
        if self.message_id != MSG_COMMAND_LONG or len(self.payload) < 30:
            return None
        return int.from_bytes(self.payload[28:30], "little")

    @property
    def file_transfer_protocol_target(self) -> tuple[int, int] | None:
        if self.message_id != MSG_FILE_TRANSFER_PROTOCOL or len(self.payload) < 7:
            return None
        return self.payload[1], self.payload[2]

    @property
    def file_transfer_protocol_opcode(self) -> int | None:
        if self.message_id != MSG_FILE_TRANSFER_PROTOCOL or len(self.payload) < 7:
            return None
        # FILE_TRANSFER_PROTOCOL payload starts with target network/system/component.
        # The embedded FTP header opcode is byte 3 of that inner payload.
        return self.payload[6]


class MavlinkFrameDecoder:
    """Split a MAVLink byte stream without interpreting or rewriting payloads."""

    def __init__(self, *, maximum_buffer_bytes: int = 4096) -> None:
        if maximum_buffer_bytes < 280:
            raise ValueError("maximum_buffer_bytes must hold one signed MAVLink 2 frame")
        self._buffer = bytearray()
        self.maximum_buffer_bytes = maximum_buffer_bytes
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> list[MavlinkFrame]:
        self._buffer.extend(data)
        frames: list[MavlinkFrame] = []
        while self._buffer:
            magic = self._buffer[0]
            if magic not in {MAVLINK_V1_MAGIC, MAVLINK_V2_MAGIC}:
                del self._buffer[0]
                self.discarded_bytes += 1
                continue
            minimum_header = 6 if magic == MAVLINK_V1_MAGIC else 10
            if len(self._buffer) < minimum_header:
                break
            payload_length = self._buffer[1]
            if magic == MAVLINK_V1_MAGIC:
                frame_length = payload_length + 8
                payload_offset = 6
                message_id = self._buffer[5]
                source_system_id = self._buffer[3]
                source_component_id = self._buffer[4]
                version = 1
            else:
                signature_length = 13 if self._buffer[2] & MAVLINK_V2_SIGNED_FLAG else 0
                frame_length = payload_length + 12 + signature_length
                payload_offset = 10
                message_id = int.from_bytes(self._buffer[7:10], "little")
                source_system_id = self._buffer[5]
                source_component_id = self._buffer[6]
                version = 2
            if len(self._buffer) < frame_length:
                break
            encoded = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            frames.append(
                MavlinkFrame(
                    encoded=encoded,
                    version=version,
                    message_id=message_id,
                    source_system_id=source_system_id,
                    source_component_id=source_component_id,
                    payload=encoded[payload_offset : payload_offset + payload_length],
                )
            )
        if len(self._buffer) > self.maximum_buffer_bytes:
            overflow = len(self._buffer) - self.maximum_buffer_bytes
            del self._buffer[:overflow]
            self.discarded_bytes += overflow
        return frames


def qgc_frame_is_read_only(frame: MavlinkFrame) -> bool:
    if frame.message_id in READ_ONLY_QGC_MESSAGE_IDS:
        return True
    if frame.message_id == MSG_FILE_TRANSFER_PROTOCOL:
        return (
            frame.file_transfer_protocol_target == (1, 1)
            and frame.file_transfer_protocol_opcode in READ_ONLY_FTP_OPCODES
        )
    if frame.message_id != MSG_COMMAND_LONG:
        return False
    return frame.command_long_id in READ_ONLY_COMMAND_LONG_IDS


@dataclass(slots=True)
class BridgeSummary:
    started_monotonic_s: float
    autopilot_frames_forwarded: int = 0
    qgc_frames_forwarded: int = 0
    qgc_frames_blocked: int = 0
    qgc_udp_connection_resets: int = 0
    tcp_reconnects: int = 0
    observer_frames_mirrored: int = 0
    qgc_message_ids: Counter[int] = field(default_factory=Counter)
    blocked_message_ids: Counter[int] = field(default_factory=Counter)
    blocked_command_ids: Counter[int] = field(default_factory=Counter)

    def as_dict(
        self,
        *,
        gr01_host: str,
        gr01_port: int,
        qgc_host: str,
        qgc_port: int,
        local_udp_port: int,
        observer_udp_host: str | None,
        observer_udp_port: int,
        autopilot_discarded_bytes: int,
        qgc_discarded_bytes: int,
    ) -> dict[str, object]:
        elapsed = max(0.0, time.monotonic() - self.started_monotonic_s)
        return {
            "event": "qgc_gr01_readonly_bridge_summary",
            "gr01": {"host": gr01_host, "port": gr01_port, "transport": "tcp"},
            "qgc": {
                "host": qgc_host,
                "port": qgc_port,
                "local_udp_port": local_udp_port,
            },
            "observer": {
                "enabled": observer_udp_port > 0,
                "host": observer_udp_host if observer_udp_port > 0 else None,
                "port": observer_udp_port if observer_udp_port > 0 else None,
                "frames_mirrored": self.observer_frames_mirrored,
            },
            "duration_seconds": round(elapsed, 3),
            "autopilot_frames_forwarded": self.autopilot_frames_forwarded,
            "qgc_frames_forwarded": self.qgc_frames_forwarded,
            "qgc_frames_blocked": self.qgc_frames_blocked,
            "qgc_udp_connection_resets": self.qgc_udp_connection_resets,
            "tcp_reconnects": self.tcp_reconnects,
            "qgc_message_ids": dict(sorted(self.qgc_message_ids.items())),
            "blocked_message_ids": dict(sorted(self.blocked_message_ids.items())),
            "blocked_command_ids": dict(sorted(self.blocked_command_ids.items())),
            "autopilot_discarded_bytes": autopilot_discarded_bytes,
            "qgc_discarded_bytes": qgc_discarded_bytes,
            "policy": "read_only_allowlist",
            "parameter_writes_allowed": False,
            "flight_commands_allowed": False,
            "mission_writes_allowed": False,
            "actuator_commands_allowed": False,
            "payload_commands_allowed": False,
        }


def run_bridge(
    *,
    gr01_host: str,
    gr01_port: int,
    qgc_host: str,
    qgc_port: int,
    local_udp_port: int,
    observer_udp_host: str = "127.0.0.1",
    observer_udp_port: int = 0,
    duration_seconds: float,
    connect_timeout_seconds: float,
) -> dict[str, object]:
    if duration_seconds < 0:
        raise ValueError("duration_seconds cannot be negative")
    if observer_udp_port < 0 or observer_udp_port > 65535:
        raise ValueError("observer_udp_port must be zero or a valid UDP port")
    if observer_udp_port > 0 and not observer_udp_host:
        raise ValueError("observer_udp_host is required when observer mirroring is enabled")
    summary = BridgeSummary(started_monotonic_s=time.monotonic())
    autopilot_decoder = MavlinkFrameDecoder()
    qgc_decoder = MavlinkFrameDecoder()
    udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_socket.bind(("127.0.0.1", local_udp_port))
    udp_socket.setblocking(False)
    tcp_socket = socket.create_connection(
        (gr01_host, gr01_port),
        timeout=connect_timeout_seconds,
    )
    tcp_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    deadline = summary.started_monotonic_s + duration_seconds if duration_seconds > 0 else None
    try:
        while deadline is None or time.monotonic() < deadline:
            readable, _, exceptional = select.select(
                [tcp_socket, udp_socket],
                [],
                [tcp_socket],
                0.25,
            )
            if exceptional:
                raise ConnectionError("GR01 TCP socket entered an exceptional state")
            if tcp_socket in readable:
                chunk = tcp_socket.recv(8192)
                if not chunk:
                    raise ConnectionError("GR01 TCP connection closed")
                for frame in autopilot_decoder.feed(chunk):
                    udp_socket.sendto(frame.encoded, (qgc_host, qgc_port))
                    summary.autopilot_frames_forwarded += 1
                    if observer_udp_port > 0:
                        udp_socket.sendto(
                            frame.encoded,
                            (observer_udp_host, observer_udp_port),
                        )
                        summary.observer_frames_mirrored += 1
            if udp_socket in readable:
                received = _recv_qgc_datagram(udp_socket)
                if received is None:
                    # Windows reports ICMP port-unreachable responses as
                    # WSAECONNRESET when QGC is not listening yet. The UDP
                    # bridge must stay alive so QGC can be started/restarted.
                    summary.qgc_udp_connection_resets += 1
                    continue
                datagram, source = received
                if source[0] != "127.0.0.1":
                    continue
                for frame in qgc_decoder.feed(datagram):
                    summary.qgc_message_ids[frame.message_id] += 1
                    if qgc_frame_is_read_only(frame):
                        tcp_socket.sendall(frame.encoded)
                        summary.qgc_frames_forwarded += 1
                        continue
                    summary.qgc_frames_blocked += 1
                    summary.blocked_message_ids[frame.message_id] += 1
                    if frame.message_id == MSG_COMMAND_LONG:
                        command_id = frame.command_long_id
                        summary.blocked_command_ids[-1 if command_id is None else command_id] += 1
    finally:
        tcp_socket.close()
        udp_socket.close()
    return summary.as_dict(
        gr01_host=gr01_host,
        gr01_port=gr01_port,
        qgc_host=qgc_host,
        qgc_port=qgc_port,
        local_udp_port=local_udp_port,
        observer_udp_host=observer_udp_host,
        observer_udp_port=observer_udp_port,
        autopilot_discarded_bytes=autopilot_decoder.discarded_bytes,
        qgc_discarded_bytes=qgc_decoder.discarded_bytes,
    )


def _recv_qgc_datagram(
    udp_socket: socket.socket,
) -> tuple[bytes, tuple[str, int]] | None:
    try:
        return udp_socket.recvfrom(8192)
    except ConnectionResetError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bridge GR01 TCP telemetry to local QGroundControl UDP while blocking all "
            "parameter writes, flight commands, mission writes, actuators and payload messages."
        )
    )
    parser.add_argument("--gr01-host", default="192.168.144.11")
    parser.add_argument("--gr01-port", type=int, default=5760)
    parser.add_argument("--qgc-host", default="127.0.0.1")
    parser.add_argument("--qgc-port", type=int, default=14550)
    parser.add_argument("--local-udp-port", type=int, default=14560)
    parser.add_argument(
        "--observer-udp-host",
        default="127.0.0.1",
        help="local-only host that receives a copy of autopilot telemetry",
    )
    parser.add_argument(
        "--observer-udp-port",
        type=int,
        default=0,
        help="zero disables the read-only telemetry mirror",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=0.0,
        help="zero runs until interrupted",
    )
    parser.add_argument("--connect-timeout-seconds", type=float, default=5.0)
    args = parser.parse_args()
    try:
        report = run_bridge(
            gr01_host=args.gr01_host,
            gr01_port=args.gr01_port,
            qgc_host=args.qgc_host,
            qgc_port=args.qgc_port,
            local_udp_port=args.local_udp_port,
            observer_udp_host=args.observer_udp_host,
            observer_udp_port=args.observer_udp_port,
            duration_seconds=args.duration_seconds,
            connect_timeout_seconds=args.connect_timeout_seconds,
        )
    except KeyboardInterrupt:
        return 130
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
