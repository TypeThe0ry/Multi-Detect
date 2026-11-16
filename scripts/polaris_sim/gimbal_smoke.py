#!/usr/bin/env python3
"""Send a MAVLink Gimbal Manager v2 command and verify telemetry readback."""

from __future__ import annotations

import argparse
import math
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5770)
    parser.add_argument("--sysid", type=int, default=1)
    parser.add_argument("--pitch-deg", type=float, default=-25.0)
    parser.add_argument("--yaw-deg", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        from pymavlink import mavutil
    except ImportError:
        print("pymavlink is required; install the polaris-sim optional extra", file=sys.stderr)
        return 2

    connection = mavutil.mavlink_connection(f"tcp:{args.host}:{args.port}", source_system=255, source_component=190)
    try:
        heartbeat = connection.wait_heartbeat(timeout=args.timeout)
        if heartbeat is None or connection.target_system != args.sysid:
            raise TimeoutError(f"expected PX4 sysid {args.sysid}, received {connection.target_system}")
        target_system = connection.target_system
        target_component = connection.target_component
        commands = (
            (mavutil.mavlink.MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE,
             (255.0, 190.0, -1.0, -1.0, 0.0, 0.0, 1.0)),
            (mavutil.mavlink.MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW,
             (args.pitch_deg, args.yaw_deg, math.nan, math.nan, 44.0, 0.0, 1.0)),
        )
        for command, params in commands:
            connection.mav.command_long_send(
                target_system, target_component, command, 0, *params
            )
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                message = connection.recv_match(type=["COMMAND_ACK"], blocking=True, timeout=0.5)
                if message is not None and message.command == command:
                    if message.result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
                        raise RuntimeError(f"command {command} rejected with result {message.result}")
                    break
            else:
                raise TimeoutError(f"COMMAND_ACK timeout for command {command}")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            message = connection.recv_match(type=["GIMBAL_DEVICE_ATTITUDE_STATUS"], blocking=True, timeout=0.5)
            if message is not None:
                q = message.q
                w, x, y, z = (float(value) for value in q)
                pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (w * y - z * x)))))
                yaw = math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))
                pitch_error = abs((pitch - args.pitch_deg + 180.0) % 360.0 - 180.0)
                yaw_error = abs((yaw - args.yaw_deg + 180.0) % 360.0 - 180.0)
                if max(pitch_error, yaw_error) <= 4.0:
                    print(f"PASS sysid={target_system} pitch_error_deg={pitch_error:.1f} yaw_error_deg={yaw_error:.1f} flags={message.flags}")
                    return 0
        raise TimeoutError("GIMBAL_DEVICE_ATTITUDE_STATUS readback timeout")
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
