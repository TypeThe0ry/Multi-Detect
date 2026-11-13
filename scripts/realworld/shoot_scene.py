#!/usr/bin/env python3
"""Render still cameras in a world, for visual review. One throwaway container on one GPU."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "multidetect/px4-sitl-gazebo:gpu"
RESOLVED = (ROOT / "artifacts/cache/gz-fuel/resolved").resolve()


def sample_gpu_memory(container: str, gpu: str, stop: threading.Event, rows: list) -> None:
    """Count only processes belonging to this review container, including graphics."""
    while not stop.wait(2):
        try:
            processes = subprocess.run(["docker", "top", container, "-eo", "pid"],
                                       capture_output=True, text=True, timeout=5)
            owned = {line.strip() for line in processes.stdout.splitlines() if line.strip().isdigit()}
            if not owned:
                continue
            report = subprocess.run(["nvidia-smi", "-i", gpu, "-q", "-x"],
                                    capture_output=True, text=True, timeout=5)
            memory = []
            for proc in ET.fromstring(report.stdout).findall(".//process_info"):
                if proc.findtext("pid") in owned:
                    value = proc.findtext("used_memory", "")
                    if value.endswith(" MiB"):
                        memory.append(float(value.split()[0]))
            if memory:
                rows.append({"wall": time.time(), "used_mib": sum(memory)})
        except (OSError, subprocess.TimeoutExpired, ET.ParseError, ValueError):
            continue


def cam_model(name: str, x, y, z, tx, ty, tz, w=1280, h=960, fov=1.3) -> str:
    dx, dy, dz = tx - x, ty - y, tz - z
    yaw = math.atan2(dy, dx)
    pitch = -math.atan2(dz, math.hypot(dx, dy))
    return (f'<model name="shotcam_{name}"><static>true</static><pose>{x} {y} {z} 0 {pitch:.4f} {yaw:.4f}</pose><link name="l">'
            f'<sensor name="c" type="camera"><topic>shot/{name}</topic><update_rate>1</update_rate><always_on>1</always_on>'
            f'<camera><horizontal_fov>{fov}</horizontal_fov><image><width>{w}</width><height>{h}</height></image><clip><near>0.2</near><far>9000</far></clip></camera></sensor></link></model>')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("world", type=Path)
    ap.add_argument("--gpu", default="1")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--cam", action="append", help="name:x,y,z:tx,ty,tz", required=True)
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--z-from", help="site name; camera z values are metres above that site's ground")
    a = ap.parse_args()
    if a.gpu not in {"0", "1"}:
        ap.error("realworld review permits only GPU0 or GPU1")
    a.out = a.out.resolve()
    shutil.rmtree(a.out, ignore_errors=True)
    a.out.mkdir(parents=True)
    text = a.world.read_text()
    names = []
    cams = []
    terrain = None
    if a.z_from:
        from build_scene import Terrain, CFG, ASSET_ROOT, SCENE_ROOT
        scene = json.loads((SCENE_ROOT / "sites" / a.z_from / "scene.json").read_text())
        terrain = Terrain(CFG["sites"][a.z_from], Path(scene.get("raw_dir", ASSET_ROOT / "sites" / a.z_from / "raw")))
    for spec in a.cam:
        n, p, t = spec.split(":")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) or n in names:
            ap.error("camera names must be unique shell-safe identifiers")
        position, target = list(map(float, p.split(","))), list(map(float, t.split(",")))
        if terrain is not None:
            position[2] += terrain.z(*position[:2])
            target[2] += terrain.z(*target[:2])
        names.append(n)
        cams.append(cam_model(n, *position, *target))
    text = text.replace("</world>", "".join(cams) + "</world>")
    (a.out / "world.sdf").write_text(text)
    wait = " ".join(names)
    script = f"""
