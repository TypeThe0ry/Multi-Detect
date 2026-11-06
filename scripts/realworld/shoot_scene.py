#!/usr/bin/env python3
"""Render still cameras in a world, for visual review. One throwaway container on one GPU."""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "multidetect/px4-sitl-gazebo:gpu"
RESOLVED = (ROOT / "artifacts/cache/gz-fuel/resolved").resolve()


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
    a.out = a.out.resolve()
    shutil.rmtree(a.out, ignore_errors=True)
    a.out.mkdir(parents=True)
    text = a.world.read_text()
    names = []
    cams = []
    for spec in a.cam:
        n, p, t = spec.split(":")
        names.append(n)
        cams.append(cam_model(n, *map(float, p.split(",")), *map(float, t.split(","))))
    text = text.replace("</world>", "".join(cams) + "</world>")
    (a.out / "world.sdf").write_text(text)
    wait = " ".join(names)
    script = f"""
source /opt/ros/*/setup.bash 2>/dev/null
export GZ_IP=127.0.0.1
T0=$(date +%s)
gz sim -s -r --headless-rendering --render-engine ogre2 /w/world.sdf > /w/server.log 2>&1 &
SP=$!
for n in {wait}; do (timeout {a.timeout - 20} gz topic -e --json-output -n {a.frames} -t shot/$n > /w/$n.json 2>/dev/null) & done
for i in $(seq 1 {a.timeout // 2}); do sleep 2; c=0; for n in {wait}; do [ "$(wc -l < /w/$n.json)" -ge {a.frames} ] && c=$((c+1)); done; [ $c = {len(names)} ] && break; done
echo "first frames after $(( $(date +%s) - T0 )) s" > /w/time.txt
gz stats -p 2>/dev/null | true
timeout 8 gz topic -e -n 5 -t /stats 2>/dev/null | grep -E "real_time_factor" | tail -3 >> /w/time.txt
kill -INT $SP; sleep 1; kill -9 $SP 2>/dev/null; chmod -R a+rw /w; true
"""
    (a.out / "run.sh").write_text(script)
    cname = f"rw-shot-{a.out.name}-{int(time.time()) % 100000}"
    cmd = ["docker", "run", "--rm", "--name", cname, "--gpus", f"device={a.gpu}",
           "-e", "GZ_SIM_RESOURCE_PATH=/opt/f:/opt/m:/opt/px4-gazebo/share/gz/models",
           "-v", f"{RESOLVED}:/opt/f:ro", "-v", f"{(ROOT / 'simulation/gazebo/models').resolve()}:/opt/m:ro",
           "-v", f"{a.out}:/w", "--entrypoint", "/bin/bash", IMAGE, "/w/run.sh"]
    t0 = time.time()
    subprocess.run(cmd, timeout=a.timeout + 120, check=False)
    print("wall", round(time.time() - t0), (a.out / "time.txt").read_text() if (a.out / "time.txt").exists() else "")
    for n in names:
        lines = (a.out / f"{n}.json").read_text().splitlines()
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


if __name__ == "__main__":
    main()
