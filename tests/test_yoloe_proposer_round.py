from __future__ import annotations

import json

import cv2
import numpy as np

from scripts.build_sim_detector_dataset import _split_map, build_dataset
from scripts.build_yoloe_proposer_round import (
    assert_cache_keys_match,
    compare_histograms,
    manifest_histogram,
    size_bucket,
)
from scripts.evaluate_yoloe_proposer_round import collect_frames, score
from scripts.preflight_yoloe_proposer_round import run_training_preflight


def test_cache_onboard_proposals_uses_detection_score_and_custom_weights(tmp_path, monkeypatch):
    from scripts import cache_onboard_proposals as cache

    campaign = tmp_path / "campaign"
    episode = campaign / "gen_w0000" / "episode"
    (episode / "rgb").mkdir(parents=True)
    image = np.zeros((4, 5, 3), dtype=np.uint8)
    cv2.imwrite(str(episode / "rgb" / "f.png"), image)
    (episode / "steps.jsonl").write_text(
        json.dumps({"obs_before": {"rgb_path": "rgb/f.png"}}) + "\n", encoding="utf-8"
    )

    class Detection:
        bbox = (1.04, 2.06, 3.07, 4.08)
        score = 0.87654

    class Proposer:
        def __init__(self, weights, confidence, imgsz):
            assert str(weights).endswith("new.pt")
            assert confidence == 0.2 and imgsz == 960
        def __call__(self, bgr): return [Detection()]

    class Loop:
        def __init__(self, **kwargs): pass
        def filter_detections(self, detections): return detections

    monkeypatch.setattr(cache, "YoloeProposer", Proposer)
    monkeypatch.setattr(cache, "OnboardLoop", Loop)
    out = tmp_path / "cache.jsonl"
    assert cache.main([
        "--campaign", str(campaign), "--output", str(out), "--weights", str(tmp_path / "new.pt"),
        "--imgsz", "960", "--confidence", "0.2", "--source-root", str(tmp_path),
    ]) == 0
    row = json.loads(out.read_text())
    assert row["image"] == "campaign/gen_w0000/episode/rgb/f.png"
    assert row["boxes"] == [[1.0, 2.1, 3.1, 4.1, 0.8765]]


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


def test_round_cache_key_check_rejects_different_image_sets(tmp_path):
    reference = tmp_path / "r3.jsonl"
    candidate = tmp_path / "new.jsonl"
    reference.write_text(json.dumps({"image": "a.png", "boxes": []}) + "\n")
    candidate.write_text(json.dumps({"image": "b.png", "boxes": []}) + "\n")
    try:
        assert_cache_keys_match(reference, candidate)
    except ValueError as exc:
        assert "image-key mismatch" in str(exc)
    else:
        raise AssertionError("cache key mismatch must fail closed")


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


def test_score_reports_one_to_one_extra_metric_only():
    objects = [
        {"category": "a", "box": (0, 0, 10, 10), "size_bucket": "8-16"},
        {"category": "a", "box": (0, 0, 10, 10), "size_bucket": "8-16"},
    ]
    frames = [{"image": "x", "objects": objects}]
    predictions = {"x": [((0, 0, 10, 10), 0.5)]}
    report = score(frames, predictions)
    assert "legacy_r3_recall" not in report
    assert report["one_to_one_recall"]["tables"]["overall"][0]["hit03"] == 1


def test_preflight_runs_real_trainer_and_retries_oom(tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        batch = command[command.index("--batch") + 1]
        if batch == "2":
            from subprocess import CalledProcessError
            raise CalledProcessError(1, command, stderr="CUDA out of memory")
        memory = tmp_path / "work" / "train-batch-1" / "cuda-memory.json"
        memory.parent.mkdir(parents=True)
        memory.write_text(json.dumps({
            "peak_gpu_memory_bytes": 11, "peak_gpu_reserved_bytes": 22,
            "device_total_memory_bytes": 33,
        }))
        return type("Completed", (), {"stdout": "ok"})()

    result = run_training_preflight(
        weights=tmp_path / "weights.pt", imgsz=1280, requested_batch=2,
        output_root=tmp_path / "work", run=fake_run,
    )
    assert result["batch"] == 1
    assert result["peak_gpu_memory_bytes"] == 11
    batches = [calls[i][calls[i].index("--batch") + 1] for i in range(2)]
    assert batches == ["2", "1"]
    data_yaml = (tmp_path / "work" / "tiny-dataset" / "data.yaml").read_text()
    assert "0: vehicle" in data_yaml
    assert "22: stop_sign" in data_yaml
    assert "23: suv" in data_yaml
    assert "21: postbox" in data_yaml
    assert "20: pickup" in data_yaml


def test_split_map_merges_indexes_and_rejects_conflict(tmp_path):
    first, second, conflict = (tmp_path / name for name in ("a.json", "b.json", "c.json"))
    first.write_text(json.dumps({"worlds": [{"world_id": "gen_cfs_w0000", "split": "train"}]}))
    second.write_text(json.dumps({"worlds": [{"world_id": "gen_cfs2_w0000", "split": "train"}]}))
    conflict.write_text(json.dumps({"worlds": [{"world_id": "gen_cfs_w0000", "split": "test"}]}))
    assert _split_map([first, second])["gen_cfs2_w0000"] == "train"
    try:
        _split_map([first, conflict])
    except ValueError as exc:
        assert "conflicting splits" in str(exc)
    else:
        raise AssertionError("conflicting index declarations must fail")


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
