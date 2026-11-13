from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.procfs_futex_owner as futex_owner
import scripts.px4_clock_stall_capture as stall_capture
from scripts.px4_clock_stall_capture import (
    GDB_STDERR_LIMIT_CHARS,
    GDB_STDOUT_LIMIT_CHARS,
    _capture_gz_gdb,
    _capture_px4_gdb,
    _gdb_blocks,
    _parse_gz_topic_info,
    _parse_transport_sockets,
    _parse_world_stats,
    _read_host_threads,
    _run,
    analyze_capture,
    capture_stall,
)


@pytest.fixture(autouse=True)
def clear_futex_proof_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(stall_capture.FUTEX_PROOF_ENV, raising=False)


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
    assert len(commands) == 22
    assert all("pxh" not in command for command in commands)
    gz_commands = [command for command in commands if "gz" in command]
    assert len(gz_commands) == 6
    assert [command[3:] for command in gz_commands] == [
        ["gz", "topic", "-i", "-t", "/world/default/clock"],
        ["timeout", "5s", "gz", "topic", "-e", "-n", "1", "-t", "/world/default/clock"],
        ["timeout", "3s", "gz", "topic", "-e", "-n", "1", "-t", "/world/default/stats"],
    ] * 2
    assert saved["samples"][0]["transport_probe_order"] == [
        "proc_net_sockets", "process_threads", "gz_topic_info"
    ]
    assert saved["samples"][1]["transport_probe_order"] == [
        "proc_net_sockets", "process_threads", "gz_topic_info"
    ]
    first_topic_call = next(
        index for index, command in enumerate(commands) if "gz topic" in " ".join(command)
    )
    first_sample_px4_probe_call = max(
        index for index, command in enumerate(commands)
        if index < first_topic_call
        and any("px4-listener" in part or "px4-work_queue" in part for part in command)
    )
    assert first_sample_px4_probe_call < first_topic_call
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
    assert saved["gz_gdb"]["status"] == "unavailable"
    assert Path(saved["gz_gdb_output_path"]).is_file()


def test_world_stats_parser_reads_pause_flag_and_sim_time() -> None:
    result = _parse_world_stats("paused: true\nsim { sec: 49 nsec: 116000000 }\n")
    assert result == {"paused": True, "sim_time_s": [49.116]}

    assert _parse_world_stats("sim { sec: 50 nsec: 0 }\n")["paused"] is None
    assert _parse_world_stats("pause: false\nsim_time { sec: 51 nsec: 2 }\n") == {
        "paused": False,
        "sim_time_s": [51.000000002],
    }


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
        stat_fields = [state, *(["0"] * 10), "123", "45", *(["0"] * 6), str(9000 + tid)]
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
    assert result["threads"]["px4"][0]["starttime_ticks"] == 9101
    assert result["threads"]["px4"][1]["starttime_ticks"] == 9102
    assert result["threads"]["gz"][0]["starttime_ticks"] == 9202
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


def test_analyze_capture_names_polling_reception_when_clock_probe_times_out(
    tmp_path: Path,
) -> None:
    output = tmp_path / "gdb.txt"
    output.write_text(
        "=== GDB before_delay ===\nThread 2 (LWP 201 \"px4\"):\n"
        "#0 poll ()\n#1 gz::transport::v13::NodeShared::RunReceptionTask()\n"
        "=== GDB after_delay ===\nThread 2 (LWP 201 \"px4\"):\n"
        "#0 poll ()\n#1 gz::transport::v13::NodeShared::RunReceptionTask()\n"
    )
    samples = [_sample(clock=None, threads=[]), _sample(clock=None, threads=[])]
    for sample in samples:
        sample["gz_clock"]["timed_out"] = True
    result = analyze_capture({"samples": samples, "gdb": {"output_path": str(output)}})
    assert result["gz_clock_advanced"] == "unknown"
    assert result["reception_thread_classification"] == "reception_polling_clock_unknown"
    assert result["callback_thread_classification"] == "not_observed"


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


@pytest.fixture
def large_thread_backtrace() -> str:
    # A 129-thread Gazebo capture exceeded the old 20k tail, losing the owner.
    return "".join(
        f"Thread {index} (LWP {1000 + index}):\n"
        f"#0 {'mutex_owner' if index == 129 else 'waiter'}_{index} "
        + "frame_detail " * 30 + "\n"
        for index in range(129, 0, -1)
    )


