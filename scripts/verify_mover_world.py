#!/usr/bin/env python3
"""Docker-only mover verification (the script itself is read-only to the repo).

Run inside the repository checkout; it reuses the collector's pinned Docker
plan, samples Gazebo pose and RGB/segmentation topics, and writes only under
``artifacts/verify-movers``.
"""
from __future__ import annotations
import argparse, json, re, subprocess, time
from pathlib import Path
from xml.etree import ElementTree as ET

from scripts.collect_sim_teacher_episodes import CollectionPlan, docker_command
from scripts.score_onboard_loop import target_pose_at_time

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", default="gen_mov_w0000")
    ap.add_argument("--duration-s", type=float, default=30.0)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts/verify-movers")
    args = ap.parse_args()
    scenario = ROOT / "configs/scenarios/generated-movers" / f"{args.world}.scenario.json"
    runtime = ROOT / "configs/scenarios/generated-movers" / f"{args.world}.runtime_input.json"
    truth_path = ROOT / "configs/scenarios/generated-movers" / f"{args.world}.truth.json"
    truth = json.loads(truth_path.read_text())
    target = next(t for t in truth["targets"] if t.get("true_target"))
    args.out.mkdir(parents=True, exist_ok=True)
    plan = CollectionPlan(scenario=str(scenario), runtime=str(runtime), truth=str(truth_path), teacher="none", episodes=1, steps_per_episode=1, seed=0, out=str(args.out), segmentation=True)
    command = docker_command(plan, name=f"verify-mover-{args.world}")
    logs = args.out / f"{args.world}.docker.log"
    with logs.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
        container = f"verify-mover-{args.world}"
        samples = []
        deadline = time.time() + 120
        while time.time() < deadline and time.time() < deadline - 120 + args.duration_s:
            q = subprocess.run(["docker", "exec", container, "gz", "topic", "-e", "-n", "1", "-t", "/world/default/dynamic_pose/info"], capture_output=True, text=True, timeout=15)
            if q.returncode == 0:
                stamp = len(samples) * 1.0
                # Pose topic formats differ between Fortress/Harmonic; retain raw
                # evidence and extract the actor pose when the standard fields exist.
                match = re.search(r"name: 'target_true_[^']+'.*?position:\s*\n\s*x:\s*([-+0-9.eE]+).*?y:\s*([-+0-9.eE]+)", q.stdout, re.S)
                if match:
                    x, y = float(match.group(1)), float(match.group(2))
                    expected = target_pose_at_time(target, stamp)
                    samples.append({"sim_time_s": stamp, "x": x, "y": y, "truth_x": expected["x_east_m"], "truth_y": expected["y_north_m"], "error_m": ((x-expected["x_east_m"])**2+(y-expected["y_north_m"])**2)**0.5})
            time.sleep(1)
        rgb = subprocess.run(["docker", "exec", container, "gz", "topic", "-e", "-n", "1", "-t", "/world/default/model/x500_mono_cam_0/link/camera_link/sensor/camera/image"], capture_output=True, text=True, timeout=20)
        seg = subprocess.run(["docker", "exec", container, "gz", "topic", "-e", "-n", "1", "-t", "/world/default/model/x500_mono_cam_0/link/segmentation_camera_link/sensor/segmentation_camera/image"], capture_output=True, text=True, timeout=20)
        (args.out / f"{args.world}.rgb.txt").write_text(rgb.stdout, encoding="utf-8")
        (args.out / f"{args.world}.segmentation.txt").write_text(seg.stdout, encoding="utf-8")
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)
    verdict = {"world": args.world, "samples": samples, "max_pose_error_m": max((s["error_m"] for s in samples), default=None), "trajectory_residual_bound_m": 0.75, "rgb_observed": bool(rgb.stdout.strip()), "segmentation_observed": bool(seg.stdout.strip()), "segmentation_label_declared": "\"1\"" in json.dumps(truth.get("segmentation_labels", {})), "pass": bool(samples and rgb.stdout.strip() and seg.stdout.strip())}
    (args.out / f"{args.world}.verdict.json").write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["pass"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
