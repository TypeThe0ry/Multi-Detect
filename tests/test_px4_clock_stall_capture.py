from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.px4_clock_stall_capture import (
    _capture_px4_gdb,
    _parse_gz_topic_info,
    _parse_transport_sockets,
    _read_host_threads,
    analyze_capture,
    capture_stall,
)


def _thread(
    *,
    group: str = "px4",
    pid: int = 10,
    tid: int = 10,
    name: str = "wq:rate_ctrl",
    state: str = "R",
    wchan: str = "0",
    on_cpu_ns: int = 1_000_000_000,
    wait_ns: int = 0,
    cpu_time_s: float = 1.0,
) -> dict[str, object]:
    return {
        "group": group,
        "pid": pid,
        "tid": tid,
        "name": name,
        "state": state,
        "wchan": wchan,
        "on_cpu_ns": on_cpu_ns,
        "run_queue_wait_ns": wait_ns,
        "cpu_time_s": cpu_time_s,
        "process_cmdline": (
            "/opt/px4-gazebo/bin/px4 -i 0" if group == "px4" else "gz sim"
        ),
    }


def _sample(*, clock: float | None, threads: list[dict[str, object]]) -> dict[str, object]:
    return {
        "gz_clock": {"latest_sim_time_s": clock},
        "process_threads": {"threads": {"px4": threads, "gz": []}},
        "cpu_stat": {"stdout": "nr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"},
        "host_load_average": [40.0, 38.0, 36.0],
        "host_load_per_cpu": [0.42, 0.40, 0.38],
    }


