from __future__ import annotations

import random
from dataclasses import replace

import pytest

from multidetect.approach_hil import ApproachHilPhase
from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    MissionPhase,
    ReleaseTimingStatus,
    RuleCheck,
    Verdict,
)
from multidetect.multimodal_ranging import RangeSourceContribution, RangeValidity
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
    SceneContextRegionEntry,
    SceneContextState,
    SceneContextStatusMessage,
    SelectionAction,
    TargetGeolocationStatusMessage,
    TargetPoolEntry,
    TargetPoolStatusMessage,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from multidetect.operator_protocol import (
    MAX_TUNNEL_PAYLOAD_BYTES,
    ApproachConfirmationAck,
    ApproachConfirmationAckReason,
    AuthorizationDecisionAck,
    AuthorizationDecisionAckReason,
    OperatorProtocolError,
    OperatorTunnelCodec,
    PayloadTargetConfirmationAck,
    PayloadTargetConfirmationAckReason,
    SelectionAck,
    SelectionAckReason,
    WireMessageType,
)
from multidetect.patrol_advisory import (
    AdvisoryValidity,
    PatrolPhase,
    ReturnObserveDirection,
)
from multidetect.payload_target_gate import PayloadTargetEligibility
from multidetect.unified_tracking import UnifiedTrackState

KEY = b"operator-link-unit-test-key-32-bytes-minimum"
GEOMETRY = VideoGeometry("camera-main", 1280, 720)
COMMAND_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"
STATUS_ID = "33333333-3333-4333-8333-333333333333"


def _codec() -> OperatorTunnelCodec:
    return OperatorTunnelCodec(hmac_key=KEY, geometries=(GEOMETRY,))


def _selection(**overrides: object) -> TargetSelectionCommand:
    values: dict[str, object] = {
        "command_id": COMMAND_ID,
        "session_id": SESSION_ID,
        "sequence": 7,
        "action": SelectionAction.SELECT,
        "geometry": GEOMETRY,
        "issued_at_s": 1_000.125,
        "expires_at_s": 1_003.125,
        "bbox": BoundingBox(0.32, 0.21, 0.61, 0.72),
        "displayed_frame_id": "g20-frame-500",
    }
    values.update(overrides)
    return TargetSelectionCommand(**values)


def _authorization_challenge() -> AuthorizationChallengeStatusMessage:
    return AuthorizationChallengeStatusMessage(
        challenge_token=11,
        mission_token=12,
        target_token=13,
        scene_token=14,
        ruleset_token=15,
        payload_slot_token=16,
        target_revision=7,
        created_at_s=1_000.0,
        expires_at_s=1_010.0,
        sequence=21,
        produced_at_s=1_003.0,
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
        target_revision=7,
        decision=AuthorizationDecision.APPROVE,
        operator_token=103,
        sequence=22,
        issued_at_s=1_003.1,
        expires_at_s=1_005.1,
    )


def test_selection_round_trip_fits_tunnel_payload() -> None:
    codec = _codec()

    encoded = codec.encode_selection(_selection())
    decoded = codec.decode(encoded)

    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.TARGET_SELECTION
    assert decoded.sequence == 7
    command = decoded.message
    assert isinstance(command, TargetSelectionCommand)
    assert command.command_id == COMMAND_ID
    assert command.session_id == SESSION_ID
    assert command.geometry == GEOMETRY
    assert command.bbox is not None
    assert command.bbox.x1 == pytest.approx(0.32, abs=1 / 65535)
    assert command.bbox.y2 == pytest.approx(0.72, abs=1 / 65535)
    assert command.displayed_frame_id is not None
    assert command.displayed_frame_id.startswith("hash64:")
    assert command.issued_at_s == pytest.approx(1_000.125)
    assert command.expires_at_s == pytest.approx(1_003.125)


def test_cancel_round_trip_has_no_bbox() -> None:
    decoded = _codec().decode(
        _codec().encode_selection(
            _selection(action=SelectionAction.CANCEL, bbox=None, displayed_frame_id=None)
        )
    )

    command = decoded.message
    assert isinstance(command, TargetSelectionCommand)
    assert command.action is SelectionAction.CANCEL
    assert command.bbox is None
    assert command.displayed_frame_id is None


def test_single_track_cancel_round_trip_keeps_target_bbox() -> None:
    decoded = _codec().decode(
        _codec().encode_selection(_selection(action=SelectionAction.CANCEL_TRK))
    )

    command = decoded.message
    assert isinstance(command, TargetSelectionCommand)
    assert command.action is SelectionAction.CANCEL_TRK
    assert command.bbox is not None
    assert command.bbox.x1 == pytest.approx(0.32, abs=1 / 65535)


def test_acknowledgement_round_trip_is_correlated() -> None:
    codec = _codec()
    ack = SelectionAck(COMMAND_ID, True, SelectionAckReason.ACCEPTED, 7)

    decoded = codec.decode(codec.encode_ack(ack, sequence=12, sent_at_s=1_000.25))

    assert decoded.message_type is WireMessageType.SELECTION_ACK
    assert decoded.sequence == 12
    assert decoded.message == ack


