from __future__ import annotations

import socket
import threading
import time

from pymavlink.dialects.v20 import common as mavlink2

from multidetect.approach_hil import ApproachHilPhase
from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    RuleCheck,
    Verdict,
)
from multidetect.multimodal_ranging import RangeValidity
from multidetect.operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachStatusMessage,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    AuthorizationDisplayState,
    MissionStatusMessage,
    PatrolStatusMessage,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetStatusMessage,
    RangeStatusMessage,
    SafetyStatusMessage,
    SelectionAction,
    SelectionCommandGuard,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_mavlink import (
    OperatorMavlinkEndpoint,
    OperatorMavlinkTunnelAdapter,
)
from multidetect.operator_protocol import (
    ApproachConfirmationAck,
    AuthorizationDecisionAck,
    OperatorTunnelCodec,
    PayloadTargetConfirmationAck,
)
from multidetect.operator_udp import (
    UdpOperatorSelectionClient,
    UdpOperatorSelectionServer,
    UdpOperatorSessionClient,
)
from multidetect.patrol_advisory import AdvisoryValidity, PatrolPhase, ReturnObserveDirection
from multidetect.payload_target_gate import PayloadTargetEligibility
from multidetect.unified_tracking import UnifiedTrackState

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


def _signed_qgc_heartbeat() -> bytes:
    serializer = mavlink2.MAVLink(None, srcSystem=255, srcComponent=190)
    serializer.signing.secret_key = MAVLINK_KEY
    serializer.signing.link_id = 190
    # Keep this earlier than the operator adapter's first packet, as one real
    # QGC signer monotonically advances the timestamp across all messages.
    serializer.signing.timestamp = 2_500_000
    serializer.signing.sign_outgoing = True
    message = mavlink2.MAVLink_heartbeat_message(6, 8, 0, 0, 3, 3)
    return bytes(message.pack(serializer, force_mavlink1=False))


def test_background_server_registers_qgc_heartbeat_before_selection() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))
    g20 = _adapter(OperatorMavlinkEndpoint(255, 190, 1, 191))

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
    ) as server:
        server.start_background()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as heartbeat_sender:
            heartbeat_sender.sendto(_signed_qgc_heartbeat(), server.bound_address)
            heartbeat_peer = heartbeat_sender.getsockname()
            deadline = time.monotonic() + 1.0
            discovered_peer = server.active_metadata_peer()
            while discovered_peer is None and time.monotonic() < deadline:
                time.sleep(0.005)
                discovered_peer = server.active_metadata_peer()
            assert discovered_peer == ("127.0.0.1", heartbeat_peer[1])
        assert server.poll_error() is None

        with UdpOperatorSessionClient(
            host="127.0.0.1",
            port=server.bound_address[1],
            mavlink=g20,
        ) as client:
            now_s = time.time()
            receipt = client.deliver(
                TargetSelectionCommand(
                    command_id="99999999-9999-4999-8999-999999999999",
                    session_id="88888888-8888-4888-8888-888888888888",
                    sequence=1,
                    action=SelectionAction.SELECT,
                    geometry=GEOMETRY,
                    issued_at_s=now_s,
                    expires_at_s=now_s + 3.0,
                    bbox=BoundingBox(0.30, 0.25, 0.65, 0.75),
                )
            )

        assert receipt.acknowledgement.accepted is True
        assert server.poll_error() is None