def test_capture_uses_owner_cmdline_absolute_commands_and_independent_timeouts(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], float]] = []
    sample_index = {"threads": 0, "clock": 0}
    thread_outputs = [
        "T\tpx4\t10\t10\twq:rate_ctrl\tS\tfutex_wait\t100\t20\t100000000\t20000000\t4\t/opt/px4-gazebo/bin/px4 -i 0\n"
        "T\tpx4\t10\t11\twkr_hrt\tS\thrtimeout\t100\t20\t100000000\t20000000\t4\t/opt/px4-gazebo/bin/px4 -i 0\n"
        "T\tpx4\t10\t12\twq:INS0\tS\tfutex_wait\t100\t20\t100000000\t20000000\t4\t/opt/px4-gazebo/bin/px4 -i 0\n"
        "T\tgz\t20\t20\truby\tS\tpoll_schedule_timeout\t100\t20\t100000000\t20000000\t4\tgz sim\n",
        "T\tpx4\t10\t10\twq:rate_ctrl\tS\tfutex_wait\t100\t20\t100000000\t20000000\t4\t/opt/px4-gazebo/bin/px4 -i 0\n"
        "T\tpx4\t10\t11\twkr_hrt\tS\thrtimeout\t100\t20\t100000000\t20000000\t4\t/opt/px4-gazebo/bin/px4 -i 0\n"
        "T\tpx4\t10\t12\twq:INS0\tS\tfutex_wait\t100\t20\t100000000\t20000000\t4\t/opt/px4-gazebo/bin/px4 -i 0\n"
        "T\tgz\t20\t20\truby\tS\tpoll_schedule_timeout\t100\t20\t100000000\t20000000\t4\tgz sim\n",
    ]

    def runner(command, **kwargs):
        calls.append((list(command), float(kwargs["timeout"])))
        joined = " ".join(command)
        if "task/" in joined:
            stdout = thread_outputs[sample_index["threads"]]
            sample_index["threads"] += 1
        elif "gz topic" in joined:
            stdout = f"sim {{ sec: {7 + sample_index['clock']} nsec: 0 }}\n"
            sample_index["clock"] += 1
        elif "cpu.stat" in joined:
            stdout = "usage_usec 100\nnr_periods 0\nnr_throttled 0\nthrottled_usec 0\n"
        elif "memory.events" in joined:
            stdout = "oom 0\n"
        else:
            stdout = "ok"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    document = capture_stall(
        "fake",
        tmp_path,
        delay_s=0,
        runner=runner,
        sleep=lambda _: None,
        load_average=[48.0, 40.0, 32.0],
    )

    assert len(document["samples"]) == 2
    saved = json.loads((tmp_path / "px4-clock-stall-capture.json").read_text())
    assert saved["captured_before_container_removal"] is True
    assert saved["path"] == str(tmp_path / "px4-clock-stall-capture.json")
    assert len(saved["analysis"]["thread_deltas"]) == 4
    assert [len(row["process_threads"]["threads"]["px4"]) for row in saved["samples"]] == [
        3,
        3,
    ]
    assert len(saved["samples"][0]["process_threads"]["threads"]["gz"]) == 1
    commands = [command for command, _ in calls]
    assert len(commands) == 20
    assert all("pxh" not in command for command in commands)
    gz_commands = [command for command in commands if "gz" in command]
    assert len(gz_commands) == 4
    assert [command[3:] for command in gz_commands] == [
        ["gz", "topic", "-i", "-t", "/world/default/clock"],
        ["timeout", "5s", "gz", "topic", "-e", "-n", "1", "-t", "/world/default/clock"],
    ] * 2
    assert saved["samples"][0]["transport_probe_order"] == [
        "gz_topic_info", "proc_net_sockets", "process_threads"
    ]
    work_queue_commands = [
        command for command in commands if any("px4-work_queue" in part for part in command)
    ]
    assert [timeout for command, timeout in calls if command in work_queue_commands] == [4.0, 4.0]
    assert work_queue_commands == [
        ["docker", "exec", "fake", "/opt/px4-gazebo/bin/px4-work_queue", "status"]
    ] * 2
    assert [
        timeout
        for command, timeout in calls
        if any("px4-listener" in part for part in command)
    ] == [3.0, 3.0, 3.0, 3.0]
    listener_commands = [
        command for command in commands if any("px4-listener" in part for part in command)
    ]
    assert all(command[3] == "/opt/px4-gazebo/bin/px4-listener" for command in listener_commands)
    assert [command[-1] for command in listener_commands] == [
        "sensor_combined",
        "vehicle_status",
        "sensor_combined",
        "vehicle_status",
    ]
    thread_commands = [
        command for command in commands
        if "self_pid=$$" in command[-1] and "printf 'T\\t" in command[-1]
    ]
    assert len(thread_commands) == 2
    assert all(timeout == 10.0 for command, timeout in calls if command in thread_commands)
    assert "self_pid=$$" in thread_commands[0][-1]
    assert saved["samples"][0]["host_load_per_core"] is not None
    assert saved["gdb"]["status"] == "unavailable"
    assert [row["label"] for row in saved["gdb"]["snapshots"]] == [
        "before_delay", "after_delay"
    ]
    assert Path(saved["gdb_output_path"]).is_file()


def test_host_proc_thread_reader_parses_fake_proc_tree_and_stat_comm_delimiter(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"

    def write_thread(pid: int, tid: int, *, comm: str, state: str = "S") -> None:
        task = proc_root / str(pid) / "task" / str(tid)
        task.mkdir(parents=True)
        (task.parent.parent / "cmdline").write_bytes(
            b"/opt/px4-gazebo/bin/px4 -i 0\0" if pid == 101 else b"gz sim -r\0"
        )
        stat_fields = [state, *(["0"] * 10), "123", "45"]
        (task / "stat").write_text(f"{tid} ({comm}) " + " ".join(stat_fields))
        (task / "comm").write_text("wq:rate_ctrl")
        (task / "wchan").write_text("futex_wait")
        (task / "schedstat").write_text("100000000 20000000 4\n")

    write_thread(101, 101, comm="thread)with-paren")
    write_thread(101, 102, comm="wkr_hrt")
    write_thread(202, 202, comm="ruby")

    def runner(command, **kwargs):
        assert command == ["docker", "top", "fake", "-eo", "pid,args"]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "PID COMMAND\n"
                "/not-a-pid ignored\n"
                "101 /opt/px4-gazebo/bin/px4 -i 0\n"
                "202 gz sim -r\n"
            ),
            stderr="",
        )

    result = _read_host_threads("docker", "fake", proc_root=proc_root, runner=runner)

    assert result["collection_path"] == "host_proc"
    assert [row["tid"] for row in result["threads"]["px4"]] == [101, 102]
    assert [row["tid"] for row in result["threads"]["gz"]] == [202]
    assert result["threads"]["px4"][0]["state"] == "S"
    assert result["threads"]["px4"][0]["utime_ticks"] == 123
    assert result["threads"]["px4"][0]["stime_ticks"] == 45
    assert result["threads"]["px4"][0]["read_wall_time"] >= result["sample_wall_time"]


