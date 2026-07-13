from __future__ import annotations

import socket
import time
from collections import deque
from dataclasses import dataclass
from queue import Empty, SimpleQueue
from threading import Event, Thread

from .operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDecisionAcceptance,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    MissionStatusMessage,
    SafetyStatusMessage,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackStatusMessage,
)
from .operator_mavlink import OperatorMavlinkTunnelAdapter
from .operator_protocol import (
    AuthorizationDecisionAck,
    AuthorizationDecisionAckReason,
    OperatorProtocolError,
    SelectionAck,
    WireMessageType,
)
from .operator_transport import (
    AuthorizationDecisionCommandServer,
    AuthorizationDecisionDeliveryTimeout,
    AuthorizationDecisionRetryClient,
    SelectionCommandServer,
    SelectionDeliveryTimeout,
    SelectionRetryClient,
    ServerAuthorizationDecisionResult,
    ServerSelectionResult,
)

MAX_OPERATOR_UDP_DATAGRAM_BYTES = 512


@dataclass(frozen=True, slots=True)
class UdpSelectionReceipt:
    acknowledgement: SelectionAck
    attempts: int
    elapsed_s: float
    remote: tuple[str, int]


@dataclass(frozen=True, slots=True)
class UdpAuthorizationDecisionReceipt:
    acknowledgement: AuthorizationDecisionAck
    attempts: int
    elapsed_s: float
    remote: tuple[str, int]


