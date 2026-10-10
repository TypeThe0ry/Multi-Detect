# ruff: noqa: E501
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from multidetect.autonomy.policy_v2.perception_bridge import PerceptionBridge, robust_depth_median
from multidetect.domain import BoundingBox, Detection
from scripts.autolabel_sim_teacher_frames import autolabel_episode, project_target_bbox
from scripts.report_sim_teacher_detectability import evaluate_episode


def _observation(tmp_path: Path, name: str) -> dict[str, object]:
    rgb = tmp_path / "rgb" / f"{name}.png"
    rgb.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(rgb), np.zeros((100, 200, 3), dtype=np.uint8))
    return {
        "rgb_path": f"rgb/{name}.png",
        "camera_info": {
            "width": 200,
            "height": 100,
            "fx": 100.0,
            "fy": 100.0,
            "cx": 100.0,
            "cy": 50.0,
        },
        "local_ned_m": [0.0, 0.0, -2.0],
        "attitude": {"yaw_deg": 0.0},
    }


def test_project_target_bbox_is_clipped_and_normalizable() -> None:
    bbox = project_target_bbox(
        target_ned=(10.0, 0.0, 0.0),
        pose={"local_ned_m": [0.0, 0.0, -2.0], "attitude": {"yaw_deg": 0.0}},
        camera_info={"fx": 100.0, "fy": 100.0, "cx": 100.0, "cy": 50.0},
        image_size=(200, 100),
    )
    assert bbox is not None
    assert 0.0 <= bbox[0] < bbox[2] <= 200.0
    assert 0.0 <= bbox[1] < bbox[3] <= 100.0


def test_bridge_uses_production_detection_shape_and_robust_depth() -> None:
    class FakeDetector:
        def detect(self, image: np.ndarray) -> tuple[Detection, ...]:
            del image
            return (
                Detection("car", 0.9, BoundingBox(0.25, 0.25, 0.75, 0.75)),
            )

    depth = np.full((2, 2), 5000, dtype=np.uint16)
    depth[0, 0] = 0
    depth[1, 1] = 65535
    assert robust_depth_median(
        depth,
        (0.0, 0.0, 2.0, 2.0),
        rgb_size=(2, 2),
        depth_camera_info={
            "encoding": "UINT16_MM",
            "minimum_depth_m": 0.1,
            "maximum_depth_m": 60.0,
        },
    ) == 5.0
    result = PerceptionBridge((FakeDetector(),)).annotate_observation(
        {
            "camera_info": {"fx": 100.0, "fy": 100.0, "cx": 1.0, "cy": 1.0},
            "attitude": {"yaw_deg": 90.0},
            "depth_camera_info": {"minimum_depth_m": 0.1, "maximum_depth_m": 60.0},
        },
        np.zeros((2, 2, 3), dtype=np.uint8),
        np.full((2, 2), 5.0, dtype=np.float32),
    )
    detection = result["detections"][0]
    assert detection["class"] == "car"
    assert detection["range_m"] == 5.0
    assert detection["bearing_deg"] == 0.0
    assert detection["absolute_bearing_deg"] == 90.0


def test_autolabel_episode_writes_yolo_and_visuals(tmp_path: Path) -> None:
    episode = tmp_path / "episode"
    episode.mkdir()
    rows = []
    for index in (0, 1):
        rows.append(
            {
                "step_index": index,
                "obs_before": _observation(episode, f"{index:06d}_before"),
                "obs_after": _observation(episode, f"{index:06d}_after"),
                "labels": {"target_in_fov": True},
            }
        )
    (episode / "steps.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n"
    )
    output = tmp_path / "labels"
    visual = tmp_path / "visual"
    count = autolabel_episode(
        episode,
        {"targets": [{"pose": {"x_east_m": 0.0, "y_north_m": 10.0, "z_up_m": 0.0}}]},
        output,
        visual_directory=visual,
    )
    assert count == 4
    assert len(list(output.glob("*.txt"))) == 4
    assert all(len(path.read_text(encoding="utf-8").split()) == 5 for path in output.glob("*.txt"))
    assert len(list(visual.glob("*.png"))) == 4


def test_detectability_joins_labels_only_for_evaluation(tmp_path: Path) -> None:
    episode = tmp_path / "episode"
    episode.mkdir()
    observation = _observation(episode, "000000_after")
    step = {
        "step_index": 0,
        "task_text": "find target",
        "obs_after": observation,
        "labels": {"target_in_fov": True, "target_bearing_deg": 10.0, "target_range_m": 4.0},
    }
    (episode / "steps.jsonl").write_text(json.dumps(step) + "\n", encoding="utf-8", newline="\n")
    (episode / "episode.json").write_text(
        json.dumps({"status": "complete"}), encoding="utf-8", newline="\n"
    )
    perception = {
        "step_index": 0,
        "observation": "after",
        "detections": [
            {"detector_id": "test", "class": "car", "bearing_deg": 12.0, "range_m": 5.5}
        ],
    }
    (episode / "perception.jsonl").write_text(
        json.dumps(perception) + "\n", encoding="utf-8", newline="\n"
    )
    result = evaluate_episode(episode, dataset_root=tmp_path)
    metric = next(
        item
        for item in result["metrics"]
        if item["detector_id"] == "test" and item["detected_class"] == "__all__"
    )
    assert metric["positive_frames"] == 1
    assert metric["matched_positive_frames"] == 1
    assert metric["recall"] == 1.0
    assert metric["range_error_mae_m"] == 1.5
