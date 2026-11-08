import json
import sys

import pytest

from scripts.eval_r21id_identity import _merge, calculate_metrics, evaluate, main, markdown_report


def _rows(target="t1", category="fire_hydrant"):
    rows = []
    for view, expected in (("front", "confirm_target"), ("oblique", "confirm_target"), ("edge_truncated", "reject_track"), ("recovery", "confirm_target")):
        rows.append({"sample_id": f"{target}-{view}", "world": "test-world", "target_id": target, "view": view,
                     "task_category": category, "label_type": "true_target" if expected != "reject_track" else "distractor",
                     "target_short_side_px": 10, "assistant_target": {"decision": expected}, "expected": expected})
    return rows


def test_calculates_gate_metrics_from_synthetic_four_view_instance():
    rows = _rows()
    preds = [{"sample_id": r["sample_id"], "expected": r["expected"], "decision": r["expected"]} for r in rows]
    merged = _merge(rows, preds)
    metrics = calculate_metrics(merged)
    assert metrics["multiview_consistency"]["value"] == 0.0  # reject edge view differs
    assert metrics["recovery_rate"]["value"] == 1.0
    assert metrics["average_recovery_observations"] == 1.0
    assert metrics["fire_hydrant_false_reject"]["value"] == 0.0
    assert metrics["distractor_false_accept"]["value"] == 0.0


def test_evaluate_failure_names_r18_retention(tmp_path):
    rows = _rows()
    ds = tmp_path / "test.jsonl"
    ds.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    bad = [{"sample_id": r["sample_id"], "expected": r["expected"], "decision": "reject_track"} for r in rows]
    baseline, candidate = tmp_path / "r18.json", tmp_path / "candidate.json"
    baseline.write_text(json.dumps({"rows": bad}), encoding="utf-8")
    candidate.write_text(json.dumps({"rows": bad}), encoding="utf-8")
    result = evaluate(ds, baseline, candidate)
    assert result["candidate_pass"] is False
    assert result["decision"] == "保留 r18"
    assert "保留 r18" in markdown_report(result)


def test_cli_records_failed_acceptance_and_exits_success(tmp_path, monkeypatch):
    rows = _rows()
    dataset = tmp_path / "test.jsonl"
    dataset.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    reject_all = [{"sample_id": row["sample_id"], "decision": "reject_track"} for row in rows]
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps({"rows": reject_all}), encoding="utf-8")
    candidate.write_text(json.dumps({"rows": reject_all}), encoding="utf-8")
    output_json = tmp_path / "identity-report.json"
    output_md = tmp_path / "identity-report.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eval_r21id_identity.py",
            "--dataset", str(dataset),
            "--baseline", str(baseline),
            "--candidate", str(candidate),
            "--output-json", str(output_json),
            "--output-md", str(output_md),
        ],
    )

    assert main() == 0
    report = json.loads(output_json.read_text(encoding="utf-8"))
    assert report["candidate_pass"] is False
    assert report["decision"] == "保留 r18"
    assert "不通过原因" in output_md.read_text(encoding="utf-8")


def test_rows_without_target_id_are_skipped_not_scored():
    # search_region / no-candidate rows name no instance.
    assert _merge([{"sample_id": "x"}], [{"sample_id": "x", "decision": "search_region"}]) == []


def test_rejects_candidate_with_more_distractor_accepts_than_r18(tmp_path):
    rows = _rows()
    ds = tmp_path / "test.jsonl"
    ds.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    baseline_rows = [{"sample_id": r["sample_id"], "expected": r["expected"], "decision": r["expected"]} for r in rows]
    candidate_rows = [dict(r, decision="confirm_target") for r in baseline_rows]
    baseline, candidate = tmp_path / "r18.json", tmp_path / "candidate.json"
    baseline.write_text(json.dumps({"rows": baseline_rows}), encoding="utf-8")
    candidate.write_text(json.dumps({"rows": candidate_rows}), encoding="utf-8")
    result = evaluate(ds, baseline, candidate)
    assert result["distractor_gate_vs_r18"]["pass"] is False
    assert "候选干扰物接受率高于 r18" in result["reasons"][-1]
    assert result["decision"] == "保留 r18"


def test_null_view_rows_are_counted_and_incomplete_instances_reported(tmp_path):
    rows = _rows()
    rows[0]["view"] = None
    ds = tmp_path / "test.jsonl"
    ds.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    predictions = [{"sample_id": r["sample_id"], "decision": r["expected"]} for r in rows]
    first, second = tmp_path / "r18.json", tmp_path / "candidate.json"
    first.write_text(json.dumps({"rows": predictions}), encoding="utf-8")
    second.write_text(json.dumps({"rows": predictions}), encoding="utf-8")
    result = evaluate(ds, first, second)
    assert result["null_view_rows"] == {"r18": 1, "candidate": 1}
    assert result["incomplete_instances"] == {"r18": 1, "candidate": 1}


def test_custom_model_labels_are_used_in_report(tmp_path):
    rows = _rows()
    ds = tmp_path / "test.jsonl"
    ds.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    predictions = [{"sample_id": r["sample_id"], "decision": r["expected"]} for r in rows]
    baseline, candidate = tmp_path / "r3.json", tmp_path / "r4.json"
    baseline.write_text(json.dumps({"rows": predictions}), encoding="utf-8")
    candidate.write_text(json.dumps({"rows": predictions}), encoding="utf-8")

    report = evaluate(ds, baseline, candidate, baseline_label="r21id-r3", candidate_label="r21id-r4")
    rendered = markdown_report(report)

    assert report["baseline_label"] == "r21id-r3"
    assert report["candidate_label"] == "r21id-r4"
    assert "| 指标 | r21id-r3 | r21id-r4 | 门槛 |" in rendered
    assert report["decision"] == "保留 r21id-r3"
