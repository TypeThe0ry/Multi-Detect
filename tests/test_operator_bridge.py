from __future__ import annotations

from collections import deque
from dataclasses import replace

from multidetect.approach_hil import ApproachHilPhase
from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    ReleaseTimingStatus,
    RuleCheck,
    TrackSnapshot,
    Verdict,
)
from multidetect.manual_tracking import OpenCVManualTargetTracker
from multidetect.multimodal_ranging import RangeValidity
from multidetect.operator_bridge import LiveOperatorBridge
from multidetect.operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    ReleaseStatusMessage,
    SafetyStatusMessage,
    SelectionAction,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    VideoGeometry,
)
from multidetect.operator_tracking import OperatorTargetLock, TargetLockConfig
from multidetect.patrol_advisory import PatrolPhase
from multidetect.payload_target_gate import PayloadTargetEligibility
from multidetect.unified_tracking import UnifiedTrackState

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
        self.mission_published = []
        self.safety_published = []
        self.patrol_published = []
        self.range_published = []
        self.release_published = []
        self.approach_challenges = []
        self.approach_statuses = []
        self.payload_target_challenges = []
        self.payload_target_statuses = []
        self.target_pool_statuses = []
        self.approach_confirmations = deque()
        self.payload_target_confirmations = deque()
        self.active_approach_challenge = None
        self.active_payload_target_challenge = None
        self.authorization_decisions = deque()
        self.authorization_challenges = []
        self.active_authorization_challenge = None
        self.started = False
        self.closed = False

    def start_background(self) -> None:
        self.started = True

    def poll_selection(self):
        return self.commands.popleft() if self.commands else None

    def poll_error(self):
        return self.errors.popleft() if self.errors else None

    def poll_authorization_decision(self):
        return self.authorization_decisions.popleft() if self.authorization_decisions else None

    def poll_approach_confirmation(self):
        return self.approach_confirmations.popleft() if self.approach_confirmations else None

    def poll_payload_target_confirmation(self):
        if self.payload_target_confirmations:
            return self.payload_target_confirmations.popleft()
        return None

    def set_authorization_challenge(self, status) -> None:
        self.active_authorization_challenge = status

    def set_approach_challenge(self, status) -> None:
        self.active_approach_challenge = status

    def set_payload_target_challenge(self, status) -> None:
        self.active_payload_target_challenge = status

    def publish_track_status(self, status, *, peer) -> None:
        self.published.append((status, peer))

    def publish_mission_status(self, status, *, peer) -> None:
        self.mission_published.append((status, peer))

    def publish_safety_status(self, status, *, peer) -> None:
        self.safety_published.append((status, peer))

    def publish_patrol_status(self, status, *, peer) -> None:
        self.patrol_published.append((status, peer))

    def publish_range_status(self, status, *, peer) -> None:
        self.range_published.append((status, peer))

    def publish_release_status(self, status, *, peer) -> None:
        self.release_published.append((status, peer))

    def publish_approach_challenge(self, status, *, peer) -> None:
        self.approach_challenges.append((status, peer))

    def publish_approach_status(self, status, *, peer) -> None:
        self.approach_statuses.append((status, peer))

    def publish_payload_target_challenge(self, status, *, peer) -> None:
        self.payload_target_challenges.append((status, peer))

    def publish_payload_target_status(self, status, *, peer) -> None:
        self.payload_target_statuses.append((status, peer))

    def publish_target_pool_status(self, status, *, peer) -> None:
        self.target_pool_statuses.append((status, peer))

    def publish_authorization_challenge(self, status, *, peer) -> None:
        self.authorization_challenges.append((status, peer))

    def close(self) -> None:
        self.closed = True


class _PeerAwareTransport(_Transport):
    def __init__(self) -> None:
        super().__init__()
        self.metadata_peer = PEER

    def active_metadata_peer(self):
        return self.metadata_peer


