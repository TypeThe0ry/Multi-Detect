"""Admission and smoke-training tests for the policy-v2 BC skeleton."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("torch")
pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")

from scripts.train_policy_v2_bc import (  # noqa: E402, I001
    DatasetAdmissionError,
    build_transitions,
    load_episodes,
    main,
)


_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _write_dataset(root: Path, *, sensor_status: str = "live") -> None:
    for episode_index in range(3):
        episode = root / "episodes" / f"episode-{episode_index}"
        (episode / "rgb").mkdir(parents=True)
        (episode / "depth").mkdir()
        (episode / "rgb" / "before.png").write_bytes(_PNG)
        (episode / "rgb" / "after.png").write_bytes(_PNG)
        np.save(episode / "depth" / "before.npy", np.ones((2, 2), dtype=np.float32))
        np.save(episode / "depth" / "after.npy", np.ones((2, 2), dtype=np.float32))
        (episode / "episode.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "episode_id": episode.name,
                    "sensor_status": sensor_status,
                    "world_id": f"world-{episode_index}",
                }
            ),
            encoding="utf-8",
        )
        before = {
            "captured_at_s": 1.0,
            "frame_id": f"frame-{episode_index}",
            "pose": {
                "local_ned_m": [0.0, 0.0, -1.0],
                "lat_deg": 1.0,
                "lon_deg": 2.0,
                "alt_m": 3.0,
            },
            "gps_fix": {"fix_type": 3},
            "rgb_path": "rgb/before.png",
            "depth_path": "depth/before.npy",
        }
        after = {**before, "rgb_path": "rgb/after.png", "depth_path": "depth/after.npy"}
        row = {
            "step_index": 0,
            "obs_before": before,
            "action": {
                "kind": "hold",
                "forward_m": 0.0,
                "right_m": 0.0,
                "up_m": 0.0,
                "yaw_delta_deg": 0.0,
                "seconds": 1.0,
            },
            "outcome": {"status": "success"},
            "obs_after": after,
            "labels": {"privileged_target": True},
        }
        (episode / "steps.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_training_script_runs_two_epoch_synthetic_smoke(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    output = tmp_path / "output"
    _write_dataset(dataset)
    assert main(
        [
            "--dataset-root",
            str(dataset),
            "--output-dir",
            str(output),
            "--epochs",
            "2",
            "--batch-size",
            "2",
            "--device",
            "cpu",
        ]
    ) == 0
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert len(report["metrics"]["history"]) == 2
    assert (output / "policy_v2_bc.pt").is_file()
    assert (output / "policy_v2_bc.onnx").is_file()


def test_non_live_episode_is_refused(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    _write_dataset(dataset, sensor_status="awaiting_live_gazebo_stream")
    with pytest.raises(DatasetAdmissionError, match="sensor_status=live"):
        load_episodes(dataset)


def test_training_loader_joins_before_perception_without_truth(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    _write_dataset(dataset)
    episode = dataset / "episodes" / "episode-0"
    perception = {
        "step_index": 0,
        "observation": "before",
        "detections": [
            {
                "class": "car",
                "confidence": 0.9,
                "bbox": [0.1, 0.2, 0.3, 0.4],
                "bearing_deg": 5.0,
                "range_m": 12.0,
            }
        ],
    }
    (episode / "perception.jsonl").write_text(
        json.dumps(perception) + "\n", encoding="utf-8", newline="\n"
    )
    loaded = load_episodes(dataset)
    state = build_transitions([loaded[0]])[0].state
    assert state.valid_mask["tracks"] is True
    assert state.valid_mask["range"] is True
    assert state.tracks[0]["label"] == "car"
    assert "privileged_target" not in state.to_json()