def test_analyze_capture_uses_clock_drivers_for_progress_and_starvation() -> None:
    names = ("wq:rate_ctrl", "wkr_hrt", "wq:INS0")
    before = [
        _thread(tid=index, name=name)
        for index, name in enumerate(names, start=10)
    ]
    after = [
        _thread(
            tid=index,
            name=name,
            on_cpu_ns=1_010_000_000,
            wait_ns=400_000_000,
            cpu_time_s=1.0,
        )
        for index, name in enumerate(names, start=10)
    ]
    # MAVLink can continue to tick while clock-driving work is starved.
    before.append(_thread(tid=30, name="mavlink_rcv_if0", on_cpu_ns=1_000_000_000))
    after.append(
        _thread(tid=30, name="mavlink_rcv_if0", on_cpu_ns=2_000_000_000, cpu_time_s=2.0)
    )
    result = analyze_capture(
        {"samples": [_sample(clock=10.0, threads=before), _sample(clock=13.0, threads=after)]}
    )

    assert result["gz_clock_advanced"] is True
    assert result["px4_thread_progress"] is False
    assert result["supports"]["bridge_input_or_lockstep_path"] is True
    assert result["supports"]["scheduling_starvation"] is True
    assert len(result["thread_deltas"]) == 4
    assert len(result["clock_driver_threads"]) == 3
    assert result["thread_deltas"][-1]["name"] == "mavlink_rcv_if0"


def test_analyze_capture_reports_driver_block_state_wchan_and_clock_unknown() -> None:
    names = ("wq:rate_ctrl", "wkr_hrt", "wq:INS0")
    before = [
        _thread(tid=index, name=name, state="S", wchan="futex_wait", on_cpu_ns=100)
        for index, name in enumerate(names, start=10)
    ]
    after = [
        _thread(
            tid=index,
            name=name,
            state="S",
            wchan="futex_wait",
            on_cpu_ns=100,
            wait_ns=0,
        )
        for index, name in enumerate(names, start=10)
    ]
    result = analyze_capture(
        {
            "samples": [
                _sample(clock=10.0, threads=before),
                _sample(clock=None, threads=after),
            ]
        }
    )

    assert result["gz_clock_advanced"] == "unknown"
    assert result["px4_threads_appear_blocked"] is True
    assert result["supports"]["px4_thread_block"] is False
    assert len(result["px4_blocked_clock_driver_threads"]) == 3
    assert result["px4_blocked_clock_driver_threads"][0]["wchan_after"] == "futex_wait"


def test_analyze_capture_does_not_infer_starvation_from_load_or_throttling() -> None:
    before = [_thread(name="wq:rate_ctrl")]
    after = [_thread(name="wq:rate_ctrl", on_cpu_ns=1_001_000_000, wait_ns=10_000_000)]
    samples = [_sample(clock=1.0, threads=before), _sample(clock=1.0, threads=after)]
    samples[0]["cpu_stat"] = {"stdout": "nr_periods 0\nnr_throttled 1\nthrottled_usec 10\n"}
    samples[1]["cpu_stat"] = {"stdout": "nr_periods 0\nnr_throttled 2\nthrottled_usec 20\n"}
    result = analyze_capture({"samples": samples})

    assert result["gz_clock_advanced"] is False
    assert result["cgroup_cpu_throttling_increased"] is True
    assert result["supports"]["scheduling_starvation"] is False


