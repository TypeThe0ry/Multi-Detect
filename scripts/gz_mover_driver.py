#!/usr/bin/env python3
"""Drive one static mover model from Gazebo simulation time.

This file intentionally uses only the Python standard library because the PX4
SITL image does not include Gazebo Python bindings.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def parse_clock_text(text: str) -> float | None:
    """Sim time (s) from one gz.msgs.Clock text message.

    The message also carries header/system/real times; only the ``sim { }``
    block is simulation time.
    """
    block = re.search(r"\bsim\s*\{([^}]*)\}", text)
    if block is None:
        return None
    sec = re.search(r"\bsec\s*:\s*(-?\d+)", block.group(1))
    nsec = re.search(r"\bnsec\s*:\s*(-?\d+)", block.group(1))
    if sec is None:
        return None
    return float(sec.group(1)) + float(nsec.group(1) if nsec else 0) / 1_000_000_000.0


def trajectory_position_yaw(
    trajectory: Mapping[str, Any], time_s: float
) -> tuple[float, float, float]:
    """Return ENU east, north, yaw for the looping constant-speed polyline."""
    points = [tuple(float(v) for v in p[:2]) for p in trajectory.get("waypoints", ())]
    if len(points) < 2:
        raise ValueError("trajectory requires at least two waypoints")
    speed = float(trajectory.get("speed_mps", 0.0))
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("trajectory speed_mps must be positive")
    legs = []
    total = 0.0
    for start, end in zip(points, points[1:] + points[:1], strict=False):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        legs.append((start, end, length))
        total += length
    if total <= 0:
        return points[0][0], points[0][1], 0.0
    distance = (float(time_s) * speed) % total
    for start, end, length in legs:
        if distance <= length:
            fraction = distance / length if length else 0.0
            east = start[0] + fraction * (end[0] - start[0])
            north = start[1] + fraction * (end[1] - start[1])
            return east, north, math.atan2(end[1] - start[1], end[0] - start[0])
        distance -= length
    start, end, _ = legs[-1]
    return end[0], end[1], math.atan2(end[1] - start[1], end[0] - start[0])


def pose_request(model: str, east: float, north: float, z: float, yaw: float) -> str:
    """Build the text protobuf accepted by gz service."""
    return (
        f'name: "{model}", position: {{x: {east:.6f}, y: {north:.6f}, z: {z:.6f}}}, '
        f"orientation: {{x: 0, y: 0, z: {math.sin(yaw / 2):.9f}, w: {math.cos(yaw / 2):.9f}}}"
    )


def _service(world: str, request: str) -> tuple[bool, float, str]:
    started = time.monotonic()
    result = subprocess.run(
        [
            "gz",
            "service",
            "-s",
            f"/world/{world}/set_pose",
            "--reqtype",
            "gz.msgs.Pose",
            "--reptype",
            "gz.msgs.Boolean",
            "--timeout",
            "500",
            "--req",
            request,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        result.returncode == 0 and "data: true" in (result.stdout or "").lower(),
        time.monotonic() - started,
        (result.stderr or result.stdout or "").strip(),
    )


class LatestClock:
    """Follow `gz topic -e -t /clock` on a thread and keep only the newest sim time.

    set_pose calls block for 0.1-0.5 s of wall time; reading the clock stream in
    the same loop would act on an ever older backlog, so the reader runs apart.
    """

    def __init__(self) -> None:
        self.sim_time: float | None = None
        self._lock = threading.Lock()
        self.process = subprocess.Popen(
            ["gz", "topic", "-e", "-t", "/clock"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        message = ""
        for line in self.process.stdout:
            # `gz topic -e` separates messages with a blank line.
            if line.strip():
                message += line
                continue
            value = parse_clock_text(message)
            message = ""
            if value is not None:
                with self._lock:
                    if self.sim_time is None or value >= self.sim_time:
                        self.sim_time = value

    def latest(self) -> float | None:
        with self._lock:
            return self.sim_time

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def run(args: argparse.Namespace) -> int:
    trajectory = json.loads(args.trajectory_json)
    stop = False

    def handle(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    log = Path(args.log).open("a", encoding="utf-8", buffering=1) if args.log else None
    clock = LatestClock()
    last_sim = -math.inf
    try:
        while not stop:
            sim_time = clock.latest()
            if sim_time is None or sim_time < last_sim + args.period_s:
                time.sleep(0.01)
                continue
            last_sim = sim_time
            east, north, yaw = trajectory_position_yaw(trajectory, sim_time)
            ok, latency, detail = _service(
                args.world, pose_request(args.model, east, north, args.z, yaw)
            )
            applied = clock.latest()
            row = {
                "sim_time_s": sim_time,
                "east_m": east,
                "north_m": north,
                "z_m": args.z,
                "yaw_rad": yaw,
                "ok": ok,
                "wall_latency_s": latency,
                # Sim time when the call returned: how stale the commanded pose was.
                "applied_sim_time_s": applied,
                "detail": detail,
            }
            if log:
                log.write(json.dumps(row, sort_keys=True) + "\n")
    finally:
        clock.close()
        if log:
            log.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", default="default")
    parser.add_argument("--model", required=True)
    parser.add_argument("--trajectory-json", required=True)
    parser.add_argument("--z", type=float, required=True)
    parser.add_argument("--period-s", type=float, default=0.1)
    parser.add_argument("--log", default=None)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
