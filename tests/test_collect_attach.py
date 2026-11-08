from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import collect_sim_teacher_episodes as collector
from multidetect.sim_teacher.streams import MavlinkState


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
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")

    def run(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ["docker", "inspect"] and "{{.State.Running}}" in command:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Id": "fake-container-id",
                        "State": {
                            "Running": True,
                            "ExitCode": 0,
                            "OOMKilled": False,
                            "FinishedAt": "0001-01-01T00:00:00Z",
                        },
                    }
                ),
                stderr="",
            )
        if command[:2] == ["docker", "top"]:
            return SimpleNamespace(returncode=0, stdout="PID COMMAND\n1234 px4\n", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
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
    assert calls == [
        ["docker", "inspect", "--format", "{{.State.Running}}", "session-container"],
        ["docker", "inspect", "--format", "{{json .}}", "session-container"],
        ["docker", "top", "session-container", "-eo", "pid,comm"],
    ]


def test_failure_snapshot_uses_fake_docker_and_keeps_process_identity(monkeypatch):
    def run(command, **kwargs):
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Id": "fake-container-id",
                        "State": {
                            "Running": True,
                            "ExitCode": 0,
                            "OOMKilled": False,
                            "FinishedAt": "0001-01-01T00:00:00Z",
                        },
                    }
                ),
                stderr="",
            )
        if command[:2] == ["docker", "top"]:
            return SimpleNamespace(returncode=0, stdout="PID COMMAND\n1234 px4\n", stderr="")
        if command[:2] == ["docker", "logs"] or command[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
    monkeypatch.setattr(collector, "_host_load_average", lambda: [7.5, 6.0, 5.0])
    monkeypatch.setattr(
        collector,
        "_host_gpu_memory_mib",
        lambda: [{"index": 0, "used_mib": 1234, "total_mib": 20480}],
    )
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "onboard", 1, 1, 0, "out", max_episode_s=1.0
    )
    environment = collector.DockerSimEnvironment(plan, "episode")
    environment.started = True
    error = TimeoutError("PX4 simulation clock stalled")

    assert environment.__exit__(TimeoutError, error, None) is False
    diagnostics = environment.failure_diagnostics
    assert diagnostics is not None
    assert diagnostics["failure_class"] == "clock_stalled_alive"
    assert diagnostics["container_id"] == "fake-container-id"
    assert diagnostics["failure_state"]["state"] == {
        "running": True,
        "exit_code": 0,
        "oom_killed": False,
        "finished_at": "0001-01-01T00:00:00Z",
    }
    assert diagnostics["failure_state"]["px4_process_alive"] is True
    assert diagnostics["failure_state"]["px4_host_pids"] == [1234]
    assert diagnostics["failure_state"]["host_load_average"] == [7.5, 6.0, 5.0]
    assert diagnostics["failure_state"]["gpu_memory_mib"] == [
        {"index": 0, "used_mib": 1234, "total_mib": 20480}
    ]
    assert diagnostics["cleanup_state"]["px4_process_alive"] is True


def test_sensor_stream_timeout_gets_stall_capture_and_distinct_failure_class(monkeypatch, tmp_path):
    captured = []

    def capture(container, episode_dir, **kwargs):
        captured.append((container, episode_dir, kwargs))
        return {"path": str(tmp_path / "px4-clock-stall-capture.json"), "samples": [{}, {}]}

    monkeypatch.setattr(collector, "capture_px4_clock_stall", capture)
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "onboard", 1, 1, 0, "out", max_episode_s=1.0
    )
    environment = collector.DockerSimEnvironment(plan, "episode", episode_dir=tmp_path)
    environment._container_snapshot = lambda **kwargs: {
        "container_id": "container-id",
        "state": {"running": True, "exit_code": 0, "oom_killed": False},
        "px4_process_alive": True,
        "px4_host_pids": [1234],
    }
    error = TimeoutError("sensor stream produced no frame within 15.0s")

    assert environment.__exit__(TimeoutError, error, None) is False
    diagnostics = environment.failure_diagnostics
    assert diagnostics is not None
    assert diagnostics["failure_class"] == "sensor_stream_stalled"
    assert collector._failure_class(str(error), {"px4_process_alive": None}) == "sensor_stream_stalled"
    assert diagnostics["stall_capture"] == {
        "path": str(tmp_path / "px4-clock-stall-capture.json"),
        "samples": 2,
    }
    assert len(captured) == 1
    assert captured[0][0] == environment.name


