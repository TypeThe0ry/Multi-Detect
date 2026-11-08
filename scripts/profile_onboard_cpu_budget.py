#!/usr/bin/env python3
"""Repeatable CPU-only stage microbenchmarks for onboard workload budgeting."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_loop import _iou  # noqa: E402
from multidetect.sim_teacher.primitives import ActionPrimitive, primitive_to_ned  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction + 0.5)))]


def input_description() -> dict[str, Any]:
    replay = ROOT / "tests/fixtures/onboard_replay"
    files = [replay / "episode.json", replay / "steps.jsonl"]
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    lines = [line for line in (replay / "steps.jsonl").read_text().splitlines() if line.strip()]
    raw = b"".join(
        bytes(((x * 17 + y * 31 + c * 53) % 256) for c in range(3))
        for y in range(72) for x in range(96)
    )
    record = {"step": 17, "pose": [1.2, -0.4, -8.0],
              "detections": [
                  [i * 8.0, i * 6.0, i * 8.0 + 20.0, i * 6.0 + 16.0]
                  for i in range(8)
              ],
              "decision": "approach_track", "timing_ms": {"policy": 1.2}}
    return {
        "replay": "tests/fixtures/onboard_replay/episode.json + steps.jsonl",
        "replay_sha256_combined": digest.hexdigest(),
        "replay_step_rows": len(lines),
        "synthetic_frame": {"width": 96, "height": 72, "channels": 3,
                            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
        "synthetic_candidates": 64,
        "synthetic_tracks": 64,
        "synthetic_log_record_bytes": len(json.dumps(record, sort_keys=True).encode()),
    }


def make_workloads() -> dict[str, Any]:
    replay = ROOT / "tests/fixtures/onboard_replay"
    raw = bytes(
        ((x * 17 + y * 31 + c * 53) % 256)
        for y in range(72) for x in range(96) for c in range(3)
    )

    def replay_decode() -> tuple[int, int]:
        json.loads((replay / "episode.json").read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in (replay / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        task_text = rows[0].get("task_text", "") if rows else ""
        return len(rows), len(task_text)

    # A compact deterministic image preprocessing and proposal workload; no model is loaded.
    def preprocess_detector() -> tuple[int, int]:
        gray = bytearray(96 * 72)
        for pixel in range(96 * 72):
            offset = pixel * 3
            gray[pixel] = (77 * raw[offset] + 150 * raw[offset + 1] + 29 * raw[offset + 2]) >> 8
        proposals = sum(1 for value in gray if value >= 96)
        return proposals, sum(gray)

    boxes = [(i * 8.0, i * 6.0, i * 8.0 + 20.0, i * 6.0 + 16.0) for i in range(64)]

    def planner_policy() -> tuple[float, ...]:
        index = sum(raw[::97]) % len(boxes)
        action = ActionPrimitive("translate", forward_m=2.0 + index / 64,
                                 right_m=(index % 9 - 4) * 0.1, up_m=0.0,
                                 yaw_delta_deg=(index % 17) - 8)
        return primitive_to_ned(action, yaw_deg=23.0)

    def geometry_association() -> int:
        matches = 0
        for i, candidate in enumerate(boxes):
            for j, track in enumerate(boxes):
                if _iou(candidate, track) >= 0.3 and (i == j or abs(i - j) == 1):
                    matches += 1
        return matches

    record = {"step": 17, "pose": [1.2, -0.4, -8.0], "detections": boxes[:8],
              "decision": "approach_track", "timing_ms": {"policy": 1.2}}

    def serialization_logging() -> int:
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return len(encoded) + hashlib.sha256(encoded).digest()[0]

    funcs = {"replay_decode_fixed_episode": replay_decode,
             "detector_preprocess_synthetic": preprocess_detector,
             "planner_policy_primitive": planner_policy,
             "geometry_association_iou": geometry_association,
             "serialization_logging_json": serialization_logging}
    # Ensure deterministic inputs are consumed and avoid dead-code-like constant results.
    return funcs


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def run(repeats: int, iterations: int, warmup: int) -> dict[str, Any]:
    funcs = make_workloads()
    timings: dict[str, list[float]] = {name: [] for name in funcs}
    results: dict[str, Any] = {}
    for name, fn in funcs.items():
        for _ in range(warmup):
            fn()
        for _ in range(repeats):
            for _ in range(iterations):
                start = time.perf_counter_ns()
                result = fn()
                timings[name].append((time.perf_counter_ns() - start) / 1e6)
            results[name] = result
    rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
    return {
        "schema_version": 1,
        "scope": (
            "CPU-only deterministic component microbenchmarks; synthetic detector, "
            "not model inference"
        ),
        "device": {"cpu": cpu_model(), "architecture": platform.machine(),
                   "platform": platform.platform(), "logical_cpus": os.cpu_count(),
                   "process_cpu_affinity": affinity,
                   "active_python_threads": threading.active_count(),
                   "threading_env": {
                       key: os.environ.get(key)
                       for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
                   }},
        "python": sys.version,
        "git_head": revision,
        "input": input_description(),
        "measurement": {"clock": "time.perf_counter_ns", "unit": "ms per invocation",
                        "warmup_invocations_per_stage": warmup, "repeats": repeats,
                        "iterations_per_repeat": iterations,
                        "total_samples_per_stage": repeats * iterations,
                        "cpu_threads_configured": 1, "rss_peak_kib_process_high_water": rss_kib},
        "stages": {name: {"p50_ms": round(statistics.median(values), 6),
                          "p95_ms": round(percentile(values, 0.95), 6),
                          "mean_ms": round(statistics.mean(values), 6),
                          "samples": len(values), "result_signature": str(results[name])[:160]}
                   for name, values in timings.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.repeats, args.iterations) < 3 or args.warmup < 1:
        parser.error("repeats and iterations must be >= 3; warmup must be >= 1")
    report = run(args.repeats, args.iterations, args.warmup)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
