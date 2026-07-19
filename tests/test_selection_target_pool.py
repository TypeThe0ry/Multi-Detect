from __future__ import annotations

from uuid import uuid4

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_bridge import OperatorBridgeResult
from multidetect.operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.selection_target_pool import SelectionTargetPoolConfig, UnifiedSelectionTargetPool
from multidetect.unified_tracking import (
    TargetMotionHint,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTargetPoolConfig,
    UnifiedTrackState,
)

GEOMETRY = VideoGeometry("camera-main", 1280, 720)
PEER = ("192.168.144.11", 14580)


def _command(
    sequence: int,
    action: SelectionAction,
    bbox: BoundingBox | None,
) -> TargetSelectionCommand:
    return TargetSelectionCommand(
        command_id=str(uuid4()),
        session_id="22222222-2222-4222-8222-222222222222",
        sequence=sequence,
        action=action,
        geometry=GEOMETRY,
        issued_at_s=100.0 + sequence * 0.1,
        expires_at_s=102.0 + sequence * 0.1,
        bbox=bbox,
    )


def _status(
    command: TargetSelectionCommand,
    *,
    state: TrackingState,
    bbox: BoundingBox | None,
    label: str | None,
    target_id: str | None = "operator-target",
) -> TrackStatusMessage:
    return TrackStatusMessage(
        status_id=str(uuid4()),
        selection_command_id=command.command_id,
        sequence=command.sequence,
        geometry=GEOMETRY,
        state=state,
        target_id=target_id,
        bbox=bbox,
        label=label,
        confidence=0.9 if state is TrackingState.TRACKING else None,
        tracking_quality=0.85 if state is TrackingState.TRACKING else 0.0,
        source_frame_id=f"frame-{command.sequence}",
        source_captured_at_s=100.0 + command.sequence * 0.1,
        produced_at_s=100.01 + command.sequence * 0.1,
    )


def _bridge_result(
    *,
    command: TargetSelectionCommand | None = None,
    status: TrackStatusMessage | None = None,
) -> OperatorBridgeResult:
    return OperatorBridgeResult(
        accepted_command_count=int(command is not None),
        published_statuses=() if status is None else (status,),
        published_mission_statuses=(),
        published_safety_statuses=(),
        accepted_authorization_decisions=(),
        published_authorization_challenges=(),
        transport_errors=(),
        accepted_selection_commands=() if command is None else ((command, PEER),),
    )


def _observation(label: str, bbox: BoundingBox) -> TargetObservation:
    return TargetObservation(label=label, confidence=0.95, bbox=bbox)


def test_operator_selection_binds_existing_detection_and_makes_it_primary() -> None:
    bbox = BoundingBox(0.3, 0.3, 0.5, 0.6)
    pool = UnifiedTargetPool()
    update = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(_observation("flame", bbox),),
    )
    target_id = update.tracks[0].track_id
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT, bbox)

    result = coordinator.consume_bridge_result(
        _bridge_result(
            command=command,
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="flame"),
        ),
        now_s=100.11,
    )

    selected = pool.snapshots()[0]
    assert result.bound_track_id == target_id
    assert result.active_track_id == target_id
    assert result.primary_switch_latency_ms is not None
    assert result.primary_switch_latency_ms < 200.0
    assert selected.locked is True and selected.primary is True
    assert result.metadata_only is True and result.flight_control_enabled is False


