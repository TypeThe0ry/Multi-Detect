from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from scripts import build_flight_identity_eval as builder


def _truth() -> dict:
    return {
        "targets": [
            {
                "class": "backpack",
                "category_words": ["backpack", "bag"],
                "label": "backpack bag",
                "pose": {"y_north_m": 10.0, "x_east_m": 5.0},
                "true_target": True,
            },
            {
                "class": "electrical_box",
                "category_words": ["electrical box", "box"],
                "label": "electrical box",
                "pose": {"y_north_m": 12.0, "x_east_m": 5.0},
                "true_target": False,
            },
        ]
    }


def _row() -> dict:
    return {
        "task_text": "Find the backpack and record its position.",
        "obs_before": {
            "rgb_path": "rgb\\frame.png",
            "camera_info": {"width": 100, "height": 100},
        },
        "labels": {
            "onboard": {
                "tracks": [
                    {"id": "t-match", "bbox": [10, 20, 30, 40], "ground_ne": [10.0, 5.0]},
                    {"id": "t-distractor", "bbox": [40, 20, 60, 40], "ground_ne": [12.0, 5.0]},
                    {"id": "t-far", "bbox": [70, 20, 90, 40], "ground_ne": [100.0, 100.0]},
                ]
            }
        },
    }


@pytest.fixture
def flight_dir():
    # This sandbox's default system temp directory is inaccessible.
    with tempfile.TemporaryDirectory(prefix=".flight-identity-test-", dir=builder.ROOT) as name:
        yield Path(name)


def _episode(directory):
    episode = directory / "episode"
    (episode / "rgb").mkdir(parents=True)
    Image.new("RGB", (100, 100), (80, 80, 80)).save(episode / "rgb" / "frame.png")
    (episode / "episode.json").write_text(
        json.dumps({"world_sdf": "gen_test.sdf"}), encoding="utf-8"
    )
    return episode


def test_truth_labels_and_focus_only_candidate(flight_dir):
    episode = _episode(flight_dir)
    row = _row()
    truth = _truth()

    match, kind = builder._row_for_call(
        episode,
        row,
        {"track_id": "t-match", "decision": "approach_track"},
        truth,
        4.0,
        "test",
        "old.gguf",
        "Find the backpack and record its position.",
    )
    assert kind == "truth_match"
    assert match is not None
    assert match["label_type"] == "truth_match"
    assert match["assistant_target"]["decision"] == "confirm_target"
    assert match["candidates"] == [
        {"track_id": "track-1", "box_xyxy_normalized": [0.1, 0.2, 0.3, 0.4]}
    ]

    distractor, kind = builder._row_for_call(
        episode,
        row,
        {"track_id": "t-distractor", "decision": "confirm_target"},
        truth,
        4.0,
        "test",
        None,
        "Find the backpack and record its position.",
    )
    assert kind == "truth_distractor"
    assert distractor["assistant_target"]["decision"] == "reject_track"
    assert distractor["truth_category"] == "electrical_box"

    unmatched, kind = builder._row_for_call(
        episode,
        row,
        {"track_id": "t-far", "decision": "reject_track"},
        truth,
        4.0,
        "test",
        None,
        "Find the backpack and record its position.",
    )
    assert kind == "unmatched"
    assert unmatched["label_type"] == "unmatched"
    assert unmatched["truth_distance_m"] > 4.0


def test_build_excludes_remembered_unmatched_and_duplicates(flight_dir):
    episode = _episode(flight_dir)
    row = _row()
    row["labels"]["onboard"]["slow_calls"] = [
        {"track_id": "t-match", "decision": "approach_track"},
        {"track_id": "t-match", "decision": "confirm_target"},
        {"track_id": "t-distractor", "decision": "confirm_target"},
        {"track_id": "t-far", "decision": "reject_track"},
        {"track_id": "t-match", "decision": "confirm_target", "remembered": True},
    ]
    (episode / "steps.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    index = flight_dir / "index.json"
    index.write_text(
        json.dumps({"worlds": [{"world_id": "gen_test", "split": "test"}]}),
        encoding="utf-8",
    )
    (flight_dir / "gen_test.truth.json").write_text(json.dumps(_truth()), encoding="utf-8")
    (flight_dir / "onboard.json").write_text(json.dumps({"gguf": "old.gguf"}), encoding="utf-8")
    out = flight_dir / "out"
    manifest = builder.build([episode], out, indexes=[index])
    assert manifest["counts"]["rows_written"] == 2
    assert manifest["counts"]["real_calls"] == 4
    assert manifest["counts"]["dedupe_skipped"] == 1
    assert manifest["counts"]["remembered_skipped"] == 1
    assert manifest["counts"]["unmatched_excluded"] == 1
    assert manifest["recorded_old_model"]["identity_accuracy"] == 0.5
    assert manifest["recorded_old_model"]["distractor_rejection_rate"] == 0.0
    from scripts.slow_system_runtime import chat_messages, load_jsonl

    rows = load_jsonl(out / "test.jsonl", split="test")
    assert rows[0]["model"] == "old.gguf"
    assert len(chat_messages(rows[0], crop_policy="candidate_zoom_v2")[0]["content"]) == 3
    assert len(load_jsonl(out / "unmatched.jsonl")) == 1
    index.write_text(
        json.dumps({"worlds": [{"world_id": "gen_test", "split": "train"}]}),
        encoding="utf-8",
    )
    assert builder.build([episode], out, indexes=[index])["counts"]["rows_written"] == 0
    assert (
        builder.build([episode], out, indexes=[index], allow_train=True)["counts"]["rows_written"]
        == 2
    )


def test_task_semantics_and_recovered_mission_queries():
    truth = _truth()
    backpack, electrical = truth["targets"]
    assert builder._truth_match(backpack, truth, "找到背包并记录位置")
    assert not builder._truth_match(electrical, truth, "backpack")
    assert not builder._truth_match(backpack, truth, "electrical box")
    assert builder._truth_match(electrical, truth, "electrical box")
    assert builder._task_query("找到红色停止标志并记录位置") == "red stop sign"
    row = _row()
    config = {
        "mission": "from_text",
        "task_text": "Monitor the site for backpacks for five minutes.",
    }
    text, source = builder._task_for_call(row, {}, config, {}, {})
    assert text == "Find the backpack and record its position."
    assert source == "inferred_config_query"
    row["labels"]["onboard"]["step"] = "s1"
    text, source = builder._task_for_call(row, {}, config, {}, {"s1": "blue backpack"})
    assert text == "Find the blue backpack and record its position."
    assert source == "recorded_alert_query"
    # A different mission query must never inherit the world's true target.
    assert not builder._truth_match(backpack, truth, "person")
    assert builder._task_for_call(_row(), {}, {}, {}, {})[0] == _row()["task_text"]


def test_conflicting_world_splits_fail_closed(flight_dir):
    indexes = []
    for split in ("train", "test"):
        path = flight_dir / f"{split}.json"
        path.write_text(
            json.dumps({"worlds": [{"world_id": "gen_test", "split": split}]}),
            encoding="utf-8",
        )
        indexes.append(path)
    with pytest.raises(ValueError, match="Conflicting index splits"):
        builder._world_split("gen_test", indexes)
