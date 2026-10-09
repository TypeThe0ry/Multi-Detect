"""Audit hash-bound Mission clock receipts without inferring clock synchronization."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from bisect import bisect_left, bisect_right
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.serialization import strict_json_loads  # noqa: E402
from scripts.clock_offset_probe import describe_probes, validate_host_identity  # noqa: E402
from scripts.gazebo_clock_capture import describe_clock_progress  # noqa: E402


def read_bound(path, digest):
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("exact lowercase SHA256 required")
    with Path(path).open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("artifact size/hash mismatch")
    return raw


def audit_receipts(
    rows, start_s, end_s, *, host_clock_identity=None, max_receipt_gap_ns=None
):
    if max_receipt_gap_ns is not None and (
        type(max_receipt_gap_ns) is not int or not 1 <= max_receipt_gap_ns <= 3_600_000_000_000
    ):
        raise ValueError("receipt gap budget must be integer ns in 1..3600000000000")
    if host_clock_identity is not None:
        validate_host_identity(host_clock_identity)
    if not 2 <= len(rows) <= 10000:
        raise ValueError("2..10000 clock receipts required")
    for value in (start_s, end_s):
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError("finite nonnegative host time required")
    if start_s >= end_s:
        raise ValueError("positive Mission window required")
    hosts, clocks = [], []
    fields = {
        "schema_version",
        "source",
        "topic",
        "sequence",
        "sim_ns",
        "real_ns",
        "system_ns",
        "container_receive_monotonic_ns",
        "host_px4_alignment_claimed",
    }
    for i, row in enumerate(rows):
        envelope_fields = {"host_receive_monotonic_ns", "clock"}
        if host_clock_identity is not None:
            envelope_fields.add("host_clock_identity")
        if not isinstance(row, dict) or set(row) != envelope_fields:
            raise ValueError("invalid receipt envelope")
        if row.get("host_clock_identity") != host_clock_identity:
            raise ValueError("receipt host clock identity mismatch")
        host, clock = row["host_receive_monotonic_ns"], row["clock"]
        if type(host) is not int or host < 0 or (hosts and host <= hosts[-1]):
            raise ValueError("strictly increasing integer host receipts required")
        if not isinstance(clock, dict) or set(clock) != fields:
            raise ValueError("invalid clock fields")
        if (
            type(clock["schema_version"]) is not int
            or clock["schema_version"] != 1
            or clock["source"] != "gz.msgs.Clock"
            or clock["topic"] != "/world/default/clock"
            or type(clock["sequence"]) is not int
            or clock["sequence"] != i
            or clock["host_px4_alignment_claimed"] is not False
        ):
            raise ValueError("clock identity/sequence/claim mismatch")
        hosts.append(host)
        clocks.append(clock)
    summary = describe_clock_progress(clocks)
    # Mission timestamps were recorded as float seconds. Keep the original
    # value and explicitly label rounding; raw receipt/source ns stay integers.
    start, end = round(start_s * 1e9), round(end_s * 1e9)

    def bracket(event):
        left, right = bisect_left(hosts, event) - 1, bisect_right(hosts, event)
        if left < 0 or right >= len(hosts):
            return None
        return {
            "before_sequence": left,
            "after_sequence": right,
            "host_gap_ns": hosts[right] - hosts[left],
            "receipt_associated_sim_ns": [clocks[left]["sim_ns"], clocks[right]["sim_ns"]],
        }

    gaps = {
        "host_receive_monotonic_ns": [b - a for a, b in zip(hosts, hosts[1:], strict=False)],
        **{
            key: [b[key] - a[key] for a, b in zip(clocks, clocks[1:], strict=False)]
            for key in ("sim_ns", "real_ns", "container_receive_monotonic_ns")
        },
    }
    brackets = {"arm": bracket(start), "completed": bracket(end)}
    covered = all(value is not None for value in brackets.values())
    # Include both strict boundary brackets, including neighbors of an exact
    # timestamp match. Gaps outside this bounded window do not qualify it.
    window_gaps = (
        gaps["host_receive_monotonic_ns"][
            brackets["arm"]["before_sequence"] : brackets["completed"]["after_sequence"]
        ]
        if covered
        else []
    )
    maximum_window_gap = max(window_gaps) if window_gaps else None
    continuity_status = (
        "insufficient_coverage"
        if not covered
        else "budget_not_supplied"
        if max_receipt_gap_ns is None
        else "gap_exceeded"
        if maximum_window_gap > max_receipt_gap_ns
        else "passed"
    )
    return {
        "schema_version": 1,
        **({"host_clock_identity": host_clock_identity} if host_clock_identity else {}),
        "clock_progress": summary,
        "host_window_s": [start_s, end_s],
        "event_ns_conversion": "round(original_float_seconds * 1e9)",
        "strict_receipt_brackets": brackets,
        "receipt_window_covered": covered,
        "receipt_continuity": {
            "status": continuity_status,
            "max_receipt_gap_budget_ns": max_receipt_gap_ns,
            "maximum_window_gap_ns": maximum_window_gap,
            "window_gap_count": len(window_gaps),
            "qualified": continuity_status == "passed",
            "scope": "host receipt sampling including strict boundary brackets",
            "observation_freshness_claimed": False,
            "source_alignment_claimed": False,
        },
        "maximum_sample_gaps_ns": {key: max(value) for key, value in gaps.items()},
        "sim_pause_intervals": sum(value == 0 for value in gaps["sim_ns"]),
        "clock_synchronization_claimed": False,
        "source_event_time_bounds_claimed": False,
        "transport_latency_bound_claimed": False,
        "training_label_eligible": False,
    }


def audit_files(outer_path, outer_sha, clock_path, mission_path, *, max_receipt_gap_ns=None):
    outer = strict_json_loads(read_bound(outer_path, outer_sha).decode("utf-8"))
    if outer.get("gate_passed") is not True or outer["gates"].get("clock_capture") is not True:
        raise ValueError("successful clock capture run required")
    capture = outer["clock_capture"]
    if capture["exit_code"] != 0 or capture["host_px4_alignment_claimed"] is not False:
        raise ValueError("invalid capture terminal state")
    clock_raw = read_bound(clock_path, capture["sha256"])
    if len(clock_raw) != capture["bytes"]:
        raise ValueError("clock byte count mismatch")
    rows = [strict_json_loads(line) for line in clock_raw.decode("utf-8").splitlines()]
    # Resolve a caller-supplied Mission file only; never follow paths embedded
    # in a report. Its bytes must match one of the parent-verified sorties.
    with Path(mission_path).open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("mission report too large")
    mission_sha = hashlib.sha256(raw).hexdigest()
    if "campaign" in outer:
        entries = outer["campaign"]["verified_sorties"]
        if sum(entry["report_sha256"] == mission_sha for entry in entries) != 1:
            raise ValueError("Mission not uniquely bound to campaign")
    else:
        final = outer.get("final_integrity", {})
        if (
            outer.get("mission", {}).get("file_sha256") != mission_sha
            or final.get("hashes", {}).get("mission_evidence") != mission_sha
            or final.get("revalidated_after_mission_and_cleanup") is not True
            or outer["gates"].get("complete_vtol_mission") is not True
            or outer["gates"].get("final_artifact_integrity") is not True
        ):
            raise ValueError("Mission not bound to revalidated single capture")
    mission = strict_json_loads(raw.decode("utf-8"))
    if (
        mission.get("gate_passed") is not True
        or mission.get("failure") is not None
        or mission["container"]["id"] != capture["container_id"]
    ):
        raise ValueError("Mission status/container mismatch")
    window = mission["mission_receive_window"]
    identity = capture.get("host_clock_identity")
    if identity is not None:
        validate_host_identity(identity)
        if (
            window.get("host_clock_identity") != identity
            or mission["flight"].get("host_clock_identity") != identity
            or window.get("clock") != "host_perf_counter_since_collector_start"
        ):
            raise ValueError("Mission and capture clock domains differ")
        offset = describe_probes(capture["host_container_probes"])
        if (
            offset != capture["host_container_offset"]
            or offset.get("host_clock_identity") != identity
        ):
            raise ValueError("capture probe identity/summary mismatch")
    elif (
        window.get("host_clock_identity", {}).get("source") == "python.perf_counter"
        or "host_clock_identity" in capture
    ):
        raise ValueError("missing capture host clock identity")
    if window["completed_host_monotonic_s"] != mission["flight"]["completed_host_monotonic_s"]:
        raise ValueError("completion time mismatch")
    result = audit_receipts(
        rows,
        window["arm_request_host_monotonic_s"],
        window["completed_host_monotonic_s"],
        host_clock_identity=identity,
        max_receipt_gap_ns=max_receipt_gap_ns,
    )
    if result["clock_progress"] != capture["summary"]:
        raise ValueError("clock summary mismatch")
    result["inputs"] = {
        "outer_sha256": outer_sha,
        "clock_sha256": capture["sha256"],
        "mission_sha256": mission_sha,
        "container_id": capture["container_id"],
    }
    result["mission_journal_revalidated"] = False
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("outer", "clock", "mission", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--outer-sha256", required=True)
    parser.add_argument(
        "--max-receipt-gap-ns", type=int,
        help="Explicit host receipt sampling budget; does not qualify source synchronization",
    )
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError("output must be new")
    result = audit_files(
        args.outer, args.outer_sha256, args.clock, args.mission,
        max_receipt_gap_ns=args.max_receipt_gap_ns,
    )
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