def test_explicit_trk_then_lck_keeps_one_primary_lock() -> None:
    first_box = BoundingBox(0.1, 0.2, 0.25, 0.5)
    second_box = BoundingBox(0.65, 0.2, 0.8, 0.5)
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("vehicle", first_box),
            _observation("person", second_box),
        ),
    ).tracks
    for index in range(2, 4):
        pool.update(
            frame_id=f"frame-{index}",
            captured_at_s=100.0 + index * 0.01,
            observations=(
                _observation("vehicle", first_box),
                _observation("person", second_box),
            ),
        )
    coordinator = UnifiedSelectionTargetPool(pool)

    select_first = _command(1, SelectionAction.SELECT_TRK, first_box)
    coordinator.consume_bridge_result(
        _bridge_result(
            command=select_first,
            status=_status(
                select_first,
                state=TrackingState.TRACKING,
                bbox=first_box,
                label="vehicle",
            ),
        ),
        now_s=100.11,
    )
    first_snapshot = {item.track_id: item for item in pool.snapshots()}[tracks[0].track_id]
    assert first_snapshot.locked is False and first_snapshot.primary is False

    promote_first = _command(2, SelectionAction.PROMOTE_LCK, first_box)
    coordinator.consume_bridge_result(_bridge_result(command=promote_first), now_s=100.21)
    first_snapshot = {item.track_id: item for item in pool.snapshots()}[tracks[0].track_id]
    assert first_snapshot.locked is True and first_snapshot.primary is True

    select_second = _command(3, SelectionAction.SELECT_TRK, second_box)
    coordinator.consume_bridge_result(
        _bridge_result(
            command=select_second,
            status=_status(
                select_second,
                state=TrackingState.TRACKING,
                bbox=second_box,
                label="person",
            ),
        ),
        now_s=100.31,
    )
    snapshots = {item.track_id: item for item in pool.snapshots()}
    assert snapshots[tracks[0].track_id].locked is True
    assert snapshots[tracks[1].track_id].locked is False
    assert set(coordinator.tracked_track_ids) == {
        tracks[0].track_id,
        tracks[1].track_id,
    }

    promote_second = _command(4, SelectionAction.PROMOTE_LCK, second_box)
    coordinator.consume_bridge_result(_bridge_result(command=promote_second), now_s=100.41)
    snapshots = {item.track_id: item for item in pool.snapshots()}
    assert snapshots[tracks[0].track_id].locked is False
    assert snapshots[tracks[1].track_id].locked is True
    assert snapshots[tracks[1].track_id].primary is True
    assert coordinator.tracked_track_ids == (tracks[1].track_id,)
    assert coordinator.exclusive_lock_track_id == tracks[1].track_id
    assert coordinator.exclusive_high_rate is True


def test_trk_keeps_three_operator_selected_targets_concurrently() -> None:
    boxes = (
        BoundingBox(0.05, 0.2, 0.2, 0.5),
        BoundingBox(0.4, 0.2, 0.55, 0.5),
        BoundingBox(0.75, 0.2, 0.9, 0.5),
    )
    labels = ("vehicle", "person", "flame")
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=tuple(
            _observation(label, bbox) for label, bbox in zip(labels, boxes, strict=True)
        ),
    ).tracks
    coordinator = UnifiedSelectionTargetPool(pool)

    for sequence, (bbox, label) in enumerate(zip(boxes, labels, strict=True), start=1):
        command = _command(sequence, SelectionAction.SELECT_TRK, bbox)
        result = coordinator.consume_bridge_result(
            _bridge_result(
                command=command,
                status=_status(
                    command,
                    state=TrackingState.TRACKING,
                    bbox=bbox,
                    label=label,
                ),
            ),
            now_s=100.01 + sequence * 0.1,
        )

    assert set(result.tracked_track_ids) == {track.track_id for track in tracks}
    assert len(result.tracked_track_ids) == 3
    assert all(not track.locked and not track.primary for track in pool.snapshots())
    assert coordinator.visual_confirmation_track_ids == tuple(
        sorted(track.track_id for track in tracks)
    )
    assert coordinator.exclusive_high_rate is False


