#!/usr/bin/env python3
"""Fly one airframe through the project's smoke flight in a leased SITL slot, sampling rotors live.

usage: leased_flight.py <slot> <airframe> <world-stem> <out-dir>
Slots 0-2 belong to r15 and 3-4 to the delta loop, so this uses 5 and up.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.sitl_lock import SitlLock, sitl_lock_context  # noqa: E402


def sample_rotors(container_prefix: str, out: Path, stop: threading.Event) -> None:
    """Read Gazebo poses inside the running container, without touching PX4."""
    rows = []
    while not stop.wait(20):
        names = subprocess.run(["docker", "ps", "--format", "{{.Names}}", "--filter", f"name={container_prefix}"], capture_output=True, text=True).stdout.split()
        if not names:
            continue
        cmd = "source /opt/ros/*/setup.bash 2>/dev/null; timeout 8 gz topic -e --json-output -n 12 -t /world/default/dynamic_pose/info"
        text = subprocess.run(["docker", "exec", names[0], "bash", "-c", cmd], capture_output=True, text=True, timeout=30).stdout
        seq: dict[str, list] = {}
        for line in text.splitlines():
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            st = m["header"]["stamp"]
            t = float(st.get("sec", 0)) + float(st.get("nsec", 0)) * 1e-9
            for p in m.get("pose", []):
                if "rotor" in p.get("name", ""):
                    o = p.get("orientation", {})
                    seq.setdefault(p["name"], []).append((t, [o.get("w", 1.0), o.get("x", 0.0), o.get("y", 0.0), o.get("z", 0.0)]))
        rates = {}
        for name, s in seq.items():
            vals = []
            for (t0, a), (t1, b) in zip(s, s[1:]):
                if t1 > t0:
                    d = min(1.0, abs(sum(x * y for x, y in zip(a, b))))
                    vals.append(2 * math.acos(d) / (t1 - t0))
            rates[name] = round(sorted(vals)[len(vals) // 2], 1) if vals else 0.0
        rows.append({"wall": time.time(), "rad_s": rates})
        out.write_text(json.dumps(rows, indent=0))


def main() -> None:
    slot, airframe, world, out = int(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
    lock = SitlLock(label=f"rw-flight-{airframe}", slots=int(os.environ.get("MULTIDETECT_SITL_SLOTS", "8")), slot=slot, retry_s=20.0)
    with sitl_lock_context(lock):
        control = 14560 + 10 * slot
        prefix = f"md-smoke-rw-slot-{slot}"
        out.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        th = threading.Thread(target=sample_rotors, args=(prefix, out / "rotors_in_flight.json", stop), daemon=True)
        th.start()
        extra = sys.argv[5:]
        rc = subprocess.run([sys.executable, "scripts/smoke_airframe_flight.py", "--airframe", airframe,
                             "--scenario", f"configs/scenarios/realworld/{world}.scenario.json", "--out", str(out),
                             "--container-prefix", prefix, "--control-port", str(control), "--telemetry-port", str(control + 1), *extra], cwd=ROOT).returncode
        stop.set()
        sys.exit(rc)


if __name__ == "__main__":
    main()
