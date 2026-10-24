import json
from pathlib import Path

from scripts.mine_slow_model_hardcases import expected_positive, hardcase_counts, mine_episode


def test_mine_four_hardcase_types(tmp_path: Path):
    episode = tmp_path / "episode-0000"
    episode.mkdir()
    (episode / "episode.json").write_text(json.dumps({"task_text": "Find the fire hydrant", "world_sdf": "gen_test.sdf"}), encoding="utf-8")
    truth = {
        "targets": [
            {"target_id": "hydrant", "asset_slug": "fire_hydrant", "class": "fire_hydrant", "true_target": True, "pose": {"x_east_m": 0, "y_north_m": 0}},
                {"target_id": "box", "asset_slug": "cardboard_box", "class": "cardboard_box", "true_target": False, "pose": {"x_east_m": 0, "y_north_m": 10}},
        ]
    }
    rows = []
    cases = [(0, "t0", [10, 10, 30, 30], [0, 0], "no_match", 0.99), (1, "t1", [40, 10, 60, 30], [10, 0], "match", 0.99), (2, "t2", [10, 10, 30, 30], [0, 0], "match", 0.8), (3, "t3", [10, 10, 30, 30], [0, 0], "no_match", 0.99), (4, "t4", [10, 10, 30, 30], [0, 0], "match", 0.5)]
    for step, track, bbox, ground, decision, confidence in cases:
        rows.append({"step_index": step, "obs_before": {"rgb_path": "frame.png"}, "labels": {"onboard": {"tracks": [{"id": track, "bbox": bbox, "ground_ne": ground}], "slow_calls": [{"track_id": track, "decision": decision, "confidence": confidence}]}}})
    (episode / "steps.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    records, eligible = mine_episode(episode, truth, "synthetic")
    assert eligible == 5
    kinds = [record["kind"] for record in records]
    assert "false_reject" not in kinds
    assert kinds.count("decision_flip") == 4
    assert "false_accept" in kinds
    assert any("low_conf_correct" in record["hardcase_types"] for record in records)


def test_count_member_is_positive_and_attribute_find_uses_task_equivalence(tmp_path: Path):
    count_truth = {
        "targets": [
            {"target_id": "cone", "class": "traffic_cone", "category_words": ["traffic cone"], "color": "orange", "true_target": True, "count_member": True, "pose": {"x_east_m": 0, "y_north_m": 0}},
            {"target_id": "member", "class": "traffic_cone", "category_words": ["traffic cone"], "color": "orange", "count_member": True, "pose": {"x_east_m": 0, "y_north_m": 10}},
            {"target_id": "barrier", "class": "jersey_barrier", "category_words": ["jersey barrier"], "distractor": True, "pose": {"x_east_m": 0, "y_north_m": 20}},
        ],
        "segmentation_labels": {"1": {"target_id": "member", "role": "count_member"}, "2": {"target_id": "barrier", "role": "distractor"}},
    }
    assert expected_positive("count", "Count orange traffic cones", count_truth["targets"][1], count_truth, "count_member") is True

    find_truth = {
        "targets": [
            {"target_id": "orange", "class": "traffic_cone", "category_words": ["traffic cone"], "color": "orange", "true_target": True},
            {"target_id": "blue", "class": "traffic_cone", "category_words": ["traffic cone"], "color": "blue", "count_member": True},
            {"target_id": "unknown-color", "class": "traffic_cone", "category_words": ["traffic cone"], "color": None},
        ]
    }
    assert expected_positive("find", "Find orange traffic cone", find_truth["targets"][0], find_truth, "true_target") is True
    assert expected_positive("find", "Find orange traffic cone", find_truth["targets"][1], find_truth, "count_member") is False
    assert expected_positive("find", "Find orange traffic cone", find_truth["targets"][2], find_truth, "other") is True

    episode = tmp_path / "count-episode"
    episode.mkdir()
    (episode / "episode.json").write_text(json.dumps({"task_text": "Count orange traffic cones"}), encoding="utf-8")
    row = {"step_index": 0, "labels": {"onboard": {"skill": "count", "tracks": [{"id": "tm", "bbox": [1, 1, 20, 20], "ground_ne": [10, 0]}], "slow_calls": [{"track_id": "tm", "decision": "match", "confidence": 0.99}]}}}
    (episode / "steps.jsonl").write_text(json.dumps(row), encoding="utf-8")
    records, _ = mine_episode(episode, count_truth, "synthetic")
    assert len(records) == 1
    assert records[0]["truth_role"] == "count_member"
    assert records[0]["expected_positive"] is True
    assert records[0]["hardcase_types"] == []


def test_summary_counts_match_jsonl_record_labels():
    rows = [
        {"hardcase_types": ["false_reject"]},
        {"hardcase_types": ["decision_flip", "false_reject"]},
        {"hardcase_types": []},
    ]
    summary = dict(hardcase_counts(rows))
    assert summary == {"false_reject": 2, "decision_flip": 1}
    assert sum(summary.values()) == 3