def test_analyze_capture_reports_gone_shared_futexes_and_canned_gdb_frames(tmp_path: Path) -> None:
    output = tmp_path / "px4-clock-stall-gdb.txt"
    output.write_text(
        "Thread 4 (LWP 101):\n"
        "#0  __futex_abstimed_wait_cancelable64 () at futex-internal.c:57\n"
        "#1  pthread_cond_wait () from /lib/libpthread.so.0\n"
        "Thread 5 (LWP 102):\n"
        "#0  poll () from /lib/libc.so.6\n"
    )
    blocked = _thread(tid=101, name="", state="S", wchan="futex_wait")
    blocked["syscall"] = "202 0x7ffd1234, 0, 0, 0, 0, 0"
    sibling = _thread(tid=102, name="", state="S", wchan="futex_wait")
    sibling["syscall"] = "202 0x7ffd1234, 0, 0, 0, 0, 0"
    result = analyze_capture({
        "samples": [_sample(clock=1.0, threads=[blocked, sibling])],
        "gdb": {"output_path": str(output)},
    })

    assert result["shared_futex_addresses"]["0x7ffd1234"][1]["tid"] == 102
    assert result["clock_thread_top_frames"] == [
        {
            "tid": 101,
            "top_frames": [
                "#0  __futex_abstimed_wait_cancelable64 () at futex-internal.c:57",
                "#1  pthread_cond_wait () from /lib/libpthread.so.0",
            ],
        },
        {"tid": 102, "top_frames": ["#0  poll () from /lib/libc.so.6"]},
    ]


def test_transport_parsers_report_topic_roles_socket_state_and_queues() -> None:
    topic = _parse_gz_topic_info(
        "Publishers [Address, Message Type]:\n  tcp://127.0.0.1:1234\n"
        "Subscribers [Address, Message Type]:\n  tcp://127.0.0.1:5678\n"
    )
    assert topic["publisher_count"] == 1
    assert topic["subscribers"] == ["tcp://127.0.0.1:5678"]
    sockets = _parse_transport_sockets(
        "S\tpx4\t10\tpx4 -i 0\t4\t12345\t01\t0000000A:0000000F\t0100007F:1234\t0200007F:5678\n"
        "SS\tESTAB 0 15 127.0.0.1:1 127.0.0.1:2\n"
    )
    assert sockets["sockets"][0]["state_name"] == "ESTABLISHED"
    assert sockets["sockets"][0]["tx_queue_bytes"] == 10
    assert sockets["sockets"][0]["rx_queue_bytes"] == 15
    assert sockets["ss_available"] is True


def test_analyze_capture_separates_polling_reception_from_blocked_callback(
    tmp_path: Path,
) -> None:
    output = tmp_path / "gdb.txt"
    output.write_text(
        "=== GDB before_delay ===\n"
        "Thread 2 (LWP 201 \"px4\"):\n"
        "#0 poll ()\n#1 gz::transport::v13::NodeShared::RunReceptionTask()\n"
        "=== GDB after_delay ===\n"
        "Thread 2 (LWP 201 \"px4\"):\n"
        "#0 poll ()\n#1 gz::transport::v13::NodeShared::RunReceptionTask()\n"
    )
    samples = [_sample(clock=1.0, threads=[]), _sample(clock=2.0, threads=[])]
    for sample, queue in zip(samples, (4, 0)):
        sample["transport_sockets"] = {
            "sockets": [{
                "group": "px4", "pid": 10, "inode": 20, "local_address": "a",
                "remote_address": "b", "state": "01", "state_name": "ESTABLISHED",
                "rx_queue_bytes": queue,
            }]
        }
    result = analyze_capture({"samples": samples, "gdb": {"output_path": str(output)}})
    assert result["messages_stopped_arriving"] is True
    assert result["callback_blocked"] is False
    assert result["reception_thread_classification"] == "messages_stopped_arriving"
    assert result["transport_socket_rx_queue_changes"][0]["rx_queue_delta"] == -4
    assert result["transport_socket_rx_queue_delta_bytes"]["px4"] == -4
    assert result["transport_probe_can_disturb_state"] is True


