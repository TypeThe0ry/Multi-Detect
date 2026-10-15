from __future__ import annotations

import json
import shutil
import subprocess
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


def test_listed_alternative_plan_passes_all_expectations():
    mission = _mission(
        expect={
            "skills": ["area_search", "land"],
            "alternatives": [["area_search", "record", "land"]],
        }
    )
    episode = {
        "world": mission["world"],
        "status": "complete",
        "mission_plan_skills": ["area_search", "record", "land"],
        "mission_completed": True,
    }
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["plan_match"] is True
    assert row["expectation_checks"] == {"skills": True}
    assert row["expectations_passed"] is True


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


def _supervised_mission():
    return _mission(
        expect={
            "skills": ["monitor", "return_home"],
            "supervisor": {
                "final_decision": "return_now",
                "reason": "explicit operator abort",
                "return_home_started": True,
                "onboard_log_exists": True,
                "logged_decisions": ["continue", "return_now"],
                "logged_reasons": ["explicit operator abort"],
                "abort_hover_before_return": True,
                "original_mission_completed": False,
            },
        }
    )


def _supervised_episode(mission):
    return {
        "world": mission["world"],
        "status": "complete",
        "mission_plan_skills": ["monitor", "return_home"],
        "mission_supervisor": {
            **mission["expect"]["supervisor"],
            "logged_decisions": ["continue", "hold", "return_now"],
        },
    }


def test_supervisor_expectations_check_durable_decisions_and_recovery():
    mission = _supervised_mission()
    episode = _supervised_episode(mission)
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["expectations_passed"] is True
    assert row["expectation_checks"]["supervisor.original_mission_completed"] is True
    assert row["supervisor"]["original_mission_completed"] is False
    markdown = suite.render_markdown(
        {
            "summary": {
                "missions": 1,
                "episodes": 1,
                "plan_accuracy": 1,
                "completion_rate": 0,
                "expectation_pass_rate": 1,
            },
            "missions": [row],
        }
    )
    assert "Supervisor outcomes" in markdown
    assert "explicit operator abort" in markdown


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("final_decision", "continue"),
        ("reason", "operator link-loss policy"),
        ("return_home_started", False),
        ("onboard_log_exists", False),
        ("logged_decisions", ["continue"]),
        ("logged_reasons", []),
        ("abort_hover_before_return", False),
        ("original_mission_completed", True),
    ],
)
def test_supervisor_expectation_failures_are_visible(field, value):
    mission = _supervised_mission()
    episode = _supervised_episode(mission)
    episode["mission_supervisor"][field] = value
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["expectation_checks"][f"supervisor.{field}"] is False
    assert row["expectations_passed"] is False


def test_missing_supervisor_evidence_fails_even_false_expectations():
    mission = _supervised_mission()
    episode = _supervised_episode(mission)
    episode.pop("mission_supervisor")
    row = suite.build_table([mission], {mission["id"]: {"episodes": [episode]}})["missions"][0]
    assert row["expectations_passed"] is False
    assert not any(v for k, v in row["expectation_checks"].items() if k.startswith("supervisor."))


def test_v2_retains_the_eight_v1_missions_and_adds_six_packages(work_tmp, capsys):
    v1 = json.loads((suite.ROOT / "configs/onboard/mission-suite-v1.json").read_text("utf-8"))
    v2 = json.loads((suite.ROOT / "configs/onboard/mission-suite-v2.json").read_text("utf-8"))
    assert v2["defaults"] == v1["defaults"]
    assert v2["missions"][:8] == v1["missions"]
    assert len(v2["missions"]) == 14
    assert all("mission_package" in m for m in v2["missions"][8:])
    count = v2["missions"][8]
    assert count["sim_ground_events"] == []
    assert count["mission_package"]["link_loss_policy"] == {"action": "continue"}
    assert suite.main(["--out", str(work_tmp / "default-v2"), "--dry-run"]) == 0
    assert len(list((work_tmp / "default-v2").glob("*/onboard.json"))) == 14
    capsys.readouterr()


@pytest.mark.parametrize("supervised", [False, True])
def test_expectation_failure_exit_preserves_legacy_behavior(work_tmp, supervised):
    mission = _supervised_mission() if supervised else _mission()
    payload = {"defaults": {}, "missions": [mission]}
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps(payload), encoding="utf-8")

    def fake_runner(command, **kwargs):
        episode = {"world": mission["world"], "status": "complete"}
        Path(command[command.index("--report") + 1]).write_text(
            json.dumps({"episodes": [episode]}), encoding="utf-8"
        )

    # The historical default only treats a missing scored row as a failing
    # suite exit.  Failed expectations remain visible in the report but do
    # not change that default status.
    assert (
        suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out")], runner=fake_runner)
        == 0
    )
    table = json.loads((work_tmp / "out/suite-report.json").read_text("utf-8"))
    assert ("supervisor" in table["missions"][0]) == supervised
    assert ("Supervisor outcomes" in suite.render_markdown(table)) == supervised


def test_strict_exit_rejects_expectation_failure_and_collector_error(work_tmp):
    mission = _mission()
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")

    def fake_runner(command, **kwargs):
        raise subprocess.CalledProcessError(7, command)

    # Strict mode is opt-in for model-round runs.  It reports the collector
    # failure as nonzero while the default mode above remains legacy-compatible.
    assert (
        suite.main(
            ["--suite", str(suite_path), "--out", str(work_tmp / "strict"), "--strict"],
            runner=fake_runner,
        )
        == 1
    )


def test_build_command_strict_is_explicit():
    mission = _mission()
    base = suite.build_command(mission, Path("onboard.json"), Path("campaign"), Path("report.json"))
    strict = suite.build_command(
        mission, Path("onboard.json"), Path("campaign"), Path("report.json"), strict=True
    )
    assert "--strict" not in base
    assert strict[strict.index("--strict")] == "--strict"


def test_dry_run_rejects_invalid_package_before_writing_config(work_tmp):
    mission = _mission(mission_package={"mission_id": "bad"}, sim_ground_events=[])
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--dry-run"])
    assert exc.value.code == 2
    assert not (work_tmp / "out").exists()


def test_dry_run_rejects_invalid_sim_timeline(work_tmp):
    v2 = json.loads((suite.ROOT / "configs/onboard/mission-suite-v2.json").read_text("utf-8"))
    mission = v2["missions"][8]
    mission["sim_ground_events"] = [{"t_s": -1, "event": "abort"}]
    suite_path = work_tmp / "suite.json"
    suite_path.write_text(json.dumps({"defaults": {}, "missions": [mission]}), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        suite.main(["--suite", str(suite_path), "--out", str(work_tmp / "out"), "--dry-run"])
    assert exc.value.code == 2
    assert not (work_tmp / "out").exists()
