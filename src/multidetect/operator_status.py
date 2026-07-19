from __future__ import annotations

from collections.abc import Collection, Mapping
from uuid import uuid4

from .approach_hil import ApproachHilAssessment, SlideConfirmationChallenge
from .domain import AuthorizationChallenge, DeploymentWindowSolution, MissionPhase
from .mission import MissionStatus, ObservationOutcome
from .multimodal_ranging import RangeSolution
from .operator_link import (
    ApproachChallengeStatusMessage,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    SafetyStatusMessage,
    SceneContextRegionEntry,
    SceneContextState,
    SceneContextStatusMessage,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    operator_identifier_token,
)
from .patrol_advisory import PatrolModeAssessment
from .payload_target_gate import (
    PayloadSlideChallenge,
    PayloadSlideGrant,
    PayloadTargetResolution,
)
from .semantic_environment import SemanticContextSnapshot
from .semantic_environment import SemanticContextState as ModelContextState
from .unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState

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


def build_patrol_status_message(
    *,
    mission_id: str,
    sequence: int,
    assessment: PatrolModeAssessment,
    tracks: tuple[UnifiedTrackSnapshot, ...],
    source_frame_id: str,
    source_captured_at_s: float,
    produced_at_s: float,
) -> PatrolStatusMessage:
    """Build compact mode-1 metadata without route, attitude or actuator commands."""

    primary = next(
        (
            track
            for track in tracks
            if assessment.primary_target_id is not None
            and track.track_id == assessment.primary_target_id
        ),
        None,
    )
    advisory = assessment.return_to_observe
    return PatrolStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        mission_id=mission_id,
        phase=assessment.phase,
        primary_target_id=assessment.primary_target_id,
        target_state=assessment.target_state,
        bbox=primary.bbox if primary is not None else None,
        label=primary.label if primary is not None else None,
        confidence=primary.confidence if primary is not None else None,
        tracking_quality=primary.tracking_quality if primary is not None else None,
        total_track_count=len(tracks),
        locked_track_count=sum(track.locked for track in tracks),
        source_frame_id=source_frame_id,
        source_captured_at_s=source_captured_at_s,
        produced_at_s=produced_at_s,
        return_direction=advisory.direction if advisory is not None else None,
        return_validity=advisory.validity if advisory is not None else None,
        return_evidence_age_s=advisory.evidence_age_s if advisory is not None else None,
        estimated_minimum_turn_radius_m=(
            advisory.estimated_minimum_turn_radius_m if advisory is not None else None
        ),
    )


def build_range_status_message(
    *,
    sequence: int,
    solution: RangeSolution,
    source_captured_at_s: float,
) -> RangeStatusMessage:
    """Build complete read-only range metadata for authenticated operator transport."""

    return RangeStatusMessage(
        status_id=str(uuid4()),
        sequence=sequence,
        target_id=solution.target_id,
        calibration_id=solution.calibration_id,
        source_frame_id=solution.frame_id,
        source_captured_at_s=source_captured_at_s,
        produced_at_s=solution.evaluated_at_s,
        validity=solution.validity,
        reasons=solution.reasons,
        sources=solution.sources,
        rejected_sources=solution.rejected_sources,
        slant_range_m=solution.slant_range_m,
        ground_range_m=solution.ground_range_m,
        slant_range_ci95_m=solution.slant_range_ci95_m,
        ground_range_ci95_m=solution.ground_range_ci95_m,
        relative_bearing_deg=solution.relative_bearing_deg,
        absolute_bearing_deg=solution.absolute_bearing_deg,
        bearing_sigma_deg=solution.bearing_sigma_deg,
        north_offset_m=solution.north_offset_m,
        east_offset_m=solution.east_offset_m,
        data_freshness_s=solution.data_freshness_s,
        sensor_consistency=solution.sensor_consistency,
    )


