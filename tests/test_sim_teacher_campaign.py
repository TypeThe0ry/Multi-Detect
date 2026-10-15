from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.run_sim_teacher_campaign import (
    cleanup_leftover_containers,
    run_campaign,
)


def test_campaign_runs_one_episode_and_writes_summary(tmp_path: Path) -> None:
    scenario = tmp_path / "gen_w0000.scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if "audit_sim_teacher_dataset.py" in command[1]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"files": {}, "step_count": 3, "sim_skew_ms": {"max_abs": 44.0}}),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "collector ok", "")

    result = run_campaign(
        [scenario],
        teacher="expert_search",
        episodes_per_scenario=1,
        steps_per_episode=3,
        seed_base=100,
        out=tmp_path / "campaign",
        max_episode_s=12,
        max_retries=0,
        command_runner=fake_run,
        cleanup=lambda: None,
        clock=iter([0.0, 2.5]).__next__,
        python_executable="python",
    )
    assert result["summary"]["total_transitions"] == 3
    assert result["episodes"][0]["status"] == "success"
    collector = calls[0]
    assert collector[collector.index("--episodes") + 1] == "1"
    assert collector[collector.index("--seed") + 1] == "100"
    assert (tmp_path / "campaign" / "campaign.json").is_file()


def test_campaign_retries_infrastructure_failure(tmp_path: Path) -> None:
    scenario = tmp_path / "urban.scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    attempts = 0

    def fake_run(command, **kwargs):
        nonlocal attempts
        if "audit_sim_teacher_dataset.py" in command[1]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"files": {}, "step_count": 1}), ""
            )
        attempts += 1
        if attempts == 1:
            return subprocess.CompletedProcess(command, 1, "", "EKF heartbeat timeout")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_campaign(
        [scenario],
        teacher="explore",
        episodes_per_scenario=1,
        steps_per_episode=1,
        seed_base=0,
        out=tmp_path / "out",
        max_retries=1,
        command_runner=fake_run,
        cleanup=lambda: None,
    )
    assert attempts == 2
    assert result["episodes"][0]["attempts"] == 2
    assert result["episodes"][0]["status"] == "success"


def test_campaign_skips_existing_audit(tmp_path: Path) -> None:
    scenario = tmp_path / "gen_w0001.scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    dataset = tmp_path / "out" / "gen_w0001" / "7"
    dataset.mkdir(parents=True)
    (dataset / "audit.json").write_text(
        json.dumps({"files": {}, "step_count": 2}), encoding="utf-8"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "audit_sim_teacher_dataset.py" in command[1]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"files": {}, "step_count": 2}), ""
            )
        raise AssertionError("collector must not run for an audited episode")

    result = run_campaign(
        [scenario],
        teacher="expert_search",
        episodes_per_scenario=1,
        steps_per_episode=1,
        seed_base=7,
        out=tmp_path / "out",
        command_runner=fake_run,
        cleanup=lambda: None,
    )
    assert len(calls) == 1
    assert result["episodes"][0]["status"] == "skipped"


def test_campaign_does_not_skip_aborted_and_excludes_it_from_rate(tmp_path: Path) -> None:
    scenario = tmp_path / "gen_abort.scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    dataset = tmp_path / "out" / "gen_abort" / "7"
    episode = dataset / "episodes" / "episode-0000"
    episode.mkdir(parents=True)
    (episode / "episode.json").write_text(
        json.dumps({"status": "aborted_low_disk", "failure": "floor"}), encoding="utf-8"
    )
    (episode / "steps.jsonl").write_text("", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "audit_sim_teacher_dataset.py" in command[1]:
            raise AssertionError("aborted dataset must not be resumed as skipped")
        return subprocess.CompletedProcess(command, 0, "aborted_low_disk", "")

    result = run_campaign(
        [scenario], teacher="explore", episodes_per_scenario=1, steps_per_episode=1,
        seed_base=7, out=tmp_path / "out", max_retries=0,
        command_runner=fake_run, cleanup=lambda: None,
    )
    assert result["episodes"][0]["status"] == "aborted_low_disk"
    assert result["summary"]["success_rate"] == 0.0
    assert result["summary"]["aborted_low_disk"] == 1


def test_cleanup_removes_matching_containers() -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(command, 0, "abc\ndef\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    assert cleanup_leftover_containers(fake_run) == ["abc", "def"]
    assert calls[1][0:3] == ["docker", "rm", "-f"]


def test_mixed_split_and_acquisition_from_expert_labels(tmp_path: Path) -> None:
    scenario = tmp_path / "gen_w0002.scenario.json"
    scenario.write_text("{}", encoding="utf-8")
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps({"worlds": [{"world_id": "gen_w0002", "split": "val"}]}),
        encoding="utf-8",
    )
    teachers = []

    def fake_run(command, **kwargs):
        if "audit_sim_teacher_dataset.py" in command[1]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"files": {}, "step_count": 2}), ""
            )
        teachers.append(command[command.index("--teacher") + 1])
        dataset = Path(command[command.index("--out") + 1])
        steps = dataset / "episodes" / "episode-0000" / "steps.jsonl"
        steps.parent.mkdir(parents=True, exist_ok=True)
        labels = (
            [
                {"labels": {"target_in_fov": True}},
                {"labels": {"target_in_fov": False}},
            ]
            if teachers[-1] == "expert_search"
            else [{"labels": {}}]
        )
        steps.write_text("\n".join(json.dumps(row) for row in labels), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_campaign(
        [scenario],
        teacher="mixed",
        episodes_per_scenario=2,
        steps_per_episode=2,
        seed_base=10,
        out=tmp_path / "out",
        split_from_index=index,
        command_runner=fake_run,
        cleanup=lambda: None,
    )
    assert teachers == ["expert_search", "explore"]
    assert result["summary"]["counts_by_split"] == {"val": {"success": 2}}
    assert result["summary"]["acquisition_rate"] == 0.5
    assert result["summary"]["total_transitions"] == 4
