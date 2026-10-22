from __future__ import annotations

from types import SimpleNamespace

from scripts import collect_sim_teacher_episodes as collector


def test_parser_accepts_attach_container_and_task_text():
    args = collector.build_parser().parse_args(
        [
            "--scenario",
            "scenario.json",
            "--teacher",
            "onboard",
            "--episodes",
            "1",
            "--steps-per-episode",
            "1",
            "--out",
            "out",
            "--attach-container",
            "session-container",
            "--task-text",
            "Count cones",
        ]
    )
    assert args.attach_container == "session-container"
    assert args.task_text == "Count cones"


def test_attached_environment_does_not_start_or_remove(monkeypatch):
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(collector.subprocess, "run", run)
    plan = collector.CollectionPlan(
        "scenario",
        "runtime",
        "truth",
        "onboard",
        1,
        1,
        0,
        "out",
        attach_container="session-container",
        max_episode_s=1.0,
    )
    with collector.DockerSimEnvironment(plan, "episode") as environment:
        assert environment.name == "session-container"
        assert environment.attached
        assert not environment.started
    assert calls == [["docker", "inspect", "--format", "{{.State.Running}}", "session-container"]]


def test_parser_rejects_sim_param_without_value():
    import argparse

    import pytest

    base = [
        "--scenario",
        "s.json",
        "--teacher",
        "onboard",
        "--episodes",
        "1",
        "--steps-per-episode",
        "1",
        "--out",
        "o",
    ]
    args = collector.build_parser().parse_args([*base, "--sim-param", "SIM_BAT_MIN_PCT=95"])
    assert args.sim_param == ["SIM_BAT_MIN_PCT=95"]
    for bad in ("SIM_BAT_MIN_PCT", "=95", "SIM_BAT_MIN_PCT="):
        with pytest.raises((SystemExit, argparse.ArgumentError)):
            collector.build_parser().parse_args([*base, "--sim-param", bad])
