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
