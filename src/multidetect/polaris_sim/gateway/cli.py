"""Command-line entry point for ``polaris-sim-gateway``."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .server import GatewayConfig, PolarisSimGateway, VideoSourceConfig, vehicle_for_slot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Polaris MAVLink, RTSP video, HTTP control and discovery gateway"
    )
    parser.add_argument("--slots", default="5", help="comma-separated SITL slots: 5,6,7")
    parser.add_argument("--world", default="rw_suburb_davis")
    parser.add_argument("--airframe", default="x500_mono_cam")
    parser.add_argument("--camera-profile", choices=("lite", "standard", "hd"), default="lite")
    parser.add_argument("--tcp-port", type=int, default=5770)
    parser.add_argument("--udp-port", type=int, default=14550)
    parser.add_argument("--http-port", type=int, default=8780)
    parser.add_argument("--discovery-port", type=int, default=8781)
    parser.add_argument("--host-address", default=None)
    parser.add_argument("--record-dir", type=Path, default=Path("/data/scratch/polaris-sim/recordings"))
    parser.add_argument("--state-file", type=Path, default=Path("/data/scratch/polaris-sim/state.json"))
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--video-source",
        nargs=5,
        action="append",
        default=[],
        metavar=("SLOT", "CAMERA", "CONTAINER", "WORLD", "MODEL"),
        help="embed one Gazebo camera source in this gateway process; repeat per vehicle",
    )
    parser.add_argument("--no-video", action="store_true", help="disable embedded RTSP video")
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        slots = [int(value) for value in args.slots.split(",") if value]
        if not slots or len(set(slots)) != len(slots):
            raise ValueError("--slots must contain unique slot numbers")
        vehicles = [
            vehicle_for_slot(slot, args.world, args.airframe, args.camera_profile)
            for slot in slots
        ]
        video_sources = []
        for raw_slot, camera, container, world, model in args.video_source:
            slot = int(raw_slot)
            if slot not in slots:
                raise ValueError(f"video source slot {slot} is not in --slots")
            if camera not in {"cam", "gimbal"}:
                raise ValueError("video source camera must be cam or gimbal")
            video_sources.append(
                VideoSourceConfig(slot - 5, camera, container, world, model)
            )
        if args.no_video and video_sources:
            raise ValueError("--no-video cannot be combined with --video-source")
        if not args.no_video and not video_sources:
            camera = "gimbal" if "gimbal" in args.airframe else "cam"
            video_sources = [
                VideoSourceConfig(
                    slot - 5,
                    camera,
                    f"md-polaris-sim-slot-{slot}",
                    args.world,
                    f"{args.airframe}_{slot - 5}",
                )
                for slot in slots
            ]
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    config = GatewayConfig(
        tcp_port=args.tcp_port,
        udp_port=args.udp_port,
        http_port=args.http_port,
        discovery_port=args.discovery_port,
        record_dir=args.record_dir,
        state_file=args.state_file,
        host_address=args.host_address,
        netem_seed=args.seed,
        video_sources=tuple(video_sources),
        camera_profile=args.camera_profile,
    )
    gateway = PolarisSimGateway(vehicles, config)
    await gateway.start()
    print(json.dumps({"status": "ready", **gateway.discovery()}, ensure_ascii=False), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await stop.wait()
    await gateway.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except OSError as exc:
        print(f"polaris-sim-gateway: {exc}", file=sys.stderr)
        return 2
    return 0


__all__ = ["main"]