def test_authenticated_metadata_peer_lease_expires_without_heartbeat() -> None:
    jetson = _adapter(OperatorMavlinkEndpoint(1, 191, 255, 190))

    with UdpOperatorSelectionServer(
        bind_host="127.0.0.1",
        port=0,
        mavlink=jetson,
        guard=SelectionCommandGuard(GEOMETRY),
        metadata_peer_timeout_s=0.03,
    ) as server:
        server.start_background()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as heartbeat_sender:
            heartbeat_sender.sendto(_signed_qgc_heartbeat(), server.bound_address)
            deadline = time.monotonic() + 1.0
            while server.active_metadata_peer() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            assert server.active_metadata_peer() is not None
            time.sleep(0.05)
            assert server.active_metadata_peer() is None


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
            server.publish_patrol_status(
                PatrolStatusMessage(
                    status_id="66666666-6666-4666-8666-666666666666",
                    sequence=4,
                    mission_id="fire-patrol-demo",
                    phase=PatrolPhase.LOST,
                    primary_target_id="track-42",
                    target_state=UnifiedTrackState.LOST,
                    bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
                    label="flame",
                    confidence=0.91,
                    tracking_quality=0.2,
                    total_track_count=10,
                    locked_track_count=2,
                    source_frame_id="jetson-frame-700",
                    source_captured_at_s=issued_at_s + 0.01,
                    produced_at_s=issued_at_s + 0.05,
                    return_direction=ReturnObserveDirection.RIGHT,
                    return_validity=AdvisoryValidity.DEGRADED,
                    return_evidence_age_s=0.5,
                    estimated_minimum_turn_radius_m=75.0,
                ),
                peer=peer,
            )
            patrol_status = client.receive_patrol_status(timeout_s=1.0)
            pool_entries = (
                TargetPoolEntry(
                    target_id="track-42",
                    state=UnifiedTrackState.TRACKING,
                    label="flame",
                    confidence=0.91,
                    tracking_quality=0.87,
                    locked=True,
                    primary=True,
                    actionable=True,
                    reid_confirmed=True,
                ),
                TargetPoolEntry(
                    target_id="track-43",
                    state=UnifiedTrackState.OCCLUDED,
                    label="vehicle",
                    confidence=0.80,
                    tracking_quality=0.55,
                    locked=True,
                    primary=False,
                    actionable=False,
                    reid_confirmed=True,
                ),
            )
            server.publish_target_pool_status(
                TargetPoolStatusMessage(
                    sequence=5,
                    pool_revision=3,
                    page_index=0,
                    page_count=1,
                    total_track_count=2,
                    entries=pool_entries,
                    produced_at_s=issued_at_s + 0.055,
                ),
                peer=peer,
            )
            target_pool_status = client.receive_target_pool_status(timeout_s=1.0)
            server.publish_range_status(
                RangeStatusMessage(
                    status_id="77777777-7777-4777-8777-777777777777",
                    sequence=5,
                    target_id="track-42",
                    calibration_id="camera-main-v2",
                    source_frame_id="jetson-frame-700",
                    source_captured_at_s=issued_at_s + 0.01,
                    produced_at_s=issued_at_s + 0.06,
                    validity=RangeValidity.DEGRADED,
                    reasons=("single_absolute_range_method",),
                    sources=("pixhawk_agl", "camera_ground"),
                    rejected_sources=(),
                    slant_range_m=82.0,
                    ground_range_m=75.0,
                    slant_range_ci95_m=(78.0, 86.0),
                    ground_range_ci95_m=(71.0, 79.0),
                    relative_bearing_deg=4.2,
                    absolute_bearing_deg=94.2,
                    bearing_sigma_deg=1.0,
                    north_offset_m=-5.5,
                    east_offset_m=74.8,
                    data_freshness_s=0.05,
                    sensor_consistency=0.5,
                ),
                peer=peer,
            )
            range_status = client.receive_range_status(timeout_s=1.0)

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
    assert patrol_status.phase is PatrolPhase.LOST
    assert patrol_status.return_direction is ReturnObserveDirection.RIGHT
    assert patrol_status.total_track_count == 10
    assert patrol_status.advisory_only is True
    assert patrol_status.flight_control_enabled is False
    assert target_pool_status.pool_revision == 3
    assert len(target_pool_status.entries) == 2
    assert target_pool_status.entries[0].primary is True
    assert target_pool_status.entries[1].state is UnifiedTrackState.OCCLUDED
    assert target_pool_status.advisory_only is True
    assert target_pool_status.flight_control_enabled is False
    assert target_pool_status.physical_release_enabled is False
    assert range_status.validity is RangeValidity.DEGRADED
    assert range_status.slant_range_m == 82.0
    assert range_status.slant_range_ci95_m == (78.0, 86.0)
    assert range_status.advisory_only is True
    assert range_status.flight_control_enabled is False
    assert range_status.physical_release_enabled is False


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


