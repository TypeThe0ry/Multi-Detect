from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .domain import TrackSnapshot
from .operator_link import TargetSelectionCommand, TrackStatusMessage
from .operator_tracking import OperatorTargetLock

OperatorPeer = tuple[str, int]


class OperatorStatusTransport(Protocol):
    def start_background(self) -> None: ...

    def poll_selection(self) -> tuple[TargetSelectionCommand, OperatorPeer] | None: ...

    def poll_error(self) -> Exception | None: ...

    def publish_track_status(
        self,
        status: TrackStatusMessage,
        *,
        peer: OperatorPeer,
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class OperatorBridgeResult:
    accepted_command_count: int
    published_statuses: tuple[TrackStatusMessage, ...]
    transport_errors: tuple[str, ...]


class LiveOperatorBridge:
    """Joins remote selection metadata to tracker snapshots without mission-control access."""

    def __init__(
        self,
        transport: OperatorStatusTransport,
        target_lock: OperatorTargetLock,
        *,
        maximum_commands_per_frame: int = 8,
        maximum_errors_per_frame: int = 8,
    ) -> None:
        if maximum_commands_per_frame <= 0 or maximum_errors_per_frame <= 0:
            raise ValueError("operator bridge per-frame limits must be positive")
        self.transport = transport
        self.target_lock = target_lock
        self.maximum_commands_per_frame = maximum_commands_per_frame
        self.maximum_errors_per_frame = maximum_errors_per_frame
        self._active_peer: OperatorPeer | None = None
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
            status = self.target_lock.apply_command(
                command,
                tracks=tracks,
                frame_id=frame_id,
                now_s=produced_at_s,
            )
            if self._publish(status, peer, errors):
                statuses.append(status)
        if accepted_commands == 0 and self._active_peer is not None:
            status = self.target_lock.update(
                tracks=tracks,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
            )
            if status is not None and self._publish(status, self._active_peer, errors):
                statuses.append(status)
        return OperatorBridgeResult(
            accepted_commands,
            tuple(statuses),
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

    def _drain_errors(self) -> list[str]:
        errors: list[str] = []
        for _ in range(self.maximum_errors_per_frame):
            error = self.transport.poll_error()
            if error is None:
                break
            errors.append(type(error).__name__)
        return errors


__all__ = [
    "LiveOperatorBridge",
    "OperatorBridgeResult",
    "OperatorPeer",
    "OperatorStatusTransport",
]
