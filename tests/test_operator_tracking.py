from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox, TrackSnapshot
from multidetect.operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    VideoGeometry,
)
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig

GEOMETRY = VideoGeometry("camera-main", 1280, 720)
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
SELECTION_BBOX = BoundingBox(0.32, 0.21, 0.61, 0.72)


def _track(
    track_id: str,
    bbox: BoundingBox,
    *,
    label: str = "flame",
    last_seen_at_s: float = 100.0,
    confidence: float = 0.9,
    confirmed: bool = True,
    consecutive: int = 5,
) -> TrackSnapshot:
    return TrackSnapshot(
        track_id=track_id,
        revision=4,
        label=label,
        bbox=bbox,
        first_seen_at_s=99.0,
        last_seen_at_s=last_seen_at_s,
        observation_count=5,
        consecutive_observations=consecutive,
        confidence_floor=confidence - 0.05,
        confidence_mean=confidence,
        maximum_gap_s=0.1,
        area_growth_rate=0.0,
        thermal_corroborated=False,
        confirmed=confirmed,
    )


def _command(
    *,
    action: SelectionAction = SelectionAction.SELECT,
    bbox: BoundingBox | None = SELECTION_BBOX,
) -> TargetSelectionCommand:
    return TargetSelectionCommand(
        command_id=COMMAND_ID,
        session_id=SESSION_ID,
        sequence=1,
        action=action,
        geometry=GEOMETRY,
        issued_at_s=100.0,
        expires_at_s=103.0,
        bbox=bbox,
    )


def _lock() -> OperatorTargetLock:
    return OperatorTargetLock(
        GEOMETRY,
        TargetLockConfig(frozenset({"flame", "smoke"})),
    )


def test_selection_associates_best_allowed_fire_track_not_person() -> None:
    target_lock = _lock()
    tracks = (
        _track("track-person", BoundingBox(0.35, 0.25, 0.55, 0.65), label="person"),
        _track("track-fire", BoundingBox(0.40, 0.30, 0.58, 0.68)),
    )

    status = target_lock.apply_command(
        _command(),
        tracks=tracks,
        frame_id="frame-100",
        now_s=100.0,
    )

    assert status.state is TrackingState.TRACKING
    assert status.target_id == "track-fire"
    assert target_lock.active_track_id == "track-fire"
    assert status.tracking_quality == pytest.approx(0.9)


def test_empty_selection_initializes_then_acquires_track_on_later_frame() -> None:
    target_lock = _lock()

    initializing = target_lock.apply_command(
        _command(),
        tracks=(),
        frame_id="frame-100",
        now_s=100.0,
    )
    acquired = target_lock.update(
        tracks=(_track("track-fire", BoundingBox(0.4, 0.3, 0.55, 0.6), last_seen_at_s=100.2),),
        frame_id="frame-101",
        captured_at_s=100.2,
        produced_at_s=100.21,
    )

    assert initializing.state is TrackingState.INITIALIZING
    assert initializing.target_id is not None and initializing.target_id.startswith("pending:")
    assert acquired is not None
    assert acquired.state is TrackingState.TRACKING
    assert acquired.target_id == "track-fire"


def test_acquisition_timeout_rejects_without_selecting_an_unseen_target() -> None:
    target_lock = _lock()
    target_lock.apply_command(_command(), tracks=(), frame_id="frame-100", now_s=100.0)

    rejected = target_lock.update(
        tracks=(),
        frame_id="frame-101",
        captured_at_s=101.1,
        produced_at_s=101.1,
    )
    quiet = target_lock.update(
        tracks=(),
        frame_id="frame-102",
        captured_at_s=101.2,
        produced_at_s=101.2,
    )

    assert rejected is not None and rejected.state is TrackingState.REJECTED
    assert quiet is None


def test_locked_track_updates_bbox_then_reports_lost_after_freshness_timeout() -> None:
    target_lock = _lock()
    target_lock.apply_command(
        _command(),
        tracks=(_track("track-fire", BoundingBox(0.4, 0.3, 0.55, 0.6)),),
        frame_id="frame-100",
        now_s=100.0,
    )
    updated_track = _track(
        "track-fire",
        BoundingBox(0.42, 0.31, 0.57, 0.61),
        last_seen_at_s=100.2,
        consecutive=3,
    )

    tracking = target_lock.update(
        tracks=(updated_track,),
        frame_id="frame-101",
        captured_at_s=100.2,
        produced_at_s=100.21,
    )
    lost = target_lock.update(
        tracks=(updated_track,),
        frame_id="frame-102",
        captured_at_s=101.0,
        produced_at_s=101.0,
    )

    assert tracking is not None and tracking.state is TrackingState.TRACKING
    assert tracking.bbox == updated_track.bbox
    assert tracking.tracking_quality is not None and tracking.tracking_quality < 0.9
    assert lost is not None and lost.state is TrackingState.LOST
    assert lost.target_id == "track-fire"
    assert target_lock.active_track_id is None


def test_cancel_clears_lock_and_returns_cancelled_status() -> None:
    target_lock = _lock()
    target_lock.apply_command(
        _command(),
        tracks=(_track("track-fire", BoundingBox(0.4, 0.3, 0.55, 0.6)),),
        frame_id="frame-100",
        now_s=100.0,
    )

    cancelled = target_lock.apply_command(
        _command(action=SelectionAction.CANCEL, bbox=None),
        tracks=(),
        frame_id="frame-101",
        now_s=100.1,
    )

    assert cancelled.state is TrackingState.CANCELLED
    assert target_lock.active_track_id is None
    assert cancelled.bbox is None
