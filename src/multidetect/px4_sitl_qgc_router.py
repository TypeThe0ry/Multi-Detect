from __future__ import annotations

import argparse
import json
import select
import socket
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from multidetect.qgc_readonly_bridge import (
    MSG_COMMAND_LONG,
    MSG_FILE_TRANSFER_PROTOCOL,
    MSG_HEARTBEAT,
    MSG_SYSTEM_TIME,
    MavlinkFrame,
    MavlinkFrameDecoder,
    qgc_frame_is_read_only,
)

MSG_TUNNEL: Final = 385
OPERATOR_TUNNEL_PAYLOAD_TYPE: Final = 42000
QGC_SYSTEM_ID: Final = 255
QGC_COMPONENT_ID: Final = 190
PX4_SYSTEM_ID: Final = 1
PX4_AUTOPILOT_COMPONENT_ID: Final = 1
JETSON_COMPONENT_ID: Final = 191
PROTECTED_GROUND_STATION_PORT: Final = 14550


class SitlQgcRouterError(RuntimeError):
    """Raised when the software-only QGC/PX4 routing boundary is invalid."""


class QgcFrameDisposition(str, Enum):
    READ_ONLY_TO_PX4 = "read_only_to_px4"
    OPERATOR_TUNNEL_LOCAL_ONLY = "operator_tunnel_local_only"
    BLOCKED_HOUSEKEEPING = "blocked_housekeeping"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TunnelHeader:
    payload_type: int
    target_system_id: int
    target_component_id: int
    payload_length: int


def tunnel_header(frame: MavlinkFrame) -> TunnelHeader | None:
    if frame.message_id != MSG_TUNNEL or len(frame.payload) < 5:
        return None
    return TunnelHeader(
        payload_type=int.from_bytes(frame.payload[0:2], "little"),
        target_system_id=frame.payload[2],
        target_component_id=frame.payload[3],
        payload_length=frame.payload[4],
    )


def classify_qgc_frame(frame: MavlinkFrame) -> QgcFrameDisposition:
    if frame.source_system_id != QGC_SYSTEM_ID or frame.source_component_id != QGC_COMPONENT_ID:
        return QgcFrameDisposition.BLOCKED
    if frame.message_id == MSG_SYSTEM_TIME:
        # QGC periodically offers wall-clock synchronization. Keep the bridge
        # strictly read-only by recording but never forwarding it.
        return QgcFrameDisposition.BLOCKED_HOUSEKEEPING
    header = tunnel_header(frame)
    if header is not None:
        if (
            header.payload_type == OPERATOR_TUNNEL_PAYLOAD_TYPE
            and header.target_system_id == PX4_SYSTEM_ID
            and header.target_component_id == JETSON_COMPONENT_ID
            and 0 < header.payload_length <= 128
        ):
            return QgcFrameDisposition.OPERATOR_TUNNEL_LOCAL_ONLY
        return QgcFrameDisposition.BLOCKED
    if frame.message_id == MSG_FILE_TRANSFER_PROTOCOL:
        if frame.file_transfer_protocol_target != (
            PX4_SYSTEM_ID,
            PX4_AUTOPILOT_COMPONENT_ID,
        ):
            return QgcFrameDisposition.BLOCKED
    if qgc_frame_is_read_only(frame):
        return QgcFrameDisposition.READ_ONLY_TO_PX4
    return QgcFrameDisposition.BLOCKED


