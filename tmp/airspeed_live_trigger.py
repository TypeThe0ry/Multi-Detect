from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from pymavlink import mavutil


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/evaluation/v6x-airspeed-live-trigger-20260716.json"
READY = ROOT / "tmp/airspeed_live_trigger.ready"
ENDPOINT = "tcp:192.168.144.11:5760"
BASELINE_SECONDS = 5.0
TIMEOUT_SECONDS = 900.0
PRESSURE_TRIGGER_DELTA_PA = 20.0
POST_TRIGGER_SECONDS = 5.0


connection = mavutil.mavlink_connection(
    ENDPOINT,
    autoreconnect=False,
    source_system=241,
    source_component=192,
)


def _eof() -> None:
    raise EOFError("GR01 TCP closed")


def _disconnect() -> None:
    raise ConnectionAbortedError("GR01 TCP reset")


connection.handle_eof = _eof
connection.handle_disconnect = _disconnect
heartbeat = connection.wait_heartbeat(timeout=8)
if heartbeat is None:
    raise RuntimeError("heartbeat timeout")

connection.target_system = heartbeat.get_srcSystem()
connection.target_component = heartbeat.get_srcComponent()


def set_interval(message_id: int, interval_us: int) -> None:
    connection.mav.command_long_send(
        connection.target_system,
        connection.target_component,
        mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
        0,
        message_id,
        interval_us,
        0,
        0,
        0,
        0,
        0,
    )


for message_id, interval in ((29, 50_000), (74, 100_000)):
    set_interval(message_id, interval)

samples: list[dict[str, float | str]] = []
baseline_pressure: list[float] = []
message_counts: dict[str, int] = {}
start = time.monotonic()
baseline_pa: float | None = None
trigger: dict[str, float | str] | None = None
trigger_time: float | None = None

try:
    while time.monotonic() - start < TIMEOUT_SECONDS:
        message = connection.recv_match(blocking=True, timeout=0.25)
        if message is None:
            continue

        message_type = message.get_type()
        message_counts[message_type] = message_counts.get(message_type, 0) + 1
        elapsed = time.monotonic() - start

        if message_type == "SCALED_PRESSURE":
            pressure_pa = float(message.press_diff) * 100.0
            if not math.isfinite(pressure_pa):
                continue
            samples.append(
                {"t_s": round(elapsed, 4), "kind": "pressure", "value": pressure_pa}
            )

            if elapsed <= BASELINE_SECONDS:
                baseline_pressure.append(pressure_pa)
            elif baseline_pa is not None and trigger is None:
                delta_pa = pressure_pa - baseline_pa
                if abs(delta_pa) >= PRESSURE_TRIGGER_DELTA_PA:
                    trigger = {
                        "t_s": round(elapsed, 4),
                        "pressure_pa": pressure_pa,
                        "delta_pa": delta_pa,
                        "direction": "positive" if delta_pa > 0 else "negative",
                    }
                    trigger_time = elapsed

        elif message_type == "VFR_HUD":
            airspeed_mps = float(message.airspeed)
            if math.isfinite(airspeed_mps):
                samples.append(
                    {
                        "t_s": round(elapsed, 4),
                        "kind": "airspeed",
                        "value": airspeed_mps,
                    }
                )

        if baseline_pa is None and elapsed >= BASELINE_SECONDS:
            if not baseline_pressure:
                raise RuntimeError("no differential-pressure baseline samples")
            baseline_pa = statistics.median(baseline_pressure)
            READY.write_text(
                json.dumps(
                    {
                        "ready_at_utc": datetime.now(timezone.utc).isoformat(),
                        "baseline_pa": baseline_pa,
                        "trigger_delta_pa": PRESSURE_TRIGGER_DELTA_PA,
                        "timeout_s": TIMEOUT_SECONDS,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        if trigger_time is not None and elapsed - trigger_time >= POST_TRIGGER_SECONDS:
            break
finally:
    for message_id in (29, 74):
        try:
            set_interval(message_id, 0)
        except Exception:
            pass
    try:
        connection.close()
    except Exception:
        pass
    READY.unlink(missing_ok=True)

pressure = [float(s["value"]) for s in samples if s["kind"] == "pressure"]
airspeed = [float(s["value"]) for s in samples if s["kind"] == "airspeed"]


def extrema(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


result = {
    "event": "px4_airspeed_live_trigger",
    "captured_at_utc": datetime.now(timezone.utc).isoformat(),
    "duration_s": time.monotonic() - start,
    "identity": {
        "system_id": heartbeat.get_srcSystem(),
        "component_id": heartbeat.get_srcComponent(),
        "armed": bool(heartbeat.base_mode & 128),
    },
    "baseline_pressure_pa": baseline_pa,
    "trigger_threshold_delta_pa": PRESSURE_TRIGGER_DELTA_PA,
    "trigger": trigger,
    "timed_out": trigger is None,
    "message_counts": message_counts,
    "differential_pressure_pa": extrema(pressure),
    "airspeed_mps": extrema(airspeed),
    "samples": samples,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k: v for k, v in result.items() if k != "samples"}, ensure_ascii=False, indent=2))