def _bridge(transport: _Transport) -> LiveOperatorBridge:
    return LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            GEOMETRY,
            TargetLockConfig(frozenset({"flame", "smoke"})),
        ),
    )


def test_bridge_publishes_detection_pool_to_heartbeat_peer_before_selection() -> None:
    transport = _PeerAwareTransport()
    bridge = _bridge(transport)
    bridge.start()

    result = bridge.process_frame(
        tracks=(),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        target_pool_statuses=_target_pool_statuses(),
    )
    transport.metadata_peer = None
    disconnected = bridge.process_frame(
        tracks=(),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        target_pool_statuses=_target_pool_statuses(),
    )
    bridge.close()

    assert result.accepted_command_count == 0
    assert bridge.active_peer is None
    assert result.published_target_pool_statuses == _target_pool_statuses()
    assert transport.target_pool_statuses == [
        (status, PEER) for status in _target_pool_statuses()
    ]
    assert disconnected.published_target_pool_statuses == ()


def test_bridge_throttles_fast_target_pool_revisions_to_stable_ui_cadence() -> None:
    transport = _PeerAwareTransport()
    bridge = _bridge(transport)
    bridge.start()

    first = bridge.process_frame(
        tracks=(),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        target_pool_statuses=_target_pool_statuses(revision=1, produced_at_s=100.01),
    )
    suppressed = bridge.process_frame(
        tracks=(),
        frame_id="frame-101",
        captured_at_s=100.05,
        produced_at_s=100.06,
        target_pool_statuses=_target_pool_statuses(revision=2, produced_at_s=100.06),
    )
    latest = bridge.process_frame(
        tracks=(),
        frame_id="frame-102",
        captured_at_s=100.22,
        produced_at_s=100.23,
        target_pool_statuses=_target_pool_statuses(revision=3, produced_at_s=100.23),
    )
    bridge.close()

    assert len(first.published_target_pool_statuses) == 2
    assert suppressed.published_target_pool_statuses == ()
    assert {status.pool_revision for status in latest.published_target_pool_statuses} == {3}


def test_bridge_can_publish_target_pool_at_twenty_five_hz_for_ground_overlay() -> None:
    transport = _PeerAwareTransport()
    bridge = _bridge(transport)
    bridge.target_pool_status_heartbeat_s = 1.0 / 25.0
    bridge.start()

    results = []
    for index, produced_at_s in enumerate((100.00, 100.02, 100.041, 100.06, 100.082), start=1):
        results.append(
            bridge.process_frame(
                tracks=(),
                frame_id=f"frame-{index}",
                captured_at_s=produced_at_s,
                produced_at_s=produced_at_s,
                target_pool_statuses=_target_pool_statuses(
                    revision=index,
                    produced_at_s=produced_at_s,
                ),
            )
        )
    bridge.close()

    assert [len(result.published_target_pool_statuses) for result in results] == [2, 0, 2, 0, 2]
    assert [status.pool_revision for status, _peer in transport.target_pool_statuses] == [
        1,
        1,
        3,
        3,
        5,
        5,
    ]


def _mission_status() -> MissionStatusMessage:
    return MissionStatusMessage(
        status_id="33333333-3333-4333-8333-333333333333",
        sequence=1,
        mission_id="fire-demo",
        phase=MissionPhase.AWAITING_AUTHORIZATION,
        authorization_state=AuthorizationDisplayState.PENDING,
        release_window=DeploymentWindowStatus.WAIT,
        safety_allowed=False,
        remaining_payload_count=1,
        total_payload_count=1,
        target_id="track-fire",
        active_payload_slot_id="payload-1",
        target_confidence=0.9,
        relative_bearing_deg=2.0,
        estimated_range_m=80.0,
        cross_track_error_m=1.0,
        along_track_error_m=20.0,
        release_lead_distance_m=60.0,
        produced_at_s=100.01,
    )


