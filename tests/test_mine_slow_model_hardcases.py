import json
from pathlib import Path

from scripts.mine_slow_model_hardcases import mine_episode


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
    assert "false_reject" not in kinds  # the target's conflicting views are a flip group
    assert kinds.count("decision_flip") == 4
    assert "false_accept" in kinds
    assert any("low_conf_correct" in record["hardcase_types"] for record in records)
