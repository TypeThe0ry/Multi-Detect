from __future__ import annotations

from dataclasses import replace

from multidetect.domain import BoundingBox, TrackSnapshot
from multidetect.payload_target_gate import (
    PayloadSlideConfirmationController,
    PayloadTargetEligibility,
    PayloadTargetResolver,
)
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


def _selected(label: str = "flame", bbox: BoundingBox | None = None) -> UnifiedTrackSnapshot:
    box = bbox or BoundingBox(0.30, 0.30, 0.60, 0.65)
    return UnifiedTrackSnapshot(
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


def _fire(
    track_id: str = "mission-fire-1",
    bbox: BoundingBox | None = None,
    *,
    revision: int = 7,
    confirmed: bool = True,
    corroborated: bool = True,
) -> TrackSnapshot:
    box = bbox or BoundingBox(0.32, 0.32, 0.58, 0.62)
    return TrackSnapshot(
        track_id=track_id,
        revision=revision,
        label="flame",
        bbox=box,
        first_seen_at_s=7.0,
        last_seen_at_s=10.0,
        observation_count=8,
        consecutive_observations=8,
        confidence_floor=0.86,
        confidence_mean=0.91,
        maximum_gap_s=0.1,
        area_growth_rate=0.0,
        thermal_corroborated=False,
        confirmed=confirmed,
        independent_rgb_corroborated=corroborated,
    )


def _resolve(selected: UnifiedTrackSnapshot, fires=()):
    return PayloadTargetResolver().resolve(
        selection_command_id="selection-1",
        selected_target_revision=11,
        selected=selected,
        fire_tracks=fires,
        now_s=10.1,
    )


def test_selected_qualified_fire_resolves_to_fire_aimpoint() -> None:
    resolution = _resolve(_selected(), (_fire(),))

    assert resolution.eligibility is PayloadTargetEligibility.ELIGIBLE_FIRE
    assert resolution.aimpoint_target_id == "mission-fire-1"
    assert resolution.composite_context is False
    assert resolution.physical_release_enabled is False


def test_immediate_locked_pool_state_is_stable_for_payload_resolution() -> None:
    resolution = _resolve(
        replace(_selected(), state=UnifiedTrackState.LOCKED),
        (_fire(),),
    )

    assert resolution.eligibility is PayloadTargetEligibility.ELIGIBLE_FIRE


def test_person_smoke_and_unsupported_targets_remain_selectable_but_payload_ineligible() -> None:
    for label in ("person", "firefighter", "smoke", "road"):
        resolution = _resolve(_selected(label), (_fire(),))
        assert resolution.eligibility is PayloadTargetEligibility.TARGET_NOT_PAYLOAD_ELIGIBLE
        assert resolution.eligible is False


def test_vehicle_only_resolves_through_a_qualified_fire_aimpoint() -> None:
    vehicle = _selected("vehicle", BoundingBox(0.20, 0.20, 0.72, 0.75))
    resolution = _resolve(vehicle, (_fire(),))

    assert resolution.eligibility is PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT
    assert resolution.aimpoint_target_id == "mission-fire-1"
    assert resolution.aimpoint_bbox == _fire().bbox
    assert resolution.composite_context is True


def test_ordinary_vehicle_without_qualified_fire_is_denied() -> None:
    vehicle = _selected("car", BoundingBox(0.20, 0.20, 0.72, 0.75))

    assert _resolve(vehicle).eligibility is PayloadTargetEligibility.FIRE_EVIDENCE_UNAVAILABLE
    assert (
        _resolve(vehicle, (_fire(corroborated=False),)).eligibility
        is PayloadTargetEligibility.FIRE_EVIDENCE_UNAVAILABLE
    )


def test_ambiguous_context_fire_association_fails_closed() -> None:
    vehicle = _selected("vehicle", BoundingBox(0.20, 0.20, 0.72, 0.75))
    first = _fire("fire-a", BoundingBox(0.28, 0.30, 0.44, 0.58))
    second = _fire("fire-b", BoundingBox(0.48, 0.30, 0.64, 0.58))

    resolution = _resolve(vehicle, (first, second))

    assert resolution.eligibility is PayloadTargetEligibility.FIRE_ASSOCIATION_AMBIGUOUS
    assert resolution.eligible is False


def test_unstable_or_stale_selection_cannot_enter_payload_gate() -> None:
    occluded = replace(_selected(), state=UnifiedTrackState.OCCLUDED)
    stale = replace(_selected(), last_seen_at_s=8.0)

    for selected in (occluded, stale):
        assert (
            _resolve(selected, (_fire(),)).eligibility
            is PayloadTargetEligibility.TARGET_NOT_STABLY_TRACKED
        )


def test_slide_confirmation_binds_selection_and_real_fire_aimpoint() -> None:
    resolution = _resolve(_selected(), (_fire(),))
    controller = PayloadSlideConfirmationController()
    challenge = controller.issue(resolution, now_s=10.2)

    grant = controller.accept(
        token=challenge.token,
        resolution=resolution,
        slide_started_at_s=10.3,
        slide_completed_at_s=11.0,
        completion_fraction=1.0,
        continuous=True,
    )

    assert grant is not None
    assert grant.selected_target_id == "unified-selected"
    assert grant.aimpoint_target_id == "mission-fire-1"
    assert controller.grant_valid(grant, resolution, now_s=11.1) is True
    assert grant.physical_release_enabled is False


def test_slide_token_is_one_time_and_invalid_motion_is_consumed() -> None:
    resolution = _resolve(_selected(), (_fire(),))
    controller = PayloadSlideConfirmationController()
    challenge = controller.issue(resolution, now_s=10.2)
    invalid = controller.accept(
        token=challenge.token,
        resolution=resolution,
        slide_started_at_s=10.3,
        slide_completed_at_s=10.4,
        completion_fraction=0.5,
        continuous=False,
    )

    assert invalid is None
    assert (
        controller.accept(
            token=challenge.token,
            resolution=resolution,
            slide_started_at_s=10.3,
            slide_completed_at_s=11.0,
            completion_fraction=1.0,
            continuous=True,
        )
        is None
    )


def test_slide_grant_invalidates_on_selected_or_aimpoint_revision_change() -> None:
    resolution = _resolve(_selected(), (_fire(),))
    controller = PayloadSlideConfirmationController()
    challenge = controller.issue(resolution, now_s=10.2)
    grant = controller.accept(
        token=challenge.token,
        resolution=resolution,
        slide_started_at_s=10.3,
        slide_completed_at_s=11.0,
        completion_fraction=1.0,
        continuous=True,
    )
    assert grant is not None

    selected_changed = replace(resolution, selected_target_revision=12)
    fire_changed = replace(resolution, aimpoint_target_revision=8)
    assert controller.grant_valid(grant, selected_changed, now_s=11.1) is False
    assert controller.grant_valid(grant, fire_changed, now_s=11.1) is False


def test_slide_grant_expires_at_the_exact_deadline() -> None:
    resolution = _resolve(_selected(), (_fire(),))
    controller = PayloadSlideConfirmationController()
    challenge = controller.issue(resolution, now_s=10.2)
    grant = controller.accept(
        token=challenge.token,
        resolution=resolution,
        slide_started_at_s=10.3,
        slide_completed_at_s=11.0,
        completion_fraction=1.0,
        continuous=True,
    )

    assert grant is not None
    assert controller.grant_valid(grant, resolution, now_s=challenge.expires_at_s - 0.001)
    assert not controller.grant_valid(grant, resolution, now_s=challenge.expires_at_s)


def test_ineligible_target_cannot_issue_slide_challenge() -> None:
    import pytest

    resolution = _resolve(_selected("person"), (_fire(),))
    controller = PayloadSlideConfirmationController()

    with pytest.raises(ValueError, match="ineligible"):
        controller.issue(resolution, now_s=10.2)
