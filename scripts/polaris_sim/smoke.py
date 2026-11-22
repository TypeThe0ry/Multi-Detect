#!/usr/bin/env python3
"""Exercise the TCP gateway with a fake GCS: arm, climb to 20 m, hover and land."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time
import urllib.request


def _command(connection, command: int, params: tuple[float, ...], timeout: float) -> float:
    started = time.monotonic()
    connection.mav.command_long_send(connection.target_system, connection.target_component,
                                     command, 0, *params)
    deadline = started + timeout
    while time.monotonic() < deadline:
        ack = connection.recv_match(type=["COMMAND_ACK"], blocking=True, timeout=0.5)
        if ack is not None and ack.command == command:
            if ack.result not in (0, 5):
                raise RuntimeError(f"MAV_CMD {command} returned ACK {ack.result}")
            return time.monotonic() - started
    raise TimeoutError(f"MAV_CMD {command} ACK timed out")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5770)
    parser.add_argument("--sysid", type=int, default=1)
    parser.add_argument("--takeoff-m", type=float, default=20.0)
    parser.add_argument("--hover-s", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, default=Path("/data/scratch/polaris-sim/smoke"))
    args = parser.parse_args(argv)
    try:
        from pymavlink import mavutil
    except ImportError:
        print("pymavlink is required; install the project's pixhawk optional extra", file=sys.stderr)
        return 2
    connection = mavutil.mavlink_connection(f"tcp:{args.host}:{args.port}", source_system=255, source_component=190)
    samples: list[dict[str, object]] = []
    acks: list[dict[str, object]] = []
    record_started = False
    try:
        record_url = f"http://{args.host}:8780/v1/record/start"
        request = urllib.request.Request(record_url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=2) as response:
            record_started = response.status == 200
        heartbeat = connection.wait_heartbeat(timeout=30)
        if heartbeat is None or connection.target_system != args.sysid:
            raise TimeoutError(f"expected sysid {args.sysid}, got {connection.target_system}")
        arm_ack = _command(connection, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                           (1, 0, 0, 0, 0, 0, 0), args.timeout)
        acks.append({"command": "arm", "ack_latency_ms": arm_ack * 1000})
        takeoff_ack = _command(connection, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                               (0, 0, 0, math.nan, math.nan, math.nan, args.takeoff_m), args.timeout)
        acks.append({"command": "takeoff", "ack_latency_ms": takeoff_ack * 1000})
        reached_at = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            message = connection.recv_match(type=["GLOBAL_POSITION_INT", "HEARTBEAT"], blocking=True, timeout=1)
            if message is None or message.get_type() != "GLOBAL_POSITION_INT":
                continue
            relative_alt = float(message.relative_alt) / 1000.0
            samples.append({"wall_time": time.time(), "relative_alt_m": relative_alt,
                            "lat_e7": message.lat, "lon_e7": message.lon})
            if relative_alt >= args.takeoff_m - 2.0:
                reached_at = time.monotonic()
                break
        if reached_at is None:
            raise TimeoutError(f"vehicle did not reach {args.takeoff_m} m")
        while time.monotonic() - reached_at < args.hover_s:
            message = connection.recv_match(type=["GLOBAL_POSITION_INT"], blocking=True, timeout=1)
            if message is not None:
                samples.append({"wall_time": time.time(), "relative_alt_m": float(message.relative_alt) / 1000.0,
                                "lat_e7": message.lat, "lon_e7": message.lon})
        land_ack = _command(connection, mavutil.mavlink.MAV_CMD_NAV_LAND,
                            (0, 0, 0, math.nan, math.nan, math.nan, 0), args.timeout)
        acks.append({"command": "land", "ack_latency_ms": land_ack * 1000})
        landed = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            message = connection.recv_match(type=["GLOBAL_POSITION_INT"], blocking=True, timeout=1)
            if message is not None:
                relative_alt = float(message.relative_alt) / 1000.0
                samples.append({"wall_time": time.time(), "relative_alt_m": relative_alt,
                                "lat_e7": message.lat, "lon_e7": message.lon})
                if relative_alt < 1.0:
                    landed = True
                    break
        if not landed:
            raise TimeoutError("vehicle did not descend below 1 m after LAND")
        args.out.mkdir(parents=True, exist_ok=True)
        path = args.out / f"smoke-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        path.write_text(json.dumps({"status": "PASS", "host": args.host, "sysid": args.sysid,
                                   "takeoff_m": args.takeoff_m, "hover_s": args.hover_s,
                                   "samples": samples, "command_acks": acks}, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "samples": len(samples), "evidence": str(path), "command_acks": acks}, ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    finally:
        if record_started:
            request = urllib.request.Request("http://" + args.host + ":8780/v1/record/stop",
                                             data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(request, timeout=2).close()
            except OSError:
                pass
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