@pytest.mark.parametrize("group,capture", [("px4", _capture_px4_gdb), ("gz", _capture_gz_gdb)])
def test_gdb_keeps_every_thread_above_old_tail_limit(
    tmp_path: Path, large_thread_backtrace: str, group: str, capture
) -> None:
    assert 20000 < len(large_thread_backtrace) < GDB_STDOUT_LIMIT_CHARS
    document = {"samples": [{"process_threads": {
        "collection_path": "host_proc", "host_pids": [321],
        "threads": {group: [_thread(group=group, pid=321, tid=321)]},
    }}]}
    executable = tmp_path / "fake-gdb"
    executable.touch()
    output = tmp_path / "gdb.txt"

    def runner(command, **kwargs):
        assert kwargs["timeout"] == 7.5
        return SimpleNamespace(returncode=0, stdout=large_thread_backtrace, stderr="warning")

    capture(document, output_path=output, gdb_path=str(executable), timeout_s=7.5, runner=runner)
    key = "gdb" if group == "px4" else "gz_gdb"
    snapshot = document[key]["snapshots"][0]
    assert document[key]["status"] == "ok"
    assert snapshot["stdout_retention"]["original_chars"] == len(large_thread_backtrace)
    assert snapshot["stdout_retention"]["truncated"] is False
    saved = output.read_text()
    assert large_thread_backtrace in saved
    assert "mutex_owner_129" in saved and "waiter_1 " in saved
    assert '"truncated": false' in saved
    # Retention headers must not become spurious GDB snapshot delimiters.
    blocks = _gdb_blocks({"gdb": {"output_path": str(output)}})
    assert [label for label, _ in blocks] == ["snapshot"]
    assert large_thread_backtrace in blocks[0][1]


@pytest.mark.parametrize("outcome", ["success", "timeout_exception", "timeout_returncode"])
def test_gdb_oversize_output_has_bounded_endpoints_and_exact_truncation(
    tmp_path: Path, outcome: str
) -> None:
    stdout = "Thread 999 mutex_owner FIRST\n" + "x" * GDB_STDOUT_LIMIT_CHARS + "\nThread 1 LAST"
    stderr = "FIRST_WARNING\n" + "w" * GDB_STDERR_LIMIT_CHARS + "\nLAST_WARNING"
    document = {"samples": [{"process_threads": {
        "collection_path": "host_proc", "host_pids": [321],
        "threads": {"px4": [_thread(pid=321, tid=321)]},
    }}]}
    executable = tmp_path / "fake-gdb"
    executable.touch()
    output = tmp_path / "gdb.txt"

    def runner(command, **kwargs):
        if outcome == "timeout_exception":
            # subprocess.TimeoutExpired can expose bytes even with text=True.
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=stdout.encode(), stderr=stderr.encode())
        return SimpleNamespace(returncode=124 if outcome == "timeout_returncode" else 0, stdout=stdout, stderr=stderr)

    _capture_px4_gdb(document, output_path=output, gdb_path=str(executable), timeout_s=7.5, runner=runner)
    snapshot = document["gdb"]["snapshots"][0]
    assert snapshot["status"] == ("ok" if outcome == "success" else "timeout")
    saved = output.read_text()
    for name, value, limit, first, last in (
        ("stdout", stdout, GDB_STDOUT_LIMIT_CHARS, "Thread 999 mutex_owner FIRST", "Thread 1 LAST"),
        ("stderr", stderr, GDB_STDERR_LIMIT_CHARS, "FIRST_WARNING", "LAST_WARNING"),
    ):
        retention = snapshot[f"{name}_retention"]
        assert retention["original_chars"] == len(value)
        assert retention["stored_chars"] == retention["limit_chars"] == limit
        assert retention["truncated"] is True
        assert retention["mode"] == "head_tail"
        assert retention["omitted_chars"] == len(value) - retention["retained_chars"] > 0
        assert first in saved and last in saved
    assert saved.count("[... output truncated: middle omitted;") == 2
    assert '"truncated": true' in saved


@pytest.mark.parametrize("timed_out", [False, True])
def test_non_gdb_command_keeps_original_default_tail_limits(timed_out: bool) -> None:
    stdout, stderr = "FIRST" + "x" * 25000, "FIRST" + "w" * 5000

    def runner(command, **kwargs):
        if timed_out:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=stdout, stderr=stderr)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)

    row = _run(["fake-topic-probe"], timeout_s=5, runner=runner)
    assert row["stdout"] == stdout[-20000:]
    assert row["stderr"] == stderr[-4000:]
    assert row["stdout_retention"]["mode"] == "tail"
    assert row["stdout_retention"]["original_chars"] == len(stdout)
    assert row["timed_out"] is timed_out


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
    monkeypatch.setattr(collector, "resolve_px4_logger_mode", lambda _mode=None: "off")
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


