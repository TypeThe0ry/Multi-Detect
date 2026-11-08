from __future__ import annotations

import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from multidetect.camera_mount import CameraMount, compose_camera_pose, rpy_rotation
from multidetect.sim_teacher.recording import EpisodeRecorder
from multidetect.sim_teacher.streams import (
    MavlinkState,
    SensorSample,
    _propeller_joint_state,
    build_observation,
    validate_gimbal_recording_observation,
)
from scripts.gazebo_sensor_stream import _frame_joint_state
from multidetect.simulator.camera_projection import _pixel_camera_ray_raw
from scripts.render_onboard_sdf_blender_dataset import apply_propeller_state, camera_samples

ROOT = Path(__file__).resolve().parents[1]


def _mavlink() -> MavlinkState:
    state = MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {
                "x": 1.0,
                "y": 2.0,
                "z": -5.0,
                "vx": 0.0,
                "vy": 0.0,
                "vz": 0.0,
                "time_boot_ms": 10,
            },
            "ATTITUDE": {"roll": 0.1, "pitch": 0.2, "yaw": 0.3},
            "GLOBAL_POSITION_INT": {"lat": 1_000_000, "lon": 2_000_000, "relative_alt": 5000},
            "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10, "eph": 100},
        },
        received_monotonic_ns={"LOCAL_POSITION_NED": 100},
    )
    return state


def test_fixed_observation_contains_composed_pose_and_explicit_joint_states() -> None:
    sensor = SensorSample(
        rgb=b"rgb",
        width=1,
        height=1,
        depth=None,
        camera_info={"width": 1, "height": 1},
        pose={},
        velocity=[0.0, 0.0, 0.0],
        sensor_stamp_ns=1_000_000,
        received_monotonic_ns=100,
        received_wall_time=1.0,
    )
    result = build_observation(
        sensor,
        _mavlink(),
        camera_mount=CameraMount.fixed(pitch_rad=0.18, offset_body_ned_m=(0.18, 0.0, -0.12)),
    )
    assert "camera_pose" not in result
    assert result["gimbal_joint_state"]["source"] == "fixed_mount"
    assert result["propeller_joint_state"]["source"] == "unavailable"


def test_sidecar_joint_state_survives_episode_recording(tmp_path: Path) -> None:
    recorder = EpisodeRecorder(tmp_path, "episode")
    obs = {
        "rgb": b"raw",
        "depth": np.ones((1, 1), dtype=np.float32),
        "gimbal_joint_state": {"timestamp_ns": 10, "position_rad": {"pitch": 0.2}},
        "propeller_joint_state": {"timestamp_ns": 10, "angles_rad": [1.0], "spin_rad_s": [12.0]},
        "camera_pose": {"center_ned": [1.0, 2.0, 3.0]},
    }
    after = {
        **obs,
        "gimbal_joint_state": {
            "timestamp_ns": 20,
            "position_rad": {"pitch": 0.4, "yaw": 0.5, "roll": 0.6},
        },
        "propeller_joint_state": {
            "timestamp_ns": 20,
            "angles_rad": [1.1, 2.1, 3.1, 4.1],
            "spin_rad_s": [12.0, 13.0, 14.0, 15.0],
        },
    }
    recorder.record_step(obs, {}, obs_after=after)
    row = json.loads((recorder.episode_dir / "steps.jsonl").read_text().strip())
    assert row["obs_before"]["gimbal_joint_state"]["position_rad"]["pitch"] == 0.2
    assert row["obs_before"]["propeller_joint_state"]["spin_rad_s"] == [12.0]
    assert row["obs_before"]["camera_pose"]["center_ned"] == [1.0, 2.0, 3.0]
    assert row["obs_after"]["gimbal_joint_state"]["position_rad"]["yaw"] == 0.5
    assert row["obs_after"]["propeller_joint_state"]["angles_rad"] == [1.1, 2.1, 3.1, 4.1]


def _valid_gimbal_recording_observation() -> dict[str, object]:
    return {
        "frame_sensor_stamp_ns": 100,
        "gimbal_joint_state": {
            "timestamp_ns": 100,
            "position_rad": {"pitch": 0.1, "yaw": -0.2, "roll": 0.3},
            "velocity_rad_s": {"pitch": 0.01, "yaw": -0.02, "roll": 0.03},
            "source": "gazebo_joint_state",
        },
        "propeller_joint_state": {
            "timestamp_ns": 100,
            "angles_rad": [0.0, 0.1, 0.2, 0.3],
            "spin_rad_s": [120.0, -121.0, 122.0, -123.0],
            "source": "gazebo_joint_state",
        },
    }


