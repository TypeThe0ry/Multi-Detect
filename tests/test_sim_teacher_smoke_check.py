import json
from pathlib import Path

from scripts.check_sim_teacher_smoke import evaluate_episode


def test_smoke_checker_enforces_frames_altitude_and_ground_range(tmp_path):
    episode_dir = tmp_path / "episode"
    episode_dir.mkdir()
    (episode_dir / "rgb").mkdir()
    (episode_dir / "rgb/frame.png").write_bytes(b"png")
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps({"observation": {
        "camera_altitude_m": 20,
        "observation_waypoint": {"x_east_m": 0, "y_north_m": 0, "z_up_m": 0},
    }}), encoding="utf-8")
    truth = tmp_path / "truth.json"
    truth.write_text(json.dumps({"targets": [{
        "target_id": "target", "true_target": True,
        "pose": {"x_east_m": 20, "y_north_m": 0, "z_up_m": 0},
    }]}), encoding="utf-8")
    (episode_dir / "episode.json").write_text(json.dumps({"episode_id": "e", "status": "complete"}), encoding="utf-8")
    rows = []
    for i in range(4):
        rows.append({
            "step_index": i,
            "labels": {"phase": "observe", "target_in_fov": True},
            "obs_before": {"pose": {"alt_m": 20.5, "local_ned_m": [0, 0, 0]}, "rgb_path": "rgb/frame.png"},
        })
    (episode_dir / "steps.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")
    report = evaluate_episode(episode_dir, intent_path=intent, truth_path=truth, minimum_frames=4)
    assert report["passed"]
    assert report["observed_frame_count"] == 4
    assert report["observed_ground_distance_mean_m"] == 20
