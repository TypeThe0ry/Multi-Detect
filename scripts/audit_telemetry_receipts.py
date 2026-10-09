"""Correlate userspace QPC telemetry receipts without asserting send-time latency."""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from multidetect.clock import validate_host_identity  # noqa: E402

KINDS = {"HEARTBEAT": 0, "SYSTEM_TIME": 2, "GLOBAL_POSITION_INT": 33, "VFR_HUD": 74}


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("finite nonnegative receipt timestamp required")
    return value


def digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("exact frame SHA256 required")
    return value


def complete_samples(record):
    rows = record.get("samples")
    if (
        not isinstance(rows, list)
        or len(rows) > 131072
        or type(record.get("samples_seen")) is not int
        or record["samples_seen"] != len(rows)
        or type(record.get("samples_dropped")) is not int
        or record["samples_dropped"] != 0
    ):
        raise ValueError("complete bounded receipt history required")
    return rows


def correlate(trace, history):
    identity = validate_host_identity(trace.get("host_clock_identity"))
    if (
        validate_host_identity(history.get("host_clock_identity")) != identity
        or history.get("receive_clock") != "host_perf_counter_since_collector_start"
    ):
        raise ValueError("same QPC clock identity required")
    router = complete_samples(trace)
    samples = complete_samples(history)
    origin = finite(history.get("receive_origin_host_monotonic_s"))
    by_hash = defaultdict(list)
    previous = -1
    for row in router:
        start, end = finite(row["router_decoded_host_s"]), finite(row["router_forwarded_host_s"])
        if start < previous or end < start:
            raise ValueError("ordered router timestamps required")
        previous = start
        by_hash[digest(row["frame_sha256"])].append(row)
    counts = Counter(s.get("frame_sha256") for s in samples)
    matched = []
    ambiguous = missing = 0
    last_by_kind = {}
    gaps = []
    consecutive_gaps = []
    previous_load = {}
    previous = -1
    for row in samples:
        at = finite(row["at_s"])
        if at < previous:
            raise ValueError("ordered collector timestamps required")
        previous = at
        key = row.get("frame_sha256")
        if row.get("type") in ("HEARTBEAT", "VFR_HUD"):
            group = (row["type"], row.get("source_system"), row.get("source_component"))
            if group in previous_load:
                preceding = previous_load[group]
                consecutive_gaps.append({
                    "type": row["type"], "from_at_s": preceding["at_s"], "to_at_s": at,
                    "collector_gap_s": at - preceding["at_s"],
                    "from_sha256": preceding.get("frame_sha256"), "to_sha256": key,
                    "pump_started_host_s": row.get("pump_started_host_s"),
                    "previous_pump_finished_host_s": row.get("previous_pump_finished_host_s"),
                })
            previous_load[group] = row
        if key is None:
            missing += 1
            continue
        digest(key)
        candidates = by_hash.get(key, [])
        if len(candidates) > 1 or counts[key] > 1:
            ambiguous += 1
            continue
        if not candidates:
            missing += 1
            continue
        routed = candidates[0]
        if KINDS.get(row["type"]) != routed["message_id"] or any(
            type(row.get(k)) is not int or row[k] != routed.get(k)
            for k in ("source_system", "source_component")
        ):
            raise ValueError("matched frame metadata mismatch")
        observed = origin + at
        decoded = routed["router_decoded_host_s"]
        if observed < decoded:
            raise ValueError("collector processed before router decoded matching frame")
        start = row.get("pump_started_host_s")
        end_previous = row.get("previous_pump_finished_host_s")
        if start is not None and finite(start) > observed:
            raise ValueError("pump started after processing")
        if end_previous is not None:
            finite(end_previous)
            if start is not None and end_previous > start:
                raise ValueError("previous pump ended after next start")
        item = {
            "frame_sha256": key,
            "type": row["type"],
            "at_s": at,
            "router_decode_to_collect_s": observed - decoded,
            "router_forward_to_collect_s": observed - routed["router_forwarded_host_s"],
            "between_pumps_s": start - end_previous
            if start is not None and end_previous is not None
            else None,
            "inside_pump_to_collect_s": observed - start if start is not None else None,
        }
        matched.append(item)
        group = (row["type"], row["source_system"], row["source_component"])
        if group in last_by_kind:
            prev_at, prev_decoded, prev_hash = last_by_kind[group]
            gaps.append(
                {
                    "type": row["type"],
                    "from_sha256": prev_hash,
                    "to_sha256": key,
                    "from_at_s": prev_at,
                    "to_at_s": at,
                    "collector_gap_s": at - prev_at,
                    "router_decode_gap_s": decoded - prev_decoded,
                }
            )
        last_by_kind[group] = (at, decoded, key)
    return {
        "schema_version": 1,
        "host_clock_identity": identity,
        "router_records": len(router),
        "collector_records": len(samples),
        "unique_matches": len(matched),
        "ambiguous_records": ambiguous,
        "missing_or_unavailable_records": missing,
        "largest_decode_to_collect": sorted(
            matched, key=lambda item: item["router_decode_to_collect_s"], reverse=True
        )[:20],
        "largest_load_message_gaps": sorted(
            [g for g in gaps if g["type"] in ("HEARTBEAT", "VFR_HUD")],
            key=lambda item: item["collector_gap_s"],
            reverse=True,
        )[:20],
        "gap_scope": "between uniquely matched records; not necessarily consecutive messages",
        "largest_consecutive_collector_load_gaps": sorted(
            consecutive_gaps, key=lambda item: item["collector_gap_s"], reverse=True
        )[:20],
        "forward_delta_can_be_negative": "receiver can process before sender records send return",
        "source_send_timing_claimed": False,
        "kernel_receive_timing_claimed": False,
        "network_latency_claimed": False,
        "gap_root_cause_proven": False,
    }


def read_bound(path, expected):
    digest(expected)
    with path.open("rb") as stream:
        raw = stream.read(128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("input size/hash mismatch")
    return strict_json_loads(raw.decode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("outer", "mission", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("outer", "mission"):
        parser.add_argument("--" + name + "-sha256", required=True)
    args = parser.parse_args()
    outer = read_bound(args.outer, args.outer_sha256)
    mission = read_bound(args.mission, args.mission_sha256)
    if outer["container"]["id"] != mission["container"]["id"]:
        raise ValueError("container identity mismatch")
    trace = outer["router"]["result"]["telemetry_receipts"]
    if trace["host_clock_identity"] != outer["host_clock_identity"]:
        raise ValueError("router differs from parent clock")
    result = correlate(trace, mission["flight"]["post_mission_observation"]["energy_input_history"])
    result["inputs"] = {"outer_sha256": args.outer_sha256, "mission_sha256": args.mission_sha256}
    with args.output.open("x", encoding="utf-8") as stream:
        import json

        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(f"unique={result['unique_matches']} ambiguous={result['ambiguous_records']}")


if __name__ == "__main__":
    main()
