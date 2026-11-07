#!/usr/bin/env python3
"""Report PX4 sensor timeout counts, episode RTF, and recorded host pressure."""

from __future__ import annotations

import argparse
import datetime
import gzip
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


def _jsonl_gz(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.is_file():
        return rows
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            try:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
            except (EOFError, OSError):
                pass
    except OSError:
        return rows
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
    mavlink = _jsonl(episode_dir / "mavlink.jsonl")
    if not mavlink:
        mavlink = _jsonl_gz(episode_dir / "mavlink.jsonl.gz")
    timeout_events = []
    for row in mavlink:
        kind = str(row.get("message_type", row.get("mavpackettype", ""))).upper()
        if kind != "STATUSTEXT":
            continue
        text = str(row.get("text", "")).lower()
        if "timeout" not in text:
            continue
        for sensor in counts:
            if sensor in text:
                counts[sensor] += 1
                timeout_events.append(
                    {
                        "sensor": sensor,
                        "text": str(row.get("text", "")),
                        "wall_time": _finite(row.get("receive_wall_time")),
                    }
                )
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
        "timeout_events": timeout_events,
        "rtf": sim_delta / wall_s if sim_delta is not None and wall_s and wall_s > 0 else None,
        "wall_s": wall_s,
        "created_wall_time": _finite(meta.get("created_wall_time")),
        "finished_wall_time": _finite(meta.get("finished_wall_time")),
        "host_pressure": _pressure(_jsonl(episode_dir / "host_pressure.jsonl")),
    }


def _trace_rows(path: Path) -> list[dict[str, Any]]:
    return sorted(
        (row for row in _jsonl(path) if _finite(row.get("t")) is not None),
        key=lambda row: float(row["t"]),
    )