def test_track_status_round_trip_fits_worst_case_tunnel_frame() -> None:
    codec = _codec()
    status = TrackStatusMessage(
        status_id=STATUS_ID,
        selection_command_id=COMMAND_ID,
        sequence=99,
        geometry=GEOMETRY,
        state=TrackingState.TRACKING,
        target_id="tracker-target-123",
        bbox=BoundingBox(0.33, 0.22, 0.62, 0.73),
        label="smolder_area",
        confidence=0.91,
        tracking_quality=0.87,
        source_frame_id="jetson-frame-700",
        source_captured_at_s=1_000.2,
        produced_at_s=1_000.25,
        relative_bearing_deg=-4.2,
        estimated_range_m=82.0,
    )

    encoded = codec.encode_track_status(status)
    decoded = codec.decode(encoded)

    assert len(encoded) == 121
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.TRACK_STATUS
    message = decoded.message
    assert isinstance(message, TrackStatusMessage)
    assert message.status_id.startswith("hash64:")
    assert message.selection_command_id == COMMAND_ID
    assert message.geometry == GEOMETRY
    assert message.target_id is not None and message.target_id.startswith("hash64:")
    assert message.source_frame_id.startswith("hash64:")
    assert message.confidence == pytest.approx(0.91, abs=1 / 254)
    assert message.tracking_quality == pytest.approx(0.87, abs=1 / 254)
    assert message.relative_bearing_deg == pytest.approx(-4.2)
    assert message.estimated_range_m == pytest.approx(82.0)


def test_mission_status_round_trip_is_compact_and_display_only() -> None:
    codec = _codec()
    status = MissionStatusMessage(
        status_id=STATUS_ID,
        sequence=100,
        mission_id="fire-fixed-wing-demo",
        phase=MissionPhase.AWAITING_AUTHORIZATION,
        authorization_state=AuthorizationDisplayState.PENDING,
        release_window=DeploymentWindowStatus.READY,
        safety_allowed=True,
        remaining_payload_count=3,
        total_payload_count=4,
        target_id="track-fire-7",
        active_payload_slot_id="payload-2",
        target_confidence=0.92,
        relative_bearing_deg=-3.25,
        estimated_range_m=62.8,
        cross_track_error_m=-1.4,
        along_track_error_m=0.1,
        release_lead_distance_m=62.7,
        produced_at_s=1_000.25,
    )

    encoded = codec.encode_mission_status(status)
    decoded = codec.decode(encoded)

    assert len(encoded) == 89
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.MISSION_STATUS
    message = decoded.message
    assert isinstance(message, MissionStatusMessage)
    assert message.mission_id.startswith("hash64:")
    assert message.phase is MissionPhase.AWAITING_AUTHORIZATION
    assert message.authorization_state is AuthorizationDisplayState.PENDING
    assert message.release_window is DeploymentWindowStatus.READY
    assert message.safety_allowed is True
    assert message.target_id is not None and message.target_id.startswith("hash64:")
    assert message.target_confidence == pytest.approx(0.92, abs=1 / 254)
    assert message.cross_track_error_m == pytest.approx(-1.4)
    assert message.release_lead_distance_m == pytest.approx(62.7)
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False


def test_range_status_round_trip_carries_uncertainty_and_consistency() -> None:
    status = RangeStatusMessage(
        status_id=STATUS_ID,
        sequence=103,
        target_id="target-fire-7",
        calibration_id="camera-main-v2",
        source_frame_id="jetson-frame-702",
        source_captured_at_s=1_000.20,
        produced_at_s=1_000.25,
        validity=RangeValidity.DEGRADED,
        reasons=("single_absolute_range_method",),
        sources=("pixhawk_agl", "camera_ground"),
        rejected_sources=(),
        slant_range_m=123.4,
        ground_range_m=105.2,
        slant_range_ci95_m=(119.8, 127.1),
        ground_range_ci95_m=(102.1, 108.3),
        relative_bearing_deg=-12.34,
        absolute_bearing_deg=347.66,
        bearing_sigma_deg=1.25,
        north_offset_m=102.4,
        east_offset_m=-22.7,
        data_freshness_s=0.08,
        sensor_consistency=0.5,
        source_contributions=(
            RangeSourceContribution(
                source="camera_ground",
                range_m=122.8,
                sigma_m=4.1,
                weight=0.63,
                freshness_s=0.08,
            ),
            RangeSourceContribution(
                source="pixhawk_agl",
                range_m=124.2,
                sigma_m=7.0,
                weight=0.37,
                freshness_s=0.08,
            ),
        ),
        vehicle_profile="fixed-wing",
        navigation_state="gps-aided",
        motion_regime="cruise",
    )

    encoded = _codec().encode_range_status(status)
    decoded = _codec().decode(encoded)

    assert len(encoded) == 127
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.RANGE_STATUS
    message = decoded.message
    assert isinstance(message, RangeStatusMessage)
    assert message.validity is RangeValidity.DEGRADED
    assert message.reasons == ("single_absolute_range_method",)
    assert message.sources == ("pixhawk_agl", "camera_ground")
    assert message.slant_range_m == pytest.approx(123.4)
    assert message.slant_range_ci95_m == pytest.approx((119.8, 127.1))
    assert message.relative_bearing_deg == pytest.approx(-12.34)
    assert message.absolute_bearing_deg == pytest.approx(347.66)
    assert message.north_offset_m == pytest.approx(102.4)
    assert message.east_offset_m == pytest.approx(-22.7)
    assert message.data_freshness_s == pytest.approx(0.1)
    assert message.sensor_consistency == pytest.approx(0.5, abs=1 / 254)
    assert message.vehicle_profile == "fixed-wing"
    assert message.navigation_state == "gps-aided"
    assert message.motion_regime == "cruise"
    assert message.source_contributions[0].source == "camera_ground"
    assert message.source_contributions[0].range_m == pytest.approx(122.8)
    assert message.source_contributions[0].weight == pytest.approx(0.63, abs=1 / 254)
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False


