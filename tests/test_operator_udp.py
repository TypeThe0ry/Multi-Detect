from __future__ import annotations

import socket
import threading
import time

from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    RuleCheck,
    Verdict,
)
from multidetect.operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    AuthorizationDisplayState,
    MissionStatusMessage,
    SafetyStatusMessage,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_mavlink import (
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import AuthorizationDecisionAck, OperatorTunnelCodec
from multidetect.operator_udp import (
    UdpOperatorSelectionClient,
    UdpOperatorSelectionServer,
    UdpOperatorSessionClient,
)

APP_KEY = b"operator-udp-application-key-at-least-32-bytes"
MAVLINK_KEY = b"U" * 32
GEOMETRY = VideoGeometry("camera-main", 1280, 720)


def _adapter(endpoint: OperatorMavlinkEndpoint) -> OperatorMavlinkTunnelAdapter:
    return OperatorMavlinkTunnelAdapter(
        OperatorTunnelCodec(hmac_key=APP_KEY, geometries=(GEOMETRY,)),
        endpoint,
        signing_key=MAVLINK_KEY,
        signing_link_id=endpoint.local_component_id,
        initial_signing_timestamp=3_000_000 + endpoint.local_system_id,
    )


def test_real_localhost_udp_selection_and_ack_round_trip() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    g20 = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))
    received = []

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
        receive_timeout_s=2.0,
    ) as server:
        worker = threading.Thread(target=lambda: received.append(server.serve_once()))
        worker.start()
        issued_at_s = time.time()
        receipt = UdpOperatorSelectionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=g20,
        ).deliver(
            TargetSelectionCommand(
                command_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=issued_at_s,
                expires_at_s=issued_at_s + 3.0,
                bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
                displayed_frame_id="g20-frame-500",
            )
        )
        worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert len(received) == 1
    server_result, peer = received[0]
    assert server_result.acceptance.allowed is True
    assert peer[0] == "127.0.0.1"
    assert receipt.acknowledgement.accepted is True
    assert receipt.attempts == 1
    assert receipt.elapsed_s < 2.0


def test_background_server_delivers_selection_and_returns_tracking_status() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    g20 = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
    ) as server:
        server.start_background()
        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=g20,
        ) as client:
            issued_at_s = time.time()
            command = TargetSelectionCommand(
                command_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=issued_at_s,
                expires_at_s=issued_at_s + 3.0,
                bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
            )
            assert client.deliver(command).acknowledgement.accepted is True

            deadline = time.monotonic() + 1.0
            queued = server.poll_selection()
            while queued is None and time.monotonic() < deadline:
                time.sleep(0.005)
                queued = server.poll_selection()
            assert queued is not None
            received_command, peer = queued
            assert received_command.command_id == command.command_id
            server.publish_track_status(
                TrackStatusMessage(
                    status_id="33333333-3333-4333-8333-333333333333",
                    selection_command_id=command.command_id,
                    sequence=1,
                    geometry=GEOMETRY,
                    state=TrackingState.TRACKING,
                    target_id="track-42",
                    bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
                    label="flame",
                    confidence=0.91,
                    tracking_quality=0.87,
                    source_frame_id="jetson-frame-700",
                    source_captured_at_s=issued_at_s + 0.01,
                    produced_at_s=issued_at_s + 0.02,
                ),
                peer=peer,
            )
            status = client.receive_track_status(timeout_s=1.0)
            server.publish_mission_status(
                MissionStatusMessage(
                    status_id="44444444-4444-4444-8444-444444444444",
                    sequence=2,
                    mission_id="fire-fixed-wing-demo",
                    phase=MissionPhase.AWAITING_AUTHORIZATION,
                    authorization_state=AuthorizationDisplayState.PENDING,
                    release_window=DeploymentWindowStatus.WAIT,
                    safety_allowed=False,
                    remaining_payload_count=4,
                    total_payload_count=4,
                    target_id="track-42",
                    active_payload_slot_id="payload-1",
                    target_confidence=0.91,
                    relative_bearing_deg=4.2,
                    estimated_range_m=82.0,
                    cross_track_error_m=2.5,
                    along_track_error_m=19.0,
                    release_lead_distance_m=62.0,
                    produced_at_s=issued_at_s + 0.03,
                ),
                peer=peer,
            )
            mission_status = client.receive_mission_status(timeout_s=1.0)
            server.publish_safety_status(
                SafetyStatusMessage(
                    status_id="55555555-5555-4555-8555-555555555555",
                    sequence=3,
                    mission_id="fire-fixed-wing-demo",
                    target_id="track-42",
                    ruleset_version="rules-v1",
                    checks=(
                        RuleCheck("target.confirmed_track", Verdict.PASS, "confirmed"),
                        RuleCheck("navigation.allowed_zone", Verdict.UNKNOWN, "unknown"),
                    ),
                    produced_at_s=issued_at_s + 0.04,
                ),
                peer=peer,
            )
            safety_status = client.receive_safety_status(timeout_s=1.0)

    assert status.state is TrackingState.TRACKING
    assert status.selection_command_id == command.command_id
    assert status.label == "flame"
    assert mission_status.phase is MissionPhase.AWAITING_AUTHORIZATION
    assert mission_status.release_window is DeploymentWindowStatus.WAIT
    assert mission_status.advisory_only is True
    assert safety_status.pass_count == 1
    assert safety_status.unknown_count == 1
    assert safety_status.allowed is False
    assert safety_status.advisory_only is True


