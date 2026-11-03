from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.report_identity_round import build_report, markdown_report, new_false_accepts


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _inputs(tmp_path: Path) -> tuple[dict[str, Path], dict[str, Path], dict[str, Path]]:
    pipelines, views = {}, {}
    dataset = tmp_path / "test.jsonl"
    rows = [
        {"sample_id": "new", "target_id": "d", "label_type": "distractor", "task_text": "找 SUV | 左侧",
         "task_query": "SUV", "candidate_category": "postbox", "view": "front", "world": "synthetic",
         "image_size": {"width": 100, "height": 100},
         "candidates": [{"track_id": "t", "box_xyxy_normalized": [0, 0, .1, .1]}]},
        {"sample_id": "old", "target_id": "d", "label_type": "distractor", "task_text": "找 SUV | 左侧",
         "candidate_category": "postbox", "view": "front", "world": "synthetic"},
        {"sample_id": "positive", "target_id": "t", "label_type": "true_target", "view": "recovery"},
    ]
    dataset.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    for label in ("r3", "r4", "r5"):
        pipelines[label], views[label] = tmp_path / label / "pipeline", tmp_path / label / "views"
        predictions = [{"sample_id": row["sample_id"], "decision": decision} for row, decision in zip(rows, (
            "reject_track" if label == "r3" else "approach_track", "confirm_target", "confirm_target"))]
        metrics = {"distractor_rejection_rate": 1.0 if label == "r3" else .98,
                   "size_buckets": {"<12": {"true_target_recall": .80 if label == "r4" else .77}}}
        for split in ("v1", "v3"):
            _write(pipelines[label] / f"eval-{split}.json", {"metrics": metrics, "rows": predictions})
        _write(views[label] / f"{label}-predictions.json", {"rows": predictions})
        _write(views[label] / "identity-report.json", {"candidate_pass": False,
            "baseline": {"multiview_consistency": {"value": 0.1, "count": 10}},
            "candidate": {key: {"value": value, "count": 10} for key, value in (
                ("multiview_consistency", .833), ("true_target_false_reject", .042), ("distractor_false_accept", .075))}})
    return pipelines, views, {key: dataset for key in ("v1", "v3", "four_view")}


def test_multiple_candidates_boundaries_and_complete_new_errors(tmp_path: Path) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    report = build_report("r3", {key: pipelines[key] for key in ("r4", "r5")}, pipelines["r3"], views, datasets, "r4")
    assert report["rounds"]["r3"]["metrics"]["four_view.multiview_consistency"] == .833
    for label in ("r4", "r5"):
        result = report["candidates"][label]
        assert result["hard_gate_status"] == "pass"
        assert result["absolute_gate_status"] == "fail"
        assert result["evidence_status"] == "complete"
        assert len(result["hard_gates"]) == 6
        assert len(result["absolute_gates"]) == 3
        for changes in result["new_false_accepts"].values():
            assert changes["count"] == 1  # old accepts and positive matches are excluded
            group = changes["groups"][0]
            assert group["dimensions"] == {"task_text": "找 SUV | 左侧", "candidate_category": "postbox",
                                           "size_bucket": "8-12", "view": "front"}
            assert group["samples"] == [{"sample_id": "new", "world": "synthetic",
                                          "baseline_decision": "reject_track", "candidate_decision": "approach_track"}]
    md = markdown_report(report)
    assert "找 SUV \\| 左侧" in md
    assert "新增未见 world" in md
    assert "-2.000" in md


@pytest.mark.parametrize("key,value", [
    ("v1.distractor_rejection", .9799), ("v3.distractor_rejection", .9799),
    ("v3.small_recall", .7699), ("multiview_consistency", .8329),
    ("true_target_false_reject", .0421), ("distractor_false_accept", .0751),
])
def test_each_hard_gate_fails_outside_limit(tmp_path: Path, key: str, value: float) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    if key.startswith("v"):
        path = pipelines["r5"] / f"eval-{key[:2]}.json"
        data = json.loads(path.read_text())
        if key.endswith("small_recall"):
            data["metrics"]["size_buckets"]["<12"]["true_target_recall"] = value
        else:
            data["metrics"]["distractor_rejection_rate"] = value
    else:
        path = views["r5"] / "identity-report.json"
        data = json.loads(path.read_text())
        data["candidate"][key]["value"] = value
    _write(path, data)
    report = build_report("r3", {"r4": pipelines["r4"], "r5": pipelines["r5"]}, pipelines["r3"], views, datasets, "r4")
    assert report["candidates"]["r5"]["hard_gate_status"] == "fail"
    assert sum(gate["status"] == "fail" for gate in report["candidates"]["r5"]["hard_gates"]) == 1


def test_missing_inputs_and_reference_are_pending(tmp_path: Path) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    report = build_report("r3", {"future": tmp_path / "absent"}, pipelines["r3"],
                          {**views, "future": tmp_path / "absent-views"}, datasets, "r4")
    future = report["candidates"]["future"]
    assert future["hard_gate_status"] == future["absolute_gate_status"] == future["evidence_status"] == "pending"
    assert all(gate["status"] == "pending" for gate in future["hard_gates"])
    assert future["hard_gates"][2]["threshold"] is None
    assert len(report["rounds"]["future"]["pending"]) == 9
    assert all(change["count"] is None for change in future["new_false_accepts"].values())
    assert "pending；缺少" in markdown_report(report)
    datasets["v1"] = tmp_path / "absent.jsonl"
    report = build_report("r3", {"r4": pipelines["r4"]}, pipelines["r3"], views, datasets, "r4")
    assert report["candidates"]["r4"]["hard_gate_status"] == "pass"
    assert report["candidates"]["r4"]["evidence_status"] == "pending"


