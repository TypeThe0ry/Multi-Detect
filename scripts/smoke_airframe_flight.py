#!/usr/bin/env python3
"""固定翼/VTOL 仿真冒烟飞行计划与执行器。

默认执行完整采集器 episode；``--dry-run`` 只解析档案和场景并输出可审计
的步骤/命令 JSON，不启动 Docker。手抛和弹射不会伪造成功，结果会明确标记
需要 Gazebo 外部冲量。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.airframe_profile import AirframeProfile  # noqa: E402

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
        "pass_fields": ["takeoff", "transition", "loiter_airspeed", "reposition_error", "rtl", "landed_state", "frames_aligned"],
        "judgement": {
            "takeoff_success": None,
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
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    result = dict(plan)
    result["status"] = "pass" if completed.returncode == 0 else "fail"
    result["collector_returncode"] = completed.returncode
    result["failure_reason"] = None if completed.returncode == 0 else (completed.stderr or completed.stdout)[-2000:]
    result["collector_stdout_tail"] = completed.stdout[-2000:]
    result["judgement"] = dict(plan["judgement"])
    result["judgement"]["takeoff_success"] = completed.returncode == 0
    result["judgement"]["landed"] = completed.returncode == 0
    result["judgement"]["frames_aligned"] = completed.returncode == 0
    if completed.returncode != 0:
        result["judgement"]["failure_reason"] = result["failure_reason"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
