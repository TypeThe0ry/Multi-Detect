from __future__ import annotations

import ipaddress
import secrets
import socket
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from queue import Empty, Full, Queue
from threading import Condition, Event, Lock, Thread
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .autonomy.mission_operator import MissionOperatorAdapter

from .operator_link import (
    DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
    ApproachChallengeStatusMessage,
    ApproachConfirmationAcceptance,
    ApproachConfirmationCommand,
    ApproachConfirmationCommandGuard,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecisionAcceptance,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    GnssStatusMessage,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationAcceptance,
    PayloadTargetConfirmationCommand,
    PayloadTargetConfirmationCommandGuard,
    PayloadTargetStatusMessage,
    PersistentReplayState,
    PolicyRuntimeStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    RuntimeStatusMessage,
    SafetyStatusMessage,
    SceneContextStatusMessage,
    SelectionAcceptance,
    SelectionCommandGuard,
    TargetGeolocationStatusMessage,
    TargetObservationStatusMessage,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackStatusMessage,
    serial_is_newer,
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
    RuntimeStatusAck,
    SelectionAck,
    SelectionAckReason,
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
DEFAULT_BACKGROUND_ERROR_QUEUE_SIZE = 128
DEFAULT_BACKGROUND_ERROR_RATE_PER_SOURCE = 10.0
DEFAULT_BACKGROUND_ERROR_BURST_PER_SOURCE = 20.0
DEFAULT_COMMAND_RATE_PER_SECOND = 20.0
DEFAULT_COMMAND_BURST = 20.0
DEFAULT_DUPLICATE_ACK_INTERVAL_S = 0.25
MAX_TRACKED_PEER_STATE_ENTRIES = 64


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    updated_at_s: float

    def consume(self, *, now_s: float, refill_per_s: float, capacity: float) -> bool:
        elapsed = max(0.0, now_s - self.updated_at_s)
        self.tokens = min(capacity, self.tokens + elapsed * refill_per_s)
        self.updated_at_s = now_s
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


_STATUS_MESSAGE_TYPES = frozenset(
    {
        WireMessageType.TRACK_STATUS,
        WireMessageType.MISSION_STATUS,
        WireMessageType.SAFETY_STATUS,
        WireMessageType.PATROL_STATUS,
        WireMessageType.RANGE_STATUS,
        WireMessageType.TARGET_GEOLOCATION_STATUS,
        WireMessageType.GNSS_STATUS,
        WireMessageType.TARGET_OBSERVATION_STATUS,
        WireMessageType.RELEASE_STATUS,
        WireMessageType.RUNTIME_STATUS,
        WireMessageType.APPROACH_CHALLENGE,
        WireMessageType.APPROACH_STATUS,
        WireMessageType.PAYLOAD_TARGET_CHALLENGE,
        WireMessageType.PAYLOAD_TARGET_STATUS,
        WireMessageType.TARGET_POOL_STATUS,
        WireMessageType.SCENE_CONTEXT_STATUS,
        WireMessageType.AUTHORIZATION_CHALLENGE,
    }
)


class _StatusFreshnessError(OperatorProtocolError):
    """Authenticated status failed freshness before it could enter a cache."""

    def __init__(self, message_type: WireMessageType, detail: str) -> None:
        super().__init__(detail)
        self.message_type = message_type


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
class UdpRuntimeStatusSendReceipt:
    """One locally completed type-23 send armed for a canonical QGC ACK."""

    acknowledged_runtime_sequence: int
    run_hash64: int
    remote: tuple[str, int]
    sent_at_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class UdpRuntimeStatusAckReceipt:
    """Authenticated type-24 acceptance correlated to one local type-23 send."""

    acknowledgement: RuntimeStatusAck
    remote: tuple[str, int]
    sent_at_monotonic_ns: int
    acknowledged_at_monotonic_ns: int

    @property
    def latency_ms(self) -> float:
        return (
            max(
                0,
                self.acknowledged_at_monotonic_ns - self.sent_at_monotonic_ns,
            )
            / 1_000_000.0
        )


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
        metadata_peer_timeout_s: float = 5.0,
        command_clock: Callable[[], float] = time.time,
        mission_adapter: MissionOperatorAdapter | None = None,
        configured_peer: tuple[str, int] | None = None,
        require_configured_peer: bool = False,
        allow_first_packet_peer: bool = True,
        background_error_queue_size: int = DEFAULT_BACKGROUND_ERROR_QUEUE_SIZE,
        background_error_rate_per_source: float = DEFAULT_BACKGROUND_ERROR_RATE_PER_SOURCE,
        background_error_burst_per_source: float = DEFAULT_BACKGROUND_ERROR_BURST_PER_SOURCE,
        command_rate_per_second: float = DEFAULT_COMMAND_RATE_PER_SECOND,
        command_burst: float = DEFAULT_COMMAND_BURST,
        duplicate_ack_interval_s: float = DEFAULT_DUPLICATE_ACK_INTERVAL_S,
        max_commands_per_cycle: int = 16,
        replay_state_path: str | None = None,
        status_max_age_s: float = 5.0,
        status_future_tolerance_s: float = 0.5,
    ) -> None:
        if not bind_host.strip():
            raise ValueError("UDP bind host cannot be empty")
        if not 0 <= port <= 65_535:
            raise ValueError("UDP port must be in [0, 65535]")
        if receive_timeout_s is not None and receive_timeout_s <= 0.0:
            raise ValueError("UDP receive timeout must be positive")
        if not isfinite(metadata_peer_timeout_s) or metadata_peer_timeout_s <= 0.0:
            raise ValueError("metadata peer timeout must be finite and positive")
        if not callable(command_clock):
            raise TypeError("command_clock must be callable")
        if configured_peer is not None:
            if len(configured_peer) != 2 or not isinstance(configured_peer[0], str):
                raise ValueError("configured_peer must be an (IPv4 host, port) tuple")
            if not 0 <= int(configured_peer[1]) <= 65_535:
                raise ValueError("configured_peer port must be in [0, 65535]")
            configured_peer = (
                str(ipaddress.ip_address(configured_peer[0])),
                int(configured_peer[1]),
            )
        if require_configured_peer and configured_peer is None:
            raise ValueError(
                "configured_peer is required for real/flight deployments; "
                "first-packet peer binding is only valid with an explicit sim/dev flag"
            )
        for value, name in (
            (background_error_rate_per_source, "background_error_rate_per_source"),
            (background_error_burst_per_source, "background_error_burst_per_source"),
            (command_rate_per_second, "command_rate_per_second"),
            (command_burst, "command_burst"),
            (status_max_age_s, "status_max_age_s"),
            (status_future_tolerance_s, "status_future_tolerance_s"),
        ):
            if not isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        if not isfinite(duplicate_ack_interval_s) or duplicate_ack_interval_s < 0.0:
            raise ValueError("duplicate_ack_interval_s must be finite and non-negative")
        if background_error_queue_size <= 0 or max_commands_per_cycle <= 0:
            raise ValueError("queue sizes and max_commands_per_cycle must be positive")
        self.mavlink = mavlink
        if mission_adapter is None:
            from .autonomy.mission_operator import MissionOperatorAdapter

            mission_adapter = MissionOperatorAdapter(mavlink.codec)
        self.mission_adapter = mission_adapter
        self.metadata_peer_timeout_s = metadata_peer_timeout_s
        self._command_clock = command_clock
        self.configured_peer = configured_peer
        self.status_max_age_s = status_max_age_s
        self.status_future_tolerance_s = status_future_tolerance_s
        self.max_commands_per_cycle = max_commands_per_cycle
        self._peer_session_nonce = secrets.token_hex(16)
        self._retired_peer_session_nonces: deque[str] = deque(
            (self._peer_session_nonce,),
            maxlen=64,
        )
        self._command_peer: tuple[str, int] | None = configured_peer
        self._peer_session_established = configured_peer is not None
        self.allow_first_packet_peer = allow_first_packet_peer
        self._first_packet_peer_rejection_logged = False
        self._replay_state = PersistentReplayState(replay_state_path)
        self._max_sequence_forward_jump = DEFAULT_MAX_SEQUENCE_FORWARD_JUMP
        self._command_rate_per_second = command_rate_per_second
        self._command_burst = command_burst
        self._duplicate_ack_interval_s = duplicate_ack_interval_s
        self._command_buckets: OrderedDict[
            tuple[tuple[str, int], WireMessageType], _TokenBucket
        ] = OrderedDict()
        self._duplicate_ack_sent_at: OrderedDict[
            tuple[tuple[str, int], WireMessageType], float
        ] = OrderedDict()
        self._background_error_rate_per_source = background_error_rate_per_source
        self._background_error_burst_per_source = background_error_burst_per_source
        self._background_error_buckets: OrderedDict[tuple[str, int], _TokenBucket] = OrderedDict()
        self._background_error_counts: OrderedDict[
            tuple[tuple[str, int] | None, str], int
        ] = OrderedDict()
        self._unknown_peer_bucket = _TokenBucket(8.0, time.monotonic())
        self._background_error_drop_count = 0
        self._last_received_peer: tuple[str, int] | None = None
        self.selection_server = SelectionCommandServer(mavlink.codec, guard)
        self.authorization_guard = authorization_guard or AuthorizationDecisionCommandGuard()
        self.authorization_server = AuthorizationDecisionCommandServer(
            mavlink.codec,
            self.authorization_guard,
        )
        self.approach_guard = approach_guard or ApproachConfirmationCommandGuard()
        self.payload_target_guard = payload_target_guard or PayloadTargetConfirmationCommandGuard()
        if replay_state_path is not None:
            # Keep all command classes on one crash-safe sidecar.  The guards
            # still namespace their keys by command/session kind.
            for command_guard in (
                guard,
                self.authorization_guard,
                self.approach_guard,
                self.payload_target_guard,
            ):
                command_guard._replay_state = self._replay_state  # type: ignore[attr-defined]
        self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._channel.settimeout(receive_timeout_s)
        self._channel.bind((bind_host, port))
        self._acknowledgement_sequence = 0
        self._authorization_peer: tuple[str, int] | None = None
        self._approach_peer: tuple[str, int] | None = None
        self._payload_target_peer: tuple[str, int] | None = None
        self._metadata_peer: tuple[str, int] | None = None
        self._metadata_peer_seen_at_s: float | None = None
        self._metadata_peer_has_heartbeat = False
        self._metadata_peer_lock = Lock()
        self._runtime_status_ack_condition = Condition(Lock())
        self._pending_runtime_status_send: UdpRuntimeStatusSendReceipt | None = None
        self._pending_runtime_status_ack: UdpRuntimeStatusAckReceipt | None = None
        self._pending_runtime_status_ack_deadline_ns: int | None = None
        self._pending_runtime_status_ack_failure: Exception | None = None
        self._runtime_status_ack_waiting = False
        self._retired_runtime_status_ack_sequence_by_run_peer: dict[
            tuple[int, tuple[str, int]], int
        ] = {}
        self._last_runtime_status_ack_sequence_by_run: dict[int, int] = {}
        # Tests replace only this private clock. Condition timeouts continue to
        # use the real OS clock, while acceptance ordering is decided from one
        # monotonic timestamp domain under the condition lock.
        self._runtime_status_clock_ns = time.monotonic_ns
        self._accepted_commands: Queue[tuple[TargetSelectionCommand, tuple[str, int]]] = Queue(
            maxsize=max_commands_per_cycle
        )
        self._accepted_authorization_decisions: Queue[
            tuple[AuthorizationDecisionCommand, tuple[str, int]]
        ] = Queue(maxsize=max_commands_per_cycle)
        self._accepted_approach_confirmations: Queue[
            tuple[ApproachConfirmationCommand, tuple[str, int]]
        ] = Queue(maxsize=max_commands_per_cycle)
        self._accepted_payload_target_confirmations: Queue[
            tuple[PayloadTargetConfirmationCommand, tuple[str, int]]
        ] = Queue(maxsize=background_error_queue_size)
        self._background_errors: Queue[Exception] = Queue(maxsize=background_error_queue_size)
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
        self._last_received_peer = peer
        if self.configured_peer is not None and peer != self.configured_peer:
            # Do not let an authenticated frame from an unexpected UDP tuple
            # replace the configured operator identity.
            return None
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
                registered = self._register_metadata_peer(
                    peer,
                    from_heartbeat=True,
                    allow_replacement=False,
                )
                if registered and self.configured_peer is not None:
                    self._bind_initial_command_peer(peer)
            return None
        packet = self.mavlink.codec.decode(inner)
        if int(packet.message_type) >= 28:
            # Mission v2 shares the authenticated peer/session gate, token
            # bucket, persistent sequence high-water mark and datagram bound
            # with the legacy operator commands.  Do this before handing the
            # packet to the mission state machine so uploads and telemetry ACKs
            # cannot bypass link hardening.
            if self._command_peer is None:
                if not self.allow_first_packet_peer:
                    self._record_background_error(
                        OperatorProtocolError(
                            "authenticated mission packet rejected: configured_peer is required"
                        ),
                        peer=peer,
                    )
                    return None
                self._bind_initial_command_peer(peer)
            elif self._command_peer != peer:
                return None
            if not self._allow_command(peer, packet.message_type, now_s=time.monotonic()):
                return None
            replay_key = f"mission:{peer[0]}:{peer[1]}"
            if not self._replay_state.accept_sequence(
                replay_key,
                packet.sequence,
                max_forward_jump=self._max_sequence_forward_jump,
            ):
                return None
            self._register_metadata_peer(peer, from_heartbeat=False)
            for response in self.mission_adapter.handle(packet, peer=peer):
                self._channel.sendto(
                    self.mavlink.wrap_authenticated_operator_payload(response), peer
                )
            self.poll_mission_telemetry()
            return None
        if packet.message_type is WireMessageType.RUNTIME_STATUS_ACK:
            if not isinstance(packet.message, RuntimeStatusAck):
                raise OperatorProtocolError("decoded runtime-status ACK has the wrong type")
            self._accept_runtime_status_ack(packet.message, peer=peer)
            return None
        if self._command_peer is None:
            if not self.allow_first_packet_peer:
                if not self._first_packet_peer_rejection_logged:
                    self._first_packet_peer_rejection_logged = True
                    self._record_background_error(
                        OperatorProtocolError(
                            "authenticated command rejected: configured_peer is required"
                        ),
                        peer=peer,
                    )
                return None
            self._bind_initial_command_peer(peer)
        # Existing command traffic may discover a metadata peer for interactive
        # operation, but only an authenticated HEARTBEAT marks it eligible for
        # the synchronized runtime-status acceptance barrier.
        # A command may refresh an already authenticated heartbeat lease, but
        # it can never establish or replace the metadata peer.  Peer adoption
        # is reserved for the signed heartbeat or explicit re-handshake.
        with self._metadata_peer_lock:
            metadata_peer = self._metadata_peer
        if metadata_peer is None and self._command_peer == peer:
            self._register_metadata_peer(peer, from_heartbeat=False, allow_replacement=False)
        elif metadata_peer == peer:
            self._register_metadata_peer(peer, from_heartbeat=False, allow_replacement=False)
        if not self._allow_command(peer, packet.message_type, now_s=time.monotonic()):
            return None
        # QGC derives command time from authenticated Jetson status envelopes.
        # Integrated/live runtimes inject that exact runtime clock; standalone
        # diagnostic clients retain the historical Unix-time default.
        received_at_s = float(self._command_clock())
        if not isfinite(received_at_s) or received_at_s < 0.0:
            raise ValueError("operator command clock returned an invalid timestamp")
        self._acknowledgement_sequence = self._replay_state.next_sequence(
            "acknowledgement",
            max_forward_jump=self._max_sequence_forward_jump,
        )
        if packet.message_type is WireMessageType.TARGET_SELECTION:
            if self._command_peer != peer:
                if not isinstance(packet.message, TargetSelectionCommand):
                    raise OperatorProtocolError("decoded selection message has the wrong type")
                acceptance = SelectionAcceptance(
                    False,
                    ("selection peer does not match the authenticated session",),
                )
                acknowledgement_payload = self.mavlink.codec.encode_ack(
                    SelectionAck(
                        packet.message.command_id,
                        False,
                        SelectionAckReason.INVALID,
                        packet.message.sequence,
                    ),
                    sequence=self._acknowledgement_sequence,
                    sent_at_s=received_at_s,
                )
                result = ServerSelectionResult(
                    packet.message,
                    acceptance,
                    acknowledgement_payload,
                    False,
                )
            else:
                result = self.selection_server.handle_selection(
                    inner,
                    received_at_s=received_at_s,
                    acknowledgement_sequence=self._acknowledgement_sequence,
                )
        elif packet.message_type is WireMessageType.AUTHORIZATION_DECISION:
            if not isinstance(packet.message, AuthorizationDecisionCommand):
                raise OperatorProtocolError("decoded authorization decision has the wrong type")
            if self._command_peer != peer or self._authorization_peer != peer:
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
                    sent_at_s=received_at_s,
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
                    received_at_s=received_at_s,
                    acknowledgement_sequence=self._acknowledgement_sequence,
                )
        elif packet.message_type is WireMessageType.APPROACH_CONFIRMATION:
            if not isinstance(packet.message, ApproachConfirmationCommand):
                raise OperatorProtocolError("decoded approach confirmation has the wrong type")
            if self._command_peer != peer or self._approach_peer != peer:
                acceptance = ApproachConfirmationAcceptance(
                    False,
                    ("approach confirmation peer does not match the challenge recipient",),
                )
            else:
                acceptance = self.approach_guard.evaluate(
                    packet.message,
                    received_at_s=received_at_s,
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
                sent_at_s=received_at_s,
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
            if self._command_peer != peer or self._payload_target_peer != peer:
                acceptance = PayloadTargetConfirmationAcceptance(
                    False,
                    ("payload target confirmation peer does not match the challenge recipient",),
                )
            else:
                acceptance = self.payload_target_guard.evaluate(
                    packet.message,
                    received_at_s=received_at_s,
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
                sent_at_s=received_at_s,
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
        should_send_ack = not result.duplicate or self._allow_duplicate_ack(
            peer,
            packet.message_type,
            now_s=time.monotonic(),
        )
        if should_send_ack:
            self._channel.sendto(acknowledgement, peer)
            self._duplicate_ack_sent_at[(peer, packet.message_type)] = time.monotonic()
        if result.acceptance.allowed and not result.duplicate:
            if isinstance(result, ServerSelectionResult):
                self._put_bounded(self._accepted_commands, (result.command, peer))
            elif isinstance(result, ServerAuthorizationDecisionResult):
                self._put_bounded(self._accepted_authorization_decisions, (result.command, peer))
            elif isinstance(result, ServerApproachConfirmationResult):
                self._put_bounded(self._accepted_approach_confirmations, (result.command, peer))
            else:
                self._put_bounded(
                    self._accepted_payload_target_confirmations,
                    (result.command, peer),
                )
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
            if self._expire_metadata_peer_locked(now_s=time.monotonic()):
                return None
            return self._metadata_peer

    def active_heartbeat_peer(self) -> tuple[str, int] | None:
        """Return the live peer only when QGC proved it with a signed HEARTBEAT."""

        with self._metadata_peer_lock:
            if self._expire_metadata_peer_locked(now_s=time.monotonic()):
                return None
            if (
                self._metadata_peer is None
                or self._metadata_peer_seen_at_s is None
                or not self._metadata_peer_has_heartbeat
            ):
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

    @property
    def background_error_drop_count(self) -> int:
        return self._background_error_drop_count

    @property
    def background_error_counts(self) -> dict[tuple[tuple[str, int] | None, str], int]:
        return dict(self._background_error_counts)

    @property
    def peer_session_nonce(self) -> str:
        """Nonce identifying the currently bound authenticated UDP session."""

        return self._peer_session_nonce

    def rehandshake_peer(self, peer: tuple[str, int], challenge_nonce: str) -> None:
        """Replace a peer only after an application-level fresh challenge.

        The nonce is intentionally supplied by the authenticated challenge
        controller, rather than inferred from a UDP source tuple.  Callers
        must retain the nonce until both sides have acknowledged the new
        session; old frames remain bound to the retired tuple/session.
        """

        if not isinstance(challenge_nonce, str) or len(challenge_nonce) < 16:
            raise ValueError("peer re-handshake nonce must be a fresh non-empty value")
        if len(peer) != 2 or not isinstance(peer[0], str) or not 0 <= int(peer[1]) <= 65_535:
            raise ValueError("peer must be an (IPv4 host, port) tuple")
        normalized = (str(ipaddress.ip_address(peer[0])), int(peer[1]))
        with self._metadata_peer_lock:
            if challenge_nonce in self._retired_peer_session_nonces:
                raise ValueError("peer re-handshake nonce was already used")
            self._peer_session_nonce = challenge_nonce
            self._retired_peer_session_nonces.append(challenge_nonce)
            self._command_peer = normalized
            self._peer_session_established = True
        self._register_metadata_peer(
            normalized,
            from_heartbeat=True,
            allow_replacement=True,
        )

    def _allow_command(
        self,
        peer: tuple[str, int],
        message_type: WireMessageType,
        *,
        now_s: float,
    ) -> bool:
        key = (peer, message_type)
        bucket = self._command_buckets.get(key)
        if bucket is None:
            if self._command_peer is not None and peer != self._command_peer:
                if not self._unknown_peer_bucket.consume(
                    now_s=now_s,
                    refill_per_s=self._command_rate_per_second,
                    capacity=self._command_burst,
                ):
                    return False
            self._evict_bounded(self._command_buckets, self._command_peer)
            bucket = _TokenBucket(self._command_burst, now_s)
            self._command_buckets[key] = bucket
        else:
            self._command_buckets.move_to_end(key)
        return bucket.consume(
            now_s=now_s,
            refill_per_s=self._command_rate_per_second,
            capacity=self._command_burst,
        )

    def _allow_duplicate_ack(
        self,
        peer: tuple[str, int],
        message_type: WireMessageType,
        *,
        now_s: float,
    ) -> bool:
        key = (peer, message_type)
        previous = self._duplicate_ack_sent_at.get(key)
        if previous is not None and now_s - previous < self._duplicate_ack_interval_s:
            return False
        self._evict_bounded(self._duplicate_ack_sent_at, self._command_peer)
        self._duplicate_ack_sent_at[key] = now_s
        return True

    @staticmethod
    def _evict_bounded(mapping: OrderedDict, protected_peer: tuple[str, int] | None) -> None:
        while len(mapping) >= MAX_TRACKED_PEER_STATE_ENTRIES:
            victim = next(iter(mapping))
            key_peer = (
                victim[0]
                if isinstance(victim, tuple) and isinstance(victim[0], tuple)
                else victim
            )
            if protected_peer is not None and key_peer == protected_peer:
                mapping.move_to_end(victim)
                if len(mapping) == 1:
                    return
                continue
            del mapping[victim]
            return

    @staticmethod
    def _put_bounded(queue: Queue[object], item: object) -> None:
        try:
            queue.put_nowait(item)
        except Full:
            # Accepted commands are deliberately bounded.  Dropping a command
            # is safer than allowing an authenticated flood to exhaust memory.
            return

    def _record_background_error(
        self,
        exc: Exception,
        *,
        peer: tuple[str, int] | None,
    ) -> None:
        source = peer
        key = (source, type(exc).__name__)
        self._evict_bounded(self._background_error_counts, self._command_peer)
        self._background_error_counts[key] = self._background_error_counts.get(key, 0) + 1
        if source is not None:
            bucket = self._background_error_buckets.get(source)
            now_s = time.monotonic()
            if bucket is None:
                self._evict_bounded(self._background_error_buckets, self._command_peer)
                bucket = _TokenBucket(self._background_error_burst_per_source, now_s)
                self._background_error_buckets[source] = bucket
            else:
                self._background_error_buckets.move_to_end(source)
            if not bucket.consume(
                now_s=now_s,
                refill_per_s=self._background_error_rate_per_source,
                capacity=self._background_error_burst_per_source,
            ):
                self._background_error_drop_count += 1
                return
        try:
            self._background_errors.put_nowait(exc)
        except Full:
            self._background_error_drop_count += 1

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

    def publish_target_geolocation_status(
        self,
        status: TargetGeolocationStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_target_geolocation_status(status), peer)

    def publish_gnss_status(
        self,
        status: GnssStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_gnss_status(status), peer)

    def publish_target_observation_status(
        self,
        status: TargetObservationStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_target_observation_status(status), peer)

    def publish_release_status(
        self,
        status: ReleaseStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_release_status(status), peer)

    def publish_runtime_status(
        self,
        status: RuntimeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_runtime_status(status), peer)

    def publish_policy_runtime_status(
        self,
        status: PolicyRuntimeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> None:
        self._channel.sendto(self.mavlink.encode_policy_runtime_status(status), peer)

    def publish_runtime_status_for_acceptance(
        self,
        status: RuntimeStatusMessage,
        *,
        peer: tuple[str, int],
    ) -> UdpRuntimeStatusSendReceipt:
        """Arm correlation, then complete the local type-23 send exactly once."""

        # Lock order is always metadata -> runtime ACK condition. That makes a
        # heartbeat-peer change atomic with arming or accepting one type-24 ACK.
        with self._metadata_peer_lock:
            self._expire_metadata_peer_locked(now_s=time.monotonic())
            active_peer = self._metadata_peer if self._metadata_peer_has_heartbeat else None
            if active_peer is None:
                raise RuntimeError(
                    "runtime-status acceptance requires an active QGC heartbeat peer"
                )
            if peer != active_peer:
                raise RuntimeError("runtime-status recipient is not the active QGC heartbeat peer")
            receipt = UdpRuntimeStatusSendReceipt(
                acknowledged_runtime_sequence=status.sequence,
                run_hash64=status.run_hash64,
                remote=peer,
                sent_at_monotonic_ns=self._runtime_status_clock_ns(),
            )
            with self._runtime_status_ack_condition:
                if self._pending_runtime_status_send is not None:
                    raise RuntimeError(
                        "a runtime-status acceptance acknowledgement is already pending"
                    )
                last_sequence = self._last_runtime_status_ack_sequence_by_run.get(status.run_hash64)
                retired_sequence = self._retired_runtime_status_ack_sequence_by_run_peer.get(
                    (status.run_hash64, peer)
                )
                if (
                    last_sequence is not None
                    and not serial_is_newer(
                        status.sequence,
                        last_sequence,
                        max_forward_jump=self._max_sequence_forward_jump,
                    )
                ) or (
                    retired_sequence is not None
                    and not serial_is_newer(
                        status.sequence,
                        retired_sequence,
                        max_forward_jump=self._max_sequence_forward_jump,
                    )
                ):
                    raise RuntimeError(
                        "runtime-status acceptance sequence is duplicate or out of order"
                    )
                self._pending_runtime_status_send = receipt
                self._pending_runtime_status_ack = None
                self._pending_runtime_status_ack_deadline_ns = None
                self._pending_runtime_status_ack_failure = None
                self._runtime_status_ack_waiting = False
        try:
            self._channel.sendto(self.mavlink.encode_runtime_status(status), peer)
        except BaseException:
            with self._runtime_status_ack_condition:
                if self._pending_runtime_status_send == receipt:
                    self._clear_pending_runtime_status_locked()
                    self._runtime_status_ack_condition.notify_all()
            raise
        return receipt

    def wait_runtime_status_ack(
        self,
        send_receipt: UdpRuntimeStatusSendReceipt,
        *,
        timeout_s: float,
    ) -> UdpRuntimeStatusAckReceipt:
        """Wait for one exact, authenticated type-24 QGC acceptance proof."""

        if not isfinite(timeout_s) or timeout_s <= 0.0:
            raise ValueError("runtime-status ACK timeout must be finite and positive")
        deadline_ns = self._runtime_status_clock_ns() + int(timeout_s * 1_000_000_000)
        with self._runtime_status_ack_condition:
            if self._pending_runtime_status_send != send_receipt:
                raise RuntimeError(
                    "runtime-status send receipt is no longer the pending acceptance"
                )
            if self._runtime_status_ack_waiting:
                raise RuntimeError("runtime-status acceptance acknowledgement already has a waiter")
            self._runtime_status_ack_waiting = True
            self._pending_runtime_status_ack_deadline_ns = deadline_ns
            while True:
                background_error = self.poll_error()
                if background_error is not None:
                    self._retire_runtime_status_send_locked(send_receipt)
                    self._clear_pending_runtime_status_locked()
                    raise RuntimeError(
                        "runtime-status ACK transport rejected a datagram"
                    ) from background_error

                failure = self._pending_runtime_status_ack_failure
                if failure is not None:
                    self._retire_runtime_status_send_locked(send_receipt)
                    self._clear_pending_runtime_status_locked()
                    raise failure

                acknowledgement = self._pending_runtime_status_ack
                if acknowledgement is not None:
                    if acknowledgement.remote != send_receipt.remote:
                        self._retire_runtime_status_send_locked(send_receipt)
                        self._clear_pending_runtime_status_locked()
                        raise OperatorProtocolError("runtime-status ACK peer does not match")
                    if (
                        acknowledgement.acknowledgement.acknowledged_runtime_sequence
                        != send_receipt.acknowledged_runtime_sequence
                    ):
                        self._retire_runtime_status_send_locked(send_receipt)
                        self._clear_pending_runtime_status_locked()
                        raise OperatorProtocolError("runtime-status ACK sequence does not match")
                    if acknowledgement.acknowledgement.run_hash64 != send_receipt.run_hash64:
                        self._retire_runtime_status_send_locked(send_receipt)
                        self._clear_pending_runtime_status_locked()
                        raise OperatorProtocolError("runtime-status ACK run hash does not match")
                    if acknowledgement.sent_at_monotonic_ns != send_receipt.sent_at_monotonic_ns:
                        self._retire_runtime_status_send_locked(send_receipt)
                        self._clear_pending_runtime_status_locked()
                        raise OperatorProtocolError(
                            "runtime-status ACK send receipt does not match"
                        )
                    if acknowledgement.acknowledged_at_monotonic_ns > deadline_ns:
                        self._retire_runtime_status_send_locked(send_receipt)
                        self._clear_pending_runtime_status_locked()
                        raise TimeoutError(
                            "runtime-status acceptance acknowledgement arrived after deadline"
                        )
                    self._retire_runtime_status_send_locked(send_receipt)
                    self._clear_pending_runtime_status_locked()
                    return acknowledgement

                remaining_ns = deadline_ns - self._runtime_status_clock_ns()
                if remaining_ns <= 0:
                    self._retire_runtime_status_send_locked(send_receipt)
                    self._clear_pending_runtime_status_locked()
                    raise TimeoutError(
                        "runtime-status acceptance acknowledgement timed out"
                    ) from None
                self._runtime_status_ack_condition.wait(remaining_ns / 1_000_000_000)

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

    def _register_metadata_peer(
        self,
        peer: tuple[str, int],
        *,
        from_heartbeat: bool,
        allow_replacement: bool = True,
    ) -> bool:
        with self._metadata_peer_lock:
            if (
                not allow_replacement
                and self._metadata_peer is not None
                and self._metadata_peer != peer
            ):
                return False
            previous_heartbeat_peer = (
                self._metadata_peer if self._metadata_peer_has_heartbeat else None
            )
            same_heartbeat_peer = self._metadata_peer_has_heartbeat and self._metadata_peer == peer
            self._metadata_peer = peer
            self._metadata_peer_seen_at_s = time.monotonic()
            self._metadata_peer_has_heartbeat = from_heartbeat or same_heartbeat_peer
            current_heartbeat_peer = (
                self._metadata_peer if self._metadata_peer_has_heartbeat else None
            )
            if previous_heartbeat_peer != current_heartbeat_peer:
                self._fail_pending_runtime_status_for_peer_change_locked(
                    previous_peer=previous_heartbeat_peer,
                    current_peer=current_heartbeat_peer,
                )
            return True

    def _bind_initial_command_peer(self, peer: tuple[str, int]) -> None:
        """Bind the first authenticated command/session peer exactly once."""

        with self._metadata_peer_lock:
            if self._command_peer is None:
                self._command_peer = peer
                self._peer_session_established = True

    def _expire_metadata_peer_locked(self, *, now_s: float) -> bool:
        """Expire one peer lease while preserving metadata -> ACK lock order."""

        if self._metadata_peer is None or self._metadata_peer_seen_at_s is None:
            return False
        if now_s - self._metadata_peer_seen_at_s <= self.metadata_peer_timeout_s:
            return False
        previous_heartbeat_peer = self._metadata_peer if self._metadata_peer_has_heartbeat else None
        self._metadata_peer = None
        self._metadata_peer_seen_at_s = None
        self._metadata_peer_has_heartbeat = False
        if previous_heartbeat_peer is not None:
            self._fail_pending_runtime_status_for_peer_change_locked(
                previous_peer=previous_heartbeat_peer,
                current_peer=None,
            )
        return True

    def _fail_pending_runtime_status_for_peer_change_locked(
        self,
        *,
        previous_peer: tuple[str, int] | None,
        current_peer: tuple[str, int] | None,
    ) -> None:
        """Invalidate an armed ACK without allowing its late packet into a later send."""

        with self._runtime_status_ack_condition:
            pending = self._pending_runtime_status_send
            if pending is None or self._pending_runtime_status_ack_failure is not None:
                return
            if pending.remote == current_peer:
                return
            self._retire_runtime_status_send_locked(pending)
            self._pending_runtime_status_ack_failure = RuntimeError(
                "active QGC heartbeat peer changed before runtime-status acceptance "
                f"({previous_peer!r} -> {current_peer!r})"
            )
            self._runtime_status_ack_condition.notify_all()

    def _accept_runtime_status_ack(
        self,
        acknowledgement: RuntimeStatusAck,
        *,
        peer: tuple[str, int],
    ) -> None:
        acknowledged_at_monotonic_ns = self._runtime_status_clock_ns()
        with self._runtime_status_ack_condition:
            if self._runtime_status_ack_is_retired_locked(acknowledgement, peer=peer):
                return

        # Match the lock order used by publish and heartbeat replacement so a
        # peer cannot change between validation and the ACK state transition.
        with self._metadata_peer_lock:
            self._expire_metadata_peer_locked(now_s=time.monotonic())
            active_peer = self._metadata_peer if self._metadata_peer_has_heartbeat else None
            with self._runtime_status_ack_condition:
                if self._runtime_status_ack_is_retired_locked(
                    acknowledgement,
                    peer=peer,
                ):
                    return
                if active_peer is None:
                    raise OperatorProtocolError(
                        "runtime-status ACK arrived without an active QGC heartbeat peer"
                    )
                if peer != active_peer:
                    raise OperatorProtocolError(
                        "runtime-status ACK peer is not the active QGC heartbeat peer"
                    )
                pending = self._pending_runtime_status_send
                if pending is None:
                    raise OperatorProtocolError(
                        "runtime-status ACK is duplicate, stale or was not requested"
                    )
                if pending.remote != peer:
                    raise OperatorProtocolError("runtime-status ACK peer does not match")
                if (
                    acknowledgement.acknowledged_runtime_sequence
                    != pending.acknowledged_runtime_sequence
                ):
                    raise OperatorProtocolError("runtime-status ACK sequence does not match")
                if acknowledgement.run_hash64 != pending.run_hash64:
                    raise OperatorProtocolError("runtime-status ACK run hash does not match")

                deadline_ns = self._pending_runtime_status_ack_deadline_ns
                if deadline_ns is not None and acknowledged_at_monotonic_ns > deadline_ns:
                    self._retire_runtime_status_send_locked(pending)
                    self._pending_runtime_status_ack_failure = TimeoutError(
                        "runtime-status acceptance acknowledgement arrived after deadline"
                    )
                    self._runtime_status_ack_condition.notify_all()
                    return

                previous = self._last_runtime_status_ack_sequence_by_run.get(
                    acknowledgement.run_hash64
                )
                if previous is not None and not serial_is_newer(
                    acknowledgement.acknowledged_runtime_sequence,
                    previous,
                    max_forward_jump=self._max_sequence_forward_jump,
                ):
                    raise OperatorProtocolError(
                        "runtime-status ACK sequence is duplicate or out of order"
                    )
                receipt = UdpRuntimeStatusAckReceipt(
                    acknowledgement=acknowledgement,
                    remote=peer,
                    sent_at_monotonic_ns=pending.sent_at_monotonic_ns,
                    acknowledged_at_monotonic_ns=acknowledged_at_monotonic_ns,
                )
                self._last_runtime_status_ack_sequence_by_run[acknowledgement.run_hash64] = (
                    acknowledgement.acknowledged_runtime_sequence
                )
                self._retire_runtime_status_send_locked(pending)
                self._pending_runtime_status_ack = receipt
                self._runtime_status_ack_condition.notify_all()

    def _runtime_status_ack_is_retired_locked(
        self,
        acknowledgement: RuntimeStatusAck,
        *,
        peer: tuple[str, int],
    ) -> bool:
        retired_sequence = self._retired_runtime_status_ack_sequence_by_run_peer.get(
            (acknowledgement.run_hash64, peer)
        )
        return retired_sequence is not None and not serial_is_newer(
            acknowledgement.acknowledged_runtime_sequence,
            retired_sequence,
            max_forward_jump=self._max_sequence_forward_jump,
        )

    def _retire_runtime_status_send_locked(
        self,
        send_receipt: UdpRuntimeStatusSendReceipt,
    ) -> None:
        key = (send_receipt.run_hash64, send_receipt.remote)
        previous = self._retired_runtime_status_ack_sequence_by_run_peer.get(key)
        if previous is None or serial_is_newer(
            send_receipt.acknowledged_runtime_sequence,
            previous,
            max_forward_jump=self._max_sequence_forward_jump,
        ):
            self._retired_runtime_status_ack_sequence_by_run_peer[key] = (
                send_receipt.acknowledged_runtime_sequence
            )

    def _clear_pending_runtime_status_locked(self) -> None:
        self._pending_runtime_status_send = None
        self._pending_runtime_status_ack = None
        self._pending_runtime_status_ack_deadline_ns = None
        self._pending_runtime_status_ack_failure = None
        self._runtime_status_ack_waiting = False

    def _cancel_pending_runtime_status_send(
        self,
        send_receipt: UdpRuntimeStatusSendReceipt,
    ) -> None:
        with self._runtime_status_ack_condition:
            if self._pending_runtime_status_send == send_receipt:
                self._retire_runtime_status_send_locked(send_receipt)
                self._clear_pending_runtime_status_locked()
                self._runtime_status_ack_condition.notify_all()

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_mission_telemetry()
                self.serve_once()
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._stop_event.is_set():
                    self._record_background_error(exc, peer=self._last_received_peer)
                    with self._runtime_status_ack_condition:
                        self._runtime_status_ack_condition.notify_all()
                return
            except (RuntimeError, ValueError, TypeError) as exc:
                self._record_background_error(exc, peer=self._last_received_peer)
                with self._runtime_status_ack_condition:
                    self._runtime_status_ack_condition.notify_all()

    def poll_mission_telemetry(self) -> None:
        """Also callable by a foreground runtime using ``serve_once`` directly."""
        peer = self.active_metadata_peer()
        sender = None if peer is None else lambda payload: self._channel.sendto(
            self.mavlink.wrap_authenticated_operator_payload(payload), peer
        )
        self.mission_adapter.tick(sender)

    def close(self) -> None:
        self._stop_event.set()
        self.mission_adapter.close()
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
        source_host: str | None = None,
        replay_state_path: str | None = None,
    ) -> None:
        self.remote = _remote_address(host, port)
        self.mavlink = mavlink
        self.retry_interval_s = retry_interval_s
        self.maximum_attempts = maximum_attempts
        self.source_host = source_host
        self.replay_state_path = replay_state_path

    def deliver(self, command: TargetSelectionCommand) -> UdpSelectionReceipt:
        with UdpOperatorSessionClient(
            host=self.remote[0],
            port=self.remote[1],
            mavlink=self.mavlink,
            retry_interval_s=self.retry_interval_s,
            maximum_attempts=self.maximum_attempts,
            source_host=self.source_host,
            replay_state_path=self.replay_state_path,
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
        source_host: str | None = None,
        replay_state_path: str | None = None,
        status_max_age_s: float = 5.0,
        status_future_tolerance_s: float = 0.5,
    ) -> None:
        self.remote = _remote_address(host, port)
        if retry_interval_s <= 0.0:
            raise ValueError("UDP retry interval must be positive")
        if maximum_attempts <= 0:
            raise ValueError("UDP maximum attempts must be positive")
        if not isfinite(status_max_age_s) or status_max_age_s <= 0.0:
            raise ValueError("status_max_age_s must be finite and positive")
        if not isfinite(status_future_tolerance_s) or status_future_tolerance_s < 0.0:
            raise ValueError("status_future_tolerance_s must be finite and non-negative")
        self.mavlink = mavlink
        self.retry_interval_s = retry_interval_s
        self.maximum_attempts = maximum_attempts
        self.status_max_age_s = status_max_age_s
        self.status_future_tolerance_s = status_future_tolerance_s
        self._status_replay_state = PersistentReplayState(replay_state_path)
        self._status_session_nonce = secrets.token_hex(16)
        self._status_stale_count = 0
        self._status_future_count = 0
        self._last_status_stale = False
        self._channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if source_host is not None:
            try:
                parsed_source = ipaddress.ip_address(source_host.strip())
            except (AttributeError, ValueError) as exc:
                self._channel.close()
                raise ValueError(
                    "operator UDP source_host must be a concrete IPv4 address"
                ) from exc
            if parsed_source.version != 4:
                self._channel.close()
                raise ValueError("operator UDP source_host must be an IPv4 address")
            source_host = str(parsed_source)
            self._channel.bind((source_host, 0))
        self._channel.connect(self.remote)
        self.local_source_address = str(ipaddress.ip_address(self._channel.getsockname()[0]))
        if source_host is not None and self.local_source_address != source_host:
            self._channel.close()
            raise OSError("operator UDP sender source differs from configured binding")
        self._active_command_id: str | None = None
        self._last_status_sequence: int | None = None
        self._last_mission_status_sequence: int | None = None
        self._last_safety_status_sequence: int | None = None
        self._last_patrol_status_sequence: int | None = None
        self._last_range_status_sequence: int | None = None
        self._last_target_geolocation_status_sequence: int | None = None
        self._last_gnss_status_sequence: int | None = None
        self._last_target_observation_status_sequence: int | None = None
        self._last_release_status_sequence: int | None = None
        self._last_runtime_status_sequence: int | None = None
        self._runtime_status_ack_sequence = 0
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
            WireMessageType.TARGET_GEOLOCATION_STATUS: deque(maxlen=1),
            WireMessageType.GNSS_STATUS: deque(maxlen=1),
            WireMessageType.TARGET_OBSERVATION_STATUS: deque(maxlen=1),
            WireMessageType.RELEASE_STATUS: deque(maxlen=1),
            WireMessageType.RUNTIME_STATUS: deque(maxlen=1),
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
                except _StatusFreshnessError as exc:
                    self._note_status_freshness_error(exc)
                    continue
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
            self._last_gnss_status_sequence = None
            self._last_release_status_sequence = None
            self._last_runtime_status_sequence = None
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
                except _StatusFreshnessError as exc:
                    self._note_status_freshness_error(exc)
                    continue
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
                except _StatusFreshnessError as exc:
                    self._note_status_freshness_error(exc)
                    continue
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
                except _StatusFreshnessError as exc:
                    self._note_status_freshness_error(exc)
                    continue
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
        if self._last_authorization_challenge_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_authorization_challenge_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_status_sequence is not None and not serial_is_newer(

            status.sequence,

            self._last_status_sequence,

            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,

        ):
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
        if self._last_mission_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_mission_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_safety_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_safety_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_patrol_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_patrol_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_range_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_range_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        ):
            raise OperatorProtocolError("range status sequence is not newer")
        self._last_range_status_sequence = status.sequence
        return status

    def receive_target_geolocation_status(
        self,
        *,
        timeout_s: float,
    ) -> TargetGeolocationStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("target geolocation status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(
            WireMessageType.TARGET_GEOLOCATION_STATUS,
            timeout_s=timeout_s,
        )
        if not isinstance(
            status,
            TargetGeolocationStatusMessage,
        ):  # pragma: no cover - type narrowing
            raise OperatorProtocolError("decoded target geolocation status has the wrong type")
        if self._last_target_geolocation_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_target_geolocation_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        ):
            raise OperatorProtocolError("target geolocation status sequence is not newer")
        self._last_target_geolocation_status_sequence = status.sequence
        return status

    def receive_gnss_status(self, *, timeout_s: float) -> GnssStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("GNSS status timeout must be positive")
        status = self._receive_message(WireMessageType.GNSS_STATUS, timeout_s=timeout_s)
        if not isinstance(status, GnssStatusMessage):
            raise OperatorProtocolError("decoded GNSS status has the wrong type")
        if self._last_gnss_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_gnss_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        ):
            raise OperatorProtocolError("GNSS status sequence is not newer")
        self._last_gnss_status_sequence = status.sequence
        return status

    def receive_target_observation_status(
        self,
        *,
        timeout_s: float,
    ) -> TargetObservationStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("target observation status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(
            WireMessageType.TARGET_OBSERVATION_STATUS,
            timeout_s=timeout_s,
        )
        if not isinstance(status, TargetObservationStatusMessage):
            raise OperatorProtocolError("decoded target observation status has the wrong type")
        if self._last_target_observation_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_target_observation_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        ):
            raise OperatorProtocolError("target observation status sequence is not newer")
        self._last_target_observation_status_sequence = status.sequence
        return status

    def receive_release_status(self, *, timeout_s: float) -> ReleaseStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("release status timeout must be positive")
        if self._active_command_id is None:
            raise RuntimeError("no accepted selection command is active")
        status = self._receive_message(WireMessageType.RELEASE_STATUS, timeout_s=timeout_s)
        if not isinstance(status, ReleaseStatusMessage):
            raise OperatorProtocolError("decoded release status has the wrong type")
        if self._last_release_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_release_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        ):
            raise OperatorProtocolError("release status sequence is not newer")
        self._last_release_status_sequence = status.sequence
        return status

    def receive_runtime_status(self, *, timeout_s: float) -> RuntimeStatusMessage:
        if timeout_s <= 0.0:
            raise ValueError("runtime status timeout must be positive")
        status = self._receive_message(WireMessageType.RUNTIME_STATUS, timeout_s=timeout_s)
        if not isinstance(status, RuntimeStatusMessage):
            raise OperatorProtocolError("decoded runtime status has the wrong type")
        if self._last_runtime_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_runtime_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        ):
            raise OperatorProtocolError("runtime status sequence is not newer")
        self._last_runtime_status_sequence = status.sequence
        return status

    def acknowledge_runtime_status(
        self,
        status: RuntimeStatusMessage,
        *,
        sent_at_s: float | None = None,
    ) -> RuntimeStatusAck:
        """Send the same canonical type-24 proof used by QGC diagnostics."""

        if self._last_runtime_status_sequence != status.sequence:
            raise RuntimeError("runtime status must be freshly received before acknowledgement")
        self._runtime_status_ack_sequence = self._status_replay_state.next_sequence(
            f"acknowledgement:{self.remote[0]}:{self.remote[1]}",
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
        )
        acknowledgement = RuntimeStatusAck(
            acknowledged_runtime_sequence=status.sequence,
            run_hash64=status.run_hash64,
        )
        self._channel.send(
            self.mavlink.encode_runtime_status_ack(
                acknowledgement,
                sequence=self._runtime_status_ack_sequence,
                sent_at_s=time.time() if sent_at_s is None else sent_at_s,
            )
        )
        return acknowledgement

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
        if self._last_approach_challenge_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_approach_challenge_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_approach_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_approach_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_payload_target_challenge_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_payload_target_challenge_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_payload_target_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_payload_target_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_target_pool_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_target_pool_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        if self._last_scene_context_status_sequence is not None and not serial_is_newer(
            status.sequence,
            self._last_scene_context_status_sequence,
            max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
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
        packet = self.mavlink.codec.decode(inner)
        if packet.message_type in _STATUS_MESSAGE_TYPES:
            now_s = time.time()
            age_s = now_s - packet.sent_at_s
            if age_s > self.status_max_age_s:
                raise _StatusFreshnessError(
                    packet.message_type,
                    "status packet is stale and cannot be shown as current"
                )
            if age_s < -self.status_future_tolerance_s:
                raise _StatusFreshnessError(
                    packet.message_type,
                    "status packet is dated in the future",
                )
            self._last_status_stale = False
            producer_epoch = getattr(packet.message, "producer_epoch", None)
            epoch_suffix = "" if producer_epoch is None else f":{producer_epoch}"
            key = (
                f"status:{self.remote[0]}:{self.remote[1]}:"
                f"{packet.message_type.name}{epoch_suffix}"
            )
            previous = self._status_replay_state.sequences.get(key)
            if previous is not None and not serial_is_newer(
                packet.sequence,
                previous,
                max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
            ):
                raise OperatorProtocolError(
                    "status sequence is not newer than persisted high-water"
                )
            if not self._status_replay_state.accept_sequence(
                key,
                packet.sequence,
                max_forward_jump=DEFAULT_MAX_SEQUENCE_FORWARD_JUMP,
            ):
                raise OperatorProtocolError("status sequence replay was rejected")
        return inner, packet

    @property
    def status_stale_count(self) -> int:
        return self._status_stale_count

    @property
    def status_future_count(self) -> int:
        return self._status_future_count

    @property
    def last_status_stale(self) -> bool:
        return self._last_status_stale

    def _note_status_freshness_error(self, error: _StatusFreshnessError | None = None) -> None:
        if error is not None and "future" in str(error):
            self._status_future_count += 1
            self._last_status_stale = False
        else:
            self._status_stale_count += 1
            self._last_status_stale = True

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
            try:
                _inner, packet = self._receive_packet(timeout_s=remaining_s)
            except _StatusFreshnessError as exc:
                self._note_status_freshness_error(exc)
                if exc.message_type is expected_type:
                    raise OperatorProtocolError(str(exc)) from exc
                continue
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
    "UdpRuntimeStatusAckReceipt",
    "UdpRuntimeStatusSendReceipt",
    "UdpSelectionReceipt",
]
