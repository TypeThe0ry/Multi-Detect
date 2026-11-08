#!/usr/bin/env python3
"""Offline timeline analysis for a recorded PX4/Gazebo clock-stall episode."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _iso_utc(epoch_s: float | None) -> str | None:
    if epoch_s is None:
        return None
    return datetime.fromtimestamp(epoch_s, UTC).isoformat(timespec="milliseconds")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def analyze_episode(episode_dir: str | Path) -> dict[str, Any]:
    root = Path(episode_dir)
    meta = _read_json(root / "episode.json")
    steps = _read_jsonl(root / "steps.jsonl")
    mavlink = _read_jsonl(root / "mavlink.jsonl")
    pressure = _read_jsonl(root / "host_pressure.jsonl")
    capture = _read_json(root / "px4-clock-stall-capture.json")

    created_wall = _number(meta.get("created_wall_time"))
    created_mono_ns = _number(meta.get("created_monotonic_ns"))
    wall_mono_offset = (
        created_wall - created_mono_ns / 1e9
        if created_wall is not None and created_mono_ns is not None
        else None
    )

    local_rows = []
    for row in mavlink:
        if (
            str(row.get("message_type", row.get("mavpackettype", ""))).upper()
            != "LOCAL_POSITION_NED"
        ):
            continue
        boot_ms = _number(row.get("time_boot_ms"))
        mono_ns = _number(row.get("receive_monotonic_ns"))
        wall = _number(row.get("receive_wall_time"))
        if boot_ms is not None:
            local_rows.append(
                {
                    "px4_s": boot_ms / 1000.0,
                    "monotonic_s": mono_ns / 1e9 if mono_ns is not None else None,
                    "wall_time": wall,
                }
            )
    local_rows.sort(key=lambda row: (row["monotonic_s"] is None, row["monotonic_s"] or 0))

    climb_rows = []
    for row in steps:
        action = row.get("action") if isinstance(row.get("action"), dict) else {}
        outcome = row.get("outcome") if isinstance(row.get("outcome"), dict) else {}
        if action.get("up_m", 0) <= 0 and action.get("kind") != "climb":
            continue
        before = row.get("obs_before") if isinstance(row.get("obs_before"), dict) else {}
        after = row.get("obs_after") if isinstance(row.get("obs_after"), dict) else {}
        start_px4 = _number(before.get("pose_time_boot_ms"))
        end_px4 = _number(after.get("pose_time_boot_ms"))
        wait_ms = _number((row.get("timing") or {}).get("command_and_primitive_wait_ms"))
        sim_delta = (
            (end_px4 - start_px4) / 1000 if start_px4 is not None and end_px4 is not None else None
        )
        wall_s = wait_ms / 1000 if wait_ms is not None else None
        climb_rows.append(
            {
                "step_index": row.get("step_index"),
                "status": outcome.get("status"),
                "fault_injection": (row.get("ack") or {}).get("sim_fault_injection"),
                "px4_start_s": start_px4 / 1000 if start_px4 is not None else None,
                "px4_end_s": end_px4 / 1000 if end_px4 is not None else None,
                "px4_elapsed_s": sim_delta,
                "primitive_wait_wall_s": wall_s,
                "rtf": sim_delta / wall_s
                if sim_delta is not None and wall_s and wall_s > 0
                else None,
                "timing_source": "steps.timing.command_and_primitive_wait_ms",
                "receive_timestamp_is_primitive_boundary": False,
            }
        )

    samples = capture.get("samples") if isinstance(capture.get("samples"), list) else []
    capture_timeline = []
    analysis = capture.get("analysis") if isinstance(capture.get("analysis"), dict) else {}
    gz_values = (
        analysis.get("gz_clock_samples_s")
        if isinstance(analysis.get("gz_clock_samples_s"), list)
        else []
    )
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            continue
        wall = _number(sample.get("captured_wall_time"))
        mono = _number(sample.get("captured_monotonic_s"))
        derived = mono is None and wall is not None and wall_mono_offset is not None
        if derived:
            mono = wall - wall_mono_offset
        gz = _number((sample.get("gz_clock") or {}).get("latest_sim_time_s"))
        if gz is None and index < len(gz_values):
            gz = _number(gz_values[index])
        capture_timeline.append(
            {
                "label": sample.get("label"),
                "wall_time": wall,
                "wall_utc": _iso_utc(wall),
                "monotonic_s": mono,
                "monotonic_source": "derived_from_episode_wall_monotonic_anchor"
                if derived
                else "capture",
                "px4_s": None,
                "gazebo_s": gz,
                "px4_listener_timestamp_s": None,
            }
        )

    diagnostics = (
        capture.get("px4_clock_diagnostics")
        if isinstance(capture.get("px4_clock_diagnostics"), dict)
        else {}
    )
    last_progress_mono = _number(diagnostics.get("last_progress_at_monotonic_s"))
    last_progress_px4 = _number(diagnostics.get("last_progress_px4_clock_s"))
    stall_window = (
        _number((capture.get("reason") or "").split("wall_timeout_s=")[-1])
        if "wall_timeout_s=" in str(capture.get("reason", ""))
        else 45.0
    )
    watchdog_deadline = (
        last_progress_mono + stall_window
        if last_progress_mono is not None and stall_window is not None
        else None
    )

    for point in capture_timeline:
        if point["label"] == "deadline":
            point["px4_s"] = last_progress_px4
            point["watchdog_deadline_monotonic_s"] = watchdog_deadline
        elif point["label"] == "after_3s":
            point["px4_s"] = last_progress_px4

    pressure_metrics: dict[str, Any] = {"sample_count": len(pressure)}
    load1, busy, cpu_some, memory_some = [], [], [], []
    for row in pressure:
        raw_load = row.get("loadavg")
        if isinstance(raw_load, str):
            load1_value = _number(raw_load.split()[0]) if raw_load.split() else None
        else:
            load1_value = _number(raw_load)
        if load1_value is not None:
            load1.append(load1_value)
        for field, values in (("core_busy_percent_mean", busy),):
            value = _number(row.get(field))
            if value is not None:
                values.append(value)
        for group, values in (("cpu_pressure", cpu_some), ("memory_pressure", memory_some)):
            pressure_group = row.get(group)
            some = pressure_group.get("some") if isinstance(pressure_group, dict) else None
            value = _number(some.get("avg10")) if isinstance(some, dict) else None
            if value is not None:
                values.append(value)
    for key, values in (
        ("load1", load1),
        ("core_busy_percent_mean", busy),
        ("cpu_psi_some_avg10", cpu_some),
        ("memory_psi_some_avg10", memory_some),
    ):
        pressure_metrics[key] = {
            "min": min(values) if values else None,
            "median": statistics.median(values) if values else None,
            "p95": _percentile(values, 0.95),
            "max": max(values) if values else None,
            "observed_count": len(values),
        }

    events = []
    for step in climb_rows:
        source_step = next(
            (row for row in steps if row.get("step_index") == step["step_index"]), {}
        )
        before = source_step.get("obs_before") or {}
        start_mono = _number(before.get("receive_monotonic_ns"))
        end_mono = (
            start_mono / 1e9 + step["primitive_wait_wall_s"]
            if start_mono is not None and step["primitive_wait_wall_s"] is not None
            else None
        )
        events.append(
            {
                "event": "climb_primitive_timeout"
                if step["status"] == "timeout"
                else "climb_primitive",
                "px4_s": step["px4_end_s"],
                "monotonic_s": end_mono,
                "wall_time": (
                    end_mono + wall_mono_offset
                    if end_mono is not None and wall_mono_offset is not None
                    else None
                ),
                "time_source": "obs_before_receive_monotonic_plus_primitive_wait_duration",
                "step_index": step["step_index"],
            }
        )
    if last_progress_mono is not None:
        events.append(
            {
                "event": "last_px4_progress",
                "px4_s": last_progress_px4,
                "monotonic_s": last_progress_mono,
                "wall_time": last_progress_mono + wall_mono_offset
                if wall_mono_offset is not None
                else None,
            }
        )
    if watchdog_deadline is not None:
        events.append(
            {
                "event": "watchdog_deadline_after_no_progress",
                "px4_s": last_progress_px4,
                "monotonic_s": watchdog_deadline,
                "wall_time": watchdog_deadline + wall_mono_offset
                if wall_mono_offset is not None
                else None,
                "duration_s": stall_window,
            }
        )
    for point in capture_timeline:
        events.append({"event": f"capture_{point['label']}", **point})
    events.sort(
        key=lambda event: (
            event.get("monotonic_s") if event.get("monotonic_s") is not None else float("inf")
        )
    )

    missing = []
    if not steps:
        missing.append("steps.jsonl")
    if not mavlink:
        missing.append("mavlink.jsonl")
    if not pressure:
        missing.append("host_pressure.jsonl")
    if not capture:
        missing.append("px4-clock-stall-capture.json")
    missing_evidence = [
        "capture monotonic timestamps are absent; capture times are wall/monotonic-anchor derived",
        "capture samples omit PX4 listener timestamps; capture analysis has probe results",
        "capture world paused state is unknown",
        "camera progress is unknown because capture does not probe camera frames",
        "finite capture samples cannot establish continuous Gazebo/PX4 behavior between probes",
        "no time-series cgroup cpu.stat or memory.events evidence for the failure window",
    ]
    return {
        "episode": root.name,
        "read_only": True,
        "clock_alignment": {
            "wall_monotonic_offset_s": wall_mono_offset,
            "offset_source": "episode.created_wall_time - episode.created_monotonic_ns",
            "px4_clock_source": (
                "MAVLink LOCAL_POSITION_NED.time_boot_ms and step pose_time_boot_ms"
            ),
            "gazebo_clock_source": "px4-clock-stall-capture samples gz_clock.latest_sim_time_s",
            "capture_clock_conversion_is_derived": True,
        },
        "climb_primitives": climb_rows,
        "mavlink_local_position": {
            "count": len(local_rows),
            "first": local_rows[0] if local_rows else None,
            "last": local_rows[-1] if local_rows else None,
            "last_progress_px4_s_from_capture_diagnostics": last_progress_px4,
            "last_progress_monotonic_s": last_progress_mono,
        },
        "capture_samples": capture_timeline,
        "capture_analysis": {
            "gz_clock_advanced": analysis.get("gz_clock_advanced", "unknown"),
            "gz_world_paused_samples": analysis.get("gz_world_paused_samples", "unknown"),
            "imu_advanced": (analysis.get("sensor_progress") or {})
            .get("imu", {})
            .get("advanced", "unknown"),
            "vehicle_status_advanced": (analysis.get("sensor_progress") or {})
            .get("vehicle_status", {})
            .get("advanced", "unknown"),
            "camera_advanced": (analysis.get("camera_progress") or {}).get("advanced", "unknown"),
            "gz_runner_stack_classification": analysis.get("gz_runner_stack_classification", []),
            "gz_publish_stats_mutex_wait_observed": analysis.get(
                "gz_publish_stats_mutex_wait_observed", "unknown"
            ),
        },
        "host_pressure": pressure_metrics,
        "events": events,
        "missing_files": missing,
        "missing_or_limited_evidence": missing_evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "episode", type=Path, help="episode directory or directory containing episode inputs"
    )
    parser.add_argument("--json", action="store_true", help="print JSON report")
    args = parser.parse_args(argv)
    report = analyze_episode(args.episode)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"Episode: {report['episode']}")
        for row in report["climb_primitives"]:
            print(
                f"climb step={row['step_index']} status={row['status']} "
                f"PX4={row['px4_elapsed_s']}s wall={row['primitive_wait_wall_s']}s "
                f"RTF={row['rtf']}"
            )
        print(
            f"capture samples: {len(report['capture_samples'])}; "
            f"pressure samples: {report['host_pressure']['sample_count']}"
        )
        print("missing/limited evidence:")
        for item in report["missing_or_limited_evidence"]:
            print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
