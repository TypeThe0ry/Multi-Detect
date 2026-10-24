from pathlib import Path

import pytest

from scripts.replay_onboard_episode import Results, compare, first_non_search_divergence, read_rows, replay


FIXTURE = Path(__file__).parent / "fixtures/onboard_replay"


def test_fixture_episode_loads_and_logged_results_are_supported():
    assert read_rows(FIXTURE)[0]["step_index"] == 0
    results = Results("logged", None)
    assert results.get("det:0", [{"bbox": [1, 2, 3, 4]}], "detector")[0]["bbox"] == [1, 2, 3, 4]


def test_cache_mode_writes_and_reads_cached_results(tmp_path):
    path = tmp_path / "cache.json"
    results = Results("cache", path)
    assert results.get("k", {"decision": "approach"}, "slow")["decision"] == "approach"
    results.save()
    again = Results("cache", path)
    assert again.get("k", None, "slow")["decision"] == "approach"
    assert again.stats["cache_hits"] == 1


def test_compare_reports_first_divergence():
    left = {"trace": [{"step": 3, "action": "hold", "calls": []}]}
    right = {"trace": [{"step": 3, "action": "turn", "calls": []}]}
    result = compare(left, right)
    assert result["first_divergence"] == 0
    assert result["step"] == 3


def test_non_search_divergence_skips_search_action_differences():
    trace = [
        {"step": 5, "decision_class": "search", "action": {"kind": "turn"},
         "logged_action": {"kind": "translate"}},
        {"step": 6, "decision_class": "approach", "action": {"kind": "hold"},
         "logged_action": {"kind": "translate"}},
    ]
    assert first_non_search_divergence(trace) == {
        "index": 1, "step": 6, "decision_class": "approach"
    }


def test_real_mode_is_explicitly_unimplemented():
    with pytest.raises(RuntimeError, match="real 模式未实现"):
        replay(FIXTURE, mode="real")


def test_closed_loop_reports_pose_rebind_and_pre_divergence_metrics(tmp_path):
    episode = tmp_path / "count-cones" / "campaign" / "episode-0000"
    episode.mkdir(parents=True)
    rows = []
    camera = {"fx": 500, "fy": 500, "cx": 320, "cy": 240, "width": 640, "height": 480}
    from multidetect.sim_teacher.policies import ExpertSearchPolicy

    policy = ExpertSearchPolicy(target_ned=(1e7, 1e7, 0), intrinsics=camera,
                                image_size=(640, 480), altitude_m=12, search_radius_m=55,
                                restart_route=False)
    waypoint = policy._waypoints[0]
    poses = [(0.0, 0.0), (waypoint[0], waypoint[1]), (waypoint[0], waypoint[1]),
             (waypoint[0], waypoint[1]), (waypoint[0], waypoint[1]), (waypoint[0], waypoint[1])]
    for index, (before, after) in enumerate(zip(poses, poses[1:] + [poses[-1]], strict=True)):
        obs_before = {"camera_info": camera, "pose": {"local_ned_m": [before[0], before[1], -12.0]},
                      "attitude": {"yaw_deg": 0.0}, "frame_sensor_stamp_ns": index * 1_000_000_000}
        obs_after = {"camera_info": camera, "pose": {"local_ned_m": [after[0], after[1], -12.0]},
                     "attitude": {"yaw_deg": 0.0}, "frame_sensor_stamp_ns": (index + 1) * 1_000_000_000}
        rows.append({"step_index": index, "task_text": "count cone", "obs_before": obs_before,
                     "obs_after": obs_after, "action": {"kind": "hold"},
                     "labels": {"onboard": {"skill": "count", "tracks": [], "slow_calls": []}}})
    (episode / "steps.jsonl").write_text("\n".join(__import__("json").dumps(row) for row in rows), encoding="utf-8")
    result = replay(episode, closed_loop=True)
    metrics = result["metrics"]
    assert result["open_loop"] is False
    assert result["trace"][1]["pose_ned"] == [waypoint[0], waypoint[1], -12.0]
    assert result["trace"][4]["search_waypoint_index"] > 0
    assert result["trace"][4]["diagnostics"]["search_altitude_m"] > 8.0
    assert metrics["pre_divergence_steps"] <= metrics["steps"]
    assert "diverged_at_step" in metrics
    assert metrics["search_placeholder_mismatches"] < metrics["steps"]
    open_result = replay(episode)
    assert open_result["open_loop"] is True
    assert all(item["action"]["kind"] == "hold" for item in open_result["trace"])
