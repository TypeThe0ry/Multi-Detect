"""Loopback-only QGC manual-control gateway for interactive SIM.

The acceptance router remains read-only.  This module is the separate,
explicitly SIM-scoped path used when a human flies the PX4 SITL vehicle from
QGC.  It accepts only ``MANUAL_CONTROL`` from the local QGC identity, decodes
the normalized joystick frame, and sends it through the same authority and
MAVLink adapter used by the AI executor.
"""

from __future__ import annotations

import argparse
import json
import math
import select
import socket
import struct
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..qgc_readonly_bridge import MavlinkFrame, MavlinkFrameDecoder
from .control import SimulatorControlAuthority
from .isolation import SimulationIsolationError
from .mavlink_control import (
    SimulatorMavlinkControlAdapter,
    SimulatorMavlinkControlConfig,
)

MSG_MANUAL_CONTROL = 69
QGC_SYSTEM_ID = 255
QGC_COMPONENT_ID = 190
PX4_SYSTEM_ID = 1
QGC_LOOPBACK_HOST = "127.0.0.1"
PROTECTED_GROUND_STATION_PORT = 14550


class SimulatorControlGatewayError(RuntimeError):
    """Raised when the interactive SIM control boundary is invalid."""


@dataclass(frozen=True, slots=True)
class SimulatorControlGatewayConfig:
    """Literal local endpoints for one disposable SIM job."""

    qgc_input_host: str = QGC_LOOPBACK_HOST
    qgc_input_port: int = 14650
    px4_output_endpoint: str = "udpout:127.0.0.1:14540"
    qgc_system_id: int = QGC_SYSTEM_ID
    qgc_component_id: int = QGC_COMPONENT_ID
    target_system: int = PX4_SYSTEM_ID
    maximum_datagram_bytes: int = 8192
    # QGC emits ``z`` as thrust*1000 (0..1000).  ``signed`` is available for
    # controllers that use the MAVLink-documented -1000..1000 representation.
    throttle_wire_mode: str = "qgc_legacy"

    def __post_init__(self) -> None:
        if not isinstance(self.qgc_input_host, str) or self.qgc_input_host != QGC_LOOPBACK_HOST:
            raise SimulationIsolationError("SIM control gateway must bind to 127.0.0.1")
        if isinstance(self.qgc_input_port, bool) or not isinstance(self.qgc_input_port, int):
            raise ValueError("qgc_input_port must be an integer")
        if not 1024 <= self.qgc_input_port <= 65535:
            raise SimulationIsolationError("SIM control gateway port must be in 1024..65535")
        if self.qgc_input_port == PROTECTED_GROUND_STATION_PORT:
            raise SimulationIsolationError("UDP 14550 is protected and cannot be used by SIM")
        for name, value in (
            ("qgc_system_id", self.qgc_system_id),
            ("qgc_component_id", self.qgc_component_id),
            ("target_system", self.target_system),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 255:
                raise ValueError(f"{name} must be in 1..255")
        if (
            isinstance(self.maximum_datagram_bytes, bool)
            or not isinstance(self.maximum_datagram_bytes, int)
            or not 64 <= self.maximum_datagram_bytes <= 65535
        ):
            raise ValueError("maximum_datagram_bytes must be in 64..65535")
        if self.throttle_wire_mode not in {"qgc_legacy", "signed"}:
            raise ValueError("throttle_wire_mode must be 'qgc_legacy' or 'signed'")
        output_port = _loopback_output_port(self.px4_output_endpoint)
        if output_port == self.qgc_input_port:
            raise SimulationIsolationError("QGC input and PX4 output ports must differ")


@dataclass(slots=True)
class SimulatorControlGatewaySummary:
    started_monotonic_s: float
    datagrams_received: int = 0
    non_loopback_datagrams_blocked: int = 0
    frames_received: int = 0
    manual_frames_accepted: int = 0
    rejected_frames: int = 0
    rejected_message_ids: Counter[int] = field(default_factory=Counter)
    rejected_reasons: Counter[str] = field(default_factory=Counter)
    stop_requested: bool = False
    shutdown_neutral_count: int = 0

    def as_dict(
        self,
        *,
        config: SimulatorControlGatewayConfig,
        adapter: SimulatorMavlinkControlAdapter,
        discarded_bytes: int,
    ) -> dict[str, object]:
        authority_evidence = adapter.evidence()
        return {
            "schema_version": 1,
            "event": "simulator_control_gateway_finished",
            "duration_seconds": round(max(0.0, time.monotonic() - self.started_monotonic_s), 3),
            "endpoints": {
                "qgc_input": f"{config.qgc_input_host}:{config.qgc_input_port}",
                "px4_output": config.px4_output_endpoint,
            },
            "qgc_identity": {
                "system_id": config.qgc_system_id,
                "component_id": config.qgc_component_id,
            },
            "target_system": config.target_system,
            "qgc_control_mode": "manual_only",
            "throttle_wire_mode": config.throttle_wire_mode,
            "manual_watchdog_s": authority_evidence["manual_watchdog_s"],
            "manual_watchdog_enabled": authority_evidence["manual_watchdog_s"] is not None,
            "datagrams_received": self.datagrams_received,
            "non_loopback_datagrams_blocked": self.non_loopback_datagrams_blocked,
            "frames_received": self.frames_received,
            "manual_frames_accepted": self.manual_frames_accepted,
            "rejected_frames": self.rejected_frames,
            "rejected_message_ids": dict(sorted(self.rejected_message_ids.items())),
            "rejected_reasons": dict(sorted(self.rejected_reasons.items())),
            "stop_requested": self.stop_requested,
            "shutdown_neutral_count": self.shutdown_neutral_count,
            "discarded_bytes": discarded_bytes,
            "literal_loopback_only": True,
            "transport": "MAVLink MANUAL_CONTROL",
            "authority": authority_evidence,
        }


class SimulatorControlGateway:
    """Decode QGC joystick frames and route them through the SIM authority."""

    def __init__(
        self,
        adapter: SimulatorMavlinkControlAdapter,
        *,
        config: SimulatorControlGatewayConfig | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        if not isinstance(adapter, SimulatorMavlinkControlAdapter):
            raise TypeError("adapter must be a SimulatorMavlinkControlAdapter")
        self.adapter = adapter
        self.config = config or SimulatorControlGatewayConfig(
            px4_output_endpoint=adapter.config.endpoint,
            throttle_wire_mode=adapter.config.throttle_wire_mode,
        )
        self.clock = clock
        self.decoder = MavlinkFrameDecoder(maximum_buffer_bytes=32768)
        self.summary = SimulatorControlGatewaySummary(started_monotonic_s=float(clock()))
        self._stopped = False
        # ``stop`` requests a graceful event-loop drain while the gateway is
        # running.  Keep the public ``process_datagram`` path fail-closed for
        # callers that race with shutdown; only the loop-owned drain window
        # may consume datagrams that were already queued by the OS.
        self._stop_requested = False
        self._draining = False
        # Only the event-loop thread is allowed to consume packets during the
        # short graceful-drain window.  A boolean alone would let a racing
        # external caller slip through while ``_draining`` is true.
        self._drain_thread_id: int | None = None
        self._accepting_input = True
        self._run_lock = threading.RLock()
        self._running = False
        self.ready_event = threading.Event()
        self.bound_endpoint: tuple[str, int] | None = None
        self._channel: socket.socket | None = None

    def process_datagram(
        self,
        datagram: bytes,
        *,
        source: tuple[str, int] = (QGC_LOOPBACK_HOST, 0),
        timestamp_s: float | None = None,
    ) -> tuple[Any, ...]:
        """Process one UDP datagram; return receipts for accepted frames."""
        self.summary.datagrams_received += 1
        if not self._accepting_input or (
            self._stop_requested
            and (not self._draining or threading.get_ident() != self._drain_thread_id)
        ):
            self._reject("gateway_not_accepting")
            return ()
        if not _is_loopback_source(source):
            self.summary.non_loopback_datagrams_blocked += 1
            self._reject("non_loopback_source")
            return ()
        if not isinstance(datagram, (bytes, bytearray)):
            raise TypeError("datagram must be bytes")
        if len(datagram) > self.config.maximum_datagram_bytes:
            self._reject("datagram_too_large")
            return ()
        receipts: list[Any] = []
        for frame in self.decoder.feed(bytes(datagram)):
            self.summary.frames_received += 1
            reason = self._manual_frame_rejection_reason(frame)
            if reason is not None:
                self._reject(reason, message_id=frame.message_id)
                continue
            pitch, roll, yaw, throttle = _decode_manual_control(
                frame.payload,
                version=frame.version,
                throttle_wire_mode=self.config.throttle_wire_mode,
            )
            receipt = self.adapter.submit_manual(
                roll=roll,
                pitch=pitch,
                yaw=yaw,
                throttle=throttle,
                timestamp_s=timestamp_s,
            )
            self.summary.manual_frames_accepted += 1
            receipts.append(receipt)
        return tuple(receipts)

    def run(self, *, duration_s: float = 0.0) -> dict[str, object]:
        """Serve QGC input until duration expiry or ``stop`` is called."""
        if (
            isinstance(duration_s, bool)
            or not isinstance(duration_s, (int, float))
            or not math.isfinite(duration_s)
        ):
            raise ValueError("duration_s must be a finite number")
        if duration_s < 0:
            raise ValueError("duration_s cannot be negative")
        with self._run_lock:
            if self._running:
                raise SimulatorControlGatewayError("SIM control gateway is already running")
            self._running = True
            self._accepting_input = True
            self._stopped = False
            self._stop_requested = False
            self._draining = False
            self._drain_thread_id = None
        try:
            channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            channel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except BaseException:
            self._accepting_input = False
            with self._run_lock:
                self._running = False
            raise
        with self._run_lock:
            self._channel = channel
        deadline = self.clock() + duration_s if duration_s > 0 else None
        self._stopped = False
        try:
            channel.bind((self.config.qgc_input_host, self.config.qgc_input_port))
            self.bound_endpoint = (self.config.qgc_input_host, self.config.qgc_input_port)
            self.ready_event.set()
            channel.setblocking(False)
            while True:
                # ``stop`` is graceful for an active run: consume datagrams
                # already queued before revoking input.  This prevents a
                # caller's send→stop race from dropping the final rejection
                # evidence (particularly important for oversize packets).
                if self._stopped:
                    self._drain_pending_datagrams(channel)
                    break
                if deadline is not None and self.clock() >= deadline:
                    break
                self.poll_watchdog()
                timeout = 0.25
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - self.clock()))
                readable, _, _ = select.select([channel], [], [], timeout)
                if not readable:
                    continue
                # Read one byte beyond the configured limit so an oversized
                # UDP packet is rejected instead of being silently truncated.
                datagram, source = channel.recvfrom(self.config.maximum_datagram_bytes + 1)
                self.process_datagram(datagram, source=source, timestamp_s=self.clock())
                if self._stopped:
                    self._drain_pending_datagrams(channel)
                    break
        finally:
            self._accepting_input = False
            self._stop_requested = True
            self._draining = False
            self._drain_thread_id = None
            self._shutdown_adapter()
            channel.close()
            self.bound_endpoint = None
            self.ready_event.clear()
            with self._run_lock:
                self._running = False
                self._channel = None
        return self.evidence()

    def poll_watchdog(self, *, now_s: float | None = None) -> Any:
        """Tick the shared AI watchdog from the gateway/event-loop owner."""
        return self.adapter.check_watchdog(now_s=self.clock() if now_s is None else now_s)

    def stop(self) -> None:
        self.summary.stop_requested = True
        with self._run_lock:
            running = self._running
            self._stop_requested = True
            self._stopped = True
            # Keep the loop's input gate open until its bounded drain finishes;
            # direct callers still fail closed through ``_stop_requested``.
            if not running:
                self._accepting_input = False
        self._shutdown_adapter()

    def _drain_pending_datagrams(self, channel: socket.socket) -> None:
        """Consume packets already queued before graceful shutdown.

        The socket is non-blocking and the loop owns this method, so the drain
        is bounded by the kernel receive queue and cannot wait for new input.
        ``process_datagram`` remains fail-closed for all external callers
        while this private window is active.
        """

        self._draining = True
        self._drain_thread_id = threading.get_ident()
        try:
            while True:
                try:
                    datagram, source = channel.recvfrom(self.config.maximum_datagram_bytes + 1)
                except (BlockingIOError, ConnectionResetError):
                    return
                self.process_datagram(
                    datagram,
                    source=source,
                    timestamp_s=self.clock(),
                )
        finally:
            self._draining = False
            self._drain_thread_id = None

    def _shutdown_adapter(self) -> None:
        if self.adapter.shutdown(timestamp_s=self.clock()) is not None:
            self.summary.shutdown_neutral_count += 1

    def evidence(self) -> dict[str, object]:
        return self.summary.as_dict(
            config=self.config,
            adapter=self.adapter,
            discarded_bytes=self.decoder.discarded_bytes,
        )

    def _manual_frame_rejection_reason(self, frame: MavlinkFrame) -> str | None:
        if frame.source_system_id != self.config.qgc_system_id:
            return "source_system_mismatch"
        if frame.source_component_id != self.config.qgc_component_id:
            return "source_component_mismatch"
        if frame.message_id != MSG_MANUAL_CONTROL:
            return "message_id_not_manual_control"
        if len(frame.payload) >= 11 and frame.payload[10] != self.config.target_system:
            return "manual_control_target_mismatch"
        try:
            _decode_manual_control(
                frame.payload,
                version=frame.version,
                throttle_wire_mode=self.config.throttle_wire_mode,
            )
        except ValueError as exc:
            return str(exc)
        return None

    def _is_accepted_manual_frame(self, frame: MavlinkFrame) -> bool:
        return self._manual_frame_rejection_reason(frame) is None

    def _reject(self, reason: str, *, message_id: int | None = None) -> None:
        self.summary.rejected_frames += 1
        self.summary.rejected_reasons[reason] += 1
        if message_id is not None:
            self.summary.rejected_message_ids[message_id] += 1


