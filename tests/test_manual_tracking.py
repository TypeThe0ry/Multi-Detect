from __future__ import annotations

from multidetect.domain import BoundingBox
from multidetect.manual_tracking import OpenCVManualTargetTracker
from multidetect.operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    VideoGeometry,
)


class _Tracker:
    def __init__(self) -> None:
        self.initialized_with = None
        self.updates = [(True, (96.0, 72.0, 256.0, 192.0)), (False, (0, 0, 0, 0))]

    def init(self, image, bbox):
        self.initialized_with = (image, bbox)
        return True

    def update(self, _image):
        return self.updates.pop(0)


def _command(action: SelectionAction, *, bbox: BoundingBox | None) -> TargetSelectionCommand:
    return TargetSelectionCommand(
        command_id="66666666-6666-4666-8666-666666666666",
        session_id="77777777-7777-4777-8777-777777777777",
        sequence=1,
        action=action,
        geometry=VideoGeometry("local-camera", 640, 480),
        issued_at_s=10.0,
        expires_at_s=13.0,
        bbox=bbox,
        displayed_frame_id="frame-1",
    )


def test_manual_tracker_follows_selected_box_and_reports_lost() -> None:
    backend = _Tracker()
    tracker = OpenCVManualTargetTracker(
        VideoGeometry("local-camera", 640, 480),
        tracker_factory=lambda: backend,
    )
    image = object()

    selected = tracker.apply_command(
        _command(SelectionAction.SELECT, bbox=BoundingBox(0.1, 0.1, 0.5, 0.5)),
        image_bgr=image,
        frame_id="frame-1",
        now_s=10.0,
    )

    assert selected.state is TrackingState.TRACKING
    assert selected.label == "manual"
    assert selected.tracking_quality is None
    assert backend.initialized_with == (image, (64, 48, 256, 192))

    moved = tracker.update(
        image_bgr=image,
        frame_id="frame-2",
        captured_at_s=10.1,
        produced_at_s=10.1,
    )
    assert moved is not None
    assert moved.state is TrackingState.TRACKING
    assert moved.bbox == BoundingBox(0.15, 0.15, 0.55, 0.55)

    reacquiring = tracker.update(
        image_bgr=image,
        frame_id="frame-3",
        captured_at_s=10.2,
        produced_at_s=10.2,
    )
    assert reacquiring is not None
    assert reacquiring.state is TrackingState.INITIALIZING
    assert tracker.active is True

    lost = tracker.update(
        image_bgr=image,
        frame_id="frame-4",
        captured_at_s=12.3,
        produced_at_s=12.3,
    )
    assert lost is not None
    assert lost.state is TrackingState.LOST
    assert tracker.active is False


def test_manual_tracker_cancel_is_fail_safe() -> None:
    tracker = OpenCVManualTargetTracker(
        VideoGeometry("local-camera", 640, 480),
        tracker_factory=_Tracker,
    )
    cancelled = tracker.apply_command(
        _command(SelectionAction.CANCEL, bbox=None),
        image_bgr=object(),
        frame_id="frame-1",
        now_s=10.0,
    )

    assert cancelled.state is TrackingState.CANCELLED
    assert tracker.active is False


def test_manual_tracker_reinitializes_after_bounded_reacquisition() -> None:
    first = _Tracker()
    first.updates = [(False, (0, 0, 0, 0))]
    second = _Tracker()
    backends = iter((first, second))
    tracker = OpenCVManualTargetTracker(
        VideoGeometry("local-camera", 640, 480),
        tracker_factory=lambda: next(backends),
    )
    image = object()
    tracker.apply_command(
        _command(SelectionAction.SELECT, bbox=BoundingBox(0.1, 0.1, 0.5, 0.5)),
        image_bgr=image,
        frame_id="frame-1",
        now_s=10.0,
    )
    tracker._reacquire_from_template = lambda _image: (
        BoundingBox(0.2, 0.2, 0.6, 0.6),
        0.8,
    )

    reacquired = tracker.update(
        image_bgr=image,
        frame_id="frame-2",
        captured_at_s=10.1,
        produced_at_s=10.1,
    )

    assert reacquired is not None
    assert reacquired.state is TrackingState.TRACKING
    assert reacquired.bbox == BoundingBox(0.2, 0.2, 0.6, 0.6)
    assert reacquired.tracking_quality == 0.8
    assert second.initialized_with == (image, (128, 96, 256, 192))
