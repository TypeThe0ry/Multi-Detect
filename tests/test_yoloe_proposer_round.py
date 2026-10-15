from __future__ import annotations

import json

import cv2
import numpy as np

from scripts.build_sim_detector_dataset import build_dataset
from scripts.build_yoloe_proposer_round import (
    compare_histograms,
    manifest_histogram,
    size_bucket,
)
from scripts.evaluate_yoloe_proposer_round import collect_frames


def test_size_buckets_match_small_object_contract() -> None:
    assert [size_bucket(value) for value in (0, 7.99, 8, 15.99, 16, 31.99, 32, 63.99, 64)] == [
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


def test_histogram_comparison_preserves_old_and_new_counts() -> None:
    old = {"backpack": {"<8": 2, "8-16": 3}}
    new = {"backpack": {"<8": 5, "8-16": 1}, "cone": {">=64": 4}}
    assert manifest_histogram({"counts": {"category_size_histogram": old}}) == {
        "backpack": {"<8": 2, "8-16": 3, "16-32": 0, "32-64": 0, ">=64": 0}
    }
    result = compare_histograms(old, new)
    assert result["backpack"]["<8"] == {"old": 2, "new": 5}
    assert result["cone"][">=64"] == {"old": 0, "new": 4}


def test_generated_v2_missing_from_wrong_index_fails_closed(tmp_path):
    campaign = tmp_path / "campaign" / "gen_v2_w0034" / "episode"
    (campaign / "rgb").mkdir(parents=True)
    (campaign / "seg").mkdir()
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    cv2.imwrite(str(campaign / "rgb" / "a.png"), image)
    cv2.imwrite(str(campaign / "seg" / "a.png"), image)
    (campaign / "steps.jsonl").write_text(
        json.dumps({"obs_before": {"rgb_path": "rgb/a.png", "segmentation_path": "seg/a.png"}})
        + "\n"
    )
    (campaign / "episode.json").write_text(json.dumps({"status": "complete"}))
    (tmp_path / "truth.json").write_text(json.dumps({"targets": []}))
    (tmp_path / "index.json").write_text(json.dumps({"worlds": []}))
    try:
        build_dataset([tmp_path / "campaign"], tmp_path / "out", index_path=tmp_path / "index.json")
    except ValueError as exc:
        assert "gen_v2_w0034" in str(exc)
    else:
        raise AssertionError("missing generated-v2 world must fail closed")


def test_evaluator_rejects_non_test_world(tmp_path):
    episode = tmp_path / "eval" / "gen_v2_w0034" / "episode"
    episode.mkdir(parents=True)
    (episode / "steps.jsonl").write_text("{}\n", encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"worlds": [{"world_id": "gen_v2_w0034", "split": "train"}]}),
        encoding="utf-8",
    )
    try:
        collect_frames([tmp_path / "eval"], index)
    except ValueError as exc:
        assert "requires test world" in str(exc)
    else:
        raise AssertionError("evaluator must reject train/val worlds")


def test_asset_holdout_uses_segmentation_slug_for_person_variants(tmp_path):
    campaign = tmp_path / "campaign"
    world = "gen_w0000"
    episode = campaign / world / "episode"
    (episode / "rgb").mkdir(parents=True)
    (episode / "seg").mkdir()
    truth = {
        "targets": [
            {
                "target_id": "person-1",
                "asset_slug": "walking_person",
                "class": "person",
                "pose": {"x_east_m": 20.0, "y_north_m": 0.0, "z_up_m": 0.75},
                "size_m": [1.0, 1.0, 2.0],
            }
        ],
        "segmentation_labels": {
            "1": {"target_id": "person-1", "asset_slug": "walking_person", "role": "target"}
        },
    }
    (campaign / f"{world}.truth.json").write_text(json.dumps(truth), encoding="utf-8")
    rgb = np.zeros((8, 8, 3), dtype=np.uint8)
    seg = np.zeros((8, 8, 3), dtype=np.uint8)
    seg[2:6, 2:6, 2] = 1
    cv2.imwrite(str(episode / "rgb" / "frame.png"), rgb)
    cv2.imwrite(str(episode / "seg" / "frame.png"), seg)
    (episode / "steps.jsonl").write_text(
        json.dumps(
            {
                "obs_before": {
                    "rgb_path": "rgb/frame.png",
                    "segmentation_path": "seg/frame.png",
                    "pose": {"local_ned_m": [0.0, 0.0, -4.0]},
                    "attitude": {"yaw_deg": 90.0},
                    "camera_info": {"fx": 90.0, "fy": 90.0, "cx": 4.0, "cy": 4.0},
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (episode / "episode.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"worlds": [{"world_id": world, "split": "train"}]}), encoding="utf-8"
    )

    report = build_dataset(
        [campaign], tmp_path / "dataset", index_path=index, label_source="segmentation",
        min_box_px=1, min_seg_area_px=1, holdout_assets={"walking_person"}
    )
    assert report["counts"]["frames"] == 0
    assert report["heldout_asset_images"]["train"]["walking_person"] == 1
    assert report["remaining_asset_instances"]["train"]["walking_person"] == 0
