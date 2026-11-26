#!/usr/bin/env python3
"""Verify the X1 evidence-follow SITL acceptance contract from recorded JSONL."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from itertools import chain
from pathlib import Path
from typing import Any


_START = "collector_x1_retask_primitive_start"
_RESULT = "collector_x1_retask_primitive_result"
_HANDOFF = "collector_x1_retask_landing_handoff"
_SEQUENCE = (
    ("fw_goto_waypoint", "reposition", "FW", "arrived"),
    ("loiter_observe", "orbit", "FW", "duration"),
    ("back_transition", "transition", "MC", "transitioned"),
    ("mc_return_to_home", "mc_reposition", "MC", "settled"),
)
_RING_TOLERANCE_M = 8.0
_ORBIT_VERTICAL_TOLERANCE_M = 10.0  # Scoped ASSUMED_SIM vertical tracking bound.
_MIN_SWEEP_DEG = 10.0
_MAX_GAP_MS = 1000


def _obj(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> bool:
    return type(value) in (float, int) and -1e12 <= value <= 1e12


def _index(value: Any) -> bool:
    return type(value) is int and value >= 0


def _stamp(row: Mapping) -> int:
    value = row.get("receive_monotonic_ns")
    return value if _index(value) else -1


def _peer(row: Mapping) -> tuple:
    return row.get("_source_system"), row.get("_source_component")


def _key(row: Mapping) -> tuple:
    return tuple(row.get(key) for key in (
        "revision", "step_index", "source_step_id", "primitive", "command", "executed_command",
    ))


def _motion_wait(row: Mapping) -> Mapping:
    return _obj(_obj(row.get("ack")).get("motion_wait"))


def _detection_identity(report):
    report = _obj(report)
    return {key: report.get(key) for key in ("source", "t_ms", "detection", "target_status")}


def _tools_match(receipt, perception):
    detection = _obj(perception.get("detection"))
    position = _obj(detection.get("position_estimate"))
    common = {"captured_ms": perception.get("t_ms"), "track_id": detection.get("track_ref")}
    shared = {"schema": "x1.contingency.tool.v1", "ok": True, "source": "fault", "error": None}
    expected = {
        "look": {**common, "truncated": False, "detections": [
            {key: detection.get(key) for key in ("class", "bearing_deg", "elevation_deg")}]},
        "target_status": {**common, "visible": True, "last_seen_age_ms_at_snapshot": 0,
                          "position_wgs84_amsl": [position.get(k) for k in ("lat_deg", "lon_deg", "alt_amsl_m")],
                          "error_bound_m": position.get("error_bound_m")},
    }
    rows = receipt.get("injected_tool_results")
    if not isinstance(rows, (tuple, list)):
        return False
    tools = {row.get("tool") for row in map(_obj, rows)
             if all(row.get(key) == value for key, value in shared.items())
             and row.get("tool") in expected and row.get("data") == expected[row["tool"]]
             and _index(row.get("age_ms")) and row["age_ms"] <= 1000}
    return tools >= {"target_status"}


def _passage(wait: Mapping) -> bool:
    passage = _obj(wait.get("x1_passage"))
    before = passage.get("pre_plane_sim_stamp_ns")
    crossing = passage.get("crossing_sim_stamp_ns")
    cross_track = passage.get("crossing_cross_track_m")
    limit = passage.get("crossing_cross_track_limit_m")
    return ("passage_failure" not in passage and _index(before) and _index(crossing) and before < crossing
            and _number(cross_track) and _number(limit) and 0 <= abs(cross_track) <= limit)


def _observed(row: Mapping) -> bool:
    if row.get("fault") != "target_found" or row.get("source") != "fault":
        return False
    value = _obj(row.get("observed_value"))
    detection = _obj(value.get("detection", value))
    position = _obj(detection.get("position_estimate"))
    return (isinstance(detection.get("class"), str) and bool(detection["class"].strip())
            and all(_number(detection.get(key)) and abs(detection[key]) <= bound
                    for key, bound in (("bearing_deg", 180), ("elevation_deg", 90)))
            and all(_number(position.get(key)) and low <= position[key] <= high
                    for key, low, high in (("lat_deg", -90, 90), ("lon_deg", -180, 180),
                                           ("alt_amsl_m", -500, 10_000), ("error_bound_m", 0, 1000)))
            and position["error_bound_m"] > 0)


def _orbit(mavlink: list[Mapping], start: Mapping, result: Mapping, peer: tuple) -> dict:
    """Recompute ring residence from scoped PX4 samples on the vehicle clock."""
    metrics = {"passed": False, "dwell_s": 0.0, "swept_deg": 0.0, "sample_count": 0}
    command = _obj(start.get("command"))
    executed = _obj(start.get("executed_command", command))
    wait = _obj(result.get("motion_wait"))
    center = executed.get("target_ned")
    radius, duration = command.get("radius_m"), command.get("duration_s")
    capture, residence = wait.get("orbit_capture_sim_ns"), wait.get("orbit_residence_sim_s")
    if (not isinstance(center, (list, tuple)) or len(center) != 3 or not all(map(_number, center))
            or not _number(radius) or radius <= 0 or not _number(duration) or duration <= 0
            or executed.get("radius_m") != radius or executed.get("duration_s") != duration
            or (_obj(command.get("metadata")).get("long_range_home_relative") is True
                and _obj(executed.get("metadata")).get("long_range_home_relative") is not False)
            or wait.get("target_ned_m") != center or wait.get("radius_m") != radius
            or not _index(capture) or not _number(residence) or not duration <= residence <= 3600):
        return metrics
    sim_end = capture + round(residence * 1e9)

    begin, end = _stamp(start), _stamp(result)
    flight_state = {}
    last_boot = None
    previous = None
    run_ms = 0
    swept = 0.0
    for row in mavlink:
        stamp, name = _stamp(row), row.get("message_type")
        if stamp > end:
            break
        if _peer(row) != peer:
            continue
        if name == "EXTENDED_SYS_STATE":
            flight_state = row
            if row.get("vtol_state") != 4 or row.get("landed_state") != 2:
                previous, run_ms, swept = None, 0, 0.0
        if name != "LOCAL_POSITION_NED" or stamp < begin:
            continue
        boot, north, east, down = (row.get(key) for key in ("time_boot_ms", "x", "y", "z"))
        if not _index(boot) or not capture <= boot * 1_000_000 <= sim_end:
            continue
        if last_boot is not None and boot <= last_boot:
            continue
        last_boot = boot
        if (flight_state.get("vtol_state") != 4 or flight_state.get("landed_state") != 2
                or not 0 <= stamp - _stamp(flight_state) <= 2_000_000_000
                or not all(map(_number, (north, east, down))) or abs(down - center[2]) > _ORBIT_VERTICAL_TOLERANCE_M
                or abs(math.hypot(north - center[0], east - center[1]) - radius) > _RING_TOLERANCE_M):
            previous, run_ms, swept = None, 0, 0.0
            continue
        metrics["sample_count"] += 1
        angle = math.degrees(math.atan2(east - center[1], north - center[0]))
        if previous is None or boot - previous[0] > _MAX_GAP_MS:
            run_ms, swept = 0, 0.0
        else:
            run_ms += boot - previous[0]
            swept += abs((angle - previous[1] + 180) % 360 - 180)
        previous = (boot, angle)
        if (run_ms / 1000, swept) > (metrics["dwell_s"], metrics["swept_deg"]):
            metrics.update(dwell_s=run_ms / 1000, swept_deg=swept)
        if run_ms * 1_000_000 >= round(duration * 1e9) and swept >= _MIN_SWEEP_DEG:
            metrics["passed"] = True
    return metrics


def verify_evidence_follow(mavlink: Iterable[Mapping], steps: Iterable[Mapping]) -> tuple[bool, dict]:
    """Return a verdict and the supporting evidence or the first failing check."""
    evidence: dict = {"scenario": "evidence-follow", "steps": []}

    def fail(reason: str) -> tuple[bool, dict]:
        return False, {**evidence, "reason": reason}

    mavlink = list(mavlink)
    steps = list(steps)
    if any(not isinstance(row, Mapping) or _stamp(row) < 0 for row in chain(mavlink, steps)):
        return fail("invalid_evidence_row")
    mavlink.sort(key=_stamp)
    by_type: dict[str, list[Mapping]] = {}
    for row in mavlink:
        kind = row.get("message_type")
        if isinstance(kind, str):
            by_type.setdefault(kind, []).append(row)

    detected = [row for row in by_type.get("scenario_observed", []) if _observed(row)]
    if not detected:
        return fail("target_found_observation_missing")
    receipts = [row for row in by_type.get("scenario_decision_receipt", [])
                if row.get("action") == "retask" and row.get("status") == "installed"]
    if len(receipts) != 1:
        return fail("installed_retask_receipt_missing_or_ambiguous")
    receipt = receipts[0]
    proof = _obj(receipt.get("motion_proof"))
    bounds_hash = receipt.get("assumed_bounds_sha256")
    if (receipt.get("basis") != "ASSUMED_SIM" or proof.get("basis") != "ASSUMED_SIM"
            or proof.get("geofence_clear") is not True or not isinstance(bounds_hash, str)
            or re.fullmatch(r"[a-f0-9]{64}", bounds_hash) is None):
        return fail("assumed_sim_proof_missing")
    requests = [row for row in by_type.get("scenario_decision_request", [])
                if _obj(row.get("request")).get("request_id") == receipt.get("request_id")]
    if len(requests) != 1 or not isinstance(receipt.get("request_id"), str):
        return fail("retask_request_missing_or_ambiguous")
    request_marker = requests[0]
    request = _obj(request_marker.get("request"))
    observations = [row for row in detected if _stamp(row) < _stamp(request_marker)]
    if not observations:
        return fail("target_found_out_of_order")
    state = _obj(request.get("state"))
    events = state.get("events")
    perception = _obj(request.get("scenario_observation"))
    trigger = request.get("trigger", "event")
    event_id = state.get("latest_event_id") if trigger == "cadence" else request.get("event_id")
    observed_identity = _detection_identity(perception)
    if (not isinstance(events, list) or not _index(request.get("event_id"))
            or trigger not in {"event", "cadence"} or receipt.get("trigger", "event") != trigger
            or not _index(event_id) or event_id < 1
            or not any(_obj(event).get("id") == event_id
                       and _obj(event).get("type") == "target_found" and _obj(event).get("source") == "fault"
                       for event in events)
            or perception.get("source") != "fault"
            or _obj(perception.get("target_status")).get("visible") is not True
            or not any(observed_identity == _detection_identity(row.get("observed_value")) for row in observations)):
        return fail("retask_request_does_not_reference_detection")
    if not _tools_match(receipt, perception):
        return fail("retask_tool_evidence_mismatch")
    observation = next(row for row in reversed(observations)
                       if observed_identity == _detection_identity(row.get("observed_value")))
    evidence.update(observation_monotonic_ns=_stamp(observation), request_id=receipt["request_id"],
                    trigger=trigger, basis="ASSUMED_SIM", assumed_bounds_sha256=bounds_hash)

    starts, results, handoffs = (by_type.get(key, []) for key in (_START, _RESULT, _HANDOFF))
    if len(starts) != len(_SEQUENCE) or len(results) != len(_SEQUENCE) or len(handoffs) != 1:
        return fail("replacement_primitive_markers_missing_or_ambiguous")
    replacements = by_type.get("scenario_plan_replaced", [])
    if len(replacements) != 1:
        return fail("replacement_marker_missing_or_ambiguous")
    replacement = replacements[0]
    old = state.get("revision")
    revision, first = replacement.get("revision"), replacement.get("step_index")
    wire = receipt.get("wire")
    try:
        decision = _obj(json.loads(wire)) if isinstance(wire, str) else {}
        canonical = json.dumps(dict(decision), ensure_ascii=False, allow_nan=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
    except (ValueError, RecursionError):
        return fail("invalid_retask_wire")
    if (not _index(old) or not _index(revision) or revision != old + 1 or not _index(first)
            or replacement.get("previous_revision") != old or replacement.get("request_id") != receipt["request_id"]
            or starts[0].get("revision") != revision or starts[0].get("step_index") != first
            or not _stamp(request_marker) < _stamp(receipt) < _stamp(replacement) < _stamp(starts[0])
            or decision.get("revision") != old or decision.get("action") != "retask"
            or decision.get("request_id") != receipt["request_id"] or decision.get("event_id") != request["event_id"]
            or decision.get("mission_id") != state.get("mission_id")
            or hashlib.sha256(canonical).hexdigest() != proof.get("decision_digest")):
        return fail("replacement_revision_or_order_mismatch")
    planned_steps = _obj(_obj(decision.get("params")).get("plan")).get("steps")
    if not isinstance(planned_steps, list) or len(planned_steps) != len(_SEQUENCE):
        return fail("replacement_plan_mismatch")
    evidence.update(revision=revision, first_step_index=first, replacement_monotonic_ns=_stamp(replacement))
    labels = [_obj(_obj(row.get("labels")).get("x1_retask")) for row in steps]
    recorded_steps = [row for row, label in zip(steps, labels) if label]
    if len(recorded_steps) != len(_SEQUENCE) or len({row.get("step_index") for row in recorded_steps
                                                    if _index(row.get("step_index"))}) != len(_SEQUENCE) or any(
        not label and _stamp(row) >= _stamp(replacement) for row, label in zip(steps, labels)
    ):
        return fail("replacement_step_rows_missing_or_ambiguous")

    peers = {_peer(row) for row in by_type.get("HEARTBEAT", []) if row.get("autopilot") == 12
             and _stamp(row) < _stamp(observation) and _index(row.get("_source_system"))
             and row.get("_source_component") == 1}
    if len(peers) != 1:
        return fail("px4_peer_missing_or_ambiguous")
    peer = next(iter(peers))
    previous_end = _stamp(receipt)
    for offset, (step, start, result, spec) in enumerate(zip(recorded_steps, starts, results, _SEQUENCE)):
        primitive, kind, mode, waited = spec
        valid_primitives = (primitive, "gimbal_stare_at") if kind == "orbit" else (primitive,)
        label = _obj(_obj(step.get("labels")).get("x1_retask"))
        command = _obj(start.get("command"))
        wait = _motion_wait(step)
        planned = _obj(planned_steps[offset])
        planned_skill = "return_home" if kind == "mc_reposition" else start.get("primitive")
        if (start.get("primitive") not in valid_primitives or planned.get("skill") != planned_skill
                or planned.get("id") != start.get("source_step_id") or start.get("revision") != revision
                or start.get("step_index") != first + offset or step.get("step_index") != first + offset
                or not isinstance(start.get("source_step_id"), str) or not start["source_step_id"]
                or not previous_end < _stamp(start) < _stamp(result) <= _stamp(step)
                or _key(start) != _key(result) or _key(start) != _key(label)
                or command.get("kind") != kind or command.get("mode") != mode
                or _obj(step.get("outcome")).get("status") != "success" or result.get("status") != "success"
                or wait.get("waited") != waited or wait != result.get("motion_wait")):
            return fail(f"{primitive}_execution_mismatch")
        if kind == "reposition" and not _passage(wait):
            return fail("fw_passage_missing")
        if kind == "orbit":
            evidence["orbit"] = _orbit(mavlink, start, result, peer)
            if not evidence["orbit"]["passed"]:
                return fail("orbit_radius_residence_unconfirmed")
        if kind == "transition" and not any(
            _peer(row) == peer and _stamp(start) < _stamp(row) <= _stamp(result)
            and row.get("vtol_state") == 3 and row.get("landed_state") == 2
            for row in by_type.get("EXTENDED_SYS_STATE", [])
        ):
            return fail("px4_mc_transition_unconfirmed")
        if kind == "mc_reposition":
            target = command.get("target_ned")
            if not isinstance(target, (list, tuple)) or len(target) != 3 or target[:2] not in ([0, 0], (0, 0)):
                return fail("return_home_target_mismatch")
            if not any(_peer(row) == peer and _stamp(start) < _stamp(row) <= _stamp(result)
                       and all(_number(row.get(k)) for k in ("x", "y", "vx", "vy", "vz"))
                       and math.hypot(row["x"], row["y"]) <= 1.5
                       and math.hypot(row["vx"], row["vy"], row["vz"]) <= 0.6
                       for row in by_type.get("LOCAL_POSITION_NED", [])):
                return fail("return_home_settle_unconfirmed")
        evidence["steps"].append({"primitive": start["primitive"], "source_step_id": start["source_step_id"],
                                  "step_index": start["step_index"], "waited": waited})
        previous_end = _stamp(step)

    handoff = handoffs[0]
    if (handoff.get("revision") != revision or handoff.get("step_index") != first + len(_SEQUENCE)
            or handoff.get("primitive") != "vtol_land"
            or _obj(handoff.get("command")).get("kind") != "land"
            or _obj(handoff.get("command")).get("mode") != "MC"
            or handoff.get("source_step_id") != _obj(planned_steps[-1]).get("id")
            or not _stamp(handoff) > previous_end):
        return fail("landing_handoff_mismatch")
    landed_markers = [row for row in by_type.get("collector_landing_complete", [])
                      if _stamp(row) > _stamp(handoff)]
    for marker in landed_markers:
        state = _obj(marker.get("flight_state"))
        px4 = [row for row in by_type.get("EXTENDED_SYS_STATE", [])
               if _peer(row) == peer and _stamp(handoff) < _stamp(row) <= _stamp(marker)]
        if (marker.get("status") == "landed" and marker.get("revision") == revision
                and state.get("landed_state") == 1 and state.get("vtol_state") == 3 and px4
                and px4[-1].get("landed_state") == 1 and px4[-1].get("vtol_state") == 3
                and _stamp(marker) - _stamp(px4[-1]) <= 2_000_000_000):
            evidence["landed_monotonic_ns"] = _stamp(px4[-1])
            return True, evidence
    return fail("px4_landing_unconfirmed")


def _jsonl(path: Path) -> Iterable[Mapping]:
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_number}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path, help="episode directory containing MAVLink and step logs")
    args = parser.parse_args(argv)
    try:
        mavlink = args.episode / "mavlink.jsonl"
        if not mavlink.is_file():
            mavlink = args.episode / "mavlink.jsonl.gz"
        passed, evidence = verify_evidence_follow(_jsonl(mavlink), _jsonl(args.episode / "steps.jsonl"))
    except (OSError, EOFError, ValueError) as exc:
        passed, evidence = False, {"reason": "episode_evidence_unreadable", "detail": str(exc)}
    print(json.dumps({"passed": passed, "evidence": evidence}, indent=2, allow_nan=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
