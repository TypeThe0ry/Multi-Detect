from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_link import (
    ApproachChallengeStatusMessage,
    ApproachConfirmationCommand,
    ApproachConfirmationCommandGuard,
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    AuthorizationDecisionCommandGuard,
    PayloadTargetChallengeStatusMessage,
    PayloadTargetConfirmationCommand,
    PayloadTargetConfirmationCommandGuard,
    SelectionAction,
    SelectionCommandGuard,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)

GEOMETRY = VideoGeometry("camera-main", 1280, 720)
APPROACH_SELECTION_ID = "11111111-1111-4111-8111-111111111111"
PAYLOAD_SELECTION_ID = "33333333-3333-4333-8333-333333333333"


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


def _challenge() -> AuthorizationChallengeStatusMessage:
    return AuthorizationChallengeStatusMessage(
        challenge_token=11,
        mission_token=12,
        target_token=13,
        scene_token=14,
        ruleset_token=15,
        payload_slot_token=16,
        target_revision=7,
        created_at_s=100.0,
        expires_at_s=110.0,
        sequence=1,
        produced_at_s=103.0,
    )


def _decision(**overrides: object) -> AuthorizationDecisionCommand:
    values: dict[str, object] = {
        "command_token": 101,
        "session_token": 102,
        "challenge_token": 11,
        "mission_token": 12,
        "target_token": 13,
        "scene_token": 14,
        "ruleset_token": 15,
        "payload_slot_token": 16,
        "target_revision": 7,
        "decision": AuthorizationDecision.APPROVE,
        "operator_token": 103,
        "sequence": 2,
        "issued_at_s": 103.1,
        "expires_at_s": 105.1,
    }
    values.update(overrides)
    return AuthorizationDecisionCommand(**values)


def _approach_challenge() -> ApproachChallengeStatusMessage:
    return ApproachChallengeStatusMessage(
        challenge_token=501,
        target_token=502,
        target_revision=7,
        selection_command_id=APPROACH_SELECTION_ID,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=1,
        produced_at_s=100.1,
    )


def _approach_confirmation(**overrides: object) -> ApproachConfirmationCommand:
    values: dict[str, object] = {
        "command_token": 601,
        "session_token": 602,
        "challenge_token": 501,
        "target_token": 502,
        "target_revision": 7,
        "selection_command_id": APPROACH_SELECTION_ID,
        "sequence": 2,
        "issued_at_s": 101.0,
        "expires_at_s": 103.0,
        "slide_duration_s": 0.8,
        "completion_fraction": 1.0,
        "continuous": True,
    }
    values.update(overrides)
    return ApproachConfirmationCommand(**values)


def _payload_challenge() -> PayloadTargetChallengeStatusMessage:
    return PayloadTargetChallengeStatusMessage(
        challenge_token=701,
        selected_target_token=702,
        selected_target_revision=11,
        aimpoint_target_token=703,
        aimpoint_target_revision=17,
        selection_command_id=PAYLOAD_SELECTION_ID,
        issued_at_s=100.0,
        expires_at_s=105.0,
        sequence=1,
        produced_at_s=100.1,
    )


def _payload_confirmation(**overrides: object) -> PayloadTargetConfirmationCommand:
    values: dict[str, object] = {
        "command_token": 801,
        "session_token": 802,
        "challenge_token": 701,
        "selected_target_token": 702,
        "selected_target_revision": 11,
        "aimpoint_target_token": 703,
        "aimpoint_target_revision": 17,
        "selection_command_id": PAYLOAD_SELECTION_ID,
        "sequence": 2,
        "issued_at_s": 101.0,
        "expires_at_s": 103.0,
        "slide_duration_s": 0.8,
        "completion_fraction": 1.0,
        "continuous": True,
    }
    values.update(overrides)
    return PayloadTargetConfirmationCommand(**values)


def test_accepts_fresh_normalized_selection_once() -> None:
    guard = SelectionCommandGuard(GEOMETRY)
    command = _selection()

    assert guard.evaluate(command, received_at_s=100.2).allowed is True
    replay = guard.evaluate(command, received_at_s=100.3)
    assert replay.allowed is False
    assert "already been processed" in " ".join(replay.reasons)


def test_payload_target_guard_binds_selected_and_fire_aimpoint_revisions() -> None:
    guard = PayloadTargetConfirmationCommandGuard(clock_tolerance_s=0.0)
    guard.set_active_challenge(_payload_challenge())

    accepted = guard.evaluate(_payload_confirmation(), received_at_s=101.1)
    assert accepted.allowed is True
    assert guard.evaluate(_payload_confirmation(), received_at_s=101.2).duplicate is True

    for changed in (
        {"command_token": 811, "selected_target_revision": 12},
        {"command_token": 812, "aimpoint_target_token": 999},
        {"command_token": 813, "aimpoint_target_revision": 18},
        {"command_token": 814, "selection_command_id": APPROACH_SELECTION_ID},
    ):
        result = PayloadTargetConfirmationCommandGuard(clock_tolerance_s=0.0)
        result.set_active_challenge(_payload_challenge())
        assessment = result.evaluate(_payload_confirmation(**changed), received_at_s=101.1)
        assert assessment.allowed is False
        assert "does not match" in " ".join(assessment.reasons)