@dataclass(slots=True)
class RouterSummary:
    started_monotonic_s: float
    px4_frames_forwarded: int = 0
    px4_autopilot_heartbeats_forwarded: int = 0
    px4_unexpected_system_frames_blocked: int = 0
    qgc_frames_received: int = 0
    qgc_read_only_frames_forwarded: int = 0
    qgc_operator_tunnel_frames_local_only: int = 0
    qgc_housekeeping_frames_blocked: int = 0
    qgc_forbidden_frames_blocked: int = 0
    udp_connection_resets: int = 0
    qgc_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_forwarded_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_blocked_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_blocked_housekeeping_message_ids: Counter[int] = field(default_factory=Counter)
    qgc_blocked_command_ids: Counter[int] = field(default_factory=Counter)
    qgc_forwarded_ftp_opcodes: Counter[int] = field(default_factory=Counter)
    qgc_blocked_ftp_opcodes: Counter[int] = field(default_factory=Counter)

    def as_dict(
        self,
        *,
        qgc_port: int,
        router_port: int,
        sitl_telemetry_port: int,
        sitl_input_port: int,
        px4_discarded_bytes: int,
        qgc_discarded_bytes: int,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "event": "px4_sitl_qgc_readonly_router_finished",
            "duration_seconds": round(max(0.0, time.monotonic() - self.started_monotonic_s), 3),
            "endpoints": {
                "qgc": f"127.0.0.1:{qgc_port}",
                "router_return": f"127.0.0.1:{router_port}",
                "sitl_telemetry_input": f"127.0.0.1:{sitl_telemetry_port}",
                "sitl_gcs_input": f"127.0.0.1:{sitl_input_port}",
            },
            "px4_frames_forwarded": self.px4_frames_forwarded,
            "px4_autopilot_heartbeats_forwarded": (self.px4_autopilot_heartbeats_forwarded),
            "px4_unexpected_system_frames_blocked": (self.px4_unexpected_system_frames_blocked),
            "qgc_frames_received": self.qgc_frames_received,
            "qgc_read_only_frames_forwarded": self.qgc_read_only_frames_forwarded,
            "qgc_operator_tunnel_frames_local_only": (self.qgc_operator_tunnel_frames_local_only),
            "qgc_housekeeping_frames_blocked": self.qgc_housekeeping_frames_blocked,
            "qgc_forbidden_frames_blocked": self.qgc_forbidden_frames_blocked,
            "qgc_message_ids": dict(sorted(self.qgc_message_ids.items())),
            "qgc_forwarded_message_ids": dict(sorted(self.qgc_forwarded_message_ids.items())),
            "qgc_blocked_message_ids": dict(sorted(self.qgc_blocked_message_ids.items())),
            "qgc_blocked_housekeeping_message_ids": dict(
                sorted(self.qgc_blocked_housekeeping_message_ids.items())
            ),
            "qgc_blocked_command_ids": dict(sorted(self.qgc_blocked_command_ids.items())),
            "qgc_forwarded_ftp_opcodes": dict(sorted(self.qgc_forwarded_ftp_opcodes.items())),
            "qgc_blocked_ftp_opcodes": dict(sorted(self.qgc_blocked_ftp_opcodes.items())),
            "udp_connection_resets": self.udp_connection_resets,
            "px4_discarded_bytes": px4_discarded_bytes,
            "qgc_discarded_bytes": qgc_discarded_bytes,
            "policy": "read_only_allowlist_plus_local_operator_tunnel",
            "operator_tunnel_forwarded_to_px4": False,
            "parameter_writes_allowed": False,
            "flight_commands_allowed": False,
            "mission_writes_allowed": False,
            "file_mutating_ftp_opcodes_forwarded": 0,
            "system_time_frames_forwarded": 0,
            "diagnostic_prearm_check_only": True,
            "actuator_commands_allowed": False,
            "payload_commands_allowed": False,
            "software_only": True,
            "real_v6x_contacted": False,
        }


def _recv_datagram(
    channel: socket.socket,
) -> tuple[bytes, tuple[str, int]] | None:
    try:
        return channel.recvfrom(8192)
    except ConnectionResetError:
        return None


def _validated_ports(ports: tuple[int, ...]) -> None:
    if any(port < 1024 or port > 65535 for port in ports):
        raise SitlQgcRouterError("all UDP ports must be in 1024..65535")
    if len(set(ports)) != len(ports):
        raise SitlQgcRouterError("all QGC/SITL router ports must be distinct")
    if PROTECTED_GROUND_STATION_PORT in ports:
        raise SitlQgcRouterError("UDP 14550 is protected and cannot be used by SITL HIL")


