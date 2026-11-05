from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.streams import MavlinkReader
from scripts import collect_sim_teacher_episodes as collector


class FakeRecorder:
    def __init__(self, root: Path):
        self.episode_dir = root
        self.episode_path = root / "episode.json"
        self.meta = {}
        self.rows = []
        root.mkdir(parents=True)

    def record_mavlink(self, row):
        self.rows.append(row)


class FakeEnvironment:
    name = "sim-test"

    def __init__(self):
        self.stamp_ns = 0
        self.progress_wall = 0.0

    def sim_clock_snapshot(self):
        return self.stamp_ns, self.progress_wall

    def inspect_state(self):
        return {"Running": True, "ExitCode": 0, "OOMKilled": False}


def _reader():
    reader = MavlinkReader(object())
    reader.state.count = 1
    reader.state.vehicle_time_boot_ms = 1000
    reader.state.received_monotonic_ns = {"ATTITUDE": 0}
    return reader


def test_telemetry_silence_uses_sim_and_wall_clocks_at_point_one_realtime(tmp_path):
    now = [0.0]
    reader = _reader()
    environment = FakeEnvironment()
    recorder = FakeRecorder(tmp_path / "episode")
    watchdog = collector.MavlinkTelemetrySilenceWatchdog(
        reader, environment, recorder, clock=lambda: now[0]
    )

    now[0] = 49.9
    environment.stamp_ns = 4_990_000_000
    environment.progress_wall = now[0]
    watchdog.check()

    now[0] = 50.0
    environment.stamp_ns = 5_000_000_000
    environment.progress_wall = now[0]
    with pytest.raises(TimeoutError, match="MAVLink telemetry silent"):
        watchdog.check()

    diagnostic = json.loads((recorder.episode_dir / "telemetry-silence.json").read_text())
    assert diagnostic["sim_elapsed_since_last_mavlink_s"] == pytest.approx(5.0)
    assert diagnostic["sim_clock_advancing"] is True
    assert diagnostic["container"]["running"] is True


def test_telemetry_silence_detects_frozen_sim_clock_with_wall_guard(tmp_path):
    now = [0.0]
    reader = _reader()
    environment = FakeEnvironment()
    recorder = FakeRecorder(tmp_path / "episode")
    watchdog = collector.MavlinkTelemetrySilenceWatchdog(
        reader, environment, recorder, clock=lambda: now[0]
    )

    now[0] = 14.9
    watchdog.check()
    now[0] = 15.0
    with pytest.raises(TimeoutError, match="sim_time_advancing=False"):
        watchdog.check()

    diagnostic = json.loads((recorder.episode_dir / "telemetry-silence.json").read_text())
    assert diagnostic["sim_clock_advancing"] is False
    assert diagnostic["sim_time_stalled_wall_s"] == pytest.approx(15.0)


def test_telemetry_silence_allows_point_one_realtime_before_failing(tmp_path):
    now = [0.0]
    reader = _reader()
    environment = FakeEnvironment()
    recorder = FakeRecorder(tmp_path / "episode")
    watchdog = collector.MavlinkTelemetrySilenceWatchdog(
        reader, environment, recorder, clock=lambda: now[0]
    )

    now[0] = 49.9
    environment.stamp_ns = 4_990_000_000
    environment.progress_wall = now[0]
    watchdog.check()

    now[0] = 50.0
    environment.stamp_ns = 5_000_000_000
    environment.progress_wall = now[0]
    with pytest.raises(TimeoutError, match="MAVLink telemetry silent"):
        watchdog.check()

    diagnostic = json.loads((recorder.episode_dir / "telemetry-silence.json").read_text())
    assert diagnostic["thresholds"]["minimum_realtime_factor"] == pytest.approx(0.1)
    assert diagnostic["thresholds"]["minimum_wall_s"] == pytest.approx(50.0)


def test_console_tail_removes_px4_prompt_redraw_but_keeps_recent_console_text():
    output, prompts_removed = collector.DockerSimEnvironment._console_tail(
        "\x1b[2K\rpxh>\x1b[2K\rpxh> commander status\rPX4 boot complete\r"
    )
    assert prompts_removed == 2
    assert "commander status" in output
    assert "PX4 boot complete" in output
    assert "pxh>" not in output


