import json

import numpy as np
from PIL import Image

from scripts import analyze_small_object_proposals as analysis
from scripts.analyze_small_object_proposals import (
    evaluate_frames,
    load_caches,
    segmentation_objects,
    size_bucket,
)


def test_size_bucket_boundaries():
    assert [size_bucket(x) for x in (1, 7, 8, 15, 16, 31, 32, 63, 64)] == [
        "<8",
        "<8",
        "8-16",
        "8-16",
        "16-32",
        "16-32",
        "32-64",
        "32-64",
        ">=64",
    ]


def test_segmentation_retains_one_pixel_and_separates_scenery():
    labels = np.zeros((8, 8, 3), dtype=np.uint8)
    labels[0, 0] = [1, 0, 1]
    labels[2:5, 2:5] = [2, 0, 2]
    truth = {
        "targets": [{"target_id": "cone", "class": "traffic_cone"}],
        "segmentation_labels": {
            "1": {"target_id": "cone", "role": "distractor"},
            "2": {"target_id": "house", "role": "obstacle", "asset_slug": "house"},
        },
    }
    objects, scene = segmentation_objects(labels, truth)
    assert len(objects) == 1 and len(scene) == 2
    assert objects[0]["box"] == (0, 0, 1, 1)
    assert objects[0]["short_side_px"] == 1


def test_recall_independent_matches_and_false_definitions():
    objects = [
        {"category": "stop_sign", "box": [0, 0, 4, 4], "size_bucket": "<8"},
        {"category": "backpack", "box": [0, 0, 4, 4], "size_bucket": "<8"},
    ]
    frame = {
        "image": "frame",
        "image_key": "frame",
        "objects": objects,
        "scene": objects + [{"box": [10, 10, 20, 20]}],
        "altitude_bucket": "16-20",
        "cached_boxes": [[0, 0, 4, 4, 0.5], [10, 10, 20, 20, 0.8], [30, 30, 40, 40, 0.4]],
    }
    metrics = evaluate_frames([frame])
    assert metrics["tables"]["overall"][0]["recalled_iou_0.3"] == 2
    assert metrics["false_catalogued_per_frame"] == 2
    assert metrics["false_scene_per_frame"] == 1
    changed = evaluate_frames([frame], {"frame": [[0, 0, 7, 7, 0.9]]})
    assert changed["tables"]["overall"][0]["recall_iou_0.3"] == 1
    assert changed["tables"]["overall"][0]["recall_iou_0.5"] == 0


def test_cache_deduplication_and_conflict_policy(tmp_path):
    first = {"image": "rgb/frame.png", "boxes": [[1, 2, 3, 4, 0.5]]}
    other = {**first, "boxes": []}
    (tmp_path / "a.jsonl").write_text(json.dumps(first) + "\n" + json.dumps(first) + "\n")
    (tmp_path / "b.jsonl").write_text(json.dumps(other) + "\n")
    cached, stats = load_caches(tmp_path)
    assert cached[first["image"]] == first["boxes"]
    assert stats["identical_duplicate_rows"] == 1
    assert len(stats["conflicts"]) == 1


def test_episode_join_deduplicates_before_after_and_preserves_tiny_truth(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "ROOT", tmp_path)
    episode = tmp_path / "data/gen_fixture/episodes/episode-0"
    episode.mkdir(parents=True)
    (episode / "rgb").mkdir()
    (episode / "seg").mkdir()
    Image.new("RGB", (8, 8)).save(episode / "rgb/frame.png")
    labels = np.zeros((8, 8, 3), dtype=np.uint8)
    labels[2, 3] = [1, 0, 1]
    Image.fromarray(labels).save(episode / "seg/frame.png")
    truth_dir = tmp_path / "configs/scenarios/generated-v2"
    truth_dir.mkdir(parents=True)
    (truth_dir / "gen_fixture.truth.json").write_text(
        json.dumps(
            {
                "targets": [{"target_id": "tiny", "class": "stop_sign"}],
                "segmentation_labels": {"1": {"target_id": "tiny", "role": "target"}},
            }
        )
    )
    obs = {
        "rgb_path": "rgb\\frame.png",
        "segmentation_path": "seg/frame.png",
        "pose": {"local_ned_m": [0, 0, -19]},
        "sim_skew_ms": 300,
    }
    (episode / "steps.jsonl").write_text(json.dumps({"obs_before": obs, "obs_after": obs}))
    key = "data/gen_fixture/episodes/episode-0/rgb/frame.png"
    frames, stats = analysis.load_episode((str(episode), [(key, [])], str(tmp_path)))
    assert stats == {"included_frames": 1, "included_frames_pose_skew_over_250ms": 1}
    assert len(frames) == 1
    assert frames[0]["altitude_m"] == 19
    assert frames[0]["objects"][0]["box"] == (3, 2, 4, 3)
