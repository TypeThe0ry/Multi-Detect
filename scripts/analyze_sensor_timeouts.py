#!/usr/bin/env python3
"""Report PX4 sensor timeout counts, episode RTF, and recorded host pressure."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _sim_seconds(row: dict[str, Any], key: str) -> float | None:
    observation = row.get(key) or {}
    value = _finite(observation.get("pose_time_boot_ms"))
    return value / 1000.0 if value is not None else None


def _wall_duration(meta: dict[str, Any]) -> float | None:
    start = _finite(meta.get("created_monotonic_ns"))
    finish = _finite(meta.get("finished_monotonic_ns"))
    if start is not None and finish is not None and finish >= start:
        return (finish - start) / 1e9
    start = _finite(meta.get("created_wall_time"))
    finish = _finite(meta.get("finished_wall_time"))
    if start is not None and finish is not None and finish >= start:
        return finish - start
    return None


def _pressure(rows: list[dict[str, Any]]) -> dict[str, float] | None:
    fields = ("core_busy_percent_mean", "core_busy_percent_max")
    values = {field: [_finite(row.get(field)) for row in rows] for field in fields}
    result = {}
    for field, items in values.items():
        numeric = [item for item in items if item is not None]
        if numeric:
            result[f"{field}_mean"] = statistics.fmean(numeric)
            result[f"{field}_peak"] = max(numeric)
    loads = []
    for row in rows:
        raw = row.get("loadavg")
        if isinstance(raw, str):
            try:
                loads.append(float(raw.split()[0]))
            except (ValueError, IndexError):
                continue
    if loads:
        result["load1_mean"] = statistics.fmean(loads)
        result["load1_peak"] = max(loads)
    return result or None


def analyze_episode(episode_dir: Path) -> dict[str, Any]:
    episode_dir = episode_dir.resolve()
    try:
        meta = json.loads((episode_dir / "episode.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    counts = {"accel": 0, "gyro": 0, "mag": 0}
    for row in _jsonl(episode_dir / "mavlink.jsonl"):
        kind = str(row.get("message_type", row.get("mavpackettype", ""))).upper()
        if kind != "STATUSTEXT":
            continue
        text = str(row.get("text", "")).lower()
        if "timeout" not in text:
            continue
        for sensor in counts:
            if sensor in text:
                counts[sensor] += 1
                break
    steps = _jsonl(episode_dir / "steps.jsonl")
    stamps = [
        value
        for row in steps
        for value in (
            _sim_seconds(row, "obs_before"),
            _sim_seconds(row, "obs_after"),
        )
        if value is not None
    ]
    sim_delta = max(stamps) - min(stamps) if len(stamps) >= 2 else None
    wall_s = _wall_duration(meta)
    return {
        "episode": episode_dir.name,
        "status": meta.get("status", "unknown"),
        "timeouts": counts,
        "rtf": sim_delta / wall_s if sim_delta is not None and wall_s and wall_s > 0 else None,
        "wall_s": wall_s,
        "host_pressure": _pressure(_jsonl(episode_dir / "host_pressure.jsonl")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path, help="episode directories")
    args = parser.parse_args(argv)
    reports = [analyze_episode(path) for path in args.episodes]
    print("episode\tstatus\taccel_timeout\tgyro_timeout\tmag_timeout\trtf\tpressure")
    for report in reports:
        pressure = report["host_pressure"]
        print(
            f"{report['episode']}\t{report['status']}\t"
            f"{report['timeouts']['accel']}\t{report['timeouts']['gyro']}\t{report['timeouts']['mag']}\t"
            f"{report['rtf'] if report['rtf'] is not None else '-'}\t"
            f"{json.dumps(pressure, separators=(',', ':')) if pressure else '-'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
