import json
from pathlib import Path

from scripts.compare_nightly_arms import build_comparison, render_text, run


def _episode(root: Path, mission: str, *, status=None, failure=None, alive=None,
             success=None, capture=False, metrics=None):
    episode_dir = root / "main" / mission / "campaign" / "world" / "episodes" / "episode-0000-test"
    episode_dir.mkdir(parents=True)
    episode = {"status": status, "created_wall_time": 10.0, "finished_wall_time": 25.0,
               "container_failure_diagnostics": {"failure_class": failure,
               "failure_state": {"px4_process_alive": alive}}}
    if status is None:
        episode.pop("finished_wall_time")
    if metrics:
        episode.update(metrics)
    (episode_dir / "episode.json").write_text(json.dumps(episode), encoding="utf-8")
    if capture:
        (episode_dir / "px4-clock-stall-capture.json").write_text("{}", encoding="utf-8")
    report = {"episodes": [{"episode": str(episode_dir), "success": success}]}
    (episode_dir.parents[4] / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (root / "main" / mission / "onboard.json").write_text("{}", encoding="utf-8")


def test_compare_keeps_unfinished_out_of_denominator_and_renders_json(tmp_path, capsys):
    arm_a = tmp_path / "arm-a"
    arm_b = tmp_path / "arm-b"
    _episode(arm_a, "finished", status="complete", failure="none", alive=False,
             success=True, metrics={"frame_rate_hz": 20.0})
    steps = next((arm_a / "main" / "finished").glob("campaign/*/episodes/*")) / "steps.jsonl"
    steps.write_text('{"propeller_joint_state": {"angles_rad": [1.0]}}\n{"propeller_joint_state": null}\n',
                     encoding="utf-8")
    _episode(arm_a, "running")
    _episode(arm_b, "finished", status="failed", failure="px4_crashed", alive=False,
             success=False, capture=True)
    result = build_comparison([arm_a, arm_b])
    assert result["arms"][0]["finished"] == 1
    finished = result["arms"][0]["missions"]["finished"]
    assert (finished["propeller_frames"], finished["propeller_missing_or_stale"]) == (1, 1)
    assert finished["expected_pass"] is None
    assert result["arms"][0]["total"] == 2
    assert result["arms"][0]["missions"]["running"]["outcome"] == "unfinished"
    assert result["arms"][1]["missions"]["finished"]["stall_capture_count"] == 1
    assert "Per-mission comparison across arms" in render_text(result)
    assert run(["--json", str(arm_a), str(arm_b)]) == 0
    assert json.loads(capsys.readouterr().out)["arms"][0]["name"] == "arm-a"