def _safety_status() -> SafetyStatusMessage:
    return SafetyStatusMessage(
        status_id="55555555-5555-4555-8555-555555555555",
        sequence=1,
        mission_id="fire-demo",
        target_id="track-fire",
        ruleset_version="rules-v1",
        checks=(
            RuleCheck("target.confirmed_track", Verdict.PASS, "confirmed"),
            RuleCheck("navigation.allowed_zone", Verdict.UNKNOWN, "unknown"),
        ),
        produced_at_s=100.01,
    )


def _patrol_status() -> PatrolStatusMessage:
    return PatrolStatusMessage(
        status_id="66666666-6666-4666-8666-666666666666",
        sequence=1,
        mission_id="fire-demo",
        phase=PatrolPhase.TRACKING,
        primary_target_id="track-fire",
        target_state=UnifiedTrackState.TRACKING,
        bbox=BoundingBox(0.4, 0.3, 0.55, 0.6),
        label="flame",
        confidence=0.9,
        tracking_quality=0.85,
        total_track_count=10,
        locked_track_count=2,
        source_frame_id="frame-100",
        source_captured_at_s=100.0,
        produced_at_s=100.01,
    )


def _range_status() -> RangeStatusMessage:
    return RangeStatusMessage(
        status_id="77777777-7777-4777-8777-777777777777",
        sequence=1,
        target_id="track-fire",
        calibration_id="camera-main-v2",
        source_frame_id="frame-100",
        source_captured_at_s=100.0,
        produced_at_s=100.01,
        validity=RangeValidity.DEGRADED,
        reasons=("single_absolute_range_method",),
        sources=("pixhawk_agl", "camera_ground"),
        rejected_sources=(),
        slant_range_m=80.0,
        ground_range_m=70.0,
        slant_range_ci95_m=(76.0, 84.0),
        ground_range_ci95_m=(66.0, 74.0),
        relative_bearing_deg=2.0,
        absolute_bearing_deg=92.0,
        bearing_sigma_deg=1.0,
        north_offset_m=-2.4,
        east_offset_m=69.9,
        data_freshness_s=0.01,
        sensor_consistency=0.5,
    )


def _release_status() -> ReleaseStatusMessage:
    return ReleaseStatusMessage(
        sequence=1,
        target_id="track-fire",
        calibration_id="release-v2",
        produced_at_s=100.01,
        timing_status=ReleaseTimingStatus.WINDOW,
        reasons=("multimodal_release_window_ready",),
        range_target_id="track-fire",
        range_frame_id="frame-100",
        target_north_offset_m=70.0,
        target_east_offset_m=2.0,
        impact_north_offset_m=69.2,
        impact_east_offset_m=2.3,
        along_track_error_m=0.8,
        cross_track_error_m=-0.3,
        error_ellipse_major_m=4.0,
        error_ellipse_minor_m=2.0,
        error_ellipse_orientation_deg=5.0,
        estimated_ground_range_m=70.0,
        ground_range_ci95_m=(66.0, 74.0),
        payload_descent_time_s=2.5,
        release_lead_distance_m=60.0,
        range_sensor_consistency=0.8,
    )


def _approach_challenge() -> ApproachChallengeStatusMessage:
    return ApproachChallengeStatusMessage(
        challenge_token=501,
        target_token=502,
        target_revision=7,
        selection_command_id=_command().command_id,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=1,
        produced_at_s=100.01,
    )


def _approach_status() -> ApproachStatusMessage:
    return ApproachStatusMessage(
        sequence=1,
        target_id="track-fire",
        target_revision=7,
        phase=ApproachHilPhase.CENTERING_SIM,
        reasons=("centering_advice_only",),
        produced_at_s=100.01,
        yaw_error_deg=2.0,
        yaw_advice_deg=2.0,
        ground_range_m=70.0,
        confirmation_expires_at_s=105.0,
    )


