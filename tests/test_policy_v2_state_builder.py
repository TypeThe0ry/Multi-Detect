import hashlib

import pytest

from multidetect.autonomy.policy_v2 import (
    PolicyContractError,
    StructuredSceneStateBuilder,
    TaskSpecV1,
)


def test_runtime_and_sim_observation_have_same_canonical_state() -> None:
    task = TaskSpecV1(prompt_sha256=hashlib.sha256(b"same").hexdigest())
    observation = {
        "captured_at_s": 2.0,
        "frame_id": "frame-2",
        "pose": {"local_ned_m": [1.0, 2.0, -3.0]},
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 90.0},
        "velocity": [1.0, 0.0, 0.0],
        "gps_fix": {"fix_type": 3},
    }
    live = StructuredSceneStateBuilder().from_runtime(
        captured_at_s=2.0,
        frame_id="frame-2",
        task_spec=task,
        pose=observation,
    )
    replay = StructuredSceneStateBuilder().from_sim_step_record(
        {"task_text": "ignored", "obs_before": observation}, task
    )
    assert live.canonical_hash == replay.canonical_hash


def test_missing_pose_is_masked_and_labels_do_not_enter_state() -> None:
    builder = StructuredSceneStateBuilder()
    state = builder.from_sim_step_record(
        {
            "task_text": "search",
            "obs_before": {
                "captured_at_s": 1.0,
                "frame_id": "f",
                "labels": {"privileged_target": True},
            },
            "labels": {"truth": "hidden"},
        }
    )
    assert state.valid_mask["pose"] is False
    assert "labels" not in state.to_json()


def _task() -> TaskSpecV1:
    return TaskSpecV1(prompt_sha256=hashlib.sha256(b"policy-v2-cases").hexdigest())


def _observation(case: str) -> dict[str, object]:
    base: dict[str, object] = {
        "captured_at_s": 2.0,
        "frame_id": "frame-case",
        "local_ned_m": [1.0, 2.0, -3.0],
        "attitude": {"roll_deg": 1.0, "pitch_deg": -2.0, "yaw_deg": 90.0},
        "velocity": [1.0, 0.0, -0.1],
        "gps_fix_type": 3,
    }
    if case == "one_track_with_range":
        base["tracks"] = [{
            "track_id": "t1", "label": "car", "state": "TRACKING",
            "bbox": {"x1": 0.1, "y1": 0.2, "x2": 0.4, "y2": 0.5},
            "confidence": 0.9, "tracking_quality": 0.8, "last_seen_at_s": 1.8,
            "primary": True, "actionable": True, "class_match": True, "color_match": False,
        }]
        base["ranges"] = [
            {
                "target_id": "t1",
                "validity": "valid",
                "ground_range_m": 20.0,
                "absolute_bearing_deg": 15.0,
            }
        ]
    elif case == "track_without_range":
        base["tracks"] = [{
            "track_id": "t1", "label": "person", "state": "LOST", "bbox": [0.2, 0.2, 0.3, 0.4],
            "confidence": 0.4,
            "tracking_quality": 0.2,
            "missed_frame_count": 3,
            "last_seen_at_s": 1.0,
        }]
        base["ranges"] = []
    elif case == "gps_lost":
        base["gps_fix_type"] = 0
    elif case == "pose_stale":
        base.pop("local_ned_m")
        base["pose_age_s"] = 10.0
    elif case == "no_track":
        base["tracks"] = []
        base["ranges"] = []
    else:
        raise AssertionError(case)
    return base


@pytest.mark.parametrize(
    "case",
    ["no_track", "one_track_with_range", "track_without_range", "gps_lost", "pose_stale"],
)
def test_runtime_and_sim_records_match_for_realistic_cases(case: str) -> None:
    task = _task()
    observation = _observation(case)
    runtime = StructuredSceneStateBuilder().from_runtime(
        captured_at_s=observation["captured_at_s"],
        frame_id=observation["frame_id"],
        task_spec=task,
        pose=observation,
        tracks=observation.get("tracks", ()),
        ranges=observation.get("ranges", ()),
    )
    replay = StructuredSceneStateBuilder().from_sim_step_record(
        {"task_text": "ignored", "obs_before": observation}, task
    )
    assert runtime.canonical_hash == replay.canonical_hash
    if case == "gps_lost":
        assert runtime.valid_mask["gps"] is False
    if case == "pose_stale":
        assert runtime.valid_mask["pose"] is False
    if case == "track_without_range":
        assert runtime.valid_mask["range"] is False


def test_history_is_zero_padded_until_sixteen_ticks() -> None:
    state = StructuredSceneStateBuilder().from_runtime(
        captured_at_s=1.0,
        frame_id="frame-1",
        task_spec=_task(),
        pose={"local_ned_m": [0.0, 0.0, 0.0]},
    )
    assert len(state.history) == 16
    assert all(item == {} for item in state.history[:-1])
    assert state.history[-1]["frame_id"] == "frame-1"


def test_builder_rejects_time_moving_backwards() -> None:
    builder = StructuredSceneStateBuilder()
    builder.from_runtime(captured_at_s=2.0, frame_id="frame-1", task_spec=_task())
    with pytest.raises(PolicyContractError, match="moved backwards"):
        builder.from_runtime(captured_at_s=1.0, frame_id="frame-2", task_spec=_task())
