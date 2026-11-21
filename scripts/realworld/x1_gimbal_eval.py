#!/usr/bin/env python3
"""Evaluate X1 episode artifacts offline; discover batch receipts without scanning RGB."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if __name__ == "__main__":
    sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from multidetect.sim_teacher.target_tracking import (  # noqa: E402
    WorldTarget,
    pointing_error_deg,
    unit,
    world_target_direction,
)

from scripts.realworld.horizon_report import finite_number, percentile, summarize  # noqa: E402
from scripts.realworld.x1_scenario_class import WIDE_AREA, WIDE_DISTANCE_M  # noqa: E402

CONFIG = ROOT / "configs/simulation"
PRESETS = {"clear_noon": ("noon", "clear"), "overcast": ("noon", "overcast"),
           "dusk": ("dusk", "clear")}
COMMANDS = {204: "DO_MOUNT_CONFIGURE", 205: "DO_MOUNT_CONTROL",
            1000: "DO_GIMBAL_MANAGER_PITCHYAW", 1001: "DO_GIMBAL_MANAGER_CONFIGURE"}
REJECTIONS = {1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED", 4: "FAILED",
              6: "CANCELLED", 7: "COMMAND_LONG_ONLY", 8: "COMMAND_INT_ONLY",
              9: "COMMAND_UNSUPPORTED_MAV_FRAME"}
ARTIFACT_ERRORS = (OSError, EOFError, ValueError, TypeError, AttributeError, OverflowError)
ACCEPTED_LEDGER_NAMES = ("accepted.jsonl", "accepted.jsonl.gz")


def mapping(value):
    return value if isinstance(value, dict) else {}


def read_object(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def jsonl(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"expected an object: {path}:{line_no}")
                yield line_no, row


def artifact(directory, name):
    return next((p for p in (directory / name, directory / (name + ".gz")) if p.is_file()), None)


def optional_object(path, issues):
    if path is None:
        return {}
    try:
        return read_object(path)
    except ARTIFACT_ERRORS as exc:
        issues.append(f"{path}: {exc}")
        return {}


def expected_grid():
    worlds = read_object(CONFIG / "x1-worlds.json")
    variants = read_object(CONFIG / "x1-variants.json")
    iowa = read_object(CONFIG / "x1-resume.json")["job"]["site"]
    base = (*worlds["sites"], *worlds.get("collection_only_sites", []), iowa)
    return [(site, "noon", "clear") for site in base
            if worlds.get("scenario_classes", {}).get(site, WIDE_AREA) == WIDE_AREA] + [
        (site, row["time_of_day"], row["weather"])
        for site in variants["sites"] for row in variants["conditions"]
    ]


def condition_of(row):
    keys = ("site", "time_of_day", "weather")
    if all(isinstance(row.get(key), str) and row[key] for key in keys):
        return row["site"], row["time_of_day"], row["weather"]
    for key in ("scenario", "world_sdf"):
        if isinstance(row.get(key), str):
            name = Path(row[key]).name.removesuffix(".scenario.json").removesuffix(".sdf")
            site, sep, preset = name.partition("_w6km__")
            if sep and preset in PRESETS:
                return site, *PRESETS[preset]
    return None


def receipt_files(root):
    if root.is_file():
        if root.name not in ACCEPTED_LEDGER_NAMES:
            raise ValueError("--accepted-root file must be accepted.jsonl or accepted.jsonl.gz")
        yield root
    elif root.is_dir():
        def onerror(exc):
            raise exc

        for directory, children, files in os.walk(root, onerror=onerror):
            children[:] = sorted(set(children) - {"jobs", "flight", "episodes"})
            for name in ACCEPTED_LEDGER_NAMES:
                if name in files:
                    yield Path(directory) / name
                    break
    else:
        raise FileNotFoundError(root)


def receipt_rows(path):
    if path.name.endswith((".jsonl", ".jsonl.gz")):
        yield from jsonl(path)
    else:
        yield None, read_object(path)


def attempt_dir(path, row):
    attempt_id = row.get("attempt_id")
    if path.name in ("accepted.jsonl", "accepted.jsonl.gz", "results.jsonl", "results.jsonl.gz"):
        if isinstance(attempt_id, str):
            parts = Path(attempt_id).parts
            if len(parts) == 2 and not Path(attempt_id).is_absolute() and ".." not in parts:
                return (path.parent / "jobs" / attempt_id).resolve()
    if path.name in ("batch-result.json", "batch-result.json.gz"):
        return path.parent.resolve()
    if isinstance(row.get("attempt_dir"), str):
        return (path.parent / row["attempt_dir"]).resolve()
    return None


def bound_to_episode(receipt, episode, meta):
    path, _, row = receipt
    attempt = attempt_dir(path, row)
    if attempt is None or episode.parent != attempt / "flight/episodes":
        return False
    container = mapping(meta.get("simulator_container"))
    return (bool(row.get("container_id")) and row["container_id"] == container.get("container_id")
            and (not row.get("container_name")
                 or row["container_name"] == container.get("container_name")))


def valid_round(row):
    return row.get("valid_round") is True and not row.get("voided_reason")


def qualified(row):
    minimum = row.get("minimum_max_distance_m", WIDE_DISTANCE_M)
    distance = row.get("max_distance_from_spawn_m")
    return (row.get("accepted") is True and valid_round(row) and row.get("route_completed") is True
            and finite_number(minimum) and minimum > 0 and finite_number(distance)
            and distance >= minimum)


def collect_inputs(episode_dirs, accepted_roots, evidence_files, issues):
    entries = {p.resolve(): {} for p in episode_dirs}
    receipts = defaultdict(list)
    files = set()
    for root in accepted_roots:
        try:
            files.update(path.resolve() for path in receipt_files(root))
        except ARTIFACT_ERRORS as exc:
            issues.append(f"{root}: {exc}")
    for path in sorted(files):
        try:
            for line, row in receipt_rows(path):
                if not qualified(row):
                    issues.append(f"{path}:{line}: receipt is not a qualified acceptance")
                    continue
                attempt = attempt_dir(path, row)
                episodes = [] if attempt is None else sorted(
                    p.parent.resolve()
                    for p in (attempt / "flight/episodes").glob("*/episode.json*")
                    if p.name in ("episode.json", "episode.json.gz"))
                if not episodes:
                    issues.append(f"{path}:{line}: accepted episode artifacts not found")
                for episode in set(episodes):
                    entries.setdefault(episode, {})
                    receipts[episode].append((path, line, row))
        except ARTIFACT_ERRORS as exc:
            issues.append(f"{path}: {exc}")

    evidence_receipts = {}
    for path in evidence_files:
        rows = read_object(path).get("episodes")
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected an episodes array")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("episode_dir"), str):
                raise ValueError(f"{path}: each entry needs an episode_dir")
            episode = (path.parent / row["episode_dir"]).resolve()
            entry = entries.setdefault(episode, {})
            for key in ("site", "time_of_day", "weather", "evidence_label"):
                if key in row:
                    if not isinstance(row[key], str) or (key in entry and row[key] != entry[key]):
                        raise ValueError(f"{path}: inconsistent {key} for {episode}")
                    entry[key] = row[key]
            if row.get("receipt_path") is not None:
                source = (path.parent / row["receipt_path"]).resolve()
                if source not in evidence_receipts:
                    evidence_receipts[source] = list(receipt_rows(source))
                receipts[episode].extend((source, line, record)
                                         for line, record in evidence_receipts[source]
                                         if row.get("receipt_line") in (None, line))
    return entries, receipts


def stats(values):
    return {"count": len(values), "p50": percentile(values, .50),
            "p95": percentile(values, .95), "max": max(values, default=None)}


def vector(values, length):
    if not isinstance(values, (list, tuple)) or len(values) != length or not all(
        finite_number(v) for v in values
    ):
        raise ValueError("invalid vector")
    return values


def measured_ray(row, stabilization, max_age_ms):
    camera = mapping(row.get("camera_pose"))
    age = camera.get("age_ms")
    if not finite_number(age) or not 0 <= age <= max_age_ms:
        raise ValueError("camera pose unavailable or stale")
    rotation = vector(camera.get("optical_to_world"), 9)
    columns = [rotation[i::3] for i in range(3)]
    if any(not math.isclose(sum(a*b for a, b in zip(left, right, strict=True)),
                            int(i == j), abs_tol=1e-4)
           for i, left in enumerate(columns) for j, right in enumerate(columns)):
        raise ValueError("invalid optical rotation")
    ref = mapping(stabilization.get("target_ref"))
    if ref.get("kind") == "direction_ned":
        direction = unit(vector(ref.get("vector"), 3))
    elif ref.get("kind") == "world_point" and ref.get("altitude_reference", "AMSL") == "AMSL":
        pose = mapping(row.get("pose"))
        skew = pose.get("global_position_skew_ms")
        if not finite_number(skew) or abs(skew) > max_age_ms:
            raise ValueError("global position unavailable or stale")
        offset = tuple(c - p for c, p in zip(vector(camera.get("center_ned"), 3),
                                             vector(pose.get("local_ned_m"), 3), strict=True))
        origin = WorldTarget(pose["lat_deg"], pose["lon_deg"], pose["alt_amsl_m"])
        target = WorldTarget(ref["lat_deg"], ref["lon_deg"], ref["alt_m"])
        direction = world_target_direction(origin, target, camera_offset_ned=offset)
    else:
        raise ValueError("no frame-aligned target")
    ray = tuple(sum(a*b for a, b in zip(direction, col, strict=True)) for col in columns)
    return ray, pointing_error_deg(direction, rotation)


def in_view(ray, camera):
    keys = ("fx", "fy", "cx", "cy", "width", "height")
    fx, fy, cx, cy, width, height = (camera.get(k) for k in keys)
    if not all(finite_number(v) for v in (fx, fy, cx, cy, width, height)) or min(
        fx, fy, width, height
    ) <= 0:
        return None
    forward, right, down = ray
    return (forward > 0 and 0 <= fx * right / forward + cx < width
            and 0 <= fy * down / forward + cy < height)


def tracking_summary(counts, pointing, roll):
    inside, outside = counts["in_view_frames"], counts["out_of_view_frames"]
    active, unresolved = counts["active_frames"], counts["unresolved_frames"]
    return {**counts, "pointing_error_deg": stats(pointing), "track_horizon_error_deg": stats(roll),
            "geometric_in_view_fraction_of_resolved": inside / (inside + outside)
            if inside + outside else None,
            "geometric_in_view_fraction_bounds": {
                "lower": inside / active, "upper": (inside + unresolved) / active,
            } if active else None}


TRACK_COUNTS = ("active_frames", "fresh_frames", "in_view_frames", "out_of_view_frames",
                "unresolved_frames")


def frame_samples(path, meta):
    counts = dict.fromkeys(TRACK_COUNTS, 0)
    pointing, roll, lock_errors = [], [], []
    max_age_s = mapping(meta.get("horizon_stabilization")).get("max_age_s", 0.2)
    max_age_ms = 1000 * max_age_s if finite_number(max_age_s) and max_age_s > 0 else 200.0
    for _, row in jsonl(path):
        state = mapping(row.get("gimbal_state"))
        st = mapping(state.get("stabilization"))
        if row.get("event") is not None:
            continue
        horizon = st.get("horizon_error_deg")
        if st.get("mode") != "track":
            # Same sample set as horizon_report's horizon_acceptance.feasible_error.
            if (st.get("mode") == "lock" and st.get("horizon_feasible") is True
                    and finite_number(horizon) and horizon >= 0):
                lock_errors.append(horizon)
            continue
        counts["active_frames"] += 1
        visible = None
        if state.get("fresh") is True and st.get("measurement_fresh") is True:
            counts["fresh_frames"] += 1
            angle = st.get("pointing_error_deg")
            try:
                ray, derived_angle = measured_ray(row, st, max_age_ms)
                camera = (row.get("camera_intrinsics") or row.get("camera_info")
                          or meta.get("camera_intrinsics") or meta.get("camera_info"))
                visible = in_view(ray, mapping(camera))
                if not finite_number(angle) or not 0 <= angle <= 180:
                    angle = derived_angle
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
            if finite_number(angle) and 0 <= angle <= 180:
                pointing.append(angle)
            if finite_number(horizon) and 0 <= horizon <= 180:
                roll.append(horizon)
        counts[{True: "in_view_frames", False: "out_of_view_frames", None: "unresolved_frames"}[
            visible]] += 1
    return counts, pointing, roll, lock_errors


def ack_evidence(path, issues):
    raw_rows = dispatches = ack_rows = unidentified = other_results = 0
    rejected = []
    types = set()
    complete = False
    if path is not None:
        try:
            for line, row in jsonl(path):
                msg = row.get("message_type") or row.get("mavpackettype")
                if msg == "host_horizon_dispatch":
                    dispatches += 1
                    types.add("GIMBAL_MANAGER_SET_ATTITUDE")
                if not isinstance(msg, str) or not msg.isupper():
                    continue
                raw_rows += 1
                if msg in {"GIMBAL_MANAGER_SET_ATTITUDE", "GIMBAL_MANAGER_SET_PITCHYAW",
                           "GIMBAL_DEVICE_SET_ATTITUDE"}:
                    types.add(msg)
                if msg != "COMMAND_ACK":
                    continue
                ack_rows += 1
                command, result = row.get("command"), row.get("result")
                if type(command) is not int or (command in COMMANDS and type(result) is not int):
                    unidentified += 1
                elif command in COMMANDS and result in REJECTIONS:
                    support = {key: row[key] for key in (
                        "_source_system", "_source_component", "target_system", "target_component",
                        "receive_monotonic_ns", "receive_wall_time", "result_param2") if key in row}
                    rejected.append({"line": line, "command": command, "result": result,
                                     "command_name": COMMANDS[command],
                                     "result_name": REJECTIONS[result],
                                     **support})
                elif command in COMMANDS and result not in (0, 5):
                    other_results += 1
            complete = True
        except ARTIFACT_ERRORS as exc:
            issues.append(f"{path}: {exc}")
    return {"path": str(path) if path else None, "read_complete": complete if path else None,
            "raw_rows": raw_rows if path else None,
            "command_ack_rows": ack_rows if raw_rows else None,
            "unidentifiable_ack_rows": unidentified if raw_rows else None,
            "unknown_gimbal_result_rows": other_results if raw_rows else None,
            "rejected_ack_rows_observed": len(rejected) if raw_rows else None,
            "rejection_evidence": rejected, "observed_ackless_messages": sorted(types),
            "host_stream_dispatch_rows": dispatches if path else None}


def flight_metrics(episode, meta, receipt, issues):
    sources = {}
    result = dict.fromkeys(("route_completed", "max_distance_from_spawn_m",
                            "minimum_max_distance_m", "rtf_window_median",
                            "rtf_delta_sim_over_delta_real"))

    def use(row, path, *, rtf=False, route=False):
        for key in result:
            if not (rtf if key.startswith("rtf_") else route):
                continue
            value = row.get(key)
            valid = (type(value) is bool if key == "route_completed" else finite_number(value)
                     and (value >= 0 if key == "max_distance_from_spawn_m" else value > 0))
            if result[key] is None and valid:
                result[key], sources[key] = value, str(path)

    if receipt and valid_round(receipt[2]):
        use(receipt[2], receipt[0], rtf=True, route=True)
    if episode.parent.name == "episodes":
        flight = episode.parent.parent
        attempt = flight.parent if flight.name == "flight" else flight
        path = artifact(flight, "long-range-result.json")
        row = optional_object(path, issues)
        claimed_episode = row.get("episode")
        if isinstance(claimed_episode, str) and (flight / claimed_episode).resolve() == episode:
            use(row, path, route=True)
        container_id = mapping(meta.get("simulator_container")).get("container_id")
        for name in ("result.json", "stats.json"):
            path = artifact(attempt, name)
            row = optional_object(path, issues)
            if container_id and row.get("container_id") == container_id:
                valid = valid_round(row)
                measured = row.get("measurement_complete") is True and row.get("errors") == []
                use(row, path, rtf=valid or measured, route=valid)
    return {**result, "sources": sources}


def evaluate_episode(episode, entry, receipts):
    issues = []
    meta = optional_object(artifact(episode, "episode.json"), issues)
    if not meta:
        issues.append("episode.json missing or invalid")
    sibling = artifact(episode.parent.parent.parent, "batch-result.json") if (
        episode.parent.parent.name == "flight" and episode.parent.name == "episodes") else None
    if sibling:
        receipts = [*receipts, (sibling, None, optional_object(sibling, issues))]
    bound = [r for r in receipts if bound_to_episode(r, episode, meta)]
    accepted = [r for r in bound if qualified(r[2])]
    baseline = [r for r in accepted if r[0].name in ACCEPTED_LEDGER_NAMES
                and mapping(r[2].get("horizon_acceptance")).get("passed") is True]
    primary = next(iter(baseline or accepted or bound), None)
    if receipts and not primary:
        issues.append("no receipt bound to the episode attempt and simulator container")
    receipt_row = primary[2] if primary else {}
    recorded_gate = mapping(receipt_row.get("horizon_acceptance")).get("passed")
    identities = {c for c in (condition_of(entry), condition_of(meta), *(
        condition_of(mapping(r[2].get("inputs"))) for r in bound)) if c is not None}
    condition = next(iter(identities)) if len(identities) == 1 else None
    if len(identities) > 1:
        issues.append("conflicting condition identities")
    scenario_class = receipt_row.get("scenario_class", mapping(receipt_row.get("inputs")).get(
        "scenario_class", WIDE_AREA if accepted else None))
    path = artifact(episode / "video", "manifest.jsonl")
    horizon = track = counts = None
    pointing, roll, lock_errors = [], [], []
    if path:
        try:
            horizon = summarize(path)
            counts, pointing, roll, lock_errors = frame_samples(path, meta)
            track = tracking_summary(counts, pointing, roll)
        except ARTIFACT_ERRORS as exc:
            issues.append(f"{path}: {exc}")
    else:
        issues.append("video manifest missing")
    row = {"episode_dir": str(episode), "episode_id": meta.get("episode_id", episode.name),
           "episode_status": meta.get("status"), "evidence_label": entry.get("evidence_label"),
           "condition": condition, "scenario_class": scenario_class,
           "accepted": True if accepted else (
               False if receipt_row.get("accepted") is False else None),
           "baseline_receipt_qualified": bool(baseline),
           "recorded_horizon_acceptance_passed": recorded_gate
           if type(recorded_gate) is bool else None,
           "receipt": {"path": str(primary[0]), "line": primary[1],
                       "attempt_id": receipt_row.get("attempt_id")} if primary else None,
           "video_manifest": str(path) if path else None,
           "frame_count": horizon["frame_count"] if horizon else None,
           "truncated": horizon["truncated"] if horizon else None,
           "horizon_acceptance": horizon["horizon_acceptance"] if horizon else None,
           "track_acceptance": horizon["track_acceptance"] if horizon else None,
           "tracking": track, "flight": flight_metrics(episode, meta, primary, issues),
           "mavlink": ack_evidence(artifact(episode, "mavlink.jsonl"), issues), "issues": issues}
    return row, counts, pointing, roll, lock_errors


def pooled_horizon(gates, errors):
    if not gates:
        return None
    totals = {key: sum(h[key] for h in gates) for key in (
        "frame_count", "feasible_frames", "degraded_frames", "unavailable_frames")}
    feasible = totals["feasible_frames"]
    degraded_known = all(h["degraded_fraction_of_feasible"] is not None
                         for h in gates if h["feasible_frames"])
    return {**totals, "feasible_error_deg": stats(errors),
            "degraded_fraction_of_feasible": totals["degraded_frames"] / feasible
            if feasible and degraded_known else None,
            "unavailable_fraction": totals["unavailable_frames"] / totals["frame_count"]}


def evaluate(episode_dirs=(), *, accepted_roots=(), evidence_files=()):
    issues = []
    entries, receipts = collect_inputs(episode_dirs, accepted_roots, evidence_files, issues)
    grid = {condition: [] for condition in expected_grid()}
    episodes = []
    for episode, entry in sorted(entries.items()):
        record, counts, pointing, roll, lock_errors = evaluate_episode(
            episode, entry, receipts[episode])
        condition = record["condition"]
        in_grid = (record["baseline_receipt_qualified"] and record["scenario_class"] == WIDE_AREA
                   and condition in grid)
        record["in_accepted_grid"] = in_grid
        episodes.append(record)
        if in_grid:
            grid[condition].append((record, counts, pointing, roll, lock_errors))

    conditions = []
    for (site, time_of_day, weather), members in grid.items():
        records = [m[0] for m in members]
        available = [m for m in members if m[1] is not None]
        counts = {key: sum(m[1][key] for m in available) for key in TRACK_COUNTS}
        horizon = [r["horizon_acceptance"] for r in records if r["horizon_acceptance"] is not None
                   and r["horizon_acceptance"]["frame_count"] > 0]
        horizon_p95 = [h["feasible_error"]["p95_deg"] for h in horizon
                       if h["feasible_error"]["p95_deg"] is not None]
        track_gates = [r["track_acceptance"] for r in records if r["track_acceptance"] is not None
                       and r["track_acceptance"]["frame_count"] > 0]
        ack_counts = [r["mavlink"]["rejected_ack_rows_observed"] for r in records
                      if r["mavlink"]["rejected_ack_rows_observed"] is not None]
        route = [r["flight"]["route_completed"] for r in records
                 if r["flight"]["route_completed"] is not None]
        conditions.append({
            "site": site, "time_of_day": time_of_day, "weather": weather,
            "accepted_episode_count": len(records),
            "episode_dirs": [r["episode_dir"] for r in records],
            "frame_data_episode_count": len(available), "horizon_gate_episode_count": len(horizon),
            "horizon_gate_passed_count": sum(h["passed"] for h in horizon) if horizon else None,
            "horizon_worst_episode_p95_deg": max(horizon_p95, default=None),
            "horizon": pooled_horizon(horizon, [v for m in members for v in m[4]]),
            "track_gate_episode_count": len(track_gates),
            "track_gate_passed_count": sum(t["passed"] for t in track_gates)
            if track_gates else None,
            "tracking": tracking_summary(counts, [v for m in available for v in m[2]],
                                         [v for m in available for v in m[3]])
            if available else None,
            "ack_evidence_episode_count": len(ack_counts),
            "rejected_ack_rows_observed": sum(ack_counts) if ack_counts else None,
            "route_evidence_episode_count": len(route),
            "route_completed_count": sum(route) if route else None,
            **{key: stats([r["flight"][key] for r in records if r["flight"][key] is not None])
               for key in ("rtf_window_median", "rtf_delta_sim_over_delta_real",
                           "max_distance_from_spawn_m")},
        })
    return {"schema_version": 1, "condition_count": len(grid),
            "covered_condition_count": sum(bool(v) for v in grid.values()),
            "accepted_grid_episode_count": sum(len(v) for v in grid.values()),
            "conditions": conditions, "episodes": episodes, "issues": issues}


def markdown(report):
    def fmt(value):
        if value is None:
            return "n/a"
        if type(value) is bool:
            return "pass" if value else "fail"
        if isinstance(value, float):
            return f"{value:.3f}"
        return str(value).replace("|", r"\|").replace("\n", " ")

    def cells(*values):
        return "| " + " | ".join(map(fmt, values)) + " |"

    def angles(values):
        return " / ".join(fmt(values.get(k)) for k in ("p50", "p95", "max"))

    def fraction(value):
        return f"{value:.1%}" if value is not None else "n/a"

    def degradation(horizon):
        return tuple(f"{fmt(horizon.get(count))}/{fmt(horizon.get(denominator))} "
                     f"({fraction(horizon.get(ratio))})" if horizon.get(denominator) else None
                     for count, denominator, ratio in (
                         ("degraded_frames", "feasible_frames", "degraded_fraction_of_feasible"),
                         ("unavailable_frames", "frame_count", "unavailable_fraction")))

    def track_cells(track):
        t = mapping(track)
        bounds = mapping(t.get("geometric_in_view_fraction_bounds"))
        return (t.get("active_frames"), angles(mapping(t.get("pointing_error_deg"))),
                angles(mapping(t.get("track_horizon_error_deg"))),
                " / ".join(fmt(t.get(f"{key}_frames"))
                           for key in ("in_view", "out_of_view", "unresolved")),
                f"{fraction(bounds['lower'])} to {fraction(bounds['upper'])}" if bounds else None)

    lines = ["# X1 gimbal offline evaluation", "",
             f"Accepted wide-area coverage: {report['covered_condition_count']}/"
             f"{report['condition_count']} conditions; "
             f"{report['accepted_grid_episode_count']} episodes.",
             "", "Grid membership requires a bound acceptance ledger entry with a recorded "
             "horizon pass. Recomputed gate failures are reported without removing those entries.",
             "", "In-view is a geometric projection using recorded pinhole intrinsics, not visual "
             "lock or proof of visibility. Bounds treat unresolved active frames as all out of "
             "view (lower) or all in view (upper). ACK counts are observed raw rejections only; "
             "SET_ATTITUDE/PITCHYAW streaming messages do not use COMMAND_ACK.", "",
             "## Accepted wide-area conditions", ""]
    track_headers = ("Track frames", "Pointing P50/P95/max (deg)", "Track roll P50/P95/max (deg)",
                     "In/out/unresolved", "Geometric lock bounds")
    lock_headers = ("Lock P50/P95/max (deg)", "Lock degraded/feasible", "Lock unavailable/all")
    condition_headers = ("Site", "Time", "Weather", "Accepted", "Lock gate passes/evaluated",
                         *lock_headers, "Track gate passes/evaluated", *track_headers,
                         "Raw rejects", "Routes completed/known", "Distance P50 (m)",
                         "RTF window/whole P50")
    lines += [cells(*condition_headers), cells(*(["---"] * len(condition_headers)))]
    def tally(condition, numerator, denominator):
        return f"{condition[numerator]}/{condition[denominator]}" if condition[denominator] else None

    for c in report["conditions"]:
        horizon = mapping(c["horizon"])
        lines.append(cells(c["site"], c["time_of_day"], c["weather"], c["accepted_episode_count"],
                           tally(c, "horizon_gate_passed_count", "horizon_gate_episode_count"),
                           angles(mapping(horizon.get("feasible_error_deg"))),
                           *degradation(horizon),
                           tally(c, "track_gate_passed_count", "track_gate_episode_count"),
                           *track_cells(c["tracking"]), c["rejected_ack_rows_observed"],
                           tally(c, "route_completed_count", "route_evidence_episode_count"),
                           c["max_distance_from_spawn_m"]["p50"],
                           f"{fmt(c['rtf_window_median']['p50'])} / "
                           f"{fmt(c['rtf_delta_sim_over_delta_real']['p50'])}"))
    groups = (("Accepted grid episodes", True), ("Other episodes (diagnostic only)", False))
    for title, in_grid in groups:
        headers = ("Episode", "Evidence", "Condition", "Frames", "Lock gate", *lock_headers,
                   "Track gate", *track_headers, "Raw rejects", "Route", "Distance (m)",
                   "RTF window/whole")
        lines += ["", f"## {title}", "", cells(*headers), cells(*(["---"] * len(headers)))]
        for e in report["episodes"]:
            if e["in_accepted_grid"] is not in_grid:
                continue
            lock = mapping(e["horizon_acceptance"])
            track = mapping(e["track_acceptance"])
            flight = e["flight"]
            err = mapping(lock.get("feasible_error"))
            lines.append(cells(e["episode_dir"], e["evidence_label"],
                               "/".join(e["condition"] or ()) or None, e["frame_count"],
                               lock.get("passed") if lock.get("frame_count") else None,
                               angles({k: err.get(f"{k}_deg") for k in ("p50", "p95", "max")}),
                               *degradation(lock),
                               track.get("passed") if track.get("frame_count") else None,
                               *track_cells(e["tracking"]),
                               e["mavlink"]["rejected_ack_rows_observed"],
                               flight["route_completed"], flight["max_distance_from_spawn_m"],
                               f"{fmt(flight['rtf_window_median'])} / "
                               f"{fmt(flight['rtf_delta_sim_over_delta_real'])}"))
    issues = [*report["issues"], *(f"{e['episode_dir']}: {issue}"
                                  for e in report["episodes"] for issue in e["issues"])]
    if issues:
        lines += ["", "## Input issues", "", *(f"- {fmt(issue)}" for issue in issues)]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode_dirs", type=Path, nargs="*", help="explicit episode directories")
    parser.add_argument("--accepted-root", action="append", type=Path, default=[],
                        help="batch root (recursive) or accepted.jsonl[.gz]; repeatable")
    parser.add_argument("--evidence", action="append", type=Path, default=[],
                        help="JSON with episodes: [{episode_dir, receipt_path?, receipt_line?, "
                             "site?, time_of_day?, weather?, evidence_label?}]; relative paths")
    parser.add_argument("--json-out", type=Path, help="JSON output path (default: stdout)")
    parser.add_argument("--markdown-out", type=Path, help="optional Markdown output path")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.episode_dirs, accepted_roots=args.accepted_root,
                          evidence_files=args.evidence)
        text = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if args.json_out is None:
            print(text, end="")
        else:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(text, encoding="utf-8")
        if args.markdown_out is not None:
            args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_out.write_text(markdown(report), encoding="utf-8")
    except ARTIFACT_ERRORS as exc:
        parser.exit(2, f"x1-gimbal-eval: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
