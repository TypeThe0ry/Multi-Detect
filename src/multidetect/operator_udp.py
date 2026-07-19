from __future__ import annotations

import socket
import time
from collections import deque
from dataclasses import dataclass
from math import isfinite
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread

from .operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationAcceptance,
    ApproachConfirmationCommand,
    ApproachConfirmationCommandGuard,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecisionAcceptance,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationAcceptance,
    PayloadTargetConfirmationCommand,
    PayloadTargetConfirmationCommandGuard,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    SafetyStatusMessage,
    SceneContextStatusMessage,
    SelectionCommandGuard,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackStatusMessage,
)
from .operator_mavlink import OperatorMavlinkTunnelAdapter
from .operator_protocol import (
    ApproachConfirmationAck,
    ApproachConfirmationAckReason,
    AuthorizationDecisionAck,
    AuthorizationDecisionAckReason,
    OperatorProtocolError,
    PayloadTargetConfirmationAck,
    PayloadTargetConfirmationAckReason,
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


@dataclass(frozen=True, slots=True)
class UdpApproachConfirmationReceipt:
    acknowledgement: ApproachConfirmationAck
    attempts: int
    elapsed_s: float
    remote: tuple[str, int]


@dataclass(frozen=True, slots=True)
class UdpPayloadTargetConfirmationReceipt:
    acknowledgement: PayloadTargetConfirmationAck
    attempts: int
    elapsed_s: float
    remote: tuple[str, int]


@dataclass(frozen=True, slots=True)
class ServerApproachConfirmationResult:
    command: ApproachConfirmationCommand
    acceptance: ApproachConfirmationAcceptance
    acknowledgement_payload: bytes
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ServerPayloadTargetConfirmationResult:
    command: PayloadTargetConfirmationCommand
    acceptance: PayloadTargetConfirmationAcceptance
    acknowledgement_payload: bytes
    duplicate: bool


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
        approach_guard: ApproachConfirmationCommandGuard | None = None,
        payload_target_guard: PayloadTargetConfirmationCommandGuard | None = None,
        receive_timeout_s: float | None = None,
        metadata_peer_timeout_s: float = 3.0,
    ) -> None:
        if not bind_host.strip():
            raise ValueError("UDP bind host cannot be empty")
        if not 0 <= port <= 65_535:
            raise ValueError("UDP port must be in [0, 65535]")
        if receive_timeout_s is not None and receive_timeout_s <= 0.0:
            raise ValueError("UDP receive timeout must be positive")
        if not isfinite(metadata_peer_timeout_s) or metadata_peer_timeout_s <= 0.0:
            raise ValueError("metadata peer timeout must be finite and positive")
        self.mavlink = mavlink
        self.metadata_peer_timeout_s = metadata_peer_timeout_s
        self.selection_server = SelectionCommandServer(mavlink.codec, guard)
        self.authorization_guard = authorization_guard or AuthorizationDecisionCommandGuard()
        self.authorization_server = AuthorizationDecisionCommandServer(
            mavlink.codec,
            self.authorization_guard,
        )
        self.approach_guard = approach_guard or ApproachConfirmationCommandGuard()
        self.payload_target_guard = payload_target_guard or PayloadTargetConfirmationCommandGuard()
        self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._channel.settimeout(receive_timeout_s)
        self._channel.bind((bind_host, port))
        self._acknowledgement_sequence = 0
        self._authorization_peer: tuple[str, int] | None = None
        self._approach_peer: tuple[str, int] | None = None
        self._payload_target_peer: tuple[str, int] | None = None
        self._metadata_peer: tuple[str, int] | None = None
        self._metadata_peer_seen_at_s: float | None = None
        self._metadata_peer_lock = Lock()
        self._accepted_commands: SimpleQueue[tuple[TargetSelectionCommand, tuple[str, int]]] = (
            SimpleQueue()
        )
        self._accepted_authorization_decisions: SimpleQueue[
            tuple[AuthorizationDecisionCommand, tuple[str, int]]
        ] = SimpleQueue()
        self._accepted_approach_confirmations: SimpleQueue[
            tuple[ApproachConfirmationCommand, tuple[str, int]]
        ] = SimpleQueue()
        self._accepted_payload_target_confirmations: SimpleQueue[
            tuple[PayloadTargetConfirmationCommand, tuple[str, int]]
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
    ) -> (
        tuple[
            ServerSelectionResult
            | ServerAuthorizationDecisionResult
            | ServerApproachConfirmationResult
            | ServerPayloadTargetConfirmationResult,
            tuple[str, int],
        ]
        | None
    ):
        frame, raw_peer = self._channel.recvfrom(MAX_OPERATOR_UDP_DATAGRAM_BYTES + 1)
        if len(frame) > MAX_OPERATOR_UDP_DATAGRAM_BYTES:
            raise ValueError("operator UDP datagram exceeds the diagnostic limit")
        peer = str(raw_peer[0]), int(raw_peer[1])
        # QGroundControl emits a signed heartbeat on this direct link. Treat it
        # as authenticated peer discovery so Jetson can publish DET candidates
        # before the first manual selection command arrives.
        datagram = self.mavlink.decode_authenticated_datagram(
            frame,
            ignore_unrelated_message=True,
        )
        inner = datagram.operator_payload
        if inner is None:
            if datagram.is_heartbeat:
                self._register_metadata_peer(peer)
            return None
        # A valid signed+HMAC TUNNEL packet also refreshes the same peer lease.
        self._register_metadata_peer(peer)
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
        elif packet.message_type is WireMessageType.APPROACH_CONFIRMATION:
            if not isinstance(packet.message, ApproachConfirmationCommand):
                raise OperatorProtocolError("decoded approach confirmation has the wrong type")
            if self._approach_peer is None or peer != self._approach_peer:
                acceptance = ApproachConfirmationAcceptance(
                    False,
                    ("approach confirmation peer does not match the challenge recipient",),
                )
            else:
                acceptance = self.approach_guard.evaluate(
                    packet.message,
                    received_at_s=time.time(),
                )
            reason = _approach_ack_reason(acceptance)
            acknowledgement_payload = self.mavlink.codec.encode_approach_ack(
                ApproachConfirmationAck(
                    command_token=packet.message.command_token,
                    accepted=acceptance.allowed,
                    reason=reason,
                    acknowledged_sequence=packet.message.sequence,
                ),
                sequence=self._acknowledgement_sequence,
                sent_at_s=time.time(),
            )
            result = ServerApproachConfirmationResult(
                packet.message,
                acceptance,
                acknowledgement_payload,
                acceptance.duplicate,
            )
        elif packet.message_type is WireMessageType.PAYLOAD_TARGET_CONFIRMATION:
            if not isinstance(packet.message, PayloadTargetConfirmationCommand):
                raise OperatorProtocolError(
                    "decoded payload target confirmation has the wrong type"
                )
            if self._payload_target_peer is None or peer != self._payload_target_peer:
                acceptance = PayloadTargetConfirmationAcceptance(
                    False,
                    ("payload target confirmation peer does not match the challenge recipient",),
                )
            else:
                acceptance = self.payload_target_guard.evaluate(
                    packet.message,
                    received_at_s=time.time(),
                )
            reason = _payload_target_ack_reason(acceptance)
            acknowledgement_payload = self.mavlink.codec.encode_payload_target_ack(
                PayloadTargetConfirmationAck(
                    command_token=packet.message.command_token,
                    accepted=acceptance.allowed,
                    reason=reason,
                    acknowledged_sequence=packet.message.sequence,
                ),
                sequence=self._acknowledgement_sequence,
                sent_at_s=time.time(),
            )
            result = ServerPayloadTargetConfirmationResult(
                packet.message,
                acceptance,
                acknowledgement_payload,
                acceptance.duplicate,
            )
        else:
            raise OperatorProtocolError(
                "Jetson operator endpoint accepts only selection, authorization "
                "and bounded slide-confirmation commands"
            )
        acknowledgement = self.mavlink.wrap_authenticated_operator_payload(
            result.acknowledgement_payload
        )
        self._channel.sendto(acknowledgement, peer)
        if result.acceptance.allowed and not result.duplicate:
            if isinstance(result, ServerSelectionResult):
                self._accepted_commands.put((result.command, peer))
            elif isinstance(result, ServerAuthorizationDecisionResult):
                self._accepted_authorization_decisions.put((result.command, peer))
            elif isinstance(result, ServerApproachConfirmationResult):
                self._accepted_approach_confirmations.put((result.command, peer))
            else:
                self._accepted_payload_target_confirmations.put((result.command, peer))
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

    def active_metadata_peer(self) -> tuple[str, int] | None:
        """Return the freshest authenticated QGC peer while its heartbeat lease is live."""

        with self._metadata_peer_lock:
            if self._metadata_peer is None or self._metadata_peer_seen_at_s is None:
                return None
            if time.monotonic() - self._metadata_peer_seen_at_s > self.metadata_peer_timeout_s:
                self._metadata_peer = None
                self._metadata_peer_seen_at_s = None
                return None
            return self._metadata_peer

    def poll_authorization_decision(
        self,
    ) -> tuple[AuthorizationDecisionCommand, tuple[str, int]] | None:
        try:
            return self._accepted_authorization_decisions.get_nowait()
        except Empty:
            return None

    def poll_approach_confirmation(
        self,
    ) -> tuple[ApproachConfirmationCommand, tuple[str, int]] | None:
        try:
            return self._accepted_approach_confirmations.get_nowait()
        except Empty:
            return None

    def poll_payload_target_confirmation(
        self,
    ) -> tuple[PayloadTargetConfirmationCommand, tuple[str, int]] | None:
        try:
            return self._accepted_payload_target_confirmations.get_nowait()
        except Empty:
            return None

    def set_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage | None,
    ) -> None:
        self.authorization_guard.set_active_challenge(status)
        if status is None:
            self._authorization_peer = None

    def set_approach_challenge(
        self,
        status: ApproachChallengeStatusMessage | None,
    ) -> None:
        self.approach_guard.set_active_challenge(status)
        if status is None:
            self._approach_peer = None

    def set_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage | None,
    ) -> None:
        self.payload_target_guard.set_active_challenge(status)
        if status is None:
            self._payload_target_peer = None

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

    def publish_patrol_status(
        self,
        status: PatrolStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_patrol_status(status), peer)

    def publish_range_status(
        self,
        status: RangeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_range_status(status), peer)

    def publish_release_status(
        self,
        status: ReleaseStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_release_status(status), peer)

    def publish_approach_challenge(
        self,
        status: ApproachChallengeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self.approach_guard.set_active_challenge(status)
        self._approach_peer = peer
        self._channel.sendto(self.mavlink.encode_approach_challenge(status), peer)

    def publish_approach_status(
        self,
        status: ApproachStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_approach_status(status), peer)

    def publish_payload_target_challenge(
        self,
        status: PayloadTargetChallengeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self.payload_target_guard.set_active_challenge(status)
        self._payload_target_peer = peer
        self._channel.sendto(self.mavlink.encode_payload_target_challenge(status), peer)

    def publish_payload_target_status(
        self,
        status: PayloadTargetStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_payload_target_status(status), peer)

    def publish_target_pool_status(
        self,
        status: TargetPoolStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_target_pool_status(status), peer)

    def publish_scene_context_status(
        self,
        status: SceneContextStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_scene_context_status(status), peer)

    def publish_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self.authorization_guard.set_active_challenge(status)
        self._authorization_peer = peer
        self._channel.sendto(self.mavlink.encode_authorization_challenge(status), peer)

    def _register_metadata_peer(self, peer: tuple[str, int]) -> None:
        with self._metadata_peer_lock:
            self._metadata_peer = peer
            self._metadata_peer_seen_at_s = time.monotonic()

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
        self._last_patrol_status_sequence: int | None = None
        self._last_range_status_sequence: int | None = None
        self._last_release_status_sequence: int | None = None
        self._last_approach_challenge_sequence: int | None = None
        self._last_approach_status_sequence: int | None = None
        self._last_payload_target_challenge_sequence: int | None = None
        self._last_payload_target_status_sequence: int | None = None
        self._last_target_pool_status_sequence: int | None = None
        self._last_scene_context_status_sequence: int | None = None
        self._last_authorization_challenge_sequence: int | None = None
        self._pending_messages: dict[WireMessageType, deque[object]] = {
            WireMessageType.TRACK_STATUS: deque(maxlen=1),
            WireMessageType.MISSION_STATUS: deque(maxlen=1),
            WireMessageType.SAFETY_STATUS: deque(maxlen=1),
            WireMessageType.PATROL_STATUS: deque(maxlen=1),
            WireMessageType.RANGE_STATUS: deque(maxlen=1),
            WireMessageType.RELEASE_STATUS: deque(maxlen=1),
            WireMessageType.APPROACH_CHALLENGE: deque(maxlen=1),
            WireMessageType.APPROACH_STATUS: deque(maxlen=1),
            WireMessageType.PAYLOAD_TARGET_CHALLENGE: deque(maxlen=1),
            WireMessageType.PAYLOAD_TARGET_STATUS: deque(maxlen=1),
            WireMessageType.TARGET_POOL_STATUS: deque(maxlen=128),
            WireMessageType.SCENE_CONTEXT_STATUS: deque(maxlen=128),
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
            self._last_patrol_status_sequence = None
            self._last_range_status_sequence = None
            self._last_release_status_sequence = None
            self._last_approach_challenge_sequence = None
            self._last_approach_status_sequence = None
            self._last_payload_target_challenge_sequence = None
            self._last_payload_target_status_sequence = None
            self._last_target_pool_status_sequence = None
            self._last_scene_context_status_sequence = None
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

    def deliver_approach_confirmation(
        self,
        command: ApproachConfirmationCommand,
    ) -> UdpApproachConfirmationReceipt:
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        started_s = time.monotonic()
        attempts = 0
        acknowledgement: ApproachConfirmationAck | None = None
        for _ in range(self.maximum_attempts):
            attempts += 1
            self._channel.send(self.mavlink.encode_approach_confirmation(command))
            deadline_s = time.monotonic() + self.retry_interval_s
            while time.monotonic() < deadline_s:
                try:
                    _inner, packet = self._receive_packet(timeout_s=deadline_s - time.monotonic())
                except TimeoutError:
                    break
                if packet.message_type is WireMessageType.APPROACH_ACK:
                    if not isinstance(packet.message, ApproachConfirmationAck):
                        raise OperatorProtocolError("decoded approach ACK has the wrong type")
                    if packet.message.command_token != command.command_token:
                        raise OperatorProtocolError("approach ACK command token does not match")
                    acknowledgement = packet.message
                    break
                self._queue_status_packet(packet.message_type, packet.message)
            if acknowledgement is not None:
                break
        if acknowledgement is None:
            raise TimeoutError("approach confirmation acknowledgement timed out")
        return UdpApproachConfirmationReceipt(
            acknowledgement,
            attempts,
            time.monotonic() - started_s,
            self.remote,
        )

    def deliver_payload_target_confirmation(
        self,
        command: PayloadTargetConfirmationCommand,
    ) -> UdpPayloadTargetConfirmationReceipt:
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        started_s = time.monotonic()
        attempts = 0
        acknowledgement: PayloadTargetConfirmationAck | None = None
        for _ in range(self.maximum_attempts):
            attempts += 1
            self._channel.send(self.mavlink.encode_payload_target_confirmation(command))
            deadline_s = time.monotonic() + self.retry_interval_s
            while time.monotonic() < deadline_s:
                try:
                    _inner, packet = self._receive_packet(timeout_s=deadline_s - time.monotonic())
                except TimeoutError:
                    break
                if packet.message_type is WireMessageType.PAYLOAD_TARGET_ACK:
                    if not isinstance(packet.message, PayloadTargetConfirmationAck):
                        raise OperatorProtocolError("decoded payload target ACK has the wrong type")
                    if packet.message.command_token != command.command_token:
                        raise OperatorProtocolError(
                            "payload target ACK command token does not match"
                        )
                    acknowledgement = packet.message
                    break
                self._queue_status_packet(packet.message_type, packet.message)
            if acknowledgement is not None:
                break
        if acknowledgement is None:
            raise TimeoutError("payload target confirmation acknowledgement timed out")
        return UdpPayloadTargetConfirmationReceipt(
            acknowledgement,
            attempts,
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

    def receive_patrol_status(self, *, timeout_s: float) -> PatrolStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("patrol status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.PATROL_STATUS, timeout_s=timeout_s)
        if not isinstance(status, PatrolStatusMessage):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded patrol status has the wrong type")
        if (
            self._last_patrol_status_sequence is not None
            and status.sequence <= self._last_patrol_status_sequence
        ):
            raise OperatorProtocolError("patrol status sequence is not newer")
        self._last_patrol_status_sequence = status.sequence
        return status

    def receive_range_status(self, *, timeout_s: float) -> RangeStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("range status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.RANGE_STATUS, timeout_s=timeout_s)
        if not isinstance(status, RangeStatusMessage):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded range status has the wrong type")
        if (
            self._last_range_status_sequence is not None
            and status.sequence <= self._last_range_status_sequence
        ):
            raise OperatorProtocolError("range status sequence is not newer")
        self._last_range_status_sequence = status.sequence
        return status

    def receive_release_status(self, *, timeout_s: float) -> ReleaseStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("release status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.RELEASE_STATUS, timeout_s=timeout_s)
        if not isinstance(status, ReleaseStatusMessage):
            raise OperatorProtocolError("decoded release status has the wrong type")
        if (
            self._last_release_status_sequence is not None
            and status.sequence <= self._last_release_status_sequence
        ):
            raise OperatorProtocolError("release status sequence is not newer")
        self._last_release_status_sequence = status.sequence
        return status

    def receive_approach_challenge(
        self,
        *,
        timeout_s: float,
    ) -> ApproachChallengeStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("approach challenge timeout must be positive")
        status = self._receive_message(WireMessageType.APPROACH_CHALLENGE, timeout_s=timeout_s)
        if not isinstance(status, ApproachChallengeStatusMessage):
            raise OperatorProtocolError("decoded approach challenge has the wrong type")
        if (
            self._last_approach_challenge_sequence is not None
            and status.sequence <= self._last_approach_challenge_sequence
        ):
            raise OperatorProtocolError("approach challenge sequence is not newer")
        self._last_approach_challenge_sequence = status.sequence
        return status

    def receive_approach_status(self, *, timeout_s: float) -> ApproachStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("approach status timeout must be positive")
        status = self._receive_message(WireMessageType.APPROACH_STATUS, timeout_s=timeout_s)
        if not isinstance(status, ApproachStatusMessage):
            raise OperatorProtocolError("decoded approach status has the wrong type")
        if (
            self._last_approach_status_sequence is not None
            and status.sequence <= self._last_approach_status_sequence
        ):
            raise OperatorProtocolError("approach status sequence is not newer")
        self._last_approach_status_sequence = status.sequence
        return status

    def receive_payload_target_challenge(
        self,
        *,
        timeout_s: float,
    ) -> PayloadTargetChallengeStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("payload target challenge timeout must be positive")
        status = self._receive_message(
            WireMessageType.PAYLOAD_TARGET_CHALLENGE,
            timeout_s=timeout_s,
        )
        if not isinstance(status, PayloadTargetChallengeStatusMessage):
            raise OperatorProtocolError("decoded payload target challenge has the wrong type")
        if (
            self._last_payload_target_challenge_sequence is not None
            and status.sequence <= self._last_payload_target_challenge_sequence
        ):
            raise OperatorProtocolError("payload target challenge sequence is not newer")
        self._last_payload_target_challenge_sequence = status.sequence
        return status

    def receive_payload_target_status(self, *, timeout_s: float) -> PayloadTargetStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("payload target status timeout must be positive")
        status = self._receive_message(
            WireMessageType.PAYLOAD_TARGET_STATUS,
            timeout_s=timeout_s,
        )
        if not isinstance(status, PayloadTargetStatusMessage):
            raise OperatorProtocolError("decoded payload target status has the wrong type")
        if status.selection_command_id != self._active_command_id:
            raise OperatorProtocolError("payload target status selection command ID does not match")
        if (
            self._last_payload_target_status_sequence is not None
            and status.sequence <= self._last_payload_target_status_sequence
        ):
            raise OperatorProtocolError("payload target status sequence is not newer")
        self._last_payload_target_status_sequence = status.sequence
        return status

    def receive_target_pool_status(self, *, timeout_s: float) -> TargetPoolStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("target-pool status timeout must be positive")
        status = self._receive_message(WireMessageType.TARGET_POOL_STATUS, timeout_s=timeout_s)
        if not isinstance(status, TargetPoolStatusMessage):
            raise OperatorProtocolError("decoded target-pool status has the wrong type")
        if (
            self._last_target_pool_status_sequence is not None
            and status.sequence <= self._last_target_pool_status_sequence
        ):
            raise OperatorProtocolError("target-pool status sequence is not newer")
        self._last_target_pool_status_sequence = status.sequence
        return status

    def receive_scene_context_status(self, *, timeout_s: float) -> SceneContextStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("scene-context status timeout must be positive")
        status = self._receive_message(WireMessageType.SCENE_CONTEXT_STATUS, timeout_s=timeout_s)
        if not isinstance(status, SceneContextStatusMessage):
            raise OperatorProtocolError("decoded scene-context status has the wrong type")
        if (
            self._last_scene_context_status_sequence is not None
            and status.sequence <= self._last_scene_context_status_sequence
        ):
            raise OperatorProtocolError("scene-context status sequence is not newer")
        self._last_scene_context_status_sequence = status.sequence
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


def _approach_ack_reason(
    acceptance: ApproachConfirmationAcceptance,
) -> ApproachConfirmationAckReason:
    if acceptance.allowed:
        return ApproachConfirmationAckReason.ACCEPTED
    combined = " ".join(acceptance.reasons)
    if "no active" in combined:
        return ApproachConfirmationAckReason.NO_ACTIVE_CHALLENGE
    if "does not match" in combined or "peer does not match" in combined:
        return ApproachConfirmationAckReason.BINDING_MISMATCH
    if "expired" in combined or "outlives" in combined or "stale" in combined:
        return ApproachConfirmationAckReason.EXPIRED
    if "sequence" in combined:
        return ApproachConfirmationAckReason.SEQUENCE_REJECTED
    if "token was reused" in combined:
        return ApproachConfirmationAckReason.COMMAND_TOKEN_CONFLICT
    if "slide evidence" in combined:
        return ApproachConfirmationAckReason.INVALID_SLIDE
    return ApproachConfirmationAckReason.INVALID


def _payload_target_ack_reason(
    acceptance: PayloadTargetConfirmationAcceptance,
) -> PayloadTargetConfirmationAckReason:
    if acceptance.allowed:
        return PayloadTargetConfirmationAckReason.ACCEPTED
    combined = " ".join(acceptance.reasons)
    if "no active" in combined:
        return PayloadTargetConfirmationAckReason.NO_ACTIVE_CHALLENGE
    if "does not match" in combined or "peer does not match" in combined:
        return PayloadTargetConfirmationAckReason.BINDING_MISMATCH
    if "expired" in combined or "outlives" in combined or "stale" in combined:
        return PayloadTargetConfirmationAckReason.EXPIRED
    if "sequence" in combined:
        return PayloadTargetConfirmationAckReason.SEQUENCE_REJECTED
    if "token was reused" in combined:
        return PayloadTargetConfirmationAckReason.COMMAND_TOKEN_CONFLICT
    if "slide evidence" in combined:
        return PayloadTargetConfirmationAckReason.INVALID_SLIDE
    return PayloadTargetConfirmationAckReason.INVALID


__all__ = [
    "MAX_OPERATOR_UDP_DATAGRAM_BYTES",
    "ServerApproachConfirmationResult",
    "ServerPayloadTargetConfirmationResult",
    "UdpOperatorSelectionClient",
    "UdpOperatorSelectionServer",
    "UdpOperatorSessionClient",
    "UdpAuthorizationDecisionReceipt",
    "UdpApproachConfirmationReceipt",
    "UdpPayloadTargetConfirmationReceipt",
    "UdpSelectionReceipt",
]
