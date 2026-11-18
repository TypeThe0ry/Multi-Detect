#!/usr/bin/env python3
"""CPU-only telemetry replay; setpoint FK is a model, never replacement feedback."""
from __future__ import annotations

import argparse
import bisect
import importlib.util
import json
import logging
import math
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path[:0] = [str(Path(__file__).resolve().parents[2]),
                    str(Path(__file__).resolve().parents[2] / "src")]

from multidetect.camera_mount import _mat_mul  # noqa: E402
from multidetect.sim_teacher.horizon_stabilization import (  # noqa: E402
    AXES, HorizonStabilizer, c11_rotation_deg, horizon_error_deg, joint_command_quaternion,
    rotation_deg,
)
from scripts.realworld.horizon_report import LEGACY_LIMITS, classify_frame, percentile  # noqa: E402


def stats(values):
    return {"count": len(values), "mean_deg": sum(values)/len(values) if values else None,
            "p50_deg": percentile(values, .5), "p95_deg": percentile(values, .95),
            "max_deg": max(values) if values else None}


def load_episode(path):
    frames = [json.loads(line) for line in (path / "video/manifest.jsonl").open()]
    frames = [r for r in frames if "frame_index" in r]
    attitudes = []
    with (path / "mavlink.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("message_type") == "ATTITUDE":
                attitudes.append((row["time_boot_ms"]/1000,
                                  {a: math.degrees(row[a]) for a in AXES}))
    # Receive order can differ slightly from source order. Equal source times
    # denote the same sample; bisect deliberately uses the last one.
    attitudes.sort(key=lambda r: r[0])
    return frames, attitudes


def replay(frames, attitudes, *, controller=HorizonStabilizer, delay_s=0.0):
    """50 Hz, zero-order-held source ATTITUDE and ideal emitted-setpoint tracking.

    Initialize from actual joint readback. Recompute every original measured
    camera FK as a separate invariant. No servo/bridge model is inferred from
    the sparse recorded video readback; this cannot establish flight quality.
    """
    first, last = frames[0]["sensor_stamp_ns"]/1e9, frames[-1]["sensor_stamp_ns"]/1e9
    times = [t for t, _ in attitudes]
    target = dict(frames[0]["gimbal_state"]["stabilization"]["desired_earth_deg"])
    target["yaw"] = 0.0  # This episode requests yaw follow with zero offset.
    stabilizer = controller(target, limits_deg=LEGACY_LIMITS)
    stabilizer.last_joint = {a: math.degrees(frames[0]["gimbal_state"]["joint_position_rad"][a])
                             for a in AXES}
    sequence, commands, max_step, rate_steps = [], [], 0.0, 0
    for index in range(math.ceil((last-first)/.02)+1):
        now = first + index*.02
        sample_index = bisect.bisect_right(times, now-delay_s)-1
        if sample_index < 0:
            raise ValueError("no ATTITUDE available at replay start")
        stamp, body = attitudes[sample_index]
        previous = dict(stabilizer.last_joint)
        output = stabilizer.update(body, now_s=now, attitude_time_s=stamp)
        # Every emitted value must survive the existing carrier assertions.
        joint_command_quaternion(output["joint_deg"])
        step = max(abs(output["joint_deg"][a]-previous[a]) for a in AXES)
        max_step = max(max_step, step)
        assert step <= 180*.02 + 1e-6
        rate_steps += any("rate_limit" in r for r in output["reasons"])
        sequence.append(now)
        commands.append(output)
    rows = []
    fk_difference = 0.0
    for frame in frames:
        index = max(0, bisect.bisect_right(sequence, frame["sensor_stamp_ns"]/1e9)-1)
        output = commands[index]
        body = {a: frame["attitude"][a+"_deg"] for a in AXES}
        desired = rotation_deg(frame["gimbal_state"]["stabilization"]["desired_earth_deg"])
        measured_joints = {a: math.degrees(frame["gimbal_state"]["joint_position_rad"][a])
                           for a in AXES}
        measured = _mat_mul(rotation_deg(body), c11_rotation_deg(measured_joints))
        recorded = frame["camera_pose"]["optical_to_world"]
        fk_difference = max(fk_difference, max(abs(x-y) for x, y in zip(measured, recorded)))
        error = horizon_error_deg(desired, _mat_mul(rotation_deg(body),
                                                   c11_rotation_deg(output["joint_deg"])))
        rows.append({"frame_index": frame["frame_index"], "model_error_deg": error,
                     "measured_error_deg": horizon_error_deg(desired, measured),
                     "target_pose_feasible": classify_frame(frame)["target_pose_feasible"],
                     "joint_deg": output["joint_deg"], "reasons": output["reasons"]})
    return {"model": "ideal emitted-setpoint tracking; initial joints from real readback; "
                     "50 Hz simulation clock, source ATTITUDE ZOH; no servo or bridge dynamics",
            "delay_s": delay_s, "control_steps": len(commands), "max_step_deg": max_step,
            "rate_limited_steps": rate_steps, "measured_fk_max_matrix_difference": fk_difference,
            "all_model": stats([r["model_error_deg"] for r in rows]),
            "feasible_model": stats([r["model_error_deg"] for r in rows if r["target_pose_feasible"]]),
            "measured": stats([r["measured_error_deg"] for r in rows]), "frames": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--baseline-module", type=Path)
    parser.add_argument("--delay-s", type=float, default=0.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.delay_s) or args.delay_s < 0:
        parser.error("delay must be finite and nonnegative")
    logging.disable(logging.WARNING)
    frames, attitudes = load_episode(args.episode)
    results = {"after": replay(frames, attitudes, delay_s=args.delay_s)}
    if args.baseline_module:
        spec = importlib.util.spec_from_file_location("baseline_horizon", args.baseline_module)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        results["before"] = replay(frames, attitudes, controller=module.HorizonStabilizer,
                                   delay_s=args.delay_s)
    args.output.write_text(json.dumps(results, indent=2)+"\n")
    print(json.dumps({k: {a: b for a, b in v.items() if a != "frames"}
                      for k, v in results.items()}, indent=2))


if __name__ == "__main__":
    main()
