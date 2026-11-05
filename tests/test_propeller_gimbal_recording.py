from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from multidetect.camera_mount import CameraMount
from multidetect.sim_teacher.recording import EpisodeRecorder
from multidetect.sim_teacher.streams import (
    MavlinkState,
    SensorSample,
    _propeller_joint_state,
    build_observation,
)
from multidetect.simulator.camera_projection import _pixel_camera_ray_raw
from scripts.render_onboard_sdf_blender_dataset import camera_samples


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
    recorder.record_step(obs, {})
    row = json.loads((recorder.episode_dir / "steps.jsonl").read_text().strip())
    assert row["obs_before"]["gimbal_joint_state"]["position_rad"]["pitch"] == 0.2
    assert row["obs_before"]["propeller_joint_state"]["spin_rad_s"] == [12.0]
    assert row["obs_before"]["camera_pose"]["center_ned"] == [1.0, 2.0, 3.0]


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
        gimbal_angle_rad=(0.2, -0.1, 0.3),
        camera_pose={
            "center_ned": [3.0, 4.0, 5.0],
            "optical_to_world": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        },
    )
    sample = samples[0]
    assert len({tuple(value["location"]) for value in samples}) == 1
    assert sample["location"] == [4.0, 3.0, -5.0]
    assert sample["look_at"] == [4.0, 4.0, -5.0]
    assert sample["rotation_matrix"] == [
        [1.0, 0.0, 0.0],
        [0.0, 0.0, -1.0],
        [0.0, 1.0, 0.0],
    ]


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