def test_analyze_capture_keeps_camera_unknown_and_reports_imu_progress(tmp_path: Path) -> None:
    output = tmp_path / "gz-gdb.txt"
    output.write_text(
        "=== GDB before_delay ===\n"
        "Thread 1 (LWP 300):\n"
        "#0 pthread_mutex_lock ()\n#1 gz::sim::SimulationRunner::PublishStats()\n"
        "#2 gz::sim::SimulationRunner::Step()\n"
        "=== GDB after_delay ===\n"
        "Thread 1 (LWP 300):\n"
        "#0 pthread_mutex_lock ()\n#1 gz::sim::SimulationRunner::PublishStats()\n"
        "#2 gz::sim::SimulationRunner::Step()\n"
    )
    samples = [_sample(clock=None, threads=[]), _sample(clock=None, threads=[])]
    samples[0]["px4_listener_sensor_combined"] = {"returncode": 0, "stdout": "timestamp_sample: 100\n"}
    samples[1]["px4_listener_sensor_combined"] = {"returncode": 0, "stdout": "timestamp_sample: 200\n"}
    result = analyze_capture({
        "samples": samples,
        "gz_gdb": {"output_path": str(output)},
    })
    assert result["imu_progress"]["advanced"] is True
    assert result["camera_progress"]["advanced"] == "unknown"
    assert result["gz_publish_stats_mutex_wait_observed"] is True
    assert result["gz_publish_stats_mutex_wait_both_snapshots"] is True
    assert result["supports"]["gz_publish_stats_mutex_wait"] is True
    assert result["gz_runner_stack_classification"][0]["classification"] == "publish_stats_mutex_wait"


def test_sensor_progress_unknown_when_listener_has_no_timestamp() -> None:
    samples = [_sample(clock=None, threads=[]), _sample(clock=None, threads=[])]
    samples[0]["px4_listener_sensor_combined"] = {"returncode": 124, "stdout": ""}
    samples[1]["px4_listener_sensor_combined"] = {"returncode": 124, "stdout": ""}
    result = analyze_capture({"samples": samples})
    assert result["imu_progress"]["advanced"] == "unknown"
    assert result["camera_progress"]["advanced"] == "unknown"


def _owner_sample() -> dict:
    main = _thread(group="gz", pid=202, tid=202)
    main.update(starttime_ticks=9202)
    return {"process_threads": {"collection_path": "host_proc", "threads": {"gz": [main]}}}


def test_host_sample_to_real_observer_roundtrip_uses_only_fake_proc_files(tmp_path) -> None:
    proc_root = tmp_path / "fake-proc"
    process = proc_root / "202"
    process.mkdir(parents=True)
    stat_fields = ["S", *(["0"] * 18), "9202"]
    (process / "stat").write_text("202 (fake) " + " ".join(stat_fields))
    (process / "cmdline").write_bytes(b"gz sim --fixture-only\0")
    elf = bytearray(64)
    elf[:6], elf[18:20] = b"\x7fELF\x02\x01", (62).to_bytes(2, "little")
    (process / "exe").write_bytes(elf)
    library = process / "root/usr/lib/libc.so.6"
    library.parent.mkdir(parents=True)
    library.write_bytes(elf)
    (process / "maps").write_text(
        "1000-2000 rw-p 0000 00:00 0\n3000-4000 r-xp 0000 00:00 0 /usr/lib/libc.so.6\n"
    )
    memory = bytearray(0x1000 + 20)
    memory[0x1000:] = struct.pack("<iIiIi", 2, 2, 8, 1, 1)
    (process / "mem").write_bytes(memory)
    for tid, namespace_tid in ((202, 7), (203, 8)):
        task = process / "task" / str(tid)
        task.mkdir(parents=True)
        (task / "stat").write_text(f"{tid} (fake) " + " ".join(stat_fields))
        (task / "comm").write_text("fixture-only")
        (task / "wchan").write_text("futex_do_wait")
        (task / "schedstat").write_text("0 0 0")
        (task / "status").write_text(
            f"Tgid:\t202\nPid:\t{tid}\nNSpid:\t{tid}\t{namespace_tid}\nState:\tS (sleeping)\n"
        )
        (task / "syscall").write_text(
            "202 0x1000 0x80 0x2 0x0 0x0 0x0 0x0 0x0" if tid == 202 else "running"
        )
    source = _read_host_threads(
        "fake-docker",
        "fake-container",
        proc_root=proc_root,
        runner=lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="PID COMMAND\n202 gz sim --fixture-only\n", stderr=""
        ),
    )
    proof = {
        "architecture": "x86_64",
        "endian": "little",
        "sizeof_mutex": 40,
        "alignof_mutex": 8,
        "offsets": futex_owner.OFFSETS,
        "recursive_kind": 1,
        "verification": "owned-pthread-child-raw-owner-roundtrip",
        "libc_sha256": hashlib.sha256(elf).hexdigest(),
        "proof_id": "synthetic-only",
    }
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(proof))
    config, proofs = stall_capture._futex_owner_configuration(path)
    sample = {"process_threads": source}
    stall_capture._add_futex_owner_observation(sample, config, proofs, proc_root=proc_root)
    result = sample["procfs_futex_owner_candidate"]
    assert result["status"] == "stable_recursive_mutex_candidate"
    observed = result["observation"]
    assert observed["counters"] == {"process_memory_reads": 2, "process_memory_bytes": 40}
    candidate = observed["processes"][0]["mutex_owner_candidate"]
    assert candidate["owner_host_lwp_candidate"] == 203
    assert candidate["owner_namespace_tid_candidate"] == 8
    assert candidate["node_shared_mutex_verified"] is False
    assert candidate["deadlock_cycle_proven"] is False


