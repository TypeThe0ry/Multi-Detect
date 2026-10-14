from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from scripts import run_mission_suite as suite


@pytest.fixture
def work_tmp():
    path = suite.ROOT / ".pytest-mission-suite" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _mission(**overrides):
    mission = {
        "id": "count-cones",
        "task_text": "Count cones at the site.",
        "index": "configs/scenarios/generated-count-site/index.json",
        "world": "gen_cnt2_w0000",
        "refs": {"zone": "site"},
        "ref_labels": {"zone": "site"},
        "expect": {"skills": ["survey", "count"], "min_alert_precision": 0.8},
        "steps": 42,
        "max_episode_s": 120,
    }
    mission.update(overrides)
    return mission


def test_merge_config_deep_copies_and_excludes_suite_metadata():
    defaults = {
        "gguf": "model.gguf",
        "mission": "from_text",
        "plan_sorted_keys": True,
        "refs": {"base": "default"},
        "ref_labels": {"base": "Base"},
    }
    mission = _mission(refs={"zone": "site", "nested": {"x": 1}}, ref_labels={"zone": "Zone"})
    merged = suite.merge_config(defaults, mission)

    assert merged["gguf"] == "model.gguf"
    assert merged["mission"] == "from_text"
    assert merged["refs"] == {"base": "default", "zone": "site", "nested": {"x": 1}}
    assert merged["ref_labels"] == {"base": "Base", "zone": "Zone"}
    excluded = ("id", "index", "world", "expect", "steps", "max_episode_s")
    assert all(key not in merged for key in excluded)
    mission["refs"]["nested"]["x"] = 9
    assert merged["refs"]["nested"]["x"] == 1


def test_build_command_uses_same_interpreter_and_absolute_paths(work_tmp):
    mission = _mission()
    command = suite.build_command(
        mission,
        work_tmp / "onboard.json",
        work_tmp / "campaign",
        work_tmp / "report.json",
        episodes=3,
    )

    assert command[0] == sys.executable
    assert command[1] == str(suite.ROOT / "scripts/eval_onboard_loop_campaign.py")
    assert command[command.index("--index") + 1] == str((suite.ROOT / mission["index"]).resolve())
    assert command[command.index("--split") + 1] == "test"
    assert command[command.index("--worlds") + 1] == mission["world"]
    assert command[command.index("--episodes") + 1] == "3"
    assert command[command.index("--steps") + 1] == "42"
    assert command[command.index("--max-episode-s") + 1] == "120"


def test_build_table_maps_metrics_and_missing_episode_is_failure():
    mission = _mission()
    report = {
        "episodes": [
            {
                "world": mission["world"],
                "status": "complete",
                "mission_plan_skills": ["survey", "count"],
                "mission_completed": True,
                "mission_alert_precision": 0.9,
                "mission_alert_recall": 0.75,
                "expected_count": 4,
                "reported_count": 4,
                "matched_count": 4,
                "false_findings": 0,
                "mission_inspect_match_fraction": 1.0,
                "mission_survey_coverage": {"site": 0.8},
                "steps": 42,
            }
        ]
    }
    table = suite.build_table([mission], {mission["id"]: report})
    row = table["missions"][0]
    assert row["plan_match"] is True
    assert row["mission_completed"] is True
    assert row["alert_precision"] == pytest.approx(0.9)
    assert row["alert_recall"] == pytest.approx(0.75)
    assert row["expected_count"] == 4
    assert row["reported_count"] == 4
    assert row["matched_count"] == 4
    assert row["false_findings"] == 0
    assert row["inspect_match_fraction"] == pytest.approx(1.0)
    assert row["survey_coverage"] == {"site": 0.8}
    assert table["summary"]["plan_accuracy"] == 1.0
    assert table["summary"]["completion_rate"] == 1.0

    missing = suite.build_table([_mission(id="missing", world="gen_cnt2_w0001")], {})
    missing_row = missing["missions"][0]
    assert missing_row["status"] == "missing"
    assert missing_row["plan_match"] is False
    assert missing_row["mission_completed"] is False
    assert missing["summary"]["plan_accuracy"] == 0.0
    assert missing["summary"]["completion_rate"] == 0.0


def _write_suite(path: Path):
    payload = {
        "defaults": {"gguf": "model.gguf", "plan_sorted_keys": True},
        "missions": [_mission(), _mission(id="count-hydrants", world="gen_cnt2_w0001")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_main_only_filters_and_runner_receives_expected_command(work_tmp):
    suite_path = work_tmp / "suite.json"
    _write_suite(suite_path)
    calls = []

    def fake_runner(command, *, cwd, check):
        calls.append((command, cwd, check))
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(json.dumps({"episodes": []}), encoding="utf-8")

    code = suite.main(
        ["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--only", "count-hydrants"],
        runner=fake_runner,
    )
    assert code == 1  # no episode was reported, so the selected mission is a failure
    assert len(calls) == 1
    assert calls[0][1] == suite.ROOT
    assert calls[0][2] is True
    assert "gen_cnt2_w0001" in calls[0][0]


def test_main_dry_run_writes_configs_and_does_not_call_runner(work_tmp, capsys):
    suite_path = work_tmp / "suite.json"
    _write_suite(suite_path)
    calls = []

    def fake_runner(*args, **kwargs):
        calls.append((args, kwargs))

    out = work_tmp / "dry-run"
    args = ["--suite", str(suite_path), "--out", str(out), "--dry-run"]
    assert suite.main(args, runner=fake_runner) == 0
    assert calls == []
    assert (out / "count-cones" / "onboard.json").is_file()
    assert (out / "count-hydrants" / "onboard.json").is_file()
    assert not (out / "suite-report.json").exists()
    output = capsys.readouterr().out
    assert '"command"' in output
    assert '"config"' in output
