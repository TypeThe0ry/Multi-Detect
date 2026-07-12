from __future__ import annotations

from collections import deque

from multidetect.domain import BoundingBox, TrackSnapshot
from multidetect.operator_bridge import LiveOperatorBridge
from multidetect.operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    VideoGeometry,
)
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig

GEOMETRY = VideoGeometry("camera-main", 1280, 720)
PEER = ("192.168.144.11", 14580)


def _track(bbox: BoundingBox, *, last_seen_at_s: float) -> TrackSnapshot:
    return TrackSnapshot(
        track_id="track-fire",
        revision=3,
        label="flame",
        bbox=bbox,
        first_seen_at_s=99.0,
        last_seen_at_s=last_seen_at_s,
        observation_count=4,
        consecutive_observations=4,
        confidence_floor=0.86,
        confidence_mean=0.9,
        maximum_gap_s=0.1,
        area_growth_rate=0.0,
        thermal_corroborated=False,
        confirmed=True,
    )


def _command() -> TargetSelectionCommand:
    return TargetSelectionCommand(
        command_id="11111111-1111-4111-8111-111111111111",
        session_id="22222222-2222-4222-8222-222222222222",
        sequence=1,
        action=SelectionAction.SELECT,
        geometry=GEOMETRY,
        issued_at_s=100.0,
        expires_at_s=103.0,
        bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
    )


class _Transport:
    def __init__(self) -> None:
        self.commands = deque()
        self.errors = deque()
        self.published = []
        self.started = False
        self.closed = False

    def start_background(self) -> None:
        self.started = True

    def poll_selection(self):
        return self.commands.popleft() if self.commands else None

    def poll_error(self):
        return self.errors.popleft() if self.errors else None

    def publish_track_status(self, status, *, peer) -> None:
        self.published.append((status, peer))

    def close(self) -> None:
        self.closed = True


def _bridge(transport: _Transport) -> LiveOperatorBridge:
    return LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            GEOMETRY,
            TargetLockConfig(frozenset({"flame", "smoke"})),
        ),
    )


def test_bridge_consumes_selection_and_publishes_continuous_tracking_status() -> None:
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = _bridge(transport)
    bridge.start()

    selected = bridge.process_frame(
        tracks=(_track(BoundingBox(0.4, 0.3, 0.55, 0.6), last_seen_at_s=100.0),),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
    )
    updated = bridge.process_frame(
        tracks=(_track(BoundingBox(0.42, 0.31, 0.57, 0.61), last_seen_at_s=100.1),),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
    )
    bridge.close()

    assert transport.started is True and transport.closed is True
    assert selected.accepted_command_count == 1
    assert selected.published_statuses[0].state is TrackingState.TRACKING
    assert updated.accepted_command_count == 0
    assert updated.published_statuses[0].bbox == BoundingBox(0.42, 0.31, 0.57, 0.61)
    assert [peer for _, peer in transport.published] == [PEER, PEER]


def test_bridge_reports_transport_error_without_exposing_a_control_path() -> None:
    transport = _Transport()
    transport.errors.append(ValueError("bad signed datagram"))
    bridge = _bridge(transport)
    bridge.start()

    result = bridge.process_frame(
        tracks=(),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.0,
    )
    bridge.close()

    assert result.transport_errors == ("ValueError",)
    assert result.published_statuses == ()