def _target_pool_statuses(*, revision: int = 1, produced_at_s: float = 100.01):
    entries = tuple(
        TargetPoolEntry(
            target_id=f"target-{index}",
            state=UnifiedTrackState.TRACKING,
            label="vehicle",
            confidence=0.9,
            tracking_quality=0.8,
            locked=index < 2,
            primary=index == 0,
            actionable=True,
            reid_confirmed=True,
        )
        for index in range(3)
    )
    return (
        TargetPoolStatusMessage(
            sequence=20,
            pool_revision=revision,
            page_index=0,
            page_count=2,
            total_track_count=3,
            entries=entries[:2],
            produced_at_s=produced_at_s,
        ),
        TargetPoolStatusMessage(
            sequence=21,
            pool_revision=revision,
            page_index=1,
            page_count=2,
            total_track_count=3,
            entries=entries[2:],
            produced_at_s=produced_at_s,
        ),
    )


def _approach_confirmation() -> ApproachConfirmationCommand:
    return ApproachConfirmationCommand(
        command_token=601,
        session_token=602,
        challenge_token=501,
        target_token=502,
        target_revision=7,
        selection_command_id=_command().command_id,
        sequence=2,
        issued_at_s=100.0,
        expires_at_s=102.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )


def _payload_target_challenge() -> PayloadTargetChallengeStatusMessage:
    return PayloadTargetChallengeStatusMessage(
        challenge_token=701,
        selected_target_token=702,
        selected_target_revision=7,
        aimpoint_target_token=703,
        aimpoint_target_revision=8,
        selection_command_id=_command().command_id,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=1,
        produced_at_s=100.01,
    )


def _payload_target_status() -> PayloadTargetStatusMessage:
    return PayloadTargetStatusMessage(
        sequence=1,
        selection_command_id=_command().command_id,
        selected_target_token=702,
        selected_target_revision=7,
        eligibility=PayloadTargetEligibility.ELIGIBLE_FIRE,
        produced_at_s=100.01,
        aimpoint_target_token=703,
        aimpoint_target_revision=8,
        confirmation_pending=True,
        confirmation_expires_at_s=105.0,
    )


def _payload_target_confirmation() -> PayloadTargetConfirmationCommand:
    return PayloadTargetConfirmationCommand(
        command_token=801,
        session_token=802,
        challenge_token=701,
        selected_target_token=702,
        selected_target_revision=7,
        aimpoint_target_token=703,
        aimpoint_target_revision=8,
        selection_command_id=_command().command_id,
        sequence=2,
        issued_at_s=100.0,
        expires_at_s=102.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )


def _authorization_challenge() -> AuthorizationChallengeStatusMessage:
    return AuthorizationChallengeStatusMessage(
        challenge_token=11,
        mission_token=12,
        target_token=13,
        scene_token=14,
        ruleset_token=15,
        payload_slot_token=16,
        target_revision=3,
        created_at_s=100.0,
        expires_at_s=110.0,
        sequence=1,
        produced_at_s=100.01,
    )


