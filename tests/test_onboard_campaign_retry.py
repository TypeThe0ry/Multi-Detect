from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from scripts import collect_sim_teacher_episodes as collector
from scripts import eval_onboard_loop_campaign as campaign
from scripts import run_mission_suite as suite
from scripts.train_policy_v2_bc import _real_depth


@pytest.fixture
def tmp_path():
    path = campaign.ROOT / ".pytest-campaign-retry" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def recording(
    out: Path, name: str, status: str | None, *, steps: bool = True, step_count: int = 1
) -> Path:
    episode = out / "episodes" / name
    episode.mkdir(parents=True)
    if status is not None:
        (episode / "episode.json").write_text(
            json.dumps({"status": status, "step_count": step_count}), encoding="utf-8"
        )
    if steps:
        rows = [{"step_index": index} for index in range(step_count)]
        (episode / "steps.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
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
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test",
                                              "task_text": "find", "target_combo": "x"}]}))

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


def test_campaign_collector_failure_is_loud_only_in_strict_mode(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test",
                                              "task_text": "find", "target_combo": "x"}]}))
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: None)
    monkeypatch.setattr(
        campaign.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=7)
    )
    (tmp_path / "test.truth.json").write_text("{}")
    assert campaign.main(["--index", str(index), "--out", str(tmp_path / "out")]) == 0
    with pytest.raises(RuntimeError, match="Collector failed for test: exit 7"):
        campaign.main(["--index", str(index), "--out", str(tmp_path / "out2"), "--strict"])


def test_complete_requires_nonempty_parseable_consistent_steps(tmp_path):
    episode = recording(tmp_path, "ep", "complete", step_count=2)
    assert campaign.episode_is_complete(episode)
    (episode / "steps.jsonl").write_text('{"step_index": 0}\n', encoding="utf-8")
    assert not campaign.episode_is_complete(episode)
    (episode / "steps.jsonl").write_text('{"step_index": 0}\nnot-json\n', encoding="utf-8")
    assert not campaign.episode_is_complete(episode)


def test_retry_preserves_original_seed_and_does_not_reuse_complete_seed(tmp_path, monkeypatch):
    out = tmp_path / "out" / "test"
    recording(out, "episode-000000-complete", "complete")
    complete_meta = out / "episodes" / "episode-000000-complete" / "episode.json"
    complete_meta.write_text(
        json.dumps({"status": "complete", "step_count": 1, "seed": 42}), encoding="utf-8"
    )
    failed = recording(out, "episode-000001-failed", "failed")
    (failed / "episode.json").write_text(
        json.dumps({"status": "failed", "seed": 99, "episode_index": 1}), encoding="utf-8"
    )
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test",
                                              "task_text": "find", "target_combo": "x"}]}))
    (tmp_path / "test.truth.json").write_text("{}")
    seeds = []

    def run(command, **kwargs):
        seeds.append(command[command.index("--seed") + 1])
        recording(Path(command[command.index("--out") + 1]), f"new-{len(seeds)}", "complete")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: None)
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
    assert (
        campaign.main(
            [
                "--index",
                str(index),
                "--out",
                str(tmp_path / "out"),
                "--episodes",
                "2",
                "--retry-failed-episodes",
            ]
        )
        == 0
    )
    assert seeds == ["99"]


def test_initial_low_disk_abort_persists_seed_and_retry_reuses_it(tmp_path, monkeypatch):
    campaign_out = tmp_path / "campaign"
    plan = SimpleNamespace(out=campaign_out / "test", min_free_gb=15.0, seed=100)
    aborted = collector.run_episode(plan, Path("unused"), 0, free_space_fn=lambda _: 1)
    metadata = json.loads((aborted / "episode.json").read_text(encoding="utf-8"))
    assert metadata["episode_index"] == 0
    assert metadata["seed"] == 100

    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test"}]}))
    (tmp_path / "test.truth.json").write_text("{}")
    seeds = []

    def run(command, **kwargs):
        seeds.append(command[command.index("--seed") + 1])
        recording(Path(command[command.index("--out") + 1]), "replacement", "complete")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: None)
    monkeypatch.setattr(
        campaign,
        "score_episode",
        lambda *args, **kwargs: {"status": "complete", "success": False, "false_confirm": False,
                                 "task_equivalent": False, "confirmed": False},
    )
    assert campaign.main([
        "--index", str(index), "--out", str(campaign_out), "--episodes", "1",
        "--retry-failed-episodes",
    ]) == 0
    assert seeds == ["100"]


def test_zero_exit_abort_does_not_fill_missing_and_reports_low_disk(tmp_path, monkeypatch, capsys):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test"}]}))
    (tmp_path / "test.truth.json").write_text("{}")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        out = Path(command[command.index("--out") + 1])
        recording(out, "aborted", "aborted_low_disk", steps=False)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: None)
    assert campaign.main(
        ["--index", str(index), "--out", str(tmp_path / "out"), "--episodes", "1"]
    ) == 0
    assert len(calls) == 1
    assert "not filled: low disk" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="not filled: low disk"):
        campaign.main([
            "--index", str(index), "--out", str(tmp_path / "strict-out"),
            "--episodes", "1", "--strict"
        ])


def test_complete_replacement_reduces_missing(tmp_path, monkeypatch):
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"worlds": [{"world_id": "test", "split": "test"}]}))
    (tmp_path / "test.truth.json").write_text("{}")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        out = Path(command[command.index("--out") + 1])
        count = int(command[command.index("--episodes") + 1])
        for i in range(count):
            recording(out, f"complete-{len(calls)}-{i}", "complete")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(campaign.subprocess, "run", run)
    monkeypatch.setattr(campaign, "ensure_free_disk", lambda *args: None)
    monkeypatch.setattr(
        campaign, "score_episode",
        lambda *args, **kwargs: {"status": "complete", "success": False, "false_confirm": False,
                                 "task_equivalent": False, "confirmed": False},
    )
    assert campaign.main([
        "--index", str(index), "--out", str(tmp_path / "out"), "--episodes", "2",
        "--disk-check-every", "2",
    ]) == 0
    assert len(calls) == 1


def test_training_admission_normalizes_backslash_depth_paths(tmp_path):
    episode = tmp_path / "episode"
    depth = episode / "depth"
    depth.mkdir(parents=True)
    np.save(depth / "before.npy", np.ones((2, 2), dtype=np.float32))
    assert _real_depth(episode, "depth\\before.npy")
