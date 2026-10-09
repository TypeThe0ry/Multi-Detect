import hashlib

from multidetect.autonomy.policy_v2 import StructuredSceneStateBuilder, TaskSpecV1


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