def test_recognized_multi_trk_uses_validated_visual_hints_between_detector_passes() -> None:
    boxes = (
        BoundingBox(0.12, 0.20, 0.30, 0.70),
        BoundingBox(0.62, 0.18, 0.88, 0.74),
    )
    pool = UnifiedTargetPool(UnifiedTargetPoolConfig(minimum_confirmed_hits=1))
    tracks = pool.update(
        frame_id="recognized-1",
        captured_at_s=100.0,
        observations=(_observation("person", boxes[0]), _observation("chair", boxes[1])),
    ).tracks
    # A second detector observation makes the identities stable before the
    # operator selects them, matching a real common-detector cadence.
    pool.update(
        frame_id="recognized-2",
        captured_at_s=100.1,
        observations=(_observation("person", boxes[0]), _observation("chair", boxes[1])),
    )
    coordinator = UnifiedSelectionTargetPool(pool)
    for sequence, (bbox, label) in enumerate(
        ((boxes[0], "person"), (boxes[1], "chair")),
        start=1,
    ):
        command = _command(sequence, SelectionAction.SELECT_TRK, bbox)
        coordinator.consume_bridge_result(
            _bridge_result(
                command=command,
                status=_status(
                    command,
                    state=TrackingState.TRACKING,
                    bbox=bbox,
                    label=label,
                ),
            ),
            now_s=100.2 + sequence * 0.01,
        )

    selected_ids = coordinator.visual_confirmation_track_ids
    assert selected_ids == tuple(sorted(track.track_id for track in tracks))

    update = pool.update(
        frame_id="recognized-detector-gap",
        captured_at_s=100.3,
        observations=(),
        motion_hints=tuple(
            TargetMotionHint(
                track_id=track_id,
                residual_dx=0.0,
                residual_dy=0.0,
                confidence=0.9,
            )
            for track_id in selected_ids
        ),
        visual_confirmation_track_ids=selected_ids,
    )

    by_id = {track.track_id: track for track in update.tracks}
    assert all(by_id[track_id].state is UnifiedTrackState.TRACKING for track_id in selected_ids)
    assert all(by_id[track_id].missed_frame_count == 0 for track_id in selected_ids)
    assert update.visual_confirmed_track_ids == selected_ids


def test_two_manual_trk_selections_both_receive_visual_confirmation() -> None:
    boxes = (
        BoundingBox(0.08, 0.2, 0.28, 0.62),
        BoundingBox(0.70, 0.18, 0.92, 0.60),
    )
    pool = UnifiedTargetPool()
    coordinator = UnifiedSelectionTargetPool(pool)
    bound_ids: list[str] = []

    for sequence, bbox in enumerate(boxes, start=1):
        command = _command(sequence, SelectionAction.SELECT_TRK, bbox)
        queued = coordinator.consume_bridge_result(
            _bridge_result(
                command=command,
                status=_status(
                    command,
                    state=TrackingState.TRACKING,
                    bbox=bbox,
                    label="manual",
                ),
            ),
            now_s=100.0 + sequence * 0.2,
        )
        assert queued.pending_manual_observation is True
        pool.update(
            frame_id=f"manual-{sequence}",
            captured_at_s=100.05 + sequence * 0.2,
            observations=coordinator.observations_for_next_pool_update(),
        )
        bound = coordinator.after_pool_update(now_s=100.06 + sequence * 0.2)
        assert bound.bound_track_id is not None
        bound_ids.append(bound.bound_track_id)

    expected = tuple(sorted(bound_ids))
    assert len(set(bound_ids)) == 2
    assert coordinator.tracked_track_ids == expected
    assert coordinator.visual_confirmation_track_ids == expected
    assert all(track.label == "manual" for track in pool.snapshots())


def test_select_trk_queues_manual_fallback_before_legacy_lock_status() -> None:
    """A moving manual rectangle must not wait on the singleton legacy lock."""

    bbox = BoundingBox(0.32, 0.18, 0.58, 0.72)
    pool = UnifiedTargetPool()
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT_TRK, bbox)

    queued = coordinator.consume_bridge_result(
        _bridge_result(command=command),
        now_s=100.11,
    )

    assert queued.pending_manual_observation is True
    assert coordinator.observations_for_next_pool_update()[0].bbox == bbox

    pool.update(
        frame_id="manual-fallback-1",
        captured_at_s=100.12,
        observations=coordinator.observations_for_next_pool_update(),
    )
    bound = coordinator.after_pool_update(now_s=100.13)

    assert bound.bound_track_id is not None
    assert coordinator.tracked_track_ids == (bound.bound_track_id,)
    assert coordinator.visual_confirmation_track_ids == (bound.bound_track_id,)


