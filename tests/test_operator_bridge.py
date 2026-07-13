from __future__ import annotations

from collections import deque
from dataclasses import replace

from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    RuleCheck,
    TrackSnapshot,
    Verdict,
)
from multidetect.operator_bridge import LiveOperatorBridge
from multidetect.operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDisplayState,
    MissionStatusMessage,
    SafetyStatusMessage,
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
        self.mission_published = []
        self.safety_published = []
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

    def set_authorization_challenge(self, status) -> None:
        self.active_authorization_challenge = status

    def publish_track_status(self, status, *, peer) -> None:
        self.published.append((status, peer))

    def publish_mission_status(self, status, *, peer) -> None:
        self.mission_published.append((status, peer))

    def publish_safety_status(self, status, *, peer) -> None:
        self.safety_published.append((status, peer))

    def publish_authorization_challenge(self, status, *, peer) -> None:
        self.authorization_challenges.append((status, peer))

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
    assert selected.published_mission_statuses == (_mission_status(),)
    assert transport.mission_published == [(_mission_status(), PEER)]
    assert selected.published_safety_statuses == (_safety_status(),)
    assert transport.safety_published == [(_safety_status(), PEER)]


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