class UdpOperatorSelectionServer:
    """Jetson UDP endpoint for selection/status and challenge-bound decisions, never actuators."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        mavlink: OperatorMavlinkTunnelAdapter,
        guard: SelectionCommandGuard,
        authorization_guard: AuthorizationDecisionCommandGuard | None = None,
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
        self.authorization_guard = authorization_guard or AuthorizationDecisionCommandGuard()
        self.authorization_server = AuthorizationDecisionCommandServer(
            mavlink.codec,
            self.authorization_guard,
        )
        self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._channel.settimeout(receive_timeout_s)
        self._channel.bind((bind_host, port))
        self._acknowledgement_sequence = 0
        self._authorization_peer: tuple[str, int] | None = None
        self._accepted_commands: SimpleQueue[tuple[TargetSelectionCommand, tuple[str, int]]] = (
            SimpleQueue()
        )
        self._accepted_authorization_decisions: SimpleQueue[
            tuple[AuthorizationDecisionCommand, tuple[str, int]]
        ] = SimpleQueue()
        self._background_errors: SimpleQueue[Exception] = SimpleQueue()
        self._stop_event = Event()
        self._thread: Thread | None = None

    @property
    def bound_address(self) -> tuple[str, int]:
        host, port = self._channel.getsockname()[:2]
        return str(host), int(port)

    def serve_once(
        self,
    ) -> tuple[
        ServerSelectionResult | ServerAuthorizationDecisionResult,
        tuple[str, int],
    ]:
        frame, raw_peer = self._channel.recvfrom(MAX_OPERATOR_UDP_DATAGRAM_BYTES + 1)
        if len(frame) > MAX_OPERATOR_UDP_DATAGRAM_BYTES:
            raise ValueError("operator UDP datagram exceeds the diagnostic limit")
        peer = str(raw_peer[0]), int(raw_peer[1])
        inner = self.mavlink.extract_authenticated_operator_payload(frame)
        packet = self.mavlink.codec.decode(inner)
        self._acknowledgement_sequence = (self._acknowledgement_sequence + 1) & 0xFFFFFFFF
        if packet.message_type is WireMessageType.TARGET_SELECTION:
            result = self.selection_server.handle_selection(
                inner,
                received_at_s=time.time(),
                acknowledgement_sequence=self._acknowledgement_sequence,
            )
        elif packet.message_type is WireMessageType.AUTHORIZATION_DECISION:
            if not isinstance(packet.message, AuthorizationDecisionCommand):
                raise OperatorProtocolError("decoded authorization decision has the wrong type")
            if self._authorization_peer is None or peer != self._authorization_peer:
                acceptance = AuthorizationDecisionAcceptance(
                    False,
                    ("authorization decision peer does not match the challenge recipient",),
                )
                acknowledgement_payload = self.mavlink.codec.encode_authorization_ack(
                    AuthorizationDecisionAck(
                        command_token=packet.message.command_token,
                        accepted=False,
                        reason=AuthorizationDecisionAckReason.INVALID,
                        acknowledged_sequence=packet.message.sequence,
                    ),
                    sequence=self._acknowledgement_sequence,
                    sent_at_s=time.time(),
                )
                result = ServerAuthorizationDecisionResult(
                    packet.message,
                    acceptance,
                    acknowledgement_payload,
                    False,
                )
            else:
                result = self.authorization_server.handle_decision(
                    inner,
                    received_at_s=time.time(),
                    acknowledgement_sequence=self._acknowledgement_sequence,
                )
        else:
            raise OperatorProtocolError(
                "Jetson operator endpoint accepts only selection and authorization commands"
            )
        acknowledgement = self.mavlink.wrap_authenticated_operator_payload(
            result.acknowledgement_payload
        )
        self._channel.sendto(acknowledgement, peer)
        if result.acceptance.allowed and not result.duplicate:
            if isinstance(result, ServerSelectionResult):
                self._accepted_commands.put((result.command, peer))
            else:
                self._accepted_authorization_decisions.put((result.command, peer))
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

    def poll_authorization_decision(
        self,
    ) -> tuple[AuthorizationDecisionCommand, tuple[str, int]] | None:
        try:
            return self._accepted_authorization_decisions.get_nowait()
        except Empty:
            return None

    def set_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage | None,
    ) -> None:
        self.authorization_guard.set_active_challenge(status)
        if status is None:
            self._authorization_peer = None

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

    def publish_mission_status(
        self,
        status: MissionStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_mission_status(status), peer)

    def publish_safety_status(
        self,
        status: SafetyStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_safety_status(status), peer)

    def publish_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self.authorization_guard.set_active_challenge(status)
        self._authorization_peer = peer
        self._channel.sendto(self.mavlink.encode_authorization_challenge(status), peer)

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
        self._last_mission_status_sequence: int | None = None
        self._last_safety_status_sequence: int | None = None
        self._last_authorization_challenge_sequence: int | None = None
        self._pending_messages: dict[WireMessageType, deque[object]] = {
            WireMessageType.TRACK_STATUS: deque(maxlen=1),
            WireMessageType.MISSION_STATUS: deque(maxlen=1),
            WireMessageType.SAFETY_STATUS: deque(maxlen=1),
            WireMessageType.AUTHORIZATION_CHALLENGE: deque(maxlen=1),
        }

    def deliver(self, command: TargetSelectionCommand) -> UdpSelectionReceipt:
        for pending in self._pending_messages.values():
            pending.clear()
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
            deadline_s = time.monotonic() + self.retry_interval_s
            while not delivery.completed:
                remaining_s = deadline_s - time.monotonic()
                if remaining_s <= 0.0:
                    break
                try:
                    inner, packet = self._receive_packet(timeout_s=remaining_s)
                except TimeoutError:
                    break
                if packet.message_type is WireMessageType.SELECTION_ACK:
                    delivery.handle_acknowledgement(inner)
                    break
                self._queue_status_packet(packet.message_type, packet.message)
        acknowledgement = delivery.acknowledgement
        if acknowledgement is None:  # pragma: no cover - loop invariant
            raise SelectionDeliveryTimeout("selection delivery ended without an acknowledgement")
        if acknowledgement.accepted:
            self._active_command_id = command.command_id
            self._last_status_sequence = None
            self._last_mission_status_sequence = None
            self._last_safety_status_sequence = None
            self._last_authorization_challenge_sequence = None
        return UdpSelectionReceipt(
            acknowledgement,
            delivery.attempts,
            time.monotonic() - started_s,
            self.remote,
        )

    def deliver_authorization_decision(
        self,
        command: AuthorizationDecisionCommand,
    ) -> UdpAuthorizationDecisionReceipt:
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        delivery = AuthorizationDecisionRetryClient(
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
            deadline_s = time.monotonic() + self.retry_interval_s
            while not delivery.completed:
                remaining_s = deadline_s - time.monotonic()
                if remaining_s <= 0.0:
                    break
                try:
                    inner, packet = self._receive_packet(timeout_s=remaining_s)
                except TimeoutError:
                    break
                if packet.message_type is WireMessageType.AUTHORIZATION_ACK:
                    delivery.handle_acknowledgement(inner)
                    break
                self._queue_status_packet(packet.message_type, packet.message)
        acknowledgement = delivery.acknowledgement
        if acknowledgement is None:  # pragma: no cover - loop invariant
            raise AuthorizationDecisionDeliveryTimeout(
                "authorization delivery ended without an acknowledgement"
            )
        return UdpAuthorizationDecisionReceipt(
            acknowledgement,
            delivery.attempts,
            time.monotonic() - started_s,
            self.remote,
        )

    def receive_authorization_challenge(
        self,
        *,
        timeout_s: float,
    ) -> AuthorizationChallengeStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("authorization challenge timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(
            WireMessageType.AUTHORIZATION_CHALLENGE,
            timeout_s=timeout_s,
        )
        if not isinstance(status, AuthorizationChallengeStatusMessage):
            raise OperatorProtocolError("decoded authorization challenge has the wrong type")
        if (
            self._last_authorization_challenge_sequence is not None
            and status.sequence <= self._last_authorization_challenge_sequence
        ):
            raise OperatorProtocolError("authorization challenge sequence is not newer")
        self._last_authorization_challenge_sequence = status.sequence
        return status

    def receive_track_status(self, *, timeout_s: float) -> TrackStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("tracking status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.TRACK_STATUS, timeout_s=timeout_s)
        if not isinstance(status, TrackStatusMessage):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded tracking status has the wrong type")
        if status.selection_command_id != self._active_command_id:
            raise OperatorProtocolError("tracking status selection command ID does not match")
        if self._last_status_sequence is not None and status.sequence <= self._last_status_sequence:
            raise OperatorProtocolError("tracking status sequence is not newer")
        self._last_status_sequence = status.sequence
        return status

    def receive_mission_status(self, *, timeout_s: float) -> MissionStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("mission status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.MISSION_STATUS, timeout_s=timeout_s)
        if not isinstance(status, MissionStatusMessage):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded mission status has the wrong type")
        if (
            self._last_mission_status_sequence is not None
            and status.sequence <= self._last_mission_status_sequence
        ):
            raise OperatorProtocolError("mission status sequence is not newer")
        self._last_mission_status_sequence = status.sequence
        return status

    def receive_safety_status(self, *, timeout_s: float) -> SafetyStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("safety status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.SAFETY_STATUS, timeout_s=timeout_s)
        if not isinstance(status, SafetyStatusMessage):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded safety status has the wrong type")
        if (
            self._last_safety_status_sequence is not None
            and status.sequence <= self._last_safety_status_sequence
        ):
            raise OperatorProtocolError("safety status sequence is not newer")
        self._last_safety_status_sequence = status.sequence
        return status

    def _receive_packet(self, *, timeout_s: float):
        self._channel.settimeout(timeout_s)
        frame = self._channel.recv(MAX_OPERATOR_UDP_DATAGRAM_BYTES + 1)
        if len(frame) > MAX_OPERATOR_UDP_DATAGRAM_BYTES:
            raise ValueError("operator UDP datagram exceeds the diagnostic limit")
        inner = self.mavlink.extract_authenticated_operator_payload(frame)
        return inner, self.mavlink.codec.decode(inner)

    def _receive_message(
        self,
        expected_type: WireMessageType,
        *,
        timeout_s: float,
    ) -> object:
        pending = self._pending_messages[expected_type]
        if pending:
            return pending.popleft()
        deadline_s = time.monotonic() + timeout_s
        while True:
            remaining_s = deadline_s - time.monotonic()
            if remaining_s <= 0.0:
                raise TimeoutError(f"operator UDP {expected_type.name.lower()} timed out")
            _inner, packet = self._receive_packet(timeout_s=remaining_s)
            if packet.message_type is expected_type:
                return packet.message
            self._queue_status_packet(packet.message_type, packet.message)

    def _queue_status_packet(self, message_type: WireMessageType, message: object) -> None:
        pending = self._pending_messages.get(message_type)
        if pending is not None:
            pending.append(message)

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
    "UdpAuthorizationDecisionReceipt",
    "UdpSelectionReceipt",
]
