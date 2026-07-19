from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_status import build_target_pool_status_messages
from multidetect.unified_tracking import (
    AppearanceEmbedding,
    CameraMotionEstimate,
    TargetMotionHint,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackState,
    _minimum_cost_candidate_assignment,
)


def _box(center_x: float, center_y: float, size: float = 0.08) -> BoundingBox:
    return BoundingBox(
        center_x - size / 2.0,
        center_y - size / 2.0,
        center_x + size / 2.0,
        center_y + size / 2.0,
    )


def _observation(
    center_x: float,
    center_y: float,
    *,
    label: str = "vehicle",
    appearance: tuple[float, ...] | None = None,
    confidence: float = 0.92,
) -> TargetObservation:
    return TargetObservation(
        label=label,
        confidence=confidence,
        bbox=_box(center_x, center_y),
        appearance=(AppearanceEmbedding(appearance) if appearance is not None else None),
    )


def test_pool_maintains_ten_targets_and_switches_primary_without_dropping_background_locks() -> (
    None
):
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(maximum_tracks=16))
    observations = tuple(
        _observation(0.08 + index * 0.085, 0.3 + (index % 2) * 0.2) for index in range(10)
    )
    update = pool.update(frame_id="frame-1", captured_at_s=1.0, observations=observations)

    assert len(update.tracks) == 10
    first_id, second_id = update.tracks[0].track_id, update.tracks[1].track_id
    pool.lock(first_id, now_s=1.01)
    pool.lock(second_id, now_s=1.02)
    switched = pool.switch_primary(second_id, now_s=1.03)

    assert switched.primary_track_id == second_id
    assert switched.background_locked_track_ids == (first_id,)
    assert switched.switch_latency_ms < 200.0
    snapshots = {track.track_id: track for track in pool.snapshots()}
    assert snapshots[first_id].locked is True and snapshots[first_id].primary is False
    assert snapshots[second_id].locked is True and snapshots[second_id].primary is True
    assert len(snapshots) == 10


def test_global_assignment_avoids_greedy_local_minimum() -> None:
    candidates = (
        (0, 0.10, "track-a", 0, None, False, False),
        (0, 0.20, "track-a", 1, None, False, False),
        (0, 0.11, "track-b", 0, None, False, False),
        (0, 0.90, "track-b", 1, None, False, False),
    )

    selected = _minimum_cost_candidate_assignment(candidates)

    assert {(candidate[2], candidate[3]) for candidate in selected} == {
        ("track-a", 1),
        ("track-b", 0),
    }
    assert sum(candidate[1] for candidate in selected) == pytest.approx(0.31)


def test_low_confidence_observation_extends_track_but_cannot_create_new_identity() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_association_confidence=0.10,
            minimum_new_track_confidence=0.35,
            high_confidence_threshold=0.55,
        )
    )
    initial = pool.update(
        frame_id="confidence-0",
        captured_at_s=1.0,
        observations=(_observation(0.20, 0.40, confidence=0.90),),
    )
    track_id = initial.tracks[0].track_id

    update = pool.update(
        frame_id="confidence-1",
        captured_at_s=1.1,
        observations=(
            _observation(0.22, 0.40, confidence=0.20),
            _observation(0.80, 0.70, confidence=0.20),
        ),
    )

    assert len(update.tracks) == 1
    assert update.tracks[0].track_id == track_id
    assert update.tracks[0].observation_count == 2


def test_priority_semantic_detection_can_start_track_below_fallback_threshold() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_association_confidence=0.10,
            priority_minimum_new_track_confidence=0.25,
            minimum_new_track_confidence=0.35,
            high_confidence_threshold=0.55,
        )
    )

    update = pool.update(
        frame_id="priority-confidence-0",
        captured_at_s=1.0,
        observations=(
            _observation(0.2, 0.4, label="person", confidence=0.30),
            _observation(0.5, 0.4, label="car", confidence=0.30),
            _observation(0.7, 0.4, label="airplane", confidence=0.30),
            _observation(0.9, 0.4, label="chair", confidence=0.30),
        ),
    )

    assert {track.label for track in update.tracks} == {"person", "car", "airplane"}
    assert update.dropped_observation_count == 1


def test_semantic_vehicle_subtype_changes_preserve_identity() -> None:
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    first = pool.update(
        frame_id="family-0",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.4, label="van"),),
    )
    track_id = first.tracks[0].track_id

    for index, (center_x, label) in enumerate(((0.21, "car"), (0.22, "truck")), start=1):
        second = pool.update(
            frame_id=f"family-{index}",
            captured_at_s=1.0 + 0.1 * index,
            observations=(_observation(center_x, 0.4, label=label),),
        )

    assert len(second.tracks) == 1
    assert second.tracks[0].track_id == track_id
    assert second.tracks[0].observation_count == 3


