from __future__ import annotations

import io
import json
import math
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


def test_delta_fw_ring_metrics_loads_recorder_jsonl_files(tmp_path):
    mavlink_path = tmp_path / "mavlink.jsonl"
    steps_path = tmp_path / "steps.jsonl"
    mavlink_rows = [
        {"message_type": "PARAM_VALUE", "param_id": "NAV_LOITER_RAD", "param_value": 80.0}
    ]
    for index in range(61):
        sim_time_s = index * 0.5
        angle = 2.0 * math.pi * index / 60.0
        mavlink_rows.extend(
            [
                {
                    "message_type": "EXTENDED_SYS_STATE",
                    "sim_time_s": sim_time_s,
                    "vtol_state": 4,
                },
                {
                    "message_type": "LOCAL_POSITION_NED",
                    "sim_time_s": sim_time_s,
                    "x": 80.0 * math.cos(angle),
                    "y": 80.0 * math.sin(angle),
                },
            ]
        )
    steps_row = {
        "ack": {
            "smoke_reposition_wait": {
                "target_ned_m": [0.0, 0.0, -40.0],
                "distance_m": 80.0,
            }
        }
    }
    mavlink_path.write_text(
        "".join(json.dumps(row) + "\n" for row in mavlink_rows), encoding="utf-8"
    )
    steps_path.write_text(json.dumps(steps_row) + "\n", encoding="utf-8")

    result = collector._delta_fw_ring_metrics_from_files(mavlink_path, steps_path)

    assert result["fw_reposition_success"] is True
    assert result["fw_reposition_radius_m"] == pytest.approx(80.0)
    assert result["fw_reposition_dwell_s"] >= 30.0
    assert result["fw_reposition_swept_deg"] >= 360.0


def test_sensor_frame_timeout_reports_reader_state():
    reader = collector.SensorStreamReader(io.BytesIO(b""))

    with pytest.raises(TimeoutError, match=r'"frames_received": 0') as error:
        collector._fresh_sensor_snapshot(
            reader,
            previous_ns=0,
            timeout_s=0.0,
            min_sensor_stamp_ns=123,
        )
    assert '"sensor_watermark_lag_s": null' in str(error.value)


def test_sensor_frame_timeout_reports_px4_watermark_lag():
    class Reader:
        latest = SimpleNamespace(received_monotonic_ns=1, sensor_stamp_ns=100_552_000_000)
        frames_received = 93
        frames_dropped = 92
        done = __import__("threading").Event()
        error = None
        _thread = None

        def request(self, _stamp):
            return None

        def raise_if_failed(self):
            return None

    with pytest.raises(TimeoutError) as error:
        collector._fresh_sensor_snapshot(
            Reader(),
            previous_ns=0,
            timeout_s=0.0,
            min_sensor_stamp_ns=114_500_000_000,
        )
    assert '"sensor_watermark_lag_s": 13.948' in str(error.value)


def test_strict_sensor_snapshot_refreshes_px4_watermark_while_waiting():
    class Reader:
        def __init__(self):
            self.latest = SimpleNamespace(received_monotonic_ns=2, sensor_stamp_ns=150)
            self.frames_received = 1
            self.frames_dropped = 0
            self.done = __import__("threading").Event()
            self.error = None
            self._thread = None
            self.requests = []

        def request(self, stamp):
            self.requests.append(stamp)
            if stamp == 200:
                self.latest = SimpleNamespace(received_monotonic_ns=3, sensor_stamp_ns=200)

        def raise_if_failed(self):
            return None

    watermarks = iter((100, 200, 200))
    reader = Reader()
    sample = collector._fresh_sensor_snapshot(
        reader,
        previous_ns=1,
        timeout_s=0.1,
        min_sensor_stamp_ns=100,
        current_min_sensor_stamp_ns=lambda: next(watermarks, 200),
        poll_interval_s=0.001,
    )
    assert sample.sensor_stamp_ns == 200
    assert reader.requests == [100, 200]


def test_sensor_frame_timeout_can_make_one_bounded_reconnect():
    class Reader:
        def __init__(self, sample=None):
            self.latest = sample
            self.frames_received = 0 if sample is None else 1
            self.frames_dropped = 0
            self.done = __import__("threading").Event()
            self.error = None
            self._thread = None

        def request(self, _stamp):
            return None

        def raise_if_failed(self):
            return None

    replacement = Reader(
        SimpleNamespace(
            received_monotonic_ns=2,
            sensor_stamp_ns=123,
        )
    )
    calls = []

    def reconnect():
        calls.append(True)
        return replacement

    sample = collector._fresh_sensor_snapshot(
        Reader(),
        previous_ns=0,
        timeout_s=0.001,
        min_sensor_stamp_ns=123,
        poll_interval_s=0.001,
        reconnect=reconnect,
        max_reconnects=1,
    )
    assert sample is replacement.latest
    assert calls == [True]


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
    assert diagnostic["failure_class"] == "telemetry_receiver"
    assert diagnostic["failure_detail"] == "receiver_thread_exit"
    assert diagnostic["sim_elapsed_since_last_mavlink_s"] == pytest.approx(5.0)
    assert diagnostic["sim_clock_advancing"] is True
    assert diagnostic["container"]["running"] is True
    assert diagnostic["receiver_thread_alive"] is False
    assert diagnostic["receiver_last_exception"] is None
    assert diagnostic["receiver_message_counts"] == {}
    assert diagnostic["receiver_socket"]["available"] is False
    assert diagnostic["receiver_recent_messages"] == []


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