def test_gdb_classifies_clock_callback_block_separately(tmp_path: Path) -> None:
    output = tmp_path / "gdb.txt"
    output.write_text(
        "=== GDB before_delay ===\nThread 1 (LWP 202 \"px4\"):\n"
        "#0 pthread_cond_broadcast ()\n#1 px4_clock_settime ()\n"
        "=== GDB after_delay ===\nThread 1 (LWP 202 \"px4\"):\n"
        "#0 pthread_cond_broadcast ()\n#1 px4_clock_settime ()\n"
    )
    result = analyze_capture({"samples": [_sample(clock=1.0, threads=[])], "gdb": {"output_path": str(output)}})
    assert result["callback_blocked"] is True
    assert result["callback_thread_classification"] == "callback_blocked"


def test_analyze_capture_marks_process_gone_instead_of_empty_thread_verdict() -> None:
    samples = [_sample(clock=None, threads=[]), _sample(clock=None, threads=[])]
    samples[0]["px4_process_status"] = "gone"
    result = analyze_capture({"samples": samples})

    assert result["px4_process_status"] == "px4_gone"
    assert result["px4_gone"] is True


def test_gdb_timeout_keeps_partial_output_and_records_failure(tmp_path: Path) -> None:
    output = tmp_path / "px4-clock-stall-gdb.txt"
    document = {
        "samples": [
            {
                "process_threads": {
                    "collection_path": "host_proc",
                    "host_pids": [321],
                    "threads": {"px4": [_thread(pid=321, tid=321)]},
                }
            }
        ]
    }

    def runner(command, **kwargs):
        assert command == [
            "/usr/bin/gdb", "-p", "321", "-batch", "-nx",
            "-ex", "set sysroot /proc/321/root",
            "-ex", "set pagination off",
            "-ex", "thread apply all bt 16",
        ]
        assert kwargs["timeout"] == 30.0
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output="partial backtrace")

    _capture_px4_gdb(
        document, output_path=output, gdb_path="/usr/bin/gdb", timeout_s=30.0, runner=runner
    )

    assert document["gdb"]["status"] == "timeout"
    assert "partial backtrace" in output.read_text()


def test_capture_records_timeout_as_unknown_and_persists_to_fallback(tmp_path: Path) -> None:
    episode_path = tmp_path / "episode"
    episode_path.write_text("directory removed before capture")

    def runner(command, **kwargs):
        if "gz" in command:
            raise subprocess.TimeoutExpired(
                command, kwargs["timeout"], output="sim { sec: 1 nsec: 0 }"
            )
        raise RuntimeError("fake command failure")

    document = capture_stall("fake", episode_path, delay_s=0, runner=runner, sleep=lambda _: None)

    assert document["path"].startswith(str(tmp_path / "px4-clock-stall-captures"))
    assert Path(document["path"]).is_file()
    assert "error" in document["samples"][0]["gz_clock"]
    assert document["samples"][0]["gz_clock"]["timed_out"] is True
    assert document["samples"][0]["gz_clock"]["latest_sim_time_s"] == 1.0
    assert analyze_capture(document)["gz_clock_advanced"] == "unknown"


