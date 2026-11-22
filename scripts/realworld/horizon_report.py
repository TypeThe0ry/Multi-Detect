#!/usr/bin/env python3
"""Offline per-frame C11 horizon acceptance. No runtime is started."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from multidetect.sim_teacher.horizon_stabilization import (  # noqa: E402
    AXES, C11_LIMITS, frame_feasibility, rotation_deg,
)

# Pinned Eulerf bridge restrictions, distinct from the SDF mechanical stops.
LEGACY_LIMITS = {**C11_LIMITS, "pitch": (-89.9, 45.0), "yaw": (-179.9, 179.9)}


def classify_frame(row):
    state = row.get("gimbal_state") or {}
    stabilization = state.get("stabilization") or {}
    try:
        body = {a: row["attitude"][a + "_deg"] for a in AXES}
        desired = stabilization["desired_earth_deg"]
        joints = {a: math.degrees(state["joint_position_rad"][a]) for a in AXES}
        if not all(type(v) in (int, float) and math.isfinite(v)
                   for values in (body, desired, joints) for v in values.values()):
            return None
        return frame_feasibility(body, rotation_deg(desired), joints,
                                 limits=stabilization.get("limits_deg", LEGACY_LIMITS))
    except (KeyError, TypeError, ValueError):
        return None


VTOL_STATES = {1: "transition_to_fw", 2: "transition_to_mc", 3: "mc", 4: "fw"}
IOWA_MIN_FRAMES = {"mc_hover": 2, "banked_fw": 2, "transition_to_fw": 2,
                   "transition_to_mc": 2}
HORIZON_P95_DEG = 4.0
HORIZON_MAX_DEG = 8.0
HORIZON_DEGRADED_MAX_FRACTION = 0.05
HORIZON_UNAVAILABLE_MAX_FRACTION = 0.15
TRACK_P95_DEG = 2.0
TRACK_MAX_DEG = 5.0


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def summarize(path: Path, *, threshold_deg=HORIZON_P95_DEG, require_iowa_coverage=False,
              expected_mode=None, track_action_index=None):
    if not 0 <= threshold_deg <= HORIZON_P95_DEG:
        raise ValueError(f"P95 threshold must be in [0, {HORIZON_P95_DEG}] degrees")
    if expected_mode not in (None, "lock", "track"):
        raise ValueError("expected_mode must be lock, track or None")
    if path.is_dir():
        path = path / "video" / "manifest.jsonl"
    groups = {mode: [] for mode in ("lock", "follow", "track")}
    mode_counts = dict.fromkeys(groups, 0)
    track_missing = track_stale = track_reachable = track_unreachable = track_unknown = 0
    track_action_frames = 0
    track_horizon_feasible = track_horizon_infeasible = track_horizon_unknown = 0
    track_errors, track_unreachable_errors = [], []
    track_coverage = dict.fromkeys(IOWA_MIN_FRAMES, 0)
    horizon_stale = 0
    frames = missing = stale = degraded = 0
    truncated = False
    stages = {}
    vtol_states = {}
    coverage = dict.fromkeys(IOWA_MIN_FRAMES, 0)
    vertical_mc_frames = 0
    feasible_errors = []
    infeasible_errors = []
    feasibility_unknown = saturated = mechanical_infeasible = command_infeasible = 0
    saturated_axes = dict.fromkeys(AXES, 0)
    feasible_stages = {}
    horizon_proven = horizon_unknown = 0
    diagnostic_horizon_errors = []
    horizon_feasible = horizon_infeasible = horizon_degraded = horizon_missing = 0
    horizon_degraded_unknown = yaw_degraded_unknown = 0
    horizon_errors = []
    yaw_degraded_frames = 0
    yaw_errors = []
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
            if mode in mode_counts:
                mode_counts[mode] += 1
            if mode in groups and (
                state.get("fresh") is not True or stabilization.get("measurement_fresh") is not True
            ):
                stale += 1
            if (stabilization.get("control") or {}).get("degraded") is True:
                degraded += 1
            if mode == "track":
                if track_action_index is not None and row.get("action_index") == track_action_index:
                    track_action_frames += 1
                pointing = stabilization.get("pointing_error_deg")
                reachable = stabilization.get("target_reachable")
                horizon_possible = stabilization.get("horizon_feasible_given_target")
                valid_pointing = finite_number(pointing) and 0 <= pointing <= 180
                valid_horizon = error is None or (finite_number(error) and error >= 0)
                valid_track = (
                    valid_pointing and type(reachable) is bool
                    and bool(stabilization.get("target_ref"))
                    and "horizon_error_deg" in stabilization and valid_horizon
                    and "horizon_feasible_given_target" in stabilization
                    and (type(horizon_possible) is bool or horizon_possible is None)
                )
                track_missing += not valid_track
                missing += not valid_track
                fresh = state.get("fresh") is True and stabilization.get("measurement_fresh") is True
                track_stale += not fresh
                track_reachable += reachable is True
                track_unreachable += reachable is False
                track_unknown += type(reachable) is not bool
                track_horizon_feasible += horizon_possible is True
                track_horizon_infeasible += horizon_possible is False
                track_horizon_unknown += type(horizon_possible) is not bool
                if valid_pointing:
                    if reachable is True:
                        track_errors.append(pointing)
                    elif reachable is False:
                        track_unreachable_errors.append(pointing)
                if finite_number(error) and error >= 0:
                    groups["track"].append(error)
                if fresh:
                    attitude = row.get("attitude") or {}
                    roll = attitude.get("roll_deg")
                    vtol = VTOL_STATES.get((row.get("flight_state") or {}).get("vtol_state"))
                    if vtol in {"transition_to_fw", "transition_to_mc"}:
                        track_coverage[vtol] += 1
                    elif vtol == "mc":
                        track_coverage["mc_hover"] += 1
                    elif vtol == "fw" and finite_number(roll) and 25 <= abs(roll) <= 180:
                        track_coverage["banked_fw"] += 1
                continue
            horizon_stale += mode in groups and (
                state.get("fresh") is not True or stabilization.get("measurement_fresh") is not True
            )
            valid_measurement = mode in groups and finite_number(error) and error >= 0
            reachability = stabilization.get("horizon_feasible")
            valid_reachability = "horizon_feasible" in stabilization and (
                type(reachability) is bool or reachability is None or reachability == "unknown"
            )
            frame_degraded = stabilization.get("horizon_degraded")
            yaw_degraded = stabilization.get("yaw_degraded")
            yaw_error = stabilization.get("yaw_error_deg")
            missing_degradation = reachability is True and type(frame_degraded) is not bool
            missing_yaw = type(yaw_degraded) is not bool
            horizon_degraded_unknown += missing_degradation
            yaw_degraded_unknown += missing_yaw
            if (
                not valid_measurement or not valid_reachability
                or missing_degradation or missing_yaw
                or (yaw_degraded is True and not finite_number(yaw_error))
            ):
                horizon_missing += 1
            if reachability is True:
                horizon_feasible += 1
                horizon_degraded += frame_degraded is True
                if mode == "lock" and valid_measurement:
                    horizon_errors.append(error)
            elif reachability is False:
                horizon_infeasible += 1
            if yaw_degraded is True:
                yaw_degraded_frames += 1
                if finite_number(yaw_error):
                    yaw_errors.append(abs(yaw_error))
            if not valid_measurement:
                missing += 1
                continue
            groups[mode].append(error)
            classification = classify_frame(row)
            if classification is None:
                feasibility_unknown += 1
            else:
                horizon_proven += classification["horizon_feasible"] is True
                if mode == "lock" and classification["horizon_feasible"] is True:
                    diagnostic_horizon_errors.append(error)
                horizon_unknown += classification["horizon_feasible"] is None
                saturated += classification["saturated"]
                for axis in classification["saturated_axes"]:
                    saturated_axes[axis] += 1
                mechanical_infeasible += not classification["mechanical_feasible"]
                command_infeasible += not classification["command_feasible"]
                if mode == "lock":
                    if classification["target_pose_feasible"]:
                        feasible_errors.append(error)
                        feasible_stages.setdefault(row.get("flight_stage", "unknown"), []).append(error)
                    else:
                        infeasible_errors.append(error)
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
    legacy_strict_passed = (
        bool(lock) and not mode_counts["follow"] and not mode_counts["track"] and not truncated
        and missing == stale == 0 and max(lock) <= threshold_deg
        and (coverage_ok or not require_iowa_coverage)
    )
    recommended_passed = (
        bool(feasible_errors) and not mode_counts["follow"] and not mode_counts["track"] and not truncated
        and missing == stale == feasibility_unknown == 0
        and percentile(feasible_errors, 0.95) <= threshold_deg
        and (coverage_ok or not require_iowa_coverage)
    )
    horizon_stats = stats(horizon_errors)
    horizon_frames = frames - mode_counts["track"]
    horizon_unavailable = horizon_frames - horizon_feasible
    unavailable_fraction = horizon_unavailable / horizon_frames if horizon_frames else None
    degraded_fraction = horizon_degraded / horizon_feasible if horizon_feasible else None
    gate_errors = []
    if not horizon_frames or horizon_missing:
        gate_errors.append("horizon_data_missing")
    if horizon_stale:
        gate_errors.append("horizon_measurement_stale")
    if mode_counts["follow"]:
        gate_errors.append("horizon_follow_frames")
    if expected_mode == "lock" and mode_counts["track"]:
        gate_errors.append("horizon_unexpected_track_frames")
    if truncated:
        gate_errors.append("horizon_manifest_truncated")
    if not horizon_errors:
        gate_errors.append("horizon_no_feasible_frames")
    else:
        if horizon_stats["p95_deg"] > threshold_deg:
            gate_errors.append("horizon_feasible_p95_exceeded")
        if horizon_stats["max_deg"] > HORIZON_MAX_DEG:
            gate_errors.append("horizon_feasible_max_exceeded")
    if degraded_fraction is not None and degraded_fraction > HORIZON_DEGRADED_MAX_FRACTION:
        gate_errors.append("horizon_degraded_fraction_exceeded")
    if unavailable_fraction is not None and unavailable_fraction > HORIZON_UNAVAILABLE_MAX_FRACTION:
        gate_errors.append("horizon_unavailable_fraction_exceeded")
    if require_iowa_coverage and not coverage_ok:
        gate_errors.append("horizon_iowa_coverage_missing")
    horizon_acceptance = {
        "passed": not gate_errors,
        "errors": gate_errors,
        "criteria": {
            "feasible_p95_max_deg": threshold_deg,
            "feasible_max_deg": HORIZON_MAX_DEG,
            "feasible_degraded_max_fraction": HORIZON_DEGRADED_MAX_FRACTION,
            "unavailable_max_fraction": HORIZON_UNAVAILABLE_MAX_FRACTION,
            "require_iowa_coverage": require_iowa_coverage,
        },
        "frame_count": horizon_frames,
        "feasible_frames": horizon_feasible,
        "feasible_error": horizon_stats,
        "infeasible_frames": horizon_infeasible,
        "unknown_frames": horizon_unavailable - horizon_infeasible,
        "unavailable_frames": horizon_unavailable,
        "unavailable_fraction": unavailable_fraction,
        "degraded_frames": horizon_degraded,
        "degraded_fraction_of_feasible": degraded_fraction if not horizon_degraded_unknown else None,
        "missing_frames": horizon_missing,
        "stale_frames": horizon_stale,
        "yaw_degraded_frames": yaw_degraded_frames,
        "yaw_degraded_fraction": (yaw_degraded_frames / horizon_frames
                                  if horizon_frames and not yaw_degraded_unknown else None),
        "yaw_degraded_absolute_error": stats(yaw_errors),
    }
    track_stats = stats(track_errors)
    track_gate_errors = []
    if not mode_counts["track"] or track_missing:
        track_gate_errors.append("track_data_missing")
    if track_stale:
        track_gate_errors.append("track_measurement_stale")
    if truncated:
        track_gate_errors.append("track_manifest_truncated")
    if expected_mode == "track" and frames != mode_counts["track"]:
        track_gate_errors.append("track_unexpected_mode_frames")
    if not track_errors:
        track_gate_errors.append("track_no_reachable_frames")
    else:
        if track_stats["p95_deg"] > TRACK_P95_DEG:
            track_gate_errors.append("track_reachable_p95_exceeded")
        if track_stats["max_deg"] > TRACK_MAX_DEG:
            track_gate_errors.append("track_reachable_max_exceeded")
    track_coverage_ok = all(track_coverage[phase] >= count for phase, count in IOWA_MIN_FRAMES.items())
    if require_iowa_coverage and not track_coverage_ok:
        track_gate_errors.append("track_iowa_coverage_missing")
    track_acceptance = {
        "passed": not track_gate_errors,
        "errors": track_gate_errors,
        "criteria": {"reachable_p95_max_deg": TRACK_P95_DEG, "reachable_max_deg": TRACK_MAX_DEG,
                     "horizon_is_diagnostic_only": True, "require_iowa_coverage": require_iowa_coverage},
        "frame_count": mode_counts["track"],
        **({"eval_action_frames": track_action_frames} if track_action_index is not None else {}),
        "reachable_frames": track_reachable,
        "unreachable_frames": track_unreachable,
        "unknown_frames": track_unknown,
        "reachable_pointing_error": track_stats,
        "unreachable_pointing_error": stats(track_unreachable_errors),
        "horizon_error": stats(groups["track"]),
        "horizon_feasible_given_target_frames": track_horizon_feasible,
        "horizon_infeasible_given_target_frames": track_horizon_infeasible,
        "horizon_unknown_given_target_frames": track_horizon_unknown,
        "missing_frames": track_missing,
        "stale_frames": track_stale,
        "iowa_coverage": track_coverage,
        "iowa_coverage_passed": track_coverage_ok,
    }
    if expected_mode == "track":
        passed = track_acceptance["passed"]
    elif expected_mode == "lock":
        passed = horizon_acceptance["passed"]
    else:
        passed = ((not horizon_frames or horizon_acceptance["passed"])
                  and (not mode_counts["track"] or track_acceptance["passed"]) and bool(frames))
    return {
        "horizon_acceptance": horizon_acceptance,
        "track_acceptance": track_acceptance,
        "expected_mode": expected_mode,
        "mode_frame_counts": mode_counts,
        "horizon_only_lock": stats(diagnostic_horizon_errors),
        "horizon_only_p95_passed": (
            bool(diagnostic_horizon_errors) and not mode_counts["follow"] and not mode_counts["track"] and not truncated
            and missing == stale == feasibility_unknown == horizon_unknown == 0
            and percentile(diagnostic_horizon_errors, 0.95) <= threshold_deg
            and (coverage_ok or not require_iowa_coverage)
        ),
        "feasibility_scope": "full target pose including yaw; horizon-only may remain reachable",
        "horizon_only_proven_feasible_frames": horizon_proven,
        "horizon_only_unknown_frames": horizon_unknown,
        "feasible_lock": stats(feasible_errors),
        "infeasible_lock": stats(infeasible_errors),
        "feasible_flight_stages": {k: stats(v) for k, v in feasible_stages.items()},
        "feasibility_unknown_frames": feasibility_unknown,
        "mechanical_infeasible_frames": mechanical_infeasible,
        "command_infeasible_frames": command_infeasible,
        "saturated_frames": saturated,
        "saturated_axes": saturated_axes,
        "recommended_passed": recommended_passed,
        "recommended_criteria": {
            "feasible_lock_p95_max_deg": threshold_deg,
            "require_nonempty_feasible_lock": True,
            "require_complete_fresh_classifiable_frames": True,
            "exclude_only_static_full_reference_unreachable": True,
            "exclude_degraded_or_rate_limited": False,
            "require_iowa_coverage": require_iowa_coverage,
            "note": "Legacy diagnostic on statically reachable full targets including yaw.",
        },
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
        "legacy_strict_passed": legacy_strict_passed,
        "passed": passed,
        "scope": "recorded frames; lock horizon and track pointing are accepted independently; follow is rejected",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--threshold-deg", type=float, default=HORIZON_P95_DEG,
                        help="stricter P95 error threshold on horizon-feasible frames (default: 4 degrees)")
    parser.add_argument("--require-iowa-coverage", action="store_true")
    parser.add_argument("--expected-mode", choices=("lock", "track"))
    args = parser.parse_args(argv)
    try:
        result = summarize(args.path, threshold_deg=args.threshold_deg,
                           require_iowa_coverage=args.require_iowa_coverage,
                           expected_mode=args.expected_mode)
    except (OSError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
