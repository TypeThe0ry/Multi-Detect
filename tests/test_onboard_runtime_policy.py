from __future__ import annotations

import io
import math
from types import SimpleNamespace

import pytest
from PIL import Image

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.onboard_loop import Detection
from multidetect.onboard_runtime.camera import CameraCalibration
from multidetect.onboard_runtime.policy import HomeRelativePolicy
from multidetect.onboard_runtime.projection import GroundProjector
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts.onboard_policy import OnboardPolicy


def test_injected_backends_run_the_actual_policy_and_mission_executor(tmp_path, monkeypatch):
    def forbidden_backend(*args, **kwargs):
        pytest.fail("injected bench must not construct a model")

    monkeypatch.setattr("scripts.onboard_policy.YoloeProposer", forbidden_backend)
    monkeypatch.setattr("scripts.eval_slow_system.LlamaServerRunner", forbidden_backend)
    from multidetect.onboard_runtime.bench import LatencySlowRunner

    slow = LatencySlowRunner(0)
    calibration = CameraCalibration(100, 80, 90, 90, 50, 40)
    policy = OnboardPolicy(
        task_text="Find a car", world_path=None, altitude_m=10,
        camera_info=calibration.camera_info, image_size=(100, 80), work_dir=tmp_path,
        config={"mission": "find", "camera_pitch_down_rad": 0.5},
        proposer=lambda bgr: [Detection((45, 45, 55, 55), 0.9)], slow_runner=slow,
        ground_projector=lambda u, v, obs: (30, 20), home_ned=(10, 20, 0),
    )
    buffer = io.BytesIO()
    Image.new("RGB", (100, 80)).save(buffer, format="PNG")
    action, labels = policy.act(
        {"rgb": buffer.getvalue(), "camera_info": calibration.camera_info},
        pose_ned=(10, 20, -10), yaw_deg=0,
    )
    assert isinstance(policy.executor, MissionExecutor)
    assert isinstance(action, ActionPrimitive)
    assert slow.calls == 1
    assert labels["onboard"]["detections"] == 1
    assert policy.executor.home == (10, 20)
    assert policy.loop.home == (10, 20)
    assert policy.loop.camera.pitch_down_rad == 0.5
    assert policy._search.home_ned == (10, 20, 0)
    assert policy._search.camera_pitch_rad == 0.5
    policy.close()
    assert slow.stopped.is_set()


def test_default_policy_still_constructs_legacy_model_backends(tmp_path, monkeypatch):
    calls = []

    def proposer(*args, **kwargs):
        calls.append(("proposer", args, kwargs))
        return lambda bgr: []

    def slow(*args, **kwargs):
        calls.append(("slow", args, kwargs))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr("scripts.onboard_policy.YoloeProposer", proposer)
    monkeypatch.setattr("scripts.eval_slow_system.LlamaServerRunner", slow)
    policy = OnboardPolicy(
        task_text="Find a car", world_path=None, altitude_m=10,
        camera_info=CameraCalibration(100, 80, 90, 90, 50, 40).camera_info,
        image_size=(100, 80), work_dir=tmp_path,
    )
    assert [item[0] for item in calls] == ["proposer", "slow"]
    assert calls[1][2]["identity_only"] is True
    assert calls[1][2]["logprobs"] is True
    assert policy.loop.home is None
    assert policy.loop.camera.pitch_down_rad == 0.18
    assert policy.ground_projector is None
    policy.close()


def calibration(**mount):
    return CameraCalibration(100, 80, 90, 90, 50, 40, extrinsics={
        "mount_pitch_down_deg": 45, "mount_yaw_right_deg": 0,
        "mount_roll_clockwise_deg": 0, "camera_offset_forward_m": 0,
        "camera_offset_right_m": 0, "camera_offset_down_m": 0, **mount,
    })


def observation(yaw=0):
    return {"pose": {"local_ned_m": (0, 0, -10)},
            "attitude": {"roll_deg": 0, "pitch_deg": 0, "yaw_deg": yaw}}


def test_ground_projector_uses_mount_and_vehicle_attitude_and_lever_arm():
    projector = GroundProjector(calibration())
    assert projector(50, 40, observation()) == pytest.approx((10, 0))
    assert projector(50, 40, observation(90)) == pytest.approx((0, 10))
    shifted = GroundProjector(calibration(camera_offset_forward_m=2, camera_offset_down_m=1))
    assert shifted(50, 40, observation()) == pytest.approx((11, 0))
    sideways = GroundProjector(calibration(mount_yaw_right_deg=90))
    assert sideways(50, 40, observation()) == pytest.approx((0, 10))
    upward = GroundProjector(calibration(mount_pitch_down_deg=-45))
    assert upward(50, 40, observation()) is None
    with pytest.raises(ValueError, match="extrinsics missing"):
        GroundProjector(CameraCalibration(100, 80, 90, 90, 50, 40))
    with pytest.raises(ValueError, match="finite"):
        GroundProjector(calibration(mount_pitch_down_deg=math.inf))


def test_home_relative_policy_only_changes_down_origin():
    calls = []

    def act(obs, *, pose_ned, yaw_deg):
        calls.append((obs, pose_ned, yaw_deg))
        return ActionPrimitive.hold(1), {}

    stopped = []
    wrapped = HomeRelativePolicy(SimpleNamespace(act=act, close=lambda: stopped.append(True)), 7)
    source = {"pose": {"local_ned_m": (10, 20, -3)}, "attitude": {"yaw_deg": 30}}
    wrapped.act(source, pose_ned=(10, 20, -3), yaw_deg=30)
    assert calls[0][1:] == ((10, 20, -10), 30)
    assert calls[0][0]["pose"]["local_ned_m"] == (10, 20, -10)
    assert source["pose"]["local_ned_m"] == (10, 20, -3)
    wrapped.close()
    assert stopped == [True]
