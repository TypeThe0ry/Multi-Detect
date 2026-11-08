#!/usr/bin/env python3
"""CPU-only scaling profile for the production onboard association entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_loop import (  # noqa: E402
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
    Track,
)

SIZES = (16, 32, 64, 128)


def _fixture_digest() -> str:
    base = ROOT / "tests/fixtures/onboard_replay"
    digest = hashlib.sha256()
    for name in ("episode.json", "steps.jsonl"):
        digest.update(name.encode("utf-8"))
        digest.update((base / name).read_bytes())
    return digest.hexdigest()


def _boxes(count: int) -> list[tuple[float, float, float, float]]:
    # Stable, separated small boxes in the recorded fixture's 640x480 geometry.
    return [
        (8.0 + (index % 20) * 30.0, 8.0 + (index // 20) * 30.0,
         24.0 + (index % 20) * 30.0, 24.0 + (index // 20) * 30.0)
        for index in range(count)
    ]


def _make_loop(count: int) -> tuple[OnboardLoop, list[Detection]]:
    boxes = _boxes(count)
    loop = OnboardLoop(
        camera=CameraModel(500, 500, 320, 240, 640, 480),
        search_action=lambda _pose, _yaw: None,  # type: ignore[arg-type]
        config=LoopConfig(max_detections=count),
    )
    loop._tracks = {
        f"t{index + 1}": Track(f"t{index + 1}", box, 0, 0)
        for index, box in enumerate(boxes)
    }
    loop._next_id = count + 1
    loop.step_index = 1
    return loop, [Detection(box, score=1.0, label="candidate") for box in boxes]


def _snapshot(tracks: list[Track]) -> list[dict[str, Any]]:
    return [
        {"track_id": track.track_id, "bbox": list(track.bbox), "first_step": track.first_step,
         "last_step": track.last_step, "hits": track.hits, "ground": track.ground,
         "appearance": None if track.appearance is None else {
             "size_range": track.appearance.size_range,
             "aspect_ratio": track.appearance.aspect_ratio,
             "footprint_m": track.appearance.footprint_m,
             "color_histogram": track.appearance.color_histogram,
             "short_side_px": track.appearance.short_side_px,
         }}
        for track in tracks
    ]


def _rss_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def run(*, repeats: int = 30, warmup: int = 5) -> dict[str, Any]:
    rows = []
    for count in SIZES:
        loop, detections = _make_loop(count)
        expected = _snapshot(loop.update_tracks(detections))
        samples = []
        for _ in range(warmup):
            current, candidates = _make_loop(count)
            if _snapshot(current.update_tracks(candidates)) != expected:
                raise AssertionError(f"non-deterministic association output at N={count}")
        for _ in range(repeats):
            current, candidates = _make_loop(count)
            started = time.perf_counter_ns()
            result = current.update_tracks(candidates)
            samples.append((time.perf_counter_ns() - started) / 1e6)
            if _snapshot(result) != expected:
                raise AssertionError(f"association output mismatch at N={count}")
        ordered = sorted(samples)
        rows.append({
            "candidates": count,
            "tracks": count,
            "p50_ms": round(statistics.median(samples), 6),
            "p95_ms_nearest_rank": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 6),
            "samples": len(samples),
            "output_items": len(expected),
            "output_sha256": hashlib.sha256(
                json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "process_rss_kib_after_scale": _rss_kib(),
        })
    return {
        "schema_version": 1,
        "scope": "Production OnboardLoop.update_tracks CPU-only benchmark; no model or runtime changes",
        "input": {
            "replay_metadata": "tests/fixtures/onboard_replay/episode.json + steps.jsonl",
            "replay_metadata_sha256_combined": _fixture_digest(),
            "replay_metadata_rows": 1,
            "original_rgb_frames": 0,
            "rgb_preprocessing": "not measured: fixture contains no RGB frame or rgb_path",
            "association_geometry": "deterministic candidate boxes; not detector output from replay RGB",
            "box_dimensions": [16, 16],
            "frame_dimensions": [640, 480],
        },
        "measurement": {
            "clock": "time.perf_counter_ns",
            "unit": "ms per update_tracks call",
            "warmup_per_scale": warmup,
            "repeats_per_scale": repeats,
            "cpu_only": True,
            "rss_note": "absolute process VmRSS after each scale, not per-scale allocation delta",
            "rss_peak_kib_process_high_water": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        },
        "device": {"cpu": platform.processor() or "unknown", "architecture": platform.machine(),
                   "logical_cpus": os.cpu_count()},
        "scales": rows,
        "equivalence": "each measured output item compared field-by-field to deterministic baseline snapshot",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 3 or args.warmup < 1:
        parser.error("repeats must be >= 3 and warmup must be >= 1")
    report = run(repeats=args.repeats, warmup=args.warmup)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
