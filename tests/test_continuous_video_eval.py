import json
from pathlib import Path

import numpy as np

from multidetect.camera_defaults import DEFAULT_CAMERA_INFO
from multidetect.continuous_video_eval import evaluate_video_episode
from multidetect.sim_teacher.continuous_video import ContinuousVideoWriter


def _truth():
    return {
        "targets": [
            {
                "target_id": "target-1",
                "pose": {"y_north_m": 10.0, "x_east_m": 0.0, "z_up_m": 0.0},
                "size_m": [3.0, 1.8, 1.5],
            }
        ]
    }


def test_video_writer_manifest_and_byte_cap(tmp_path: Path):
    writer = ContinuousVideoWriter(tmp_path, hz=10, max_bytes=1)
    obs = {
        "rgb": np.zeros((32, 32, 3), np.uint8),
        "depth": np.ones((8, 8), np.float32),
        "camera_info": DEFAULT_CAMERA_INFO,
        "pose": {"local_ned_m": [0, 0, -5]},
        "attitude": {"yaw_deg": 0},
        "velocity": [0, 0, 0],
        "frame_sensor_stamp_ns": 1,
    }
    assert writer.write(obs, action_index=0) is False
    result = writer.close()
    assert result["truncated"] is True
    assert json.loads((tmp_path / "video" / "video.json").read_text())["frame_count"] == 0
    assert (tmp_path / "video" / "manifest.jsonl").read_text().count("truncated") == 1


def test_continuous_video_runs_temporal_estimators(tmp_path: Path):
    writer = ContinuousVideoWriter(tmp_path, hz=10, max_bytes=64 * 1024 * 1024)
    for index in range(14):
        writer.write(
            {
                "rgb": np.zeros((32, 32, 3), np.uint8),
                "depth": np.ones((8, 8), np.float32),
                "camera_info": DEFAULT_CAMERA_INFO,
                "pose": {"local_ned_m": [index * 0.4, 0.0, -5.0]},
                "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": index * 1.0},
                "velocity": [4.0, 0.0, 0.0],
                "frame_sensor_stamp_ns": index * 100_000_000,
            },
            action_index=0,
        )
    writer.close()
    report = evaluate_video_episode(
        tmp_path,
        _truth(),
        calibration_path="simulation/gazebo/calibration/gazebo-multirotor-front-camera-v1.json",
    )
    assert report["frames"] == 14
    assert report["methods"]["rgb_slam"]["valid_output_rate"] > 0
    assert report["methods"]["visual_inertial"]["valid_output_rate"] > 0
    assert report["buckets"]


def test_video_sampling_takes_new_frames_that_lag_the_pose():
    """The image always lags the newest pose; the old "image stamp >= pose stamp"
    rule wrote no in-action frame in a real 10 Hz run (only action-end frames)."""
    from types import SimpleNamespace

    from scripts.collect_sim_teacher_episodes import _new_video_sample

    assert not _new_video_sample(None, -1)
    assert _new_video_sample(SimpleNamespace(sensor_stamp_ns=1_000), -1)
    assert not _new_video_sample(SimpleNamespace(sensor_stamp_ns=1_000), 1_000)
    assert _new_video_sample(SimpleNamespace(sensor_stamp_ns=1_100), 1_000)
