from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.authorization import AuthorizationExpired, AuthorizationStatus
from multidetect.config import MissionConfig
from multidetect.domain import BoundingBox, Detection, MissionPhase, PayloadState, SensorKind
from multidetect.mission import MissionController
from multidetect.payload import PayloadFeedbackError
from multidetect.replay import load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]


def controller_and_frames() -> tuple[MissionController, tuple]:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")
    controller = MissionController(config)
    controller.launch(now_s=999.0)
    controller.arrive_task_area(now_s=999.1)
    return controller, frames


def reach_authorization_challenge(controller: MissionController, frames: tuple):
    outcome = None
    for frame in frames:
        outcome = controller.process_observation(frame, now_s=frame.captured_at_s)
    assert outcome is not None
    assert outcome.challenge is not None
    return outcome.challenge


def test_replay_to_confirmed_simulated_release() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)

    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    assert controller.state.phase is MissionPhase.DEPLOYMENT_READY
    assert controller.fake_payload_port.request_count == 0

    release_id = controller.request_simulated_deployment(now_s=1003.2)
    assert controller.fake_payload_port.request_count == 1
    assert controller.payload.get_slot("payload-1").state is PayloadState.RELEASE_REQUESTED

    controller.report_simulated_execution(release_id=release_id, now_s=1003.3)
    assert controller.state.phase is MissionPhase.VERIFYING_RELEASE
    assert controller.payload.get_slot("payload-1").state is PayloadState.RELEASED

    controller.report_independent_confirmation(
        release_id=release_id,
        source_id="simulated-bay-presence-sensor",
        now_s=1003.4,
    )

    assert controller.state.phase is MissionPhase.SEARCHING
    assert controller.payload.get_slot("payload-1").state is PayloadState.RELEASE_CONFIRMED
    assert controller.payload.remaining_payload_count == 2
    assert controller.fake_payload_port.request_count == 1


def test_approval_expires_without_release() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)

    with pytest.raises(AuthorizationExpired):
        controller.approve_authorization(
            challenge_id=challenge.challenge_id,
            nonce=challenge.nonce,
            operator_id="demo-operator",
            now_s=challenge.expires_at_s,
        )

    assert controller.state.phase is MissionPhase.SEARCHING
    assert controller.fake_payload_port.request_count == 0


def test_timeout_faults_and_never_retries() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    controller.request_simulated_deployment(now_s=1003.2)

    assert controller.check_release_timeout(now_s=1008.2) is True

    assert controller.state.phase is MissionPhase.FAULT
    assert controller.payload.get_slot("payload-1").state is PayloadState.FAILED
    assert controller.fake_payload_port.request_count == 1


def test_disposable_platform_terminates_after_confirmation() -> None:
    config = MissionConfig.from_json(
        ROOT / "configs/missions/fire_suppression_disposable.demo.json"
    )
    frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")
    controller = MissionController(config)
    controller.launch(now_s=999.0)
    controller.arrive_task_area(now_s=999.1)
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    release_id = controller.request_simulated_deployment(now_s=1003.2)
    controller.report_simulated_execution(release_id=release_id, now_s=1003.3)
    controller.report_independent_confirmation(
        release_id=release_id,
        source_id="simulated-bay-presence-sensor",
        now_s=1003.4,
    )

    assert controller.state.phase is MissionPhase.TERMINATED
    assert controller.payload.remaining_payload_count == 0


def test_new_person_frame_invalidates_pending_authorization() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    person = Detection(
        "person",
        0.96,
        BoundingBox(0.50, 0.48, 0.62, 0.72),
        SensorKind.RGB,
        "safety-object-demo",
    )
    changed_scene = replace(
        frames[-1],
        frame_id="frame-004",
        captured_at_s=1003.5,
        detections=(*frames[-1].detections, person),
    )

    outcome = controller.process_observation(changed_scene, now_s=1003.5)

    assert outcome.challenge is None
    assert controller.state.phase is MissionPhase.SEARCHING
    assert controller.authorizations.status(challenge.challenge_id) is AuthorizationStatus.DENIED
    assert controller.fake_payload_port.request_count == 0


def test_equivalent_live_frame_keeps_pending_challenge_identity() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    live_frame = replace(
        frames[-1],
        frame_id="frame-004",
        captured_at_s=1003.5,
    )

    outcome = controller.process_observation(live_frame, now_s=1003.5)

    assert outcome.challenge is not None
    assert outcome.challenge.challenge_id == challenge.challenge_id
    assert outcome.challenge.nonce == challenge.nonce
    assert outcome.challenge.target_revision > challenge.target_revision
    assert controller.state.phase is MissionPhase.AWAITING_AUTHORIZATION
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.6,
    )
    assert controller.state.phase is MissionPhase.DEPLOYMENT_READY