def run_router(
    *,
    qgc_port: int,
    router_port: int,
    sitl_telemetry_port: int,
    sitl_input_port: int,
    duration_seconds: float,
) -> dict[str, object]:
    ports = (qgc_port, router_port, sitl_telemetry_port, sitl_input_port)
    _validated_ports(ports)
    if not 1.0 <= duration_seconds <= 120.0:
        raise SitlQgcRouterError("duration must be in 1..120 seconds")

    summary = RouterSummary(started_monotonic_s=time.monotonic())
    px4_decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
    qgc_decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
    px4_channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    qgc_channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    px4_channel.bind(("127.0.0.1", sitl_telemetry_port))
    qgc_channel.bind(("127.0.0.1", router_port))
    px4_channel.setblocking(False)
    qgc_channel.setblocking(False)
    deadline = summary.started_monotonic_s + duration_seconds
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select(
                [px4_channel, qgc_channel],
                [],
                [],
                min(0.25, max(0.0, deadline - time.monotonic())),
            )
            if px4_channel in readable:
                received = _recv_datagram(px4_channel)
                if received is None:
                    summary.udp_connection_resets += 1
                else:
                    datagram, _source = received
                    for frame in px4_decoder.feed(datagram):
                        if frame.source_system_id != PX4_SYSTEM_ID:
                            summary.px4_unexpected_system_frames_blocked += 1
                            continue
                        qgc_channel.sendto(frame.encoded, ("127.0.0.1", qgc_port))
                        summary.px4_frames_forwarded += 1
                        if (
                            frame.message_id == MSG_HEARTBEAT
                            and frame.source_component_id == PX4_AUTOPILOT_COMPONENT_ID
                        ):
                            summary.px4_autopilot_heartbeats_forwarded += 1

            if qgc_channel in readable:
                received = _recv_datagram(qgc_channel)
                if received is None:
                    summary.udp_connection_resets += 1
                    continue
                datagram, source = received
                if source != ("127.0.0.1", qgc_port):
                    continue
                for frame in qgc_decoder.feed(datagram):
                    summary.qgc_frames_received += 1
                    summary.qgc_message_ids[frame.message_id] += 1
                    disposition = classify_qgc_frame(frame)
                    if disposition is QgcFrameDisposition.READ_ONLY_TO_PX4:
                        px4_channel.sendto(
                            frame.encoded,
                            ("127.0.0.1", sitl_input_port),
                        )
                        summary.qgc_read_only_frames_forwarded += 1
                        summary.qgc_forwarded_message_ids[frame.message_id] += 1
                        if frame.message_id == MSG_FILE_TRANSFER_PROTOCOL:
                            opcode = frame.file_transfer_protocol_opcode
                            summary.qgc_forwarded_ftp_opcodes[-1 if opcode is None else opcode] += 1
                        continue
                    if disposition is QgcFrameDisposition.OPERATOR_TUNNEL_LOCAL_ONLY:
                        summary.qgc_operator_tunnel_frames_local_only += 1
                        continue
                    if disposition is QgcFrameDisposition.BLOCKED_HOUSEKEEPING:
                        summary.qgc_housekeeping_frames_blocked += 1
                        summary.qgc_blocked_housekeeping_message_ids[frame.message_id] += 1
                        continue
                    summary.qgc_forbidden_frames_blocked += 1
                    summary.qgc_blocked_message_ids[frame.message_id] += 1
                    if frame.message_id == MSG_COMMAND_LONG:
                        command_id = frame.command_long_id
                        summary.qgc_blocked_command_ids[
                            -1 if command_id is None else command_id
                        ] += 1
                    elif frame.message_id == MSG_FILE_TRANSFER_PROTOCOL:
                        opcode = frame.file_transfer_protocol_opcode
                        summary.qgc_blocked_ftp_opcodes[-1 if opcode is None else opcode] += 1
    finally:
        qgc_channel.close()
        px4_channel.close()

    return summary.as_dict(
        qgc_port=qgc_port,
        router_port=router_port,
        sitl_telemetry_port=sitl_telemetry_port,
        sitl_input_port=sitl_input_port,
        px4_discarded_bytes=px4_decoder.discarded_bytes,
        qgc_discarded_bytes=qgc_decoder.discarded_bytes,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Route owned PX4 SITL telemetry to an isolated local QGC HIL link while "
            "forwarding only read-only QGC traffic back to SITL."
        )
    )
    parser.add_argument("--qgc-port", type=int, default=14669)
    parser.add_argument("--router-port", type=int, default=14667)
    parser.add_argument("--sitl-telemetry-port", type=int, default=14668)
    parser.add_argument("--sitl-input-port", type=int, default=18570)
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--acknowledge-owned-disposable-sitl", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_owned_disposable_sitl:
        print(
            json.dumps(
                {
                    "event": "px4_sitl_qgc_readonly_router_failed",
                    "error": "explicit owned-disposable-SITL acknowledgement is required",
                    "software_only": True,
                    "real_v6x_contacted": False,
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        result = run_router(
            qgc_port=args.qgc_port,
            router_port=args.router_port,
            sitl_telemetry_port=args.sitl_telemetry_port,
            sitl_input_port=args.sitl_input_port,
            duration_seconds=args.duration_seconds,
        )
    except (OSError, SitlQgcRouterError, ValueError) as error:
        print(
            json.dumps(
                {
                    "event": "px4_sitl_qgc_readonly_router_failed",
                    "error": str(error),
                    "software_only": True,
                    "real_v6x_contacted": False,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