def test_gimbal_recording_contract_accepts_finite_three_axis_four_rotor_sample() -> None:
    sample = _valid_gimbal_recording_observation()
    validate_gimbal_recording_observation(sample)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda sample: sample["gimbal_joint_state"].pop("position_rad").pop("yaw"),
        lambda sample: sample["propeller_joint_state"].update({"angles_rad": [0.0] * 3}),
        lambda sample: sample["gimbal_joint_state"]["position_rad"].update({"roll": float("nan")}),
        lambda sample: sample["propeller_joint_state"].update({"spin_rad_s": [0.0] * 4}),
        lambda sample: sample["propeller_joint_state"].update({"timestamp_ns": 101}),
    ],
)
def test_gimbal_recording_contract_fails_closed_for_missing_invalid_or_misaligned_state(mutate) -> None:
    sample = _valid_gimbal_recording_observation()
    mutate(sample)
    with pytest.raises(ValueError):
        validate_gimbal_recording_observation(sample)


def test_collector_rejects_gimbal_frame_before_recorder_writes_step(tmp_path: Path) -> None:
    from scripts import collect_sim_teacher_episodes as collector

    recorder = EpisodeRecorder(tmp_path, "gimbal-rejected")
    before = _valid_gimbal_recording_observation()
    after = _valid_gimbal_recording_observation()
    after["propeller_joint_state"]["timestamp_ns"] = 101

    with pytest.raises(ValueError, match="propeller joint timestamp"):
        collector._record_policy_step(
            recorder,
            before,
            {},
            camera_mount=CameraMount.gimbal(),
            ack={},
            outcome={},
            obs_after=after,
            labels={},
        )

    assert recorder.index == 0
    assert not recorder.steps_path.exists()
    assert list(recorder.rgb_dir.iterdir()) == []
    assert list(recorder.depth_dir.iterdir()) == []


def test_collector_records_fixed_camera_without_gimbal_sidecar(tmp_path: Path) -> None:
    from scripts import collect_sim_teacher_episodes as collector
    from multidetect.sim_teacher import ActionPrimitive

    recorder = EpisodeRecorder(tmp_path, "fixed-camera")
    observation = {
        "rgb": b"fixed-camera-frame",
        "depth": np.ones((1, 1), dtype=np.float32),
        "frame_sensor_stamp_ns": 100,
    }

    assert collector._record_policy_step(
        recorder,
        observation,
        ActionPrimitive.stop(),
        camera_mount=CameraMount.fixed(),
        ack={},
        outcome={},
        obs_after=observation,
        labels={},
    ) is False

    rows = [json.loads(line) for line in recorder.steps_path.read_text().splitlines()]
    assert len(rows) == 1
    assert recorder.index == 1
    assert rows[0]["obs_before"]["rgb_path"]
    assert rows[0]["obs_before"]["depth_path"]


def test_fixed_camera_observation_does_not_require_gimbal_contract() -> None:
    sensor = SensorSample(
        rgb=b"rgb",
        width=1,
        height=1,
        depth=None,
        camera_info={"width": 1, "height": 1},
        pose={},
        velocity=[0.0, 0.0, 0.0],
        sensor_stamp_ns=100,
        received_monotonic_ns=100,
        received_wall_time=1.0,
    )
    result = build_observation(sensor, _mavlink(), camera_mount=CameraMount.fixed())
    assert result["gimbal_joint_state"]["source"] == "fixed_mount"
    assert result["propeller_joint_state"]["source"] == "unavailable"


def test_airframe_matrix_keeps_fixed_camera_optional_and_gimbal_explicit() -> None:
    fixed = json.loads((ROOT / "configs/airframes/x500_mono_cam.json").read_text())
    gimbal = json.loads((ROOT / "configs/airframes/x500_gimbal.json").read_text())
    assert "camera_mount" not in fixed
    assert fixed["camera"]["mount"] == "downward"
    assert gimbal["camera_mount"]["axes"] == ["roll", "pitch", "yaw"]
    assert set(gimbal["camera_mount"]["limits"]) == {"roll", "pitch", "yaw"}


def test_gimbal_model_publishes_three_axes_and_four_rotors() -> None:
    sdf = ET.parse(ROOT / "simulation/gazebo/model-overlays/x500_gimbal_seg/model.sdf").getroot()
    publisher = next(
        plugin for plugin in sdf.findall(".//plugin")
        if plugin.get("filename") == "gz-sim-joint-state-publisher-system"
    )
    names = [node.text for node in publisher.findall("joint_name")]
    assert names == [
        "rotor_0_joint",
        "rotor_1_joint",
        "rotor_2_joint",
        "rotor_3_joint",
        "cgo3_vertical_arm_joint",
        "cgo3_horizontal_arm_joint",
        "cgo3_camera_joint",
    ]
    gimbal_sdf = ET.parse(ROOT / "simulation/gazebo/source-assets/x500_gimbal/gimbal_model.sdf").getroot()
    commands = {
        plugin.findtext("joint_name"): plugin.findtext("sub_topic")
        for plugin in gimbal_sdf.findall(".//plugin")
        if plugin.get("filename") == "gz-sim-joint-position-controller-system"
    }
    assert commands == {
        "cgo3_horizontal_arm_joint": "command/gimbal_roll",
        "cgo3_camera_joint": "command/gimbal_pitch",
        "cgo3_vertical_arm_joint": "command/gimbal_yaw",
    }


