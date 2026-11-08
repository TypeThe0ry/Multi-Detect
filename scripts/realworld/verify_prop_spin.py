#!/usr/bin/env python3
"""Spin every airframe's motors in Gazebo without PX4 and prove the props visibly rotate.

Each airframe runs in its own throwaway container on one GPU. A motor command goes to the
same Actuators topic PX4 uses; the report holds rotor link angular rates from dynamic poses
and pixel change in frames from a camera beside the airframe.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "multidetect/px4-sitl-gazebo:gpu"
AIRFRAMES = {
    "x500_mono_cam": {"motors": 4, "cam": (2.0, 1.2, 1.0), "throttle": 0.35},
    "x500_gimbal": {"motors": 4, "cam": (2.0, 1.2, 1.0), "throttle": 0.35},
    "rc_cessna_mono_cam": {"motors": 1, "cam": (2.2, 1.6, 1.0), "throttle": 0.4},
    "standard_vtol_mono_cam": {"motors": 5, "cam": (3.0, 2.0, 1.6), "throttle": 0.3},
    "tiltrotor_mono_cam": {"motors": 4, "cam": (2.6, 1.8, 1.4), "throttle": 0.3},
    "quadtailsitter_mono_cam": {"motors": 4, "cam": (2.2, 1.6, 1.2), "throttle": 0.3},
    "advanced_plane_mono_cam": {"motors": 1, "cam": (3.2, 2.0, 1.4), "throttle": 0.4},
    "delta_tailsitter_mono_cam": {"motors": 4, "cam": (2.2, 1.6, 1.2), "throttle": 0.3},
    "delta_tailsitter_gimbal_cam": {"motors": 4, "cam": (2.2, 1.6, 1.2), "throttle": 0.3},
}


def world(model: str, cam, image_model: bool) -> str:
    cx, cy, cz = cam
    yaw = math.atan2(-cy, -cx)
    pitch = math.atan2(cz - 0.4, math.hypot(cx, cy))
    return f"""<?xml version="1.0"?><sdf version="1.9"><world name="spin">
