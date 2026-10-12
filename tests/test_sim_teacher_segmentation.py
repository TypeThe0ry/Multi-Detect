from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from multidetect.sim_teacher.recording import EpisodeRecorder
from multidetect.sim_teacher.segmentation import boxes_from_segmentation, load_labels_map
from scripts import build_sim_detector_dataset as detector
from scripts import build_slow_system_sft as sft


def _truth():
    return {
        "targets": [
            {
                "target_id": "target",
                "class": "vehicle",
                "pose": {"x_east_m": 0, "y_north_m": 10, "z_up_m": 0},
                "size_m": [1, 1, 1],
            }
        ],
        "segmentation_labels": {"7": {"target_id": "target", "asset_slug": "car"}},
    }


def _labels():
    labels = np.zeros((40, 50, 3), dtype=np.uint8)
    labels[10:22, 15:30] = [9, 1, 7]
    labels[25:29, 32:36] = [10, 1, 7]
    labels[1, 1] = [9, 1, 7]  # Noise must not enlarge the object box.
    return labels


def test_panoptic_visible_components_and_raw_rgb_roundtrip(tmp_path):
    labels = _labels()
    path = tmp_path / "labels.png"
    Image.fromarray(labels).save(path)
    assert np.array_equal(load_labels_map(path), labels)
    result = boxes_from_segmentation(labels, _truth(), min_area_px=4)
    assert result["target"]["box"] == (15, 10, 36, 29)
    assert result["target"]["visible_pixels"] == 196
    assert result["target"]["instance_ids"] == [265, 266]
    assert boxes_from_segmentation(labels, _truth(), min_box_px=30) == {}
    with pytest.raises(ValueError, match="no segmentation_labels"):
        boxes_from_segmentation(labels, {})


def test_recorder_saves_optional_segmentation_losslessly(tmp_path):
    recorder = EpisodeRecorder(tmp_path, "episode")
    labels = _labels()
    recorder.record_step(
        {
            "segmentation": labels,
            "camera_info": {"width": 50, "height": 40},
            "segmentation_info": {"encoding": "PANOPTIC_RGB8", "source_stamp_ns": 1},
        },
        {},
    )
    row = json.loads(recorder.steps_path.read_text())
    obs = row["obs_before"]
    assert "segmentation" not in obs
    assert obs["segmentation_info"]["source_stamp_ns"] == 1
    assert np.array_equal(load_labels_map(recorder.episode_dir / obs["segmentation_path"]), labels)


def _episode(tmp_path):
    world = "gen_seg_test"
    campaign = tmp_path / "campaign"
    episode = campaign / world / "episode"
    episode.mkdir(parents=True)
    (episode / "episode.json").write_text(
        json.dumps({"status": "complete", "task_text": "Find red_vehicle."})
    )
    Image.new("RGB", (50, 40)).save(episode / "rgb.png")
    Image.fromarray(_labels()).save(episode / "seg.png")
    obs = {
        "rgb_path": "rgb.png",
        "segmentation_path": "seg.png",
        "pose": {"local_ned_m": [9999, 9999, 9999]},
        "camera_info": {},
    }
    (episode / "steps.jsonl").write_text(json.dumps({"obs_before": obs}) + "\n")
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(json.dumps(_truth()))
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": world, "split": "train"}]}))
    return campaign, episode, truth_path, index, world


def test_detector_segmentation_does_not_project_bad_pose(tmp_path, monkeypatch):
    campaign, _, truth_path, index, _ = _episode(tmp_path)
    monkeypatch.setattr(detector, "_find_truth", lambda *args: truth_path)
    monkeypatch.setattr(detector, "_find_sdf", lambda *args: None)
    monkeypatch.setattr(
        detector, "project_target_bbox", lambda **kwargs: pytest.fail("projection called")
    )
    monkeypatch.setattr(
        detector, "visible_fraction", lambda **kwargs: pytest.fail("occlusion projection called")
    )
    report = detector.build_dataset(
        [campaign], tmp_path / "out", index_path=index, label_source="segmentation"
    )
    assert report["counts"]["frames"] == 1
    assert report["frames"][0]["objects"][0]["bbox_xyxy"] == [15, 10, 36, 29]
    assert report["frames"][0]["objects"][0]["visible_fraction"] is None


def test_sft_candidates_prefer_segmentation(tmp_path, monkeypatch):
    campaign, _, truth_path, _, world = _episode(tmp_path)
    root = tmp_path / "repo"
    directory = root / "configs/scenarios/generated"
    directory.mkdir(parents=True)
    (directory / f"{world}.truth.json").write_text(truth_path.read_text())
    monkeypatch.setattr(sft, "ROOT", root)
    monkeypatch.setattr(sft, "_relative", lambda path: str(path))
    monkeypatch.setattr(sft, "load_sdf_obstacles", lambda *args: ())
    monkeypatch.setattr(sft, "_project", lambda *args, **kwargs: pytest.fail("projection called"))
    rows = list(sft._eligible_frames([campaign], {world: "train"}, {}, seed=1))
    assert len(rows) == 1
    assert rows[0][6][0]["box"] == (15, 10, 36, 29)
    assert rows[0][6][0]["label_source"] == "segmentation"
