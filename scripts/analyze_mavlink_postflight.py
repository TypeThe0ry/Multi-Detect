#!/usr/bin/env python3
"""Summarize flight state and telemetry from MAVLink JSONL artifacts."""

from __future__ import annotations

import argparse
import bisect
import json
import math
import sys
from pathlib import Path
from typing import Any


SEVERITIES = {
    0: "EMERGENCY",
    1: "ALERT",
    2: "CRITICAL",
    3: "ERROR",
    4: "WARNING",
    5: "NOTICE",
    6: "INFO",
    7: "DEBUG",
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _kind(row: dict[str, Any]) -> str:
    return str(row.get("message_type", row.get("mavpackettype", ""))).upper()


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _stamp(row: dict[str, Any]) -> int | None:
    value = row.get("receive_monotonic_ns")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _seconds(stamp: int | None, origin: int | None) -> float | None:
    if stamp is None or origin is None:
        return None
    return round((stamp - origin) / 1e9, 3)


def _mode_name(custom_mode: Any) -> str:
    value = _number(custom_mode)
    if value is None:
        return "unknown"
    raw = int(value)
    main, sub = (raw >> 16) & 0xFF, (raw >> 24) & 0xFF
    main_names = {
        1: "MANUAL",
        2: "ALTCTL",
        3: "POSCTL",
        4: "AUTO",
        5: "ACRO",
        6: "OFFBOARD",
        7: "STABILIZED",
        8: "RATTITUDE",
    }
    auto_sub = {1: "READY", 2: "TAKEOFF", 3: "LOITER", 4: "MISSION", 5: "RTL", 6: "LAND", 8: "FOLLOW_TARGET", 9: "PRECLAND"}
    name = main_names.get(main, f"MAIN_{main}")
    if main == 4 and sub:
        name += "." + auto_sub.get(sub, f"SUB_{sub}")
    return name


def _euler(q: Any) -> tuple[float, float, float] | None:
    if not isinstance(q, (list, tuple)) or len(q) != 4:
        return None
    values = [_number(v) for v in q]
    if any(v is None for v in values):
        return None
    w, x, y, z = values
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm < 1e-9:
        return None
    w, x, y, z = (v / norm for v in (w, x, y, z))
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch_arg = max(-1.0, min(1.0, 2 * (w * y - z * x)))
    pitch = math.asin(pitch_arg)
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    return tuple(math.degrees(v) for v in (roll, pitch, yaw))


def _extremes(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": round(min(values), 4), "max": round(max(values), 4)}


def analyze_episode(episode_dir: Path) -> dict[str, Any]:
    episode_dir = episode_dir.resolve()
    mavlink_path = episode_dir / "mavlink.jsonl"
    if not mavlink_path.is_file():
        raise FileNotFoundError(f"missing {mavlink_path}")
    rows = _read_jsonl(mavlink_path)
    ordered = sorted(rows, key=lambda row: _stamp(row) if _stamp(row) is not None else -1)
    stamps = [_stamp(row) for row in ordered]
    origin = next((stamp for stamp in stamps if stamp is not None), None)
    events: dict[str, Any] = {
        "armed_at_s": None,
        "disarmed_at_s": None,
        "landed_at_s": None,
        "airborne_at_s": None,
        "flight_mode_changes": [],
        "heartbeat_gaps_s": [],
        "status_text": [],
    }
    armed_previous = None
    landed_previous = None
    mode_previous = object()
    heartbeat_stamps = []
    attitudes = {"roll_deg": [], "pitch_deg": [], "yaw_deg": []}
    altitudes = []
    positions = {"x_m": [], "y_m": [], "z_m": []}
    target_comparisons: dict[str, list[float]] = {"altitude_error_m": [], "position_error_m": []}
    local_samples = []
    gimbal_angles = {"roll_deg": [], "pitch_deg": [], "yaw_deg": []}
    gimbal_samples: list[tuple[int, tuple[float, float, float]]] = []
    gimbal_command_errors = {"roll_deg": [], "pitch_deg": [], "yaw_deg": []}
    gimbal_commands = 0
    gimbal_command_valid = 0
    servo_samples = 0
    servo_ranges: dict[str, list[float]] = {}
    type_counts: dict[str, int] = {}

    for row in ordered:
        kind = _kind(row)
        type_counts[kind] = type_counts.get(kind, 0) + 1
        stamp = _stamp(row)
        if kind == "HEARTBEAT":
            if stamp is not None:
                heartbeat_stamps.append(stamp)
            base_mode = _number(row.get("base_mode"))
            armed = bool(int(base_mode) & 128) if base_mode is not None else None
            if armed is not None and armed != armed_previous:
                key = "armed_at_s" if armed else "disarmed_at_s"
                if events[key] is None:
                    events[key] = _seconds(stamp, origin)
                armed_previous = armed
            custom = row.get("custom_mode")
            if custom is not None and custom != mode_previous:
                events["flight_mode_changes"].append({
                    "time_s": _seconds(stamp, origin),
                    "custom_mode": custom,
                    "mode": _mode_name(custom),
                })
                mode_previous = custom
        elif kind == "EXTENDED_SYS_STATE":
            landed_state = _number(row.get("landed_state"))
            if landed_state in (1, 2) and landed_state != landed_previous:
                if landed_state == 2 and events["airborne_at_s"] is None:
                    events["airborne_at_s"] = _seconds(stamp, origin)
                if landed_state == 1 and events["airborne_at_s"] is not None and events["landed_at_s"] is None:
                    events["landed_at_s"] = _seconds(stamp, origin)
                landed_previous = landed_state
        elif kind == "STATUSTEXT":
            severity = _number(row.get("severity"))
            severity_int = int(severity) if severity is not None else None
            events["status_text"].append({
                "time_s": _seconds(stamp, origin),
                "severity": severity_int,
                "severity_name": SEVERITIES.get(severity_int, "UNKNOWN"),
                "text": row.get("text", ""),
            })
        elif kind == "ATTITUDE":
            for name in attitudes:
                value = _number(row.get(name.removesuffix("_deg")))
                if value is not None:
                    attitudes[name].append(math.degrees(value))
        elif kind == "LOCAL_POSITION_NED":
            sample = {axis: _number(row.get(axis)) for axis in ("x", "y", "z")}
            if all(v is not None for v in sample.values()):
                local_samples.append((stamp, sample))
                for axis, value in sample.items():
                    positions[f"{axis}_m"].append(value)
                altitudes.append(-sample["z"])
        elif kind in {"GIMBAL_DEVICE_ATTITUDE_STATUS", "GIMBAL_DEVICE_SET_ATTITUDE"}:
            angles = _euler(row.get("q"))
            if kind == "GIMBAL_DEVICE_SET_ATTITUDE":
                gimbal_commands += 1
                if angles is not None:
                    gimbal_command_valid += 1
            elif angles is not None:
                if stamp is not None:
                    gimbal_samples.append((stamp, angles))
                for key, value in zip(gimbal_angles, angles, strict=True):
                    gimbal_angles[key].append(value)
        elif kind == "SERVO_OUTPUT_RAW":
            servo_samples += 1
            for key, value in row.items():
                if key.startswith("servo") and key.endswith("_raw"):
                    number = _number(value)
                    if number is not None:
                        servo_ranges.setdefault(key, []).append(number)

    for a, b in zip(heartbeat_stamps, heartbeat_stamps[1:]):
        events["heartbeat_gaps_s"].append(round((b - a) / 1e9, 3))

    targets = [row for row in ordered if _kind(row) == "POSITION_TARGET_LOCAL_NED"]
    local_stamps = [stamp for stamp, _ in local_samples if stamp is not None]
    for target in targets:
        target_stamp = _stamp(target)
        if target_stamp is None or not local_samples:
            continue
        mask = _number(target.get("type_mask"))
        if mask is None:
            continue
        index = bisect.bisect_left(local_stamps, target_stamp)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(local_samples)]
        nearest_index = min(candidates, key=lambda i: abs(local_samples[i][0] - target_stamp), default=None)
        nearest_stamp, actual = local_samples[nearest_index] if nearest_index is not None else (None, None)
        if actual is None or abs(nearest_stamp - target_stamp) > 250_000_000:
            continue
        valid_axes = [axis for bit, axis in enumerate(("x", "y", "z")) if not (int(mask) & (1 << bit))]
        errors = []
        for axis in valid_axes:
            desired = _number(target.get(axis))
            if desired is not None:
                errors.append(actual[axis] - desired)
        if errors:
            target_comparisons["position_error_m"].append(math.sqrt(sum(e * e for e in errors)))
            if "z" in valid_axes:
                desired_z = _number(target.get("z"))
                if desired_z is not None:
                    target_comparisons["altitude_error_m"].append(actual["z"] - desired_z)

    gimbal_stamps = [stamp for stamp, _ in gimbal_samples]
    for row in ordered:
        if _kind(row) != "GIMBAL_DEVICE_SET_ATTITUDE":
            continue
        command_angles = _euler(row.get("q"))
        stamp = _stamp(row)
        if command_angles is None or stamp is None or not gimbal_stamps:
            continue
        index = bisect.bisect_left(gimbal_stamps, stamp)
        candidates = [i for i in (index - 1, index) if 0 <= i < len(gimbal_samples)]
        nearest_index = min(candidates, key=lambda i: abs(gimbal_stamps[i] - stamp), default=None)
        if nearest_index is None or abs(gimbal_stamps[nearest_index] - stamp) > 250_000_000:
            continue
        measured = gimbal_samples[nearest_index][1]
        for key, actual, desired in zip(gimbal_command_errors, measured, command_angles, strict=True):
            gimbal_command_errors[key].append((actual - desired + 180) % 360 - 180)

    def error_summary(values: list[float]) -> dict[str, float] | None:
        if not values:
            return None
        return {"samples": len(values), "mean_abs": round(sum(abs(v) for v in values) / len(values), 3), "max_abs": round(max(abs(v) for v in values), 3)}

    step_path = episode_dir / "steps.jsonl"
    step_count = sum(1 for _ in _read_jsonl(step_path)) if step_path.is_file() else None
    gap_values = events["heartbeat_gaps_s"]
    return {
        "episode": str(episode_dir),
        "steps_count": step_count,
        "message_counts": type_counts,
        "timeline": events,
        "altitude_m": _extremes(altitudes),
        "local_position_ned_m": {key: _extremes(values) for key, values in positions.items()},
        "setpoint_comparison": {
            "position_error_m": error_summary(target_comparisons["position_error_m"]),
            "altitude_error_m": error_summary(target_comparisons["altitude_error_m"]),
            "valid_matched_samples": len(target_comparisons["position_error_m"]),
            "note": "LOCAL_POSITION_NED compared to POSITION_TARGET_LOCAL_NED only on unmasked, finite axes; time match <= 0.25 s.",
        },
        "attitude_extremes_deg": {key: _extremes(values) for key, values in attitudes.items()},
        "heartbeat_gap_s": {
            "samples": len(gap_values),
            "max": max(gap_values) if gap_values else None,
            "over_2s": sum(value > 2 for value in gap_values),
        },
        "gimbal": {
            "measured_angle_extremes_deg": {key: _extremes(values) for key, values in gimbal_angles.items()},
            "command_messages": gimbal_commands,
            "finite_quaternion_commands": gimbal_command_valid,
            "command_error_deg": {key: error_summary(values) for key, values in gimbal_command_errors.items()},
            "comparison": "nearest finite GIMBAL_DEVICE_ATTITUDE_STATUS quaternion within 0.25 s of a finite commanded GIMBAL_DEVICE_SET_ATTITUDE quaternion",
        },
        "servo_output_raw": {
            "samples": servo_samples,
            "channel_extremes_raw": {key: _extremes(values) for key, values in servo_ranges.items()},
            "rotor_mapping": None,
        },
        "ulog_data_not_in_these_mavlink_logs": [
            "ULog topic-level estimator internals and innovations",
            "raw/high-rate IMU sensor samples and synchronized uORB topic timing",
            "individual motor/rotor actuator outputs or rotor mapping (SERVO_OUTPUT_RAW is present but channels are not identified as rotors)",
            "controller-specific setpoints/status beyond MAVLink telemetry",
        ],
    }


def _episode_dirs(root: Path) -> list[Path]:
    if (root / "mavlink.jsonl").is_file():
        return [root]
    return sorted(path.parent for path in root.rglob("mavlink.jsonl"))


def _text(report: dict[str, Any]) -> str:
    lines = [f"Episode: {report['episode']}", f"Steps: {report['steps_count']}"]
    timeline = report["timeline"]
    for label, key in (("armed", "armed_at_s"), ("airborne", "airborne_at_s"), ("landed", "landed_at_s"), ("disarmed", "disarmed_at_s")):
        lines.append(f"{label}: {timeline[key]} s")
    lines.append("Flight modes: " + (", ".join(f"{x['time_s']}s {x['mode']} (0x{int(x['custom_mode']):08x})" for x in timeline["flight_mode_changes"]) or "none"))
    lines.append(f"Altitude (NED-derived, m): {report['altitude_m']}")
    lines.append(f"Position setpoint error: {report['setpoint_comparison']['position_error_m']}")
    lines.append(f"Altitude setpoint error: {report['setpoint_comparison']['altitude_error_m']}")
    lines.append(f"Attitude extremes (deg): {report['attitude_extremes_deg']}")
    lines.append(f"Heartbeat gaps (s): max={report['heartbeat_gap_s']['max']}, >2s={report['heartbeat_gap_s']['over_2s']}")
    lines.append(f"STATUSTEXT: {len(timeline['status_text'])} messages")
    for item in timeline["status_text"]:
        lines.append(f"  {item['time_s']}s [{item['severity_name']}] {item['text']}")
    lines.append(f"Gimbal: measured {report['gimbal']['measured_angle_extremes_deg']}; command error {report['gimbal']['command_error_deg']}; finite quaternions {report['gimbal']['finite_quaternion_commands']}/{report['gimbal']['command_messages']}")
    lines.append(f"SERVO_OUTPUT_RAW: {report['servo_output_raw']['samples']} samples; no rotor mapping in log")
    lines.append("ULog-only data absent here: " + "; ".join(report["ulog_data_not_in_these_mavlink_logs"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="episode directory or campaign directory")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    episodes = _episode_dirs(args.path)
    if not episodes:
        parser.error(f"no episode mavlink.jsonl files found under {args.path}")
    try:
        reports = [analyze_episode(path) for path in episodes]
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(reports[0] if len(reports) == 1 else reports, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print("\n\n".join(_text(report) for report in reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