def _authorization_decision() -> AuthorizationDecisionCommand:
    return AuthorizationDecisionCommand(
        command_token=101,
        session_token=102,
        challenge_token=11,
        mission_token=12,
        target_token=13,
        scene_token=14,
        ruleset_token=15,
        payload_slot_token=16,
        target_revision=3,
        decision=AuthorizationDecision.APPROVE,
        operator_token=103,
        sequence=2,
        issued_at_s=100.1,
        expires_at_s=102.1,
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
        mission_status=_mission_status(),
        safety_status=_safety_status(),
        patrol_status=_patrol_status(),
        range_status=_range_status(),
        release_status=_release_status(),
        approach_challenge=_approach_challenge(),
        approach_status=_approach_status(),
        target_pool_statuses=_target_pool_statuses(),
    )
    updated = bridge.process_frame(
        tracks=(_track(BoundingBox(0.42, 0.31, 0.57, 0.61), last_seen_at_s=100.1),),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        approach_challenge=_approach_challenge(),
        approach_status=_approach_status(),
    )
    transport.approach_confirmations.append((_approach_confirmation(), PEER))
    confirmed = bridge.process_frame(
        tracks=(_track(BoundingBox(0.43, 0.32, 0.58, 0.62), last_seen_at_s=100.2),),
        frame_id="frame-102",
        captured_at_s=100.2,
        produced_at_s=100.21,
        approach_challenge=_approach_challenge(),
        approach_status=_approach_status(),
    )
    bridge.close()

    assert transport.started is True and transport.closed is True
    assert selected.accepted_command_count == 1
    assert selected.accepted_selection_commands == ((_command(), PEER),)
    assert selected.published_statuses[0].state is TrackingState.TRACKING
    assert updated.accepted_command_count == 0
    assert updated.published_statuses[0].bbox == BoundingBox(0.42, 0.31, 0.57, 0.61)
    assert [peer for _, peer in transport.published] == [PEER, PEER, PEER]
    assert selected.published_mission_statuses == (_mission_status(),)
    assert transport.mission_published == [(_mission_status(), PEER)]
    assert selected.published_safety_statuses == (_safety_status(),)
    assert transport.safety_published == [(_safety_status(), PEER)]
    assert selected.published_patrol_statuses == (_patrol_status(),)
    assert transport.patrol_published == [(_patrol_status(), PEER)]
    assert selected.published_range_statuses == (_range_status(),)
    assert transport.range_published == [(_range_status(), PEER)]
    assert selected.published_release_statuses == (_release_status(),)
    assert transport.release_published == [(_release_status(), PEER)]
    assert selected.accepted_approach_confirmations == ()
    assert selected.published_approach_challenges == ()
    assert selected.published_approach_statuses == ()
    assert selected.published_target_pool_statuses == _target_pool_statuses()
    assert transport.target_pool_statuses == [(status, PEER) for status in _target_pool_statuses()]
    assert updated.published_approach_challenges == (_approach_challenge(),)
    assert updated.published_approach_statuses == (_approach_status(),)
    assert confirmed.accepted_approach_confirmations == ((_approach_confirmation(), PEER),)
    assert transport.approach_challenges == [(_approach_challenge(), PEER)]
    assert transport.approach_statuses == [(_approach_status(), PEER)]


def test_single_track_cancel_preserves_the_other_active_track() -> None:
    first_box = BoundingBox(0.10, 0.20, 0.25, 0.50)
    second_box = BoundingBox(0.65, 0.20, 0.80, 0.50)
    first_track = replace(
        _track(first_box, last_seen_at_s=100.0),
        track_id="track-first",
    )
    second_track = replace(
        _track(second_box, last_seen_at_s=100.0),
        track_id="track-second",
    )
    first_command = replace(
        _command(),
        action=SelectionAction.SELECT_TRK,
        bbox=first_box,
    )
    second_command = replace(
        _command(),
        command_id="33333333-3333-4333-8333-333333333333",
        sequence=2,
        action=SelectionAction.SELECT_TRK,
        bbox=second_box,
        issued_at_s=100.1,
        expires_at_s=103.1,
    )
    cancel_first = replace(
        _command(),
        command_id="44444444-4444-4444-8444-444444444444",
        sequence=3,
        action=SelectionAction.CANCEL_TRK,
        bbox=first_box,
        issued_at_s=100.2,
        expires_at_s=103.2,
    )
    transport = _Transport()
    bridge = _bridge(transport)
    bridge.start()

    transport.commands.append((first_command, PEER))
    bridge.process_frame(
        tracks=(first_track, second_track),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
    )
    transport.commands.append((second_command, PEER))
    bridge.process_frame(
        tracks=(first_track, second_track),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
    )
    transport.commands.append((cancel_first, PEER))
    cancelled = bridge.process_frame(
        tracks=(first_track, second_track),
        frame_id="frame-102",
        captured_at_s=100.2,
        produced_at_s=100.21,
    )
    continued = bridge.process_frame(
        tracks=(first_track, second_track),
        frame_id="frame-103",
        captured_at_s=100.3,
        produced_at_s=100.31,
    )
    bridge.close()

    assert bridge.target_lock.active_track_id == "track-second"
    assert bridge.target_lock.selection_command_id == second_command.command_id
    assert cancelled.published_statuses[0].state is TrackingState.CANCELLED
    assert cancelled.published_statuses[0].target_id == "track-first"
    assert continued.published_statuses[-1].target_id == "track-second"


