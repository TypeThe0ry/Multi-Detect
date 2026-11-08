import math

import pytest

from scripts.evaluate_onboard_ranging_reuse import iou, ray, stats, triangulate


def test_stats_empty_is_explicit():
    result = stats([])
    assert result["count"] == 0
    assert result["p95_m"] is None


def test_stats_and_iou_are_deterministic():
    result = stats([1, 2, 3, 4])
    assert result["count"] == 4
    assert result["median_m"] == 2.5
    assert math.isclose(result["rmse_m"], math.sqrt(7.5))
    assert iou([0, 0, 2, 2], [1, 1, 3, 3]) == 1 / 7


def test_triangulate_reconstructs_point_from_three_rays():
    point = (4.0, 1.5, 2.0)
    cameras = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    samples = []
    for camera in cameras:
        ray = tuple(point[i] - camera[i] for i in range(3))
        norm = math.sqrt(sum(value * value for value in ray))
        samples.append((camera, tuple(value / norm for value in ray)))
    estimate = triangulate(samples)
    assert estimate is not None
    assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(estimate, point, strict=False))


def test_triangulate_rejects_degenerate_single_direction():
    assert triangulate([((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))] * 3) is None


def test_ray_uses_recorded_joint_state_positions():
    observation = {
        "pose": {"local_ned_m": [3.0, -2.0, -22.0]},
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
        "camera_info": {"width": 1280, "height": 960, "fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
        "gimbal_joint_state": {
            "timestamp_ns": 1_000_000,
            "position_rad": {"pitch": 0.3, "yaw": math.pi / 2.0, "roll": 0.0},
            "velocity_rad_s": {},
            "source": "GIMBAL_DEVICE_ATTITUDE_STATUS",
            "fresh": True,
        },
    }
    assert ray(observation, 640.0, 480.0) == pytest.approx(
        (0.0, math.cos(0.3), math.sin(0.3)), abs=1e-12
    )


@pytest.mark.parametrize("source", ["fixed_mount", "unavailable"])
def test_ray_without_recorded_gimbal_keeps_legacy_calibration(source):
    observation = {
        "pose": {"local_ned_m": [3.0, -2.0, -22.0]},
        "attitude": {"roll_deg": 2.0, "pitch_deg": -4.0, "yaw_deg": 35.0},
        "camera_info": {"width": 1280, "height": 960, "fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
    }
    expected = ray(observation, 650.0, 510.0)
    observation["gimbal_joint_state"] = {
        "timestamp_ns": 1_000_000,
        "position_rad": {"pitch": 1.2, "yaw": 0.8, "roll": 0.1},
        "velocity_rad_s": {},
        "source": source,
        "fresh": source == "fixed_mount",
    }
    assert ray(observation, 650.0, 510.0) == pytest.approx(expected, abs=1e-12)
