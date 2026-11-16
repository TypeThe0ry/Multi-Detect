#!/usr/bin/env python3
"""Publish one Gazebo camera as rtsp://host:8554/v{index}/{cam|gimbal}."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.polaris_sim.video import VideoService, VideoSettings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicle-index", type=int, choices=(0, 1, 2), required=True)
    parser.add_argument("--camera", choices=("cam", "gimbal"), required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--world", required=True)
    parser.add_argument("--model", required=True, help="Gazebo model instance, e.g. x500_gimbal_0")
    parser.add_argument("--topic", help="override Gazebo Image topic")
    parser.add_argument("--host", default="192.168.1.138")
    parser.add_argument("--port", type=int, default=8554)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate-kbps", type=int, default=2500)
    parser.add_argument("--profile", choices=("baseline", "main"), default="baseline")
    parser.add_argument("--encoder-threads", type=int, default=2)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = VideoSettings(
        width=args.width,
        height=args.height,
        fps=args.fps,
        bitrate_kbps=args.bitrate_kbps,
        profile=args.profile,
        encoder_threads=args.encoder_threads,
        ffmpeg=args.ffmpeg,
    )
    service = VideoService(args.host, args.port)
    service.add_gazebo_camera(
        args.vehicle_index,
        args.camera,
        args.container,
        args.world,
        args.model,
        topic=args.topic,
        settings=settings,
    )
    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    service.start()
    print(
        f"rtsp://{args.host}:{service.server.bound_port}/v{args.vehicle_index}/{args.camera}",
        flush=True,
    )
    try:
        stopped.wait()
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
