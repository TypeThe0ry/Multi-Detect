from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import eval_onboard_loop_campaign as campaign
from scripts import run_mission_suite as suite


@pytest.fixture
def tmp_path():
    path = campaign.ROOT / ".pytest-campaign-retry" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def recording(out: Path, name: str, status: str | None, *, steps: bool = True) -> Path:
    episode = out / "episodes" / name
    episode.mkdir(parents=True)
    if status is not None:
        (episode / "episode.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    if steps:
        (episode / "steps.jsonl").write_text("", encoding="utf-8")
    return episode


def test_retry_selection_preserves_completed_and_archives_incomplete(tmp_path):
    complete = recording(tmp_path, "complete", "complete")
    failed = recording(tmp_path, "failed", "failed")
    missing = recording(tmp_path, "missing", None)
    no_steps = recording(tmp_path, "no-steps", "complete", steps=False)
    malformed = recording(tmp_path, "malformed", None)
    (malformed / "episode.json").write_text("{", encoding="utf-8")
    null_metadata = recording(tmp_path, "null", None)
    (null_metadata / "episode.json").write_text("null", encoding="utf-8")

    archived = campaign.retry_failed_episodes(tmp_path)

    assert complete.is_dir()
    assert len(archived) == 5
    assert all(path.is_dir() and path.parent.name == "retry-history" for path in archived)
    assert all(not path.exists() for path in (failed, missing, no_steps, malformed, null_metadata))
    assert campaign.retry_failed_episodes(tmp_path) == []


def test_campaign_retries_same_output_without_duplicating_completed(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(
        json.dumps(
            {
                "worlds": [
                    {
                        "world_id": "test",
                        "split": "test",
                        "task_text": "find",
                        "target_combo": "x",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "test.truth.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "out"
    complete = recording(out / "test", "complete", "complete")
    recording(out / "test", "failed", "failed")
    calls, guards = [], []

    def run(command, **kwargs):
        calls.append(command)
        assert command[command.index("--episodes") + 1] == "1"
        recording(Path(command[command.index("--out") + 1]), f"new-{len(calls)}", "complete")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: guards.append(args))
    monkeypatch.setattr(
        campaign,
        "score_episode",
        lambda *args, **kwargs: {
            "status": "complete",
            "success": False,
            "false_confirm": False,
            "task_equivalent": False,
            "confirmed": False,
        },
    )
    arguments = [
        "--index",
        str(index),
        "--out",
        str(out),
        "--report",
        str(tmp_path / "report.json"),
        "--episodes",
        "3",
        "--retry-failed-episodes",
        "--min-free-disk-gb",
        "30",
    ]
    assert campaign.main(arguments) == 0
    assert complete.is_dir()
    assert len(calls) == len(guards) == 2
    assert len(list((out / "test/episodes").iterdir())) == 3
    assert campaign.main(arguments) == 0
    assert len(calls) == 2


def test_campaign_stops_on_disk_guard_before_collector(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test"}]}))

    def no_disk(*args):
        raise RuntimeError("free disk")

    monkeypatch.setattr(campaign, "ensure_free_disk", no_disk)
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *args, **kwargs: pytest.fail("collector ran")
    )
    with pytest.raises(RuntimeError, match="free disk"):
        campaign.main(["--index", str(index), "--out", str(tmp_path / "out")])


def test_suite_forwards_retry_and_disk_options(tmp_path):
    command = suite.build_command(
        {"index": "index.json", "world": "test", "steps": 1, "max_episode_s": 10},
        tmp_path / "config.json",
        tmp_path / "out",
        tmp_path / "report.json",
        retry_failed_episodes=True,
        min_free_disk_gb=30,
        disk_check_every=5,
    )
    assert "--retry-failed-episodes" in command
    assert command[command.index("--min-free-disk-gb") + 1] == "30"
    assert command[command.index("--disk-check-every") + 1] == "5"


def test_campaign_collector_failure_is_loud(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test"}]}))
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: None)
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7)
    )
    with pytest.raises(RuntimeError, match="Collector failed for test: exit 7"):
        campaign.main(["--index", str(index), "--out", str(tmp_path / "out")])