def test_range_status_rejects_unregistered_reasons_and_control_flags() -> None:
    values = dict(
        status_id=STATUS_ID,
        sequence=1,
        target_id="target",
        calibration_id="calibration",
        source_frame_id="frame",
        source_captured_at_s=100.0,
        produced_at_s=100.1,
        validity=RangeValidity.INVALID,
        reasons=("pixhawk_agl_unavailable",),
        sources=(),
        rejected_sources=(),
    )
    with pytest.raises(ValueError, match="display-only"):
        RangeStatusMessage(**values, flight_control_enabled=True)
    with pytest.raises(ValueError, match="registered wire vocabulary"):
        RangeStatusMessage(**{**values, "reasons": ("unknown_reason",)})


def test_target_geolocation_status_round_trip_is_explicitly_gps_qualified() -> None:
    status = TargetGeolocationStatusMessage(
        sequence=108,
        target_id="target-fire-7",
        source_frame_id="jetson-frame-702",
        source_captured_at_s=1_000.20,
        produced_at_s=1_000.25,
        available=True,
        reason="gps_qualified",
        latitude_deg=1.3008983,
        longitude_deg=103.8004493,
        horizontal_sigma_m=6.71,
    )

    encoded = _codec().encode_target_geolocation_status(status)
    decoded = _codec().decode(encoded)

    assert len(encoded) == 66
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.TARGET_GEOLOCATION_STATUS
    message = decoded.message
    assert isinstance(message, TargetGeolocationStatusMessage)
    assert message.target_id.startswith("hash64:")
    assert message.source_frame_id.startswith("hash64:")
    assert message.available is True
    assert message.reason == "gps_qualified"
    assert message.latitude_deg == pytest.approx(1.3008983)
    assert message.longitude_deg == pytest.approx(103.8004493)
    assert message.horizontal_sigma_m == pytest.approx(6.7)
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False


def test_target_geolocation_status_withholds_coordinates_when_gps_is_not_qualified() -> None:
    status = TargetGeolocationStatusMessage(
        sequence=109,
        target_id="target-fire-7",
        source_frame_id="jetson-frame-702",
        source_captured_at_s=1_000.20,
        produced_at_s=1_000.25,
        available=False,
        reason="gps_navigation_not_qualified",
    )

    decoded = _codec().decode(_codec().encode_target_geolocation_status(status))

    message = decoded.message
    assert isinstance(message, TargetGeolocationStatusMessage)
    assert message.available is False
    assert message.reason == "gps_navigation_not_qualified"
    assert message.latitude_deg is None
    assert message.longitude_deg is None
    assert message.horizontal_sigma_m is None

    with pytest.raises(ValueError, match="cannot publish coordinates"):
        replace(status, latitude_deg=1.3)


def test_release_status_round_trip_carries_bound_impact_uncertainty() -> None:
    status = ReleaseStatusMessage(
        sequence=104,
        target_id="target-fire-7",
        calibration_id="release-cal-v2",
        produced_at_s=1_000.25,
        timing_status=ReleaseTimingStatus.WINDOW,
        reasons=("multimodal_release_window_ready",),
        range_target_id="target-fire-7",
        range_frame_id="jetson-frame-702",
        target_north_offset_m=102.4,
        target_east_offset_m=-22.7,
        impact_north_offset_m=101.1,
        impact_east_offset_m=-21.9,
        along_track_error_m=0.8,
        cross_track_error_m=-0.4,
        error_ellipse_major_m=4.6,
        error_ellipse_minor_m=2.1,
        error_ellipse_orientation_deg=-12.34,
        estimated_ground_range_m=105.2,
        ground_range_ci95_m=(102.1, 108.3),
        payload_descent_time_s=2.7,
        release_lead_distance_m=62.7,
        range_sensor_consistency=0.82,
    )

    encoded = _codec().encode_release_status(status)
    decoded = _codec().decode(encoded)

    assert len(encoded) == 115
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.RELEASE_STATUS
    message = decoded.message
    assert isinstance(message, ReleaseStatusMessage)
    assert message.timing_status is ReleaseTimingStatus.WINDOW
    assert message.reasons == ("multimodal_release_window_ready",)
    assert message.range_target_id is not None
    assert message.range_frame_id is not None
    assert message.target_north_offset_m == pytest.approx(102.4)
    assert message.impact_east_offset_m == pytest.approx(-21.9)
    assert message.error_ellipse_major_m == pytest.approx(4.6)
    assert message.error_ellipse_orientation_deg == pytest.approx(-12.34)
    assert message.ground_range_ci95_m == pytest.approx((102.1, 108.3))
    assert message.range_sensor_consistency == pytest.approx(0.82, abs=1 / 254)
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False


def test_release_status_rejects_unbound_window_and_control_flags() -> None:
    invalid = dict(
        sequence=1,
        target_id="target",
        calibration_id="calibration",
        produced_at_s=100.0,
        timing_status=ReleaseTimingStatus.INVALID,
        reasons=("multimodal_range_evidence_unavailable",),
    )
    with pytest.raises(ValueError, match="display-only"):
        ReleaseStatusMessage(**invalid, physical_release_enabled=True)
    with pytest.raises(ValueError, match="registered wire vocabulary"):
        ReleaseStatusMessage(**{**invalid, "reasons": ("unknown_reason",)})
    with pytest.raises(ValueError, match="complete bound impact geometry"):
        ReleaseStatusMessage(
            **{
                **invalid,
                "timing_status": ReleaseTimingStatus.WINDOW,
                "reasons": ("multimodal_release_window_ready",),
            }
        )


