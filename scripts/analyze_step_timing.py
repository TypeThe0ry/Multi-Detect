#!/usr/bin/env python3
"""Summarise fine-grained SITL step timings.

The collector stores phase durations in ``timing`` (milliseconds).  This
utility also understands the historical steps.jsonl format and emits the old
coarse decomposition (wall, primitive, detector, slow_model, other).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _duration_ms(value: Any) -> float | None:
    """Extract a duration in milliseconds from a scalar or duration object."""
    n = _number(value)
    if n is not None:
        return n
    if not isinstance(value, dict):
        return None
    for key in ("elapsed_ms", "duration_ms", "ms", "wall_ms"):
        n = _number(value.get(key))
        if n is not None:
            return n
    for key in ("elapsed_s", "duration_s", "seconds", "s"):
        n = _number(value.get(key))
        if n is not None:
            return n * 1000.0
    return None


def _flatten_timing(value: Any, prefix: str = "") -> dict[str, float]:
    """Flatten timing phase values while ignoring aggregate/count metadata."""
    direct = _duration_ms(value)
    if direct is not None:
        return {prefix.rstrip("."): direct} if prefix else {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for key, child in value.items():
        if key in {"total_ms", "total_s", "count", "started_ns", "finished_ns", "start_ns", "end_ns"}:
            continue
        # ``timing.phases`` is a container, not a phase of its own.
        parent = "" if prefix == "phases" else prefix
        name = f"{parent}.{key}" if parent else str(key)
        result.update(_flatten_timing(child, name))
    return result


def _slow_model_ms(row: dict[str, Any]) -> float:
    calls = ((row.get("labels") or {}).get("onboard") or {}).get("slow_calls") or []
    total = 0.0
    for call in calls:
        if not isinstance(call, dict):
            continue
        value = _duration_ms(call)
        if value is None:
            value = _number(call.get("latency_s"))
            value = value * 1000.0 if value is not None else 0.0
        total += value
    return total


def _old_row(row: dict[str, Any], previous_receive_ns: int | None) -> tuple[dict[str, float], int | None]:
    receive = row.get("receive_monotonic_ns")
    receive_ns = int(receive) if _number(receive) is not None else None
    wall_ms = 0.0
    if previous_receive_ns is not None and receive_ns is not None and receive_ns >= previous_receive_ns:
        wall_ms = (receive_ns - previous_receive_ns) / 1e6
    outcome = row.get("outcome") or {}
    primitive = _number(outcome.get("elapsed_s")) or 0.0
    detector = _number((((row.get("labels") or {}).get("onboard") or {}).get("fast_latency_s"))) or 0.0
    detector *= 1000.0
    slow = _slow_model_ms(row)
    other = max(0.0, wall_ms - primitive * 1000.0 - detector - slow)
    return {
        "wall": wall_ms,
        "primitive": primitive * 1000.0,
        "detector": detector,
        "slow_model": slow,
        "other": other,
    }, receive_ns


def read_steps(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if isinstance(item, dict):
                rows.append(item)
    return rows


def analyze_steps(path: Path) -> dict[str, Any]:
    rows = read_steps(path)
    phases: dict[str, list[float]] = {}
    task = "unknown"
    previous_receive_ns: int | None = None
    fine = any(isinstance(row.get("timing"), dict) for row in rows)
    for row in rows:
        task_value = row.get("task_text") or row.get("task")
        if task_value:
            task = str(task_value)
        if isinstance(row.get("timing"), dict):
            values = _flatten_timing(row["timing"])
            # A total is useful as a wall-clock phase even if no explicit wall key exists.
            if "total_ms" in row["timing"] and "wall" not in values:
                total = _duration_ms(row["timing"].get("total_ms"))
                if total is not None:
                    values["wall"] = total
        else:
            values, previous_receive_ns = _old_row(row, previous_receive_ns)
        for name, value in values.items():
            phases.setdefault(name, []).append(float(value))
    return {"path": str(path), "task": task, "steps": len(rows), "source": "fine" if fine else "legacy", "phases": summarize(phases)}


def summarize(phases: dict[str, list[float]]) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for name, values in sorted(phases.items()):
        if not values:
            continue
        result[name] = {
            "count": len(values),
            "mean_ms": statistics.fmean(values),
            "median_ms": statistics.median(values),
            "p95_ms": _percentile(values, 0.95),
        }
    return result


def _percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = fraction * (len(ordered) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _find_steps(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("steps.jsonl"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="steps.jsonl, episode directory, or campaign root")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    paths = _find_steps(args.root)
    if not paths:
        parser.error(f"no steps.jsonl found below {args.root}")
    reports = [analyze_steps(path) for path in paths]
    if args.as_json:
        print(json.dumps({"episodes": reports}, ensure_ascii=False, indent=2))
        return 0
    print("task\tphase\tsteps\tmean_ms\tmedian_ms\tp95_ms\tsource")
    for report in reports:
        for phase, stats in report["phases"].items():
            print(f"{report['task']}\t{phase}\t{stats['count']}\t{stats['mean_ms']:.3f}\t{stats['median_ms']:.3f}\t{stats['p95_ms']:.3f}\t{report['source']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