def test_aircraft_aliases_and_fast_image_motion_preserve_one_identity() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.10,
            maximum_center_distance=0.08,
        )
    )
    first = pool.update(
        frame_id="aircraft-0",
        captured_at_s=1.0,
        observations=(_observation(0.20, 0.40, label="airplane"),),
    )
    track_id = first.tracks[0].track_id

    for index, (center_x, label) in enumerate(
        ((0.33, "aircraft"), (0.46, "plane"), (0.59, "aeroplane")),
        start=1,
    ):
        update = pool.update(
            frame_id=f"aircraft-{index}",
            captured_at_s=1.0 + 0.1 * index,
            observations=(_observation(center_x, 0.40, label=label),),
        )
        assert len(update.tracks) == 1
        assert update.tracks[0].track_id == track_id


def test_vehicle_motion_gate_stays_conservative_for_same_fast_jump() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.10,
            maximum_center_distance=0.08,
        )
    )
    first = pool.update(
        frame_id="vehicle-jump-0",
        captured_at_s=1.0,
        observations=(_observation(0.20, 0.40, label="car"),),
    )
    original_id = first.tracks[0].track_id
    update = pool.update(
        frame_id="vehicle-jump-1",
        captured_at_s=1.1,
        observations=(_observation(0.33, 0.40, label="car"),),
    )

    assert len(update.tracks) == 2
    original_track = next(track for track in update.tracks if track.track_id == original_id)
    assert original_track.observation_count == 1


def test_vehicle_motion_gate_keeps_bounded_staggered_detector_motion() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.10,
            maximum_center_distance=0.08,
        )
    )
    first = pool.update(
        frame_id="vehicle-bounded-0",
        captured_at_s=1.0,
        observations=(_observation(0.20, 0.40, label="car"),),
    )
    track_id = first.tracks[0].track_id

    update = pool.update(
        frame_id="vehicle-bounded-1",
        captured_at_s=1.1,
        observations=(_observation(0.29, 0.40, label="van"),),
    )

    assert len(update.tracks) == 1
    assert update.tracks[0].track_id == track_id
    assert update.tracks[0].observation_count == 2


def test_fixed_fire_damps_local_flow_jitter_after_camera_compensation() -> None:
    def follow(label: str) -> tuple[float, ...]:
        pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
        first = pool.update(
            frame_id=f"{label}-0",
            captured_at_s=1.0,
            observations=(_observation(0.30, 0.40, label=label),),
        )
        track_id = first.tracks[0].track_id
        residuals: list[float] = []
        for index, jitter in enumerate((0.018, -0.020, 0.016, -0.019, 0.014, -0.017), start=1):
            expected_center_x = 0.30 + 0.03 * index
            update = pool.update(
                frame_id=f"{label}-{index}",
                captured_at_s=1.0 + 0.1 * index,
                observations=(),
                camera_motion=CameraMotionEstimate(dx=0.03, dy=0.0, confidence=0.95),
                motion_hints=(
                    TargetMotionHint(
                        track_id=track_id,
                        residual_dx=jitter,
                        residual_dy=0.0,
                        confidence=0.95,
                    ),
                ),
                visual_confirmation_track_ids=(track_id,),
            )
            assert len(update.tracks) == 1
            track = next(track for track in update.tracks if track.track_id == track_id)
            residuals.append(track.bbox.center[0] - expected_center_x)
        return tuple(residuals)

    fire_residuals = follow("flame")
    generic_residuals = follow("chair")

    assert max(fire_residuals) - min(fire_residuals) < 0.60 * (
        max(generic_residuals) - min(generic_residuals)
    )
    assert max(abs(value) for value in fire_residuals) < 0.03


def test_operator_pool_waits_for_stability_and_orders_typed_before_manual() -> None:
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=2))
    observations = (
        _observation(0.1, 0.2, label="manual"),
        _observation(0.3, 0.2, label="car"),
        _observation(0.5, 0.2, label="person"),
        _observation(0.7, 0.2, label="flame"),
    )
    tentative = pool.update(
        frame_id="priority-0",
        captured_at_s=1.0,
        observations=observations,
    )
    hidden = build_target_pool_status_messages(
        sequence_start=1,
        pool_revision=1,
        tracks=tentative.tracks,
        produced_at_s=1.0,
        include_tentative=False,
    )
    assert hidden[0].entries == ()

    stable = pool.update(
        frame_id="priority-1",
        captured_at_s=1.1,
        observations=observations,
    )
    messages = build_target_pool_status_messages(
        sequence_start=2,
        pool_revision=2,
        tracks=stable.tracks,
        produced_at_s=1.1,
        include_tentative=False,
        operator_tracked_ids=(stable.tracks[1].track_id, stable.tracks[2].track_id),
        relative_bearing_by_target_id={stable.tracks[0].track_id: -3.2},
        estimated_range_by_target_id={stable.tracks[0].track_id: 41.5},
        target_speed_by_target_id={stable.tracks[0].track_id: 2.4},
    )
    labels = tuple(entry.label for message in messages for entry in message.entries)
    assert labels == ("flame", "person", "car", "manual")
    tracked_labels = {
        entry.label for message in messages for entry in message.entries if entry.operator_tracked
    }
    assert tracked_labels == {stable.tracks[1].label, stable.tracks[2].label}
    metric_entry = next(
        entry
        for message in messages
        for entry in message.entries
        if entry.target_id == stable.tracks[0].track_id
    )
    assert metric_entry.relative_bearing_deg == pytest.approx(-3.2)
    assert metric_entry.estimated_range_m == pytest.approx(41.5)
    assert metric_entry.target_speed_mps == pytest.approx(2.4)


