"""Bounded, read-only Gazebo clock capture inside the pinned Harmonic image.

No PX4/host clock conversion is inferred. stdout is JSONL; diagnostics stderr.
The container receiver clock is explicitly a third, unaligned clock domain.
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import sys
import threading
import time


def time_ns(stamp):
    sec, nsec = stamp.sec, stamp.nsec
    if type(sec) is not int or type(nsec) is not int or sec < 0 or not 0 <= nsec < 1_000_000_000:
        raise ValueError("invalid Gazebo time")
    return sec * 1_000_000_000 + nsec


def clock_record(message, *, sequence, received_ns, topic):
    if type(sequence) is not int or sequence < 0 or type(received_ns) is not int or received_ns < 0:
        raise ValueError("invalid receiver clock/sequence")
    for field in ("sim", "real", "system"):
        if not message.HasField(field):
            raise ValueError(f"missing Gazebo clock field: {field}")
    return {
        "schema_version": 1,
        "source": "gz.msgs.Clock",
        "topic": topic,
        "sequence": sequence,
        "container_receive_monotonic_ns": received_ns,
        "sim_ns": time_ns(message.sim),
        "real_ns": time_ns(message.real),
        "system_ns": time_ns(message.system),
        "host_px4_alignment_claimed": False,
    }


def describe_clock_progress(rows):
    if len(rows) < 2:
        raise ValueError("at least two clock samples required")
    keys = ("sim_ns", "real_ns", "system_ns", "container_receive_monotonic_ns")
    for i, row in enumerate(rows):
        if row["sequence"] != i or row["topic"] != rows[0]["topic"]:
            raise ValueError("clock sequence/topic mismatch")
        for key in keys:
            value = row[key]
            if type(value) is not int or value < 0:
                raise ValueError("nonnegative integer clock required")
            # System wall-clock adjustments are recorded but not used as a
            # monotonic source. A reset of sim/real/receiver invalidates a run.
            if i and key != "system_ns" and value < rows[i - 1][key]:
                raise ValueError(f"clock reset: {key}")
    delta = {key: rows[-1][key] - rows[0][key] for key in keys}
    if delta["real_ns"] <= 0 or delta["container_receive_monotonic_ns"] <= 0:
        raise ValueError("clock capture has no elapsed observation interval")
    return {
        "sample_count": len(rows),
        "delta_ns": delta,
        "observed_sim_per_gazebo_real": delta["sim_ns"] / delta["real_ns"],
        "host_px4_alignment_claimed": False,
        "calibration_eligible": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", default="default")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=15)
    parser.add_argument("--min-interval-seconds", type=float, default=0)
    parser.add_argument("--stop-on-stdin", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.world):
        raise ValueError("invalid world name")
    if not 2 <= args.samples <= 10000 or not 0 < args.timeout_seconds <= 3600:
        raise ValueError("bounded samples/timeout required")
    if not 0 <= args.min_interval_seconds <= 1:
        raise ValueError("sample interval must be in [0, 1]")
    from gz.msgs10.clock_pb2 import Clock
    from gz.transport13 import Node

    topic = f"/world/{args.world}/clock"
    rows, errors = [], []
    lock = threading.Lock()
    done = threading.Event()
    stopping = threading.Event()
    stop_requested = threading.Event()

    def read_stop():
        if sys.stdin.readline().strip() == "STOP":
            stop_requested.set()
        else:
            errors.append("clock input closed without STOP")
        done.set()

    if args.stop_on_stdin:
        threading.Thread(target=read_stop, daemon=True).start()

    def callback(message):
        with lock:
            if stopping.is_set() or done.is_set():
                return
            try:
                received_ns = time.monotonic_ns()
                if rows and received_ns - rows[-1]["container_receive_monotonic_ns"] < (
                    args.min_interval_seconds * 1_000_000_000
                ):
                    return
                row = clock_record(
                    message, sequence=len(rows), received_ns=received_ns, topic=topic
                )
                rows.append(row)
                print(json.dumps(row, separators=(",", ":")), flush=True)
                if len(rows) >= args.samples:
                    done.set()
            except Exception as exc:
                errors.append(str(exc))
                done.set()

    node = Node()
    try:
        if not node.subscribe(Clock, topic, callback):
            raise ValueError("Gazebo clock subscription failed")
        if not done.wait(args.timeout_seconds):
            raise ValueError("Gazebo clock capture timed out")
    finally:
        stopping.set()
        with lock:
            pass
        if node.unsubscribe(topic) is False:
            errors.append("Gazebo clock unsubscribe failed")
        time.sleep(0.1)
        del node
        gc.collect()
    if errors:
        raise ValueError("; ".join(errors))
    if args.stop_on_stdin and not stop_requested.is_set():
        raise ValueError("sample limit reached before requested STOP")
    print(json.dumps({"summary": describe_clock_progress(rows)}), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
