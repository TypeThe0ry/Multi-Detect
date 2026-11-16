"""Command-line entry point for ``polaris-sim-gateway``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys

from .server import GatewayConfig, PolarisSimGateway, vehicle_for_slot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Polaris MAVLink, HTTP control and discovery gateway")
    parser.add_argument("--slots", default="5", help="comma-separated SITL slots: 5,6,7")
    parser.add_argument("--world", default="rw_suburb_davis")
    parser.add_argument("--airframe", default="x500_mono_cam")
    parser.add_argument("--tcp-port", type=int, default=5770)
    parser.add_argument("--udp-port", type=int, default=14550)
    parser.add_argument("--http-port", type=int, default=8780)
    parser.add_argument("--discovery-port", type=int, default=8781)
    parser.add_argument("--host-address", default=None)
    parser.add_argument("--record-dir", type=Path, default=Path("/data/scratch/polaris-sim/recordings"))
    parser.add_argument("--state-file", type=Path, default=Path("/data/scratch/polaris-sim/state.json"))
    parser.add_argument("--seed", type=int, default=1)
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        slots = [int(value) for value in args.slots.split(",") if value]
        if not slots or len(set(slots)) != len(slots):
            raise ValueError("--slots must contain unique slot numbers")
        vehicles = [vehicle_for_slot(slot, args.world, args.airframe) for slot in slots]
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