def build_release_status_message(
    *,
    sequence: int,
    solution: DeploymentWindowSolution,
) -> ReleaseStatusMessage:
    """Build authenticated, display-only Mode-2 impact and timing advice."""

    return ReleaseStatusMessage(
        sequence=sequence,
        target_id=solution.target_id,
        calibration_id=solution.calibration_id,
        produced_at_s=solution.evaluated_at_s,
        timing_status=solution.timing_status,
        reasons=solution.reasons,
        range_target_id=solution.range_target_id,
        range_frame_id=solution.range_frame_id,
        target_north_offset_m=solution.target_north_offset_m,
        target_east_offset_m=solution.target_east_offset_m,
        impact_north_offset_m=solution.impact_north_offset_m,
        impact_east_offset_m=solution.impact_east_offset_m,
        along_track_error_m=solution.along_track_error_m,
        cross_track_error_m=solution.cross_track_error_m,
        error_ellipse_major_m=solution.error_ellipse_major_m,
        error_ellipse_minor_m=solution.error_ellipse_minor_m,
        error_ellipse_orientation_deg=solution.error_ellipse_orientation_deg,
        estimated_ground_range_m=solution.estimated_ground_range_m,
        ground_range_ci95_m=solution.ground_range_ci95_m,
        payload_descent_time_s=solution.payload_descent_time_s,
        release_lead_distance_m=solution.release_lead_distance_m,
        range_sensor_consistency=solution.range_sensor_consistency,
    )


def build_approach_challenge_status_message(
    *,
    challenge: SlideConfirmationChallenge,
    selection_command_id: str,
    sequence: int,
    produced_at_s: float,
    challenge_clock_now_s: float,
) -> ApproachChallengeStatusMessage:
    """Tokenize a Mode-3 slide challenge and translate monotonic time to wire time."""

    clock_offset_s = produced_at_s - challenge_clock_now_s
    return ApproachChallengeStatusMessage(
        challenge_token=operator_identifier_token(challenge.token),
        target_token=operator_identifier_token(challenge.target_id),
        target_revision=challenge.target_revision,
        selection_command_id=selection_command_id,
        issued_at_s=challenge.issued_at_s + clock_offset_s,
        expires_at_s=challenge.expires_at_s + clock_offset_s,
        sequence=sequence,
        produced_at_s=produced_at_s,
    )


def build_payload_target_challenge_status_message(
    *,
    challenge: PayloadSlideChallenge,
    sequence: int,
    produced_at_s: float,
    challenge_clock_now_s: float,
) -> PayloadTargetChallengeStatusMessage:
    """Tokenize a Mode-2 challenge binding both selected object and fire aimpoint."""

    clock_offset_s = produced_at_s - challenge_clock_now_s
    return PayloadTargetChallengeStatusMessage(
        challenge_token=operator_identifier_token(challenge.token),
        selected_target_token=operator_identifier_token(challenge.selected_target_id),
        selected_target_revision=challenge.selected_target_revision,
        aimpoint_target_token=operator_identifier_token(challenge.aimpoint_target_id),
        aimpoint_target_revision=challenge.aimpoint_target_revision,
        selection_command_id=challenge.selection_command_id,
        issued_at_s=challenge.issued_at_s + clock_offset_s,
        expires_at_s=challenge.expires_at_s + clock_offset_s,
        sequence=sequence,
        produced_at_s=produced_at_s,
    )


def build_payload_target_status_message(
    *,
    resolution: PayloadTargetResolution,
    challenge: PayloadSlideChallenge | None,
    grant: PayloadSlideGrant | None,
    sequence: int,
    produced_at_s: float,
    resolution_clock_now_s: float,
) -> PayloadTargetStatusMessage:
    """Build Mode-2 eligibility and confirmation metadata in the operator clock domain."""

    confirmation = grant if grant is not None else challenge
    clock_offset_s = produced_at_s - resolution_clock_now_s
    return PayloadTargetStatusMessage(
        sequence=sequence,
        selection_command_id=resolution.selection_command_id,
        selected_target_token=operator_identifier_token(resolution.selected_target_id),
        selected_target_revision=resolution.selected_target_revision,
        eligibility=resolution.eligibility,
        produced_at_s=produced_at_s,
        aimpoint_target_token=(
            operator_identifier_token(str(resolution.aimpoint_target_id))
            if resolution.aimpoint_target_id is not None
            else None
        ),
        aimpoint_target_revision=resolution.aimpoint_target_revision,
        confirmation_pending=challenge is not None and grant is None,
        confirmation_accepted=grant is not None,
        confirmation_expires_at_s=(
            confirmation.expires_at_s + clock_offset_s if confirmation is not None else None
        ),
    )


