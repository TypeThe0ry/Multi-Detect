from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from scripts import train_sim_detector
from scripts.build_sim_detector_dataset import build_dataset


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8", newline="\n")


def _make_world(campaign: Path, world: str, *, count: int, seed: int) -> None:
    _write_json(
        campaign / f"{world}.truth.json",
        {
            "targets": [
                {
                    "target_id": f"{world}_target",
                    "class": "vehicle",
                    "pose": {"x_east_m": 20.0, "y_north_m": 0.0, "z_up_m": 0.75},
                },
                {
                    "target_id": f"{world}_distractor",
                    "class": "rover",
                    "pose": {"x_east_m": 22.0, "y_north_m": 3.0, "z_up_m": 0.75},
                },
            ]
        },
    )
    episode = campaign / world / "20261012" / "episodes" / "episode-0000"
    rgb_dir = episode / "rgb"
    rgb_dir.mkdir(parents=True)
    rows = []
    for step_index in range(count):
        observations = {}
        for side_index, side in enumerate(("before", "after")):
            # Each input has distinct pixels so the perceptual deduper does not
            # erase the synthetic positive and negative population.
            image = np.random.default_rng(seed + step_index * 2 + side_index).integers(
                0, 256, (96, 128, 3), dtype=np.uint8
            )
            name = f"{step_index:06d}_{side}.png"
            assert cv2.imwrite(str(rgb_dir / name), image)
            observations[f"obs_{side}"] = {
                "rgb_path": f"rgb/{name}",
                "pose": {
                    "local_ned_m": [0.0, 0.0, -4.0],
                    "attitude": {"yaw_deg": 270.0 if step_index == count - 1 else 90.0},
                },
                "attitude": {"yaw_deg": 270.0 if step_index == count - 1 else 90.0},
                "camera_info": {"fx": 90.0, "fy": 90.0, "cx": 64.0, "cy": 48.0},
            }
        rows.append({"step_index": step_index, **observations})
    (episode / "steps.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n"
    )
    _write_json(episode / "episode.json", {"status": "complete"})


def test_builder_keeps_worlds_disjoint_and_writes_yolo_labels(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    worlds = {"gen_w0000": "train", "gen_w0001": "val", "gen_w0002": "test"}
    for index, world in enumerate(worlds):
        _make_world(campaign, world, count=5, seed=100 + 100 * index)
    index_path = tmp_path / "index.json"
    _write_json(index_path, {"worlds": [{"world_id": k, "split": v} for k, v in worlds.items()]})

    output = tmp_path / "dataset"
    report = build_dataset(
        [campaign], output, index_path=index_path, negative_ratio=0.2, seed=7, preview=2
    )
    saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert saved == report
    assert saved["index_sha256"] == hashlib.sha256(index_path.read_bytes()).hexdigest()
    assert saved["counts"]["frames"] == len(saved["frames"])
    assert set(saved["counts"]["classes"]) >= {"vehicle", "rover"}
    assert len(list((output / "preview").glob("*.png"))) == 2
    assert (output / "data.yaml").is_file()
    assert b"\r\n" not in (output / "data.yaml").read_bytes()

    for world, split in worlds.items():
        frames = [frame for frame in saved["frames"] if frame["world_id"] == world]
        assert frames and {frame["split"] for frame in frames} == {split}
        split_counts = saved["counts"]["splits"][split]
        assert split_counts["frames"] == len(frames)
        assert split_counts["positives"] > 0
        assert split_counts["negatives"] > 0
        fraction = split_counts["negatives"] / split_counts["frames"]
        assert 0.1 <= fraction <= 0.25
        labels = list((output / "labels" / split).glob("*.txt"))
        images = list((output / "images" / split).glob("*.png"))
        assert len(labels) == len(images) == len(frames)
        assert any(not label.read_text(encoding="utf-8") for label in labels)
        assert any(label.read_text(encoding="utf-8") for label in labels)
        for label in labels:
            for line in label.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                assert len(fields) == 5
                assert int(fields[0]) in (0, 1)
                assert all(0.0 < float(value) < 1.0 for value in fields[1:])


def test_trainer_cli_passes_arguments_and_quarantines_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    data = dataset / "data.yaml"
    data.write_text(
        "path: .\ntrain: images/train\nval: images/val\nnames:\n"
        "  0: vehicle\n  1: rover\n",
        encoding="utf-8",
        newline="\n",
    )
    dataset_manifest = dataset / "manifest.json"
    _write_json(dataset_manifest, {"counts": {"frames": 9}})
    source = tmp_path / "yolo26n.pt"
    source.write_bytes(b"tiny synthetic checkpoint")
    best = tmp_path / "run" / "weights" / "best.pt"
    best.parent.mkdir(parents=True)
    best.write_bytes(b"synthetic trained checkpoint")
    onnx = best.with_suffix(".onnx")
    onnx.write_bytes(b"synthetic onnx")
    seen = {}

    def fake_train(args):
        seen.update(vars(args))
        return best, onnx, {"save_dir": str(best.parent.parent)}

    monkeypatch.setattr(train_sim_detector, "_train", fake_train)
    manifest_dir = tmp_path / "model-manifests"
    assert train_sim_detector.main(
        [
            "--data", str(data), "--tag", "cpu-test", "--model", str(source),
            "--epochs", "3", "--imgsz", "640", "--batch", "4", "--device", "0",
            "--seed", "123", "--workers", "0", "--project", str(tmp_path / "models"),
            "--manifest-output-dir", str(manifest_dir), "--allow-source-hash-mismatch",
        ]
    ) == 0
    assert (seen["epochs"], seen["imgsz"], seen["batch"], seen["device"]) == (3, 640, 4, "0")
    assert seen["seed"] == 123
    output = manifest_dir / "sim_yolo26n_cpu-test_candidate.json"
    candidate = json.loads(output.read_text(encoding="utf-8"))
    assert candidate["status"] == "quarantined"
    assert candidate["production_approved"] is False
    assert candidate["dataset"]["manifest_sha256"] == hashlib.sha256(
        dataset_manifest.read_bytes()
    ).hexdigest()
    assert candidate["export"]["artifact_sha256"] == hashlib.sha256(onnx.read_bytes()).hexdigest()
    assert candidate["output"]["native_export"]["nms_embedded"] is False
    assert [item["source_name"] for item in candidate["classes"]] == ["vehicle", "rover"]


def test_builder_skips_only_steps_over_sim_skew_limit(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    _make_world(campaign, "gen_w0000", count=2, seed=901)
    steps_path = campaign / "gen_w0000" / "20261012" / "episodes" / "episode-0000" / "steps.jsonl"
    rows = [json.loads(line) for line in steps_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["obs_before"]["sim_skew_ms"] = 300.0
    rows[0]["obs_after"]["sim_skew_ms"] = 300.0
    steps_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n"
    )
    index_path = tmp_path / "index.json"
    _write_json(index_path, {"worlds": [{"world_id": "gen_w0000", "split": "train"}]})

    report = build_dataset(
        [campaign], tmp_path / "dataset", index_path=index_path, negative_ratio=0.0,
        max_sim_skew_ms=250.0, skip_over_skew=True,
    )

    assert report["sim_skew"]["skipped_steps"] == 1
    assert report["counts"]["frames"] == 1
    assert all(frame["world_id"] == "gen_w0000" for frame in report["frames"])