def test_bridge_invalidates_then_round_trips_payload_target_slide_binding() -> None:
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = _bridge(transport)
    bridge.start()

    selected = bridge.process_frame(
        tracks=(_track(BoundingBox(0.4, 0.3, 0.55, 0.6), last_seen_at_s=100.0),),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        payload_target_challenge=_payload_target_challenge(),
        payload_target_status=_payload_target_status(),
    )
    published = bridge.process_frame(
        tracks=(_track(BoundingBox(0.41, 0.3, 0.56, 0.6), last_seen_at_s=100.1),),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        payload_target_challenge=_payload_target_challenge(),
        payload_target_status=_payload_target_status(),
    )
    transport.payload_target_confirmations.append((_payload_target_confirmation(), PEER))
    confirmed = bridge.process_frame(
        tracks=(_track(BoundingBox(0.42, 0.3, 0.57, 0.6), last_seen_at_s=100.2),),
        frame_id="frame-102",
        captured_at_s=100.2,
        produced_at_s=100.21,
        payload_target_challenge=_payload_target_challenge(),
        payload_target_status=_payload_target_status(),
    )
    bridge.close()

    assert selected.published_payload_target_challenges == ()
    assert selected.published_payload_target_statuses == ()
    assert published.published_payload_target_challenges == (_payload_target_challenge(),)
    assert published.published_payload_target_statuses == (_payload_target_status(),)
    assert confirmed.accepted_payload_target_confirmations == (
        (_payload_target_confirmation(), PEER),
    )
    assert transport.payload_target_challenges == [(_payload_target_challenge(), PEER)]
    assert transport.payload_target_statuses == [(_payload_target_status(), PEER)]
    assert transport.active_payload_target_challenge == _payload_target_challenge()


def test_bridge_uses_manual_tracking_until_detector_reacquires_remote_selection() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.updates = [
                (True, (448.0, 180.0, 384.0, 360.0)),
                (True, (456.0, 184.0, 384.0, 360.0)),
            ]

        def init(self, _image, _bbox) -> bool:
            return True

        def update(self, _image):
            return self.updates.pop(0)

    backend = _Backend()
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            GEOMETRY,
            TargetLockConfig(frozenset({"flame", "smoke"})),
        ),
        manual_tracker_factory=lambda geometry: OpenCVManualTargetTracker(
            geometry,
            tracker_factory=lambda: backend,
        ),
    )
    image = object()
    bridge.start()

    selected = bridge.process_frame(
        tracks=(),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        image_bgr=image,
    )
    moved = bridge.process_frame(
        tracks=(),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        image_bgr=image,
    )
    detector_reacquired = bridge.process_frame(
        tracks=(_track(BoundingBox(0.40, 0.30, 0.60, 0.70), last_seen_at_s=100.2),),
        frame_id="frame-102",
        captured_at_s=100.2,
        produced_at_s=100.21,
        image_bgr=image,
    )
    bridge.close()

    assert selected.published_statuses[0].state is TrackingState.TRACKING
    assert selected.published_statuses[0].label == "manual"
    assert moved.published_statuses[0].state is TrackingState.TRACKING
    assert moved.published_statuses[0].bbox == BoundingBox(0.35, 0.25, 0.65, 0.75)
    assert detector_reacquired.published_statuses[0].state is TrackingState.TRACKING
    assert detector_reacquired.published_statuses[0].label == "flame"
    assert detector_reacquired.published_statuses[0].target_id == "track-fire"