def test_approach_challenge_and_confirmation_are_target_selection_bound() -> None:
    challenge = ApproachChallengeStatusMessage(
        challenge_token=101,
        target_token=202,
        target_revision=7,
        selection_command_id=COMMAND_ID,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=110,
        produced_at_s=100.1,
    )
    command = ApproachConfirmationCommand(
        command_token=303,
        session_token=404,
        challenge_token=101,
        target_token=202,
        target_revision=7,
        selection_command_id=COMMAND_ID,
        sequence=111,
        issued_at_s=101.0,
        expires_at_s=103.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )

    challenge_packet = _codec().decode(_codec().encode_approach_challenge(challenge))
    command_packet = _codec().decode(_codec().encode_approach_confirmation(command))

    assert challenge_packet.message_type is WireMessageType.APPROACH_CHALLENGE
    assert isinstance(challenge_packet.message, ApproachChallengeStatusMessage)
    assert challenge_packet.message.selection_command_id == COMMAND_ID
    assert command_packet.message_type is WireMessageType.APPROACH_CONFIRMATION
    assert isinstance(command_packet.message, ApproachConfirmationCommand)
    assert command_packet.message.target_revision == 7
    assert command_packet.message.slide_duration_s == pytest.approx(0.8)
    assert command_packet.message.completion_fraction == pytest.approx(1.0)
    assert command_packet.message.continuous is True
    assert len(_codec().encode_approach_challenge(challenge)) == 89
    assert len(_codec().encode_approach_confirmation(command)) == 94


def test_approach_ack_and_status_are_advisory_only() -> None:
    acknowledgement = ApproachConfirmationAck(
        command_token=303,
        accepted=True,
        reason=ApproachConfirmationAckReason.ACCEPTED,
        acknowledged_sequence=111,
    )
    status = ApproachStatusMessage(
        sequence=112,
        target_id="manual-car-1",
        target_revision=7,
        phase=ApproachHilPhase.CENTERING_SIM,
        reasons=("centering_advice_only",),
        produced_at_s=101.1,
        yaw_error_deg=2.5,
        pitch_error_deg=-1.2,
        yaw_advice_deg=2.5,
        pitch_advice_deg=-1.2,
        bank_advice_deg=1.8,
        ground_range_m=75.0,
        confirmation_expires_at_s=104.0,
    )

    ack_packet = _codec().decode(
        _codec().encode_approach_ack(acknowledgement, sequence=113, sent_at_s=101.1)
    )
    status_packet = _codec().decode(_codec().encode_approach_status(status))

    assert ack_packet.message == acknowledgement
    assert status_packet.message_type is WireMessageType.APPROACH_STATUS
    message = status_packet.message
    assert isinstance(message, ApproachStatusMessage)
    assert message.phase is ApproachHilPhase.CENTERING_SIM
    assert message.yaw_error_deg == pytest.approx(2.5)
    assert message.ground_range_m == pytest.approx(75.0)
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False
    assert len(_codec().encode_approach_ack(acknowledgement, sequence=113, sent_at_s=101.1)) == 50
    assert len(_codec().encode_approach_status(status)) == 70


def test_approach_status_round_trips_jetson_fixed_wing_control_authority() -> None:
    status = ApproachStatusMessage(
        sequence=114,
        target_id="manual-car-1",
        target_revision=8,
        phase=ApproachHilPhase.CENTERING,
        reasons=("centering_advice_only",),
        produced_at_s=102.0,
        yaw_error_deg=1.0,
        pitch_error_deg=-0.5,
        advisory_only=False,
        sitl_hil_only=False,
        flight_control_enabled=True,
        aim_control_active=True,
    )

    packet = _codec().decode(_codec().encode_approach_status(status))

    assert isinstance(packet.message, ApproachStatusMessage)
    assert packet.message.phase is ApproachHilPhase.CENTERING
    assert packet.message.advisory_only is False
    assert packet.message.sitl_hil_only is False
    assert packet.message.flight_control_enabled is True
    assert packet.message.aim_control_active is True
    assert packet.message.pilot_input_cancelled is False


def test_approach_status_round_trips_pilot_input_cancellation() -> None:
    status = ApproachStatusMessage(
        sequence=115,
        target_id="manual-car-1",
        target_revision=8,
        phase=ApproachHilPhase.ABORT,
        reasons=("abort_latched_until_reselection",),
        produced_at_s=102.1,
        climb_pitch_advice_deg=8.0,
        advisory_only=False,
        sitl_hil_only=False,
        flight_control_enabled=True,
        pilot_input_cancelled=True,
    )

    packet = _codec().decode(_codec().encode_approach_status(status))

    assert isinstance(packet.message, ApproachStatusMessage)
    assert packet.message.aim_control_active is False
    assert packet.message.pilot_input_cancelled is True


def test_payload_target_challenge_and_confirmation_bind_both_targets() -> None:
    challenge = PayloadTargetChallengeStatusMessage(
        challenge_token=501,
        selected_target_token=502,
        selected_target_revision=11,
        aimpoint_target_token=503,
        aimpoint_target_revision=17,
        selection_command_id=COMMAND_ID,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=120,
        produced_at_s=100.1,
    )
    command = PayloadTargetConfirmationCommand(
        command_token=601,
        session_token=602,
        challenge_token=501,
        selected_target_token=502,
        selected_target_revision=11,
        aimpoint_target_token=503,
        aimpoint_target_revision=17,
        selection_command_id=COMMAND_ID,
        sequence=121,
        issued_at_s=101.0,
        expires_at_s=103.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )

    encoded_challenge = _codec().encode_payload_target_challenge(challenge)
    encoded_command = _codec().encode_payload_target_confirmation(command)
    challenge_packet = _codec().decode(encoded_challenge)
    command_packet = _codec().decode(encoded_command)

    assert challenge_packet.message_type is WireMessageType.PAYLOAD_TARGET_CHALLENGE
    assert challenge_packet.message == challenge
    assert command_packet.message_type is WireMessageType.PAYLOAD_TARGET_CONFIRMATION
    assert isinstance(command_packet.message, PayloadTargetConfirmationCommand)
    assert command_packet.message.selected_target_revision == 11
    assert command_packet.message.aimpoint_target_revision == 17
    assert command_packet.message.slide_duration_s == pytest.approx(0.8)
    assert len(encoded_challenge) == 101
    assert len(encoded_command) == 106
    assert max(len(encoded_challenge), len(encoded_command)) <= MAX_TUNNEL_PAYLOAD_BYTES