def test_futex_proof_opt_in_argument_precedes_environment_and_binds_exact_bytes(
    tmp_path, monkeypatch
) -> None:
    proof_path = tmp_path / "layout.json"
    raw = b'{"libc_sha256": "' + b"a" * 64 + b'", "proof_id": "reviewed"}\n'
    proof_path.write_bytes(raw)
    monkeypatch.setenv(stall_capture.FUTEX_PROOF_ENV, str(tmp_path / "missing.json"))
    config, proofs = stall_capture._futex_owner_configuration(proof_path)
    assert config["status"] == "configured"
    assert config["source"] == "argument"
    assert config["sha256"] == hashlib.sha256(raw).hexdigest()
    assert config["hash_scope"] == "full_file"
    assert config["read_bytes"] == len(raw)
    assert proofs == [json.loads(raw)]
    assert stall_capture._futex_owner_configuration(None)[0]["status"] == "observation_error"
    assert stall_capture._futex_owner_configuration("")[0]["status"] == "disabled"
    monkeypatch.setenv(stall_capture.FUTEX_PROOF_ENV, str(proof_path))
    assert (
        stall_capture._futex_owner_configuration(None)[0]["source"] == stall_capture.FUTEX_PROOF_ENV
    )


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"{}", b"x" * (64 * 1024 + 1)])
def test_bad_futex_proof_is_observation_error_without_calling_observer(
    tmp_path, monkeypatch, payload
) -> None:
    path = tmp_path / "proof.json"
    path.write_bytes(payload)
    config, proofs = stall_capture._futex_owner_configuration(path)
    assert config["status"] == "observation_error" and not proofs
    calls = []
    monkeypatch.setattr(
        futex_owner, "observe_known_tasks", lambda *a, **k: calls.append((a, k))
    )
    sample = _owner_sample()
    stall_capture._add_futex_owner_observation(sample, config, proofs, proc_root=tmp_path)
    assert sample["procfs_futex_owner_candidate"]["status"] == "observation_error"
    assert calls == []
    if len(payload) > 64 * 1024:
        assert config["hash_scope"] == "bounded_prefix"


@pytest.mark.parametrize(
    "invalid",
    ["fallback", "two_pids", "duplicate_main", "missing_start", "bool_start", "unknown_command"],
)
def test_futex_owner_requires_one_identified_host_gz_main(tmp_path, monkeypatch, invalid) -> None:
    sample = _owner_sample()
    source = sample["process_threads"]
    main = source["threads"]["gz"][0]
    if invalid == "fallback":
        source["collection_path"] = "in_container_fallback"
    elif invalid == "two_pids":
        source["threads"]["gz"].append({**main, "pid": 203, "tid": 203})
    elif invalid == "duplicate_main":
        source["threads"]["gz"].append(dict(main))
    elif invalid == "missing_start":
        main.pop("starttime_ticks")
    elif invalid == "bool_start":
        main["starttime_ticks"] = True
    else:
        main["process_cmdline"] = "gz simulator-unknown"
    calls = []
    monkeypatch.setattr(
        futex_owner, "observe_known_tasks", lambda *a, **k: calls.append((a, k))
    )
    stall_capture._add_futex_owner_observation(
        sample, {"status": "configured"}, [{}], proc_root=tmp_path
    )
    assert sample["procfs_futex_owner_candidate"]["status"] == "observation_error"
    assert calls == []