<physics type="ode"><max_step_size>0.004</max_step_size><real_time_factor>1</real_time_factor></physics>
<scene><ambient>0.7 0.7 0.7 1</ambient><background>0.75 0.8 0.88 1</background></scene>
<light name="sun" type="directional"><pose>0 0 50 0 0 0</pose><direction>-0.4 0.3 -0.8</direction><diffuse>1 1 1 1</diffuse></light>
<model name="ground"><static>true</static><link name="l"><collision name="c"><geometry><plane><normal>0 0 1</normal><size>100 100</size></plane></geometry></collision>
<visual name="v"><geometry><plane><normal>0 0 1</normal><size>100 100</size></plane></geometry><material><diffuse>0.4 0.5 0.35 1</diffuse></material></visual></link></model>
<include><uri>model://{model}</uri><name>af</name><pose>0 0 0.3 0 0 0</pose></include>
<model name="cam"><static>true</static><pose>{cx} {cy} {cz} 0 {pitch:.4f} {yaw:.4f}</pose><link name="l"><sensor name="c" type="camera"><topic>spin/cam</topic><update_rate>30</update_rate><always_on>1</always_on>
<camera><horizontal_fov>0.9</horizontal_fov><image><width>640</width><height>480</height></image><clip><near>0.05</near><far>200</far></clip></camera></sensor></link></model>
</world></sdf>"""


SCRIPT = r"""
source /opt/ros/*/setup.bash 2>/dev/null
export GZ_IP=127.0.0.1
gz sim -s -r --headless-rendering --render-engine ogre2 /w/world.sdf > /out/server.log 2>&1 &
SP=$!
sleep 25
gz topic -l > /out/topics.txt 2>&1
(timeout 60 gz topic -e --json-output -n 4 -t spin/cam > /out/idle.json 2>/dev/null)
T=$(grep -E 'command/motor_speed$' /out/topics.txt | head -1)
echo "motor topic: $T" > /out/motor.txt
python3 - "$THROTTLE_VEL" > /out/cmd.txt <<'PY'
import sys
print("velocity:[" + ",".join(sys.argv[1].split()) + "]")
PY
(for i in $(seq 1 400); do gz topic -t "$T" --msgtype gz.msgs.Actuators -p "$(cat /out/cmd.txt)" >/dev/null 2>&1; sleep 0.2; done) &
CP=$!
sleep 12
(timeout 60 gz topic -e --json-output -n 8 -t spin/cam > /out/spin.json 2>/dev/null)
(timeout 30 gz topic -e --json-output -n 40 -t /world/spin/dynamic_pose/info > /out/poses.json 2>/dev/null)
kill $CP 2>/dev/null; kill -INT $SP; sleep 2; kill -9 $SP 2>/dev/null; true
"""


def frames(path: Path) -> list[np.ndarray]:
    out = []
    for line in path.read_text().splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = base64.b64decode(m["data"])
        out.append(np.frombuffer(data, np.uint8).reshape(int(m["height"]), int(m["width"]), -1)[..., :3])
    return out


def rotor_rates(path: Path) -> dict[str, float]:
    samples: dict[str, list[tuple[float, np.ndarray]]] = {}
    for line in path.read_text().splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        stamp = m["header"]["stamp"]
        t = float(stamp.get("sec", 0)) + float(stamp.get("nsec", 0)) * 1e-9
        for p in m.get("pose", []):
            if "rotor" in p.get("name", ""):
                o = p.get("orientation", {})
                samples.setdefault(p["name"], []).append((t, np.array([o.get("w", 1.0), o.get("x", 0.0), o.get("y", 0.0), o.get("z", 0.0)])))
    rates = {}
    for name, rows in samples.items():
        vals = []
        for (t0, q0), (t1, q1) in zip(rows, rows[1:]):
            if t1 <= t0:
                continue
            dot = min(1.0, abs(float(np.dot(q0, q1))))
            vals.append(2 * math.acos(dot) / (t1 - t0))
        rates[name] = float(np.median(vals)) if vals else 0.0
    return rates


def run(name: str, spec: dict, out: Path, gpu: str, model_root: Path, max_rot: float) -> dict:
    d = out / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    (d / "world.sdf").write_text(world(name, spec["cam"], spec.get("image_model", False)))
    (d / "run.sh").write_text(SCRIPT)
    vel = " ".join([f"{spec['throttle'] * max_rot:.0f}"] * spec["motors"])
    cname = f"rw-spin-{name.replace('_', '-')}"
    subprocess.run(["docker", "rm", "-f", cname], capture_output=True)
    cmd = ["docker", "run", "--rm", "--name", cname, "--gpus", f"device={gpu}", "-e", f"THROTTLE_VEL={vel}",
           "-e", "GZ_SIM_RESOURCE_PATH=/opt/m:/opt/px4-gazebo/share/gz/models",
           "-v", f"{model_root}:/opt/m:ro", "-v", f"{d}:/w:ro", "-v", f"{d}:/out", "--entrypoint", "/bin/bash", IMAGE, "/w/run.sh"]
    subprocess.run(cmd, capture_output=True, timeout=240)
    report = {"airframe": name, "motors_expected": spec["motors"], "motor_topic": (d / "motor.txt").read_text().strip() if (d / "motor.txt").exists() else None}
    try:
        idle, spin = frames(d / "idle.json"), frames(d / "spin.json")
        report["frames"] = [len(idle), len(spin)]
        report["idle_pixel_change"] = float(np.mean(np.abs(idle[0].astype(float) - idle[-1].astype(float)))) if len(idle) > 1 else None
        report["spin_pixel_change"] = float(np.mean(np.abs(spin[0].astype(float) - spin[-1].astype(float)))) if len(spin) > 1 else None
        report["spin_changed_px"] = int((np.abs(spin[0].astype(int) - spin[2].astype(int)).max(axis=2) > 25).sum()) if len(spin) > 2 else 0
        report["idle_changed_px"] = int((np.abs(idle[0].astype(int) - idle[2].astype(int)).max(axis=2) > 25).sum()) if len(idle) > 2 else 0
        if spin:
            sheet = np.concatenate(spin[:3], axis=1)
            Image.fromarray(sheet).save(d / "spin_sheet.jpg", quality=88)
    except (OSError, KeyError, ValueError) as exc:
        report["frame_error"] = repr(exc)
    report["rotor_rad_s"] = rotor_rates(d / "poses.json") if (d / "poses.json").exists() else {}
    moving = [k for k, v in report["rotor_rad_s"].items() if v > 5]
    report["rotors_spinning"] = len(moving)
    report["pass"] = len(moving) >= spec["motors"] and report.get("spin_changed_px", 0) > report.get("idle_changed_px", 0)
    for f in ("idle.json", "spin.json", "poses.json"):
        (d / f).unlink(missing_ok=True)
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airframes", nargs="*", default=list(AIRFRAMES))
    ap.add_argument("--gpu", default="1")
    ap.add_argument("--out", type=Path, default=ROOT / ".tmp/prop-spin")
    ap.add_argument("--max-rot", type=float, default=1000.0)
    ap.add_argument("--model-root", type=Path, default=ROOT / "simulation/gazebo/models")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in a.airframes:
        r = run(name, AIRFRAMES[name], a.out.resolve(), a.gpu, a.model_root.resolve(), a.max_rot)
        print(json.dumps(r, ensure_ascii=False), flush=True)
        rows.append(r)
    (a.out / "report.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