def test_payload_target_ack_and_status_report_eligibility_without_control() -> None:
    acknowledgement = PayloadTargetConfirmationAck(
        command_token=601,
        accepted=True,
        reason=PayloadTargetConfirmationAckReason.ACCEPTED,
        acknowledged_sequence=121,
    )
    status = PayloadTargetStatusMessage(
        sequence=122,
        selection_command_id=COMMAND_ID,
        selected_target_token=502,
        selected_target_revision=11,
        eligibility=PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT,
        produced_at_s=101.1,
        aimpoint_target_token=503,
        aimpoint_target_revision=17,
        confirmation_pending=True,
        confirmation_expires_at_s=104.0,
    )

    ack_packet = _codec().decode(
        _codec().encode_payload_target_ack(
            acknowledgement,
            sequence=123,
            sent_at_s=101.1,
        )
    )
    encoded_status = _codec().encode_payload_target_status(status)
    status_packet = _codec().decode(encoded_status)

    assert ack_packet.message == acknowledgement
    assert status_packet.message_type is WireMessageType.PAYLOAD_TARGET_STATUS
    message = status_packet.message
    assert isinstance(message, PayloadTargetStatusMessage)
    assert message.eligibility is PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT
    assert message.confirmation_pending is True
    assert message.confirmation_accepted is False
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False
    assert len(encoded_status) == 80


def test_payload_target_frames_match_qgc_golden_vectors_byte_for_byte() -> None:
    codec = _codec()
    challenge = PayloadTargetChallengeStatusMessage(
        challenge_token=701,
        selected_target_token=702,
        selected_target_revision=7,
        aimpoint_target_token=703,
        aimpoint_target_revision=8,
        selection_command_id=COMMAND_ID,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=112,
        produced_at_s=100.01,
    )
    confirmation = PayloadTargetConfirmationCommand(
        command_token=801,
        session_token=802,
        challenge_token=701,
        selected_target_token=702,
        selected_target_revision=7,
        aimpoint_target_token=703,
        aimpoint_target_revision=8,
        selection_command_id=COMMAND_ID,
        sequence=113,
        issued_at_s=101.0,
        expires_at_s=103.0,
        slide_duration_s=0.8,
        completion_fraction=1.0,
        continuous=True,
    )
    acknowledgement = PayloadTargetConfirmationAck(
        command_token=801,
        accepted=True,
        reason=PayloadTargetConfirmationAckReason.ACCEPTED,
        acknowledged_sequence=113,
    )
    status = PayloadTargetStatusMessage(
        sequence=114,
        selection_command_id=COMMAND_ID,
        selected_target_token=702,
        selected_target_revision=7,
        eligibility=PayloadTargetEligibility.ELIGIBLE_BURNING_CONTEXT,
        produced_at_s=101.01,
        aimpoint_target_token=703,
        aimpoint_target_revision=8,
        confirmation_pending=True,
        confirmation_expires_at_s=105.01,
    )

    assert codec.encode_payload_target_challenge(challenge).hex() == (
        "4d44011200000000007000000000000186aa004100000000000002bd00000000000002be"
        "0000000700000000000002bf000000081111111111114111811111111111111100000000"
        "000186a00000000000019a2801e5637e173cd1f02289fe27d0a6be7d61"
    )
    assert codec.encode_payload_target_confirmation(confirmation).hex() == (
        "4d4401130000000000710000000000018a88004600000000000003210000000000000322"
        "00000000000002bd00000000000002be0000000700000000000002bf0000000811111111"
        "11114111811111111111111107d00320fe011f60df56d7979ec17bbd4e39a25c918f"
    )
    assert codec.encode_payload_target_ack(
        acknowledgement,
        sequence=115,
        sent_at_s=101.02,
    ).hex() == (
        "4d4401140000000000730000000000018a9c000e0000000000000321010000000071"
        "456dae89909ca6100fbb2c5d13664e49"
    )
    assert codec.encode_payload_target_status(status).hex() == (
        "4d4401150000000000720000000000018a92002c11111111111141118111111111111111"
        "00000000000002be0000000700000000000002bf00000008020028038bf2773d9e4deff1"
        "ca97368e7b4b0870"
    )


def test_payload_target_ineligible_status_cannot_fabricate_aimpoint_or_confirmation() -> None:
    values = {
        "sequence": 122,
        "selection_command_id": COMMAND_ID,
        "selected_target_token": 502,
        "selected_target_revision": 11,
        "eligibility": PayloadTargetEligibility.TARGET_NOT_PAYLOAD_ELIGIBLE,
        "produced_at_s": 101.1,
    }
    with pytest.raises(ValueError, match="eligibility and aimpoint"):
        PayloadTargetStatusMessage(
            **values,
            aimpoint_target_token=503,
            aimpoint_target_revision=17,
        )
    with pytest.raises(ValueError, match="ineligible"):
        PayloadTargetStatusMessage(
            **values,
            confirmation_pending=True,
            confirmation_expires_at_s=104.0,
        )


