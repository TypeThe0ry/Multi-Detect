from __future__ import annotations

from dataclasses import replace

from multidetect.domain import BoundingBox, TrackSnapshot
from multidetect.operator_link import PayloadTargetConfirmationCommand
from multidetect.payload_target_gate import PayloadTargetEligibility
from multidetect.payload_target_live import LivePayloadTargetCoordinator
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState

SELECTION_ID = "11111111-1111-4111-8111-111111111111"


def _selected(label: str = "flame", **changes) -> UnifiedTrackSnapshot:
    box = BoundingBox(0.30, 0.30, 0.60, 0.65)
    values = dict(
        track_id="unified-selected",
        state=UnifiedTrackState.TRACKING,
        label=label,
        bbox=box,
        predicted_bbox=box,
        first_seen_at_s=8.0,
        last_seen_at_s=10.0,
        state_changed_at_s=9.0,
        observation_count=8,
        missed_frame_count=0,
        confidence=0.92,
        tracking_quality=0.91,
        velocity_x_s=0.0,
        velocity_y_s=0.0,
        appearance_sample_count=3,
        last_appearance_distance=0.1,
        reid_confirmed=True,
        locked=True,
        primary=True,
        actionable=True,
    )
    values.update(changes)
    return UnifiedTrackSnapshot(**values)


def _fire(*, revision: int = 7, last_seen_at_s: float = 10.0) -> TrackSnapshot:
    return TrackSnapshot(
        track_id="mission-fire-1",
        revision=revision,
        label="flame",
        bbox=BoundingBox(0.32, 0.32, 0.58, 0.62),
        first_seen_at_s=7.0,
        last_seen_at_s=last_seen_at_s,
        observation_count=8,
        consecutive_observations=8,
        confidence_floor=0.86,
        confidence_mean=0.91,
        maximum_gap_s=0.1,
        area_growth_rate=0.0,
        thermal_corroborated=False,
        confirmed=True,
        independent_rgb_corroborated=True,
    )


def _prepare(coordinator: LivePayloadTargetCoordinator, **changes):
    values = dict(
        selection_command_id=SELECTION_ID,
        selected=_selected(),
        fire_tracks=(_fire(),),
        now_s=10.1,
        wire_now_s=1000.1,
    )
    values.update(changes)
    return coordinator.prepare_frame(**values)


def _command(frame, **changes) -> PayloadTargetConfirmationCommand:
    assert frame.challenge is not None
    values = dict(
        command_token=303,
        session_token=404,
        challenge_token=frame.challenge.challenge_token,
        selected_target_token=frame.challenge.selected_target_token,
        selected_target_revision=frame.challenge.selected_target_revision,
        aimpoint_target_token=frame.challenge.aimpoint_target_token,
        aimpoint_target_revision=frame.challenge.aimpoint_target_revision,
        selection_command_id=SELECTION_ID,
        sequence=111,
        issued_at_s=1000.2,
        expires_at_s=1003.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    values.update(changes)
    return PayloadTargetConfirmationCommand(**values)


def test_live_payload_target_requires_selection_and_continuous_slide() -> None:
    coordinator = LivePayloadTargetCoordinator()
    first = _prepare(coordinator)

    assert first.resolution is not None
    assert first.resolution.eligibility is PayloadTargetEligibility.ELIGIBLE_FIRE
    assert first.challenge is not None
    assert first.status is not None and first.status.confirmation_pending
    assert first.intent is None
    assert (
        coordinator.active_intent(
            selection_command_id=SELECTION_ID,
            track=_selected(last_seen_at_s=10.85),
            now_s=10.9,
        )
        is None
    )

    assert coordinator.consume_confirmation(_command(first), now_s=10.9)
    intent = coordinator.active_intent(
        selection_command_id=SELECTION_ID,
        track=_selected(last_seen_at_s=10.9),
        now_s=10.91,
    )
    assert intent is not None
    assert intent.aimpoint_target_id == "mission-fire-1"
    assert intent.physical_release_enabled is False


def test_live_payload_target_issues_challenge_on_immediate_locked_pool_state() -> None:
    coordinator = LivePayloadTargetCoordinator()

    frame = _prepare(
        coordinator,
        selected=_selected(state=UnifiedTrackState.LOCKED),
    )

    assert frame.resolution is not None and frame.resolution.eligible
    assert frame.challenge is not None
    assert frame.status is not None and frame.status.confirmation_pending


def test_ineligible_person_gets_status_but_never_a_slide_or_intent() -> None:
    coordinator = LivePayloadTargetCoordinator()
    frame = _prepare(coordinator, selected=_selected("person"))

    assert frame.resolution is not None
    assert frame.resolution.eligibility is PayloadTargetEligibility.TARGET_NOT_PAYLOAD_ELIGIBLE
    assert frame.challenge is None
    assert frame.status is not None and not frame.status.confirmation_pending
    assert frame.intent is None


def test_raw_mission_revision_increment_does_not_invalidate_same_fire_identity() -> None:
    coordinator = LivePayloadTargetCoordinator()
    first = _prepare(coordinator)
    assert coordinator.consume_confirmation(_command(first), now_s=10.9)

    second = _prepare(
        coordinator,
        selected=_selected(last_seen_at_s=10.92),
        fire_tracks=(_fire(revision=8, last_seen_at_s=10.92),),
        now_s=10.95,
        wire_now_s=1000.95,
    )
    assert second.intent is not None
    assert second.status is not None and second.status.confirmation_accepted
    assert second.challenge is None


def test_occlusion_or_selection_change_revokes_grant_fail_closed() -> None:
    coordinator = LivePayloadTargetCoordinator()
    first = _prepare(coordinator)
    assert coordinator.consume_confirmation(_command(first), now_s=10.9)

    occluded = replace(
        _selected(last_seen_at_s=10.9),
        state=UnifiedTrackState.OCCLUDED,
    )
    assert (
        coordinator.active_intent(
            selection_command_id=SELECTION_ID,
            track=occluded,
            now_s=10.91,
        )
        is None
    )
    changed = _prepare(
        coordinator,
        selection_command_id="22222222-2222-4222-8222-222222222222",
        selected=_selected(last_seen_at_s=10.92),
        fire_tracks=(_fire(last_seen_at_s=10.92),),
        now_s=10.95,
        wire_now_s=1000.95,
    )
    assert changed.intent is None
    assert changed.challenge is not None
