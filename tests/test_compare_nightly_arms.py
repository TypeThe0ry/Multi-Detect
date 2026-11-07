import json
from pathlib import Path

from scripts.compare_nightly_arms import build_comparison, render_markdown, render_text, run


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


def _review_mission(root: Path, suite: str, mission: str, *, passed: bool,
                    event: str | None = None, count=(3, 4), false_findings=1):
    suite_dir = root / suite
    episode_dir = suite_dir / mission / "campaign" / "world" / "episodes" / "episode-0000-fixture"
    episode_dir.mkdir(parents=True)
    (suite_dir / mission / "onboard.json").write_text("{}", encoding="utf-8")
    (episode_dir / "episode.json").write_text(json.dumps({
        "status": "complete", "created_wall_time": 10, "finished_wall_time": 20,
        "failure": None,
    }), encoding="utf-8")
    (episode_dir / "mission_summary.json").write_text(json.dumps({
        "counts": [{"step": "s1", "value": count[1]}],
    }), encoding="utf-8")
    (episode_dir / "steps.jsonl").write_text(json.dumps({
        "obs_before": {"pose_time_boot_ms": 1000, "propeller_joint_state": {}},
        "obs_after": {"pose_time_boot_ms": 5000},
    }) + "\n", encoding="utf-8")
    campaign = suite_dir / mission / "campaign"
    campaign.mkdir(exist_ok=True)
    if event:
        (campaign / "world.log").write_text(json.dumps({"event": event}) +
            "\nAccel #0 fail: TIMEOUT!\nGyro #1 fail: TIMEOUT!\nMAG #0 fail: TIMEOUT!\n", encoding="utf-8")
    (episode_dir / "px4-clock-stall-capture.json").write_text("{}", encoding="utf-8")
    report = {
        "summary": {"expectation_pass_rate": 1.0 if passed else 0.0},
        "missions": [{"id": mission, "world": "world", "episode": 1,
                      "status": "complete", "mission_completed": True,
                      "expectations_passed": passed,
                      "expectation_checks": {"min_count_exact": passed},
                      "expected_count": count[0], "reported_count": count[1],
                      "false_findings": false_findings}],
    }
    report_path = suite_dir / "suite-report.json"
    if report_path.is_file():
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        report["missions"] = previous.get("missions", []) + report["missions"]
        report["summary"]["expectation_pass_rate"] = sum(bool(row.get("expectations_passed")) for row in report["missions"]) / len(report["missions"])
    report_path.write_text(json.dumps(report), encoding="utf-8")


def test_chinese_review_summarizes_suites_events_counts_and_arm_diff(tmp_path, capsys):
    previous, current = tmp_path / "previous", tmp_path / "current"
    _review_mission(previous, "main", "count-cones", passed=False)
    _review_mission(current, "main", "count-cones", passed=True,
                    event="climb_failed_suspected_loss_of_control")
    _review_mission(current, "gimbal", "gimbal-count-cones", passed=True,
                    event="sensor_stream_stalled")
    _review_mission(current, "main", "loss-control", passed=False,
                    event="vehicle_lost_control")
    result = build_comparison([previous, current])
    current_arm = result["arms"][1]
    assert current_arm["suites"]["main"]["expected_pass"] is False
    assert current_arm["suites"]["gimbal"]["failure_classes"] == ["sensor_stream_stalled"]
    mission = current_arm["missions"]["count-cones"]
    assert mission["failure_class"] == "climb_failed_suspected_loss_of_control"
    assert (mission["count_expected"], mission["count_reported"], mission["false_findings"]) == (3, 4, 1)
    assert mission["rtf"] == 0.4
    assert mission["stall_capture_count"] == 1
    assert mission["timeout_counts"] == {"Accel": 1, "Gyro": 1, "MAG": 1}
    assert current_arm["missions"]["loss-control"]["failure_class"] == "vehicle_lost_control"
    markdown = render_markdown(result)
    assert "与上一架次对比" in markdown
    assert "no → yes" in markdown
    assert "climb_failed_suspected_loss_of_control" in markdown
    assert run(["--output", str(tmp_path / "review.md"), str(current)]) == 0
    assert "夜间回归复核" in (tmp_path / "review.md").read_text(encoding="utf-8")


def test_climb_failure_class_from_generated_supervisor_evidence(tmp_path):
    from scripts.run_onboard_fault_campaign import run_climb_failure_fixture

    fixture = run_climb_failure_fixture(tmp_path / "fixture")
    assert fixture["passed"]
    arm = tmp_path / "arm"
    _review_mission(arm, "main", "climb-fixture", passed=False)
    # Episode metadata is synthetic; the failure event itself must come from
    # the production policy/supervisor chain, not a hand-written marker.
    (arm / "main/climb-fixture/campaign/world.log").write_text(
        Path(fixture["supervisor_log"]).read_text(encoding="utf-8"), encoding="utf-8"
    )
    mission = build_comparison([arm])["arms"][0]["missions"]["climb-fixture"]
    assert mission["failure_class"] == fixture["final_summary"]["failure_class"]
    assert mission["failure_events"] == ["climb_failed_suspected_loss_of_control"]