def test_target_pool_pages_round_trip_with_two_entries_and_empty_clear() -> None:
    entries = (
        TargetPoolEntry(
            target_id="vehicle-1",
            state=UnifiedTrackState.TRACKING,
            label="vehicle",
            confidence=0.9,
            tracking_quality=0.8,
            locked=True,
            primary=True,
            actionable=True,
            reid_confirmed=True,
            operator_tracked=True,
            bbox=BoundingBox(0.1, 0.2, 0.3, 0.5),
            relative_bearing_deg=-12.34,
            estimated_range_m=82.4,
            target_speed_mps=4.6,
        ),
        TargetPoolEntry(
            target_id="person-2",
            state=UnifiedTrackState.OCCLUDED,
            label="person",
            confidence=0.7,
            tracking_quality=0.6,
            locked=True,
            primary=False,
            actionable=False,
            reid_confirmed=False,
        ),
    )
    status = TargetPoolStatusMessage(
        sequence=120,
        pool_revision=9,
        page_index=0,
        page_count=1,
        total_track_count=2,
        entries=entries,
        produced_at_s=101.2,
    )
    encoded = _codec().encode_target_pool_status(status)
    decoded = _codec().decode(encoded)
    assert decoded.message_type is WireMessageType.TARGET_POOL_STATUS
    message = decoded.message
    assert isinstance(message, TargetPoolStatusMessage)
    assert message.pool_revision == 9
    assert message.page_index == 0 and message.page_count == 1
    assert message.total_track_count == 2
    assert [entry.state for entry in message.entries] == [
        UnifiedTrackState.TRACKING,
        UnifiedTrackState.OCCLUDED,
    ]
    assert all(entry.target_id.startswith("hash64:") for entry in message.entries)
    assert message.entries[0].primary is True and message.entries[0].locked is True
    assert message.entries[0].operator_tracked is True
    assert message.entries[1].operator_tracked is False
    assert message.entries[1].primary is False and message.entries[1].locked is True
    assert message.entries[0].confidence == pytest.approx(0.9, abs=1 / 254)
    assert message.entries[0].relative_bearing_deg == pytest.approx(-12.34)
    assert message.entries[0].estimated_range_m == pytest.approx(82.4)
    assert message.entries[0].target_speed_mps == pytest.approx(4.6)
    assert message.entries[1].relative_bearing_deg is None
    assert message.entries[1].estimated_range_m is None
    assert message.entries[1].target_speed_mps is None
    assert message.entries[0].bbox is not None
    assert (
        message.entries[0].bbox.x1,
        message.entries[0].bbox.y1,
        message.entries[0].bbox.x2,
        message.entries[0].bbox.y2,
    ) == pytest.approx((0.1, 0.2, 0.3, 0.5), abs=1 / 65535)
    assert message.entries[1].bbox is None
    assert len(encoded) == MAX_TUNNEL_PAYLOAD_BYTES

    empty = TargetPoolStatusMessage(
        sequence=121,
        pool_revision=10,
        page_index=0,
        page_count=1,
        total_track_count=0,
        entries=(),
        produced_at_s=101.3,
    )
    empty_encoded = _codec().encode_target_pool_status(empty)
    assert _codec().decode(empty_encoded).message == empty
    assert len(empty_encoded) == 44


def test_scene_context_status_round_trip_has_no_confidence_or_control_authority() -> None:
    status = SceneContextStatusMessage(
        sequence=122,
        context_revision=11,
        source_frame_id="camera-frame-42",
        source_captured_at_s=101.25,
        state=SceneContextState.VALID,
        page_index=0,
        page_count=1,
        total_region_count=2,
        entries=(
            SceneContextRegionEntry(
                label="road",
                bbox=BoundingBox(0.0, 0.6, 1.0, 1.0),
                frame_area_fraction=0.32,
                bbox_fill_fraction=0.8,
            ),
            SceneContextRegionEntry(
                label="building",
                bbox=BoundingBox(0.1, 0.15, 0.45, 0.75),
                frame_area_fraction=0.18,
                bbox_fill_fraction=0.7,
            ),
        ),
        produced_at_s=101.5,
    )
    encoded = _codec().encode_scene_context_status(status)
    decoded = _codec().decode(encoded)
    assert decoded.message_type is WireMessageType.SCENE_CONTEXT_STATUS
    message = decoded.message
    assert isinstance(message, SceneContextStatusMessage)
    assert message.context_revision == 11
    assert message.state is SceneContextState.VALID
    assert [entry.label for entry in message.entries] == ["road", "building"]
    assert message.entries[0].frame_area_fraction == pytest.approx(0.32, abs=1 / 65535)
    assert message.confidence_available is False
    assert message.target_identity_authority is False
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES

    stale = SceneContextStatusMessage(
        sequence=123,
        context_revision=12,
        source_frame_id="camera-frame-42",
        source_captured_at_s=101.25,
        state=SceneContextState.STALE,
        page_index=0,
        page_count=1,
        total_region_count=0,
        entries=(),
        produced_at_s=104.0,
    )
    decoded_stale = _codec().decode(_codec().encode_scene_context_status(stale)).message
    assert isinstance(decoded_stale, SceneContextStatusMessage)
    assert decoded_stale.state is SceneContextState.STALE
    assert decoded_stale.source_frame_id.startswith("hash64:")
    assert decoded_stale.entries == ()


