#!/usr/bin/env python3
"""只读采样已有容器的 Gazebo stats、GPU 显存和主机负载，不启动仿真。"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import threading
import time
from statistics import median
import xml.etree.ElementTree as ET
from pathlib import Path

TOPIC = "/world/default/stats"


def run_read(command: list[str], timeout: float = 10) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{command[0]} exited {result.returncode}: {result.stderr.strip()[:500]}")
    return result.stdout


def find_container(name: str | None, prefix: str | None, wait_s: float) -> tuple[str, str]:
    """Resolve once to an immutable ID; never attach to a replacement container."""
    deadline = time.monotonic() + wait_s
    while True:
        rows = [line.split("\t", 1) for line in run_read(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"]
        ).splitlines() if "\t" in line]
        matches = [(cid, actual) for cid, actual in rows
                   if actual == name or (prefix is not None and actual.startswith(prefix))]
        if len(matches) > 1:
            raise ValueError(f"container prefix is ambiguous: {[actual for _, actual in matches]}")
        if matches:
            return matches[0]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("no matching running container before wait deadline")
        time.sleep(min(1.0, remaining))


def stamp_seconds(value: object) -> float:
    if not isinstance(value, dict):
        raise ValueError("missing time stamp")
    result = float(value.get("sec", 0)) + float(value.get("nsec", 0)) / 1e9
    if not math.isfinite(result):
        raise ValueError("non-finite time stamp")
    return result


def normalize_stats(message: dict, monotonic_s: float, wall_s: float) -> dict:
    if not isinstance(message, dict):
        raise ValueError("stats message must be an object")
    sim = message.get("simTime", message.get("sim_time"))
    real = message.get("realTime", message.get("real_time"))
    if sim is None or real is None:
        raise ValueError("stats lacks simTime/realTime")
    paused = message.get("paused", message.get("pause"))
    if paused is not None and not isinstance(paused, bool):
        raise ValueError("stats pause field must be boolean")
    return {"host_monotonic_s": monotonic_s, "host_wall_s": wall_s,
            "sim_time_s": stamp_seconds(sim), "real_time_s": stamp_seconds(real),
            "paused": paused, "raw": message}


def parse_gpu(xml: str, owned_pids: set[str]) -> dict:
    root = ET.fromstring(xml)
    gpu = root.find("gpu")
    if gpu is None:
        raise ValueError("nvidia-smi returned no GPU")

    def mib(text: str | None) -> float | None:
        if not text or not text.endswith(" MiB"):
            return None
        value = float(text.split()[0])
        return value if math.isfinite(value) and value >= 0 else None

    memory = []
    unknown = False
    for process in gpu.findall("./processes/process_info"):
        if process.findtext("pid") in owned_pids:
            amount = mib(process.findtext("used_memory"))
            unknown |= amount is None
            if amount is not None:
                memory.append(amount)
    return {"device_memory_used_mib": mib(gpu.findtext("./fb_memory_usage/used")),
            "device_memory_total_mib": mib(gpu.findtext("./fb_memory_usage/total")),
            # An absent matching GPU process is not proof of zero allocations.
            "container_process_memory_mib": sum(memory) if memory and not unknown else None,
            "container_gpu_process_count": len(memory), "container_host_pids": sorted(owned_pids)}


def host_sample() -> dict:
    pressure = {}
    for resource in ("cpu", "memory", "io"):
        try:
            pressure[resource] = Path(f"/proc/pressure/{resource}").read_text().strip()
        except OSError:
            pressure[resource] = None
    return {"loadavg": list(os.getloadavg()), "cpu_count": os.cpu_count(), "pressure": pressure}


def window_rtf_median(stats: list[dict]) -> float | None:
    """Median of adjacent Gazebo Δsim/Δreal, including a stalled (zero RTF) clock.

    Shared by the fixed measurement and the collection watchdog. A clock reset
    invalidates the window; host wall time is never substituted for Gazebo time.
    """
    values = []
    for previous, current in zip(stats, stats[1:]):
        sim = current["sim_time_s"] - previous["sim_time_s"]
        real = current["real_time_s"] - previous["real_time_s"]
        if not math.isfinite(sim) or not math.isfinite(real) or sim < 0 or real <= 0:
            return None
        values.append(sim / real)
    return median(values) if values else None


def summarize(stats: list[dict], resources: list[dict], errors: list[str]) -> dict:
    issues = list(errors)
    sim_delta = real_delta = host_delta = rtf = host_rtf = None
    window_rtfs: list[float] = []
    if len(stats) < 2:
        issues.append("fewer than two valid Gazebo stats messages")
    else:
        sim_delta = stats[-1]["sim_time_s"] - stats[0]["sim_time_s"]
        real_delta = stats[-1]["real_time_s"] - stats[0]["real_time_s"]
        host_delta = stats[-1]["host_monotonic_s"] - stats[0]["host_monotonic_s"]
        reset = any(b["sim_time_s"] < a["sim_time_s"] or b["real_time_s"] <= a["real_time_s"]
                    for a, b in zip(stats, stats[1:]))
        if reset or sim_delta <= 0 or real_delta <= 0 or host_delta <= 0:
            issues.append("stats clock reset, stalled simulation, or non-positive clock span")
        else:
            rtf, host_rtf = sim_delta / real_delta, sim_delta / host_delta
            for previous, current in zip(stats, stats[1:]):
                sim_step = current["sim_time_s"] - previous["sim_time_s"]
                real_step = current["real_time_s"] - previous["real_time_s"]
                if sim_step > 0 and real_step > 0:
                    window_rtfs.append(sim_step / real_step)
        if any(row["paused"] is True for row in stats):
            issues.append("world paused during measurement window")

    def peak(key: str) -> float | None:
        values = [row[key] for row in resources if row.get(key) is not None]
        return max(values) if values else None

    device_peak = peak("device_memory_used_mib")
    process_peak = peak("container_process_memory_mib")
    load1_samples = [float(row["loadavg"][0]) for row in resources
                     if isinstance(row.get("loadavg"), list) and row["loadavg"]
                     and math.isfinite(float(row["loadavg"][0]))]
    if device_peak is None:
        issues.append("no device memory samples")
    if process_peak is None:
        issues.append("no attributable container GPU process memory samples")
    if stats and resources:
        overlap_start = max(stats[0]["host_monotonic_s"], resources[0]["host_monotonic_s"])
        overlap_end = min(stats[-1]["host_monotonic_s"], resources[-1]["sample_finished_monotonic_s"])
        if overlap_end <= overlap_start:
            issues.append("stats and resource sampling windows do not overlap")
    return {"measurement_complete": not issues, "errors": issues,
            "rtf_delta_sim_over_delta_real": rtf, "rtf_delta_sim_over_host_monotonic": host_rtf,
            "rtf_window_median": window_rtf_median(stats) if rtf is not None else None,
            "rtf_window_sample_count": len(window_rtfs),
            "delta_sim_s": sim_delta, "delta_gazebo_real_s": real_delta,
            "delta_host_monotonic_s": host_delta, "stats_sample_count": len(stats),
            "resource_sample_count": len(resources), "device_memory_peak_mib": device_peak,
            "host_load1_median": median(load1_samples) if load1_samples else None,
            "container_process_memory_peak_mib": process_peak,
            "stats_first": stats[0] if stats else None, "stats_last": stats[-1] if stats else None}


def collect(container: str, gpu: str, warmup_s: float, window_s: float, interval_s: float) -> dict:
    time.sleep(warmup_s)
    started = time.monotonic()
    deadline = started + window_s
    stats: list[dict] = []
    resources: list[dict] = []
    errors: list[str] = []
    # timeout is owned by this probe and bounds its subscriber inside the container.
    command = ["docker", "exec", container, "bash", "-c",
               f"source /opt/ros/*/setup.bash 2>/dev/null; exec timeout -k 2s {window_s + 2:g}s "
               f"gz topic -e --json-output -t {TOPIC}"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def read_stats() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            now = time.monotonic()
            if now > deadline:
                continue
            try:
                stats.append(normalize_stats(json.loads(line), now, time.time()))
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(f"invalid stats message: {exc}")

    reader = threading.Thread(target=read_stats, daemon=True)
    reader.start()
    next_sample = started
    try:
        while time.monotonic() < deadline:
            row = {"host_monotonic_s": time.monotonic(), "host_wall_s": time.time(), **host_sample()}
            try:
                pids = {line.strip() for line in run_read(["docker", "top", container, "-eo", "pid"]).splitlines()
                        if line.strip().isdigit()}
                if not pids:
                    raise ValueError("docker top returned no container PIDs")
                row.update(parse_gpu(run_read(["nvidia-smi", "-i", gpu, "-q", "-x"]), pids))
            except (OSError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError, ValueError) as exc:
                row["error"] = str(exc)
                errors.append(f"resource sampling failed: {exc}")
            row["sample_finished_monotonic_s"] = time.monotonic()
            # Keep only queries completed within the same requested window.
            if row["sample_finished_monotonic_s"] <= deadline:
                resources.append(row)
            next_sample += interval_s
            time.sleep(max(0, min(next_sample, deadline) - time.monotonic()))
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            errors.append("stats subscriber exceeded its bounded lifetime")
            # This is only the docker exec CLI process started above, never Gazebo/PX4.
            process.terminate()
            process.wait(timeout=5)
        reader.join(timeout=2)
        if reader.is_alive():
            errors.append("stats reader failed to finish")
        if process.returncode not in (0, 124):
            errors.append(f"stats subscriber exited {process.returncode}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    summary = summarize(stats, resources, errors)
    observed_span = summary["delta_host_monotonic_s"]
    if observed_span is None or observed_span < window_s * 0.8:
        summary["errors"].append("Gazebo stats cover less than 80% of the requested window")
        summary["measurement_complete"] = False
    if len(resources) < 2:
        summary["errors"].append("fewer than two completed resource samples")
        summary["measurement_complete"] = False
    return {"schema_version": 1, "topic": TOPIC, "container_id": container, "gpu": gpu,
            "warmup_s": warmup_s, "requested_window_s": window_s, "interval_s": interval_s,
            "window_start_monotonic_s": started, "window_end_monotonic_s": deadline,
            "stats_probe_pid": process.pid, **summary, "stats_samples": stats, "resource_samples": resources,
            "stats_window_coverage_fraction": observed_span / window_s if observed_span is not None else None,
            "memory_scope": "device peak includes other jobs; container peak sums graphics/compute processes from docker top",
            "peak_limit": "sampled peaks may miss allocations shorter than the sampling interval"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--container")
    group.add_argument("--container-prefix")
    parser.add_argument("--wait-s", type=float, default=300)
    parser.add_argument("--gpu", choices=("1", "3"), default="1")
    parser.add_argument("--warmup-s", type=float, default=30)
    parser.add_argument("--window-s", type=float, default=120)
    parser.add_argument("--interval-s", type=float, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if any(not math.isfinite(value) for value in (args.wait_s, args.warmup_s, args.window_s, args.interval_s)):
        parser.error("timing values must be finite")
    if min(args.wait_s, args.warmup_s) < 0 or min(args.window_s, args.interval_s) <= 0:
        parser.error("wait/warmup must be non-negative; window/interval must be positive")
    try:
        container, actual_name = find_container(args.container, args.container_prefix, args.wait_s)
        result = collect(container, args.gpu, args.warmup_s, args.window_s, args.interval_s)
        result["container_name"] = actual_name
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        result = {"schema_version": 1, "measurement_complete": False, "errors": [str(exc)]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["measurement_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