def _is_loopback_source(source: tuple[str, int]) -> bool:
    return (
        isinstance(source, tuple) and len(source) >= 1 and source[0] in {QGC_LOOPBACK_HOST, "::1"}
    )


def _loopback_output_port(endpoint: str) -> int:
    if not isinstance(endpoint, str) or not endpoint.startswith("udpout:"):
        raise SimulationIsolationError(
            "SIM control gateway output must be udpout:127.0.0.1:<port> or udpout:::1:<port>"
        )
    value = endpoint[len("udpout:") :]
    if value.startswith("127.0.0.1:"):
        port_text = value[len("127.0.0.1:") :]
    elif value.startswith("[::1]:"):
        port_text = value[len("[::1]:") :]
    elif value.startswith("::1:"):
        port_text = value[len("::1:") :]
    else:
        raise SimulationIsolationError(
            "SIM control gateway output must use loopback IPv4/IPv6 udpout"
        )
    try:
        port = int(port_text)
    except ValueError as exc:
        raise SimulationIsolationError("SIM control gateway output port is invalid") from exc
    if not 1024 <= port <= 65535:
        raise SimulationIsolationError("SIM control gateway output port must be in 1024..65535")
    return port


def _decode_manual_control(
    payload: bytes, *, version: int = 1, throttle_wire_mode: str = "signed"
) -> tuple[float, float, float, float]:
    """Decode MANUAL_CONTROL with strict v1/v2 framing and axis validation."""
    if version not in (1, 2):
        raise ValueError("unsupported_mavlink_version")
    if throttle_wire_mode not in {"qgc_legacy", "signed"}:
        raise ValueError("unsupported_throttle_wire_mode")
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 11:
        raise ValueError("manual_control_payload_truncated")
    if version == 1 and len(payload) != 11:
        raise ValueError("v1_manual_control_payload_length")
    if version == 2 and (len(payload) > 30 or 11 < len(payload) < 14):
        raise ValueError("v2_manual_control_payload_length")
    x, y, z, r = struct.unpack_from("<hhhh", payload, 0)
    if any(value == 32767 for value in (x, y, z, r)):
        raise ValueError("manual_control_axis_invalid_sentinel")
    if any(abs(value) > 1000 for value in (x, y, z, r)):
        raise ValueError("manual_control_axis_out_of_range")
    if version == 2 and len(payload) >= 14:
        enabled = payload[13]
        required = 14 + 2 * enabled.bit_length() if enabled else 14
        if len(payload) < required:
            raise ValueError("v2_manual_control_extension_truncated")
        for index in range(8):
            if enabled & (1 << index):
                value = struct.unpack_from("<h", payload, 14 + 2 * index)[0]
                if value == 32767 or abs(value) > 1000:
                    raise ValueError("manual_control_extension_out_of_range")
    if throttle_wire_mode == "qgc_legacy" and z >= 0:
        throttle = z / 1000.0
    else:
        throttle = (z + 1000) / 2000.0
    return x / 1000.0, y / 1000.0, r / 1000.0, throttle