def test_transient_legacy_lost_does_not_clear_bound_manual_trk() -> None:
    bbox = BoundingBox(0.32, 0.18, 0.58, 0.72)
    pool = UnifiedTargetPool()
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT_TRK, bbox)
    coordinator.consume_bridge_result(_bridge_result(command=command), now_s=100.11)
    pool.update(
        frame_id="manual-bound-1",
        captured_at_s=100.12,
        observations=coordinator.observations_for_next_pool_update(),
    )
    bound = coordinator.after_pool_update(now_s=100.13)
    assert bound.bound_track_id is not None

    result = coordinator.consume_bridge_result(
        _bridge_result(
            status=_status(
                command,
                state=TrackingState.LOST,
                bbox=None,
                label=None,
            )
        ),
        now_s=100.14,
    )

    assert coordinator.tracked_track_ids == (bound.bound_track_id,)
    assert result.reason is not None
    assert "remains active" in result.reason


def test_rapid_manual_trk_selections_queue_before_one_pool_update() -> None:
    boxes = (
        BoundingBox(0.08, 0.2, 0.28, 0.62),
        BoundingBox(0.70, 0.18, 0.92, 0.60),
    )
    pool = UnifiedTargetPool()
    coordinator = UnifiedSelectionTargetPool(pool)
    commands = tuple(
        _command(sequence, SelectionAction.SELECT_TRK, bbox)
        for sequence, bbox in enumerate(boxes, start=1)
    )

    for command, bbox in zip(commands, boxes, strict=True):
        queued = coordinator.consume_bridge_result(
            _bridge_result(
                command=command,
                status=_status(
                    command,
                    state=TrackingState.TRACKING,
                    bbox=bbox,
                    label="manual",
                ),
            ),
            now_s=100.0 + command.sequence * 0.01,
        )
        assert queued.pending_manual_observation is True

    observations = coordinator.observations_for_next_pool_update()
    assert len(observations) == 2
    assert {observation.bbox for observation in observations} == set(boxes)
    pool.update(
        frame_id="manual-batch",
        captured_at_s=100.1,
        observations=observations,
    )
    bound = coordinator.after_pool_update(now_s=100.11)

    assert bound.pending_manual_observation is False
    assert len(coordinator.tracked_track_ids) == 2
    assert coordinator.visual_confirmation_track_ids == coordinator.tracked_track_ids


def test_promote_lck_prefers_recognized_person_over_overlapping_manual_fallback() -> None:
    manual_box = BoundingBox(0.20, 0.18, 0.62, 0.82)
    person_box = BoundingBox(0.28, 0.20, 0.55, 0.78)
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("manual", manual_box),
            _observation("person", person_box),
        ),
    ).tracks
    for index in range(2, 4):
        pool.update(
            frame_id=f"frame-{index}",
            captured_at_s=100.0 + index * 0.01,
            observations=(
                _observation("manual", manual_box),
                _observation("person", person_box),
            ),
        )
    person_id = next(track.track_id for track in tracks if track.label == "person")
    coordinator = UnifiedSelectionTargetPool(pool)

    promote = _command(1, SelectionAction.PROMOTE_LCK, manual_box)
    result = coordinator.consume_bridge_result(_bridge_result(command=promote), now_s=100.11)

    snapshots = {track.track_id: track for track in pool.snapshots()}
    assert result.active_track_id == person_id
    assert result.tracked_track_ids == (person_id,)
    assert snapshots[person_id].locked is True
    assert snapshots[person_id].primary is True


def test_promote_lck_rejects_a_low_quality_operator_track() -> None:
    bbox = BoundingBox(0.22, 0.20, 0.46, 0.66)
    pool = UnifiedTargetPool()
    track_id = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(_observation("person", bbox),),
    ).tracks[0].track_id
    for index in range(2, 4):
        pool.update(
            frame_id=f"frame-{index}",
            captured_at_s=100.0 + index * 0.01,
            observations=(_observation("person", bbox),),
        )
    coordinator = UnifiedSelectionTargetPool(
        pool,
        SelectionTargetPoolConfig(minimum_lock_tracking_quality=0.99),
    )

    result = coordinator.consume_bridge_result(
        _bridge_result(command=_command(1, SelectionAction.PROMOTE_LCK, bbox)),
        now_s=100.11,
    )

    snapshot = {item.track_id: item for item in pool.snapshots()}[track_id]
    assert snapshot.locked is False
    assert result.reason == "candidate tracking quality is below the LCK admission threshold"


