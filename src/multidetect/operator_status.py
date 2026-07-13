from __future__ import annotations

from uuid import uuid4

from .domain import AuthorizationChallenge, MissionPhase
from .mission import MissionStatus, ObservationOutcome
from .operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDisplayState,
    MissionStatusMessage,
    SafetyStatusMessage,
    operator_identifier_token,
)

_AUTHORIZED_PHASES = frozenset(
    {
        MissionPhase.DEPLOYMENT_READY,
        MissionPhase.DEPLOYING,
        MissionPhase.VERIFYING_RELEASE,
        MissionPhase.EGRESS,
    }
)


def build_mission_status_message(
    *,
    mission_id: str,
    sequence: int,
    status: MissionStatus,
    outcome: ObservationOutcome,
    produced_at_s: float,
) -> MissionStatusMessage:
    """Build read-only G20 status from the current mission decision snapshot."""

    preferred_target_id, decision = _preferred_decision(status=status, outcome=outcome)
    target_id = decision.target_id if decision is not None else preferred_target_id
    track = next((item for item in outcome.tracks if item.track_id == target_id), None)
    window = decision.deployment_window if decision is not None else None

    if status.phase is MissionPhase.AWAITING_AUTHORIZATION:
        authorization = AuthorizationDisplayState.PENDING
    elif status.phase in _AUTHORIZED_PHASES and status.active_target_id is not None:
        authorization = AuthorizationDisplayState.APPROVED
    else:
        authorization = AuthorizationDisplayState.NONE

    return MissionStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        mission_id=mission_id,
        phase=status.phase,
        authorization_state=authorization,
        release_window=window.status if window is not None else None,
        safety_allowed=decision.allowed if decision is not None else None,
        remaining_payload_count=status.remaining_payload_count,
        total_payload_count=len(status.payload_slots),
        target_id=target_id,
        active_payload_slot_id=status.active_payload_slot_id,
        target_confidence=track.confidence_mean if track is not None else None,
        relative_bearing_deg=window.relative_bearing_deg if window is not None else None,
        estimated_range_m=window.estimated_ground_range_m if window is not None else None,
        cross_track_error_m=window.cross_track_error_m if window is not None else None,
        along_track_error_m=window.along_track_error_m if window is not None else None,
        release_lead_distance_m=(window.release_lead_distance_m if window is not None else None),
        produced_at_s=produced_at_s,
    )


def build_safety_status_message(
    *,
    mission_id: str,
    sequence: int,
    status: MissionStatus,
    outcome: ObservationOutcome,
    produced_at_s: float,
) -> SafetyStatusMessage | None:
    """Build explanatory rule masks only when a concrete decision exists."""

    _preferred_target_id, decision = _preferred_decision(status=status, outcome=outcome)
    if decision is None:
        return None
    return SafetyStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        mission_id=mission_id,
        target_id=decision.target_id,
        ruleset_version=decision.ruleset_version,
        checks=decision.checks,
        produced_at_s=produced_at_s,
    )


def build_authorization_challenge_status_message(
    *,
    challenge: AuthorizationChallenge,
    sequence: int,
    produced_at_s: float,
    challenge_clock_now_s: float | None = None,
) -> AuthorizationChallengeStatusMessage:
    """Tokenize one pending challenge for G20 without exposing its nonce."""

    clock_offset_s = 0.0 if challenge_clock_now_s is None else produced_at_s - challenge_clock_now_s

    return AuthorizationChallengeStatusMessage(
        challenge_token=operator_identifier_token(challenge.challenge_id),
        mission_token=operator_identifier_token(challenge.mission_id),
        target_token=operator_identifier_token(challenge.target_id),
        scene_token=operator_identifier_token(challenge.scene_digest),
        ruleset_token=operator_identifier_token(challenge.ruleset_version),
        payload_slot_token=operator_identifier_token(challenge.payload_slot_id),
        target_revision=challenge.target_revision,
        created_at_s=challenge.created_at_s + clock_offset_s,
        expires_at_s=challenge.expires_at_s + clock_offset_s,
        sequence=sequence,
        produced_at_s=produced_at_s,
    )


def _preferred_decision(
    *,
    status: MissionStatus,
    outcome: ObservationOutcome,
):
    preferred_target_id = status.active_target_id
    if preferred_target_id is None and outcome.challenge is not None:
        preferred_target_id = outcome.challenge.target_id
    decision = next(
        (
            item
            for item in outcome.decisions
            if preferred_target_id is not None and item.target_id == preferred_target_id
        ),
        None,
    )
    if decision is None and outcome.decisions:
        decision = max(outcome.decisions, key=lambda item: item.priority_score)
    return preferred_target_id, decision


__all__ = [
    "build_authorization_challenge_status_message",
    "build_mission_status_message",
    "build_safety_status_message",
]