def test_controlled_joint_input_produces_explainable_three_axis_state_delta() -> None:
    def sample(pitch: float, yaw: float, roll: float) -> dict[str, object]:
        joints = {
            f"rotor_{index}_joint": {"position_rad": float(index), "velocity_rad_s": 10.0}
            for index in range(4)
        }
        joints.update(
            {
                "cgo3_camera_joint": {"position_rad": pitch, "velocity_rad_s": 0.2},
                "cgo3_vertical_arm_joint": {"position_rad": yaw, "velocity_rad_s": 0.3},
                "cgo3_horizontal_arm_joint": {"position_rad": roll, "velocity_rad_s": 0.4},
            }
        )
        return {"stamp_ns": 100, "joints": joints}

    before = _frame_joint_state(sample(0.0, 0.0, 0.0), 100, require_complete=True)
    after = _frame_joint_state(sample(0.2, -0.3, 0.1), 100, require_complete=True)
    assert before is not None and after is not None
    assert {
        axis: after["gimbal_joint_state"]["position_rad"][axis]
        - before["gimbal_joint_state"]["position_rad"][axis]
        for axis in ("pitch", "yaw", "roll")
    } == {"pitch": 0.2, "yaw": -0.3, "roll": 0.1}


def test_esc_rpm_is_used_for_spin_but_actuator_output_is_not() -> None:
    state = MavlinkState(
        messages={
            "ACTUATOR_OUTPUT_STATUS": {"actuator": [0.7, 0.8]},
            "ESC_STATUS": {"rpm": [60.0, 120.0]},
        },
        received_monotonic_ns={},
    )
    result = _propeller_joint_state(state, 123)
    np.testing.assert_allclose(result["spin_rad_s"], [2.0 * np.pi, 4.0 * np.pi])
    assert result["angles_rad"] == []
    assert result["source"] == "ESC_STATUS.rpm"


def test_offline_renderer_records_input_propeller_spin_and_gimbal_angle() -> None:
    world = type(
        "World",
        (),
        {
            "sources": (("world.sdf", "a" * 64),),
            "relative_path": "world.sdf",
            "target_center": (0.0, 0.0, 0.0),
        },
    )()
    samples = camera_samples(
        world,
        "train",
        1,
        2,
        propeller_angle_rad=1.0,
        propeller_spin_rad_s=6.0,
        sample_period_s=0.5,
        gimbal_angle_rad=(0.1, 0.2, 0.3),
    )
    assert samples[0]["propeller_angle_rad"] == 1.0
    assert samples[1]["propeller_angle_rad"] == 4.0
    assert samples[0]["propeller_spin_rad_s"] == 6.0
    assert samples[0]["gimbal_angle_rad"] == [0.1, 0.2, 0.3]


def test_offline_renderer_replays_per_rotor_recorded_angles() -> None:
    world = type(
        "World",
        (),
        {
            "sources": (("world.sdf", "a" * 64),),
            "relative_path": "world.sdf",
            "target_center": (0.0, 0.0, 0.0),
        },
    )()
    samples = camera_samples(
        world,
        "train",
        1,
        1,
        propeller_joint_state={
            "timestamp_ns": 100,
            "joint_names": [f"rotor_{index}_joint" for index in range(4)],
            "angles_rad": [0.1, 0.2, 0.3, 0.4],
            "spin_rad_s": [10.0, -11.0, 12.0, -13.0],
        },
    )
    assert samples[0]["propeller_angles_rad"] == [0.1, 0.2, 0.3, 0.4]
    assert samples[0]["propeller_spin_rad_s_by_rotor"] == [10.0, -11.0, 12.0, -13.0]
    assert samples[0]["propeller_state_source"] == "recorded_gazebo_joint_state"