def build_manual_control_frame(
    *,
    pitch: float,
    roll: float,
    yaw: float,
    throttle: float,
    source_system: int = QGC_SYSTEM_ID,
    source_component: int = QGC_COMPONENT_ID,
    target_system: int = PX4_SYSTEM_ID,
    version: int = 1,
    extensions: bytes = b"",
    sequence: int = 0,
    throttle_wire_mode: str = "qgc_legacy",
) -> bytes:
    """Build deterministic v1/v2 MANUAL_CONTROL fixture (CRC bytes opaque)."""
    if version not in (1, 2):
        raise ValueError("version must be 1 or 2")
    if throttle_wire_mode not in {"qgc_legacy", "signed"}:
        raise ValueError("throttle_wire_mode must be 'qgc_legacy' or 'signed'")
    axes = (
        round(float(pitch) * 1000),
        round(float(roll) * 1000),
        (
            round(float(throttle) * 1000)
            if throttle_wire_mode == "qgc_legacy"
            else round(float(throttle) * 2000 - 1000)
        ),
        round(float(yaw) * 1000),
    )
    if not all(-1000 <= value <= 1000 for value in axes):
        raise ValueError("axis outside MAVLink range")
    if version == 1 and extensions:
        raise ValueError("MAVLink v1 cannot carry extensions")
    payload = struct.pack("<hhhhHB", *axes, 0, target_system) + bytes(extensions)
    if (
        version == 2
        and extensions
        and (len(extensions) < 3 or len(extensions) > 19 or len(extensions) % 2 == 0)
    ):
        raise ValueError("extensions must contain 3+2*n bytes (up to 19)")
    if version == 1:
        return (
            bytes(
                [
                    0xFE,
                    len(payload),
                    sequence & 255,
                    source_system,
                    source_component,
                    MSG_MANUAL_CONTROL,
                ]
            )
            + payload
            + b"\x00\x00"
        )
    return (
        bytes(
            [
                0xFD,
                len(payload),
                0,
                0,
                sequence & 255,
                source_system,
                source_component,
                MSG_MANUAL_CONTROL,
                0,
                0,
            ]
        )
        + payload
        + b"\x00\x00"
    )


