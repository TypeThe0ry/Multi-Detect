import json

from scripts.altair import paired_report as pr


def row(sample_id, expected, decision, valid=True):
    return {"sample_id": sample_id, "expected": expected, "decision": decision, "valid": valid}


def test_confusion_counts_and_invalid_other():
    rows = [
        row("a", "confirm_target", "confirm_target"),
        row("b", "confirm_target", "reject_track"),
        row("c", "reject_track", "approach_track"),
        row("d", "reject_track", "nonsense", False),
    ]
    report = pr.confusion(rows)
    assert report["column_labels"] == ["confirm_target", "reject_track", "approach_track", "other"]
    assert report["matrix"] == [[1, 1, 0, 0], [0, 0, 1, 1], [0, 0, 0, 0]]


def test_mcnemar_exact_known_table():
    assert pr.mcnemar_exact(1, 3) == 0.625
    assert pr.mcnemar_exact(0, 0) == 1.0


def test_bootstrap_is_deterministic():
    inc = [True, False, True, False]
    cand = [True, True, False, False]
    assert pr._bootstrap_ci(inc, cand, bootstrap=200, seed=7) == pr._bootstrap_ci(inc, cand, bootstrap=200, seed=7)


def test_area_bins_equal_count_and_edges(tmp_path):
    dataset = tmp_path / "test.jsonl"
    records = []
    pairs = []
    for i in range(8):
        sample_id = str(i)
        records.append({
            "sample_id": sample_id,
            "focus_track_id": "t",
            "candidates": [{"track_id": "t", "box_xyxy_normalized": [0, 0, (i + 1) / 10, 1]}],
        })
        pairs.append((sample_id, row(sample_id, "confirm_target", "confirm_target"), row(sample_id, "confirm_target", "reject_track" if i % 2 else "confirm_target")))
    dataset.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    result = pr.area_bins(pairs, dataset)
    assert [item["n"] for item in result["values"]] == [1] * 8
    assert result["values"][0]["area_min"] < result["values"][-1]["area_min"]


def test_alignment_reports_missing_ids_and_scores_intersection():
    incumbent = [row("a", "confirm_target", "confirm_target"), row("only-inc", "reject_track", "reject_track")]
    candidate = [row("a", "confirm_target", "reject_track"), row("only-cand", "reject_track", "reject_track")]
    report = pr.build_report(incumbent, candidate, bootstrap=50, seed=7, dataset=pr.DEFAULT_DATASET)
    assert report["alignment"] == {
        "incumbent_rows": 2,
        "candidate_rows": 2,
        "intersection_rows": 1,
        "only_incumbent": ["only-inc"],
        "only_candidate": ["only-cand"],
    }
    assert report["metrics"]["confirm_recall"]["incumbent"] == 1.0
    assert report["metrics"]["confirm_recall"]["candidate"] == 0.0