def test_renderer_applies_recorded_joint_angles_without_guessing_turn_direction() -> None:
    class Obj:
        def __init__(self, index: int) -> None:
            self.name = f"rotor_{index}"
            self.rotation_euler = type("Euler", (), {"z": None})()
            self.props = {}

        def get(self, key: str):
            return self.props.get(key)

        def __setitem__(self, key: str, value: object) -> None:
            self.props[key] = value

    objects = [Obj(index) for index in range(4)]
    assert apply_propeller_state(
        objects,
        {
            "propeller_angles_rad": [0.1, 0.2, 0.3, 0.4],
            "propeller_spin_rad_s_by_rotor": [10.0, -11.0, 12.0, -13.0],
        },
    )
    assert [obj.rotation_euler.z for obj in objects] == [0.1, 0.2, 0.3, 0.4]


def test_gimbal_sdf_axes_and_composed_pose_are_numerically_explicit() -> None:
    sdf = ET.parse(ROOT / "simulation/gazebo/source-assets/x500_gimbal/gimbal_model.sdf").getroot()
    axes = {
        joint.get("name"): tuple(float(value) for value in joint.findtext("axis/xyz").split())
        for joint in sdf.findall(".//joint")
    }
    # x500_gimbal_model.sdf attaches the gimbal with a fixed pi yaw. Rotate
    # the raw joint axes into the vehicle frame before comparing conventions.
    fixed = rpy_rotation(roll_rad=0.0, pitch_rad=0.0, yaw_rad=math.pi)
    def apply(matrix, vector):
        return tuple(
            sum(matrix[row * 3 + col] * vector[col] for col in range(3))
            for row in range(3)
        )
    assert apply(fixed, axes["cgo3_camera_joint"]) == pytest.approx((0.0, -1.0, 0.0))
    assert apply(fixed, axes["cgo3_vertical_arm_joint"]) == pytest.approx((0.0, 0.0, -1.0))
    assert apply(fixed, axes["cgo3_horizontal_arm_joint"]) == pytest.approx((1.0, 0.0, 0.0))

    pose = compose_camera_pose(
        (0.0, 0.0, 0.0),
        vehicle_roll_rad=0.0,
        vehicle_pitch_rad=0.0,
        vehicle_yaw_rad=0.0,
        mount=CameraMount.gimbal(pitch_rad=0.2, yaw_rad=0.3, roll_rad=0.1),
    )
    expected = rpy_rotation(roll_rad=0.1, pitch_rad=-0.2, yaw_rad=0.3)
    assert pose.optical_to_world == pytest.approx(expected)


def test_offline_renderer_accepts_composed_camera_pose() -> None:
    world = type(
        "World",
        (),
        {
            "sources": (("world.sdf", "a" * 64),),
            "relative_path": "world.sdf",
            "target_center": (0.0, 0.0, 0.0),
        },
    )()
    samples = camera_samples(
        world,
        "test",
        1,
        3,
        camera_pose={
            "center_ned": [3.0, 4.0, 5.0],
            "optical_to_world": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        },
    )
    sample = samples[0]
    assert {tuple(value["location"]) for value in samples} == {(4.0, 3.0, -5.0)}
    assert sample["camera_pose_source"] == "recorded_composed_pose"
    assert sample["camera_world_matrix"][0][3:] == [4.0]
    assert sample["camera_world_matrix"][1][3:] == [3.0]
    assert sample["camera_world_matrix"][2][3:] == [-5.0]


def test_fixed_mount_projection_matches_recorded_composed_camera_pose() -> None:
    mount = CameraMount.fixed(pitch_rad=0.18, offset_body_ned_m=(0.18, 0.0, -0.12))
    pose = {
        "local_ned_m": [1.0, 2.0, -5.0],
        "attitude": {"roll_deg": 5.0, "pitch_deg": -3.0, "yaw_deg": 20.0},
    }
    from multidetect.camera_mount import compose_camera_pose

    composed = compose_camera_pose(
        tuple(pose["local_ned_m"]),
        vehicle_roll_rad=np.deg2rad(5.0),
        vehicle_pitch_rad=np.deg2rad(-3.0),
        vehicle_yaw_rad=np.deg2rad(20.0),
        mount=mount,
    )
    with_camera_pose = {
        **pose,
        "camera_pose": {
            "center_ned": list(composed.center_ned),
            "optical_to_world": list(composed.optical_to_world),
        },
    }
    legacy = _pixel_camera_ray_raw(
        400, 200, pose=pose, camera_info={"fx": 500, "fy": 500},
        image_size=(640, 480), camera_mount=mount,
    )
    recorded = _pixel_camera_ray_raw(
        400, 200, pose=with_camera_pose, camera_info={"fx": 500, "fy": 500},
        image_size=(640, 480), camera_mount=mount,
    )
    assert legacy is not None and recorded is not None
    np.testing.assert_allclose(recorded[0], legacy[0], atol=1e-12)
    np.testing.assert_allclose(recorded[1], legacy[1], atol=1e-12)
