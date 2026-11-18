#!/usr/bin/env python3
"""Offline per-frame C11 horizon acceptance. No runtime is started."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
from pathlib import Path

VTOL_STATES = {1: "transition_to_fw", 2: "transition_to_mc", 3: "mc", 4: "fw"}
IOWA_MIN_FRAMES = {"mc_hover": 2, "banked_fw": 2, "transition_to_fw": 2,
                   "transition_to_mc": 2}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)


def summarize(path: Path, *, threshold_deg=4.0, require_iowa_coverage=False):
    if not math.isfinite(threshold_deg) or threshold_deg < 0:
        raise ValueError("threshold must be finite and nonnegative")
    if path.is_dir():
        path = path / "video" / "manifest.jsonl"
    groups = {mode: [] for mode in ("lock", "follow")}
    frames = missing = stale = degraded = 0
    truncated = False
    stages = {}
    vtol_states = {}
    coverage = dict.fromkeys(IOWA_MIN_FRAMES, 0)
    vertical_mc_frames = 0
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("event") is not None:
                truncated |= row["event"] == "truncated"
                continue
            frames += 1
            state = row.get("gimbal_state") or {}
            stabilization = state.get("stabilization") or {}
            error = stabilization.get("horizon_error_deg")
            mode = stabilization.get("mode")
            if mode in groups and (
                state.get("fresh") is not True or stabilization.get("measurement_fresh") is not True
            ):
                stale += 1
            if (stabilization.get("control") or {}).get("degraded") is True:
                degraded += 1
            if (
                mode not in groups
                or type(error) not in (float, int)
                or not math.isfinite(error)
                or error < 0
            ):
                missing += 1
                continue
            groups[mode].append(error)
            stages.setdefault(row.get("flight_stage", "unknown"), []).append(error)
            vtol_value = (row.get("flight_state") or {}).get("vtol_state")
            vtol_state = VTOL_STATES.get(vtol_value, "unknown")
            vtol_states.setdefault(vtol_state, []).append(error)
            if mode == "lock" and stabilization.get("measurement_fresh") is True:
                attitude = row.get("attitude") or {}
                roll = attitude.get("roll_deg")
                pitch = attitude.get("pitch_deg")
                if vtol_state in {"transition_to_fw", "transition_to_mc"}:
                    coverage[vtol_state] += 1
                elif vtol_state == "mc":
                    coverage["mc_hover"] += 1
                    if type(pitch) in (float, int) and 80 <= abs(pitch) <= 90.01:
                        vertical_mc_frames += 1
                elif vtol_state == "fw" and type(roll) in (float, int) and 25 <= abs(roll) <= 180:
                    coverage["banked_fw"] += 1

    def stats(values):
        return {
            "count": len(values),
            "mean_deg": statistics.mean(values) if values else None,
            "p50_deg": percentile(values, 0.5),
            "p95_deg": percentile(values, 0.95),
            "max_deg": max(values) if values else None,
        }

    lock = groups["lock"]
    coverage_ok = all(coverage[phase] >= count for phase, count in IOWA_MIN_FRAMES.items())
    passed = (
        bool(lock) and not groups["follow"] and not truncated
        and missing == stale == 0 and max(lock) <= threshold_deg
        and (coverage_ok or not require_iowa_coverage)
    )
    return {
        "manifest": str(path),
        "frame_count": frames,
        "missing_frames": missing,
        "stale_frames": stale,
        "degraded_frames": degraded,
        "truncated": truncated,
        "threshold_deg": threshold_deg,
        "modes": {mode: stats(values) for mode, values in groups.items()},
        "flight_stages": {stage: stats(values) for stage, values in stages.items()},
        "vtol_states": {state: stats(values) for state, values in vtol_states.items()},
        "iowa_coverage": coverage,
        "iowa_coverage_min_frames": IOWA_MIN_FRAMES,
        "iowa_coverage_passed": coverage_ok,
        "vertical_mc_frames": vertical_mc_frames,
        "passed": passed,
        "scope": "recorded frames only; follow is not lock acceptance",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--threshold-deg", type=float, default=4.0)
    parser.add_argument("--require-iowa-coverage", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = summarize(args.path, threshold_deg=args.threshold_deg,
                           require_iowa_coverage=args.require_iowa_coverage)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
