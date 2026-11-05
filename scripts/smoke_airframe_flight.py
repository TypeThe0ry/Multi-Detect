#!/usr/bin/env python3
"""固定翼/VTOL 仿真冒烟飞行计划与执行器。

默认执行完整采集器 episode；``--dry-run`` 只解析档案和场景并输出可审计
的步骤/命令 JSON，不启动 Docker。手抛和弹射不会伪造成功，结果会明确标记
需要 Gazebo 外部冲量。
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.airframe_profile import AirframeProfile  # noqa: E402
from multidetect.simulator.delta_tailsitter_smoke import (  # noqa: E402
    tailsitter_flight_angles_deg,
)

DELTA_TAILSITTER_PROFILE_IDS = frozenset(
    {"delta_tailsitter", "delta_tailsitter_gimbal"}
)
FW_ARRIVAL_TOLERANCE_M = 8.0
FW_SATURATION_LOW = 5
FW_SATURATION_HIGH = 1095
FW_SATURATION_MAX_FRACTION = 0.15
FW_SELF_OCCLUSION_MAX_FRACTION = 0.05
FW_MIN_METRIC_SAMPLES = 30
FW_MIN_DEPTH_FRAMES = 5


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        rows = []
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


def _mav_value(row: dict[str, Any], field: str, default: Any = None) -> Any:
    if field in row:
        return row[field]
    message = row.get("message")
    if isinstance(message, dict):
        return message.get(field, default)
    return default


def _difference_norm(left: Any, right: Any) -> float | None:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return None
    if len(left) < 3 or len(right) < 3:
        return None
    try:
        return math.sqrt(
            sum((float(a) - float(b)) ** 2 for a, b in zip(left[:3], right[:3], strict=False))
        )
    except (TypeError, ValueError):
        return None


def _row_sim_time_s(row: dict[str, Any]) -> float | None:
    direct = row.get("sim_time_s")
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            return None
    boot_ms = _mav_value(row, "time_boot_ms")
    if boot_ms is not None:
        try:
            return float(boot_ms) / 1000.0
        except (TypeError, ValueError):
            pass
    time_usec = _mav_value(row, "time_usec")
    if time_usec is not None:
        try:
            return float(time_usec) / 1_000_000.0
        except (TypeError, ValueError):
            pass
    return None


def _timeline(mavlink: list[dict[str, Any]]) -> list[float | None]:
    """Align untimed MAVLink records to nearby PX4-clock-stamped packets."""
    anchors: list[tuple[int, float]] = []
    direct = [_row_sim_time_s(row) for row in mavlink]
    for row, sim_time in zip(mavlink, direct, strict=True):
        receive_ns = row.get("receive_monotonic_ns")
        if sim_time is None or receive_ns is None:
            continue
        try:
            anchors.append((int(receive_ns), sim_time))
        except (TypeError, ValueError):
            continue
    anchors.sort()
    receive_times = [item[0] for item in anchors]
    aligned: list[float | None] = []
    for row, sim_time in zip(mavlink, direct, strict=True):
        if sim_time is not None:
            aligned.append(sim_time)
            continue
        receive_ns = row.get("receive_monotonic_ns")
        if receive_ns is None or not anchors:
            aligned.append(None)
            continue
        try:
            stamp = int(receive_ns)
        except (TypeError, ValueError):
            aligned.append(None)
            continue
        right = bisect.bisect_left(receive_times, stamp)
        if right == 0:
            aligned.append(None)
        elif right >= len(anchors):
            aligned.append(None)
        else:
            left_ns, left_s = anchors[right - 1]
            right_ns, right_s = anchors[right]
            ratio = (stamp - left_ns) / max(right_ns - left_ns, 1)
            aligned.append(left_s + ratio * (right_s - left_s))
    return aligned


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percent / 100.0
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _takeoff_sim_time_s(
    mavlink: list[dict[str, Any]], takeoff_altitude_m: float
) -> float | None:
    for row in mavlink:
        if row.get("message_type") == "fixed_wing_takeoff_complete":
            sim_time = _row_sim_time_s(row)
            if sim_time is not None:
                return sim_time
    threshold_m = max(0.5, takeoff_altitude_m - 1.0)
    for row in mavlink:
        if str(row.get("message_type", "")).upper() != "LOCAL_POSITION_NED":
            continue
        try:
            if -float(_mav_value(row, "z")) >= threshold_m:
                sim_time = _row_sim_time_s(row)
                if sim_time is not None:
                    return sim_time
        except (TypeError, ValueError):
            continue
    return None


def _depth_self_occlusion(
    episode_dir: Path,
    steps: list[dict[str, Any]],
    mavlink: list[dict[str, Any]],
    *,
    takeoff_altitude_m: float,
) -> dict[str, Any]:
    root = Path(episode_dir)
    paths = sorted(
        path
        for suffix in ("*.npy", "*.npz", "*.png")
        for path in (root / "depth").glob(suffix)
    )
    if not paths:
        return {
            "fraction": None,
            "frame_count": 0,
            "eligible_frame_count": 0,
            "pixel_count": 0,
            "near_pixel_count": 0,
            "takeoff_time_s": None,
        }
    try:
        import numpy as np
    except ImportError:
        return {
            "fraction": None,
            "frame_count": 0,
            "eligible_frame_count": 0,
            "pixel_count": 0,
            "near_pixel_count": 0,
            "error": "numpy unavailable",
        }
    takeoff_time_s = _takeoff_sim_time_s(mavlink, takeoff_altitude_m)
    if takeoff_time_s is None:
        return {
            "fraction": None,
            "frame_count": 0,
            "eligible_frame_count": 0,
            "pixel_count": 0,
            "near_pixel_count": 0,
            "takeoff_time_s": None,
            "error": "takeoff simulation timestamp unavailable",
        }
    stamps_by_name: dict[str, int] = {}
    for row in steps:
        for observation_name in ("obs_before", "obs_after"):
            observation = row.get(observation_name)
            if not isinstance(observation, dict):
                continue
            frame_stamp = observation.get("frame_sensor_stamp_ns")
            depth_path = observation.get("depth_path")
            if frame_stamp is None or not isinstance(depth_path, str):
                continue
            try:
                stamps_by_name[Path(depth_path).name] = int(frame_stamp)
            except (TypeError, ValueError):
                continue
    pixel_count = 0
    near_pixel_count = 0
    eligible_frame_count = 0
    unreadable_frame_count = 0
    takeoff_stamp_ns = int(takeoff_time_s * 1e9)
    for path in paths:
        stamp_ns = stamps_by_name.get(path.name)
        if stamp_ns is None:
            try:
                stamp_ns = int(path.stem.rsplit("_", 1)[-1])
            except ValueError:
                unreadable_frame_count += 1
                continue
        if stamp_ns < takeoff_stamp_ns:
            continue
        try:
            if path.suffix == ".png":
                from PIL import Image

                with Image.open(path) as image:
                    frame = np.asarray(image)
            else:
                loaded = np.load(path, allow_pickle=False)
                if isinstance(loaded, np.lib.npyio.NpzFile):
                    try:
                        frame = loaded["depth"]
                    finally:
                        loaded.close()
                else:
                    frame = loaded
        except (ImportError, OSError, ValueError, KeyError):
            unreadable_frame_count += 1
            continue
        depth_m = frame.astype(np.float64, copy=False)
        if np.issubdtype(frame.dtype, np.integer):
            depth_m = depth_m / 1000.0
        finite = np.isfinite(depth_m)
        eligible_frame_count += 1
        pixel_count += int(depth_m.size)
        near_pixel_count += int(np.count_nonzero(finite & (depth_m > 0.0) & (depth_m < 1.0)))
    return {
        "fraction": near_pixel_count / pixel_count if pixel_count else None,
        "frame_count": eligible_frame_count,
        "eligible_frame_count": eligible_frame_count,
        "available_frame_count": len(paths),
        "unreadable_or_untimed_frame_count": unreadable_frame_count,
        "pixel_count": pixel_count,
        "near_pixel_count": near_pixel_count,
        "takeoff_time_s": takeoff_time_s,
    }


def _delta_fw_metrics(
    mavlink: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    profile: AirframeProfile,
    episode_dir: Path,
    *,
    takeoff_altitude_m: float,
) -> dict[str, Any]:
    start = next(
        (row for row in mavlink if row.get("message_type") == "delta_smoke_fw_sustain_start"),
        None,
    )
    end = next(
        (row for row in mavlink if row.get("message_type") == "delta_smoke_fw_sustain_end"),
        None,
    )
    start_s = _row_sim_time_s(start) if start else None
    end_s = _row_sim_time_s(end) if end else None
    commanded_altitude = start.get("commanded_altitude_m") if start else None
    speeds: list[float] = []
    bank_abs: list[float] = []
    pitch: list[float] = []
    height_error: list[float] = []
    motor_samples = 0
    motor_saturated_samples = 0
    motor_output_samples = 0
    motor_output_saturated = 0
    motor_counts = [0, 0, 0, 0]
    motor_saturated_counts = [0, 0, 0, 0]
    aligned_times = _timeline(mavlink)
    state_updates: list[tuple[float, int]] = []
    for row, sim_time in zip(mavlink, aligned_times, strict=True):
        if str(row.get("message_type", "")).upper() != "EXTENDED_SYS_STATE" or sim_time is None:
            continue
        try:
            state_updates.append((sim_time, int(_mav_value(row, "vtol_state"))))
        except (TypeError, ValueError):
            continue
    state_updates.sort()
    state_update_times = [item[0] for item in state_updates]
    if start_s is not None and end_s is not None and end_s >= start_s:
        for row, sim_time in zip(mavlink, aligned_times, strict=True):
            kind = str(row.get("message_type", row.get("mavpackettype", ""))).upper()
            if sim_time is None or not start_s <= sim_time <= end_s:
                continue
            state_index = bisect.bisect_right(state_update_times, sim_time) - 1
            current_vtol_state = state_updates[state_index][1] if state_index >= 0 else None
            if current_vtol_state != 4:
                continue
            if kind == "VFR_HUD":
                try:
                    speeds.append(float(_mav_value(row, "airspeed")))
                except (TypeError, ValueError):
                    pass
            elif kind == "ATTITUDE_QUATERNION":
                try:
                    q = tuple(float(_mav_value(row, f"q{i}")) for i in range(1, 5))
                    pitch_up, bank_angle = tailsitter_flight_angles_deg(q)
                except (TypeError, ValueError):
                    continue
                pitch.append(pitch_up)
                bank_abs.append(abs(bank_angle))
            elif kind == "LOCAL_POSITION_NED" and commanded_altitude is not None:
                try:
                    altitude = -float(_mav_value(row, "z"))
                    height_error.append(abs(altitude - float(commanded_altitude)))
                except (TypeError, ValueError):
                    pass
            elif kind == "SERVO_OUTPUT_RAW":
                outputs: list[float] = []
                for index in range(1, 5):
                    try:
                        outputs.append(float(_mav_value(row, f"servo{index}_raw")))
                    except (TypeError, ValueError):
                        outputs = []
                        break
                if outputs:
                    motor_samples += 1
                    saturated = [
                        value <= FW_SATURATION_LOW or value >= FW_SATURATION_HIGH
                        for value in outputs
                    ]
                    motor_output_samples += len(outputs)
                    motor_output_saturated += sum(saturated)
                    for index, value in enumerate(outputs):
                        motor_counts[index] += 1
                        motor_saturated_counts[index] += int(saturated[index])
                    if any(saturated):
                        motor_saturated_samples += 1

    fw_duration_s = float(end.get("fw_duration_s", 0.0)) if end else 0.0
    orbit_degrees = float(end.get("orbit_signed_degrees", 0.0)) if end else 0.0
    sustain_success = bool(
        end
        and end.get("status") == "complete"
        and end.get("continuous_fw") is True
        and fw_duration_s >= 120.0
        and abs(orbit_degrees) >= 360.0
    )
    motor_fraction = (
        motor_saturated_samples / motor_samples if motor_samples else None
    )
    motor_output_fraction = (
        motor_output_saturated / motor_output_samples if motor_output_samples else None
    )
    motor_saturation_by_motor = [
        motor_saturated_counts[index] / count if count else None
        for index, count in enumerate(motor_counts)
    ]
    r_limit_deg = float(profile.max_bank_deg)
    metrics = {
        "fw_sustain_duration_s": fw_duration_s if end else None,
        "fw_orbit_signed_degrees": orbit_degrees if end else None,
        "fw_airspeed_p5_mps": _percentile(speeds, 5),
        "fw_airspeed_p95_mps": _percentile(speeds, 95),
        "fw_bank_abs_p95_deg": _percentile(bank_abs, 95),
        "fw_pitch_p5_deg": _percentile(pitch, 5),
        "fw_pitch_p95_deg": _percentile(pitch, 95),
        "fw_height_error_p95_m": _percentile(height_error, 95),
        "fw_motor_saturation_fraction": motor_fraction,
        "fw_motor_output_saturation_fraction": motor_output_fraction,
        "fw_motor_saturation_fraction_by_motor": motor_saturation_by_motor,
        "fw_motor_saturation_samples": motor_saturated_samples,
        "fw_motor_output_samples": motor_output_samples,
        "fw_motor_output_saturated_count": motor_output_saturated,
        "fw_sample_counts": {
            "airspeed": len(speeds),
            "attitude": len(pitch),
            "altitude": len(height_error),
            "motor_outputs": motor_samples,
        },
        "fw_thresholds": {
            "airspeed_p5_min_mps": profile.min_airspeed_mps,
            "airspeed_p95_max_mps": profile.max_airspeed_mps + 1.0,
            "fw_r_lim_deg": r_limit_deg,
            "bank_abs_p95_max_deg": r_limit_deg + 10.0,
            "pitch_p5_min_deg": -15.0,
            "pitch_p95_max_deg": 20.0,
            "height_error_p95_max_m": 10.0,
            "motor_saturation_fraction_max": FW_SATURATION_MAX_FRACTION,
            "minimum_samples_per_metric": FW_MIN_METRIC_SAMPLES,
            "motor_saturation_definition": "one sample is saturated if any of servo1..4 is <=5 or >=1095; per-motor and any-motor fractions are also reported",
        },
        "fw_sustain_success": sustain_success,
    }
    metrics["fw_metrics_pass"] = bool(
        metrics["fw_airspeed_p5_mps"] is not None
        and metrics["fw_airspeed_p5_mps"] >= profile.min_airspeed_mps
        and metrics["fw_airspeed_p95_mps"] is not None
        and metrics["fw_airspeed_p95_mps"] <= profile.max_airspeed_mps + 1.0
        and metrics["fw_bank_abs_p95_deg"] is not None
        and metrics["fw_bank_abs_p95_deg"] <= r_limit_deg + 10.0
        and metrics["fw_pitch_p5_deg"] is not None
        and metrics["fw_pitch_p5_deg"] >= -15.0
        and metrics["fw_pitch_p95_deg"] is not None
        and metrics["fw_pitch_p95_deg"] <= 20.0
        and metrics["fw_height_error_p95_m"] is not None
        and metrics["fw_height_error_p95_m"] <= 10.0
        and len(speeds) >= FW_MIN_METRIC_SAMPLES
        and len(pitch) >= FW_MIN_METRIC_SAMPLES
        and len(height_error) >= FW_MIN_METRIC_SAMPLES
        and motor_samples >= FW_MIN_METRIC_SAMPLES
        and motor_fraction is not None
        and motor_fraction <= FW_SATURATION_MAX_FRACTION
        and motor_output_fraction is not None
        and motor_output_fraction <= FW_SATURATION_MAX_FRACTION
        and all(
            value is not None and value <= FW_SATURATION_MAX_FRACTION
            for value in motor_saturation_by_motor
        )
    )
    relocations = [
        (row.get("ack") or {}).get("smoke_reposition_wait")
        for row in steps
        if isinstance((row.get("ack") or {}).get("smoke_reposition_wait"), dict)
    ]
    reached_relocations = [
        row
        for row in relocations
        if row.get("waited") == "arrived"
        and float(row.get("distance_m", math.inf)) <= FW_ARRIVAL_TOLERANCE_M
    ]
    relocation = reached_relocations[0] if reached_relocations else (relocations[0] if relocations else None)
    metrics["fw_reposition_success"] = bool(reached_relocations)
    metrics["fw_reposition_distance_m"] = (
        relocation.get("distance_m") if relocation else None
    )
    metrics["fw_reposition_tolerance_m"] = FW_ARRIVAL_TOLERANCE_M
    post_transition = next(
        (row for row in mavlink if row.get("message_type") == "delta_smoke_post_transition_end"),
        None,
    )
    metrics["post_transition_climb_m"] = (
        post_transition.get("climb_m") if post_transition else None
    )
    metrics["post_transition_climb_success"] = bool(
        post_transition
        and post_transition.get("status") == "complete"
        and isinstance(post_transition.get("climb_m"), (int, float))
        and float(post_transition["climb_m"]) <= 20.0
    )
    depth = _depth_self_occlusion(
        episode_dir,
        steps,
        mavlink,
        takeoff_altitude_m=takeoff_altitude_m,
    )
    metrics["self_occlusion_fraction"] = depth["fraction"]
    metrics["self_occlusion_frame_count"] = depth["frame_count"]
    metrics["self_occlusion_available_frame_count"] = depth.get("available_frame_count", 0)
    metrics["self_occlusion_takeoff_time_s"] = depth.get("takeoff_time_s")
    metrics["self_occlusion_unreadable_or_untimed_frame_count"] = depth.get(
        "unreadable_or_untimed_frame_count", 0
    )
    metrics["self_occlusion_pixel_count"] = depth["pixel_count"]
    metrics["self_occlusion_near_pixel_count"] = depth["near_pixel_count"]
    metrics["self_occlusion_threshold_fraction"] = FW_SELF_OCCLUSION_MAX_FRACTION
    metrics["self_occlusion_success"] = bool(
        depth["fraction"] is not None
        and depth["fraction"] < FW_SELF_OCCLUSION_MAX_FRACTION
        and depth["frame_count"] >= FW_MIN_DEPTH_FRAMES
    )
    return metrics


def evaluate_episode(
    episode_dir: Path,
    profile: AirframeProfile,
    target_altitude_m: float = 5.0,
    position_error_m: float | None = None,
) -> dict[str, Any]:
    """Evaluate a completed collector artifact from telemetry and step records."""
    episode_dir = Path(episode_dir)
    episode = {}
    episode_path = episode_dir / "episode.json"
    if episode_path.is_file():
        episode = json.loads(episode_path.read_text(encoding="utf-8"))
    mavlink = _jsonl(episode_dir / "mavlink.jsonl")
    if not mavlink:
        mavlink = _jsonl(episode_dir / "mavlink.jsonl.gz")
    steps = _jsonl(episode_dir / "steps.jsonl")
    local_altitudes: list[float] = []
    vtol_states: list[int] = []
    airspeeds: list[float] = []
    rtl_entered = False
    landed = False
    airborne = False
    horizontal: list[float] = []
    aligned: list[bool] = []
    for row in mavlink:
        kind = str(row.get("message_type", row.get("mavpackettype", ""))).upper()
        if kind == "LOCAL_POSITION_NED":
            x, y = _mav_value(row, "x"), _mav_value(row, "y")
            if x is not None and y is not None:
                try:
                    horizontal.append(math.hypot(float(x), float(y)))
                except (TypeError, ValueError):
                    pass
            z = _mav_value(row, "z")
            if z is not None:
                try:
                    local_altitudes.append(-float(z))
                except (TypeError, ValueError):
                    pass
        if kind == "EXTENDED_SYS_STATE":
            try:
                state = int(_mav_value(row, "vtol_state"))
                vtol_states.append(state)
            except (TypeError, ValueError):
                pass
            try:
                landed_state = int(_mav_value(row, "landed_state"))
            except (TypeError, ValueError):
                landed_state = None
            # MAV_LANDED_STATE: 1 on ground, 2 in air.  Only a touchdown after
            # flight counts; the aircraft also sits on the ground before takeoff.
            airborne = airborne or landed_state == 2
            landed = landed or (airborne and landed_state == 1)
        if kind == "VFR_HUD":
            try:
                airspeeds.append(float(_mav_value(row, "airspeed")))
            except (TypeError, ValueError):
                pass
        if kind == "HEARTBEAT":
            raw_mode = _mav_value(row, "custom_mode", _mav_value(row, "mode", ""))
            if isinstance(raw_mode, int) and not isinstance(raw_mode, bool):
                # PX4 custom_mode: main mode in bits 16-23, sub mode in 24-31; AUTO.RTL = (4, 5).
                rtl_entered = rtl_entered or ((raw_mode >> 16) & 0xFF, (raw_mode >> 24) & 0xFF) == (
                    4,
                    5,
                )
            else:
                mode = str(raw_mode).upper()
                rtl_entered = rtl_entered or "RTL" in mode or "RETURN" in mode
        if kind == "STATUSTEXT":
            # A short RTL (aircraft already near home) can fall between recorded
            # heartbeats; PX4 announces it as "RTL: start return ...".
            rtl_entered = rtl_entered or str(_mav_value(row, "text", "")).startswith("RTL:")
        if kind == "collector_landing_complete":
            state = row.get("flight_state", {})
            landed = landed or state.get("landed_state") == 1 or row.get("status") == "landed"
        stamp = row.get("frame_sensor_stamp_ns")
        pose_stamp = row.get("pose_sim_stamp_ns")
        skew = row.get("sim_skew_ms")
        if stamp is not None and pose_stamp is not None:
            aligned.append(
                int(stamp) >= int(pose_stamp) and (skew is None or abs(float(skew)) <= 100.0)
            )
    for row in steps:
        after = row.get("obs_after") or {}
        if "frame_sensor_stamp_ns" in after and "pose_sim_stamp_ns" in after:
            aligned.append(int(after["frame_sensor_stamp_ns"]) >= int(after["pose_sim_stamp_ns"]))
    takeoff_success = bool(
        local_altitudes and max(local_altitudes) >= max(0.5, target_altitude_m * 0.8)
    )
    if profile.category == "vtol":
        # A pre-takeoff MC sample is always present.  Count the complete VTOL
        # route only when FW was observed before a later return to MC.
        fw_state_index = next(
            (index for index, state in enumerate(vtol_states) if state in {4, 5}),
            None,
        )
        transition_success = fw_state_index is not None and any(
            state in {3, 6} for state in vtol_states[fw_state_index + 1 :]
        )
    else:
        transition_success = True
    takeoff_method = str(episode.get("takeoff_method", profile.takeoff.methods[0]))
    launch_texts = [
        str(_mav_value(row, "text", ""))
        for row in mavlink
        if str(row.get("message_type", row.get("mavpackettype", ""))).upper() == "STATUSTEXT"
    ]
    launch_detected = any(
        "detect" in text.lower() and ("launch" in text.lower() or "takeoff" in text.lower())
        for text in launch_texts
    )
    launch_required = takeoff_method in {"hand", "catapult"}
    loiter_speeds: list[float] = []
    for row in steps:
        action = row.get("action") or {}
        if str(action.get("kind", "")) not in {"hold", "orbit"}:
            continue
        after = row.get("obs_after") or {}
        value = after.get("airspeed_mps")
        if value is None:
            value = (after.get("flight_state") or {}).get("airspeed_mps")
        try:
            if value is not None:
                loiter_speeds.append(float(value))
        except (TypeError, ValueError):
            pass
    measured_speeds = loiter_speeds or airspeeds
    # The low side is the stall margin and stays strict; right after a transition
    # or turn the measured airspeed can briefly overshoot the profile maximum.
    in_range = bool(measured_speeds) and all(
        profile.min_airspeed_mps <= speed <= profile.max_airspeed_mps * AIRSPEED_OVERSHOOT_ALLOWANCE
        for speed in measured_speeds
    )
    reposition_errors: list[float] = []
    for row in steps:
        action = row.get("action") or {}
        kind = str(action.get("kind", ""))
        if kind not in {"translate", "reposition", "turn"}:
            continue
        outcome = row.get("outcome") or {}
        error = _difference_norm(
            outcome.get("commanded_delta_ned"), outcome.get("achieved_delta_ned")
        )
        if error is not None:
            reposition_errors.append(error)
    reposition_success = bool(reposition_errors) and position_error_m is not None and position_error_m > 0
    if reposition_success:
        reposition_success = max(reposition_errors) <= position_error_m
    video = episode.get("video") or {}
    camera_frames = int(video.get("frames_written", video.get("frame_count", 0)) or 0)
    if not camera_frames:
        camera_frames = sum(1 for row in mavlink if row.get("message_type") == "video_frame")
    if not camera_frames:
        camera_frames = sum(
            1
            for row in steps
            if isinstance(row.get("obs_after"), dict)
            and row["obs_after"].get("frame_sensor_stamp_ns") is not None
        )
    failure = episode.get("failure")
    gimbal = episode.get("gimbal_smoke")
    gimbal_enabled = profile.camera.mount == "gimbal"
    delta_metrics = (
        _delta_fw_metrics(
            mavlink,
            steps,
            profile,
            episode_dir,
            takeoff_altitude_m=float(episode.get("takeoff_altitude_m", target_altitude_m)),
        )
        if profile.id in DELTA_TAILSITTER_PROFILE_IDS
        else {}
    )
    return {
        "takeoff_success": takeoff_success,
        "launch_detected": launch_detected if launch_required else True,
        "launch_climb_success": (launch_detected and takeoff_success) if launch_required else True,
        "launch_status_texts": launch_texts,
        "transition_success": transition_success,
        "loiter_airspeed_in_range": in_range,
        "loiter_airspeed_mps": measured_speeds,
        # Guided fixed-wing commands return on PX4's ACK, so the per-step deltas
        # are short-horizon samples, not route accuracy.
        "reposition_error_m": reposition_errors,
        # 仅供参考：上面的误差是 ACK 后的短时采样，不是航线精度，不能作为通过条件。
        "reposition_success": reposition_success,
        "max_distance_from_origin_m": max(horizontal) if horizontal else None,
        "stayed_in_area": bool(horizontal) and max(horizontal) <= MAX_SMOKE_RANGE_M,
        "rtl_entered": rtl_entered,
        "rtl_success": rtl_entered,
        "landed": landed,
        "camera_frames": camera_frames,
        "frames_aligned": bool(aligned) and all(aligned),
        "gimbal_command_success": (gimbal is not None and gimbal.get("status") == "reached") if gimbal_enabled else True,
        "gimbal_error_deg": (gimbal or {}).get("error_deg") if gimbal_enabled else None,
        "failure_reason": failure,
        **delta_metrics,
    }


ALIASES = {
    "advanced_plane_mono_cam": "advanced_plane",
    "quadtailsitter_mono_cam": "quadtailsitter",
    "delta_tailsitter_mono_cam": "delta_tailsitter",
    "delta_tailsitter_gimbal_cam": "delta_tailsitter_gimbal",
    "tiltrotor_mono_cam": "tiltrotor",
    "standard_vtol_mono_cam": "standard_vtol",
    "rc_cessna_mono_cam": "rc_cessna_runway",
}
SCENARIO_ALIASES = {
    "advanced_plane_mono_cam": "advanced_plane_mono_cam_001",
    "quadtailsitter_mono_cam": "quadtailsitter_mono_cam_001",
    "delta_tailsitter": "delta_tailsitter_mono_cam_001",
    "delta_tailsitter_mono_cam": "delta_tailsitter_mono_cam_001",
    "delta_tailsitter_gimbal_cam": "delta_tailsitter_gimbal_cam_001",
    "tiltrotor_mono_cam": "tiltrotor_mono_cam_001",
    "standard_vtol_mono_cam": "river_rescue_vtol_001",
    "rc_cessna_mono_cam": "coastal_patrol_fixed_wing_001",
}
DEFAULT_TAKEOFF_ALTITUDE_M = 5.0
# The smoke route spans a few hundred metres; a guided command sent with wrong
# coordinates sends the aircraft far beyond this.
MAX_SMOKE_RANGE_M = 1500.0
AIRSPEED_OVERSHOOT_ALLOWANCE = 1.10


def profile_for(airframe: str, takeoff_method: str | None = None) -> AirframeProfile:
    key = airframe.strip().lower().replace("-", "_")
    name = ALIASES.get(key, key)
    if key == "rc_cessna_mono_cam" and takeoff_method in {"hand", "catapult"}:
        name = f"rc_cessna_{takeoff_method}"
    path = ROOT / "configs" / "airframes" / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"未找到 airframe 档案: {airframe} ({path})")
    return AirframeProfile.from_json(path)


def default_scenario(airframe: str) -> Path | None:
    stem = SCENARIO_ALIASES.get(airframe.strip().lower().replace("-", "_"))
    if not stem:
        return None
    directory = ROOT / "configs" / "scenarios" / "airframes"
    candidate = directory / f"{stem}.scenario.json"
    return (
        candidate
        if candidate.is_file()
        else ROOT / "configs" / "scenarios" / f"{stem}.scenario.json"
    )


def build_plan(
    airframe: str, profile: AirframeProfile, scenario: Path, takeoff_method: str | None
) -> dict[str, Any]:
    methods = list(profile.takeoff.methods)
    selected = takeoff_method or ("vtol" if profile.category == "vtol" else methods[0])
    if selected not in methods and not (profile.category == "vtol" and selected == "runway"):
        raise ValueError(f"{airframe} 不支持起飞方式 {selected}; 可选: {', '.join(methods)}")
    transition = profile.category == "vtol"
    delta_strict = profile.id in DELTA_TAILSITTER_PROFILE_IDS
    launch_note = None
    if selected in {"hand", "catapult"}:
        launch_note = (
            "需要在 PX4 SITL/Gazebo 施加真实初速度/冲量并触发 FW_LAUN_DETCN_"
            "ON；未核实前不得宣称手抛/弹射成功"
        )
    if selected in {"hand", "catapult"}:
        launch_note = (
            "PX4 accepts takeoff, Gazebo sets body-forward velocity, and both PX4 launch detection "
            "and climb must be observed."
        )
    steps = [
        {"stage": "preflight", "action": "GCS heartbeat + prearm + arm", "timeout_s": 45},
        {"stage": "takeoff", "action": selected, "mav_cmd": 84 if selected == "vtol" else 22},
    ]
    gimbal_enabled = profile.camera.mount == "gimbal"
    if gimbal_enabled:
        steps.append(
            {
                "stage": "gimbal_check",
                "action": "DO_GIMBAL_MANAGER_CONFIGURE then DO_GIMBAL_MANAGER_PITCHYAW pitch=-45 yaw=0; verify manager ownership, GIMBAL_DEVICE_ATTITUDE_STATUS, and joint angles",
                "tolerance_deg": 4.0,
            }
        )
    if transition:
        steps.append({"stage": "transition_fw", "action": "MC -> FW", "mav_cmd": 3000, "param1": 4})
    steps.extend(
        [
            {
                "stage": "loiter",
                "action": "orbit",
                "mav_cmd": 34,
                "airspeed_mps": [profile.min_airspeed_mps, profile.max_airspeed_mps],
                **({"minimum_fw_duration_s": 120, "minimum_orbit_degrees": 360} if delta_strict else {}),
            },
            {
                "stage": "reposition",
                "action": "translate to one target and wait for arrival" if delta_strict else "translate x3",
                "mav_cmd": 192,
                "position_error_m": 8 if delta_strict else 15,
            },
            {"stage": "rtl", "action": "return to launch", "mav_cmd": 192},
        ]
    )
    if transition:
        steps.append({"stage": "transition_mc", "action": "FW -> MC", "mav_cmd": 3000, "param1": 3})
    steps.append(
        {
            "stage": "land",
            "action": profile.landing.methods[0],
            "mav_cmd": 189 if profile.landing.methods[0] == "vtol" else 21,
        }
    )
    return {
        "schema": "multidetect.sim-airframe-smoke-v1",
        "status": "dry_run",
        "airframe": airframe,
        "profile_id": profile.id,
        "category": profile.category,
        "scenario": str(scenario),
        "takeoff_method": selected,
        "launch_note": launch_note,
        "steps": steps,
        "frame_alignment": {
            "rule": "sensor_stamp_ns >= LOCAL_POSITION_NED.time_boot_ms * 1e6",
            "max_age_s": 0.10 if profile.category == "fixed_wing" else 0.25,
            "reason": "固定翼速度更高，使用更窄的帧时效门限；门限仍由实际锁步吞吐核验",
        },
        "pass_fields": [
            "takeoff",
            "launch_detected",
            "launch_climb_success",
            "transition",
            "loiter_airspeed",
            "reposition_error",
            "rtl",
            "landed_state",
            "frames_aligned",
        ]
        + (["gimbal_command_success"] if gimbal_enabled else [])
        + (
            [
                "fw_sustain_success",
                "fw_reposition_success",
                "fw_metrics_pass",
                "post_transition_climb_success",
                "self_occlusion_success",
            ]
            if delta_strict
            else []
        ),
        "judgement": {
            "takeoff_success": None,
            "launch_detected": None if selected in {"hand", "catapult"} else True,
            "launch_climb_success": None if selected in {"hand", "catapult"} else True,
            "transition_success": None if transition else True,
            "loiter_airspeed_in_range": None,
            "reposition_error_m": None,
            "rtl_success": None,
            "landed": None,
            "camera_frames": None,
            "frames_aligned": None,
            "gimbal_command_success": None if gimbal_enabled else True,
            "gimbal_error_deg": None,
            "failure_reason": None,
            **(
                {
                    "fw_sustain_success": None,
                    "fw_reposition_success": None,
                    "fw_metrics_pass": None,
                    "post_transition_climb_m": None,
                    "post_transition_climb_success": None,
                    "self_occlusion_fraction": None,
                    "self_occlusion_success": None,
                    "fw_airspeed_p5_mps": None,
                    "fw_airspeed_p95_mps": None,
                    "fw_bank_abs_p95_deg": None,
                    "fw_pitch_p5_deg": None,
                    "fw_pitch_p95_deg": None,
                    "fw_height_error_p95_m": None,
                    "fw_motor_saturation_fraction": None,
                    "fw_motor_output_saturation_fraction": None,
                    "fw_motor_saturation_fraction_by_motor": None,
                    "fw_sample_counts": None,
                    "fw_thresholds": None,
                    "self_occlusion_frame_count": None,
                    "self_occlusion_takeoff_time_s": None,
                }
                if delta_strict
                else {}
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airframe", required=True)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument(
        "--takeoff-method", choices=("runway", "hand", "catapult", "vtol", "multirotor")
    )
    parser.add_argument("--out", type=Path, default=ROOT / ".tmp" / "smoke")
    parser.add_argument("--container-prefix", default="md-smoke-airframe")
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    profile = profile_for(args.airframe, args.takeoff_method)
    scenario = args.scenario or default_scenario(args.airframe)
    if scenario is None:
        raise SystemExit("该 airframe 没有默认场景，请提供 --scenario")
    scenario = scenario.resolve()
    if not scenario.is_file():
        raise SystemExit(f"场景不存在: {scenario}")
    plan = build_plan(args.airframe, profile, scenario, args.takeoff_method)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    collector = ROOT / "scripts" / "collect_sim_teacher_episodes.py"
    command = [
        sys.executable,
        str(collector),
        "--scenario",
        str(scenario),
        "--airframe",
        args.airframe,
        "--teacher",
        "explore",
        "--episodes",
        "1",
        "--steps-per-episode",
        "8",
        "--smoke-flight",
        "--out",
        str(args.out),
        "--container-prefix",
        args.container_prefix,
        "--control-port",
        str(args.control_port),
        "--telemetry-port",
        str(args.telemetry_port),
    ]
    if plan["takeoff_method"] in {"hand", "catapult"}:
        command.extend(["--takeoff-method", plan["takeoff_method"]])
    child: subprocess.Popen[str] | None = None

    def _forward_termination(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    previous_term = signal.signal(signal.SIGTERM, _forward_termination)
    previous_int = signal.signal(signal.SIGINT, _forward_termination)
    try:
        child = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = child.communicate()
        completed = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    # 结果里只留尾部；完整采集器输出落盘，PX4 控制台刷屏时仍能找到帧服务等诊断。
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "collector.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (args.out / "collector.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    result = dict(plan)
    result["status"] = "pass" if completed.returncode == 0 else "fail"
    result["collector_returncode"] = completed.returncode
    result["failure_reason"] = (
        None if completed.returncode == 0 else (completed.stderr or completed.stdout)[-2000:]
    )
    result["collector_stdout_tail"] = completed.stdout[-2000:]
    result["judgement"] = dict(plan["judgement"])
    episodes = sorted(args.out.rglob("episode.json"), key=lambda path: path.stat().st_mtime)
    if episodes:
        reposition_tolerance = next(
            (
                float(step["position_error_m"])
                for step in plan["steps"]
                if step.get("stage") == "reposition"
                and isinstance(step.get("position_error_m"), (int, float))
            ),
            None,
        )
        parsed = evaluate_episode(
            episodes[-1].parent,
            profile,
            DEFAULT_TAKEOFF_ALTITUDE_M,
            reposition_tolerance,
        )
        result["judgement"].update(parsed)
        required = (
            "takeoff_success",
            "transition_success",
            "loiter_airspeed_in_range",
            "rtl_entered",
            "landed",
            "frames_aligned",
            "stayed_in_area",
        )
        if plan["takeoff_method"] in {"hand", "catapult"}:
            required += ("launch_detected", "launch_climb_success")
        if profile.camera.mount == "gimbal":
            required += ("gimbal_command_success",)
        if profile.id in DELTA_TAILSITTER_PROFILE_IDS:
            required += (
                "fw_sustain_success",
                "fw_reposition_success",
                "fw_metrics_pass",
                "post_transition_climb_success",
                "self_occlusion_success",
            )
        result["status"] = (
            "pass"
            if completed.returncode == 0
            and all(result["judgement"].get(field) is True for field in required)
            else "fail"
        )
    else:
        result["status"] = "fail"
        result["judgement"]["failure_reason"] = "collector produced no episode.json artifact"
    if completed.returncode != 0:
        result["judgement"]["failure_reason"] = result["failure_reason"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        completed.returncode
        if completed.returncode != 0
        else (0 if result["status"] == "pass" else 1)
    )


if __name__ == "__main__":
    raise SystemExit(main())
