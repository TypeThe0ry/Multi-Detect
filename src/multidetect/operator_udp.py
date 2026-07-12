from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from threading import Event, Thread

from .operator_link import (
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackStatusMessage,
)
from .operator_mavlink import OperatorMavlinkTunnelAdapter
from .operator_protocol import OperatorProtocolError, SelectionAck, WireMessageType
from .operator_transport import (
    SelectionCommandServer,
    SelectionDeliveryTimeout,
    SelectionRetryClient,
    ServerSelectionResult,
)

MAX_OPERATOR_UDP_DATAGRAM_BYTES = 512


@dataclass(frozen=True, slots=True)
class UdpSelectionReceipt:
    acknowledgement: SelectionAck
    attempts: int
    elapsed_s: float
    remote: tuple[str, int]


class UdpOperatorSelectionServer:
    """Jetson UDP endpoint accepting selections and publishing tracking metadata only."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        mavlink: OperatorMavlinkTunnelAdapter,
        guard: SelectionCommandGuard,
        receive_timeout_s: float | None = None,
    ) -> None:
        if not bind_host.strip():
            raise ValueError("UDP bind host cannot be empty")
        if not 0 <= port <= 65_535:
            raise ValueError("UDP port must be in [0, 65535]")
        if receive_timeout_s is not None and receive_timeout_s <= 0.0:
            raise ValueError("UDP receive timeout must be positive")
        self.mavlink = mavlink
        self.selection_server = SelectionCommandServer(mavlink.codec, guard)
        self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._channel.settimeout(receive_timeout_s)
        self._channel.bind((bind_host, port))
        self._acknowledgement_sequence = 0
        self._accepted_commands: SimpleQueue[tuple[TargetSelectionCommand, tuple[str, int]]] = (
            SimpleQueue()
        )
        self._background_errors: SimpleQueue[Exception] = SimpleQueue()
        self._stop_event = Event()
        self._thread: Thread | None = None

    @property
    def bound_address(self) -> tuple[str, int]:
        host, port = self._channel.getsockname()[:2]
        return str(host), int(port)

    def serve_once(self) -> tuple[ServerSelectionResult, tuple[str, int]]:
        frame, raw_peer = self._channel.recvfrom(MAX_OPERATOR_UDP_DATAGRAM_BYTES + 1)
        if len(frame) > MAX_OPERATOR_UDP_DATAGRAM_BYTES:
            raise ValueError("operator UDP datagram exceeds the diagnostic limit")
        peer = str(raw_peer[0]), int(raw_peer[1])
        inner = self.mavlink.extract_authenticated_operator_payload(frame)
        self._acknowledgement_sequence = (self._acknowledgement_sequence + 1) & 0xFFFFFFFF
        result = self.selection_server.handle_selection(
            inner,
            received_at_s=time.time(),
            acknowledgement_sequence=self._acknowledgement_sequence,
        )
        acknowledgement = self.mavlink.wrap_authenticated_operator_payload(
            result.acknowledgement_payload
        )
        self._channel.sendto(acknowledgement, peer)
        if result.acceptance.allowed and not result.duplicate:
            self._accepted_commands.put((result.command, peer))
        return result, peer

    def start_background(self) -> None:
        if self._thread is not None:
            raise RuntimeError("operator UDP background loop is already running")
        self._channel.settimeout(0.1)
        self._stop_event.clear()
        self._thread = Thread(
            target=self._background_loop,
            name="operator-udp-selection-server",
            daemon=True,
        )
        self._thread.start()

    def poll_selection(self) -> tuple[TargetSelectionCommand, tuple[str, int]] | None:
        try:
            return self._accepted_commands.get_nowait()
        except Empty:
            return None

    def poll_error(self) -> Exception | None:
        try:
            return self._background_errors.get_nowait()
        except Empty:
            return None

    def publish_track_status(
        self,
        status: TrackStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_track_status(status), peer)

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.serve_once()
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._stop_event.is_set():
                    self._background_errors.put(exc)
                return
            except (RuntimeError, ValueError, TypeError) as exc:
                self._background_errors.put(exc)

    def close(self) -> None:
        self._stop_event.set()
        self._channel.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def __enter__(self) -> UdpOperatorSelectionServer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class UdpOperatorSelectionClient:
    """One-shot G20 sender retained for simple GR01 selection/ACK diagnostics."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        mavlink: OperatorMavlinkTunnelAdapter,
        retry_interval_s: float = 0.25,
        maximum_attempts: int = 3,
    ) -> None:
        self.remote = _remote_address(host, port)
        self.mavlink = mavlink
        self.retry_interval_s = retry_interval_s
        self.maximum_attempts = maximum_attempts

    def deliver(self, command: TargetSelectionCommand) -> UdpSelectionReceipt:
        with UdpOperatorSessionClient(
            host=self.remote[0],
            port=self.remote[1],
            mavlink=self.mavlink,
            retry_interval_s=self.retry_interval_s,
            maximum_attempts=self.maximum_attempts,
        ) as session:
            return session.deliver(command)


