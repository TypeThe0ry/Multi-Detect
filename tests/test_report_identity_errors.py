from __future__ import annotations

import json
from pathlib import Path

from scripts.report_identity_errors import build_report, markdown_report


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_report_groups_truth_category_and_model_delta(tmp_path: Path) -> None:
    dataset = tmp_path / "test.jsonl"
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    truth = tmp_path / "configs" / "gen.truth.json"
    truth.parent.mkdir()
    truth.write_text(json.dumps({"targets": [
        {"target_id": "target-1", "class": "pickup", "true_target": True},
        {"target_id": "distractor-1", "class": "suv"},
    ]}), encoding="utf-8")
    dataset_rows = [
        {"sample_id": "p", "world": "gen", "target_id": "distractor-1", "label_type": "distractor",
         "task_query": "pickup", "view": "front", "candidates": [{"track_id": "t", "box_xyxy_normalized": [0, 0, .005, .005]}], "focus_track_id": "t"},
        {"sample_id": "t", "world": "gen", "target_id": "target-1", "label_type": "proposal_true",
         "task_query": "pickup", "view": "recovery", "candidates": [{"track_id": "t", "box_xyxy_normalized": [0, 0, .006, .006]}], "focus_track_id": "t"},
        {"sample_id": "skip", "world": "gen", "target_id": "target-1", "label_type": "true_target", "view": None},
    ]
    _write_jsonl(dataset, dataset_rows)
    baseline.write_text(json.dumps({"rows": [
        {"sample_id": "p", "decision": "confirm_target"},
        {"sample_id": "t", "decision": "reject_track"},
        {"sample_id": "skip", "decision": "reject_track"},
    ]}), encoding="utf-8")
    candidate.write_text(json.dumps({"rows": [
        {"sample_id": "p", "decision": "reject_track"},
        {"sample_id": "t", "decision": "confirm_target"},
        {"sample_id": "skip", "decision": "reject_track"},
    ]}), encoding="utf-8")
    # The implementation searches ROOT/configs/scenarios, so use a tiny cache-independent
    # v1-style category field for this unit test.
    for row in dataset_rows:
        row["candidate_category"] = "suv" if row["sample_id"] == "p" else "pickup"
    _write_jsonl(dataset, dataset_rows)
    report = build_report(dataset, baseline, candidate)
    assert report["models"]["baseline"]["distractor_false_accept"]["pickup|suv|front|<8|gen"]["errors"] == 1
    assert report["models"]["candidate"]["distractor_false_accept"]["pickup|suv|front|<8|gen"]["errors"] == 0
    assert report["models"]["candidate"]["true_target_false_reject"]["pickup|pickup|recovery|<8|gen"]["errors"] == 0
    assert report["comparisons"]["distractor_false_accept"]["pickup|suv|front|<8|gen"]["delta_errors"] == -1
    assert "候选 Top-20 错误组合" in markdown_report(report)


def test_report_accepts_plain_prediction_list(tmp_path: Path) -> None:
    dataset = tmp_path / "test.jsonl"
    prediction = tmp_path / "prediction.json"
    _write_jsonl(dataset, [{"sample_id": "x", "world": "w", "target_id": "id", "label_type": "truth_match",
                            "truth_category": "cone", "task_query": "cone", "view": "front"}])
    prediction.write_text(json.dumps([{"sample_id": "x", "decision": "approach_track"}]), encoding="utf-8")
    report = build_report(dataset, None, prediction)
    assert report["models"]["candidate"]["true_target_false_reject"]["cone|cone|front|unknown|w"]["errors"] == 0
