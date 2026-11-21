#!/usr/bin/env python3
"""Load admission and an attempt-scoped watchdog for wide collection.

No work is started at import time. Only the ``run`` CLI supervises a flight;
the batch scheduler and standalone matrix share the same admission policy.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET

from scripts.realworld import sample_stats


DEFAULTS = dict(start_load_max=60, start_wait_s=3600, poll_interval_s=2,
                abort_rtf=0.8, abort_load_max=120, abort_duration_s=90,
                rtf_window_s=30, max_retries=3, retry_backoff_s=30,
                retry_backoff_max_s=600)
HOST_DISK_REASONS = {"host_disk_below_floor", "host_disk_probe_failed"}
DEFER_REASONS = HOST_DISK_REASONS | {"x1_gpu_unavailable", "x1_start_load_exceeded"}


def settings(overrides=None):
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or overrides.keys() - DEFAULTS.keys():
        raise ValueError("invalid collection guard settings")
    config = {**DEFAULTS, **overrides}
    for key, value in config.items():
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if value < 0 or (key in ("poll_interval_s", "abort_duration_s", "rtf_window_s") and value == 0):
            raise ValueError(f"{key} is outside its allowed range")
    if type(config["max_retries"]) is not int:
        raise ValueError("max_retries must be a nonnegative integer")
    return config


def environment_settings():
    overrides = {}
    for key in DEFAULTS:
        raw = os.environ.get(f"RTF_MATRIX_{key.upper()}")
        if raw is not None:
            overrides[key] = int(raw) if key == "max_retries" else float(raw)
    if "start_wait_s" not in overrides and "RTF_MATRIX_WAIT_S" in os.environ:
        overrides["start_wait_s"] = float(os.environ["RTF_MATRIX_WAIT_S"])
    return settings(overrides)


def write_status(path, data):
    # Each directory has a single guard writer; rename keeps readers off partial JSON.
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def gpu_blockers(gpu):
    """Fail closed on unknown occupancy; inspect graphics as well as compute PIDs."""
    try:
        xml = sample_stats.run_read(["nvidia-smi", "-i", str(gpu), "-q", "-x"], timeout=5)
        device = ET.fromstring(xml).find("gpu")
        if device is None:
            raise ValueError("no GPU in nvidia-smi response")
        blockers = [f"gpu_process:{item.findtext('pid', 'unknown')}:{item.findtext('type', 'unknown')}"
                    for item in device.findall("./processes/process_info")]
        memory = device.findtext("./fb_memory_usage/used", "")
        if not memory.endswith(" MiB") or not math.isfinite(float(memory.split()[0])):
            raise ValueError("unknown GPU memory use")
        if float(memory.split()[0]) > 2000:
            blockers.append(f"gpu_memory_used:{memory}")
        return blockers
    except (OSError, RuntimeError, ValueError, ET.ParseError, subprocess.SubprocessError) as exc:
        return [f"gpu_query_failed:{exc}"]


class GateTimeout(RuntimeError):
    pass


def host_disk_reason():
    """Optional /data free-space floor, in exact GiB, for NAS-backed jobs."""
    floor = int(os.environ.get("RTF_MATRIX_HOST_DISK_MIN_GIB", "0"))
    if not floor:
        return None
    try:
        if shutil.disk_usage("/data").free < floor * 1024**3:
            return "host_disk_below_floor"
    except OSError:
        return "host_disk_probe_failed"
    return None


def wait_for_start(status_path, gpu, config):
    started = time.monotonic()
    while True:
        reason = host_disk_reason()
        if reason:
            raise GateTimeout(reason)
        load = os.getloadavg()[0]
        reasons = gpu_blockers(gpu)
        if not math.isfinite(load) or load > config["start_load_max"]:
            reasons.append("start_load_exceeded")
        elapsed = time.monotonic() - started
        if not reasons:
            status_path.unlink(missing_ok=True)
            return {"start_load": load, "waited_s": elapsed}
        timed_out = elapsed >= config["start_wait_s"]
        write_status(status_path, {"state": "WAITING", "reason": reasons[0], "reasons": reasons,
                                   "load": load if math.isfinite(load) else None,
                                   "gpu": str(gpu), "waited_s": elapsed, "timed_out": timed_out})
        if timed_out:
            raise GateTimeout("start gate timed out: " + ", ".join(reasons))
        time.sleep(min(config["poll_interval_s"], config["start_wait_s"] - elapsed))


def x1_start(out, gpu, config, uuid):
    from scripts.realworld import x1_gpu

    reason = host_disk_reason()
    if reason:
        raise GateTimeout(reason)
    load = os.getloadavg()[0]
    if not math.isfinite(load) or load > config["start_load_max"]:
        raise GateTimeout("x1_start_load_exceeded")
    try:
        selection = x1_gpu.select(out / "GPU-STATUS.json", indices=(int(gpu),), expected_uuid=uuid)
    except x1_gpu.GPUUnavailable as exc:
        print(exc, file=__import__("sys").stderr)
        raise GateTimeout(x1_gpu.UNAVAILABLE) from exc
    return {"start_load": load, "waited_s": 0, **selection}


class MonitorState:
    def __init__(self, config, start_load):
        self.config = config
        self.since = {"load_spike": None, "rtf_drop": None}
        self.summary = dict(start_load=start_load, max_load=start_load,
                            voided_reason=None, rtf_window_median=None)

    def observe(self, now_s, load, stats):
        self.summary["max_load"] = max(self.summary["max_load"], load)
        window = [row for row in stats if now_s - self.config["rtf_window_s"] <= row["host_monotonic_s"] <= now_s]
        fresh = window and now_s - window[-1]["host_monotonic_s"] <= max(2, 2 * self.config["poll_interval_s"])
        rtf = sample_stats.window_rtf_median(window) if fresh else None
        self.summary["rtf_window_median"] = rtf
        conditions = {"load_spike": load > self.config["abort_load_max"],
                      "rtf_drop": rtf is not None and rtf < self.config["abort_rtf"]}
        for reason, bad in conditions.items():
            if not bad:
                self.since[reason] = None
            elif self.since[reason] is None:
                self.since[reason] = now_s
            elif now_s - self.since[reason] >= self.config["abort_duration_s"]:
                self.summary["voided_reason"] = reason
                return reason
        return None


def stop_owned(process):
    """Only call for Popen children created here with start_new_session=True."""
    if process is None or process.poll() is not None:
        return
    # The group may still contain children after the session leader exits.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=5)


def matching_container(prefix):
    rows = sample_stats.run_read(["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"], timeout=2)
    matches = [line.split("\t", 1) for line in rows.splitlines()
               if "\t" in line and line.split("\t", 1)[1].startswith(prefix)]
    if len(matches) > 1:
        raise ValueError("ambiguous attempt container prefix")
    return tuple(matches[0]) if matches else None


def supervise(out, prefix, gpu, config, flight_command, stats_command):
    """Own the flight, probes and immutable container identity for this attempt."""
    gate = json.loads((out / "gate.json").read_text())
    monitor = MonitorState(config, gate["start_load"])
    summary = monitor.summary
    summary.update(errors=[], container_id=None, container_name=None,
                   flight_returncode=None, stats_returncode=None)
    flight = measurement = subscriber = None
    samples = deque()
    sample_lock = threading.Lock()
    reader = None

    def read_stats():
        for line in subscriber.stdout:
            try:
                row = sample_stats.normalize_stats(json.loads(line), time.monotonic(), time.time())
            except (ValueError, TypeError, KeyError):
                continue
            with sample_lock:
                samples.append(row)
                cutoff = row["host_monotonic_s"] - config["rtf_window_s"]
                while samples and samples[0]["host_monotonic_s"] < cutoff:
                    samples.popleft()

    try:
        with (out / "flight.stdout.log").open("wb") as flight_log, (out / "stats.stdout.log").open("wb") as stats_log:
            summary["voided_reason"] = host_disk_reason()
            if summary["voided_reason"]:
                return 76
            flight = subprocess.Popen(flight_command, stdout=flight_log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
            measurement = subprocess.Popen(stats_command, stdout=stats_log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
            while True:
                # Load is monitored even during startup and after the fixed stats window.
                observed_at = time.monotonic()
                with sample_lock:
                    snapshot = list(samples)
                reason = host_disk_reason() or monitor.observe(observed_at, os.getloadavg()[0], snapshot)
                if reason:
                    summary["voided_reason"] = reason
                    write_status(out / "guard.json", summary)
                    break
                if flight.poll() is not None:
                    summary["flight_returncode"] = flight.returncode
                    if measurement.poll() is not None:
                        break
                if summary["container_id"] is None and flight.poll() is None:
                    found = matching_container(prefix)
                    if found:
                        summary["container_id"], summary["container_name"] = found
                        # The subscriber lives only inside this attempt's container.
                        subscriber = subprocess.Popen(
                            ["docker", "exec", summary["container_id"], "bash", "-c",
                             "source /opt/ros/*/setup.bash 2>/dev/null; exec gz topic -e --json-output -t " + sample_stats.TOPIC],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, start_new_session=True)
                        reader = threading.Thread(target=read_stats, daemon=True)
                        reader.start()
                write_status(out / "guard.json", summary)
                time.sleep(config["poll_interval_s"])
            summary["stats_returncode"] = measurement.poll()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        summary["errors"].append(f"watchdog_error: {exc}")
    finally:
        # Stop the owned launch group before resolving a container that appeared
        # during early abort. Never select by slot alone or a non-unique prefix.
        stop_owned(flight)
        stop_owned(measurement)
        stop_owned(subscriber)
        if flight is not None and (summary["voided_reason"] or summary["errors"]):
            try:
                if summary["container_id"] is None:
                    deadline = time.monotonic() + 5
                    while summary["container_id"] is None:
                        found = matching_container(prefix)
                        if found:
                            summary["container_id"], summary["container_name"] = found
                            break
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.25)
                if summary["container_id"]:
                    sample_stats.run_read(["docker", "stop", "--time", "2", summary["container_id"]], timeout=10)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                summary["errors"].append(f"container_cleanup_failed: {exc}")
        if reader is not None:
            reader.join(timeout=2)
        if flight is not None:
            summary["flight_returncode"] = flight.returncode
        if measurement is not None:
            summary["stats_returncode"] = measurement.returncode
        if summary["voided_reason"]:
            from scripts.realworld.wide_collect_batch import remove_episodes
            remove_episodes(out)
        write_status(out / "guard.json", summary)
    return 76 if summary["voided_reason"] else int(bool(summary["errors"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("gate", "run"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--gpu", required=True)
    parser.add_argument("--prefix")
    parser.add_argument("--flight-command", help="JSON argv")
    parser.add_argument("--stats-command", help="JSON argv")
    args = parser.parse_args(argv)
    try:
        config = environment_settings()
        if args.action == "gate":
            uuid = os.environ.get("RTF_MATRIX_X1_GPU_UUID")
            gate = (x1_start(args.out, args.gpu, config, uuid) if uuid else
                    wait_for_start(args.out / "WAITING", args.gpu, config))
            write_status(args.out / "gate.json", gate)
            return 0
        return supervise(args.out, args.prefix, args.gpu, config,
                         json.loads(args.flight_command), json.loads(args.stats_command))
    except GateTimeout as exc:
        if args.action == "gate" and str(exc) in DEFER_REASONS:
            write_status(args.out / "guard.json", {"voided_reason": str(exc)})
        print(str(exc), file=__import__("sys").stderr)
        return 75
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