class UdpOperatorSessionClient:
    """Persistent G20 diagnostic session able to receive tracking status metadata."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        mavlink: OperatorMavlinkTunnelAdapter,
        retry_interval_s: float = 0.25,
        maximum_attempts: int = 3,
    ) -> None:
        self.remote = _remote_address(host, port)
        if retry_interval_s <= 0.0:
            raise ValueError("UDP retry interval must be positive")
        if maximum_attempts <= 0:
            raise ValueError("UDP maximum attempts must be positive")
        self.mavlink = mavlink
        self.retry_interval_s = retry_interval_s
        self.maximum_attempts = maximum_attempts
        self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._channel.connect(self.remote)
        self._active_command_id: str | None = None
        self._last_status_sequence: int | None = None

    def deliver(self, command: TargetSelectionCommand) -> UdpSelectionReceipt:
        delivery = SelectionRetryClient(
            self.mavlink.codec,
            command,
            retry_interval_s=self.retry_interval_s,
            maximum_attempts=self.maximum_attempts,
        )
        started_s = time.monotonic()
        self._channel.settimeout(self.retry_interval_s)
        while not delivery.completed:
            now_s = time.time()
            payload = delivery.poll(now_s=now_s)
            if payload is None:
                delay_s = max(0.0, delivery.next_attempt_at_s - now_s)
                time.sleep(min(delay_s, self.retry_interval_s))
                continue
            self._channel.send(self.mavlink.wrap_authenticated_operator_payload(payload))
            try:
                acknowledgement_frame = self._channel.recv(MAX_OPERATOR_UDP_DATAGRAM_BYTES + 1)
            except TimeoutError:
                continue
            if len(acknowledgement_frame) > MAX_OPERATOR_UDP_DATAGRAM_BYTES:
                raise ValueError("operator UDP acknowledgement exceeds the diagnostic limit")
            inner = self.mavlink.extract_authenticated_operator_payload(acknowledgement_frame)
            delivery.handle_acknowledgement(inner)
        acknowledgement = delivery.acknowledgement
        if acknowledgement is None:  # pragma: no cover - loop invariant
            raise SelectionDeliveryTimeout("selection delivery ended without an acknowledgement")
        if acknowledgement.accepted:
            self._active_command_id = command.command_id
            self._last_status_sequence = None
        return UdpSelectionReceipt(
            acknowledgement,
            delivery.attempts,
            time.monotonic() - started_s,
            self.remote,
        )

    def receive_track_status(self, *, timeout_s: float) -> TrackStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("tracking status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        self._channel.settimeout(timeout_s)
        frame = self._channel.recv(MAX_OPERATOR_UDP_DATAGRAM_BYTES + 1)
        if len(frame) > MAX_OPERATOR_UDP_DATAGRAM_BYTES:
            raise ValueError("operator UDP tracking status exceeds the diagnostic limit")
        packet = self.mavlink.decode_frame(frame)
        if packet.message_type is not WireMessageType.TRACK_STATUS:
            raise OperatorProtocolError("G20 session received a non-tracking message")
        status = packet.message
        if not isinstance(status, TrackStatusMessage):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded tracking status has the wrong type")
        if status.selection_command_id != self._active_command_id:
            raise OperatorProtocolError("tracking status selection command ID does not match")
        if self._last_status_sequence is not None and status.sequence <= self._last_status_sequence:
            raise OperatorProtocolError("tracking status sequence is not newer")
        self._last_status_sequence = status.sequence
        return status

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> UdpOperatorSessionClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _remote_address(host: str, port: int) -> tuple[str, int]:
    if not host.strip():
        raise ValueError("UDP destination host cannot be empty")
    if not 1 <= port <= 65_535:
        raise ValueError("UDP destination port must be in [1, 65535]")
    return host, port


__all__ = [
    "MAX_OPERATOR_UDP_DATAGRAM_BYTES",
    "UdpOperatorSelectionClient",
    "UdpOperatorSelectionServer",
    "UdpOperatorSessionClient",
    "UdpSelectionReceipt",
]
