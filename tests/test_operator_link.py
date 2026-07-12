from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_link import (
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)

GEOMETRY = VideoGeometry("camera-main", 1280, 720)


def _selection(**overrides: object) -> TargetSelectionCommand:
    values: dict[str, object] = {
        "command_id": "selection-1",
        "session_id": "operator-session-1",
        "sequence": 1,
        "action": SelectionAction.SELECT,
        "geometry": GEOMETRY,
        "issued_at_s": 100.0,
        "expires_at_s": 103.0,
        "bbox": BoundingBox(0.32, 0.21, 0.61, 0.72),
        "displayed_frame_id": "g20-frame-500",
    }
    values.update(overrides)
    return TargetSelectionCommand(**values)


def test_accepts_fresh_normalized_selection_once() -> None:
    guard = SelectionCommandGuard(GEOMETRY)
    command = _selection()

    assert guard.evaluate(command, received_at_s=100.2).allowed is True
    replay = guard.evaluate(command, received_at_s=100.3)
    assert replay.allowed is False
    assert "already been processed" in " ".join(replay.reasons)


def test_rejects_stale_wrong_stream_and_mismatched_geometry() -> None:
    guard = SelectionCommandGuard(GEOMETRY, clock_tolerance_s=0.0)
    command = _selection(
        geometry=VideoGeometry("camera-secondary", 1920, 1080, rotation_degrees=90)
    )

    result = guard.evaluate(command, received_at_s=103.1)

    assert result.allowed is False
    combined = " ".join(result.reasons)
    assert "stream" in combined
    assert "dimensions" in combined
    assert "rotation" in combined
    assert "stale" in combined


def test_rejects_out_of_order_sequence_without_consuming_command_id() -> None:
    guard = SelectionCommandGuard(GEOMETRY)
    assert guard.evaluate(_selection(sequence=10), received_at_s=100.1).allowed is True

    old = _selection(command_id="selection-old", sequence=9)
    assert guard.evaluate(old, received_at_s=100.2).allowed is False

    newer_session = _selection(
        command_id="selection-old",
        session_id="operator-session-2",
        sequence=1,
    )
    assert guard.evaluate(newer_session, received_at_s=100.3).allowed is True


def test_cancel_requires_no_bbox_and_selection_requires_bbox() -> None:
    cancel = _selection(action=SelectionAction.CANCEL, bbox=None)
    assert cancel.bbox is None

    with pytest.raises(ValueError, match="cannot contain"):
        _selection(action=SelectionAction.CANCEL)
    with pytest.raises(ValueError, match="require a bounding box"):
        _selection(bbox=None)


def test_selection_is_not_allowed_to_have_a_long_lived_ttl() -> None:
    with pytest.raises(ValueError, match="TTL"):
        _selection(expires_at_s=106.0)


def test_active_track_status_carries_overlay_metadata_only() -> None:
    status = TrackStatusMessage(
        status_id="status-1",
        selection_command_id="selection-1",
        sequence=1,
        geometry=GEOMETRY,
        state=TrackingState.TRACKING,
        target_id="track-42",
        bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
        label="flame",
        confidence=0.91,
        tracking_quality=0.87,
        source_frame_id="jetson-frame-700",
        source_captured_at_s=100.15,
        produced_at_s=100.18,
        relative_bearing_deg=-4.2,
        estimated_range_m=82.0,
    )

    assert status.geometry.stream_id == "camera-main"
    assert status.state is TrackingState.TRACKING


def test_active_track_status_requires_target_and_box() -> None:
    with pytest.raises(ValueError, match="target ID and bounding box"):
        TrackStatusMessage(
            status_id="status-1",
            selection_command_id="selection-1",
            sequence=1,
            geometry=GEOMETRY,
            state=TrackingState.TRACKING,
            target_id=None,
            bbox=None,
            label=None,
            confidence=None,
            tracking_quality=None,
            source_frame_id="jetson-frame-700",
            source_captured_at_s=100.15,
            produced_at_s=100.18,
        )