def test_operator_pool_never_publishes_lost_track_boxes() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.2,
            lost_retention_s=5.0,
        )
    )
    created = pool.update(
        frame_id="visible-0",
        captured_at_s=1.0,
        observations=(_observation(0.3, 0.3, label="person"),),
    )
    assert created.tracks[0].state is not UnifiedTrackState.LOST
    pool.update(frame_id="visible-1", captured_at_s=1.15, observations=())
    lost = pool.update(frame_id="visible-2", captured_at_s=1.35, observations=())
    assert lost.tracks[0].state is UnifiedTrackState.LOST

    messages = build_target_pool_status_messages(
        sequence_start=10,
        pool_revision=3,
        tracks=lost.tracks,
        produced_at_s=1.35,
        include_tentative=True,
    )

    assert messages[0].entries == ()
    assert messages[0].total_track_count == 0


def test_target_pool_confidence_cascade_requires_ordered_thresholds() -> None:
    with pytest.raises(ValueError, match="association <= priority new track"):
        UnifiedTargetPoolConfig(
            minimum_association_confidence=0.4,
            minimum_new_track_confidence=0.3,
        )


def test_locked_reacquisition_timeout_must_extend_normal_timeout() -> None:
    with pytest.raises(ValueError, match="must exceed reacquisition_timeout_s"):
        UnifiedTargetPoolConfig(
            reacquisition_timeout_s=0.5,
            locked_reacquisition_timeout_s=0.5,
        )


def test_locked_target_stays_reacquiring_beyond_normal_timeout() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            locked_reacquisition_timeout_s=0.8,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    first = pool.update(
        frame_id="locked-timeout-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.4, label="person"),),
    )
    target_id = first.tracks[0].track_id
    pool.lock(target_id, now_s=1.01)

    update = pool.update(frame_id="locked-timeout-2", captured_at_s=1.6, observations=())

    target = next(track for track in update.tracks if track.track_id == target_id)
    assert target.state is UnifiedTrackState.REACQUIRING
    assert target.locked is True


def test_locked_reid_target_wins_observation_before_active_duplicate() -> None:
    identity = (1.0, 0.0, 0.0, 0.0)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.6,
            locked_reacquisition_timeout_s=1.2,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.12,
        )
    )
    first = pool.update(
        frame_id="locked-priority-1",
        captured_at_s=1.0,
        observations=(
            _observation(0.20, 0.40, label="person", appearance=identity),
            _observation(0.70, 0.40, label="person"),
        ),
    )
    locked_id, duplicate_id = (track.track_id for track in first.tracks)
    pool.update(
        frame_id="locked-priority-2",
        captured_at_s=1.1,
        observations=(
            _observation(0.20, 0.40, label="person", appearance=identity),
            _observation(0.70, 0.40, label="person"),
        ),
    )
    pool.lock(locked_id, now_s=1.11)
    pool.update(
        frame_id="locked-priority-3",
        captured_at_s=1.3,
        observations=(_observation(0.70, 0.40, label="person"),),
    )

    recovered = pool.update(
        frame_id="locked-priority-4",
        captured_at_s=1.4,
        observations=(
            _observation(0.71, 0.40, label="person", appearance=(0.999, 0.01, 0.0, 0.0)),
        ),
    )

    by_id = {track.track_id: track for track in recovered.tracks}
    assert recovered.recovered_track_ids == (locked_id,)
    assert by_id[locked_id].state is UnifiedTrackState.RECOVERED
    assert by_id[locked_id].reid_confirmed is True
    assert by_id[locked_id].bbox.center[0] > 0.65
    assert by_id[duplicate_id].state in {
        UnifiedTrackState.OCCLUDED,
        UnifiedTrackState.REACQUIRING,
    }


