from __future__ import annotations

import json
import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from multidetect.simulator.camera_projection import (
    CAMERA_MOUNT_PITCH_RAD,
    _body_to_camera_optical,
    project_target_bbox,
)
from scripts.autolabel_sim_teacher_frames import _target_pose


def _pose(*, roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0) -> dict[str, object]:
    return {
        "local_ned_m": [0.0, 0.0, -4.0],
        "attitude": {"roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw},
    }


def test_mount_translation_and_downward_pitch_use_image_z_down() -> None:
    x, y, z = _body_to_camera_optical((10.0, 2.0, 1.0), mount_pitch_rad=CAMERA_MOUNT_PITCH_RAD)
    expected_x = math.cos(0.18) * (10.0 - 0.18) + math.sin(0.18) * (1.0 + 0.12)
    expected_z = -math.sin(0.18) * (10.0 - 0.18) + math.cos(0.18) * (1.0 + 0.12)
    assert (x, y, z) == (expected_x, 2.0, expected_z)
    assert z < 0.0  # a level forward ray is above the optical center when pitched down


def test_vehicle_attitude_changes_projection() -> None:
    camera = {"width": 640, "height": 480, "fx": 270.0, "fy": 270.0, "cx": 320.0, "cy": 240.0}
    level = project_target_bbox(
        target_ned=(20.0, 0.0, 0.75), pose=_pose(), camera_info=camera, image_size=(640, 480)
    )
    pitched = project_target_bbox(
        target_ned=(20.0, 0.0, 0.75),
        pose=_pose(pitch=8.0),
        camera_info=camera,
        image_size=(640, 480),
    )
    rolled = project_target_bbox(
        target_ned=(20.0, 0.0, 0.75),
        pose=_pose(roll=8.0),
        camera_info=camera,
        image_size=(640, 480),
    )
    assert level and pitched and rolled
    assert pitched[1] != level[1]
    assert rolled[0] != level[0] or rolled[2] != level[2]


def test_world_relative_truth_altitude_is_not_subtracted_from_origin() -> None:
    truth = {
        "targets": [
            {
                "pose": {"x_east_m": 2.0, "y_north_m": 3.0, "z_up_m": 0.75},
                "position": {
                    "latitude_deg": 1.0,
                    "longitude_deg": 2.0,
                    "altitude_m": 0.75,
                    "altitude_reference": "world",
                },
            }
        ]
    }
    assert _target_pose(truth, origin=(1.0, 2.0, 18.0)) == (3.0, 2.0, 0.75)


def test_intrinsics_are_scaled_when_metadata_size_differs() -> None:
    camera = {"width": 320, "height": 240, "fx": 100.0, "fy": 100.0, "cx": 160.0, "cy": 120.0}
    scaled = project_target_bbox(
        target_ned=(20.0, 2.0, 0.75), pose=_pose(), camera_info=camera, image_size=(640, 480)
    )
    direct = project_target_bbox(
        target_ned=(20.0, 2.0, 0.75),
        pose=_pose(),
        camera_info={
            "width": 640,
            "height": 480,
            "fx": 200.0,
            "fy": 200.0,
            "cx": 320.0,
            "cy": 240.0,
        },
        image_size=(640, 480),
    )
    assert scaled and direct
    assert scaled == direct


def test_asset_bottom_offset_projects_model_origin_as_grounded_object() -> None:
    camera = {"width": 640, "height": 480, "fx": 270.0, "fy": 270.0, "cx": 320.0, "cy": 240.0}
    centred = project_target_bbox(
        target_ned=(20.0, 0.0, 1.0), pose=_pose(), camera_info=camera,
        image_size=(640, 480), target_size_m=(2.0, 1.0, 2.0),
    )
    grounded = project_target_bbox(
        target_ned=(20.0, 0.0, 0.0), pose=_pose(), camera_info=camera,
        image_size=(640, 480), target_size_m=(2.0, 1.0, 2.0),
        target_bottom_offset_m=0.0,
    )
    assert centred == grounded


def test_real_recorded_red_target_projection_tracks_pixels() -> None:
    """Regression guard against the former 30 px vertical projection error."""
    root = Path("artifacts/datasets/sim-teacher/campaign-20261011-r2")
    episode = next(root.glob("gen_w0000/*/episodes/episode-0000-2acff440"), None)
    if episode is None:
        pytest.skip("recorded campaign-20261011-r2 frames are not present (gitignored artifacts)")
    truth = json.loads(
        Path("configs/scenarios/generated/gen_w0000.truth.json").read_text(encoding="utf-8")
    )["targets"][0]["pose"]
    steps = (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
    centers: list[tuple[float, float]] = []
    for line in steps[:3]:
        observation = json.loads(line)["obs_before"]
        image = cv2.imread(str(episode / observation["rgb_path"].replace("\\", "/")))
        assert image is not None
        height, width = image.shape[:2]
        box = project_target_bbox(
            target_ned=(truth["y_north_m"], truth["x_east_m"], truth["z_up_m"]),
            pose=observation,
            camera_info=observation["camera_info"],
            image_size=(width, height),
            target_size_m=(3.0, 1.8, 1.5),
            target_yaw_rad=truth["yaw_rad"],
        )
        assert box is not None
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        red = (((hsv[:, :, 0] <= 12) | (hsv[:, :, 0] >= 170)) & (hsv[:, :, 1] > 100)).astype(
            np.uint8
        )
        count, labels, stats, _ = cv2.connectedComponentsWithStats(red, 8)
        components = [
            stats[index]
            for index in range(1, count)
            if stats[index, cv2.CC_STAT_AREA] >= 8
        ]
        assert components
        projected_center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        nearest = min(
            components,
            key=lambda stat: math.hypot(
                projected_center[0] - (stat[cv2.CC_STAT_LEFT] + stat[cv2.CC_STAT_WIDTH] / 2.0),
                projected_center[1] - (stat[cv2.CC_STAT_TOP] + stat[cv2.CC_STAT_HEIGHT] / 2.0),
            ),
        )
        centers.append(
            (
                nearest[cv2.CC_STAT_LEFT] + nearest[cv2.CC_STAT_WIDTH] / 2.0,
                nearest[cv2.CC_STAT_TOP] + nearest[cv2.CC_STAT_HEIGHT] / 2.0,
            )
        )
        assert math.dist(projected_center, centers[-1]) < 12.0
    assert len(centers) == 3


def test_pixel_ground_point_inverts_projection_with_attitude() -> None:
    from multidetect.simulator.camera_projection import pixel_ground_point

    camera = {"width": 1280, "height": 960, "fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0}
    pose = {
        "local_ned_m": [3.0, -2.0, -22.0],
        "attitude": {"roll_deg": 2.0, "pitch_deg": -4.0, "yaw_deg": 35.0},
    }
    for north, east in ((40.0, 30.0), (25.0, 10.0)):
        box = project_target_bbox(
            target_ned=(north, east, 0.0),
            pose=pose,
            camera_info=camera,
            image_size=(1280, 960),
            target_size_m=(0.05, 0.05, 0.05),
        )
        assert box is not None
        estimate = pixel_ground_point(
            (box[0] + box[2]) / 2, (box[1] + box[3]) / 2,
            pose=pose, camera_info=camera, image_size=(1280, 960),
        )
        assert estimate is not None
        assert math.dist(estimate, (north, east)) < 0.2