def test_run_episode_stall_reaches_exit_before_cleanup_and_capture_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import collect_sim_teacher_episodes as collector

    output = tmp_path / "out"
    plan = collector.CollectionPlan(
        "scenario",
        "runtime",
        "truth",
        "hover",
        1,
        1,
        0,
        str(output),
        max_episode_s=1,
        min_free_gb=0,
    )
    events: list[str] = []

    class Policy:
        def close(self):
            return None

    monkeypatch.setattr(collector, "_airframe_profile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector, "_takeoff_altitude_for_plan", lambda _plan: (10.0, "test"))
    monkeypatch.setattr(collector, "scenario_world_path", lambda _runtime: None)
    monkeypatch.setattr(collector, "_hover_policy", lambda *_args: Policy())
    monkeypatch.setattr(collector, "snapshot_config", lambda *_args: None)
    monkeypatch.setattr(collector, "build_provenance", lambda **_kwargs: {})
    monkeypatch.setattr(collector, "model_paths_from_config", lambda _path: [])
    monkeypatch.setattr(collector, "resolve_cpu_isolation", lambda _index: None)
    monkeypatch.setattr(collector, "resolve_px4_logger_mode", lambda: "disabled")
    monkeypatch.setattr(collector, "_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector, "_drain_sim_operator_link", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector, "free_space_bytes", lambda _path: 10**15)
    monkeypatch.setattr(collector, "_docker_executable", lambda _plan: "docker")

    def enter(environment):
        environment.started = True
        events.append("container_entered")
        return environment

    monkeypatch.setattr(collector.DockerSimEnvironment, "__enter__", enter)
    monkeypatch.setattr(collector.DockerSimEnvironment, "wait", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector.DockerSimEnvironment, "_progress", lambda *_args, **_kwargs: None)

    def snapshot(_environment, *, include_host_metrics=False):
        return {
            "container_id": "running-container",
            "state": {
                "running": True,
                "exit_code": 0,
                "oom_killed": False,
                "finished_at": None,
            },
            "px4_process_alive": True,
            "px4_host_pids": [42],
            "host_load_average": [48.0, 48.0, 48.0] if include_host_metrics else None,
            "gpu_memory_mib": None,
        }

    monkeypatch.setattr(collector.DockerSimEnvironment, "_container_snapshot", snapshot)

    def trigger_clock_stall(*_args, **_kwargs):
        diagnostics = SimpleNamespace(diagnostic_snapshot=lambda: {"px4_clock_s": 12.3})
        collector._raise_if_px4_clock_stalled(
            SimpleNamespace(ack={"wall_timeout": True}), 0, "posix", sim_clock=diagnostics
        )

    monkeypatch.setattr(collector, "_start_mover_driver", trigger_clock_stall)

    def fake_capture(container, episode_dir, **kwargs):
        assert events == ["container_entered"]
        events.append("capture_started_while_running")

        def runner(command, **_runner_kwargs):
            assert events[-1] == "capture_started_while_running"
            if "gz" in command:
                return SimpleNamespace(returncode=0, stdout="sim { sec: 1 nsec: 0 }", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return capture_stall(
            container,
            episode_dir,
            docker=kwargs["docker"],
            delay_s=0,
            runner=runner,
            sleep=lambda _delay: None,
            px4_clock_diagnostics=kwargs["px4_clock_diagnostics"],
            reason=kwargs["reason"],
        )

    monkeypatch.setattr(collector, "capture_px4_clock_stall", fake_capture)

    def fake_subprocess_run(command, **_kwargs):
        if command[:2] == ["docker", "logs"]:
            events.append("logs")
        elif command[:2] == ["docker", "rm"]:
            events.append("container_removed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(collector.subprocess, "run", fake_subprocess_run)

    with pytest.raises(TimeoutError, match="PX4 simulation clock stalled"):
        collector.run_episode(plan, tmp_path / "truth.json", 0, free_space_fn=lambda _p: 10**15)

    assert events.index("capture_started_while_running") < events.index("container_removed")
    episode_dirs = list((output / "episodes").glob("episode-*"))
    assert len(episode_dirs) == 1
    episode_dir = episode_dirs[0]
    capture_path = episode_dir / "px4-clock-stall-capture.json"
    assert capture_path.is_file()
    episode = json.loads((episode_dir / "episode.json").read_text())
    assert episode["status"] == "failed"
    assert episode["failure_record"]["stall_capture_path"] == str(capture_path)
    capture = json.loads(capture_path.read_text())
    assert capture["captured_before_container_removal"] is True
    assert capture["path"] == str(capture_path)