def test_locked_person_survives_crossing_motion_detector_gap_and_pose_reid() -> None:
    person_a = (1.0, 0.0, 0.0, 0.0)
    person_b = (0.0, 1.0, 0.0, 0.0)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.15,
            reacquisition_timeout_s=0.6,
            locked_reacquisition_timeout_s=2.0,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.15,
        )
    )
    first = pool.update(
        frame_id="person-crossing-1",
        captured_at_s=1.0,
        observations=(
            _observation(0.25, 0.42, label="person", appearance=person_a),
            _observation(0.75, 0.42, label="person", appearance=person_b),
        ),
    )
    person_a_id, person_b_id = (track.track_id for track in first.tracks)
    pool.update(
        frame_id="person-crossing-2",
        captured_at_s=1.1,
        observations=(
            _observation(0.30, 0.42, label="person", appearance=(0.999, 0.01, 0.0, 0.0)),
            _observation(0.70, 0.42, label="person", appearance=(0.01, 0.999, 0.0, 0.0)),
        ),
    )
    pool.lock(person_a_id, now_s=1.11)
    pool.update(
        frame_id="person-crossing-3",
        captured_at_s=1.3,
        observations=(
            _observation(0.62, 0.42, label="person", appearance=(0.01, 0.999, 0.0, 0.0)),
        ),
    )
    gap = pool.update(
        frame_id="person-crossing-4",
        captured_at_s=1.5,
        observations=(
            _observation(0.56, 0.42, label="person", appearance=(0.02, 0.998, 0.0, 0.0)),
        ),
    )
    person_a_gap = next(track for track in gap.tracks if track.track_id == person_a_id)
    assert person_a_gap.state is UnifiedTrackState.REACQUIRING

    crossing = pool.update(
        frame_id="person-crossing-5",
        captured_at_s=1.6,
        observations=(
            _observation(0.55, 0.42, label="person", appearance=(0.997, 0.03, 0.0, 0.0)),
            _observation(0.50, 0.42, label="person", appearance=(0.03, 0.997, 0.0, 0.0)),
        ),
    )
    separated = pool.update(
        frame_id="person-crossing-6",
        captured_at_s=1.7,
        observations=(
            _observation(0.63, 0.42, label="person", appearance=(0.996, 0.04, 0.0, 0.0)),
            _observation(0.43, 0.42, label="person", appearance=(0.04, 0.996, 0.0, 0.0)),
        ),
    )

    assert crossing.recovered_track_ids == (person_a_id,)
    assert len(separated.tracks) == 2
    by_id = {track.track_id: track for track in separated.tracks}
    assert by_id[person_a_id].locked is True
    assert by_id[person_a_id].bbox.center[0] > by_id[person_b_id].bbox.center[0]
    assert by_id[person_a_id].last_appearance_distance is not None
    assert by_id[person_a_id].last_appearance_distance < 0.01


def test_locked_person_uses_pose_gallery_for_strict_full_frame_recovery() -> None:
    pose_front = (1.0, 0.0, 0.0, 0.0)
    pose_oblique = (0.6428, 0.7660, 0.0, 0.0)
    pose_side = (-0.17365, 0.9848, 0.0, 0.0)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            locked_reacquisition_timeout_s=0.8,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.08,
        )
    )
    first = pool.update(
        frame_id="person-pose-1",
        captured_at_s=1.0,
        observations=(_observation(0.20, 0.4, label="person", appearance=pose_front),),
    )
    target_id = first.tracks[0].track_id
    pool.lock(target_id, now_s=1.01)

    # A turning person moves through views that are each locally plausible but
    # whose global mean no longer represents the side view.
    pool.update(
        frame_id="person-pose-2",
        captured_at_s=1.1,
        observations=(_observation(0.22, 0.4, label="person", appearance=pose_oblique),),
    )
    side = pool.update(
        frame_id="person-pose-3",
        captured_at_s=1.2,
        observations=(_observation(0.24, 0.4, label="person", appearance=pose_side),),
    )
    assert len(side.tracks) == 1
    assert side.tracks[0].track_id == target_id
    assert side.tracks[0].appearance_sample_count == 3

    lost = pool.update(frame_id="person-pose-4", captured_at_s=2.1, observations=())
    assert lost.tracks[0].state is UnifiedTrackState.LOST
    recovered = pool.update(
        frame_id="person-pose-5",
        captured_at_s=2.2,
        observations=(_observation(0.82, 0.4, label="person", appearance=pose_side),),
    )

    assert recovered.recovered_track_ids == (target_id,)
    assert len(recovered.tracks) == 1
    assert recovered.tracks[0].state is UnifiedTrackState.RECOVERED
    assert recovered.tracks[0].reid_confirmed is True
    assert recovered.tracks[0].last_appearance_distance == pytest.approx(0.0, abs=1e-9)


def test_visual_confirmation_keeps_multiple_manual_tracks_alive() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.05,
            reacquisition_timeout_s=0.20,
            lost_retention_s=2.0,
            locked_lost_retention_s=2.0,
        )
    )
    created = pool.update(
        frame_id="manual-multi-1",
        captured_at_s=1.0,
        observations=(
            _observation(0.25, 0.4, label="manual"),
            _observation(0.75, 0.4, label="manual"),
        ),
    )
    first_id, second_id = (track.track_id for track in created.tracks)

    for index in range(1, 6):
        update = pool.update(
            frame_id=f"manual-multi-{index + 1}",
            captured_at_s=1.0 + index * 0.1,
            observations=(),
            motion_hints=(
                TargetMotionHint(first_id, 0.01, 0.0, confidence=0.90),
                TargetMotionHint(second_id, -0.01, 0.0, confidence=0.90),
            ),
            visual_confirmation_track_ids=(first_id, second_id),
        )
        assert update.visual_confirmed_track_ids == (first_id, second_id)

    by_id = {track.track_id: track for track in update.tracks}
    assert len(by_id) == 2
    assert all(track.state is not UnifiedTrackState.LOST for track in by_id.values())
    assert by_id[first_id].bbox.center[0] > 0.25
    assert by_id[second_id].bbox.center[0] < 0.75
    assert all(track.observation_count == 6 for track in by_id.values())