def _nearest_trace_sample(timestamp: float, samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not samples:
        return None
    return min(samples, key=lambda row: abs(float(row["t"]) - timestamp))


def _trace_pressure(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": sample.get("t"),
        "cpu_psi_some": (sample.get("cpu") or {}).get("some"),
        "load": sample.get("load"),
        "top_cgroups": [
            {"name": name, "cpu_percent": value}
            for name, value in (sample.get("top_cores") or {}).items()
        ],
    }


def _fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p-value for [[a, b], [c, d]]."""
    row1, row2, col1 = a + b, c + d, a + c
    total = row1 + row2
    if total == 0:
        return 1.0

    def probability(x: int) -> float:
        return (
            math.comb(col1, x)
            * math.comb(total - col1, row1 - x)
            / math.comb(total, row1)
        )

    low, high = max(0, row1 - (total - col1)), min(row1, col1)
    observed = probability(a)
    return min(1.0, sum(probability(x) for x in range(low, high + 1) if probability(x) <= observed + 1e-12))


def correlate_trace(
    reports: list[dict[str, Any]], samples: list[dict[str, Any]]
) -> dict[str, Any]:
    if not samples:
        return {
            "trace_start": None,
            "trace_end": None,
            "episodes": [],
            "events": [],
            "burst_cutoff_cpu_psi_some": None,
            "burst_vs_quiet": {
                "burst_timeout_episodes": 0,
                "burst_no_timeout_episodes": 0,
                "quiet_timeout_episodes": 0,
                "quiet_no_timeout_episodes": 0,
                "fisher_two_sided_p": 1.0,
            },
        }
    trace_start, trace_end = float(samples[0]["t"]), float(samples[-1]["t"])
    episode_rows = []
    event_rows = []
    for report in reports:
        start, end = report.get("created_wall_time"), report.get("finished_wall_time")
        if start is None or end is None or end < trace_start or start > trace_end:
            continue
        covered = [row for row in samples if start <= float(row["t"]) <= end]
        cpu_values = [
            _finite((row.get("cpu") or {}).get("some")) for row in covered
        ]
        cpu_values = [value for value in cpu_values if value is not None]
        loads = [_finite(row.get("load")) for row in covered]
        loads = [value for value in loads if value is not None]
        top_cgroups: dict[str, list[float]] = {}
        for row in covered:
            for name, value in (row.get("top_cores") or {}).items():
                numeric = _finite(value)
                if numeric is not None:
                    top_cgroups.setdefault(name, []).append(numeric)
        psi_means = {}
        for resource in ("cpu", "memory", "io"):
            values = [
                _finite((row.get(resource) or {}).get("some")) for row in covered
            ]
            values = [value for value in values if value is not None]
            psi_means[f"{resource}_psi_some_mean"] = statistics.fmean(values) if values else None
        episode_rows.append(
            {
                "episode": report["episode"],
                "start": start,
                "end": end,
                "samples": len(covered),
                "timeouts": report["timeouts"],
                "rtf": report["rtf"],
                "cpu_psi_some_mean": statistics.fmean(cpu_values) if cpu_values else None,
                "cpu_psi_some_peak": max(cpu_values) if cpu_values else None,
                **psi_means,
                "load_mean": statistics.fmean(loads) if loads else None,
                "load_peak": max(loads) if loads else None,
                "top_cgroups": [
                    {"name": name, "mean_cpu_percent": statistics.fmean(values), "samples": len(values)}
                    for name, values in sorted(
                        top_cgroups.items(), key=lambda item: -statistics.fmean(item[1])
                    )[:5]
                ],
            }
        )
        for event in report["timeout_events"]:
            timestamp = event.get("wall_time")
            sample = (
                _nearest_trace_sample(timestamp, samples)
                if timestamp is not None and trace_start <= timestamp <= trace_end
                else None
            )
            event_rows.append(
                {
                    "episode": report["episode"],
                    **event,
                    "trace_sample": _trace_pressure(sample) if sample else None,
                }
            )
    psi = [
        value
        for row in samples
        for value in [_finite((row.get("cpu") or {}).get("some"))]
        if value is not None
    ]
    burst_cutoff = statistics.quantiles(psi, n=4, method="inclusive")[2] if len(psi) > 1 else (psi[0] if psi else None)
    burst_cases = quiet_cases = burst_controls = quiet_controls = 0
    if burst_cutoff is not None:
        for row in episode_rows:
            covered = [sample for sample in samples if row["start"] <= float(sample["t"]) <= row["end"]]
            is_burst = any(
                (_finite((sample.get("cpu") or {}).get("some")) or -math.inf) > burst_cutoff
                for sample in covered
            )
            row["burst"] = is_burst
            has_timeout = sum(row["timeouts"].values()) > 0
            if is_burst:
                burst_cases += int(has_timeout)
                burst_controls += int(not has_timeout)
            else:
                quiet_cases += int(has_timeout)
                quiet_controls += int(not has_timeout)
    return {
        "trace_start": trace_start,
        "trace_end": trace_end,
        "episodes": episode_rows,
        "events": event_rows,
        "burst_cutoff_cpu_psi_some": burst_cutoff,
        "burst_vs_quiet": {
            "burst_timeout_episodes": burst_cases,
            "burst_no_timeout_episodes": burst_controls,
            "quiet_timeout_episodes": quiet_cases,
            "quiet_no_timeout_episodes": quiet_controls,
            "fisher_two_sided_p": _fisher_exact_two_sided(
                burst_cases, burst_controls, quiet_cases, quiet_controls
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path, help="episode directories")
    parser.add_argument("--trace", type=Path, help="whole-machine host trace JSONL")
    args = parser.parse_args(argv)
    episode_dirs = []
    for path in args.episodes:
        if (path / "episode.json").is_file():
            episode_dirs.append(path)
        else:
            episode_dirs.extend(sorted(p.parent for p in path.rglob("episode.json")))
    reports = [analyze_episode(path) for path in episode_dirs]
    print("episode\tstatus\taccel_timeout\tgyro_timeout\tmag_timeout\trtf\tpressure")
    for report in reports:
        pressure = report["host_pressure"]
        print(
            f"{report['episode']}\t{report['status']}\t"
            f"{report['timeouts']['accel']}\t{report['timeouts']['gyro']}\t{report['timeouts']['mag']}\t"
            f"{report['rtf'] if report['rtf'] is not None else '-'}\t"
            f"{json.dumps(pressure, separators=(',', ':')) if pressure else '-'}"
        )
    if args.trace:
        trace_samples = _trace_rows(args.trace)
        correlation = correlate_trace(reports, trace_samples)
        print(
            "trace_capture="
            + json.dumps(
                {
                    "path": str(args.trace),
                    "sample_count": len(trace_samples),
                    "trace_start": correlation["trace_start"],
                    "trace_end": correlation["trace_end"],
                    "read_completed_at": datetime.datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                    "episode_count": len(reports),
                    "overlap_episode_count": len(correlation["episodes"]),
                },
                separators=(",", ":"),
            )
        )
        print("\ntrace_episode_summaries=" + json.dumps(correlation["episodes"], separators=(",", ":")))
        print("timeout_event_trace_samples=" + json.dumps(correlation["events"], separators=(",", ":")))
        print("burst_vs_quiet=" + json.dumps(correlation["burst_vs_quiet"], separators=(",", ":")))
        print("burst_cutoff_cpu_psi_some=" + str(correlation["burst_cutoff_cpu_psi_some"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