def test_cancel_trk_removes_only_the_selected_target() -> None:
    first_box = BoundingBox(0.1, 0.2, 0.25, 0.5)
    second_box = BoundingBox(0.65, 0.2, 0.8, 0.5)
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("vehicle", first_box),
            _observation("person", second_box),
        ),
    ).tracks
    coordinator = UnifiedSelectionTargetPool(pool)
    for sequence, bbox, label in (
        (1, first_box, "vehicle"),
        (2, second_box, "person"),
    ):
        command = _command(sequence, SelectionAction.SELECT_TRK, bbox)
        coordinator.consume_bridge_result(
            _bridge_result(
                command=command,
                status=_status(
                    command,
                    state=TrackingState.TRACKING,
                    bbox=bbox,
                    label=label,
                ),
            ),
            now_s=100.01 + sequence * 0.1,
        )

    cancel_first = _command(3, SelectionAction.CANCEL_TRK, first_box)
    result = coordinator.consume_bridge_result(
        _bridge_result(command=cancel_first),
        now_s=100.31,
    )

    assert result.active_track_id == tracks[1].track_id
    assert result.tracked_track_ids == (tracks[1].track_id,)
    assert coordinator.tracked_track_ids == (tracks[1].track_id,)
    assert coordinator.exclusive_high_rate is False


def test_arbitrary_manual_box_becomes_stable_unified_track_on_next_frame() -> None:
    bbox = BoundingBox(0.2, 0.2, 0.45, 0.55)
    pool = UnifiedTargetPool()
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT, bbox)
    queued = coordinator.consume_bridge_result(
        _bridge_result(
            command=command,
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="manual"),
        ),
        now_s=100.11,
    )

    observations = coordinator.observations_for_next_pool_update()
    update = pool.update(
        frame_id="frame-2",
        captured_at_s=100.2,
        observations=observations,
    )
    bound = coordinator.after_pool_update(now_s=100.21)

    assert queued.pending_manual_observation is True
    assert len(observations) == 1 and observations[0].label == "manual"
    assert bound.bound_track_id == update.tracks[0].track_id
    assert bound.active_track_id == update.tracks[0].track_id
    assert pool.snapshots()[0].locked is True
    assert pool.snapshots()[0].primary is True


def test_recognized_binding_ignores_later_manual_tracker_label() -> None:
    bbox = BoundingBox(0.2, 0.2, 0.45, 0.55)
    pool = UnifiedTargetPool()
    recognized_id = (
        pool.update(
            frame_id="frame-1",
            captured_at_s=100.0,
            observations=(_observation("chair", bbox),),
        )
        .tracks[0]
        .track_id
    )
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT_TRK, bbox)
    coordinator.consume_bridge_result(
        _bridge_result(
            command=command,
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="chair"),
        ),
        now_s=100.11,
    )

    repeated = coordinator.consume_bridge_result(
        _bridge_result(
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="manual")
        ),
        now_s=100.12,
    )

    assert repeated.active_track_id == recognized_id
    assert repeated.tracked_track_ids == (recognized_id,)
    assert repeated.pending_manual_observation is False
    assert repeated.bound_track_id is None
    assert coordinator.observations_for_next_pool_update() == ()


def test_recognized_detection_replaces_temporary_manual_trk_identity() -> None:
    bbox = BoundingBox(0.2, 0.2, 0.45, 0.55)
    pool = UnifiedTargetPool()
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT_TRK, bbox)
    coordinator.consume_bridge_result(
        _bridge_result(
            command=command,
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="manual"),
        ),
        now_s=100.11,
    )
    pool.update(
        frame_id="frame-2",
        captured_at_s=100.2,
        observations=coordinator.observations_for_next_pool_update(),
    )
    first_binding = coordinator.after_pool_update(now_s=100.21)
    manual_id = first_binding.active_track_id
    assert manual_id is not None

    coordinator.consume_bridge_result(
        _bridge_result(
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="manual")
        ),
        now_s=100.22,
    )
    update = pool.update(
        frame_id="frame-3",
        captured_at_s=100.3,
        observations=(
            *coordinator.observations_for_next_pool_update(),
            _observation("chair", bbox),
        ),
    )
    recognized_id = next(track.track_id for track in update.tracks if track.label == "chair")
    replacement = coordinator.after_pool_update(now_s=100.31)

    assert replacement.active_track_id == recognized_id
    assert replacement.tracked_track_ids == (recognized_id,)
    assert manual_id not in replacement.tracked_track_ids
    assert coordinator.observations_for_next_pool_update() == ()