source /opt/ros/*/setup.bash 2>/dev/null
export GZ_IP=127.0.0.1
T0=$(date +%s)
gz sim -s -r --headless-rendering --render-engine ogre2 /w/world.sdf > /w/server.log 2>&1 &
SP=$!
SUBS=""
for n in {wait}; do
  timeout {a.timeout - 20} gz topic -e --json-output -n {a.frames} -t shot/$n > /w/$n.json 2>/dev/null &
  SUBS="$SUBS $!"
done
for i in $(seq 1 {a.timeout}); do
  c=0
  for n in {wait}; do
    rows=$(wc -l < /w/$n.json)
    if [ "$rows" -ge 1 ] && [ ! -f /w/$n.first_s ]; then
      echo "$(( $(date +%s) - T0 ))" > /w/$n.first_s
    fi
    [ "$rows" -ge {a.frames} ] && c=$((c+1))
  done
  [ $c = {len(names)} ] && break
  kill -0 $SP 2>/dev/null || break
  sleep 1
done
echo "all requested frames after $(( $(date +%s) - T0 )) s" > /w/time.txt
# Let shader compilation and initial uploads settle before sampling RTF.
sleep 10
# Both commands are bounded; gz stats -p is a continuous stream.
timeout 35 gz topic -e --json-output -n 200 -t /world/default/stats > /w/stats.json 2>/dev/null
kill -INT $SP 2>/dev/null
for pid in $SUBS; do kill -TERM $pid 2>/dev/null; done
for i in $(seq 1 10); do kill -0 $SP 2>/dev/null || break; sleep 1; done
kill -TERM $SP 2>/dev/null
sleep 3
kill -KILL $SP 2>/dev/null
wait $SP 2>/dev/null
chmod -R a+rw /w
"""
    (a.out / "run.sh").write_text(script)
    cname = f"rw-shot-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", "run", "--rm", "--name", cname, "--gpus", f"device={a.gpu}",
           "-e", "GZ_SIM_RESOURCE_PATH=/opt/f:/opt/m:/opt/px4-gazebo/share/gz/models",
           "-v", f"{RESOLVED}:/opt/f:ro", "-v", f"{(ROOT / 'simulation/gazebo/models').resolve()}:/opt/m:ro",
           "-v", f"{a.out}:/w", "--entrypoint", "/bin/bash", IMAGE, "/w/run.sh"]
    t0 = time.time()
    gpu_rows = []
    stop = threading.Event()
    monitor = threading.Thread(target=sample_gpu_memory, args=(cname, a.gpu, stop, gpu_rows), daemon=True)
    monitor.start()
    try:
        completed = subprocess.run(cmd, timeout=a.timeout + 120, check=False)
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", cname], check=False, capture_output=True)
        returncode = 124
    finally:
        stop.set()
        monitor.join(timeout=12)
    stats = []
    for line in (a.out / "stats.json").read_text().splitlines() if (a.out / "stats.json").exists() else []:
        try:
            stats.append(json.loads(line))
        except ValueError:
            pass
    factors = [float(row.get("realTimeFactor", row.get("real_time_factor"))) for row in stats if "realTimeFactor" in row or "real_time_factor" in row]
    def seconds(stamp):
        return float(stamp.get("sec", 0)) + float(stamp.get("nsec", 0)) * 1e-9

    aggregate_rtf = None
    if len(stats) > 1:
        wall_delta = seconds(stats[-1].get("realTime", {})) - seconds(stats[0].get("realTime", {}))
        sim_delta = seconds(stats[-1].get("simTime", {})) - seconds(stats[0].get("simTime", {}))
        if wall_delta > 0:
            aggregate_rtf = sim_delta / wall_delta
    metrics = {"world": str(a.world), "gpu": a.gpu, "returncode": returncode,
               "camera_count": len(names), "camera_update_hz": 1, "loadavg": os.getloadavg(),
               "gpu_memory_peak_mib": max((row["used_mib"] for row in gpu_rows), default=None),
               "gpu_memory_samples": gpu_rows,
               "wall_s": round(time.time() - t0, 2), "rtf_samples": factors,
               "rtf_sim_wall": aggregate_rtf, "rtf_sample_count": len(stats),
               "rtf_median": float(np.median(factors)) if factors else None,
               "first_frame_s": {n: int((a.out / f"{n}.first_s").read_text()) for n in names if (a.out / f"{n}.first_s").exists()}}
    (a.out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics), flush=True)
    print("wall", round(time.time() - t0), (a.out / "time.txt").read_text() if (a.out / "time.txt").exists() else "")
    for n in names:
        frame_path = a.out / f"{n}.json"
        lines = frame_path.read_text().splitlines() if frame_path.exists() else []
        if not lines:
            print("NO FRAME", n)
            continue
        m = json.loads(lines[-1])
        arr = np.frombuffer(base64.b64decode(m["data"]), np.uint8).reshape(int(m["height"]), int(m["width"]), -1)[..., :3]
        Image.fromarray(arr).save(a.out / f"{n}.jpg", quality=92)
        m0 = json.loads(lines[0])
        Image.fromarray(np.frombuffer(base64.b64decode(m0["data"]), np.uint8).reshape(int(m0["height"]), int(m0["width"]), -1)[..., :3]).save(a.out / f"{n}_first.jpg", quality=80)
        (a.out / f"{n}.json").unlink()
    print(sorted(p.name for p in a.out.glob("*.jpg")))
    if returncode or any(not (a.out / f"{n}.jpg").exists() for n in names):
        raise SystemExit(returncode or 1)


if __name__ == "__main__":
    main()
