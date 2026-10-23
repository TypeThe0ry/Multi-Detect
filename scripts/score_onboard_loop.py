"""Score onboard-loop episodes against scenario truth (after the flight only).

Success needs both: the confirmed position estimate lies within ``radius_m`` of
the true target, and the confirmed box overlaps the true target's visible
pixels in the confirming frame (segmentation when recorded, else projection).
Confirming anything else is a false confirm. When the confirmed object is a
distractor that the task text describes just as well (same task noun, no
conflicting colour, e.g. a second person for "find the person"), the episode is
also marked ``task_equivalent``: strict success stays false, but the world's task
text, not the vehicle, is ambiguous.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def episode_world_id(episode: Path) -> str | None:
    """Read the world id from the episode metadata used by campaign scoring."""
    try:
        metadata = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    world_sdf = metadata.get("world_sdf")
    if isinstance(world_sdf, str) and world_sdf:
        world_id = Path(world_sdf).stem
        if world_id:
            return world_id
    return next((part for part in reversed(episode.parts) if part.startswith("gen_")), None)


def truth_for_episode(
    episode: Path, scenarios: Path | None = None
) -> tuple[str | None, dict[str, Any] | None]:
    """Load the same world truth file selected by ``eval_onboard_loop_campaign``."""
    world_id = episode_world_id(episode)
    if world_id is None:
        return None, None
    roots = [scenarios] if scenarios is not None else []
    default_roots = (
        ROOT / "configs/scenarios/generated-movers",
        ROOT / "configs/scenarios/generated-count-site",
        ROOT / "configs/scenarios/generated-confusers",
        ROOT / "configs/scenarios/generated-confusers-2",
        ROOT / "configs/scenarios/generated-r15",
        ROOT / "configs/scenarios/generated-v2-appearance",
        ROOT / "configs/scenarios/generated-v2",
    )
    for root in default_roots:
        if root not in roots:
            roots.append(root)
    for root in roots:
        path = root / f"{world_id}.truth.json"
        if path.is_file():
            try:
                return world_id, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return world_id, None
    return world_id, None


from multidetect.autonomy.airframe_profile import (  # noqa: E402
    AirframeProfile,
    AirframeProfileError,
)
from multidetect.autonomy.motion_intent import profile_mount_pitch_down_rad  # noqa: E402
from multidetect.simulator.camera_projection import (  # noqa: E402
    CAMERA_MOUNT_PITCH_RAD,
    project_target_bbox,
)
from multidetect.simulator.task_semantics import task_equivalent  # noqa: E402
from multidetect.simulator.world_gen import trajectory_position  # noqa: E402


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _true_target(truth: dict[str, Any]) -> dict[str, Any]:
    return next(t for t in truth["targets"] if t.get("true_target"))


def _sim_time(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("sim_time_s", "simulation_time_s", "timestamp_s", "sim_timestamp_s"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and math.isfinite(float(candidate)):
                return float(candidate)
        # Sensor observations carry Gazebo's simulation clock in nanoseconds.
        # Host receive timestamps are deliberately ignored for mover scoring.
        for key in ("frame_sensor_stamp_ns", "pose_sim_stamp_ns"):
            candidate = value.get(key)
            if isinstance(candidate, (int, float)) and math.isfinite(float(candidate)):
                return float(candidate) / 1_000_000_000.0
        for key in ("time", "timestamp"):
            candidate = value.get(key)
            if isinstance(candidate, dict):
                found = _sim_time(candidate)
                if found is not None:
                    return found
    return None


def _row_sim_time(row: dict[str, Any]) -> float | None:
    value = _sim_time(row)
    return value if value is not None else _sim_time(row.get("obs_before"))


def target_pose_at_time(target: dict[str, Any], time_s: float | None) -> dict[str, float]:
    pose = dict(target.get("pose") or {})
    trajectory = target.get("trajectory")
    if trajectory and time_s is not None:
        x, y = trajectory_position(trajectory, time_s)
        pose["x_east_m"], pose["y_north_m"] = x, y
    return pose


def _nearest_object(
    truth: dict[str, Any], north_east, radius_m: float, time_s: float | None = None
):
    best, best_distance = None, radius_m
    for obj in truth["targets"]:
        pose = target_pose_at_time(obj, time_s)
        distance = math.hypot(
            north_east[0] - float(pose["y_north_m"]),
            north_east[1] - float(pose["x_east_m"]),
        )
        if distance <= best_distance:
            best, best_distance = obj, distance
    return best


def _count_metrics(rows: list[dict[str, Any]], members: list[dict[str, Any]], radius_m: float):
    """Reported count and findings matched one-to-one to distinct counted objects."""
    mission = {}
    for row in rows:
        mission = ((row.get("labels") or {}).get("onboard") or {}).get("mission") or mission
    counts = mission.get("counts") or {}
    reported = sum(int(v) for v in counts.values()) if counts else None
    positions = [tuple(float(v) for v in p.split(",")) for p in mission.get("positions", [])]
    remaining = [(float(m["pose"]["y_north_m"]), float(m["pose"]["x_east_m"])) for m in members]
    matched, errors = 0, []
    for north, east in positions:
        if not remaining:
            break
        nearest = min(remaining, key=lambda m: math.hypot(m[0] - north, m[1] - east))
        distance = math.hypot(nearest[0] - north, nearest[1] - east)
        if distance <= radius_m:
            matched += 1
            errors.append(round(distance, 2))
            remaining.remove(nearest)
    return {
        "expected_count": len(members),
        "reported_count": reported,
        "matched_count": matched,
        "false_findings": len(positions) - matched,
        "count_position_errors_m": errors,
        "count_exact": reported == len(members) and matched == len(members),
    }


def _word_tokens(value: object) -> set[str]:
    return set(re.findall(r"[^\W_]+", str(value).casefold(), flags=re.UNICODE))


def _query_noun(query: str) -> str | None:
    words = _word_tokens(query)
    if not words:
        return None
    return re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)[-1]


def _singular_forms(noun: str) -> set[str]:
    """The noun and its singular: a plan query is often plural ("backpacks")."""
    forms = {noun}
    if len(noun) > 3 and not noun.endswith("ss"):
        if noun.endswith("ies"):
            forms.add(noun[:-3] + "y")
        elif noun.endswith(("ches", "shes", "xes", "ses")):
            forms.add(noun[:-2])
        elif noun.endswith("s"):
            forms.add(noun[:-1])
    return forms


def _object_matches_query(obj: dict[str, Any], query: str) -> bool:
    noun = _query_noun(query)
    if noun is None:
        return False
    values = list(obj.get("category_words") or obj.get("semantic_words") or [])
    values.extend(obj.get(key) for key in ("label", "class", "category") if obj.get(key))
    # SITL mission packages ask for "backpacks": the singular truth word must match
    # (operator-pause-resume scored its one correct alert as precision 0).
    forms = _singular_forms(noun)
    return any(forms & _word_tokens(value) for value in values)


def _mission_area(
    missions: list[dict[str, Any]], mission_config: dict[str, Any] | None = None
) -> tuple[tuple[float, float], float] | None:
    """Return a configured circular mission zone when labels preserve its refs."""
    for mission in reversed(missions):
        plan = mission.get("plan") if isinstance(mission.get("plan"), dict) else {}
        config = mission.get("config") or mission.get("onboard_config") or mission_config or {}
        if not isinstance(config, dict):
            config = {}
        refs = mission.get("refs") or config.get("refs") or plan.get("refs") or {}
        if not isinstance(refs, dict):
            refs = {}
        area = mission.get("mission_area") or mission.get("area")
        if isinstance(area, dict):
            center = area.get("center_ne") or area.get("center")
            radius = area.get("radius_m")
            if center is not None and radius is not None:
                try:
                    north, east = float(center[0]), float(center[1])
                    radius = float(radius)
                except (TypeError, ValueError, IndexError):
                    pass
                else:
                    if all(math.isfinite(value) for value in (north, east, radius)) and radius > 0:
                        return (north, east), radius
        region = mission.get("region") or config.get("region")
        coords = refs.get(region) if isinstance(region, str) else None
        if isinstance(coords, (list, tuple)) and len(coords) >= 3:
            try:
                north, east, radius = (float(value) for value in coords[:3])
            except (TypeError, ValueError):
                pass
            else:
                if all(math.isfinite(value) for value in (north, east, radius)) and radius > 0:
                    return (north, east), radius
        steps = plan.get("steps") or mission.get("steps") or []
        for step in steps:
            params = step.get("params") if isinstance(step, dict) else None
            region = params.get("region") if isinstance(params, dict) else None
            coords = refs.get(region) if isinstance(region, str) else None
            if not isinstance(coords, (list, tuple)) or len(coords) < 3:
                continue
            try:
                north, east, radius = (float(value) for value in coords[:3])
            except (TypeError, ValueError):
                continue
            if all(math.isfinite(value) for value in (north, east, radius)) and radius > 0:
                return (north, east), radius
        zone_refs = [
            coords
            for name, coords in refs.items()
            if name.startswith("zone:") and isinstance(coords, (list, tuple)) and len(coords) >= 3
        ]
        parsed_refs = set()
        for coords in zone_refs:
            try:
                parsed_refs.add(tuple(float(value) for value in coords[:3]))
            except (TypeError, ValueError):
                continue
        if len(parsed_refs) == 1:
            north, east, radius = next(iter(parsed_refs))
            if all(math.isfinite(value) for value in (north, east, radius)) and radius > 0:
                return (north, east), radius
    return None


def _moving_track_metrics(
    rows: list[dict[str, Any]], target: dict[str, Any], radius_m: float, task_duration_s: float
) -> dict[str, Any]:
    """Score only SIM-timestamped target estimates; host timestamps are rejected."""
    samples = []
    for row in rows:
        time_s = _row_sim_time(row)
        confirmed = ((row.get("labels") or {}).get("onboard") or {}).get("confirmed") or {}
        estimate = confirmed.get("position_estimate_ne") if isinstance(confirmed, dict) else None
        if time_s is None or not isinstance(estimate, (list, tuple)) or len(estimate) < 2:
            continue
        try:
            north, east = float(estimate[0]), float(estimate[1])
            pose = target_pose_at_time(target, time_s)
            distance = math.hypot(north - float(pose["y_north_m"]), east - float(pose["x_east_m"]))
        except (TypeError, ValueError, KeyError):
            continue
        samples.append(
            {"time_s": time_s, "distance_m": distance, "within_radius": distance <= radius_m}
        )
    samples.sort(key=lambda item: item["time_s"])
    times = [s["time_s"] for s in samples]
    covered = max(0.0, min(task_duration_s, (times[-1] - times[0]) if times else 0.0))
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    return {
        "moving_track_samples": samples,
        "moving_distance_mean_m": (
            sum(s["distance_m"] for s in samples) / len(samples) if samples else None
        ),
        "moving_distance_max_m": (max(s["distance_m"] for s in samples) if samples else None),
        "moving_track_continuity": (
            sum(s["within_radius"] for s in samples) / len(samples) if samples else None
        ),
        "moving_track_sample_count": len(samples),
        "moving_task_duration_s": task_duration_s,
        "moving_time_coverage": covered / task_duration_s if task_duration_s > 0 else 0.0,
        "moving_max_gap_s": max(gaps, default=float("inf")),
        "moving_track_eligible": bool(
            samples
            and covered / task_duration_s >= 0.8
            and max(gaps, default=float("inf")) <= 5.0
            and sum(s["distance_m"] for s in samples) / len(samples) <= radius_m
            and sum(s["within_radius"] for s in samples) / len(samples) >= 0.8
        ),
    }


def _mission_metrics(
    rows: list[dict[str, Any]],
    truth: dict[str, Any],
    radius_m: float,
    mission_config: dict[str, Any] | None = None,
):
    """Score mission reports against truth; return no fields for non-mission episodes."""
    missions = []
    for row in rows:
        onboard = (row.get("labels") or {}).get("onboard") or {}
        mission = onboard.get("mission")
        if isinstance(mission, dict):
            missions.append(mission)
    if not missions:
        return {}

    latest = missions[-1]
    plan = next(
        (mission["plan"] for mission in missions if isinstance(mission.get("plan"), dict)), {}
    )
    skills = plan.get("skills")
    if not isinstance(skills, list):
        skills = [step.get("skill") for step in plan.get("steps", []) if step.get("skill")]
    history = latest.get("history") or []
    if not isinstance(history, list):
        history = []
    answers = latest.get("answers") or []
    if not isinstance(answers, list):
        answers = []

    coverage = []
    inspect_answers = []
    for answer in answers:
        if not isinstance(answer, str):
            continue
        coverage_match = re.fullmatch(r"(.+):coverage:([+-]?(?:\d+(?:\.\d*)?|\.\d+))", answer)
        if coverage_match:
            value = float(coverage_match.group(2))
            if math.isfinite(value):
                coverage.append({"step": coverage_match.group(1), "coverage": value})
            continue
        view_match = re.fullmatch(r"(.+):view(\d+):([^:]+)", answer)
        if view_match:
            decision = view_match.group(3).casefold()
            inspect_answers.append(
                {
                    "step": view_match.group(1),
                    "view": int(view_match.group(2)),
                    "decision": view_match.group(3),
                    "matches": decision.startswith(("approach", "confirm")),
                }
            )

    parsed_alerts = []
    alert_records = []
    previous_alerts: list[str] = []
    for row in rows:
        mission_row = ((row.get("labels") or {}).get("onboard") or {}).get("mission") or {}
        if isinstance(mission_row, dict):
            alerts = mission_row.get("alerts") or []
            if isinstance(alerts, list) and alerts[: len(previous_alerts)] == previous_alerts:
                alerts = alerts[len(previous_alerts) :]
            if isinstance(mission_row.get("alerts"), list):
                previous_alerts = list(mission_row["alerts"])
            alert_records.extend((alert, _row_sim_time(row)) for alert in alerts)
    if not alert_records:
        alert_records = [(alert, None) for alert in latest.get("alerts") or []]
    for alert, alert_time_s in alert_records:
        if not isinstance(alert, str) or ":alert:" not in alert:
            continue
        payload = alert.split(":alert:", 1)[1]
        query, separator, coordinates = payload.rpartition("@")
        if not separator:
            continue
        north_east = coordinates.split(",")
        if len(north_east) != 2:
            continue
        try:
            north, east = (float(value) for value in north_east)
        except ValueError:
            continue
        if query.strip() and math.isfinite(north) and math.isfinite(east):
            parsed_alerts.append(
                {"query": query.strip(), "north": north, "east": east, "time_s": alert_time_s}
            )

    targets = [obj for obj in truth.get("targets", []) if isinstance(obj, dict)]
    zone = _mission_area(missions, mission_config)
    alert_hit_indices: set[int] = set()
    correct_alerts = 0
    for alert in parsed_alerts:
        nearest_index, nearest_distance = None, radius_m
        for index, obj in enumerate(targets):
            pose = target_pose_at_time(obj, alert.get("time_s"))
            try:
                distance = math.hypot(
                    alert["north"] - float(pose["y_north_m"]),
                    alert["east"] - float(pose["x_east_m"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if distance <= nearest_distance:
                nearest_index, nearest_distance = index, distance
        matched = targets[nearest_index] if nearest_index is not None else None
        describes_object = bool(matched and _object_matches_query(matched, alert["query"]))
        alert["object_label"] = matched.get("label") if matched else None
        alert["matches_query"] = describes_object
        if describes_object:
            correct_alerts += 1
            alert_hit_indices.add(nearest_index)

    query_nouns = {noun for alert in parsed_alerts if (noun := _query_noun(alert["query"]))}
    for step in plan.get("steps", []):
        if isinstance(step, dict) and step.get("skill") in {"patrol", "monitor"}:
            query = (step.get("params") or {}).get("query")
            if query and (noun := _query_noun(str(query))):
                query_nouns.add(noun)
    alert_skills = {"patrol", "monitor"} & set(skills)
    alert_scoring = bool(parsed_alerts or alert_skills)
    if not query_nouns and alert_skills:
        target_words = _true_target(truth).get("category_words") or []
        if target_words and (noun := _query_noun(str(target_words[0]))):
            query_nouns.add(noun)
    eligible_indices = set()
    for index, obj in enumerate(targets):
        if not any(_object_matches_query(obj, noun) for noun in query_nouns):
            continue
        if zone is not None:
            center, zone_radius = zone
            pose = target_pose_at_time(obj, None)
            if obj.get("trajectory"):
                # Eligibility is evaluated against the object's initial position;
                # individual alert correctness above remains SIM-time aware.
                pose = target_pose_at_time(obj, 0.0)
            try:
                distance = math.hypot(
                    center[0] - float(pose["y_north_m"]),
                    center[1] - float(pose["x_east_m"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if distance > zone_radius:
                continue
        eligible_indices.add(index)

    last_history = history[-1] if history and isinstance(history[-1], dict) else {}
    result = {
        "mission_plan_skills": skills,
        "mission_step_outcomes": history,
        "mission_alerts": parsed_alerts,
        "mission_alert_precision": (correct_alerts / len(parsed_alerts) if parsed_alerts else None),
        "mission_alert_correct_count": correct_alerts,
        "mission_alert_count": len(parsed_alerts),
        "mission_alert_recall": (
            len(alert_hit_indices & eligible_indices) / len(eligible_indices)
            if alert_scoring and eligible_indices
            else None
        ),
        "mission_alert_hit_count": len(alert_hit_indices & eligible_indices),
        "mission_alert_eligible_count": len(eligible_indices),
        "mission_survey_coverage": {entry["step"]: entry["coverage"] for entry in coverage},
        "mission_inspect_answers": inspect_answers,
        "mission_inspect_match_fraction": (
            sum(answer["matches"] for answer in inspect_answers) / len(inspect_answers)
            if inspect_answers
            else None
        ),
        "mission_completed": (
            last_history.get("state") == "succeeded"
            and last_history.get("skill") in {"return_home", "land"}
        ),
    }
    if any(obj.get("trajectory") for obj in targets):
        duration = 120.0 if _true_target(truth).get("class") == "person" else 300.0
        result.update(_moving_track_metrics(rows, _true_target(truth), radius_m, duration))
    return result


def _supervisor_evidence(episode: Path) -> dict[str, Any]:
    """Extract durable supervisor evidence written beside an onboard episode.

    The collector's labels are intentionally not used for these fields: the
    mission log is the authoritative record and survives a communications
    outage. The caller only adds these fields to package episodes, preserving
    the report shape of older suites.
    """
    path = episode.parent.parent / f"{episode.name}-onboard-frames" / "mission_log/mission.jsonl"
    evidence: dict[str, Any] = {
        "final_decision": None,
        "reason": None,
        "return_home_started": False,
        "onboard_log_exists": path.is_file(),
        "logged_decisions": [],
        "logged_reasons": [],
        "logged_ground_events": [],
        "ground_event_count": None,
        "original_mission_completed": None,
        "abort_hover_before_return": False,
        "mission_plan_skills": None,
    }
    if not path.is_file():
        return evidence
    try:
        content = path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in content.splitlines() if line.strip()]
        if not content.endswith("\n") or not records:
            return evidence
        mission_id = records[0].get("mission_id")
        if any(
            not isinstance(record, dict)
            or record.get("sequence") != index
            or record.get("mission_id") != mission_id
            for index, record in enumerate(records, 1)
        ):
            return evidence
        for record in records:
            if record.get("kind") == "execution" and any(
                not isinstance(record.get(key, {}), dict) for key in ("state", "action")
            ):
                return evidence
            if record.get("kind") == "mission_started":
                plan = record.get("plan")
                if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
                    return evidence
                if not all(isinstance(step, dict) for step in plan["steps"]):
                    return evidence
            if record.get("kind") == "final_summary":
                summary = record.get("summary")
                if not isinstance(summary, dict):
                    return evidence
                for key in ("history", "recovery_history"):
                    history = summary.get(key, [])
                    if not isinstance(history, list) or not all(
                        isinstance(step, dict) for step in history
                    ):
                        return evidence
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return evidence
    decisions = [r for r in records if r.get("kind") == "decision"]
    started = next((r for r in records if r.get("kind") == "mission_started"), {})
    evidence["mission_plan_skills"] = [
        step.get("skill") for step in (started.get("plan") or {}).get("steps", [])
    ]
    evidence["logged_decisions"] = [r.get("action") for r in decisions if r.get("action")]
    evidence["logged_reasons"] = [r.get("reason") for r in decisions if r.get("reason")]
    evidence["logged_ground_events"] = [
        r.get("event_type")
        for r in records
        if r.get("kind") == "ground_event" and r.get("event_type")
    ]
    evidence["ground_event_count"] = len(evidence["logged_ground_events"])
    summary = next(
        (r.get("summary") for r in reversed(records) if r.get("kind") == "final_summary"), {}
    )
    if not isinstance(summary, dict):
        summary = {}
    if isinstance(summary, dict):
        evidence["final_decision"] = summary.get("action")
        evidence["reason"] = summary.get("reason")
        history = summary.get("history") or []
        recovery = summary.get("recovery_history") or []
        if isinstance(history, list) and isinstance(recovery, list):
            evidence["original_mission_completed"] = bool(
                summary.get("action") == "continue"
                and history
                and history[-1].get("state") == "succeeded"
                and history[-1].get("skill") in {"return_home", "land"}
            )
    evidence["return_home_started"] = any(
        r.get("kind") == "execution" and (r.get("state") or {}).get("skill") == "return_home"
        for r in records
    ) or any(r.get("skill") == "return_home" for r in summary.get("recovery_history", []))
    first_recovery = next(
        (
            i
            for i, r in enumerate(records)
            if r.get("kind") == "execution" and (r.get("state") or {}).get("skill") == "return_home"
        ),
        None,
    )
    abort = next(
        (
            i
            for i, r in enumerate(records)
            if r.get("kind") == "escalation" and r.get("reason") == "explicit operator abort"
        ),
        None,
    )
    if first_recovery is not None and abort is not None and abort < first_recovery:
        evidence["abort_hover_before_return"] = any(
            r.get("kind") == "execution" and (r.get("action") or {}).get("kind") == "hold"
            for r in records[abort + 1 : first_recovery]
        )
    return evidence


def episode_camera_pitch_rad(episode: Path) -> float:
    """Camera mount pitch of the recorded airframe (the x500's 0.18 rad by default).

    Projecting a fixed-wing frame with the x500 mount puts the true box ~10 deg
    off, so a correct confirmation would score as a miss.
    """
    try:
        meta = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
        profile = AirframeProfile.from_mapping(meta["airframe_profile"])
    except (OSError, KeyError, TypeError, ValueError, AirframeProfileError):
        return CAMERA_MOUNT_PITCH_RAD
    if profile.category == "multirotor":
        return CAMERA_MOUNT_PITCH_RAD
    return profile_mount_pitch_down_rad(profile, CAMERA_MOUNT_PITCH_RAD)


def _true_box(
    episode: Path, observation: dict[str, Any], truth: dict[str, Any], time_s: float | None = None
):
    target = _true_target(truth)
    seg_name = observation.get("segmentation_path")
    if seg_name and truth.get("segmentation_labels"):
        from multidetect.sim_teacher.segmentation import boxes_from_segmentation, load_labels_map

        labels = load_labels_map(episode / seg_name.replace("\\", "/"))
        boxes = boxes_from_segmentation(labels, truth, min_area_px=4, min_box_px=2)
        hit = boxes.get(str(target["target_id"]))
        return (list(hit["box"]) if hit else None), "segmentation"
    pose = target_pose_at_time(target, time_s)
    info = observation["camera_info"]
    box = project_target_bbox(
        target_ned=(pose["y_north_m"], pose["x_east_m"], pose["z_up_m"]),
        pose=observation,
        camera_info=info,
        image_size=(int(info["width"]), int(info["height"])),
        target_size_m=tuple(target.get("size_m", (3.0, 1.8, 1.5))),
        camera_pitch_rad=episode_camera_pitch_rad(episode),
        target_yaw_rad=pose.get("yaw_rad", 0.0),
        target_bottom_offset_m=target.get("bottom_offset_m"),
    )
    return (list(box) if box else None), "projection"


def score_episode(
    episode: Path,
    truth: dict[str, Any],
    *,
    radius_m: float = 10.0,
    mission_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    if meta.get("status") == "aborted_low_disk":
        return {
            "episode": str(episode),
            "status": "aborted_low_disk",
            "steps": 0,
            "success": False,
            "false_confirm": False,
            "task_equivalent": False,
            "excluded_from_model_failure_metrics": True,
            "abort_reason": meta.get("failure"),
        }
    rows = [
        json.loads(line)
        for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    target = _true_target(truth)
    confirmed_row = None
    slow_calls, remembered_verdicts, slow_latency, fast_latency, flown = 0, 0, [], [], 0.0
    previous = None
    for row in rows:
        onboard = (row.get("labels") or {}).get("onboard") or {}
        calls = onboard.get("slow_calls", [])
        real_calls = [c for c in calls if not c.get("remembered", False)]
        slow_calls += len(real_calls)
        remembered_verdicts += len(calls) - len(real_calls)
        slow_latency += [c["latency_s"] for c in real_calls if c.get("latency_s") is not None]
        if onboard.get("fast_latency_s") is not None:
            fast_latency.append(onboard["fast_latency_s"])
        position = row["obs_before"]["pose"]["local_ned_m"]
        if previous is not None:
            flown += math.hypot(position[0] - previous[0], position[1] - previous[1])
        previous = position
        if (
            confirmed_row is None
            and onboard.get("confirmed")
            and onboard.get("state_before") != "returning"
        ):
            confirmed_row = row
    result: dict[str, Any] = {
        "episode": str(episode),
        "status": meta.get("status"),
        "steps": len(rows),
        "slow_calls": slow_calls,
        "remembered_verdicts": remembered_verdicts,
        "slow_latency_p50_s": sorted(slow_latency)[len(slow_latency) // 2]
        if slow_latency
        else None,
        "fast_latency_p50_s": sorted(fast_latency)[len(fast_latency) // 2]
        if fast_latency
        else None,
        "distance_flown_m": round(flown, 1),
        "confirmed": confirmed_row is not None,
        "success": False,
        "false_confirm": False,
        "task_equivalent": False,
    }
    members = [t for t in truth["targets"] if t.get("count_member")]
    if members:
        result.update(_count_metrics(rows, members, radius_m))
    result.update(_mission_metrics(rows, truth, radius_m, mission_config))
    if mission_config and mission_config.get("mission_package") is not None:
        evidence = _supervisor_evidence(episode)
        result["mission_supervisor"] = evidence
        if evidence["mission_plan_skills"] is not None:
            result["mission_plan_skills"] = evidence["mission_plan_skills"]
        if evidence["original_mission_completed"] is not None:
            result["mission_completed"] = evidence["original_mission_completed"]
    if confirmed_row is None:
        return result
    confirmed = confirmed_row["labels"]["onboard"]["confirmed"]
    estimate = confirmed.get("position_estimate_ne")
    confirm_time_s = _row_sim_time(confirmed_row)
    target_pose = target_pose_at_time(target, confirm_time_s)
    true_ne = (float(target_pose["y_north_m"]), float(target_pose["x_east_m"]))
    distance = (
        math.hypot(estimate[0] - true_ne[0], estimate[1] - true_ne[1]) if estimate else math.inf
    )
    true_box, source = _true_box(episode, confirmed_row["obs_before"], truth, confirm_time_s)
    overlap = _iou(confirmed["bbox"], true_box) if true_box else 0.0
    result.update(
        confirm_step=confirmed_row["step_index"],
        confirm_rule=confirmed.get("rule"),
        position_error_m=round(distance, 2) if math.isfinite(distance) else None,
        box_iou_with_true_target=round(overlap, 3),
        identity_source=source,
        success=bool(distance <= radius_m and overlap >= 0.1),
    )
    if "moving_track_eligible" in result:
        result["success"] = bool(result["success"] and result["moving_track_eligible"])
    result["false_confirm"] = not result["success"]
    if result["false_confirm"] and estimate:
        hit = _nearest_object(truth, estimate, radius_m, confirm_time_s)
        if hit is not None:
            result["confirmed_object"] = hit.get("label")
            result["task_equivalent"] = not hit.get("true_target") and task_equivalent(target, hit)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--radius-m", type=float, default=10.0)
    args = parser.parse_args(argv)
    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    print(
        json.dumps(
            [score_episode(e, truth, radius_m=args.radius_m) for e in args.episodes], indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