def test_unrequested_motion_hint_remains_prediction_only() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.05,
            reacquisition_timeout_s=0.20,
            lost_retention_s=2.0,
            locked_lost_retention_s=2.0,
        )
    )
    created = pool.update(
        frame_id="prediction-only-1",
        captured_at_s=1.0,
        observations=(_observation(0.25, 0.4, label="person"),),
    )
    target_id = created.tracks[0].track_id
    update = pool.update(
        frame_id="prediction-only-2",
        captured_at_s=1.3,
        observations=(),
        motion_hints=(TargetMotionHint(target_id, 0.02, 0.0, confidence=0.9),),
    )

    assert update.visual_confirmed_track_ids == ()
    assert update.tracks[0].state is UnifiedTrackState.LOST


def test_motion_prediction_bridges_short_occlusion_and_preserves_identity() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=2,
            occluded_after_s=0.2,
            reacquisition_timeout_s=1.0,
            lost_retention_s=5.0,
            locked_lost_retention_s=5.0,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.20, 0.40),),
    )
    track_id = first.tracks[0].track_id
    pool.update(
        frame_id="frame-2",
        captured_at_s=1.1,
        observations=(_observation(0.24, 0.40),),
    )
    occluded = pool.update(frame_id="frame-3", captured_at_s=1.2, observations=())
    recovered = pool.update(
        frame_id="frame-4",
        captured_at_s=1.3,
        observations=(_observation(0.31, 0.40),),
    )

    assert occluded.tracks[0].state is UnifiedTrackState.OCCLUDED
    assert recovered.recovered_track_ids == (track_id,)
    assert recovered.tracks[0].track_id == track_id
    assert recovered.tracks[0].state is UnifiedTrackState.RECOVERED
    assert recovered.tracks[0].actionable is True


def test_constant_velocity_kalman_forecast_tracks_motion_through_detector_gap() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            maximum_center_distance=0.12,
            kalman_process_noise=0.02,
            kalman_measurement_noise=0.0002,
        )
    )
    for index, center_x in enumerate((0.20, 0.24, 0.28)):
        update = pool.update(
            frame_id=f"kalman-{index}",
            captured_at_s=1.0 + index * 0.1,
            observations=(_observation(center_x, 0.4),),
        )
        assert len(update.tracks) == 1

    predicted = pool.update(
        frame_id="kalman-gap",
        captured_at_s=1.3,
        observations=(),
    ).tracks[0]

    assert predicted.state is UnifiedTrackState.OCCLUDED
    assert predicted.velocity_x_s == pytest.approx(0.4, abs=0.12)
    assert predicted.predicted_bbox.center[0] == pytest.approx(0.32, abs=0.025)


def test_kalman_measurement_noise_controls_outlier_correction_strength() -> None:
    def corrected_center(measurement_noise: float) -> float:
        pool = UnifiedTargetPool(
            UnifiedTargetPoolConfig(
                minimum_confirmed_hits=1,
                maximum_center_distance=0.5,
                kalman_measurement_noise=measurement_noise,
            )
        )
        pool.update(
            frame_id="noise-0",
            captured_at_s=1.0,
            observations=(_observation(0.2, 0.4),),
        )
        return (
            pool.update(
                frame_id="noise-1",
                captured_at_s=1.1,
                observations=(_observation(0.4, 0.4),),
            )
            .tracks[0]
            .bbox.center[0]
        )

    low_noise_center = corrected_center(0.0001)
    high_noise_center = corrected_center(0.1)

    assert low_noise_center > high_noise_center + 0.12
    assert low_noise_center == pytest.approx(0.4, abs=0.015)


def test_kalman_configuration_rejects_non_positive_noise() -> None:
    with pytest.raises(ValueError, match="kalman_process_noise"):
        UnifiedTargetPoolConfig(kalman_process_noise=0.0)
    with pytest.raises(ValueError, match="kalman_measurement_noise"):
        UnifiedTargetPoolConfig(kalman_measurement_noise=-0.1)
    with pytest.raises(ValueError, match="kalman_gate_sigma"):
        UnifiedTargetPoolConfig(kalman_gate_sigma=0.0)


def test_kalman_innovation_gate_rejects_sudden_distractor_after_track_converges() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            maximum_center_distance=0.2,
            kalman_process_noise=0.0001,
            kalman_measurement_noise=0.00005,
            kalman_gate_sigma=3.0,
        )
    )
    original_id = ""
    for index in range(12):
        update = pool.update(
            frame_id=f"gate-{index}",
            captured_at_s=1.0 + index * 0.05,
            observations=(_observation(0.30, 0.40),),
        )
        original_id = update.tracks[0].track_id

    distracted = pool.update(
        frame_id="gate-distractor",
        captured_at_s=1.6,
        observations=(_observation(0.40, 0.40),),
    )
    by_id = {track.track_id: track for track in distracted.tracks}

    assert len(by_id) == 2
    assert by_id[original_id].state is UnifiedTrackState.OCCLUDED
    assert distracted.recovered_track_ids == ()


def test_lost_track_does_not_force_identity_without_reid_evidence() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.4),),
    )
    original_id = first.tracks[0].track_id
    lost = pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())
    reappeared = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(_observation(0.22, 0.4),),
    )

    assert lost.tracks[0].state is UnifiedTrackState.LOST
    assert reappeared.recovered_track_ids == ()
    assert len(reappeared.tracks) == 2
    snapshots = {track.track_id: track for track in reappeared.tracks}
    assert snapshots[original_id].state is UnifiedTrackState.LOST
    assert any(track.track_id != original_id for track in reappeared.tracks)