def test_bridge_keeps_shadow_manual_tracker_ready_when_detector_loses_target() -> None:
    class _Backend:
        def __init__(self) -> None:
            self.initial_bbox = None

        def init(self, _image, bbox) -> bool:
            self.initial_bbox = bbox
            return True

        def update(self, _image):
            return True, (448.0, 180.0, 384.0, 360.0)

    backend = _Backend()
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            GEOMETRY,
            TargetLockConfig(frozenset({"flame", "smoke"})),
        ),
        manual_tracker_factory=lambda geometry: OpenCVManualTargetTracker(
            geometry,
            tracker_factory=lambda: backend,
        ),
    )
    image = object()
    bridge.start()

    selected_by_detector = bridge.process_frame(
        tracks=(_track(BoundingBox(0.40, 0.30, 0.58, 0.68), last_seen_at_s=100.0),),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        image_bgr=image,
    )
    continued_by_manual_tracker = bridge.process_frame(
        tracks=(),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        image_bgr=image,
    )
    bridge.close()

    assert selected_by_detector.published_statuses[0].label == "flame"
    assert backend.initial_bbox == (512, 216, 230, 274)
    assert continued_by_manual_tracker.published_statuses[0].state is TrackingState.TRACKING
    assert continued_by_manual_tracker.published_statuses[0].label == "manual"
    assert continued_by_manual_tracker.published_statuses[0].bbox == BoundingBox(
        0.35, 0.25, 0.65, 0.75
    )


def test_bridge_reseeds_expired_shadow_tracker_from_healthy_detector_track() -> None:
    class _FailingBackend:
        def init(self, _image, _bbox) -> bool:
            return True

        def update(self, _image):
            return False, (0.0, 0.0, 0.0, 0.0)

    class _RecoveredBackend:
        def init(self, _image, _bbox) -> bool:
            return True

        def update(self, _image):
            return True, (448.0, 180.0, 384.0, 360.0)

    backends = iter((_FailingBackend(), _RecoveredBackend()))
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = LiveOperatorBridge(
        transport,
        OperatorTargetLock(
            GEOMETRY,
            TargetLockConfig(frozenset({"flame", "smoke"})),
        ),
        manual_tracker_factory=lambda geometry: OpenCVManualTargetTracker(
            geometry,
            tracker_factory=lambda: next(backends),
        ),
    )
    image = object()
    healthy_track = _track(BoundingBox(0.40, 0.30, 0.58, 0.68), last_seen_at_s=100.0)
    bridge.start()

    bridge.process_frame(
        tracks=(healthy_track,),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        image_bgr=image,
    )
    bridge.process_frame(
        tracks=(healthy_track,),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        image_bgr=image,
    )
    reseeded = bridge.process_frame(
        tracks=(_track(BoundingBox(0.41, 0.31, 0.59, 0.69), last_seen_at_s=102.2),),
        frame_id="frame-102",
        captured_at_s=102.2,
        produced_at_s=102.21,
        image_bgr=image,
    )
    handoff = bridge.process_frame(
        tracks=(),
        frame_id="frame-103",
        captured_at_s=102.3,
        produced_at_s=102.31,
        image_bgr=image,
    )
    bridge.close()

    assert reseeded.published_statuses[0].label == "flame"
    assert handoff.published_statuses[0].state is TrackingState.TRACKING
    assert handoff.published_statuses[0].label == "manual"


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