def test_switch_keeps_previous_target_locked_in_background() -> None:
    first_box = BoundingBox(0.1, 0.2, 0.25, 0.5)
    second_box = BoundingBox(0.65, 0.2, 0.8, 0.5)
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("vehicle", first_box),
            _observation("vehicle", second_box),
        ),
    ).tracks
    coordinator = UnifiedSelectionTargetPool(pool)
    first_command = _command(1, SelectionAction.SELECT, first_box)
    first = coordinator.consume_bridge_result(
        _bridge_result(
            command=first_command,
            status=_status(
                first_command,
                state=TrackingState.TRACKING,
                bbox=first_box,
                label="vehicle",
            ),
        ),
        now_s=100.11,
    )
    second_command = _command(2, SelectionAction.SWITCH, second_box)
    second = coordinator.consume_bridge_result(
        _bridge_result(
            command=second_command,
            status=_status(
                second_command,
                state=TrackingState.TRACKING,
                bbox=second_box,
                label="vehicle",
            ),
        ),
        now_s=100.21,
    )

    snapshots = {track.track_id: track for track in pool.snapshots()}
    assert first.bound_track_id is not None and second.bound_track_id is not None
    assert first.bound_track_id != second.bound_track_id
    assert snapshots[first.bound_track_id].locked is True
    assert snapshots[first.bound_track_id].primary is False
    assert snapshots[second.bound_track_id].locked is True
    assert snapshots[second.bound_track_id].primary is True
    assert second.background_locked_track_ids == (first.bound_track_id,)
    assert {track.track_id for track in tracks} == set(snapshots)


def test_cancel_clears_every_operator_trk_and_unlocks_the_session() -> None:
    first_box = BoundingBox(0.1, 0.2, 0.25, 0.5)
    second_box = BoundingBox(0.65, 0.2, 0.8, 0.5)
    pool = UnifiedTargetPool()
    pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("vehicle", first_box),
            _observation("vehicle", second_box),
        ),
    )
    coordinator = UnifiedSelectionTargetPool(pool)
    command_1 = _command(1, SelectionAction.SELECT, first_box)
    first = coordinator.consume_bridge_result(
        _bridge_result(
            command=command_1,
            status=_status(
                command_1,
                state=TrackingState.TRACKING,
                bbox=first_box,
                label="vehicle",
            ),
        ),
        now_s=100.11,
    )
    command_2 = _command(2, SelectionAction.SWITCH, second_box)
    second = coordinator.consume_bridge_result(
        _bridge_result(
            command=command_2,
            status=_status(
                command_2,
                state=TrackingState.TRACKING,
                bbox=second_box,
                label="vehicle",
            ),
        ),
        now_s=100.21,
    )
    cancel = _command(3, SelectionAction.CANCEL, None)
    cancelled = coordinator.consume_bridge_result(
        _bridge_result(command=cancel),
        now_s=100.31,
    )

    snapshots = {track.track_id: track for track in pool.snapshots()}
    assert cancelled.unlocked_track_id == second.bound_track_id
    assert snapshots[second.bound_track_id].locked is False
    assert snapshots[first.bound_track_id].locked is False
    assert coordinator.tracked_track_ids == ()