def test_lost_track_reacquires_only_with_strong_matching_reid_embedding() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    identity = (1.0, 0.1, 0.0, 0.0)
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.4, appearance=identity),),
    )
    original_id = first.tracks[0].track_id
    pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())
    recovered = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(_observation(0.24, 0.4, appearance=(0.99, 0.11, 0.0, 0.0)),),
    )

    assert len(recovered.tracks) == 1
    assert recovered.recovered_track_ids == (original_id,)
    assert recovered.tracks[0].track_id == original_id
    assert recovered.tracks[0].state is UnifiedTrackState.RECOVERED
    assert recovered.tracks[0].reid_confirmed is True


def test_person_reid_gate_override_does_not_relax_vehicle_association() -> None:
    config = UnifiedTargetPoolConfig(
        minimum_confirmed_hits=1,
        maximum_appearance_distance=0.38,
        person_maximum_appearance_distance=0.70,
    )
    person_pool = UnifiedTargetPool(config)
    vehicle_pool = UnifiedTargetPool(config)
    first_embedding = (1.0, 0.0)
    pose_changed_embedding = (0.5, 0.8660254)  # cosine distance ~= 0.5

    person_pool.update(
        frame_id="person-override-1",
        captured_at_s=1.0,
        observations=(_observation(0.30, 0.40, label="person", appearance=first_embedding),),
    )
    person_update = person_pool.update(
        frame_id="person-override-2",
        captured_at_s=1.1,
        observations=(
            _observation(0.31, 0.40, label="person", appearance=pose_changed_embedding),
        ),
    )

    vehicle_pool.update(
        frame_id="vehicle-override-1",
        captured_at_s=1.0,
        observations=(_observation(0.30, 0.40, label="car", appearance=first_embedding),),
    )
    vehicle_update = vehicle_pool.update(
        frame_id="vehicle-override-2",
        captured_at_s=1.1,
        observations=(
            _observation(0.31, 0.40, label="car", appearance=pose_changed_embedding),
        ),
    )

    assert person_update.created_track_ids == ()
    assert len(person_update.tracks) == 1
    assert person_update.tracks[0].observation_count == 2
    assert len(vehicle_update.tracks) == 2


def test_person_reid_override_requires_strict_gate_to_fit_the_person_gate() -> None:
    with pytest.raises(ValueError, match="person strict ReID distance"):
        UnifiedTargetPoolConfig(
            person_maximum_appearance_distance=0.60,
            person_strict_reid_distance=0.61,
        )


def test_locked_lost_track_can_reacquire_across_full_frame_with_strict_reid() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.08,
        )
    )
    identity = (1.0, 0.1, 0.0, 0.0)
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.12, 0.4, appearance=identity),),
    )
    original_id = first.tracks[0].track_id
    pool.lock(original_id, now_s=1.01)
    pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())

    recovered = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(_observation(0.86, 0.4, appearance=(0.99, 0.11, 0.0, 0.0)),),
    )

    assert recovered.recovered_track_ids == (original_id,)
    assert len(recovered.tracks) == 1
    assert recovered.tracks[0].state is UnifiedTrackState.RECOVERED
    assert recovered.tracks[0].reid_confirmed is True
    assert recovered.ambiguous_reid_recovery_count == 0


def test_unlocked_lost_track_does_not_use_full_frame_reid_override() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.08,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.12, 0.4, appearance=(1.0, 0.0, 0.0)),),
    )
    original_id = first.tracks[0].track_id
    pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())

    update = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(_observation(0.86, 0.4, appearance=(0.999, 0.02, 0.0)),),
    )

    assert update.recovered_track_ids == ()
    assert len(update.tracks) == 2
    assert next(track for track in update.tracks if track.track_id == original_id).state is (
        UnifiedTrackState.LOST
    )


def test_full_frame_reid_refuses_two_near_tied_candidates() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
            maximum_center_distance=0.08,
            strict_reid_ambiguity_margin=0.035,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.12, 0.4, appearance=(1.0, 0.0, 0.0)),),
    )
    original_id = first.tracks[0].track_id
    pool.lock(original_id, now_s=1.01)
    pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())

    update = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(
            _observation(0.76, 0.35, appearance=(0.999, 0.04, 0.0)),
            _observation(0.86, 0.45, appearance=(0.998, 0.06, 0.0)),
        ),
    )

    assert update.recovered_track_ids == ()
    assert update.ambiguous_reid_recovery_count == 2
    assert len(update.tracks) == 3
    assert next(track for track in update.tracks if track.track_id == original_id).state is (
        UnifiedTrackState.LOST
    )


def test_mismatched_reid_embedding_prevents_false_recovery_of_similar_target() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.4, appearance=(1.0, 0.0, 0.0)),),
    )
    original_id = first.tracks[0].track_id
    pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())
    update = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(_observation(0.21, 0.4, appearance=(0.0, 1.0, 0.0)),),
    )

    assert update.recovered_track_ids == ()
    assert len(update.tracks) == 2
    assert next(track for track in update.tracks if track.track_id == original_id).state is (
        UnifiedTrackState.LOST
    )


