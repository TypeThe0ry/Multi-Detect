from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from typing import Any

from pymavlink import mavutil

_LOOPBACK_UDPOUT = re.compile(r"^udpout:127\.0\.0\.1:(\d{1,5})$")
_SOURCE_SYSTEM = 255
_SOURCE_COMPONENT = mavutil.mavlink.MAV_COMP_ID_MISSIONPLANNER


class SitlHeartbeatError(RuntimeError):
    """Raised when the software-only GCS heartbeat boundary is not satisfied."""


def loopback_port(endpoint: str) -> int:
    match = _LOOPBACK_UDPOUT.fullmatch(endpoint)
    if match is None:
        raise SitlHeartbeatError("endpoint must be udpout:127.0.0.1:<port>")
    port = int(match.group(1))
    if not 1024 <= port <= 65535:
        raise SitlHeartbeatError("loopback UDP port must be in 1024..65535")
    return port


def send_heartbeats(
    endpoint: str,
    duration_seconds: float,
    rate_hz: float,
    *,
    connection_factory: Callable[..., Any] = mavutil.mavlink_connection,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    port = loopback_port(endpoint)
    if not 1.0 <= duration_seconds <= 120.0:
        raise SitlHeartbeatError("duration must be in 1..120 seconds")
    if not 0.5 <= rate_hz <= 10.0:
        raise SitlHeartbeatError("rate must be in 0.5..10 Hz")

    connection = connection_factory(
        endpoint,
        source_system=_SOURCE_SYSTEM,
        source_component=_SOURCE_COMPONENT,
    )
    interval = 1.0 / rate_hz
    started = monotonic()
    deadline = started + duration_seconds
    next_send = started
    transmitted = 0
    try:
        while True:
            now = monotonic()
            if now >= deadline:
                break
            if now < next_send:
                sleep(min(next_send - now, 0.1))
                continue
            connection.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0,
                0,
                mavutil.mavlink.MAV_STATE_ACTIVE,
                3,
            )
            transmitted += 1
            next_send += interval
    finally:
        connection.close()

    return {
        "event": "px4_sitl_gcs_heartbeat_finished",
        "endpoint": endpoint,
        "loopback_port": port,
        "duration_seconds": duration_seconds,
        "rate_hz": rate_hz,
        "messages_transmitted": transmitted,
        "source_system_id": _SOURCE_SYSTEM,
        "source_component_id": _SOURCE_COMPONENT,
        "mav_type": "MAV_TYPE_GCS",
        "software_only": True,
        "real_v6x_contacted": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send bounded GCS heartbeats to an owned localhost PX4 SITL instance."
    )
    parser.add_argument("--endpoint", default="udpout:127.0.0.1:18570")
    parser.add_argument("--duration-seconds", type=float, default=8.0)
    parser.add_argument("--rate-hz", type=float, default=2.0)
    parser.add_argument("--acknowledge-owned-disposable-sitl", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.acknowledge_owned_disposable_sitl:
        print(
            json.dumps(
                {"error": "explicit owned-disposable-SITL acknowledgement is required"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        result = send_heartbeats(args.endpoint, args.duration_seconds, args.rate_hz)
    except (OSError, SitlHeartbeatError, ValueError) as error:
        print(json.dumps({"error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
