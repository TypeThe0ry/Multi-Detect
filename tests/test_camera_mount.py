from __future__ import annotations

import math

import pytest

from multidetect.camera_mount import CameraMount
from multidetect.simulator.camera_projection import (
    pixel_camera_ray,
    pixel_ground_point,
    project_target_bbox,
)


def _pose(*, roll: float = 2.0, pitch: float = -4.0, yaw: float = 35.0) -> dict[str, object]:
    return {
        "local_ned_m": [3.0, -2.0, -22.0],
        "attitude": {"roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw},
    }


def _camera() -> dict[str, float]:
    return {"width": 1280, "height": 960, "fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0}


def test_fixed_mount_composition_matches_legacy_projection() -> None:
    pose = _pose()
    camera = _camera()
    mount = CameraMount.fixed(pitch_rad=0.18, offset_body_ned_m=(0.18, 0.0, -0.12))
    legacy_ray = pixel_camera_ray(
        650.0, 510.0, pose=pose, camera_info=camera, image_size=(1280, 960), camera_pitch_rad=0.18
    )
    composed_ray = pixel_camera_ray(
        650.0, 510.0, pose=pose, camera_info=camera, image_size=(1280, 960), camera_mount=mount
    )
    assert legacy_ray is not None and composed_ray is not None
    for actual, expected in zip(composed_ray[0] + composed_ray[1], legacy_ray[0] + legacy_ray[1]):
        assert actual == pytest.approx(expected, abs=1e-12)

    legacy_box = project_target_bbox(
        target_ned=(40.0, 30.0, 0.0), pose=pose, camera_info=camera,
        image_size=(1280, 960), camera_pitch_rad=0.18,
    )
    composed_box = project_target_bbox(
        target_ned=(40.0, 30.0, 0.0), pose=pose, camera_info=camera,
        image_size=(1280, 960), camera_mount=mount,
    )
    assert composed_box == legacy_box


def test_lock_horizon_gimbal_stays_level_at_tailsitter_pitch() -> None:
    pose = _pose(roll=0.0, pitch=90.0, yaw=27.0)
    camera = _camera()
    mount = CameraMount.gimbal(stabilization="lock-horizon")
    result = pixel_camera_ray(
        640.0, 480.0, pose=pose, camera_info=camera, image_size=(1280, 960), camera_mount=mount
    )
    assert result is not None
    _, ray = result
    assert ray == pytest.approx((1.0, 0.0, 0.0), abs=1e-12)


def test_lock_horizon_ground_intersection_uses_composed_camera_pose() -> None:
    pose = {"local_ned_m": [0.0, 0.0, -10.0], "attitude": {"pitch_deg": 90.0}}
    camera = _camera()
    mount = CameraMount.gimbal(stabilization="lock-horizon")
    # A level forward ray has no ground intersection; a pixel below the axis does.
    point = pixel_ground_point(
        640.0, 640.0, pose=pose, camera_info=camera, image_size=(1280, 960), camera_mount=mount
    )
    assert point is not None
    assert math.isfinite(point[0]) and math.isfinite(point[1])


def test_recorded_composed_camera_pose_drives_projection_without_mount_argument() -> None:
    pose = {
        **_pose(),
        "camera_pose": {
            "center_ned": [0.0, 0.0, -10.0],
            "optical_to_world": [0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        },
    }
    result = pixel_camera_ray(
        640.0, 480.0, pose=pose, camera_info=_camera(), image_size=(1280, 960)
    )
    assert result is not None
    center, ray = result
    assert center == pytest.approx((0.0, 0.0, -10.0))
    assert ray == pytest.approx((0.0, 1.0, 0.0))


def test_recorded_gimbal_state_drives_projection_when_camera_pose_is_absent() -> None:
    pose = {
        **_pose(roll=0.0, pitch=0.0, yaw=0.0),
        "gimbal_state": {
            "pitch_rad": 0.0,
            "yaw_rad": math.pi / 2.0,
            "roll_rad": 0.0,
            "yaw_frame": "body",
        },
    }
    result = pixel_camera_ray(
        640.0, 480.0, pose=pose, camera_info=_camera(), image_size=(1280, 960)
    )
    assert result is not None
    _, ray = result
    assert ray == pytest.approx((0.0, 1.0, 0.0), abs=1e-12)


def test_stale_recorded_gimbal_feedback_does_not_project_a_count_position() -> None:
    # Recorded joint2 step 105: camera_pose was omitted because the feedback
    # was 1.3146 s old. A fresh Gazebo joint sidecar is metadata, not a frame
    # camera pose; composing the stale MAVLink angles made a usable-looking hit.
    pose = {
        "pose": {"local_ned_m": [-6.805428218841553, 30.794056701660157, -8.008367156982422]},
        "attitude": {
            "roll_deg": 0.0015934283551134291,
            "pitch_deg": 0.7036840278611189,
            "yaw_deg": -167.5563647639039,
        },
        "gimbal_state": {
            "pitch_rad": -0.017267586932486898,
            "yaw_rad": -2.9248098863305474,
            "roll_rad": -0.0024389902679352207,
            "timestamp_us": 343200000,
            "yaw_frame": "unknown",
            "age_s": 1.314569954,
            "fresh": False,
            "source": "GIMBAL_DEVICE_ATTITUDE_STATUS",
        },
        "gimbal_joint_state": {
            "timestamp_ns": 343336000000,
            "position_rad": {
                "pitch": 3.742145675907657e-05,
                "roll": 1.018644536386115e-05,
                "yaw": -5.50382506293972e-05,
            },
            "source": "gazebo_joint_state",
            "fresh": True,
        },
    }
    camera = {
        "width": 1280,
        "height": 960,
        "fx": 539.93636802453,
        "fy": 539.93636802453,
        "cx": 640.0,
        "cy": 480.0,
    }
    args = {"pose": pose, "camera_info": camera, "image_size": (1280, 960)}

    assert pixel_camera_ray(599.7, 784.7, **args) is None
    assert pixel_ground_point(599.7, 784.7, **args) is None


def test_recorded_joint_state_positions_drive_projection_without_camera_pose() -> None:
    vehicle_pose = _pose()
    pose = {
        "pose": {"local_ned_m": vehicle_pose["local_ned_m"]},
        "attitude": vehicle_pose["attitude"],
        "gimbal_joint_state": {
            "timestamp_ns": 1_000_000,
            "position_rad": {"pitch": 0.6, "yaw": 0.8, "roll": 0.1},
            "velocity_rad_s": {},
            "source": "GIMBAL_DEVICE_ATTITUDE_STATUS",
            "fresh": True,
        },
    }
    actual = pixel_camera_ray(
        650.0, 510.0, pose=pose, camera_info=_camera(), image_size=(1280, 960)
    )
    expected = pixel_camera_ray(
        650.0, 510.0, pose=vehicle_pose, camera_info=_camera(), image_size=(1280, 960),
        camera_mount=CameraMount.gimbal(pitch_rad=0.6, yaw_rad=0.8, roll_rad=0.1),
    )
    assert actual is not None and expected is not None
    assert actual[0] == pytest.approx(expected[0], abs=1e-12)
    assert actual[1] == pytest.approx(expected[1], abs=1e-12)


@pytest.mark.parametrize("source", ["fixed_mount", "unavailable"])
def test_unrecorded_joint_state_keeps_legacy_projection(source: str) -> None:
    pose = {
        **_pose(roll=2.0, pitch=-4.0, yaw=35.0),
        "gimbal_joint_state": {
            "position_rad": {"pitch": 1.2, "yaw": 0.8, "roll": 0.1},
            "source": source,
        },
    }
    legacy = pixel_camera_ray(
        650.0, 510.0, pose=_pose(roll=2.0, pitch=-4.0, yaw=35.0), camera_info=_camera(), image_size=(1280, 960)
    )
    actual = pixel_camera_ray(650.0, 510.0, pose=pose, camera_info=_camera(), image_size=(1280, 960))
    assert actual is not None and legacy is not None
    for actual_value, legacy_value in zip(actual[0] + actual[1], legacy[0] + legacy[1]):
        assert actual_value == pytest.approx(legacy_value)