def test_approach_messages_reject_click_replay_and_control_capability() -> None:
    values = dict(
        command_token=303,
        session_token=404,
        challenge_token=101,
        target_token=202,
        target_revision=7,
        selection_command_id=COMMAND_ID,
        sequence=111,
        issued_at_s=101.0,
        expires_at_s=103.0,
        slide_duration_s=0.01,
        completion_fraction=1.0,
        continuous=False,
    )
    command = ApproachConfirmationCommand(**values)
    assert command.continuous is False
    with pytest.raises(ValueError, match="HIL-only"):
        ApproachConfirmationCommand(**{**values, "flight_control_enabled": True})
    with pytest.raises(ValueError, match="inconsistent"):
        ApproachConfirmationAck(
            command_token=303,
            accepted=False,
            reason=ApproachConfirmationAckReason.ACCEPTED,
            acknowledged_sequence=111,
        )


def test_mission_status_rejects_any_control_capability() -> None:
    values = dict(
        status_id=STATUS_ID,
        sequence=1,
        mission_id="mission",
        phase=MissionPhase.SEARCHING,
        authorization_state=AuthorizationDisplayState.NONE,
        release_window=None,
        safety_allowed=None,
        remaining_payload_count=0,
        total_payload_count=0,
        target_id=None,
        active_payload_slot_id=None,
        target_confidence=None,
        relative_bearing_deg=None,
        estimated_range_m=None,
        cross_track_error_m=None,
        along_track_error_m=None,
        release_lead_distance_m=None,
        produced_at_s=100.0,
    )

    with pytest.raises(ValueError, match="display-only"):
        MissionStatusMessage(**values, physical_release_enabled=True)


def test_patrol_status_round_trip_carries_target_pool_and_revisit_metadata() -> None:
    status = PatrolStatusMessage(
        status_id=STATUS_ID,
        sequence=102,
        mission_id="fire-patrol-demo",
        phase=PatrolPhase.LOST,
        primary_target_id="track-car-7",
        target_state=UnifiedTrackState.LOST,
        bbox=BoundingBox(0.10, 0.20, 0.30, 0.50),
        label="car",
        confidence=0.88,
        tracking_quality=0.21,
        total_track_count=10,
        locked_track_count=3,
        source_frame_id="jetson-frame-701",
        source_captured_at_s=1_000.2,
        produced_at_s=1_000.25,
        return_direction=ReturnObserveDirection.LEFT,
        return_validity=AdvisoryValidity.DEGRADED,
        return_evidence_age_s=0.4,
        estimated_minimum_turn_radius_m=87.3,
    )

    encoded = _codec().encode_patrol_status(status)
    decoded = _codec().decode(encoded)

    assert len(encoded) == 110
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.PATROL_STATUS
    message = decoded.message
    assert isinstance(message, PatrolStatusMessage)
    assert message.phase is PatrolPhase.LOST
    assert message.target_state is UnifiedTrackState.LOST
    assert message.primary_target_id is not None
    assert message.bbox is not None
    assert message.label == "car"
    assert message.confidence == pytest.approx(0.88, abs=1 / 254)
    assert message.tracking_quality == pytest.approx(0.21, abs=1 / 254)
    assert message.total_track_count == 10
    assert message.locked_track_count == 3
    assert message.return_direction is ReturnObserveDirection.LEFT
    assert message.return_validity is AdvisoryValidity.DEGRADED
    assert message.return_evidence_age_s == pytest.approx(0.4)
    assert message.estimated_minimum_turn_radius_m == pytest.approx(87.3)
    assert message.operator_confirmation_required is True
    assert message.sitl_validation_required is True
    assert message.advisory_only is True
    assert message.flight_control_enabled is False


def test_patrol_status_rejects_control_or_partial_revisit_metadata() -> None:
    values = dict(
        status_id=STATUS_ID,
        sequence=1,
        mission_id="mission",
        phase=PatrolPhase.LOST,
        primary_target_id="target",
        target_state=UnifiedTrackState.LOST,
        bbox=None,
        label=None,
        confidence=None,
        tracking_quality=None,
        total_track_count=1,
        locked_track_count=1,
        source_frame_id="frame",
        source_captured_at_s=100.0,
        produced_at_s=100.1,
    )
    with pytest.raises(ValueError, match="confirmed SITL-only"):
        PatrolStatusMessage(**values, flight_control_enabled=True)
    with pytest.raises(ValueError, match="atomic"):
        PatrolStatusMessage(
            **values,
            return_direction=ReturnObserveDirection.LEFT,
        )


def test_safety_status_round_trip_carries_explanatory_rule_masks() -> None:
    status = SafetyStatusMessage(
        status_id=STATUS_ID,
        sequence=101,
        mission_id="fire-fixed-wing-demo",
        target_id="track-fire-7",
        ruleset_version="safety-rules-v1",
        checks=(
            RuleCheck("target.confirmed_track", Verdict.PASS, "confirmed"),
            RuleCheck("navigation.allowed_zone", Verdict.UNKNOWN, "unavailable"),
            RuleCheck("deployment.person_exclusion", Verdict.DENY, "person nearby"),
        ),
        produced_at_s=1_000.25,
    )

    encoded = _codec().encode_safety_status(status)
    decoded = _codec().decode(encoded)

    assert len(encoded) == 86
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.SAFETY_STATUS
    message = decoded.message
    assert isinstance(message, SafetyStatusMessage)
    assert message.target_id.startswith("hash64:")
    assert message.ruleset_version.startswith("hash64:")
    assert [(check.rule_id, check.verdict) for check in message.checks] == [
        ("target.confirmed_track", Verdict.PASS),
        ("navigation.allowed_zone", Verdict.UNKNOWN),
        ("deployment.person_exclusion", Verdict.DENY),
    ]
    assert message.pass_count == 1
    assert message.deny_count == 1
    assert message.unknown_count == 1
    assert message.allowed is False
    assert message.advisory_only is True
    assert message.flight_control_enabled is False
    assert message.physical_release_enabled is False


