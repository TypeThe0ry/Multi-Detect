import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from scripts.review_slow_mistakes import build_report, review_episode


def test_review_classifies_groups_and_writes_html():
    tmp_path = Path(".pytest-review-slow-mistakes")
    shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.mkdir()
    episode = tmp_path / "gen_v2_w0000" / "episodes" / "episode-0000"
    (episode / "rgb").mkdir(parents=True)
    (episode / "seg").mkdir()
    Image.new("RGB", (100, 100), (30, 40, 50)).save(episode / "rgb" / "frame.png")
    labels = np.zeros((100, 100, 3), dtype=np.uint8)
    labels[10:40, 10:40, 2] = 1
    labels[60:90, 60:90, 2] = 2
    Image.fromarray(labels, mode="RGB").save(episode / "seg" / "frame.png")
    truth = {
        "targets": [
            {
                "target_id": "backpack-1",
                "label": "backpack",
                "category": "backpack",
                "category_words": ["backpack"],
                "true_target": True,
            },
            {
                "target_id": "electrical-1",
                "label": "electrical box",
                "category": "electrical_box",
                "category_words": ["electrical box"],
                "true_target": False,
            },
        ],
        "segmentation_labels": {
            "1": {"target_id": "backpack-1"},
            "2": {"target_id": "electrical-1"},
        },
    }
    rows = []
    cases = [
        ([10, 10, 40, 40], "approach_track"),
        ([60, 60, 90, 90], "reject_track"),
        ([60, 60, 90, 90], "approach_track"),
        ([10, 10, 40, 40], "reject_track"),
    ]
    for index, (box, decision) in enumerate(cases):
        rows.append(
            {
                "step_index": index,
                "task_text": "Find the backpack and record its position.",
                "obs_before": {"rgb_path": "rgb/frame.png", "segmentation_path": "seg/frame.png"},
                "labels": {
                    "onboard": {
                        "tracks": [{"id": f"t{index}", "bbox": box}],
                        "slow_calls": [
                            {
                                "track_id": f"t{index}",
                                "decision": decision,
                                "reason": "synthetic",
                                "confidence": 0.5,
                            }
                        ],
                    }
                },
            }
        )
    (episode / "steps.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows), encoding="utf-8"
    )

    records = review_episode(episode, truth)
    assert [record["classification"] for record in records] == [
        "correct_accept",
        "correct_reject",
        "false_accept",
        "false_reject",
    ]
    assert records[0]["judged_object"] == "backpack"
    assert records[1]["judged_object"] == "electrical box"
    out = tmp_path / "report"
    summary = (
        build_report(records, out / "review.html", out / "review.json")
        if out.mkdir() is None
        else None
    )
    assert summary["classification_counts"]["false_accept"] == 1
    assert summary["classification_counts"]["false_reject"] == 1
    assert "electrical box" in (out / "review.html").read_text(encoding="utf-8")
    shutil.rmtree(tmp_path, ignore_errors=True)
