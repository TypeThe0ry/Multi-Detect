#!/usr/bin/env python3
"""固定翼/VTOL 仿真冒烟飞行计划与执行器。

默认执行完整采集器 episode；``--dry-run`` 只解析档案和场景并输出可审计
的步骤/命令 JSON，不启动 Docker。手抛和弹射不会伪造成功，结果会明确标记
需要 Gazebo 外部冲量。
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.airframe_profile import AirframeProfile  # noqa: E402


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


def _norm3(values: Any) -> float | None:
    if not isinstance(values, (list, tuple)) or len(values) < 3:
        return None


def _difference_norm(left: Any, right: Any) -> float | None:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return None
    if len(left) < 3 or len(right) < 3:
        return None
    try:
        return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left[:3], right[:3])))
    except (TypeError, ValueError):
        return None
    try:
        return math.sqrt(sum(float(v) ** 2 for v in values[:3]))
    except (TypeError, ValueError):
        return None


def evaluate_episode(episode_dir: Path, profile: AirframeProfile,
                     target_altitude_m: float = 5.0) -> dict[str, Any]:
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
                rtl_entered = rtl_entered or ((raw_mode >> 16) & 0xFF, (raw_mode >> 24) & 0xFF) == (4, 5)
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
            aligned.append(int(stamp) >= int(pose_stamp) and (skew is None or abs(float(skew)) <= 100.0))
    for row in steps:
        after = row.get("obs_after") or {}
        if "frame_sensor_stamp_ns" in after and "pose_sim_stamp_ns" in after:
            aligned.append(int(after["frame_sensor_stamp_ns"]) >= int(after["pose_sim_stamp_ns"]))
    takeoff_success = bool(local_altitudes and max(local_altitudes) >= max(0.5, target_altitude_m * 0.8))
    if episode.get("takeoff_altitude_m") is not None:
        takeoff_success = takeoff_success or float(episode["takeoff_altitude_m"]) >= target_altitude_m * 0.8
    if profile.category == "vtol":
        transition_success = 4 in vtol_states or 5 in vtol_states
    else:
        transition_success = True
    if profile.category == "vtol" and transition_success:
        transition_success = transition_success and any(s in {3, 6} for s in vtol_states)
    takeoff_method = str(episode.get("takeoff_method", profile.takeoff.methods[0]))
    launch_texts = [str(_mav_value(row, "text", "")) for row in mavlink
                    if str(row.get("message_type", row.get("mavpackettype", ""))).upper() == "STATUSTEXT"]
    launch_detected = any("detect" in text.lower() and
                          ("launch" in text.lower() or "takeoff" in text.lower())
                          for text in launch_texts)
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
    in_range = bool(measured_speeds) and all(
        profile.min_airspeed_mps <= speed <= profile.max_airspeed_mps
        for speed in measured_speeds
    )
    reposition_errors: list[float] = []
    for row in steps:
        action = row.get("action") or {}
        kind = str(action.get("kind", ""))
        if kind not in {"translate", "reposition", "turn"}:
            continue
        outcome = row.get("outcome") or {}
        error = _difference_norm(outcome.get("commanded_delta_ned"),
                                 outcome.get("achieved_delta_ned"))
        if error is not None:
            reposition_errors.append(error)
    video = episode.get("video") or {}
    camera_frames = int(video.get("frames_written", video.get("frame_count", 0)) or 0)
    if not camera_frames:
        camera_frames = sum(1 for row in mavlink if row.get("message_type") == "video_frame")
    if not camera_frames:
        camera_frames = sum(1 for row in steps if isinstance(row.get("obs_after"), dict)
                            and row["obs_after"].get("frame_sensor_stamp_ns") is not None)
    failure = episode.get("failure")
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
        "max_distance_from_origin_m": max(horizontal) if horizontal else None,
        "stayed_in_area": bool(horizontal) and max(horizontal) <= MAX_SMOKE_RANGE_M,
        "rtl_entered": rtl_entered,
        "rtl_success": rtl_entered,
        "landed": landed,
        "camera_frames": camera_frames,
        "frames_aligned": bool(aligned) and all(aligned),
        "failure_reason": failure,
    }

ALIASES = {
    "advanced_plane_mono_cam": "advanced_plane",
    "quadtailsitter_mono_cam": "quadtailsitter",
    "tiltrotor_mono_cam": "tiltrotor",
    "standard_vtol_mono_cam": "standard_vtol",
    "rc_cessna_mono_cam": "rc_cessna_runway",
}
SCENARIO_ALIASES = {
    "advanced_plane_mono_cam": "advanced_plane_mono_cam_001",
    "quadtailsitter_mono_cam": "quadtailsitter_mono_cam_001",
    "tiltrotor_mono_cam": "tiltrotor_mono_cam_001",
    "standard_vtol_mono_cam": "river_rescue_vtol_001",
    "rc_cessna_mono_cam": "coastal_patrol_fixed_wing_001",
}
DEFAULT_TAKEOFF_ALTITUDE_M = 5.0
# The smoke route spans a few hundred metres; a guided command sent with wrong
# coordinates sends the aircraft far beyond this.
MAX_SMOKE_RANGE_M = 1500.0


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
    return candidate if candidate.is_file() else ROOT / "configs" / "scenarios" / f"{stem}.scenario.json"


def build_plan(airframe: str, profile: AirframeProfile, scenario: Path, takeoff_method: str | None) -> dict[str, Any]:
    methods = list(profile.takeoff.methods)
    selected = takeoff_method or ("vtol" if profile.category == "vtol" else methods[0])
    if selected not in methods and not (profile.category == "vtol" and selected == "runway"):
        raise ValueError(f"{airframe} 不支持起飞方式 {selected}; 可选: {', '.join(methods)}")
    transition = profile.category == "vtol"
    launch_note = None
    if selected in {"hand", "catapult"}:
        launch_note = "需要在 PX4 SITL/Gazebo 施加真实初速度/冲量并触发 FW_LAUN_DETCN_ON；未核实前不得宣称手抛/弹射成功"
    if selected in {"hand", "catapult"}:
        launch_note = "PX4 accepts takeoff, Gazebo sets body-forward velocity, and both PX4 launch detection and climb must be observed."
    steps = [
        {"stage": "preflight", "action": "GCS heartbeat + prearm + arm", "timeout_s": 45},
        {"stage": "takeoff", "action": selected, "mav_cmd": 84 if selected == "vtol" else 22},
    ]
    if transition:
        steps.append({"stage": "transition_fw", "action": "MC -> FW", "mav_cmd": 3000, "param1": 4})
    steps.extend([
        {"stage": "loiter", "action": "orbit", "mav_cmd": 34, "airspeed_mps": [profile.min_airspeed_mps, profile.max_airspeed_mps]},
        {"stage": "reposition", "action": "translate x3", "mav_cmd": 192, "position_error_m": 15},
        {"stage": "rtl", "action": "return to launch", "mav_cmd": 192},
    ])
    if transition:
        steps.append({"stage": "transition_mc", "action": "FW -> MC", "mav_cmd": 3000, "param1": 3})
    steps.append({"stage": "land", "action": profile.landing.methods[0], "mav_cmd": 189 if profile.landing.methods[0] == "vtol" else 21})
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
        "pass_fields": ["takeoff", "launch_detected", "launch_climb_success", "transition", "loiter_airspeed", "reposition_error", "rtl", "landed_state", "frames_aligned"],
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
            "failure_reason": None,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--airframe", required=True)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--takeoff-method", choices=("runway", "hand", "catapult", "vtol", "multirotor"))
    parser.add_argument("--out", type=Path, default=ROOT / ".tmp" / "smoke")
    parser.add_argument("--container-prefix", default="md-smoke-airframe")
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    profile = profile_for(args.airframe, args.takeoff_method)
    scenario = (args.scenario or default_scenario(args.airframe))
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
    command = [sys.executable, str(collector), "--scenario", str(scenario), "--airframe", args.airframe,
               "--teacher", "explore", "--episodes", "1", "--steps-per-episode", "8",
               "--smoke-flight",
               "--out", str(args.out), "--container-prefix", args.container_prefix,
               "--control-port", str(args.control_port), "--telemetry-port", str(args.telemetry_port)]
    if plan["takeoff_method"] in {"hand", "catapult"}:
        command.extend(["--takeoff-method", plan["takeoff_method"]])
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    result = dict(plan)
    result["status"] = "pass" if completed.returncode == 0 else "fail"
    result["collector_returncode"] = completed.returncode
    result["failure_reason"] = None if completed.returncode == 0 else (completed.stderr or completed.stdout)[-2000:]
    result["collector_stdout_tail"] = completed.stdout[-2000:]
    result["judgement"] = dict(plan["judgement"])
    episodes = sorted(args.out.rglob("episode.json"), key=lambda path: path.stat().st_mtime)
    if episodes:
        parsed = evaluate_episode(episodes[-1].parent, profile, DEFAULT_TAKEOFF_ALTITUDE_M)
        result["judgement"].update(parsed)
        required = ("takeoff_success", "transition_success", "loiter_airspeed_in_range",
                    "rtl_entered", "landed", "frames_aligned", "stayed_in_area")
        if plan["takeoff_method"] in {"hand", "catapult"}:
            required += ("launch_detected", "launch_climb_success")
        result["status"] = "pass" if completed.returncode == 0 and all(
            result["judgement"].get(field) is True for field in required
        ) else "fail"
    else:
        result["judgement"]["failure_reason"] = "collector produced no episode.json artifact"
    if completed.returncode != 0:
        result["judgement"]["failure_reason"] = result["failure_reason"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return completed.returncode if completed.returncode != 0 else (0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