@pytest.mark.parametrize("outcome", ["denied", "unsupported"])
def test_futex_owner_errors_remain_explicit_observation_data(
    tmp_path, monkeypatch, outcome
) -> None:
    sample = _owner_sample()

    def observe(*args, **kwargs):
        if outcome == "denied":
            raise PermissionError("fixture denied")
        return {
            "processes": [
                {
                    "host_pid": 202,
                    "errors": [],
                    "mutex_owner_candidate": {
                        "status": "observation_error",
                        "error": "unsupported libc SHA",
                        "candidate_only": True,
                    },
                }
            ],
            "errors": [],
            "counters": {"process_memory_bytes": 0},
        }

    monkeypatch.setattr(futex_owner, "observe_known_tasks", observe)
    stall_capture._add_futex_owner_observation(
        sample, {"status": "configured"}, [{}], proc_root=tmp_path
    )
    assert sample["procfs_futex_owner_candidate"]["status"] == "observation_error"


@pytest.mark.parametrize("enabled", [False, True])
def test_capture_owner_observation_once_before_first_gdb_preserves_budget_and_order(
    tmp_path, monkeypatch, enabled
) -> None:
    events = []
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps({"libc_sha256": "a" * 64}))
    if enabled:
        monkeypatch.setenv(stall_capture.FUTEX_PROOF_ENV, str(proof_path))

    def sample(*args, **kwargs):
        assert kwargs["collect_gz_topics"] is False
        events.append("procfs:" + kwargs["label"])
        return {**_owner_sample(), "label": kwargs["label"]}

    def observe(known, proofs, **kwargs):
        assert enabled, "default must not call owner observer"
        assert kwargs == {"candidate_pid": 202, "proc_root": tmp_path / "fake-proc"}
        assert known["process_threads"]["threads"]["gz"][0]["starttime_ticks"] == 9202
        assert proofs == [{"libc_sha256": "a" * 64}]
        events.append("owner")
        return {
            "errors": [],
            "processes": [
                {
                    "host_pid": 202,
                    "errors": [],
                    "mutex_owner_candidate": {
                        "status": "stable_recursive_mutex_candidate",
                        "candidate_only": True,
                        "node_shared_mutex_verified": False,
                        "deadlock_cycle_proven": False,
                    },
                }
            ],
        }

    def gdb(group):
        def capture(document, **kwargs):
            assert kwargs["timeout_s"] == 7.5
            events.append(group + ":" + kwargs["snapshot_label"])

        return capture

    monkeypatch.setattr(stall_capture, "_sample", sample)
    monkeypatch.setattr(futex_owner, "observe_known_tasks", observe)
    monkeypatch.setattr(stall_capture, "_capture_px4_gdb", gdb("px4"))
    monkeypatch.setattr(stall_capture, "_capture_gz_gdb", gdb("gz"))
    monkeypatch.setattr(
        stall_capture,
        "_collect_gz_topic_probes",
        lambda s, *a, **k: events.append("topics:" + s["label"]),
    )
    document = capture_stall(
        "fake",
        tmp_path,
        proc_root=tmp_path / "fake-proc",
        sleep=lambda seconds: events.append(f"sleep:{seconds}"),
    )
    assert events == (
        ["procfs:deadline"]
        + (["owner"] if enabled else [])
        + [
            "px4:before_delay",
            "gz:before_delay",
            "topics:deadline",
            "sleep:3.0",
            "procfs:after_3s",
            "px4:after_delay",
            "gz:after_delay",
            "topics:after_3s",
        ]
    )
    assert document["capture_budget"]["gdb_total_timeout_s"] == 30.0
    assert len(document["samples"]) == 2
    assert "procfs_futex_owner_candidate" not in document["samples"][1]
    if enabled:
        first = document["samples"][0]["procfs_futex_owner_candidate"]
        assert first["status"] == "stable_recursive_mutex_candidate"
        assert first["proof"]["sha256"] == hashlib.sha256(proof_path.read_bytes()).hexdigest()
        assert (
            first["observation"]["processes"][0]["mutex_owner_candidate"]["deadlock_cycle_proven"]
            is False
        )
    else:
        assert "procfs_futex_owner_candidate" not in document["samples"][0]
