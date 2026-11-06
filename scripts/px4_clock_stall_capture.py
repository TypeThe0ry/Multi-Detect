#!/usr/bin/env python3
"""Best-effort live PX4/Gazebo evidence capture and offline interpretation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

DEFAULT_DELAY_S = 3.0
HZ = 100


def _run(
    command: list[str],
    *,
    timeout_s: float,
    runner: Callable[..., Any] | None = None,
    input_text: str | None = None,
) -> dict[str, Any]:
    runner = subprocess.run if runner is None else runner
    row: dict[str, Any] = {"command": command, "timeout_s": timeout_s}
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout_s,
            check=False,
        )
        row.update(
            {
                "returncode": int(getattr(result, "returncode", 1)),
                "stdout": (getattr(result, "stdout", "") or "")[-20000:],
                "stderr": (getattr(result, "stderr", "") or "")[-4000:],
                "timed_out": int(getattr(result, "returncode", 1)) == 124,
            }
        )
    except BaseException as error:
        row["error"] = f"{type(error).__name__}: {error}"
        row["timed_out"] = isinstance(error, subprocess.TimeoutExpired)
        for name, fallback_name, limit in (
            ("stdout", "output", 20000),
            ("stderr", "stderr_output", 4000),
        ):
            value = getattr(error, name, None)
            if not value:
                value = getattr(error, fallback_name, None)
            if value:
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                row[name] = str(value)[-limit:]
    return row


def _parse_threads(output: str, *, clk_tck: int = HZ) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 13 or fields[0] != "T":
            continue
        try:
            pid, tid, utime, stime = (int(fields[i]) for i in (2, 3, 7, 8))
        except ValueError:
            continue
        sched_values: list[int | None] = []
        for index in (9, 10, 11):
            try:
                sched_values.append(int(fields[index]))
            except ValueError:
                sched_values.append(None)
        on_cpu_ns, run_queue_wait_ns, timeslices = sched_values
        rows.append(
            {
                "group": fields[1],
                "pid": pid,
                "tid": tid,
                "name": fields[4],
                "state": fields[5],
                "wchan": fields[6],
                "utime_ticks": utime,
                "stime_ticks": stime,
                "cpu_time_ticks": utime + stime,
                "cpu_time_s": round((utime + stime) / max(1, clk_tck), 6),
                "on_cpu_ns": on_cpu_ns,
                "run_queue_wait_ns": run_queue_wait_ns,
                "sched_timeslices": timeslices,
                "schedstat_available": on_cpu_ns is not None and run_queue_wait_ns is not None,
                "process_cmdline": fields[12],
            }
        )
    return rows


def _parse_clock(output: str) -> list[float]:
    values: list[float] = []
    for block in re.findall(r"\bsim\s*\{([^}]*)\}", output):
        sec = re.search(r"\bsec\s*:\s*(-?\d+)", block)
        nsec = re.search(r"\bnsec\s*:\s*(-?\d+)", block)
        if sec:
            values.append(float(sec.group(1)) + float(nsec.group(1) if nsec else 0) / 1e9)
    return values[-10:]


def _load_average() -> list[float] | None:
    try:
        return [round(float(value), 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _core_count() -> int | None:
    try:
        cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        cores: set[tuple[str, str]] = set()
        for record in cpuinfo.split("\n\n"):
            values = dict(
                line.split(":", 1) for line in record.splitlines() if ":" in line
            )
            physical_id, core_id = values.get("physical id"), values.get("core id")
            if physical_id is not None and core_id is not None:
                cores.add((physical_id.strip(), core_id.strip()))
        if cores:
            return len(cores)
    except (OSError, UnicodeError):
        pass
    try:
        count = os.cpu_count()
    except (AttributeError, OSError):
        return None
    return int(count) if count else None


def _exec_command(docker: str, container: str, *args: str) -> list[str]:
    return [docker, "exec", container, *args]


def _classify_process(command: str) -> str | None:
    command = command.strip()
    if command.startswith("/opt/px4-gazebo/bin/px4 -i 0"):
        return "px4"
    if command.startswith("gz sim"):
        return "gz"
    return None


def _parse_docker_top(output: str) -> list[tuple[int, str]]:
    processes: list[tuple[int, str]] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        processes.append((pid, fields[1]))
    return processes


def _read_proc_cmdline(proc_dir: Path, fallback: str = "") -> str:
    try:
        value = (proc_dir / "cmdline").read_bytes()
    except (OSError, UnicodeError):
        return fallback.strip()
    return value.replace(b"\0", b" ").decode("utf-8", errors="replace").strip() or fallback.strip()


def _read_host_threads(
    docker: str,
    container: str,
    *,
    proc_root: str | Path = "/proc",
    host_pids: Iterable[int] | None = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Read container threads from the host's procfs without docker exec."""
    runner = subprocess.run if runner is None else runner
    proc_root = Path(proc_root)
    supplied_pids = {int(pid) for pid in (host_pids or ())}
    top = _run(
        [docker, "top", container, "-eo", "pid,args"],
        timeout_s=3.0,
        runner=runner,
    )
    if top.get("error") or top.get("timed_out") or top.get("returncode") != 0:
        if not supplied_pids:
            raise RuntimeError(f"docker top failed: {top.get('error') or top.get('stderr') or top}")
        top_processes: list[tuple[int, str]] = []
    else:
        top_processes = _parse_docker_top(str(top.get("stdout", "")))

    processes: dict[int, tuple[str, str]] = {}
    for pid, listed_command in top_processes:
        processes[pid] = (listed_command, listed_command)
    for pid in supplied_pids:
        proc_dir = proc_root / str(pid)
        command = _read_proc_cmdline(proc_dir, processes.get(pid, ("", ""))[0])
        processes[pid] = (command, command)

    rows: list[dict[str, Any]] = []
    for pid, (listed_command, _) in sorted(processes.items()):
        proc_dir = proc_root / str(pid)
        command = _read_proc_cmdline(proc_dir, listed_command)
        group = _classify_process(command)
        if group is None:
            continue
        task_root = proc_dir / "task"
        try:
            task_dirs = sorted(
                (path for path in task_root.iterdir() if path.name.isdigit()),
                key=lambda path: int(path.name),
            )
        except OSError:
            continue
        for task_dir in task_dirs:
            try:
                stat_line = (task_dir / "stat").read_text(encoding="utf-8", errors="replace").strip()
                close = stat_line.rfind(")")
                if close < 0:
                    continue
                rest = stat_line[close + 2 :].split()
                if len(rest) < 13:
                    continue
                tid = int(task_dir.name)
                utime = int(rest[11])
                stime = int(rest[12])
                name = (task_dir / "comm").read_text(encoding="utf-8", errors="replace").strip() or "?"
                wchan = (task_dir / "wchan").read_text(encoding="utf-8", errors="replace").strip() or "?"
                sched_fields = (task_dir / "schedstat").read_text(
                    encoding="utf-8", errors="replace"
                ).split()
            except (OSError, UnicodeError, ValueError):
                continue
            sched_values: list[int | None] = []
            for value in sched_fields[:3]:
                try:
                    sched_values.append(int(value))
                except ValueError:
                    sched_values.append(None)
            while len(sched_values) < 3:
                sched_values.append(None)
            on_cpu_ns, run_queue_wait_ns, timeslices = sched_values
            rows.append(
                {
                    "group": group,
                    "pid": pid,
                    "tid": tid,
                    "name": name,
                    "state": rest[0],
                    "wchan": wchan,
                    "utime_ticks": utime,
                    "stime_ticks": stime,
                    "cpu_time_ticks": utime + stime,
                    "cpu_time_s": round((utime + stime) / max(1, HZ), 6),
                    "on_cpu_ns": on_cpu_ns,
                    "run_queue_wait_ns": run_queue_wait_ns,
                    "sched_timeslices": timeslices,
                    "schedstat_available": on_cpu_ns is not None and run_queue_wait_ns is not None,
                    "process_cmdline": command,
                    "read_wall_time": time.time(),
                }
            )
    if not rows and not re.search(r"(?im)^\s*PID\s+COMMAND\s*$", str(top.get("stdout", ""))):
        if top.get("error") or top.get("timed_out") or top.get("returncode") != 0:
            raise RuntimeError(
                f"docker top failed: {top.get('error') or top.get('stderr') or top}"
            )
        raise RuntimeError("host proc read found no PX4 or Gazebo threads")
    sample_wall_time = min((float(row["read_wall_time"]) for row in rows), default=time.time())
    return {
        "command": [docker, "top", container, "-eo", "pid,args"],
        "timeout_s": 3.0,
        "returncode": 0,
        "stdout": str(top.get("stdout", "")),
        "stderr": str(top.get("stderr", "")),
        "timed_out": False,
        "collection_path": "host_proc",
        "host_pids": sorted(processes),
        "sample_wall_time": sample_wall_time,
        "threads": {
            group: [row for row in rows if row["group"] == group] for group in ("px4", "gz")
        },
    }