def test_background_server_round_trips_bound_continuous_approach_slide() -> None:
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
            now_s = time.time()
            selection = TargetSelectionCommand(
                command_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=now_s,
                expires_at_s=now_s + 3.0,
                bbox=BoundingBox(0.4, 0.4, 0.6, 0.6),
            )
            assert client.deliver(selection).acknowledgement.accepted is True
            queued = None
            deadline = time.monotonic() + 1.0
            while queued is None and time.monotonic() < deadline:
                queued = server.poll_selection()
                time.sleep(0.005)
            assert queued is not None
            peer = queued[1]
            challenge = ApproachChallengeStatusMessage(
                challenge_token=501,
                target_token=502,
                target_revision=7,
                selection_command_id=selection.command_id,
                issued_at_s=now_s,
                expires_at_s=now_s + 4.0,
                sequence=2,
                produced_at_s=now_s + 0.01,
            )
            server.publish_approach_challenge(challenge, peer=peer)
            received = client.receive_approach_challenge(timeout_s=1.0)
            issued_at_s = time.time()
            command = ApproachConfirmationCommand(
                command_token=601,
                session_token=602,
                challenge_token=received.challenge_token,
                target_token=received.target_token,
                target_revision=received.target_revision,
                selection_command_id=received.selection_command_id,
                sequence=3,
                issued_at_s=issued_at_s,
                expires_at_s=min(issued_at_s + 2.0, received.expires_at_s),
                slide_duration_s=0.8,
                completion_fraction=1.0,
                continuous=True,
            )
            receipt = client.deliver_approach_confirmation(command)
            assert isinstance(receipt.acknowledgement, ApproachConfirmationAck)
            assert receipt.acknowledgement.accepted is True
            queued_confirmation = None
            deadline = time.monotonic() + 1.0
            while queued_confirmation is None and time.monotonic() < deadline:
                queued_confirmation = server.poll_approach_confirmation()
                time.sleep(0.005)
            assert queued_confirmation is not None
            assert queued_confirmation[0].command_token == command.command_token
            assert queued_confirmation[0].challenge_token == command.challenge_token
            assert queued_confirmation[0].selection_command_id == command.selection_command_id
            status = ApproachStatusMessage(
                sequence=4,
                target_id="manual-car-1",
                target_revision=7,
                phase=ApproachHilPhase.CENTERING_SIM,
                reasons=("centering_advice_only",),
                produced_at_s=time.time(),
                yaw_error_deg=2.0,
                yaw_advice_deg=2.0,
                ground_range_m=75.0,
            )
            server.publish_approach_status(status, peer=peer)
            received_status = client.receive_approach_status(timeout_s=1.0)
            assert received_status.phase is ApproachHilPhase.CENTERING_SIM
            assert received_status.flight_control_enabled is False


def test_background_server_round_trips_bound_payload_target_slide_and_status() -> None:
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
            now_s = time.time()
            selection = TargetSelectionCommand(
                command_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                session_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                sequence=1,
                action=SelectionAction.SELECT,
                geometry=GEOMETRY,
                issued_at_s=now_s,
                expires_at_s=now_s + 3.0,
                bbox=BoundingBox(0.4, 0.4, 0.6, 0.6),
            )
            assert client.deliver(selection).acknowledgement.accepted is True
            queued = None
            deadline = time.monotonic() + 1.0
            while queued is None and time.monotonic() < deadline:
                queued = server.poll_selection()
                time.sleep(0.005)
            assert queued is not None
            peer = queued[1]
            challenge = PayloadTargetChallengeStatusMessage(
                challenge_token=701,
                selected_target_token=702,
                selected_target_revision=7,
                aimpoint_target_token=703,
                aimpoint_target_revision=8,
                selection_command_id=selection.command_id,
                issued_at_s=now_s,
                expires_at_s=now_s + 4.0,
                sequence=2,
                produced_at_s=now_s + 0.01,
            )
            server.publish_payload_target_challenge(challenge, peer=peer)
            received = client.receive_payload_target_challenge(timeout_s=1.0)
            issued_at_s = time.time()
            command = PayloadTargetConfirmationCommand(
                command_token=801,
                session_token=802,
                challenge_token=received.challenge_token,
                selected_target_token=received.selected_target_token,
                selected_target_revision=received.selected_target_revision,
                aimpoint_target_token=received.aimpoint_target_token,
                aimpoint_target_revision=received.aimpoint_target_revision,
                selection_command_id=received.selection_command_id,
                sequence=3,
                issued_at_s=issued_at_s,
                expires_at_s=min(issued_at_s + 2.0, received.expires_at_s),
                slide_duration_s=0.8,
                completion_fraction=1.0,
                continuous=True,
            )
            receipt = client.deliver_payload_target_confirmation(command)
            assert isinstance(receipt.acknowledgement, PayloadTargetConfirmationAck)
            assert receipt.acknowledgement.accepted is True
            queued_confirmation = None
            deadline = time.monotonic() + 1.0
            while queued_confirmation is None and time.monotonic() < deadline:
                queued_confirmation = server.poll_payload_target_confirmation()
                time.sleep(0.005)
            assert queued_confirmation is not None
            assert queued_confirmation[0].command_token == command.command_token
            assert queued_confirmation[0].challenge_token == command.challenge_token
            assert queued_confirmation[0].selected_target_token == command.selected_target_token
            assert queued_confirmation[0].aimpoint_target_token == command.aimpoint_target_token
            status = PayloadTargetStatusMessage(
                sequence=4,
                selection_command_id=selection.command_id,
                selected_target_token=702,
                selected_target_revision=7,
                eligibility=PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT,
                produced_at_s=time.time(),
                aimpoint_target_token=703,
                aimpoint_target_revision=8,
                confirmation_accepted=True,
                confirmation_expires_at_s=time.time() + 1.0,
            )
            server.publish_payload_target_status(status, peer=peer)
            received_status = client.receive_payload_target_status(timeout_s=1.0)
            assert received_status.eligibility is PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT
            assert received_status.confirmation_accepted is True
            assert received_status.physical_release_enabled is False