def test_telemetry_silence_classifies_container_exit(tmp_path):
    class ExitedEnvironment(FakeEnvironment):
        def inspect_state(self):
            return {"Running": False, "ExitCode": 139, "OOMKilled": False}

    now = [0.0]
    reader = _reader()
    environment = ExitedEnvironment()
    recorder = FakeRecorder(tmp_path / "episode")
    watchdog = collector.MavlinkTelemetrySilenceWatchdog(
        reader, environment, recorder, clock=lambda: now[0]
    )
    now[0] = 15.0
    with pytest.raises(TimeoutError, match="MAVLink telemetry silent"):
        watchdog.check()
    diagnostic = json.loads((recorder.episode_dir / "telemetry-silence.json").read_text())
    assert diagnostic["failure_class"] == "px4_other_segfault"
    assert diagnostic["container"]["state"]["ExitCode"] == 139


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


def test_container_evidence_capture_accepts_no_ulog_files(tmp_path, monkeypatch):
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "explore", 1, 1, 0, str(tmp_path)
    )
    environment = collector.DockerSimEnvironment(plan, "episode", tmp_path / "episode")
    environment.evidence_dir.mkdir(parents=True)
    monkeypatch.setattr(
        environment,
        "inspect_state",
        lambda: {"Running": True, "ExitCode": 0, "OOMKilled": False},
    )
    monkeypatch.setattr(
        environment,
        "_capture_container_path",
        lambda _remote, destination, _errors: (destination.mkdir(parents=True, exist_ok=True) or True),
    )
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker-test")
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = environment._capture_evidence()

    assert result["copied_paths"]["/opt/px4-gazebo/log"] is True
    assert result["ulog_files"] == []
    assert result["ulog_analysis"] == []
    assert result["errors"] == {}


def test_logger_crash_kernel_capture_is_best_effort_and_records_matching_pid(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return SimpleNamespace(
            returncode=0,
            stdout="Nov 05 px4[1234]: logger[77] segfault at 2885a9\nNov 05 px4[9]: unrelated\n",
            stderr="",
        )

    monkeypatch.setattr(collector.subprocess, "run", fake_run)
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "explore", 1, 1, 0, str(tmp_path)
    )
    environment = collector.DockerSimEnvironment(plan, "episode", tmp_path / "episode")
    environment.evidence_dir.mkdir(parents=True)
    result = environment._capture_kernel_segfault(
        {"ExitCode": 139, "Pid": 1234, "StartedAt": "2026-11-05T09:38:00Z"}
    )
    assert result["failure_class"] == "px4_logger_segfault"
    assert calls[0][:4] == ["journalctl", "-k", "--since", "2026-11-05T09:38:00Z"]
    assert "px4[1234]" in (environment.evidence_dir / "kernel-segfault.log").read_text()


def test_logger_thread_name_before_colon_is_classified_as_logger_segfault(tmp_path, monkeypatch):
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="Nov 05 kernel: px4[1234] logger: segfault at 0x123456\n",
            stderr="",
        ),
    )
    plan = collector.CollectionPlan(
        "scenario", "runtime", "truth", "explore", 1, 1, 0, str(tmp_path)
    )
    environment = collector.DockerSimEnvironment(plan, "episode", tmp_path / "episode")
    environment.evidence_dir.mkdir(parents=True)

    result = environment._capture_kernel_segfault(
        {"ExitCode": 139, "Pid": 1234, "StartedAt": "2026-11-05T09:38:00Z"}
    )

    assert result["matching_lines"] == 1
    assert result["failure_class"] == "px4_logger_segfault"


def test_px4_hang_collects_gdb_and_proc_threads_before_cleanup(tmp_path, monkeypatch):
    now = [0.0]
    reader = _reader()
    reader._thread = SimpleNamespace(is_alive=lambda: True)

    class RunningEnvironment(FakeEnvironment):
        evidence_dir = tmp_path / "container-evidence"
        px4_threads_evidence = None

        def inspect_state(self):
            return {"Running": True, "ExitCode": 0, "Pid": 1234}

        def _capture_px4_threads(self, host_pid):
            self.evidence_dir.mkdir(parents=True)
            (self.evidence_dir / "px4-threads.txt").write_text("thread 1 bt\n")
            return {"file": "px4-threads.txt", "host_pid": host_pid}

    environment = RunningEnvironment()
    recorder = FakeRecorder(tmp_path / "episode")
    watchdog = collector.MavlinkTelemetrySilenceWatchdog(
        reader, environment, recorder, clock=lambda: now[0]
    )
    now[0] = 50.0
    environment.stamp_ns = 5_000_000_000
    environment.progress_wall = now[0]
    with pytest.raises(TimeoutError):
        watchdog.check()
    diagnostic = json.loads((recorder.episode_dir / "telemetry-silence.json").read_text())
    assert diagnostic["failure_class"] == "px4_hang"
    assert diagnostic["px4_threads"]["host_pid"] == 1234


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