def build_approach_status_message(
    *,
    assessment: ApproachHilAssessment,
    sequence: int,
    produced_at_s: float,
    assessment_clock_now_s: float,
    flight_control_enabled: bool = False,
) -> ApproachStatusMessage:
    """Build Mode-3 state with explicit Jetson control-authority metadata."""

    clock_offset_s = produced_at_s - assessment_clock_now_s
    return ApproachStatusMessage(
        sequence=sequence,
        target_id=assessment.target_id,
        target_revision=assessment.target_revision,
        phase=assessment.phase,
        reasons=tuple(
            "fixed_wing_aim_active" if reason == "centering_advice_only" else reason
            for reason in assessment.reasons
        )
        if flight_control_enabled
        else assessment.reasons,
        produced_at_s=produced_at_s,
        yaw_error_deg=assessment.yaw_error_deg,
        pitch_error_deg=assessment.pitch_error_deg,
        yaw_advice_deg=assessment.yaw_advice_deg,
        pitch_advice_deg=assessment.pitch_advice_deg,
        bank_advice_deg=assessment.bank_advice_deg,
        climb_pitch_advice_deg=assessment.climb_pitch_advice_deg,
        ground_range_m=assessment.ground_range_m,
        confirmation_expires_at_s=(
            assessment.confirmation_expires_at_s + clock_offset_s
            if assessment.confirmation_expires_at_s is not None
            else None
        ),
        advisory_only=not flight_control_enabled,
        sitl_hil_only=not flight_control_enabled,
        flight_control_enabled=flight_control_enabled,
    )


def build_target_pool_status_messages(
    *,
    sequence_start: int,
    pool_revision: int,
    tracks: tuple[UnifiedTrackSnapshot, ...],
    produced_at_s: float,
    include_tentative: bool = True,
    operator_tracked_ids: Collection[str] = (),
    relative_bearing_by_target_id: Mapping[str, float] | None = None,
    estimated_range_by_target_id: Mapping[str, float] | None = None,
    target_speed_by_target_id: Mapping[str, float] | None = None,
) -> tuple[TargetPoolStatusMessage, ...]:
    """Build deterministic pages; stable typed targets lead generic fallback tracks."""

    if not isinstance(include_tentative, bool):
        raise ValueError("include_tentative must be a boolean")
    tracked_ids = frozenset(operator_tracked_ids)
    bearing_by_id = relative_bearing_by_target_id or {}
    range_by_id = estimated_range_by_target_id or {}
    speed_by_id = target_speed_by_target_id or {}
    # LOST tracks stay in the onboard bank for bounded reacquisition, but they are
    # not current click targets. Publishing their last boxes creates stale "+"
    # markers in QGC and lets the operator select empty image regions.
    visible_tracks = tuple(
        track
        for track in tracks
        if track.state is not UnifiedTrackState.LOST
        and (include_tentative or track.state is not UnifiedTrackState.DETECTED)
    )

    if not visible_tracks:
        return (
            TargetPoolStatusMessage(
                sequence=sequence_start,
                pool_revision=pool_revision,
                page_index=0,
                page_count=1,
                total_track_count=0,
                entries=(),
                produced_at_s=produced_at_s,
            ),
        )
    ordered = sorted(
        visible_tracks,
        key=lambda item: (
            not item.primary,
            not item.locked,
            not item.actionable,
            _target_label_priority(item.label),
            -item.tracking_quality,
            item.track_id,
        ),
    )
    if len(ordered) > 255:
        raise ValueError("target-pool wire status supports at most 255 tracks")
    pages = [ordered[index : index + 2] for index in range(0, len(ordered), 2)]
    messages: list[TargetPoolStatusMessage] = []
    for page_index, page in enumerate(pages):
        entries = tuple(
            TargetPoolEntry(
                target_id=track.track_id,
                state=track.state,
                label=track.label,
                confidence=track.confidence,
                tracking_quality=track.tracking_quality,
                locked=track.locked,
                primary=track.primary,
                actionable=track.actionable,
                reid_confirmed=track.reid_confirmed,
                operator_tracked=track.track_id in tracked_ids,
                bbox=track.bbox,
                relative_bearing_deg=bearing_by_id.get(track.track_id),
                estimated_range_m=range_by_id.get(track.track_id),
                target_speed_mps=speed_by_id.get(track.track_id),
            )
            for track in page
        )
        messages.append(
            TargetPoolStatusMessage(
                sequence=(sequence_start + page_index) & 0xFFFFFFFF,
                pool_revision=pool_revision,
                page_index=page_index,
                page_count=len(pages),
                total_track_count=len(ordered),
                entries=entries,
                produced_at_s=produced_at_s,
            )
        )
    return tuple(messages)