def test_background_server_round_trips_bound_authorization_decision() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    g20 = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))
    authorization_guard = AuthorizationDecisionCommandGuard(clock_tolerance_s=0.5)

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
        authorization_guard=authorization_guard,
    ) as server:
        server.start_background()
        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=g20,
        ) as client:
            now_s = time.time()
            selection = TargetSelectionCommand(
                command_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=now_s,
                expires_at_s=now_s + 3.0,
                bbox=BoundingBox(0.32, 0.21, 0.61, 0.72),
            )
            assert client.deliver(selection).acknowledgement.accepted is True
            queued = None
            deadline = time.monotonic() + 1.0
            while queued is None and time.monotonic() < deadline:
                queued = server.poll_selection()
                time.sleep(0.005)
            assert queued is not None
            peer = queued[1]
            challenge = AuthorizationChallengeStatusMessage(
                challenge_token=11,
                mission_token=12,
                target_token=13,
                scene_token=14,
                ruleset_token=15,
                payload_slot_token=16,
                target_revision=7,
                created_at_s=now_s,
                expires_at_s=now_s + 4.0,
                sequence=2,
                produced_at_s=now_s + 0.01,
            )
            server.set_authorization_challenge(challenge)
            interleaved_mission = MissionStatusMessage(
                status_id="66666666-6666-4666-8666-666666666666",
                sequence=4,
                mission_id="fire-fixed-wing-demo",
                phase=MissionPhase.AWAITING_AUTHORIZATION,
                authorization_state=AuthorizationDisplayState.PENDING,
                release_window=DeploymentWindowStatus.WAIT,
                safety_allowed=True,
                remaining_payload_count=1,
                total_payload_count=1,
                target_id="track-42",
                active_payload_slot_id="payload-1",
                target_confidence=0.91,
                relative_bearing_deg=None,
                estimated_range_m=None,
                cross_track_error_m=None,
                along_track_error_m=None,
                release_lead_distance_m=None,
                produced_at_s=now_s + 0.01,
            )
            server.publish_mission_status(interleaved_mission, peer=peer)
            server.publish_authorization_challenge(challenge, peer=peer)
            received_challenge = client.receive_authorization_challenge(timeout_s=1.0)
            cached_mission = client.receive_mission_status(timeout_s=0.1)
            assert cached_mission.sequence == interleaved_mission.sequence
            assert cached_mission.phase is MissionPhase.AWAITING_AUTHORIZATION
            command = AuthorizationDecisionCommand(
                command_token=101,
                session_token=102,
                challenge_token=received_challenge.challenge_token,
                mission_token=received_challenge.mission_token,
                target_token=received_challenge.target_token,
                scene_token=received_challenge.scene_token,
                ruleset_token=received_challenge.ruleset_token,
                payload_slot_token=received_challenge.payload_slot_token,
                target_revision=received_challenge.target_revision,
                decision=AuthorizationDecision.APPROVE,
                operator_token=103,
                sequence=3,
                issued_at_s=time.time(),
                expires_at_s=min(time.time() + 2.0, received_challenge.expires_at_s),
            )
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rogue:
                rogue.settimeout(1.0)
                rogue.sendto(
                    g20.encode_authorization_decision(command),
                    server.bound_address,
                )
                rejected = g20.decode_frame(rogue.recv(512)).message
            assert isinstance(rejected, AuthorizationDecisionAck)
            assert rejected.accepted is False
            interleaved_safety = SafetyStatusMessage(
                status_id="77777777-7777-4777-8777-777777777777",
                sequence=5,
                mission_id="fire-fixed-wing-demo",
                target_id="track-42",
                ruleset_version="rules-v1",
                checks=(RuleCheck("target.confirmed_track", Verdict.PASS, "confirmed"),),
                produced_at_s=now_s + 0.02,
            )
            server.publish_safety_status(interleaved_safety, peer=peer)
            receipt = client.deliver_authorization_decision(command)
            cached_safety = client.receive_safety_status(timeout_s=0.1)
            assert cached_safety.sequence == interleaved_safety.sequence
            assert cached_safety.pass_count == 1
            queued_decision = None
            deadline = time.monotonic() + 1.0
            while queued_decision is None and time.monotonic() < deadline:
                queued_decision = server.poll_authorization_decision()
                time.sleep(0.005)

    assert receipt.acknowledgement.accepted is True
    assert queued_decision is not None
    assert queued_decision[0].command_token == command.command_token
    assert queued_decision[0].challenge_token == command.challenge_token
    assert queued_decision[0].decision is AuthorizationDecision.APPROVE