def test_frame_wait_diagnostics_distinguishes_timestamp_gate_and_px4_stall(tmp_path):
    sample = SimpleNamespace(
        sensor_stamp_ns=900,
        received_monotonic_ns=2_000,
        received_wall_time=20.0,
    )
    reader = SimpleNamespace(latest=sample, frames_received=3)
    pose = SimpleNamespace(time_boot_ms=1)
    state = MavlinkState(
        messages={"LOCAL_POSITION_NED": pose},
        received_monotonic_ns={"LOCAL_POSITION_NED": 1_100},
        received_wall_time={"LOCAL_POSITION_NED": 11.0},
        vehicle_time_boot_ms=1,
    )
    mav = SimpleNamespace(state=state)
    row = collector._frame_wait_diagnostics(
        phase="obs_after",
        sensor_reader=reader,
        mav_reader=mav,
        pose_state=state,
        min_sensor_stamp_ns=1_000,
        started_monotonic_ns=1_500,
        ended_monotonic_ns=16_500,
        started_wall_time=15.0,
        ended_wall_time=30.0,
        host_pressure_path=tmp_path / "missing.jsonl",
        error=TimeoutError("sensor stream produced no frame within 15.0s"),
    )
    assert row["failure_class"] == "frame_timestamp_below_pose_watermark"
    assert row["last_frame_sensor_stamp_ns"] == 900
    assert row["required_sensor_stamp_ns"] == 1_000
    assert row["primitive_end_monotonic_ns"] is None

    stalled = collector._frame_wait_diagnostics(
        phase="obs_after",
        sensor_reader=SimpleNamespace(latest=None, frames_received=3),
        mav_reader=mav,
        pose_state=state,
        min_sensor_stamp_ns=1_000,
        started_monotonic_ns=1_500,
        ended_monotonic_ns=16_500,
        started_wall_time=15.0,
        ended_wall_time=30.0,
        error=TimeoutError("sensor stream produced no frame within 15.0s"),
    )
    assert stalled["failure_class"] == "px4_clock_and_telemetry_stalled"

    after = collector._frame_wait_diagnostics(
        phase="obs_after",
        sensor_reader=SimpleNamespace(latest=None, frames_received=3),
        mav_reader=mav,
        pose_state=state,
        min_sensor_stamp_ns=1_000,
        started_monotonic_ns=1_500,
        primitive_end_monotonic_ns=1_400,
        ended_monotonic_ns=16_500,
        started_wall_time=15.0,
        ended_wall_time=30.0,
        error=TimeoutError("sensor stream produced no frame within 15.0s"),
    )
    assert after["primitive_end_monotonic_ns"] == 1_400


def test_exited_container_is_classified_as_px4_crashed_with_fake_docker(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "Id": "crashed-container-id",
                        "State": {
                            "Running": False,
                            "ExitCode": 139,
                            "OOMKilled": False,
                            "FinishedAt": "2026-11-05T01:02:03Z",
                        },
                    }
                ),
                stderr="",
            )
        if command[:2] == ["docker", "logs"] or command[:2] == ["docker", "rm"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(collector.subprocess, "run", run)
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")
    monkeypatch.setattr(collector, "_host_gpu_memory_mib", lambda: None)
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "onboard", 1, 1, 0, "out", max_episode_s=1.0
    )
    environment = collector.DockerSimEnvironment(plan, "episode")
    environment.started = True
    error = RuntimeError("sensor stream ended early")

    assert environment.__exit__(RuntimeError, error, None) is False
    diagnostics = environment.failure_diagnostics
    assert diagnostics is not None
    assert diagnostics["failure_class"] == "px4_crashed"
    assert diagnostics["failure_state"]["px4_process_alive"] is False
    assert diagnostics["failure_state"]["state"]["exit_code"] == 139
    assert not any(command[:2] == ["docker", "top"] for command in calls)


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