def _target_label_priority(label: str) -> int:
    normalized = label.strip().lower()
    if normalized in {"fire", "flame", "smoke", "smoldering_area", "burned_area"}:
        return 0
    if normalized in {"person", "firefighter"}:
        return 1
    if normalized in {
        "vehicle",
        "car",
        "truck",
        "bus",
        "motorcycle",
        "bicycle",
        "train",
        "boat",
    }:
        return 2
    if normalized in {"power_line", "flammable_tank", "building", "road"}:
        return 3
    return 4


def build_scene_context_status_messages(
    *,
    sequence_start: int,
    context_revision: int,
    snapshot: SemanticContextSnapshot,
    produced_at_s: float,
    maximum_age_s: float,
) -> tuple[SceneContextStatusMessage, ...]:
    """Build confidence-free road/building pages; stale or invalid evidence clears QGC."""

    age_s = produced_at_s - snapshot.produced_at_s
    if age_s < 0.0 or age_s > maximum_age_s:
        state = SceneContextState.STALE
        regions = ()
    elif snapshot.state is not ModelContextState.VALID:
        state = SceneContextState.INVALID
        regions = ()
    else:
        state = SceneContextState.VALID
        regions = snapshot.regions
    if len(regions) > 255:
        raise ValueError("scene-context wire status supports at most 255 regions")
    pages = [regions[index : index + 2] for index in range(0, len(regions), 2)] or [()]
    return tuple(
        SceneContextStatusMessage(
            sequence=(sequence_start + page_index) & 0xFFFFFFFF,
            context_revision=context_revision,
            source_frame_id=snapshot.frame_id,
            source_captured_at_s=snapshot.captured_at_s,
            state=state,
            page_index=page_index,
            page_count=len(pages),
            total_region_count=len(regions),
            entries=tuple(
                SceneContextRegionEntry(
                    label=region.label,
                    bbox=region.bbox,
                    frame_area_fraction=region.frame_area_fraction,
                    bbox_fill_fraction=region.bbox_fill_fraction,
                )
                for region in page
            ),
            produced_at_s=produced_at_s,
        )
        for page_index, page in enumerate(pages)
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
    "build_approach_challenge_status_message",
    "build_approach_status_message",
    "build_mission_status_message",
    "build_patrol_status_message",
    "build_payload_target_challenge_status_message",
    "build_payload_target_status_message",
    "build_range_status_message",
    "build_release_status_message",
    "build_safety_status_message",
    "build_scene_context_status_messages",
    "build_target_pool_status_messages",
]
