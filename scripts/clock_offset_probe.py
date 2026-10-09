"""Causal host/container monotonic offset intervals, conditional on equal rates."""

from __future__ import annotations

import math
import queue
import re
import subprocess
import threading
import time

from multidetect.clock import HighResolutionSystemClock, validate_host_identity


def host_clock_binding(clock):
    if clock is None:
        return time.monotonic_ns, None
    if not isinstance(clock, HighResolutionSystemClock):
        raise TypeError("explicit HighResolutionSystemClock required")
    return clock.monotonic_ns, validate_host_identity(clock.identity())


def _validate_target(container_id, count):
    """Caller owns lifecycle; execute only a fixed read-only clock expression."""
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("exact container ID required")
    if type(count) is not int or not 2 <= count <= 32:
        raise ValueError("2..32 probes required")


def _resolution():
    return math.ceil(time.get_clock_info("monotonic").resolution * 1e9)


def sample_exec_probes(docker, container_id, count=3):
    """Baseline with one docker exec per observation, retained for comparison."""
    _validate_target(container_id, count)
    rows = []
    for _ in range(count):
        before = time.monotonic_ns()
        result = subprocess.run(
            [
                docker,
                "exec",
                container_id,
                "python3",
                "-c",
                "import time; print(time.monotonic_ns(), flush=True)",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        after = time.monotonic_ns()
        if result.returncode != 0 or not re.fullmatch(r"[0-9]{1,30}\s*", result.stdout):
            raise ValueError("clock probe failed or returned malformed output")
        rows.append(
            {
                "host_before_ns": before,
                "container_ns": int(result.stdout),
                "host_after_ns": after,
                "container_id": container_id,
                "host_resolution_ns": _resolution(),
            }
        )
    describe_probes(rows)
    return rows


SERVER = """import sys,time
count=int(sys.argv[1])
if not 2 <= count <= 32: raise ValueError("invalid count")
print("READY", flush=True)
for i in range(count):
    if sys.stdin.readline() not in (str(i)+"\\n", str(i)+"\\r\\n"):
        raise ValueError("invalid sequence")
    stamp=time.monotonic_ns()
    print(str(i)+" "+str(stamp), flush=True)
"""


def sample_probes(docker, container_id, count=3, *, clock=None):
    """One fixed bounded server, startup excluded, sequential request IDs."""
    _validate_target(container_id, count)
    read_clock, identity = host_clock_binding(clock)
    resolution = identity["resolution_ns"] if identity else _resolution()
    process = subprocess.Popen(
        [docker, "exec", "-i", container_id, "python3", "-u", "-c", SERVER, str(count)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    replies = queue.Queue(maxsize=34)

    def read():
        for _ in range(count + 2):
            line = process.stdout.readline(128)
            replies.put_nowait(line)
            if not line:
                return

    reader = threading.Thread(target=read, daemon=True)
    reader.start()

    def receive():
        try:
            return replies.get(timeout=5)
        except queue.Empty as exc:
            raise ValueError("clock probe response timeout") from exc

    try:
        if receive() != "READY\n":
            raise ValueError("clock probe not ready")
        rows = []
        for i in range(count):
            before = read_clock()
            process.stdin.write(f"{i}\n")
            process.stdin.flush()
            line = receive()
            after = read_clock()
            match = re.fullmatch(r"([0-9]{1,2}) ([0-9]{1,30})\n", line)
            if not match or int(match[1]) != i:
                raise ValueError("clock probe response sequence/format mismatch")
            rows.append(
                {
                    "host_before_ns": before,
                    "host_after_ns": after,
                    "container_ns": int(match[2]),
                    "container_id": container_id,
                    "host_resolution_ns": resolution,
                    **({"host_clock_identity": identity} if identity else {}),
                }
            )
        process.stdin.close()
        if process.wait(timeout=5) != 0 or receive() != "":
            raise ValueError("clock probe did not terminate cleanly")
        describe_probes(rows)
        return rows
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdin.close()
        process.stdout.close()


def describe_probes(rows):
    if not 2 <= len(rows) <= 64:
        raise ValueError("2..64 observations required")
    intervals, rtts = [], []
    previous = None
    identity = rows[0].get("host_clock_identity") if isinstance(rows[0], dict) else None
    if identity is not None:
        validate_host_identity(identity)
    for row in rows:
        required = {
            "host_before_ns",
            "container_ns",
            "host_after_ns",
            "container_id",
        }
        if (
            not isinstance(row, dict)
            or not required <= set(row)
            or set(row) - required - {"host_resolution_ns", "host_clock_identity"}
        ):
            raise ValueError("invalid probe fields")
        for key in ("host_before_ns", "container_ns", "host_after_ns"):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError("nonnegative integer probe time required")
        container_id = row["container_id"]
        if (
            not isinstance(container_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", container_id)
            or container_id != rows[0]["container_id"]
        ):
            raise ValueError("probe container mismatch")
        before, sample, after = row["host_before_ns"], row["container_ns"], row["host_after_ns"]
        resolution = row.get("host_resolution_ns", 0)
        if "host_clock_identity" in row and identity is None:
            raise ValueError("explicit host clock identity cannot be null")
        if row.get("host_clock_identity") != identity:
            raise ValueError("mixed host clock identity")
        if identity is not None and resolution != identity["resolution_ns"]:
            raise ValueError("probe resolution differs from clock identity")
        if type(resolution) is not int or not 0 <= resolution <= 1_000_000_000:
            raise ValueError("invalid host resolution")
        if before > after or (before == after and resolution == 0):
            raise ValueError("positive host round trip required")
        if previous and (before < previous["host_after_ns"] or sample <= previous["container_ns"]):
            raise ValueError("overlapping or reset probe clocks")
        intervals.append([before - resolution - sample, after + resolution - sample])
        rtts.append(after - before)
        previous = row
    lower, upper = max(i[0] for i in intervals), min(i[1] for i in intervals)
    compatible = lower <= upper
    return {
        "schema_version": 1,
        **({"host_clock_identity": identity} if identity else {}),
        "container_id": rows[0]["container_id"],
        "sample_count": len(rows),
        "assumption": (
            "host_ns = container_ns + constant_offset; equal rates, no resets; "
            "host read error <= declared resolution"
        ),
        "declared_host_resolution_ns": max(row.get("host_resolution_ns", 0) for row in rows),
        "timestamp_accuracy_verified": False,
        "offset_direction": "host_minus_container_ns",
        "per_probe_offset_intervals_ns": intervals,
        "constant_offset_compatible": compatible,
        "conditional_offset_interval_ns": [lower, upper] if compatible else None,
        "conditional_interval_width_ns": upper - lower if compatible else None,
        "minimum_round_trip_ns": min(rtts),
        "maximum_round_trip_ns": max(rtts),
        "host_quantization_limited": any(value == 0 for value in rtts),
        "observed_host_span_ns": rows[-1]["host_after_ns"] - rows[0]["host_before_ns"],
        "equal_clock_rates_verified": False,
        "gazebo_source_clock_aligned": False,
        "px4_clock_aligned": False,
        "training_label_eligible": False,
    }
