"""Summarize bounded, existing MAVLink logs; never treats unknown current as zero watts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


def numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def summarize(logs: list[Path]) -> dict:
    counts: Counter = Counter()
    fields: dict[str, Counter] = defaultdict(Counter)
    powers: dict[str, list[float]] = defaultdict(list)
    phases: Counter = Counter()
    sources = []
    malformed = 0
    for path in logs:
        digest = hashlib.sha256()
        local: Counter = Counter()
        phase = "unknown"
        battery_times = []
        battery_soc = []
        opener = gzip.open if path.suffix.lower() == ".gz" else open
        for raw in opener(path, "rb"):
            digest.update(raw)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            kind = record.get("message_type", record.get("mavpackettype", "unknown"))
            counts[kind] += 1
            local[kind] += 1
            if kind == "LOCAL_POSITION_NED":
                vx, vy, vz = (record.get(key) for key in ("vx", "vy", "vz"))
                if all(numeric(value) for value in (vx, vy, vz)):
                    phase = (
                        "climb"
                        if vz < -0.3
                        else "descent"
                        if vz > 0.3
                        else "translate"
                        if math.hypot(vx, vy) > 0.5
                        else "hover"
                    )
                    phases[phase] += 1
            if kind not in {"BATTERY_STATUS", "SYS_STATUS"}:
                continue
            for field in (
                "current_battery",
                "current_consumed",
                "energy_consumed",
                "battery_remaining",
                "time_remaining",
                "voltage_battery",
            ):
                if numeric(record.get(field)):
                    fields[f"{kind}.{field}"][record[field]] += 1
            voltage = None
            if kind == "BATTERY_STATUS":
                cells = [v for v in record.get("voltages", []) if numeric(v) and 0 < v < 65535]
                if cells:
                    voltage = sum(cells) / 1000
                    fields["BATTERY_STATUS.pack_voltage_v"][voltage] += 1
                stamp = record.get("receive_monotonic_ns")
                soc = record.get("battery_remaining")
                if numeric(stamp) and numeric(soc) and 0 <= soc <= 100:
                    battery_times.append(stamp / 1e9)
                    battery_soc.append(soc)
            elif numeric(record.get("voltage_battery")):
                if 0 < record["voltage_battery"] < 65535:
                    voltage = record["voltage_battery"] / 1000
            current = record.get("current_battery")
            if voltage is not None and numeric(current) and current >= 0:
                powers[phase].append(voltage * current / 100)
        duration = battery_times[-1] - battery_times[0] if len(battery_times) > 1 else None
        sources.append(
            {
                "path": str(path.resolve()),
                "sha256": digest.hexdigest(),
                "bytes": path.stat().st_size,
                "battery_status_samples": local["BATTERY_STATUS"],
                "sys_status_samples": local["SYS_STATUS"],
                "battery_span_s": round(duration, 6) if duration else None,
                "first_soc_percent": battery_soc[0] if battery_soc else None,
                "last_soc_percent": battery_soc[-1] if battery_soc else None,
            }
        )
    field_report = {}
    for field, values in sorted(fields.items()):
        field_report[field] = {
            "samples": sum(values.values()),
            "min": min(values),
            "max": max(values),
            "unique_values": len(values),
            "most_common": [{"value": v, "count": n} for v, n in values.most_common(5)],
        }
    return {
        "schema_version": 1,
        "log_count": len(logs),
        "sources": sources,
        "message_counts": dict(sorted(counts.items())),
        "malformed_records": malformed,
        "battery_fields": field_report,
        "phase_classification": {
            "source": "LOCAL_POSITION_NED velocities; all states, not restricted to armed",
            "thresholds": "climb vz<-0.3; descent vz>0.3; translate hypot(vx,vy)>0.5; else hover",
            "sample_counts": dict(phases),
        },
        "phase_power_w": {
            phase: {
                "samples": len(powers[phase]),
                "mean": sum(powers[phase]) / len(powers[phase]) if powers[phase] else None,
            }
            for phase in ("hover", "translate", "climb", "descent", "unknown")
        },
        "calibration_status": (
            "measured_current_available"
            if any(powers.values())
            else "unavailable_no_valid_current_or_consumed_energy"
        ),
        "caveats": [
            "PX4 SITL battery is simulated: SOC/voltage are not aircraft power calibration.",
            "current_battery=-1 and energy_consumed=-1 are unknown, not zero.",
            "current_consumed=0 with unknown current cannot calibrate power or capacity.",
            "time_remaining=0 means unavailable. WIND_COV may not be published.",
            "SYS_STATUS and BATTERY_STATUS SOC may differ due to rounding/publication timing.",
            "Never infer cell count solely from voltages array: "
            "PX4 may place pack voltage in slot 0.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-logs", type=int, default=16)
    args = parser.parse_args()
    if args.max_logs < 1:
        parser.error("--max-logs must be positive")
    logs = sorted(
        [*args.input_root.rglob("mavlink.jsonl"), *args.input_root.rglob("mavlink.jsonl.gz")]
    )[: args.max_logs]
    if not logs:
        parser.error("no mavlink.jsonl files found")
    output = args.output.resolve()
    workspace = Path(__file__).resolve().parents[1]
    if not output.is_relative_to(workspace):
        parser.error("--output must be inside this worktree")
    report = summarize(logs)
    report["selection"] = {
        "input_root": str(args.input_root.resolve()),
        "max_logs": args.max_logs,
        "order": "lexicographic relative paths; bounded prefix",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"output": str(output), "logs": len(logs), "status": report["calibration_status"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
