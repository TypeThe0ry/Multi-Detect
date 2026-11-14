from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import collect_sim_teacher_episodes as collector


@pytest.fixture(autouse=True)
def memory_environment(monkeypatch):
    monkeypatch.delenv("MD_SIM_CONTAINER_MEMORY_GB", raising=False)
    monkeypatch.delenv("MD_SIM_SERVER_RSS_SOFT_LIMIT_GB", raising=False)
    monkeypatch.delenv("MD_GIMBAL_INVOCATION", raising=False)


def _plan(tmp_path, **kwargs):
    return collector.CollectionPlan(
        "scenario.json", "runtime.json", "truth.json", "hover", 1, 1, 0,
        str(tmp_path), image="sha256:" + "1" * 64, min_free_gb=0, **kwargs,
    )


def _fake_stack(monkeypatch):
    command = ("docker", "run", "-d", "--name", "owned", "image")
    monkeypatch.setattr(
        collector, "build_docker_simulator_plan",
        lambda *_args, **_kwargs: SimpleNamespace(command=command),
    )
    return command


def test_container_memory_is_unchanged_when_unset(tmp_path, monkeypatch):
    _fake_stack(monkeypatch)
    command = collector.docker_command(_plan(tmp_path))
    assert not any(arg.startswith(("--memory=", "--memory-swap=")) for arg in command)


@pytest.mark.parametrize("value", ["64", "40.5"])
def test_container_memory_adds_equal_memory_and_swap_limits(tmp_path, monkeypatch, value):
    _fake_stack(monkeypatch)
    monkeypatch.setenv("MD_SIM_CONTAINER_MEMORY_GB", value)
    command = collector.docker_command(_plan(tmp_path))
    assert command.count(f"--memory={value}g") == 1
    assert command.count(f"--memory-swap={value}g") == 1
    assert command.index(f"--memory={value}g") < command.index("image")


def test_gimbal_invocation_retains_fixed_64g_bound(tmp_path, monkeypatch):
    _fake_stack(monkeypatch)
    monkeypatch.setenv("MD_GIMBAL_INVOCATION", "a" * 32)
    monkeypatch.setenv("MD_SIM_CONTAINER_MEMORY_GB", "48")
    command = collector.docker_command(_plan(tmp_path))
    assert command.count("--memory=64g") == 1
    assert command.count("--memory-swap=64g") == 1
    assert "--memory=48g" not in command
    assert "--label" in command and "md.gimbal.invocation=" + "a" * 32 in command


@pytest.mark.parametrize("value", ["", "0", "-1", "nan", "inf", "64g", "1 --privileged"])
def test_container_memory_rejects_invalid_limits(tmp_path, monkeypatch, value):
    _fake_stack(monkeypatch)
    monkeypatch.setenv("MD_SIM_CONTAINER_MEMORY_GB", value)
    with pytest.raises(ValueError, match="MD_SIM_CONTAINER_MEMORY_GB"):
        collector.docker_command(_plan(tmp_path))


def test_server_rss_sampler_records_server_only_and_sim_clock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda command, **kwargs: (calls.append(command) or SimpleNamespace(
            returncode=0,
            stdout=(
                "PID RSS ARGS\n"
                "10 1024 gz sim -s /world/city.sdf\n"
                "11 2048 /usr/bin/gz-sim-server --headless-rendering\n"
                "12 999 sh -c gz sim -s /world/city.sdf\n"
                "13 888 gz topic -e /camera\n"
                "14 777 frame-server --gz-sim-server\n"
            ),
            stderr="",
        )),
    )
    monkeypatch.setenv("MD_SIM_SERVER_RSS_SOFT_LIMIT_GB", "4")
    sampler = collector.ServerRssSampler(SimpleNamespace(), "sim", tmp_path)
    sampler.sim_clock_provider = lambda: 1234
    row = sampler._sample_once()
    assert calls == [["docker", "exec", "sim", "ps", "-eo", "pid=,rss=,args="]]
    assert row["rss_mb"] == pytest.approx(3.0)
    assert row["processes"] == 2
    assert row["px4_sim_time_boot_ms"] == 1234
    saved = json.loads((tmp_path / "server_rss.jsonl").read_text().splitlines()[0])
    assert saved["rss_mb"] == pytest.approx(3.0)


def test_server_rss_sampler_failed_subprocess_is_recorded_and_nonfatal(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable"),
    )
    sampler = collector.ServerRssSampler(SimpleNamespace(), "sim", tmp_path)
    row = sampler._sample_once()
    assert row["rss_mb"] is None
    assert "daemon unavailable" in row["error"]
    assert (tmp_path / "server_rss.jsonl").is_file()
    sampler.check()


def test_server_rss_soft_limit_latches_after_high_sample(tmp_path, monkeypatch):
    monkeypatch.setenv("MD_SIM_SERVER_RSS_SOFT_LIMIT_GB", "1")
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
    outputs = iter([
        f"1 {2 * 1024 * 1024} gz sim -s world\n",
        "1 10 gz sim -s world\n",
    ])
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(outputs), stderr=""),
    )
    sampler = collector.ServerRssSampler(SimpleNamespace(), "sim", tmp_path)
    sampler._sample_once()
    with pytest.raises(collector._EpisodeEarlyStop) as raised:
        sampler.check()
    assert raised.value.reason == "server_rss_soft_limit"
    sampler._sample_once()
    with pytest.raises(collector._EpisodeEarlyStop, match="server_rss_soft_limit"):
        sampler.check()


def test_server_rss_sampler_file_write_failure_does_not_abort(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="1 10 gz sim -s world\n", stderr=""
        ),
    )
    original_open = Path.open
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")))
    sampler = collector.ServerRssSampler(SimpleNamespace(), "sim", tmp_path)
    row = sampler._sample_once()
    assert row["rss_mb"] == pytest.approx(10 / 1024)
    monkeypatch.setattr(Path, "open", original_open)


def test_episode_resource_guard_uses_sim_clock_not_wall_time(tmp_path, monkeypatch):
    monkeypatch.delenv("MD_SIM_SERVER_RSS_SOFT_LIMIT_GB", raising=False)
    sampler = collector.ServerRssSampler(SimpleNamespace(), "sim", tmp_path)
    guard = collector.EpisodeResourceGuard(sampler, max_sim_s=5)
    clock = iter([1000, 2000, 5999, 6000]).__next__
    guard.sim_clock_provider = clock
    guard.check()
    guard.check()
    guard.check()
    with pytest.raises(collector._EpisodeEarlyStop) as raised:
        guard.check()
    assert raised.value.reason == "max_episode_sim_seconds"
    assert raised.value.details["limit_sim_seconds"] == 5


def test_episode_resource_guard_ignores_unset_cap_and_invalid_clock_then_resets(tmp_path):
    sampler = collector.ServerRssSampler(SimpleNamespace(), "sim", tmp_path)
    guard = collector.EpisodeResourceGuard(sampler, max_sim_s=None)
    guard.sim_clock_provider = lambda: None
    guard.check()
    assert guard.sim_started_ms is None
    # A new guard represents a new episode and gets an independent clock origin.
    second = collector.EpisodeResourceGuard(sampler, max_sim_s=1)
    second.sim_clock_provider = lambda: 5000
    second.check()
    assert second.sim_started_ms == 5000


def test_parser_accepts_max_episode_sim_seconds():
    args = collector.build_parser().parse_args([
        "--scenario", "scenario.json", "--teacher", "hover", "--episodes", "1",
        "--steps-per-episode", "1", "--out", "out", "--max-episode-sim-seconds", "42",
    ])
    assert args.max_episode_sim_seconds == 42