def test_missing_baseline_and_null_or_empty_metrics(tmp_path: Path) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    path = views["r5"] / "identity-report.json"
    data = json.loads(path.read_text())
    data["candidate"]["multiview_consistency"] = {"value": .99, "count": 0}
    data["candidate"]["true_target_false_reject"] = {"value": None, "count": 0}
    _write(path, data)
    report = build_report("r3", {"r5": pipelines["r5"]}, tmp_path / "absent", views, datasets, "r4")
    assert report["candidates"]["r5"]["hard_gate_status"] == "pending"
    assert report["rounds"]["r5"]["metrics"]["four_view.multiview_consistency"] is None


def test_absolute_boundary_pass_and_failed_gate_with_missing_evidence(tmp_path: Path) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    path = views["r5"] / "identity-report.json"
    data = json.loads(path.read_text())
    for key, value in (("multiview_consistency", .90), ("true_target_false_reject", .08),
                       ("distractor_false_accept", .026)):
        data["candidate"][key]["value"] = value
    _write(path, data)
    report = build_report("r3", {"r5": pipelines["r5"]}, pipelines["r3"], views, datasets, "missing-r4")
    result = report["candidates"]["r5"]
    assert result["absolute_gate_status"] == "pass"
    assert result["hard_gate_status"] == "fail"  # .08 exceeds .042 despite the missing recall reference
    assert result["evidence_status"] == "pending"
    assert result["hard_gates"][2]["status"] == "pending"


def test_zero_count_metrics_are_pending_and_invalid_rates_raise(tmp_path: Path) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    path = pipelines["r4"] / "eval-v3.json"
    data = json.loads(path.read_text())
    data["metrics"]["size_buckets"]["<12"]["sample_count"] = 0
    _write(path, data)
    report = build_report("r3", {"r4": pipelines["r4"]}, pipelines["r3"], views, datasets, "r4")
    assert report["candidates"]["r4"]["hard_gate_status"] == "pending"
    data["metrics"]["distractor_rejection_rate"] = 1.01
    _write(path, data)
    with pytest.raises(ValueError, match="invalid rate"):
        build_report("r3", {"r4": pipelines["r4"]}, pipelines["r3"], views, datasets, "r4")


@pytest.mark.parametrize("failure", ["duplicate_dataset", "duplicate_prediction", "mismatch", "malformed"])
def test_bad_inputs_are_errors_not_pending(tmp_path: Path, failure: str) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    dataset, b, c = datasets["v1"], pipelines["r3"] / "eval-v1.json", pipelines["r4"] / "eval-v1.json"
    data = json.loads(c.read_text())
    if failure == "duplicate_dataset":
        dataset.write_text(dataset.read_text() * 2)
    elif failure == "duplicate_prediction":
        _write(c, {"rows": data["rows"] * 2})
    elif failure == "mismatch":
        _write(c, {"rows": data["rows"][:-1]})
    else:
        c.write_text("{broken")
    with pytest.raises(ValueError):
        new_false_accepts(dataset, b, c)


def test_plain_predictions_no_view_and_grouping(tmp_path: Path) -> None:
    dataset = tmp_path / "data.jsonl"
    rows = [{"id": str(i), "truth_target_id": "d", "truth_category": "postbox", "label_type": "truth_distractor",
             "task_text": "Find SUV", "task_query": "suv"} for i in range(2)]
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows))
    b = _write(tmp_path / "b.json", [{"id": str(i), "decision": {"decision": "reject_track"}} for i in range(2)])
    c = _write(tmp_path / "c.json", [{"id": str(i), "decision": {"decision": "confirm_target"}} for i in range(2)])
    report = new_false_accepts(dataset, b, c)
    assert report["count"] == 2
    assert len(report["groups"]) == 1
    assert report["groups"][0]["dimensions"]["view"] == "unknown"


def test_cli_writes_both_formats_and_rejects_duplicate_labels(tmp_path: Path) -> None:
    pipelines, views, datasets = _inputs(tmp_path)
    output = tmp_path / "out" / "report.json"
    md = output.with_suffix(".md")
    command = [sys.executable, "scripts/report_identity_round.py", "--baseline", f"r3={pipelines['r3']}",
               "--candidate", f"r4={pipelines['r4']}", "--recall-reference", "r4",
               "--output-json", str(output), "--output-md", str(md)]
    for label, path in views.items():
        if label != "r5":
            command += ["--views", f"{label}={path}"]
    for flag, key in (("--v1-dataset", "v1"), ("--v3-dataset", "v3"), ("--view-dataset", "four_view")):
        command += [flag, str(datasets[key])]
    result = subprocess.run(command, capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(("src", ".")), "PYTHONUTF8": "1"})
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["candidates"]["r4"]["hard_gate_status"] == "pass"
    assert md.read_text().startswith("# 身份轮次决策报告")
    result = subprocess.run(command + ["--candidate", f"r4={pipelines['r4']}"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "duplicate labels" in result.stderr
