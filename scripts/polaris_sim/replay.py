#!/usr/bin/env python3
"""Replay a Polaris recording as RTSP and registered-GCS MAVLink UDP."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.polaris_sim.replay import RecordingReplay  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--host", default="192.168.1.138")
    parser.add_argument("--rtsp-port", type=int, default=8554)
    parser.add_argument("--mavlink-host", default="192.168.1.138")
    parser.add_argument("--mavlink-port", type=int, default=14550)
    parser.add_argument("--speed", type=float, choices=(0.5, 1.0, 2.0), default=1.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    service = RecordingReplay(
        args.recording,
        host=args.host,
        rtsp_port=args.rtsp_port,
        mavlink_host=args.mavlink_host,
        mavlink_port=args.mavlink_port,
        speed=args.speed,
        loop=args.loop,
    )
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    service.start()
    print(
        json.dumps(
            {
                "state": "replaying",
                "rtsp_port": service.server.bound_port,
                "mavlink_port": service.mavlink.port if service.mavlink else None,
                "speed": args.speed,
                "loop": args.loop,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        stopped.wait()
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