def test_evidence_copy_failures_do_not_prevent_container_removal(tmp_path, monkeypatch):
    calls = []

    def fail_capture(command, **kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=1, stdout="", stderr="fixture failure")

    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker-test")
    monkeypatch.setattr(collector.subprocess, "run", fail_capture)
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "explore", 1, 1, 0, str(tmp_path)
    )
    environment = collector.DockerSimEnvironment(plan, "episode", tmp_path / "episode")
    environment.started = True

    environment.__exit__(RuntimeError, RuntimeError("flight failed"), None)

    assert calls[-1] == ["docker-test", "rm", "--force", environment.name]
    evidence = json.loads(
        (tmp_path / "episode" / "container-evidence" / "index.json").read_text()
    )
    assert evidence["errors"]["/opt/px4-gazebo/log"]


def test_evidence_records_container_state_before_cleanup_and_exit_source(tmp_path, monkeypatch):
    states = iter(
        [
            {"Running": True, "ExitCode": 0, "OOMKilled": False},
            {"Running": False, "ExitCode": 137, "OOMKilled": True},
        ]
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        if command[1:3] == ["inspect", "--format"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(next(states)),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker-test")
    monkeypatch.setattr(collector.subprocess, "run", fake_run)
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "explore", 1, 1, 0, str(tmp_path)
    )
    environment = collector.DockerSimEnvironment(plan, "episode", tmp_path / "episode")
    environment.started = True

    environment.__exit__(None, None, None)

    evidence = json.loads(
        (tmp_path / "episode" / "container-evidence" / "index.json").read_text()
    )
    assert evidence["running_before_cleanup"] is True
    assert evidence["state_before_cleanup"]["ExitCode"] == 0
    assert evidence["exit_code"] == 137
    assert evidence["oom_killed"] is True
    assert evidence["exit_code_source"] == "collector_stop"
    assert calls[-1] == ["docker-test", "rm", "--force", environment.name]


def test_gimbal_measurement_wait_requires_three_sim_seconds_of_level_mc(monkeypatch):
    boot_ms = [0]
    attitude = SimpleNamespace(roll=0.0, pitch=0.0, yaw=0.0, time_boot_ms=0)

    class State:
        vehicle_time_boot_ms = 0

        def get(self, name):
            if name == "ATTITUDE":
                return attitude
            if name == "EXTENDED_SYS_STATE":
                return SimpleNamespace(vtol_state=3)
            return None

    class Reader:
        state = State()

        def raise_if_failed(self):
            return None

    def advance(seconds):
        boot_ms[0] += int(seconds * 1000)
        Reader.state.vehicle_time_boot_ms = boot_ms[0]
        attitude.time_boot_ms = boot_ms[0]

    monkeypatch.setattr(collector.time, "sleep", advance)
    result = collector._wait_for_stable_mc_hover(Reader())
    assert result["status"] == "stable"
    assert result["vtol_state"] == 3
    assert result["stable_duration_s"] >= 3.0


def test_gimbal_snapshot_keeps_attitude_device_and_aligned_joint_timestamps():
    attitude = SimpleNamespace(roll=0.01, pitch=-0.02, yaw=0.5, time_boot_ms=4200)
    messages = {
        "ATTITUDE": attitude,
        "EXTENDED_SYS_STATE": SimpleNamespace(vtol_state=3),
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {"q": [1.0, 0.0, 0.0, 0.0], "flags": 64},
    }
    state = SimpleNamespace(
        get=messages.get,
        received_monotonic_ns={"GIMBAL_DEVICE_ATTITUDE_STATUS": 987654321},
    )

    class Reader:
        def __init__(self):
            self.state = state

        def aligned_state(self, stamp_ns):
            assert stamp_ns == 4_200_000_000
            return SimpleNamespace(get=messages.get, pose_alignment={"method": "interpolated"})

    joint_state = {
        "timestamp_ns": 4_200_000_000,
        "position_rad": {"yaw": 0.1, "roll": 0.2, "pitch": -0.3},
    }
    sample = SimpleNamespace(
        sensor_stamp_ns=4_200_000_000,
        joint_state={"gimbal_joint_state": joint_state},
    )

    result = collector._gimbal_measurement_snapshot(Reader(), sample)

    assert result["body_attitude_time_boot_ms"] == 4200
    assert result["body_pose_alignment"] == {"method": "interpolated"}
    assert result["gimbal_device_attitude_status"]["q"] == [1.0, 0.0, 0.0, 0.0]
    assert result["gimbal_device_attitude_status"]["flags"] == 64
    assert result["gimbal_device_receive_monotonic_ns"] == 987654321
    assert result["gimbal_joint_state"] == joint_state