def test_bridge_publishes_challenge_and_only_returns_decision_from_active_peer() -> None:
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = _bridge(transport)
    bridge.start()
    first = bridge.process_frame(
        tracks=(_track(BoundingBox(0.4, 0.3, 0.55, 0.6), last_seen_at_s=100.0),),
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        authorization_challenge=_authorization_challenge(),
    )
    transport.authorization_decisions.append((_authorization_decision(), PEER))
    accepted = bridge.process_frame(
        tracks=(),
        frame_id="frame-101",
        captured_at_s=100.1,
        produced_at_s=100.11,
        authorization_challenge=replace(
            _authorization_challenge(),
            sequence=2,
            produced_at_s=100.11,
        ),
    )
    bridge.close()

    assert first.published_authorization_challenges == (_authorization_challenge(),)
    assert transport.active_authorization_challenge is not None
    assert accepted.accepted_authorization_decisions == ((_authorization_decision(), PEER),)


def test_bridge_sends_changed_mission_status_immediately_and_unchanged_as_heartbeat() -> None:
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = _bridge(transport)
    bridge.start()
    base = _mission_status()
    tracks = (_track(BoundingBox(0.4, 0.3, 0.55, 0.6), last_seen_at_s=100.0),)

    first = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        mission_status=base,
        safety_status=_safety_status(),
    )
    suppressed = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-101",
        captured_at_s=100.5,
        produced_at_s=100.51,
        mission_status=replace(base, sequence=2, produced_at_s=100.51),
        safety_status=replace(_safety_status(), sequence=2, produced_at_s=100.51),
    )
    heartbeat = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-102",
        captured_at_s=101.0,
        produced_at_s=101.01,
        mission_status=replace(base, sequence=3, produced_at_s=101.01),
        safety_status=replace(_safety_status(), sequence=3, produced_at_s=101.01),
    )
    changed = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-103",
        captured_at_s=101.1,
        produced_at_s=101.11,
        mission_status=replace(
            base,
            sequence=4,
            phase=MissionPhase.DEPLOYMENT_READY,
            authorization_state=AuthorizationDisplayState.APPROVED,
            produced_at_s=101.11,
        ),
        safety_status=replace(
            _safety_status(),
            sequence=4,
            checks=(RuleCheck("target.confirmed_track", Verdict.DENY, "lost"),),
            produced_at_s=101.11,
        ),
    )
    bridge.close()

    assert len(first.published_mission_statuses) == 1
    assert suppressed.published_mission_statuses == ()
    assert len(heartbeat.published_mission_statuses) == 1
    assert len(changed.published_mission_statuses) == 1
    assert len(transport.mission_published) == 3
    assert len(first.published_safety_statuses) == 1
    assert suppressed.published_safety_statuses == ()
    assert len(heartbeat.published_safety_statuses) == 1
    assert len(changed.published_safety_statuses) == 1
    assert len(transport.safety_published) == 3


def test_bridge_range_status_has_fifteen_hz_heartbeat_without_control() -> None:
    transport = _Transport()
    transport.commands.append((_command(), PEER))
    bridge = _bridge(transport)
    bridge.start()
    tracks = (_track(BoundingBox(0.4, 0.3, 0.55, 0.6), last_seen_at_s=100.0),)
    base = _range_status()

    first = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-100",
        captured_at_s=100.0,
        produced_at_s=100.01,
        range_status=base,
    )
    suppressed = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-101",
        captured_at_s=100.04,
        produced_at_s=100.05,
        range_status=replace(
            base,
            sequence=2,
            source_frame_id="frame-101",
            source_captured_at_s=100.04,
            produced_at_s=100.05,
        ),
    )
    heartbeat = bridge.process_frame(
        tracks=tracks,
        frame_id="frame-102",
        captured_at_s=100.07,
        produced_at_s=100.08,
        range_status=replace(
            base,
            sequence=3,
            source_frame_id="frame-102",
            source_captured_at_s=100.07,
            produced_at_s=100.08,
        ),
    )
    bridge.close()

    assert len(first.published_range_statuses) == 1
    assert suppressed.published_range_statuses == ()
    assert len(heartbeat.published_range_statuses) == 1
    assert len(transport.range_published) == 2
    assert all(
        status.flight_control_enabled is False for status, _peer in transport.range_published
    )