def test_safety_status_rejects_unregistered_duplicate_or_control_fields() -> None:
    base = dict(
        status_id=STATUS_ID,
        sequence=1,
        mission_id="mission",
        target_id="target",
        ruleset_version="rules-v1",
        produced_at_s=100.0,
    )
    with pytest.raises(ValueError, match="not registered"):
        SafetyStatusMessage(
            **base,
            checks=(RuleCheck("custom.rule", Verdict.PASS, "pass"),),
        )
    duplicate = RuleCheck("target.confirmed_track", Verdict.PASS, "pass")
    with pytest.raises(ValueError, match="duplicated"):
        SafetyStatusMessage(**base, checks=(duplicate, duplicate))
    with pytest.raises(ValueError, match="display-only"):
        SafetyStatusMessage(
            **base,
            checks=(duplicate,),
            physical_release_enabled=True,
        )


def test_authorization_challenge_status_round_trip_fits_tunnel() -> None:
    encoded = _codec().encode_authorization_challenge(_authorization_challenge())
    decoded = _codec().decode(encoded)

    assert len(encoded) == 105
    assert decoded.message_type is WireMessageType.AUTHORIZATION_CHALLENGE
    assert decoded.message == _authorization_challenge()


def test_authorization_decision_round_trip_binds_every_challenge_token() -> None:
    encoded = _codec().encode_authorization_decision(_authorization_decision())
    decoded = _codec().decode(encoded)

    assert len(encoded) == 115
    assert len(encoded) <= MAX_TUNNEL_PAYLOAD_BYTES
    assert decoded.message_type is WireMessageType.AUTHORIZATION_DECISION
    assert decoded.message == _authorization_decision()


def test_authorization_ack_round_trip_is_correlated() -> None:
    acknowledgement = AuthorizationDecisionAck(
        command_token=101,
        accepted=True,
        reason=AuthorizationDecisionAckReason.ACCEPTED,
        acknowledged_sequence=22,
    )

    encoded = _codec().encode_authorization_ack(
        acknowledgement,
        sequence=23,
        sent_at_s=1_003.2,
    )
    decoded = _codec().decode(encoded)

    assert len(encoded) == 50
    assert decoded.message_type is WireMessageType.AUTHORIZATION_ACK
    assert decoded.message == acknowledgement


def test_authorization_command_mutation_is_rejected() -> None:
    encoded = _codec().encode_authorization_decision(_authorization_decision())

    for index in range(len(encoded)):
        mutated = bytearray(encoded)
        mutated[index] ^= 0x01
        with pytest.raises(OperatorProtocolError):
            _codec().decode(bytes(mutated))


def test_authentication_rejects_single_byte_tampering() -> None:
    encoded = bytearray(_codec().encode_selection(_selection()))
    encoded[25] ^= 0x01

    with pytest.raises(OperatorProtocolError, match="authentication"):
        _codec().decode(bytes(encoded))


def test_every_single_byte_mutation_of_selection_frame_is_rejected() -> None:
    encoded = _codec().encode_selection(_selection())

    for index in range(len(encoded)):
        mutated = bytearray(encoded)
        mutated[index] ^= 0x01
        with pytest.raises(OperatorProtocolError):
            _codec().decode(bytes(mutated))


def test_deterministic_malformed_packet_fuzz_cannot_reach_decoder_output() -> None:
    generator = random.Random(20260712)

    for _ in range(256):
        malformed = generator.randbytes(generator.randrange(MAX_TUNNEL_PAYLOAD_BYTES + 1))
        with pytest.raises(OperatorProtocolError):
            _codec().decode(malformed)


def test_decoder_rejects_unregistered_stream_even_with_valid_authentication() -> None:
    encoded = _codec().encode_selection(_selection())
    other = OperatorTunnelCodec(
        hmac_key=KEY,
        geometries=(VideoGeometry("camera-secondary", 1280, 720),),
    )

    with pytest.raises(OperatorProtocolError, match="stream is not registered"):
        other.decode(encoded)


def test_codec_requires_real_key_material_and_wire_uuids() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        OperatorTunnelCodec(hmac_key=b"demo", geometries=(GEOMETRY,))
    with pytest.raises(ValueError, match="must be a UUID"):
        _codec().encode_selection(_selection(command_id="selection-1"))


def test_track_label_and_frame_age_must_fit_compact_wire_contract() -> None:
    base = dict(
        status_id=STATUS_ID,
        selection_command_id=COMMAND_ID,
        sequence=99,
        geometry=GEOMETRY,
        state=TrackingState.TRACKING,
        target_id="target",
        bbox=BoundingBox(0.1, 0.1, 0.2, 0.2),
        confidence=0.9,
        tracking_quality=0.8,
        source_frame_id="frame",
        produced_at_s=100.0,
    )
    with pytest.raises(ValueError, match="16 UTF-8 bytes"):
        _codec().encode_track_status(
            TrackStatusMessage(
                **base,
                label="label-is-far-too-long",
                source_captured_at_s=99.9,
            )
        )
    with pytest.raises(ValueError, match="uint16"):
        _codec().encode_track_status(
            TrackStatusMessage(
                **base,
                label="flame",
                source_captured_at_s=1.0,
            )
        )