_frame_v1_manual_control = build_manual_control_frame


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the isolated QGC-to-PX4 SIM control gateway")
    parser.add_argument("--qgc-input-host", default=QGC_LOOPBACK_HOST)
    parser.add_argument("--qgc-input-port", type=int, default=14650)
    parser.add_argument("--px4-output-endpoint", default="udpout:127.0.0.1:14540")
    parser.add_argument("--duration-seconds", type=float, default=0.0)
    parser.add_argument("--enable-ai", action="store_true")
    parser.add_argument(
        "--throttle-wire-mode",
        choices=("qgc_legacy", "signed"),
        default="qgc_legacy",
        help="MANUAL_CONTROL thrust encoding used by the QGC input path",
    )
    parser.add_argument(
        "--manual-watchdog-seconds",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help="fail-safe when QGC MANUAL_CONTROL stops (default: 0.5)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        authority = SimulatorControlAuthority()
        if args.enable_ai:
            authority.enable_ai()
            authority.release_manual_control()
        config = SimulatorMavlinkControlConfig(
            endpoint=args.px4_output_endpoint,
            throttle_wire_mode=args.throttle_wire_mode,
            manual_watchdog_s=args.manual_watchdog_seconds,
        )
        adapter = SimulatorMavlinkControlAdapter(authority, config=config)
        gateway = SimulatorControlGateway(
            adapter,
            config=SimulatorControlGatewayConfig(
                qgc_input_host=args.qgc_input_host,
                qgc_input_port=args.qgc_input_port,
                px4_output_endpoint=args.px4_output_endpoint,
                throttle_wire_mode=args.throttle_wire_mode,
            ),
        )
        print(json.dumps(gateway.run(duration_s=args.duration_seconds), sort_keys=True))
        return 0
    except (OSError, SimulationIsolationError, SimulatorControlGatewayError, ValueError) as exc:
        print(json.dumps({"event": "simulator_control_gateway_failed", "error": str(exc)}))
        return 2


__all__ = [
    "MSG_MANUAL_CONTROL",
    "PX4_SYSTEM_ID",
    "QGC_COMPONENT_ID",
    "QGC_SYSTEM_ID",
    "SimulatorControlGateway",
    "SimulatorControlGatewayConfig",
    "SimulatorControlGatewayError",
    "SimulatorControlGatewaySummary",
    "build_manual_control_frame",
    "_decode_manual_control",
    "_frame_v1_manual_control",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