def test_camera_motion_compensation_keeps_track_inside_association_gate() -> None:
    config = UnifiedTargetPoolConfig(
        minimum_confirmed_hits=1,
        minimum_iou=0.1,
        maximum_center_distance=0.06,
    )
    pool = UnifiedTargetPool(config)
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.5),),
    )
    track_id = first.tracks[0].track_id
    update = pool.update(
        frame_id="frame-2",
        captured_at_s=1.1,
        observations=(_observation(0.34, 0.5),),
        camera_motion=CameraMotionEstimate(dx=0.14, dy=0.0, confidence=0.9),
    )

    assert len(update.tracks) == 1
    assert update.tracks[0].track_id == track_id


def test_camera_motion_accumulates_across_multiple_missed_frames() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.1,
            maximum_center_distance=0.04,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.5),),
    )
    track_id = first.tracks[0].track_id
    pool.update(
        frame_id="frame-2",
        captured_at_s=1.1,
        observations=(),
        camera_motion=CameraMotionEstimate(dx=0.08, dy=0.0, confidence=0.9),
    )
    update = pool.update(
        frame_id="frame-3",
        captured_at_s=1.2,
        observations=(_observation(0.36, 0.5),),
        camera_motion=CameraMotionEstimate(dx=0.08, dy=0.0, confidence=0.9),
    )

    assert len(update.tracks) == 1
    assert update.tracks[0].track_id == track_id
    assert update.recovered_track_ids == (track_id,)


def test_camera_roll_and_zoom_keep_off_axis_visual_track_continuous() -> None:
    """A 16:9 off-axis target must follow the image roll, not just its centre pan."""

    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.05,
            maximum_center_distance=0.04,
        )
    )
    initial = pool.update(
        frame_id="roll-1",
        captured_at_s=1.0,
        observations=(_observation(0.24, 0.31),),
    )
    track_id = initial.tracks[0].track_id
    motion = CameraMotionEstimate(
        dx=0.018,
        dy=-0.012,
        scale=1.06,
        confidence=0.95,
        rotation_deg=-12.0,
        aspect_ratio=16.0 / 9.0,
    )
    expected_center = motion.transform_point(0.24, 0.31)
    update = pool.update(
        frame_id="roll-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=motion,
        motion_hints=(TargetMotionHint(track_id, 0.0, 0.0, confidence=0.9),),
        visual_confirmation_track_ids=(track_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == track_id)
    assert update.visual_confirmed_track_ids == (track_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.bbox.center == pytest.approx(expected_center, abs=1e-6)


def test_camera_affine_shear_keeps_off_axis_visual_track_continuous() -> None:
    """Yaw/pitch-like shear must not collapse back to a center-only pan estimate."""

    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    initial = pool.update(
        frame_id="affine-1",
        captured_at_s=1.0,
        observations=(_observation(0.24, 0.31),),
    )
    track_id = initial.tracks[0].track_id
    motion = CameraMotionEstimate(
        dx=0.018,
        dy=-0.012,
        scale=1.0,
        confidence=0.95,
        rotation_deg=-2.3,
        aspect_ratio=16.0 / 9.0,
        affine=(1.04, 0.045, -0.035, 0.96),
    )
    expected_center = motion.transform_point(0.24, 0.31)
    update = pool.update(
        frame_id="affine-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=motion,
        motion_hints=(TargetMotionHint(track_id, 0.0, 0.0, confidence=0.9),),
        visual_confirmation_track_ids=(track_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == track_id)
    assert motion.effective_scale == pytest.approx(1.0, abs=0.01)
    assert update.visual_confirmed_track_ids == (track_id,)
    assert tracked.bbox.center == pytest.approx(expected_center, abs=1e-6)


def test_camera_homography_keeps_off_axis_visual_track_continuous() -> None:
    """Material perspective must transform the full box, not only its image-centre pan."""

    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    initial = pool.update(
        frame_id="homography-1",
        captured_at_s=1.0,
        observations=(_observation(0.24, 0.31),),
    )
    track_id = initial.tracks[0].track_id
    motion = CameraMotionEstimate(
        dx=0.008,
        dy=-0.008,
        scale=1.0,
        confidence=0.95,
        aspect_ratio=16.0 / 9.0,
        homography=(1.02, 0.01, -0.002, -0.02, 0.99, 0.012, 0.06, -0.04, 1.0),
    )
    expected_center = motion.transform_point(0.24, 0.31)
    update = pool.update(
        frame_id="homography-2",
        captured_at_s=1.05,
        observations=(),
        camera_motion=motion,
        motion_hints=(TargetMotionHint(track_id, 0.0, 0.0, confidence=0.9),),
        visual_confirmation_track_ids=(track_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == track_id)
    assert motion.homography is not None
    assert update.visual_confirmed_track_ids == (track_id,)
    assert tracked.state is UnifiedTrackState.TRACKING
    assert tracked.bbox.center == pytest.approx(expected_center, abs=0.003)


def test_camera_homography_rejects_a_centre_projective_pole() -> None:
    with pytest.raises(ValueError, match="projective pole"):
        CameraMotionEstimate(
            dx=0.0,
            dy=0.0,
            confidence=0.95,
            homography=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, -1.0, -1.0, 1.0),
        )


def test_visual_confirmation_rotates_velocity_into_the_current_camera_frame() -> None:
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    initial = pool.update(
        frame_id="velocity-roll-1",
        captured_at_s=1.0,
        observations=(_observation(0.30, 0.40),),
    )
    track_id = initial.tracks[0].track_id
    moving = pool.update(
        frame_id="velocity-roll-2",
        captured_at_s=1.1,
        observations=(_observation(0.36, 0.40),),
    )
    assert moving.tracks[0].velocity_x_s > 0.0

    update = pool.update(
        frame_id="velocity-roll-3",
        captured_at_s=1.2,
        observations=(),
        camera_motion=CameraMotionEstimate(
            dx=0.0,
            dy=0.0,
            confidence=0.95,
            rotation_deg=90.0,
        ),
        motion_hints=(TargetMotionHint(track_id, 0.0, 0.0, confidence=0.9),),
        visual_confirmation_track_ids=(track_id,),
    )

    tracked = next(track for track in update.tracks if track.track_id == track_id)
    assert abs(tracked.velocity_x_s) < 0.02
    assert tracked.velocity_y_s > 0.0


def test_short_term_motion_hint_corrects_prediction_without_becoming_an_observation() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            minimum_iou=0.1,
            maximum_center_distance=0.04,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.5),),
    )
    track_id = first.tracks[0].track_id
    update = pool.update(
        frame_id="frame-2",
        captured_at_s=1.1,
        observations=(_observation(0.4, 0.5),),
        motion_hints=(
            TargetMotionHint(
                track_id=track_id,
                residual_dx=0.2,
                residual_dy=0.0,
                confidence=0.9,
            ),
        ),
    )

    assert len(update.tracks) == 1
    assert update.tracks[0].track_id == track_id
    assert update.accepted_motion_hint_count == 1
    assert update.rejected_motion_hint_count == 0
    assert update.tracks[0].observation_count == 2


