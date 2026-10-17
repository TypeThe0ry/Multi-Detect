from scripts.eval_slow_system import size_bucket_metrics


def test_size_bucket_boundaries_and_identity_metrics():
    sizes = [11.9, 12, 15.9, 16, 19.9, 20, 31.9, 32]
    dataset, results = [], []
    for index, short_side in enumerate(sizes):
        # 1280x960 canvas: normalized width encodes the requested pixel size.
        sample_id = f"s{index}"
        dataset.append({
            "sample_id": sample_id,
            "focus_track_id": "track-1",
            "candidates": [{"track_id": "track-1", "box_xyxy_normalized": [0, 0, short_side / 1280, short_side / 960]}],
        })
        expected = "confirm_target" if index % 2 == 0 else "reject_track"
        results.append({"sample_id": sample_id, "expected": expected, "decision": expected})
    report = size_bucket_metrics(results, dataset)
    assert [report[key]["sample_count"] for key in ("<12", "12-16", "16-20", "20-32", ">=32")] == [1, 2, 2, 2, 1]
    assert report["<12"]["true_target_recall"] == 1.0
    assert report["12-16"]["distractor_rejection_rate"] == 1.0


def test_rows_without_candidate_boxes_are_skipped_not_fatal():
    dataset = [
        {"sample_id": "plan", "candidates": []},
        {"sample_id": "bad", "candidates": [{"track_id": "t", "box_xyxy_normalized": None}]},
    ]
    results = [
        {"sample_id": "plan", "expected": "reject_track", "decision": "reject_track"},
        {"sample_id": "bad", "expected": "reject_track", "decision": "reject_track"},
    ]
    report = size_bucket_metrics(results, dataset)
    assert all(bucket["sample_count"] == 0 for bucket in report.values())
