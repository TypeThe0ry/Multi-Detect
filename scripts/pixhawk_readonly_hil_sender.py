from __future__ import annotations

import argparse
import json

from multidetect.pixhawk_hil import FixedWingTelemetryHilConfig, FixedWingTelemetryHilEmitter


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emit fixed-wing MAVLink telemetry for a read-only receiver HIL check."
    )
    parser.add_argument("--endpoint", default="udpout:127.0.0.1:14550")
    parser.add_argument("--duration-seconds", type=float, default=5.0)
    parser.add_argument("--rate-hz", type=float, default=10.0)
    parser.add_argument("--latitude", type=float, default=31.123456)
    parser.add_argument("--longitude", type=float, default=121.654321)
    parser.add_argument("--altitude-agl-m", type=float, default=42.5)
    parser.add_argument("--ground-speed-mps", type=float, default=17.0)
    parser.add_argument("--heading-deg", type=float, default=90.0)
    parser.add_argument("--mission-sequence", type=int, default=3)
    args = parser.parse_args()

    config = FixedWingTelemetryHilConfig(
        endpoint=args.endpoint,
        rate_hz=args.rate_hz,
        latitude_deg=args.latitude,
        longitude_deg=args.longitude,
        altitude_agl_m=args.altitude_agl_m,
        ground_speed_mps=args.ground_speed_mps,
        heading_deg=args.heading_deg,
        mission_sequence=args.mission_sequence,
    )
    emitter = FixedWingTelemetryHilEmitter(config)
    print(
        json.dumps(
            {
                "event": "pixhawk_telemetry_hil_sender_started",
                "endpoint": config.endpoint,
                "rate_hz": config.rate_hz,
                "telemetry_only": True,
                "flight_control_enabled": False,
                "physical_release_enabled": False,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    cycle_count, message_count = emitter.run(duration_s=args.duration_seconds)
    print(
        json.dumps(
            {
                "event": "pixhawk_telemetry_hil_sender_finished",
                "cycles_transmitted": cycle_count,
                "telemetry_messages_transmitted": message_count,
                "command_messages_transmitted": 0,
                "mission_upload_messages_transmitted": 0,
                "actuator_messages_transmitted": 0,
                "telemetry_only": True,
                "flight_control_enabled": False,
                "physical_release_enabled": False,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