def _read_in_container_threads(
    docker: str,
    container: str,
    *,
    runner: Callable[..., Any] | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    thread_script = r'''self_pid=$$
for d in /proc/[0-9]*; do
  [ -r "$d/cmdline" ] || continue
  pid=${d##*/}
  [ "$pid" = "$self_pid" ] && continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null || true)
  case "$cmd" in
    /opt/px4-gazebo/bin/px4\ -i\ 0*) owner=px4 ;;
    "gz sim"*) owner=gz ;;
    *) continue ;;
  esac
  for t in "$d"/task/[0-9]*; do
    [ -r "$t/stat" ] || continue
    line=$(cat "$t/stat" 2>/dev/null || true)
    rest=${line##*) }
    set -- $rest
    [ $# -ge 13 ] || continue
    tid=${t##*/}; state=$1; utime=$12; stime=$13
    name=$(cat "$t/comm" 2>/dev/null || printf '?')
    wchan=$(cat "$t/wchan" 2>/dev/null || printf '?')
    sched=$(cat "$t/schedstat" 2>/dev/null || true)
    set -- $sched
    on_cpu_ns=; run_queue_wait_ns=; timeslices=
    if [ $# -ge 3 ]; then
      on_cpu_ns=$1; run_queue_wait_ns=$2; timeslices=$3
    fi
    printf 'T\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$owner" "$pid" "$tid" "$name" "$state" "$wchan" "$utime" "$stime" \
      "$on_cpu_ns" "$run_queue_wait_ns" "$timeslices" "$cmd"
  done
done'''
    result = _run(
        _exec_command(docker, container, "sh", "-c", thread_script),
        timeout_s=10.0,
        runner=runner,
    )
    result["collection_path"] = "in_container_fallback"
    result["fallback_reason"] = fallback_reason
    result["sample_wall_time"] = time.time()
    parsed = _parse_threads(str(result.get("stdout", "")))
    result["threads"] = {
        group: [row for row in parsed if row["group"] == group] for group in ("px4", "gz")
    }
    for row in parsed:
        row["read_wall_time"] = result["sample_wall_time"]
    return result


def _sample(
    docker: str,
    container: str,
    *,
    label: str,
    runner: Callable[..., Any] | None = None,
    load_average: list[float] | None = None,
    proc_root: str | Path = "/proc",
    host_pids: Iterable[int] | None = None,
) -> dict[str, Any]:
    runner = subprocess.run if runner is None else runner
    host_load = _load_average() if load_average is None else load_average
    core_count = _core_count()
    sample: dict[str, Any] = {
        "label": label,
        "host_load_average": host_load,
        "host_core_count": core_count,
        "host_load_per_core": (
            [round(float(value) / core_count, 4) for value in host_load]
            if isinstance(host_load, list) and core_count
            else None
        ),
    }
    try:
        sample["process_threads"] = _read_host_threads(
            docker,
            container,
            proc_root=proc_root,
            host_pids=host_pids,
            runner=runner,
        )
    except BaseException as error:
        sample["process_threads"] = _read_in_container_threads(
            docker,
            container,
            runner=runner,
            fallback_reason=f"{type(error).__name__}: {error}",
        )
    sample["captured_wall_time"] = sample["process_threads"].get("sample_wall_time", time.time())
    process_threads = sample["process_threads"].get("threads") or {}
    if process_threads.get("px4"):
        sample["px4_process_status"] = "present"
    elif sample["process_threads"].get("returncode") == 0 and not sample["process_threads"].get("error"):
        sample["px4_process_status"] = "gone"
    elif "docker top failed" in str(sample["process_threads"].get("fallback_reason", "")):
        sample["px4_process_status"] = "docker_top_failed"
    else:
        sample["px4_process_status"] = "unknown"
    for key, paths in (
        ("cpu_stat", "/sys/fs/cgroup/cpu.stat /sys/fs/cgroup/cpu/cpu.stat"),
        ("memory_events", "/sys/fs/cgroup/memory.events /sys/fs/cgroup/memory/memory.events"),
    ):
        script = f"for f in {paths}; do [ -r \"$f\" ] && cat \"$f\" && exit 0; done; exit 44"
        sample[key] = _run(
            _exec_command(docker, container, "sh", "-c", script),
            timeout_s=1.5,
            runner=runner,
        )
    sample["gz_clock"] = _run(
        _exec_command(
            docker,
            container,
            "timeout",
            "5s",
            "gz",
            "topic",
            "-e",
            "-n",
            "1",
            "-t",
            "/world/default/clock",
        ),
        timeout_s=6.0,
        runner=runner,
    )
    sample["gz_clock"]["sim_time_s"] = _parse_clock(
        str(sample["gz_clock"].get("stdout", ""))
    )
    sample["gz_clock"]["latest_sim_time_s"] = (
        sample["gz_clock"]["sim_time_s"][-1] if sample["gz_clock"]["sim_time_s"] else None
    )
    sample["px4_work_queue_status"] = _run(
        _exec_command(
            docker, container, "/opt/px4-gazebo/bin/px4-work_queue", "status"
        ),
        timeout_s=4.0,
        runner=runner,
    )
    for topic in ("sensor_combined", "vehicle_status"):
        sample[f"px4_listener_{topic}"] = _run(
            _exec_command(docker, container, "/opt/px4-gazebo/bin/px4-listener", topic),
            timeout_s=3.0,
            runner=runner,
        )
    return sample


def capture_stall(
    container: str,
    episode_dir: str | Path,
    *,
    docker: str = "docker",
    delay_s: float = DEFAULT_DELAY_S,
    runner: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    load_average: list[float] | None = None,
    proc_root: str | Path = "/proc",
    host_pids: Iterable[int] | None = None,
    px4_clock_diagnostics: dict[str, Any] | None = None,
    reason: str = "PX4 simulation clock stalled",
    gdb_path: str = "/usr/bin/gdb",
    gdb_timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Capture two samples and persist them; every failure becomes data."""
    document: dict[str, Any] = {
        "schema_version": 1,
        "container_name": container,
        "reason": reason,
        "captured_before_container_removal": True,
        "px4_clock_diagnostics": px4_clock_diagnostics,
        "samples": [],
    }
    try:
        document["samples"].append(
            _sample(
                docker,
                container,
                label="deadline",
                runner=runner,
                load_average=load_average,
                proc_root=proc_root,
                host_pids=host_pids,
            )
        )
        sleep(max(0.0, float(delay_s)))
        document["samples"].append(
            _sample(
                docker,
                container,
                label=f"after_{delay_s:g}s",
                runner=runner,
                load_average=load_average,
                proc_root=proc_root,
                host_pids=host_pids,
            )
        )
    except BaseException as error:
        document["capture_error"] = f"{type(error).__name__}: {error}"
    # Collect syscall details only after both timed samples are complete.
    for sample in document["samples"]:
        _add_px4_syscalls(sample, proc_root=proc_root)
    try:
        document["analysis"] = analyze_capture(document)
    except BaseException as error:
        document["analysis_error"] = f"{type(error).__name__}: {error}"

    episode_dir = Path(episode_dir)
    primary_path = episode_dir / "px4-clock-stall-capture.json"
    gdb_output_path = episode_dir / "px4-clock-stall-gdb.txt"
    fallback_path = (
        episode_dir.parent
        / "px4-clock-stall-captures"
        / f"{episode_dir.name}-{uuid.uuid4().hex[:8]}.json"
    )
    try:
        if not episode_dir.is_dir():
            raise FileNotFoundError(f"episode directory is unavailable: {episode_dir}")
        _capture_px4_gdb(
            document,
            output_path=gdb_output_path,
            gdb_path=gdb_path,
            timeout_s=gdb_timeout_s,
            runner=runner,
        )
        document["gdb_output_path"] = str(gdb_output_path)
        document["analysis"] = analyze_capture(document)
        document["path"] = str(primary_path)
        primary_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    except BaseException as error:
        document["write_error"] = f"{type(error).__name__}: {error}"
        try:
            fallback_path.parent.mkdir(parents=True, exist_ok=True)
            fallback_gdb_path = fallback_path.with_name("px4-clock-stall-gdb.txt")
            if not document.get("gdb"):
                _capture_px4_gdb(document, output_path=fallback_gdb_path, gdb_path=gdb_path, timeout_s=gdb_timeout_s, runner=runner)
            document["gdb_output_path"] = str(fallback_gdb_path)
            document["analysis"] = analyze_capture(document)
            document["path"] = str(fallback_path)
            fallback_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        except BaseException as fallback_error:
            document["fallback_write_error"] = f"{type(fallback_error).__name__}: {fallback_error}"
            try:
                fallback_root = Path(tempfile.gettempdir()) / "multidetect-px4-stall-captures"
                fallback_root.mkdir(parents=True, exist_ok=True)
                fallback_path = fallback_root / f"{episode_dir.name}-{uuid.uuid4().hex[:8]}.json"
                document["path"] = str(fallback_path)
                fallback_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
            except BaseException as temp_error:
                document["temp_write_error"] = f"{type(temp_error).__name__}: {temp_error}"
                document.pop("path", None)
    return document


def _add_px4_syscalls(sample: dict[str, Any], *, proc_root: str | Path) -> None:
    root = Path(proc_root)
    thread_groups = ((sample.get("process_threads") or {}).get("threads") or {})
    for thread in thread_groups.get("px4", []):
        path = root / str(thread.get("pid")) / "task" / str(thread.get("tid")) / "syscall"
        try:
            thread["syscall"] = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as error:
            thread["syscall_error"] = f"{type(error).__name__}: {error}"


def _capture_px4_gdb(
    document: dict[str, Any],
    *,
    output_path: Path,
    gdb_path: str,
    timeout_s: float,
    runner: Callable[..., Any] | None,
) -> None:
    samples = document.get("samples") or []
    host_pid_values = [
        int(pid)
        for sample in samples
        if (sample.get("process_threads") or {}).get("collection_path") == "host_proc"
        for pid in ((sample.get("process_threads") or {}).get("host_pids") or [])
    ]
    pid = sorted(set(host_pid_values))[0] if host_pid_values else None
    gdb: dict[str, Any] = {"pid": pid, "timeout_s": timeout_s}
    if pid is None:
        statuses = {sample.get("px4_process_status") for sample in samples}
        if "gone" in statuses:
            status, detail = "px4_gone", "docker top succeeded but found no PX4 process"
        elif "docker_top_failed" in statuses:
            status, detail = "docker_top_failed", "cannot determine PX4 PID because docker top failed"
        else:
            status, detail = "unavailable", "no PX4 host PID was captured"
        gdb.update({"status": status, "error": detail})
        output_path.write_text(f"PX4 gdb not run: {detail}.\n", encoding="utf-8")
    elif not Path(gdb_path).is_file() and shutil.which(gdb_path) is None:
        gdb.update({"status": "unavailable", "error": f"gdb not found: {gdb_path}"})
        output_path.write_text(gdb["error"] + "\n", encoding="utf-8")
    else:
        command = [
            gdb_path, "-p", str(pid), "-batch", "-nx",
            "-ex", f"set sysroot /proc/{pid}/root",
            "-ex", "set pagination off",
            "-ex", "thread apply all bt 16",
        ]
        result = _run(command, timeout_s=timeout_s, runner=runner)
        content = str(result.get("stdout", ""))
        stderr = str(result.get("stderr", ""))
        output_path.write_text(content + ("\n" if content and stderr else "") + stderr, encoding="utf-8")
        gdb.update({"status": "timeout" if result.get("timed_out") else ("ok" if result.get("returncode") == 0 else "failed"),
                    "command": command, "returncode": result.get("returncode"),
                    "timed_out": bool(result.get("timed_out")), "error": result.get("error")})
    gdb["output_path"] = str(output_path)
    document["gdb"] = gdb


def _keyed_threads(sample: dict[str, Any], group: str) -> dict[tuple[int, int], dict[str, Any]]:
    rows = ((sample.get("process_threads") or {}).get("threads") or {}).get(group, [])
    return {
        (int(row["pid"]), int(row["tid"])): row
        for row in rows
        if isinstance(row, dict) and "pid" in row and "tid" in row
    }


def _thread_deltas(first: dict[str, Any], second: dict[str, Any]) -> list[dict[str, Any]]:
    deltas: list[dict[str, Any]] = []
    for group in ("px4", "gz"):
        before_rows = _keyed_threads(first, group)
        after_rows = _keyed_threads(second, group)
        for key in sorted(before_rows.keys() & after_rows.keys()):
            before, after = before_rows[key], after_rows[key]
            runtime_a, runtime_b = before.get("on_cpu_ns"), after.get("on_cpu_ns")
            wait_a, wait_b = before.get("run_queue_wait_ns"), after.get("run_queue_wait_ns")
            read_a, read_b = before.get("read_wall_time"), after.get("read_wall_time")
            read_interval = (
                round(float(read_b) - float(read_a), 6)
                if isinstance(read_a, (int, float))
                and isinstance(read_b, (int, float))
                and read_b >= read_a
                else None
            )
            deltas.append(
                {
                    "group": group,
                    "pid": key[0],
                    "tid": key[1],
                    "name": after.get("name"),
                    "process_cmdline": after.get("process_cmdline"),
                    "state_before": before.get("state"),
                    "state_after": after.get("state"),
                    "wchan_before": before.get("wchan"),
                    "wchan_after": after.get("wchan"),
                    "thread_read_interval_s": read_interval,
                    "on_cpu_delta_s": (
                        round((int(runtime_b) - int(runtime_a)) / 1e9, 6)
                        if isinstance(runtime_a, int)
                        and isinstance(runtime_b, int)
                        and runtime_b >= runtime_a
                        else None
                    ),
                    "run_queue_wait_delta_s": (
                        round((int(wait_b) - int(wait_a)) / 1e9, 6)
                        if isinstance(wait_a, int)
                        and isinstance(wait_b, int)
                        and wait_b >= wait_a
                        else None
                    ),
                    "cpu_time_delta_s": round(
                        float(after.get("cpu_time_s", 0)) - float(before.get("cpu_time_s", 0)),
                        6,
                    ),
                }
            )
    return deltas


def _counter(text: str, key: str) -> int | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s+(\d+)\s*$", text)
    return int(match.group(1)) if match else None


def analyze_capture(document: dict[str, Any]) -> dict[str, Any]:
    """State which explanations the two samples support, with evidence limits."""
    samples = document.get("samples") if isinstance(document.get("samples"), list) else []
    first, second = (samples + [{}, {}])[:2]
    gz_first = (first.get("gz_clock") or {}).get("latest_sim_time_s")
    gz_second = (second.get("gz_clock") or {}).get("latest_sim_time_s")
    gz_first_probe = first.get("gz_clock") or {}
    gz_second_probe = second.get("gz_clock") or {}
    first_threads = first.get("process_threads") or {}
    second_threads = second.get("process_threads") or {}
    wall_first = first_threads.get("sample_wall_time", first.get("captured_wall_time"))
    wall_second = second_threads.get("sample_wall_time", second.get("captured_wall_time"))
    sample_interval_s = (
        round(float(wall_second) - float(wall_first), 6)
        if isinstance(wall_first, (int, float))
        and isinstance(wall_second, (int, float))
        and wall_second >= wall_first
        else None
    )
    gz_advanced: bool | str
    if (
        not gz_first_probe.get("timed_out")
        and not gz_second_probe.get("timed_out")
        and isinstance(gz_first, (int, float))
        and isinstance(gz_second, (int, float))
    ):
        gz_advanced = bool(gz_second > gz_first)
    else:
        gz_advanced = "unknown"

    deltas = _thread_deltas(first, second)
    px4_deltas = [row for row in deltas if row["group"] == "px4"]
    px4_gone = any(sample.get("px4_process_status") == "gone" for sample in samples[:2])
    all_px4_threads = [
        row
        for sample in samples[:2]
        for row in (((sample.get("process_threads") or {}).get("threads") or {}).get("px4") or [])
    ]
    blocked_threads = [
        {
            "pid": row.get("pid"), "tid": row.get("tid"), "name": row.get("name"),
            "state": row.get("state"), "wchan": row.get("wchan"),
            "syscall": row.get("syscall"),
        }
        for row in all_px4_threads
        if str(row.get("state", "")).startswith(("S", "D"))
        and row.get("wchan") not in (None, "", "0", "?")
    ]
    futex_addresses: dict[str, list[dict[str, Any]]] = {}
    for row in all_px4_threads:
        syscall = str(row.get("syscall", ""))
        # x86_64 futex syscall: 202 futex(uaddr, ...); retain the address only.
        match = re.match(r"\s*202\s+0x([0-9a-fA-F]+)", syscall)
        if match:
            futex_addresses.setdefault("0x" + match.group(1).lower(), []).append(
                {"pid": row.get("pid"), "tid": row.get("tid"), "name": row.get("name")}
            )
    shared_futexes = {
        address: threads for address, threads in futex_addresses.items() if len(threads) > 1
    }
    clock_thread_top_frames = _gdb_clock_thread_frames(document, blocked_threads)
    px4_client_failures = []
    for sample_index, sample in enumerate(samples[:2]):
        for key, value in sample.items():
            if key.startswith("px4_listener_") or key == "px4_work_queue_status":
                if isinstance(value, dict) and value.get("returncode") not in (None, 0):
                    px4_client_failures.append({
                        "sample": sample_index,
                        "probe": key,
                        "returncode": value.get("returncode"),
                        "stderr": value.get("stderr", ""),
                    })
    driver_names = {"wq:rate_ctrl", "wkr_hrt", "wq:INS0"}
    clock_drivers = [row for row in px4_deltas if row.get("name") in driver_names]
    driver_names_seen = {str(row.get("name")) for row in clock_drivers}
    driver_coverage_complete = driver_names.issubset(driver_names_seen)
    px4_progress = any(
        isinstance(row.get("on_cpu_delta_s"), (int, float)) and row["on_cpu_delta_s"] > 0.01
        for row in clock_drivers
    )
    blocked_drivers = [
        row
        for row in clock_drivers
        if str(row.get("state_after", "")).startswith(("S", "D"))
        and row.get("wchan_after") not in (None, "", "0", "?")
        and isinstance(row.get("on_cpu_delta_s"), (int, float))
        and row["on_cpu_delta_s"] <= 0.01
    ]
    starved_drivers = [
        row
        for row in clock_drivers
        if str(row.get("state_after", "")).startswith("R")
        and isinstance(row.get("on_cpu_delta_s"), (int, float))
        and isinstance(row.get("run_queue_wait_delta_s"), (int, float))
        and row["run_queue_wait_delta_s"] >= 0.05
        and row["run_queue_wait_delta_s"] > max(row["on_cpu_delta_s"], 0.001) * 10
    ]
    px4_blocked = len(blocked_drivers) >= 2
    cpu_a = str((first.get("cpu_stat") or {}).get("stdout", ""))
    cpu_b = str((second.get("cpu_stat") or {}).get("stdout", ""))
    throttled = any(
        value_b is not None and value_a is not None and value_b > value_a
        for key in ("nr_throttled", "throttled_usec")
        for value_a, value_b in [(_counter(cpu_a, key), _counter(cpu_b, key))]
    )
    host_load = [
        sample.get("host_load_average")
        for sample in (first, second)
        if isinstance(sample.get("host_load_average"), list)
    ]
    host_load_per_core = [
        sample.get("host_load_per_core")
        for sample in (first, second)
        if isinstance(sample.get("host_load_per_core"), list)
    ]
    return {
        "gz_clock_advanced": gz_advanced,
        "px4_process_status": "px4_gone" if px4_gone else next(
            (sample.get("px4_process_status") for sample in samples[:2]
             if sample.get("px4_process_status") in ("present", "docker_top_failed")),
            "unknown",
        ),
        "px4_gone": px4_gone,
        "gz_clock_samples_s": [gz_first, gz_second],
        "sample_interval_s": sample_interval_s,
        "thread_deltas": deltas,
        "clock_driver_threads": clock_drivers,
        "clock_driver_coverage_complete": driver_coverage_complete,
        "px4_thread_progress": px4_progress,
        "px4_threads_appear_blocked": px4_blocked,
        "px4_blocked_clock_driver_threads": blocked_drivers,
        "px4_blocked_threads": blocked_threads,
        "shared_futex_addresses": shared_futexes,
        "clock_thread_top_frames": clock_thread_top_frames,
        "px4_client_failures": px4_client_failures,
        "px4_runnable_clock_driver_threads_with_high_wait": starved_drivers,
        "cgroup_cpu_throttling_increased": throttled,
        "host_load_observed": host_load,
        "host_load_per_core_observed": host_load_per_core,
        "supports": {
            "bridge_input_or_lockstep_path": (
                gz_advanced is True and driver_coverage_complete and not px4_progress
            ),
            "px4_thread_block": gz_advanced is True and px4_blocked,
            "scheduling_starvation": len(starved_drivers) >= 2,
        },
        "limits": [
            "Gazebo clock advancing while PX4 clock-driving threads are idle supports a bridge/lockstep boundary, but cannot separate bridge input loss from a PX4-side blocked thread.",
            "Host load per CPU and cgroup counters are context; starvation support comes from runnable clock-driving threads' scheduler wait and on-CPU deltas.",
        ],
    }


def _gdb_clock_thread_frames(
    document: dict[str, Any], blocked_threads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    gdb = document.get("gdb") or {}
    output_path = gdb.get("output_path") or document.get("gdb_output_path")
    if not output_path:
        return []
    try:
        output = Path(output_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    blocks = re.split(r"(?=^Thread\s+\d+\s+)", output, flags=re.MULTILINE)
    blocked_tids = {int(row["tid"]) for row in blocked_threads if isinstance(row.get("tid"), int)}
    summaries: list[dict[str, Any]] = []
    for block in blocks:
        match = re.match(r"Thread\s+\d+\s+\(LWP\s+(\d+)\)", block)
        if not match or int(match.group(1)) not in blocked_tids:
            continue
        frames = re.findall(r"^\s*#\d+\s+.*$", block, flags=re.MULTILINE)
        summaries.append({"tid": int(match.group(1)), "top_frames": frames[:4]})
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args(argv)
    document = json.loads(args.capture.read_text(encoding="utf-8"))
    print(json.dumps(analyze_capture(document), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
