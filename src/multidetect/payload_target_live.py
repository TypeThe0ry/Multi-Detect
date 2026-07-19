from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace

from .domain import TrackSnapshot
from .operator_link import (
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetStatusMessage,
    operator_identifier_token,
)
from .operator_status import (
    build_payload_target_challenge_status_message,
    build_payload_target_status_message,
)
from .payload_target_gate import (
    PayloadSlideConfirmationController,
    PayloadSlideGrant,
    PayloadTargetIntent,
    PayloadTargetResolution,
    PayloadTargetResolver,
)
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


@dataclass(frozen=True, slots=True)
class LivePayloadTargetFrame:
    resolution: PayloadTargetResolution | None
    challenge: PayloadTargetChallengeStatusMessage | None
    status: PayloadTargetStatusMessage | None
    intent: PayloadTargetIntent | None


class LivePayloadTargetCoordinator:
    """Bind Mode-2 operator selection to one qualified fire aimpoint.

    This coordinator emits metadata and a short-lived authorization prerequisite. It
    has no actuator, mission-upload, setpoint, mode-change or payload-release API.
    """

    def __init__(
        self,
        *,
        resolver: PayloadTargetResolver | None = None,
        confirmation: PayloadSlideConfirmationController | None = None,
    ) -> None:
        self.resolver = resolver or PayloadTargetResolver()
        self.confirmation = confirmation or PayloadSlideConfirmationController(self.resolver.config)
        if self.confirmation.config != self.resolver.config:
            raise ValueError("payload resolver and confirmation must share one gate config")
        self._binding: tuple[str, str, int, str, int] | None = None
        self._resolution: PayloadTargetResolution | None = None
        self._grant: PayloadSlideGrant | None = None
        self._challenge_sequence = 0
        self._status_sequence = 0

    @property
    def resolution(self) -> PayloadTargetResolution | None:
        return self._resolution

    def clear(self) -> None:
        self.confirmation.clear()
        self._binding = None
        self._resolution = None
        self._grant = None

    def active_intent(
        self,
        *,
        selection_command_id: str | None,
        track: UnifiedTrackSnapshot | None,
        now_s: float,
    ) -> PayloadTargetIntent | None:
        """Return a still-valid prerequisite before mission evaluation, or fail closed."""

        self._require_time(now_s, "Mode-2 intent time")
        resolution = self._resolution
        grant = self._grant
        if (
            resolution is None
            or grant is None
            or selection_command_id is None
            or track is None
            or selection_command_id != resolution.selection_command_id
            or track.track_id != resolution.selected_target_id
            or self._selected_revision(selection_command_id, track.track_id)
            != resolution.selected_target_revision
            or not self._track_stable(track, now_s)
            or not self.confirmation.grant_valid(grant, resolution, now_s=now_s)
        ):
            self._grant = None
            return None
        return self._intent(grant)

    def prepare_frame(
        self,
        *,
        selection_command_id: str | None,
        selected: UnifiedTrackSnapshot | None,
        fire_tracks: Sequence[TrackSnapshot],
        now_s: float,
        wire_now_s: float,
    ) -> LivePayloadTargetFrame:
        self._require_time(now_s, "Mode-2 monotonic time")
        self._require_time(wire_now_s, "Mode-2 wire time")
        if selection_command_id is None or not selection_command_id.strip() or selected is None:
            if self._binding is not None:
                self.clear()
            return LivePayloadTargetFrame(None, None, None, None)

        selected_revision = self._selected_revision(selection_command_id, selected.track_id)
        resolution = self.resolver.resolve(
            selection_command_id=selection_command_id,
            selected_target_revision=selected_revision,
            selected=selected,
            fire_tracks=fire_tracks,
            now_s=now_s,
        )
        if resolution.eligible:
            # Mission tracker revisions advance every observation. The operator binding
            # instead changes only if selection identity or resolved fire identity changes.
            resolution = replace(
                resolution,
                aimpoint_target_revision=self._aimpoint_revision(
                    selection_command_id,
                    selected.track_id,
                    str(resolution.aimpoint_target_id),
                ),
            )
            binding = (
                selection_command_id,
                selected.track_id,
                selected_revision,
                str(resolution.aimpoint_target_id),
                int(resolution.aimpoint_target_revision),
            )
        else:
            binding = None

        if binding != self._binding:
            self.confirmation.clear()
            self._grant = None
            self._binding = binding
        self._resolution = resolution

        if self._grant is not None and not self.confirmation.grant_valid(
            self._grant, resolution, now_s=now_s
        ):
            self._grant = None
        challenge = self.confirmation.active_challenge
        if not resolution.eligible:
            self.confirmation.clear()
            challenge = None
        elif self._grant is None and (challenge is None or now_s >= challenge.expires_at_s):
            challenge = self.confirmation.issue(resolution, now_s=now_s)

        challenge_message = None
        if challenge is not None and self._grant is None and now_s < challenge.expires_at_s:
            self._challenge_sequence = (self._challenge_sequence + 1) & 0xFFFFFFFF
            challenge_message = build_payload_target_challenge_status_message(
                challenge=challenge,
                sequence=self._challenge_sequence,
                produced_at_s=wire_now_s,
                challenge_clock_now_s=now_s,
            )
        self._status_sequence = (self._status_sequence + 1) & 0xFFFFFFFF
        status = build_payload_target_status_message(
            resolution=resolution,
            challenge=challenge if self._grant is None else None,
            grant=self._grant,
            sequence=self._status_sequence,
            produced_at_s=wire_now_s,
            resolution_clock_now_s=now_s,
        )
        intent = self._intent(self._grant) if self._grant is not None else None
        return LivePayloadTargetFrame(resolution, challenge_message, status, intent)

    def consume_confirmation(
        self,
        command: PayloadTargetConfirmationCommand,
        *,
        now_s: float,
    ) -> bool:
        self._require_time(now_s, "Mode-2 confirmation receipt time")
        resolution = self._resolution
        challenge = self.confirmation.active_challenge
        if resolution is None or challenge is None or now_s >= challenge.expires_at_s:
            return False
        if (
            command.selection_command_id != challenge.selection_command_id
            or command.challenge_token != operator_identifier_token(challenge.token)
            or command.selected_target_token
            != operator_identifier_token(challenge.selected_target_id)
            or command.selected_target_revision != challenge.selected_target_revision
            or command.aimpoint_target_token
            != operator_identifier_token(challenge.aimpoint_target_id)
            or command.aimpoint_target_revision != challenge.aimpoint_target_revision
        ):
            return False
        completed_at_s = now_s
        grant = self.confirmation.accept(
            token=challenge.token,
            resolution=resolution,
            slide_started_at_s=completed_at_s - command.slide_duration_s,
            slide_completed_at_s=completed_at_s,
            completion_fraction=command.completion_fraction,
            continuous=command.continuous,
        )
        self._grant = grant
        return grant is not None

    @staticmethod
    def _selected_revision(selection_command_id: str, selected_target_id: str) -> int:
        return LivePayloadTargetCoordinator._revision(
            "selected", selection_command_id, selected_target_id
        )

    @staticmethod
    def _aimpoint_revision(
        selection_command_id: str,
        selected_target_id: str,
        aimpoint_target_id: str,
    ) -> int:
        return LivePayloadTargetCoordinator._revision(
            "aimpoint", selection_command_id, selected_target_id, aimpoint_target_id
        )

    @staticmethod
    def _revision(*parts: str) -> int:
        digest = hashlib.sha256("\0".join(parts).encode()).digest()
        return int.from_bytes(digest[:4], "big")

    def _track_stable(self, track: UnifiedTrackSnapshot, now_s: float) -> bool:
        return bool(
            track.state
            in {
                UnifiedTrackState.LOCKED,
                UnifiedTrackState.TRACKING,
                UnifiedTrackState.RECOVERED,
            }
            and track.locked
            and track.primary
            and track.actionable
            and now_s - track.last_seen_at_s <= self.resolver.config.maximum_evidence_age_s
        )

    @staticmethod
    def _intent(grant: PayloadSlideGrant) -> PayloadTargetIntent:
        return PayloadTargetIntent(
            selection_command_id=grant.selection_command_id,
            selected_target_id=grant.selected_target_id,
            selected_target_revision=grant.selected_target_revision,
            aimpoint_target_id=grant.aimpoint_target_id,
            aimpoint_target_revision=grant.aimpoint_target_revision,
            accepted_at_s=grant.accepted_at_s,
            expires_at_s=grant.expires_at_s,
        )

    @staticmethod
    def _require_time(value: float, name: str) -> None:
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")


__all__ = ["LivePayloadTargetCoordinator", "LivePayloadTargetFrame"]
