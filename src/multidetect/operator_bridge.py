from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from .domain import TrackSnapshot
from .operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDecisionCommand,
    MissionStatusMessage,
    SafetyStatusMessage,
    TargetSelectionCommand,
    TrackStatusMessage,
)
from .operator_tracking import OperatorTargetLock

OperatorPeer = tuple[str, int]


class OperatorStatusTransport(Protocol):
    def start_background(self) -> None: ...

    def poll_selection(self) -> tuple[TargetSelectionCommand, OperatorPeer] | None: ...

    def poll_authorization_decision(
        self,
    ) -> tuple[AuthorizationDecisionCommand, OperatorPeer] | None: ...

    def set_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage | None,
    ) -> None: ...

    def poll_error(self) -> Exception | None: ...

    def publish_track_status(
        self,
        status: TrackStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_mission_status(
        self,
        status: MissionStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_safety_status(
        self,
        status: SafetyStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def publish_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OperatorBridgeResult:
    accepted_command_count: int
    published_statuses: tuple[TrackStatusMessage, ...]
    published_mission_statuses: tuple[MissionStatusMessage, ...]
    published_safety_statuses: tuple[SafetyStatusMessage, ...]
    accepted_authorization_decisions: tuple[tuple[AuthorizationDecisionCommand, OperatorPeer], ...]
    published_authorization_challenges: tuple[AuthorizationChallengeStatusMessage, ...]
    transport_errors: tuple[str, ...]


class LiveOperatorBridge:
    """Join remote selection/status and bounded authorization metadata, never actuator control."""

    def __init__(
        self,
        transport: OperatorStatusTransport,
        target_lock: OperatorTargetLock,
        *,
        maximum_commands_per_frame: int = 8,
        maximum_errors_per_frame: int = 8,
        mission_status_heartbeat_s: float = 1.0,
    ) -> None:
        if maximum_commands_per_frame <= 0 or maximum_errors_per_frame <= 0:
            raise ValueError("operator bridge per-frame limits must be positive")
        if not isfinite(mission_status_heartbeat_s) or mission_status_heartbeat_s <= 0.0:
            raise ValueError("mission status heartbeat must be finite and positive")
        self.transport = transport
        self.target_lock = target_lock
        self.maximum_commands_per_frame = maximum_commands_per_frame
        self.maximum_errors_per_frame = maximum_errors_per_frame
        self.mission_status_heartbeat_s = mission_status_heartbeat_s
        self._active_peer: OperatorPeer | None = None
        self._last_mission_status_fingerprint: tuple[object, ...] | None = None
        self._last_mission_status_at_s: float | None = None
        self._last_safety_status_fingerprint: tuple[object, ...] | None = None
        self._last_safety_status_at_s: float | None = None
        self._last_authorization_challenge_fingerprint: tuple[object, ...] | None = None
        self._last_authorization_challenge_at_s: float | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("live operator bridge is already started")
        self.transport.start_background()
        self._started = True

    def process_frame(
        self,
        *,
        tracks: tuple[TrackSnapshot, ...],
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
        mission_status: MissionStatusMessage | None = None,
        safety_status: SafetyStatusMessage | None = None,
        authorization_challenge: AuthorizationChallengeStatusMessage | None = None,
    ) -> OperatorBridgeResult:
        if not self._started:
            raise RuntimeError("live operator bridge has not been started")
        errors = self._drain_errors()
        statuses: list[TrackStatusMessage] = []
        accepted_commands = 0
        for _ in range(self.maximum_commands_per_frame):
            queued = self.transport.poll_selection()
            if queued is None:
                break
            command, peer = queued
            accepted_commands += 1
            self._active_peer = peer
            self._last_mission_status_fingerprint = None
            self._last_mission_status_at_s = None
            self._last_safety_status_fingerprint = None
            self._last_safety_status_at_s = None
            self._last_authorization_challenge_fingerprint = None
            self._last_authorization_challenge_at_s = None
            status = self.target_lock.apply_command(
                command,
                tracks=tracks,
                frame_id=frame_id,
                now_s=produced_at_s,
            )
            if self._publish(status, peer, errors):
                statuses.append(status)
        set_challenge = getattr(self.transport, "set_authorization_challenge", None)
        if callable(set_challenge) and authorization_challenge is None:
            try:
                set_challenge(None)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors.append(type(exc).__name__)
        accepted_authorization_decisions: list[
            tuple[AuthorizationDecisionCommand, OperatorPeer]
        ] = []
        poll_decision = getattr(self.transport, "poll_authorization_decision", None)
        if callable(poll_decision):
            for _ in range(self.maximum_commands_per_frame):
                queued_decision = poll_decision()
                if queued_decision is None:
                    break
                command, peer = queued_decision
                if self._active_peer is None or peer != self._active_peer:
                    errors.append("AuthorizationPeerMismatch")
                    continue
                accepted_authorization_decisions.append((command, peer))
        if accepted_commands == 0 and self._active_peer is not None:
            status = self.target_lock.update(
                tracks=tracks,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
            )
            if status is not None and self._publish(status, self._active_peer, errors):
                statuses.append(status)
        published_mission_statuses: tuple[MissionStatusMessage, ...] = ()
        if (
            mission_status is not None
            and self._active_peer is not None
            and self._mission_status_due(mission_status)
            and self._publish_mission_status(mission_status, self._active_peer, errors)
        ):
            published_mission_statuses = (mission_status,)
            self._last_mission_status_fingerprint = self._mission_status_fingerprint(mission_status)
            self._last_mission_status_at_s = mission_status.produced_at_s
        published_safety_statuses: tuple[SafetyStatusMessage, ...] = ()
        if (
            safety_status is not None
            and self._active_peer is not None
            and self._safety_status_due(safety_status)
            and self._publish_safety_status(safety_status, self._active_peer, errors)
        ):
            published_safety_statuses = (safety_status,)
            self._last_safety_status_fingerprint = self._safety_status_fingerprint(safety_status)
            self._last_safety_status_at_s = safety_status.produced_at_s
        published_authorization_challenges: tuple[AuthorizationChallengeStatusMessage, ...] = ()
        if (
            authorization_challenge is not None
            and self._active_peer is not None
            and self._authorization_challenge_due(authorization_challenge)
            and self._publish_authorization_challenge(
                authorization_challenge,
                self._active_peer,
                errors,
            )
        ):
            published_authorization_challenges = (authorization_challenge,)
            self._last_authorization_challenge_fingerprint = (
                self._authorization_challenge_fingerprint(authorization_challenge)
            )
            self._last_authorization_challenge_at_s = authorization_challenge.produced_at_s
            if callable(set_challenge):
                try:
                    set_challenge(authorization_challenge)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    errors.append(type(exc).__name__)
        return OperatorBridgeResult(
            accepted_commands,
            tuple(statuses),
            published_mission_statuses,
            published_safety_statuses,
            tuple(accepted_authorization_decisions),
            published_authorization_challenges,
            tuple(errors),
        )

    def close(self) -> None:
        if self._started:
            self.transport.close()
            self._started = False

    def _publish(
        self,
        status: TrackStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        try:
            self.transport.publish_track_status(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_mission_status(
        self,
        status: MissionStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        try:
            self.transport.publish_mission_status(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_safety_status(
        self,
        status: SafetyStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        try:
            self.transport.publish_safety_status(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _publish_authorization_challenge(
        self,
        status: AuthorizationChallengeStatusMessage,
        peer: OperatorPeer,
        errors: list[str],
    ) -> bool:
        publish = getattr(self.transport, "publish_authorization_challenge", None)
        if not callable(publish):
            return False
        try:
            publish(status, peer=peer)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append(type(exc).__name__)
            return False
        return True

    def _drain_errors(self) -> list[str]:
        errors: list[str] = []
        for _ in range(self.maximum_errors_per_frame):
            error = self.transport.poll_error()
            if error is None:
                break
            errors.append(type(error).__name__)
        return errors

    def _mission_status_due(self, status: MissionStatusMessage) -> bool:
        fingerprint = self._mission_status_fingerprint(status)
        if fingerprint != self._last_mission_status_fingerprint:
            return True
        if self._last_mission_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_mission_status_at_s >= self.mission_status_heartbeat_s
        )

    def _safety_status_due(self, status: SafetyStatusMessage) -> bool:
        fingerprint = self._safety_status_fingerprint(status)
        if fingerprint != self._last_safety_status_fingerprint:
            return True
        if self._last_safety_status_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_safety_status_at_s >= self.mission_status_heartbeat_s
        )

    def _authorization_challenge_due(
        self,
        status: AuthorizationChallengeStatusMessage,
    ) -> bool:
        fingerprint = self._authorization_challenge_fingerprint(status)
        if fingerprint != self._last_authorization_challenge_fingerprint:
            return True
        if self._last_authorization_challenge_at_s is None:
            return True
        return (
            status.produced_at_s - self._last_authorization_challenge_at_s
            >= self.mission_status_heartbeat_s
        )

    @staticmethod
    def _mission_status_fingerprint(status: MissionStatusMessage) -> tuple[object, ...]:
        return (
            status.mission_id,
            status.phase,
            status.authorization_state,
            status.release_window,
            status.safety_allowed,
            status.remaining_payload_count,
            status.total_payload_count,
            status.target_id,
            status.active_payload_slot_id,
            _quantize(status.target_confidence, 254.0),
            _quantize(status.relative_bearing_deg, 100.0),
            _quantize(status.estimated_range_m, 10.0),
            _quantize(status.cross_track_error_m, 10.0),
            _quantize(status.along_track_error_m, 10.0),
            _quantize(status.release_lead_distance_m, 10.0),
        )

    @staticmethod
    def _safety_status_fingerprint(status: SafetyStatusMessage) -> tuple[object, ...]:
        return (
            status.mission_id,
            status.target_id,
            status.ruleset_version,
            tuple((check.rule_id, check.verdict) for check in status.checks),
        )

    @staticmethod
    def _authorization_challenge_fingerprint(
        status: AuthorizationChallengeStatusMessage,
    ) -> tuple[object, ...]:
        return (
            status.challenge_token,
            status.mission_token,
            status.target_token,
            status.scene_token,
            status.ruleset_token,
            status.payload_slot_token,
            status.target_revision,
            status.created_at_s,
            status.expires_at_s,
            status.pending,
        )


def _quantize(value: float | None, scale: float) -> int | None:
    return None if value is None else round(value * scale)


__all__ = [
    "LiveOperatorBridge",
    "OperatorBridgeResult",
    "OperatorPeer",
    "OperatorStatusTransport",
]
