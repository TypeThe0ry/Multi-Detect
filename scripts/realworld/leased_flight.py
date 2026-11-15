#!/usr/bin/env python3
"""Fly one airframe through the project's smoke flight in a leased SITL slot, sampling rotors live.

usage: leased_flight.py <slot> <airframe> <world-stem> <out-dir> [--plan-only]
Slots 0-2 belong to r15 and 3-4 to the delta loop, so this uses 5 and up.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITL_PYTHON = "/data/projects/Multi-Detect-env/Multi-Detect/.venv-vlm/bin/python"
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


def flight_command(slot: int, airframe: str, world: str, out: Path, *, plan_only: bool, extra: list[str]) -> list[str]:
    """Use the same scenario and port bindings for a flight and its CPU plan."""
    scenario = world if world.endswith(".json") else f"configs/scenarios/realworld/{'' if airframe == 'x500_mono_cam' else airframe + '/'}{world}.scenario.json"
    control = 14560 + 10 * slot
    script = "scripts/collect_sim_teacher_episodes.py" if plan_only else "scripts/smoke_airframe_flight.py"
    command = [SITL_PYTHON, script, "--airframe", airframe, "--scenario", scenario, "--out", str(out),
               "--container-prefix", f"md-smoke-rw-slot-{slot}", "--control-port", str(control), "--telemetry-port", str(control + 1)]
    if plan_only:
        return [*command, "--teacher", "explore", "--episodes", "1", "--steps-per-episode", "8", "--smoke-flight", "--dry-run"]
    return [*command, *extra]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slot", type=int)
    parser.add_argument("airframe")
    parser.add_argument("world")
    parser.add_argument("out", type=Path)
    parser.add_argument("--plan-only", action="store_true", help="只构建 CPU dry-run plan，不租用 SITL slot 或启动 Docker")
    args, extra = parser.parse_known_args(argv)
    if args.plan_only and extra:
        parser.error("--plan-only cannot be combined with extra flight arguments")
    slot, airframe, world, out = args.slot, args.airframe, args.world, args.out
    slots = int(os.environ.get("MULTIDETECT_SITL_SLOTS", "8"))
    if slots < 8:
        raise SystemExit("realworld validation requires MULTIDETECT_SITL_SLOTS>=8")
    if slot not in {5, 6, 7}:
        raise SystemExit("realworld validation may use only slots 5, 6, or 7")
    if not Path(SITL_PYTHON).is_file():
        raise SystemExit(f"missing SITL Python environment: {SITL_PYTHON}")
    command = flight_command(slot, airframe, world, out, plan_only=args.plan_only, extra=extra)
    if args.plan_only:
        # Mirror the slot marker normally inherited from sitl_lock_context.
        return subprocess.run(command, cwd=ROOT, env={**os.environ, "MULTIDETECT_SITL_SLOT": str(slot)}).returncode
    lock = SitlLock(label=f"rw-flight-{airframe}", slots=slots, slot=slot, retry_s=20.0)
    with sitl_lock_context(lock):
        prefix = f"md-smoke-rw-slot-{slot}"
        out.mkdir(parents=True, exist_ok=True)
        stop = threading.Event()
        th = threading.Thread(target=sample_rotors, args=(prefix, out / "rotors_in_flight.json", stop), daemon=True)
        th.start()
        rc = subprocess.run(command, cwd=ROOT).returncode
        stop.set()
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
