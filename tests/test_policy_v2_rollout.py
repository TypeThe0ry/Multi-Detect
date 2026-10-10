import hashlib

from multidetect.autonomy.policy_v2.contracts import TaskSpecV1
from multidetect.autonomy.policy_v2.rollout import RolloutConfig, rollout_episode


def _task() -> TaskSpecV1:
    return TaskSpecV1(prompt_sha256=hashlib.sha256(b"red car").hexdigest())


def _obs(index: int = 0) -> dict:
    return {
        "captured_at_s": float(index),
        "frame_id": f"frame-{index}",
        "pose": {"local_ned_m": [float(index), 0.0, -10.0]},
        "attitude": {"yaw_deg": 0.0},
    }


class _Detector:
    def annotate_observation(self, observation, rgb, depth):
        return {"detections": []}


class _Policy:
    def __init__(self, action):
        self.action = action
        self.states = []

    def act(self, state):
        self.states.append(state)
        return self.action


class _Expert:
    def labels(self, *, pose_ned, yaw_deg):
        return {
            "target_in_fov": True,
            "target_bearing_deg": 4.0,
            "target_range_m": 16.0,
        }

    def next_action(self, *, pose_ned, yaw_deg):
        from multidetect.sim_teacher.primitives import ActionPrimitive

        return ActionPrimitive.translate(1.0, 0.0)


def test_stop_ends_loop_and_keeps_dagger_label():
    policy = _Policy({"kind": "stop"})
    metrics, rows = rollout_episode(
        [_obs(0), _obs(1)],
        detector=_Detector(),
        policy=policy,
        task_spec=_task(),
        expert=_Expert(),
        target_ned=(0.0, 0.0, -10.0),
        config=RolloutConfig(max_steps=5),
    )
    assert metrics.steps == 1
    assert len(rows) == 1
    assert rows[0]["labels"]["expert_action"]["kind"] == "translate"
    assert rows[0]["labels"]["target_in_fov"] is True


def test_nonfinite_action_fails_closed_to_hold():
    metrics, rows = rollout_episode(
        [_obs()],
        detector=_Detector(),
        policy=_Policy({"kind": "translate", "forward_m": float("nan")}),
        task_spec=_task(),
        config=RolloutConfig(max_steps=1),
    )
    assert rows[0]["action"]["kind"] == "hold"
    assert metrics.safety_events["nan_hold"] == 1


def test_privileged_target_never_enters_policy_state():
    policy = _Policy({"kind": "stop"})
    rollout_episode(
        [_obs()],
        detector=_Detector(),
        policy=policy,
        task_spec=_task(),
        expert=_Expert(),
        target_ned=(123.0, 456.0, -10.0),
        config=RolloutConfig(max_steps=1),
    )
    state = policy.states[0].to_mapping()
    assert state["truth_free"] is True
    assert "labels" not in str(state).lower()
    assert "123.0" not in str(state)
    assert "456.0" not in str(state)