def test_equivalent_live_frame_refreshes_approved_binding_without_reapproval() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    live_frame = replace(
        frames[-1],
        frame_id="frame-004",
        captured_at_s=1003.5,
    )

    outcome = controller.process_observation(live_frame, now_s=1003.5)
    release_id = controller.request_simulated_deployment(now_s=1003.6)

    assert outcome.challenge is None
    assert controller.state.phase is MissionPhase.DEPLOYING
    assert controller.payload.get_slot("payload-1").release_id == release_id
    assert controller.fake_payload_port.request_count == 1


def test_new_person_frame_relocks_approved_slot() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    person = Detection(
        "firefighter",
        0.95,
        BoundingBox(0.50, 0.48, 0.62, 0.72),
        SensorKind.RGB,
        "safety-object-demo",
    )
    changed_scene = replace(
        frames[-1],
        frame_id="frame-004",
        captured_at_s=1003.5,
        detections=(*frames[-1].detections, person),
    )

    outcome = controller.process_observation(changed_scene, now_s=1003.5)

    assert outcome.challenge is None
    assert controller.state.phase is MissionPhase.SEARCHING
    assert controller.payload.get_slot("payload-1").state is PayloadState.LOCKED
    assert controller.fake_payload_port.request_count == 0


def test_tick_expires_unanswered_challenge() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)

    status = controller.tick(now_s=challenge.expires_at_s)

    assert status.phase is MissionPhase.SEARCHING
    assert status.pending_challenge_id is None
    assert controller.authorizations.status(challenge.challenge_id) is AuthorizationStatus.EXPIRED


def test_expired_approved_authorization_relocks_without_request() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    config = replace(
        config,
        safety=replace(
            config.safety,
            authorization_ttl_seconds=1.0,
            sensor_data_max_age_seconds=20.0,
        ),
    )
    frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")
    controller = MissionController(config)
    controller.launch(now_s=999.0)
    controller.arrive_task_area(now_s=999.1)
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )

    with pytest.raises(AuthorizationExpired):
        controller.request_simulated_deployment(now_s=challenge.expires_at_s)

    assert controller.state.phase is MissionPhase.SEARCHING
    assert controller.payload.get_slot("payload-1").state is PayloadState.LOCKED
    assert controller.fake_payload_port.request_count == 0


def test_recently_served_region_survives_track_id_rebuild() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    release_id = controller.request_simulated_deployment(now_s=1003.2)
    controller.report_simulated_execution(release_id=release_id, now_s=1003.3)
    controller.report_independent_confirmation(
        release_id=release_id,
        source_id="simulated-bay-presence-sensor",
        now_s=1003.4,
    )
    controller.process_observation(
        replace(
            frames[-1],
            frame_id="gap-frame",
            captured_at_s=1005.0,
            detections=(),
        ),
        now_s=1005.0,
    )

    outcome = None
    for index, timestamp in enumerate((1006.0, 1007.0, 1008.0, 1009.0), start=1):
        outcome = controller.process_observation(
            replace(
                frames[-1],
                frame_id=f"reidentified-{index}",
                captured_at_s=timestamp,
            ),
            now_s=timestamp,
        )

    assert outcome is not None
    assert outcome.challenge is None
    assert controller.state.phase is MissionPhase.SEARCHING
    assert controller.fake_payload_port.request_count == 1


def test_wrong_release_feedback_faults_mission() -> None:
    controller, frames = controller_and_frames()
    challenge = reach_authorization_challenge(controller, frames)
    controller.approve_authorization(
        challenge_id=challenge.challenge_id,
        nonce=challenge.nonce,
        operator_id="demo-operator",
        now_s=1003.1,
    )
    controller.request_simulated_deployment(now_s=1003.2)

    with pytest.raises(PayloadFeedbackError):
        controller.report_simulated_execution(release_id="wrong-release", now_s=1003.3)

    assert controller.state.phase is MissionPhase.FAULT
    assert controller.payload.faulted is True


def test_invalid_timestamp_does_not_advance_state() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    controller = MissionController(config)

    with pytest.raises(ValueError, match="finite"):
        controller.launch(now_s=float("nan"))

    assert controller.state.phase is MissionPhase.STANDBY
    assert len(controller.audit) == 0


def test_non_monotonic_timestamp_does_not_advance_state() -> None:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    controller = MissionController(config)
    controller.launch(now_s=10.0)

    with pytest.raises(ValueError, match="monotonic"):
        controller.arrive_task_area(now_s=9.0)

    assert controller.state.phase is MissionPhase.NAVIGATING
    assert len(controller.audit) == 1