def test_new_qgc_session_drops_stale_operator_tracks_before_its_first_selection() -> None:
    first_box = BoundingBox(0.1, 0.2, 0.25, 0.5)
    second_box = BoundingBox(0.65, 0.2, 0.8, 0.5)
    pool = UnifiedTargetPool()
    tracks = pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("vehicle", first_box),
            _observation("person", second_box),
        ),
    ).tracks
    coordinator = UnifiedSelectionTargetPool(pool)
    first = _command(1, SelectionAction.SELECT_TRK, first_box)
    coordinator.consume_bridge_result(
        _bridge_result(
            command=first,
            status=_status(
                first,
                state=TrackingState.TRACKING,
                bbox=first_box,
                label="vehicle",
            ),
        ),
        now_s=100.11,
    )
    assert coordinator.tracked_track_ids == (tracks[0].track_id,)

    second = TargetSelectionCommand(
        command_id=str(uuid4()),
        session_id="33333333-3333-4333-8333-333333333333",
        sequence=1,
        action=SelectionAction.SELECT_TRK,
        geometry=GEOMETRY,
        issued_at_s=100.2,
        expires_at_s=102.2,
        bbox=second_box,
    )
    coordinator.consume_bridge_result(
        _bridge_result(
            command=second,
            status=_status(
                second,
                state=TrackingState.TRACKING,
                bbox=second_box,
                label="person",
            ),
        ),
        now_s=100.21,
    )

    assert coordinator.tracked_track_ids == (tracks[1].track_id,)


def test_ambiguous_similar_candidates_do_not_force_identity_binding() -> None:
    left = BoundingBox(0.36, 0.3, 0.46, 0.5)
    right = BoundingBox(0.54, 0.3, 0.64, 0.5)
    selection = BoundingBox(0.3, 0.2, 0.7, 0.6)
    pool = UnifiedTargetPool()
    pool.update(
        frame_id="frame-1",
        captured_at_s=100.0,
        observations=(
            _observation("vehicle", left),
            _observation("vehicle", right),
        ),
    )
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT, selection)

    result = coordinator.consume_bridge_result(
        _bridge_result(
            command=command,
            status=_status(
                command,
                state=TrackingState.TRACKING,
                bbox=selection,
                label="vehicle",
            ),
        ),
        now_s=100.11,
    )

    assert result.bound_track_id is None
    assert result.pending_manual_observation is True
    assert all(not track.locked for track in pool.snapshots())


def test_lost_manual_status_does_not_create_fabricated_observation() -> None:
    bbox = BoundingBox(0.2, 0.2, 0.45, 0.55)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=0.3,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    coordinator = UnifiedSelectionTargetPool(pool)
    command = _command(1, SelectionAction.SELECT, bbox)
    coordinator.consume_bridge_result(
        _bridge_result(
            command=command,
            status=_status(command, state=TrackingState.TRACKING, bbox=bbox, label="manual"),
        ),
        now_s=100.11,
    )
    pool.update(
        frame_id="frame-2",
        captured_at_s=100.2,
        observations=coordinator.observations_for_next_pool_update(),
    )
    coordinator.after_pool_update(now_s=100.21)
    lost_status = _status(
        command,
        state=TrackingState.LOST,
        bbox=None,
        label=None,
        target_id="manual-target",
    )

    result = coordinator.consume_bridge_result(
        _bridge_result(status=lost_status),
        now_s=100.31,
    )

    assert result.reason is not None and "conservative" in result.reason
    assert coordinator.observations_for_next_pool_update() == ()


def test_uncertain_track_cannot_be_locked_or_selected_as_primary() -> None:
    bbox = BoundingBox(0.2, 0.2, 0.4, 0.4)
    pool = UnifiedTargetPool(
        UnifiedTargetPoolConfig(
            minimum_confirmed_hits=1,
            occluded_after_s=0.1,
            reacquisition_timeout_s=1.0,
            lost_retention_s=3.0,
            locked_lost_retention_s=3.0,
        )
    )
    target_id = (
        pool.update(
            frame_id="frame-1",
            captured_at_s=1.0,
            observations=(_observation("vehicle", bbox),),
        )
        .tracks[0]
        .track_id
    )
    pool.update(frame_id="frame-2", captured_at_s=1.2, observations=())
    assert pool.snapshots()[0].state is UnifiedTrackState.REACQUIRING

    with pytest.raises(ValueError, match="uncertain target"):
        pool.lock(target_id, now_s=1.21)