def test_payload_target_guard_rejects_click_incomplete_and_expired_slide() -> None:
    guard = PayloadTargetConfirmationCommandGuard(clock_tolerance_s=0.0)
    guard.set_active_challenge(_payload_challenge())
    incomplete = guard.evaluate(
        _payload_confirmation(
            slide_duration_s=0.1,
            completion_fraction=0.5,
            continuous=False,
        ),
        received_at_s=101.1,
    )
    assert incomplete.allowed is False
    assert "slide evidence" in " ".join(incomplete.reasons)

    expired_guard = PayloadTargetConfirmationCommandGuard(clock_tolerance_s=0.0)
    expired_guard.set_active_challenge(_payload_challenge())
    expired = expired_guard.evaluate(
        _payload_confirmation(command_token=899, issued_at_s=105.0, expires_at_s=105.1),
        received_at_s=105.0,
    )
    assert expired.allowed is False
    assert "expired" in " ".join(expired.reasons)


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


def test_authorization_guard_requires_exact_active_challenge_and_is_idempotent() -> None:
    guard = AuthorizationDecisionCommandGuard(clock_tolerance_s=0.0)
    no_challenge = guard.evaluate(_decision(), received_at_s=103.2)
    assert no_challenge.allowed is False
    assert "no pending" in " ".join(no_challenge.reasons)

    guard.set_active_challenge(_challenge())
    command = _decision(command_token=201)
    accepted = guard.evaluate(command, received_at_s=103.2)
    duplicate = guard.evaluate(command, received_at_s=103.3)

    assert accepted.allowed is True
    assert accepted.duplicate is False
    assert duplicate.allowed is True
    assert duplicate.duplicate is True


def test_authorization_guard_rejects_second_decision_and_changed_command_token_content() -> None:
    guard = AuthorizationDecisionCommandGuard(clock_tolerance_s=0.0)
    guard.set_active_challenge(_challenge())
    original = _decision(command_token=301)
    assert guard.evaluate(original, received_at_s=103.2).allowed is True

    second = guard.evaluate(
        _decision(command_token=302, sequence=3, decision=AuthorizationDecision.DENY),
        received_at_s=103.3,
    )
    changed = guard.evaluate(
        _decision(command_token=301, operator_token=999),
        received_at_s=103.3,
    )

    assert second.allowed is False
    assert "already has a decision" in " ".join(second.reasons)
    assert changed.allowed is False
    assert "different content" in " ".join(changed.reasons)


def test_authorization_guard_accepts_recent_published_snapshot_for_same_challenge() -> None:
    guard = AuthorizationDecisionCommandGuard(clock_tolerance_s=0.0)
    original = _challenge()
    guard.set_active_challenge(original)
    guard.set_active_challenge(
        replace(
            original,
            scene_token=140,
            target_revision=8,
            sequence=2,
            produced_at_s=104.0,
        )
    )

    acceptance = guard.evaluate(_decision(), received_at_s=103.2)

    assert acceptance.allowed is True


def test_authorization_guard_rejects_wrong_binding_stale_and_outliving_command() -> None:
    guard = AuthorizationDecisionCommandGuard(clock_tolerance_s=0.0)
    guard.set_active_challenge(_challenge())

    result = guard.evaluate(
        _decision(
            command_token=401,
            target_token=999,
            issued_at_s=106.0,
            expires_at_s=110.5,
        ),
        received_at_s=111.0,
    )

    assert result.allowed is False
    reasons = " ".join(result.reasons)
    assert "does not match" in reasons
    assert "expired" in reasons
    assert "outlives" in reasons
    assert "stale" in reasons


def test_approach_guard_requires_continuous_full_slide_and_exact_binding() -> None:
    guard = ApproachConfirmationCommandGuard(clock_tolerance_s=0.0)
    guard.set_active_challenge(_approach_challenge())

    click = guard.evaluate(
        _approach_confirmation(command_token=701, slide_duration_s=0.05, continuous=False),
        received_at_s=101.1,
    )
    wrong_target = guard.evaluate(
        _approach_confirmation(command_token=702, target_token=999, sequence=3),
        received_at_s=101.2,
    )
    accepted_command = _approach_confirmation(command_token=703, sequence=4)
    accepted = guard.evaluate(accepted_command, received_at_s=101.2)
    duplicate = guard.evaluate(accepted_command, received_at_s=101.3)

    assert click.allowed is False
    assert "incomplete" in " ".join(click.reasons)
    assert wrong_target.allowed is False
    assert "does not match" in " ".join(wrong_target.reasons)
    assert accepted.allowed is True
    assert duplicate.allowed is True and duplicate.duplicate is True


def test_approach_guard_rejects_replay_after_challenge_consumption() -> None:
    guard = ApproachConfirmationCommandGuard(clock_tolerance_s=0.0)
    guard.set_active_challenge(_approach_challenge())
    assert guard.evaluate(_approach_confirmation(), received_at_s=101.1).allowed is True

    replay = guard.evaluate(
        _approach_confirmation(command_token=801, sequence=3),
        received_at_s=101.2,
    )
    assert replay.allowed is False
    assert "already consumed" in " ".join(replay.reasons)