def test_motion_hint_cannot_recover_lost_identity_without_reid() -> None:
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.5),),
    )
    original_id = first.tracks[0].track_id
    pool.update(frame_id="frame-2", captured_at_s=1.5, observations=())
    update = pool.update(
        frame_id="frame-3",
        captured_at_s=1.6,
        observations=(_observation(0.4, 0.5),),
        motion_hints=(
            TargetMotionHint(
                track_id=original_id,
                residual_dx=0.2,
                residual_dy=0.0,
                confidence=0.95,
                source="template_correlation",
            ),
        ),
    )

    assert update.accepted_motion_hint_count == 1
    assert update.recovered_track_ids == ()
    assert len(update.tracks) == 2
    assert next(track for track in update.tracks if track.track_id == original_id).state is (
        UnifiedTrackState.LOST
    )


def test_low_confidence_motion_hint_is_rejected() -> None:
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_iou=0.1, maximum_center_distance=0.04))
    first = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.2, 0.5),),
    )
    update = pool.update(
        frame_id="frame-2",
        captured_at_s=1.1,
        observations=(_observation(0.4, 0.5),),
        motion_hints=(
            TargetMotionHint(
                track_id=first.tracks[0].track_id,
                residual_dx=0.2,
                residual_dy=0.0,
                confidence=0.2,
            ),
        ),
    )

    assert update.accepted_motion_hint_count == 0
    assert update.rejected_motion_hint_count == 1
    assert len(update.tracks) == 2


def test_locked_tracks_are_not_evicted_when_pool_is_full() -> None:
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(maximum_tracks=10))
    initial = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=tuple(_observation(0.06 + index * 0.09, 0.3) for index in range(10)),
    )
    locked_id = initial.tracks[0].track_id
    pool.lock(locked_id, now_s=1.01)
    update = pool.update(
        frame_id="frame-2",
        captured_at_s=1.1,
        observations=(_observation(0.5, 0.75, label="person"),),
    )

    ids = {track.track_id for track in update.tracks}
    assert locked_id in ids
    assert len(ids) == 10
    assert update.removed_track_ids


def test_pool_rejects_duplicate_or_out_of_order_frames() -> None:
    pool = UnifiedTargetPool()
    pool.update(frame_id="frame-1", captured_at_s=1.0, observations=())

    with pytest.raises(ValueError, match="duplicate frame_id"):
        pool.update(frame_id="frame-1", captured_at_s=1.1, observations=())
    with pytest.raises(ValueError, match="strictly increasing"):
        pool.update(frame_id="frame-2", captured_at_s=0.9, observations=())


def test_unlocked_or_uncertain_target_cannot_become_primary() -> None:
    pool = UnifiedTargetPool()
    update = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(_observation(0.3, 0.4),),
    )
    track_id = update.tracks[0].track_id

    with pytest.raises(ValueError, match="locked target pool"):
        pool.switch_primary(track_id, now_s=1.1)
