from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.mission import MissionController, MissionOperationError
from multidetect.payload_target_gate import PayloadTargetIntent
from multidetect.replay import load_jsonl_replay

ROOT = Path(__file__).resolve().parents[1]


def _prepared_mission() -> tuple[MissionController, object, str]:
    config = MissionConfig.from_json(ROOT / "configs/missions/fire_suppression.demo.json")
    frames = load_jsonl_replay(ROOT / "examples/fire_mission_replay.jsonl")
    mission = MissionController(config)
    mission.launch(now_s=998.0)
    mission.arrive_task_area(now_s=999.0)
    outcome = None
    for frame in frames:
        outcome = mission.process_observation(
            frame,
            now_s=frame.captured_at_s,
            require_payload_target_intent=True,
        )
        assert outcome.challenge is None
    assert outcome is not None
    target = next(track for track in outcome.tracks if track.confirmed)
    next_frame = replace(frames[-1], frame_id="frame-004", captured_at_s=1003.1)
    return mission, next_frame, target.track_id


def _intent(target_id: str, *, expires_at_s: float = 1008.0) -> PayloadTargetIntent:
    return PayloadTargetIntent(
        selection_command_id="11111111-1111-4111-8111-111111111111",
        selected_target_id="unified-fire-1",
        selected_target_revision=11,
        aimpoint_target_id=target_id,
        aimpoint_target_revision=12,
        accepted_at_s=1003.05,
        expires_at_s=expires_at_s,
    )


def test_deployment_authorization_cannot_exist_before_bound_slide_intent() -> None:
    mission, frame, target_id = _prepared_mission()

    no_intent = mission.process_observation(
        frame,
        now_s=1003.1,
        require_payload_target_intent=True,
    )
    assert no_intent.challenge is None

    authorized_frame = replace(frame, frame_id="frame-005", captured_at_s=1003.2)
    intent = _intent(target_id)
    outcome = mission.process_observation(
        authorized_frame,
        now_s=1003.2,
        payload_target_intent=intent,
        require_payload_target_intent=True,
    )
    assert outcome.challenge is not None
    assert outcome.challenge.target_id == target_id


def test_intent_for_different_fire_track_cannot_create_authorization() -> None:
    mission, frame, _target_id = _prepared_mission()
    outcome = mission.process_observation(
        frame,
        now_s=1003.1,
        payload_target_intent=_intent("mission-fire-does-not-exist"),
        require_payload_target_intent=True,
    )
    assert outcome.challenge is None


def test_slide_intent_expiry_revokes_pending_authorization_before_approval() -> None:
    mission, frame, target_id = _prepared_mission()
    intent = _intent(target_id, expires_at_s=1003.25)
    outcome = mission.process_observation(
        frame,
        now_s=1003.1,
        payload_target_intent=intent,
        require_payload_target_intent=True,
    )
    assert outcome.challenge is not None

    with pytest.raises(MissionOperationError, match="slide confirmation expired"):
        mission.approve_authorization(
            challenge_id=outcome.challenge.challenge_id,
            nonce=outcome.challenge.nonce,
            operator_id="operator-mode2",
            now_s=1003.3,
        )
    assert mission.status().pending_challenge_id is None
