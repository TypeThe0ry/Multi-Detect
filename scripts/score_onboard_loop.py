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
from pathlib import Path, PureWindowsPath
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
        # Episode metadata may have been produced on Windows and then scored on
        # POSIX, where backslashes are ordinary filename characters.
        world_path = PureWindowsPath(world_sdf) if "\\" in world_sdf else Path(world_sdf)
        world_id = world_path.stem
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
    # MissionExecutor uses the same ``mission`` label for its terminal status
    # (for example ``"done"``) when the executor has no report to attach.
    # Keep the latest structured report instead of replacing it with that
    # status string.  This is especially important when a return-home or
    # health-recovery step emits the final row after the count report.
    mission: dict[str, Any] = {}
    for row in rows:
        candidate = ((row.get("labels") or {}).get("onboard") or {}).get("mission")
        if isinstance(candidate, dict):
            mission = candidate
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


def _follow_target_visible(row: dict[str, Any], target: dict[str, Any]) -> bool:
    """Read explicit SIM visibility, falling back to a recorded confirmation."""
    onboard = ((row.get("labels") or {}).get("onboard") or {})
    confirmed = onboard.get("confirmed")
    for source in (onboard, confirmed if isinstance(confirmed, dict) else {}):
        for key in ("target_visible", "target_in_frame", "visible_target"):
            if key in source:
                return bool(source[key])
    return isinstance(confirmed, dict) and (
        isinstance(confirmed.get("position_estimate_ne"), (list, tuple))
        or bool(confirmed.get("bbox"))
    )


def _follow_false_confirm(row: dict[str, Any], target: dict[str, Any]) -> bool:
    """Detect explicit association to a distractor while following."""
    confirmed = (((row.get("labels") or {}).get("onboard") or {}).get("confirmed"))
    if not isinstance(confirmed, dict):
        return False
    expected = target.get("target_id")
    for key in ("target_id", "track_target_id", "object_id"):
        if confirmed.get(key) is not None and expected is not None:
            return str(confirmed[key]) != str(expected)
    for key in ("true_target", "is_target"):
        if key in confirmed:
            return not bool(confirmed[key])
    for key in ("object_label", "label", "class"):
        value = confirmed.get(key)
        if value is not None and target.get("label") is not None:
            return str(value).casefold() != str(target["label"]).casefold()
    return False


def _moving_track_metrics(
    rows: list[dict[str, Any]], target: dict[str, Any], radius_m: float, task_duration_s: float,
    *, visibility_threshold: float = 0.8, max_lost_count: int = 3,
    max_reacquire_s: float = 5.0,
) -> dict[str, Any]:
    """Score a moving-target track using simulation timestamps and truth."""
    samples = []
    for row in rows:
        time_s = _row_sim_time(row)
        confirmed = ((row.get("labels") or {}).get("onboard") or {}).get("confirmed") or {}
        estimate = confirmed.get("position_estimate_ne") if isinstance(confirmed, dict) else None
        if time_s is None:
            continue
        visible = _follow_target_visible(row, target)
        distance = None
        if isinstance(estimate, (list, tuple)) and len(estimate) >= 2:
            try:
                north, east = float(estimate[0]), float(estimate[1])
                pose = target_pose_at_time(target, time_s)
                distance = math.hypot(north - float(pose["y_north_m"]), east - float(pose["x_east_m"]))
            except (TypeError, ValueError, KeyError):
                distance = None
        false_follow = _follow_false_confirm(row, target)
        samples.append(
            {"time_s": time_s, "distance_m": distance, "within_radius": distance is not None and distance <= radius_m,
             "target_visible": visible, "false_follow": false_follow}
        )
    samples.sort(key=lambda item: item["time_s"])
    times = [s["time_s"] for s in samples]
    covered = max(0.0, min(task_duration_s, (times[-1] - times[0]) if times else 0.0))
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    visible_samples = [s for s in samples if s["target_visible"]]
    distances = [s["distance_m"] for s in samples if s["distance_m"] is not None]
    lost_count, reacquire_times, lost_started = 0, [], None
    for sample in samples:
        if sample["target_visible"]:
            if lost_started is not None:
                reacquire_times.append(max(0.0, sample["time_s"] - lost_started))
                lost_started = None
        elif lost_started is None and visible_samples:
            lost_count += 1
            lost_started = sample["time_s"]
    visibility_fraction = len(visible_samples) / len(samples) if samples else 0.0
    mean_distance = sum(distances) / len(distances) if distances else None
    max_distance = max(distances) if distances else None
    false_count = sum(bool(s["false_follow"]) for s in samples)
    max_reacquire = max(reacquire_times, default=None)
    return {
        "moving_track_samples": samples,
        "moving_distance_mean_m": mean_distance,
        "moving_distance_max_m": max_distance,
        "follow_mean_horizontal_distance_m": mean_distance,
        "follow_max_horizontal_distance_m": max_distance,
        "moving_track_continuity": (
            sum(s["within_radius"] for s in samples) / len(samples) if samples else None
        ),
        "moving_track_sample_count": len(samples),
        "moving_task_duration_s": task_duration_s,
        "moving_time_coverage": covered / task_duration_s if task_duration_s > 0 else 0.0,
        "moving_max_gap_s": max(gaps, default=float("inf")),
        "follow_target_in_frame_fraction": visibility_fraction,
        "follow_frame_target_fraction": visibility_fraction,
        "follow_lost_count": lost_count,
        "follow_reacquire_times_s": reacquire_times,
        "follow_reacquire_max_s": max_reacquire,
        "follow_false_confirm_count": false_count,
        "follow_visibility_pass": visibility_fraction >= visibility_threshold,
        "follow_distance_pass": mean_distance is not None and mean_distance <= radius_m and (max_distance is None or max_distance <= radius_m * 2),
        "follow_lost_pass": lost_count <= max_lost_count and (max_reacquire is None or max_reacquire <= max_reacquire_s),
        "follow_identity_pass": false_count == 0,
        "moving_track_eligible": bool(
            samples
            and covered / task_duration_s >= 0.8
            and max(gaps, default=float("inf")) <= 5.0
            and distances
            and sum(distances) / len(distances) <= radius_m
            and sum(s["within_radius"] for s in samples) / len(samples) >= 0.8
            and visibility_fraction >= visibility_threshold
            and lost_count <= max_lost_count
            and (max_reacquire is None or max_reacquire <= max_reacquire_s)
            and false_count == 0
        ),
    }


def _terminal_landing_assessment(
    rows: list[dict[str, Any]],
    mission_metrics: dict[str, Any] | None,
    ground_limit: float,
    safety_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Assess a mission whose last skill is a completed ``land`` step.

    Returns ``None`` when the mission does not qualify (the caller then keeps
    the legacy whole-flight ground-contact rule untouched).  Otherwise the
    terminal touchdown is separated from earlier flight: poses up to the
    moment ``land`` was commanded are the pre-landing flight, and everything
    after is the landing.  The landing passes only with an ON_GROUND
    (``landed_state == 1``) confirmation on the last row and a controlled
    descent rate inside ``touchdown_window_m`` of the ground.  The default
    limit 1.5 m/s is about twice the PX4 land speed observed in the evidence
    (max 0.71 m/s below 3 m); override with ``touchdown_max_descent_mps``.
    """
    metrics = mission_metrics or {}
    history = metrics.get("mission_step_outcomes")
    skills = metrics.get("mission_plan_skills")
    if not (
        metrics.get("mission_completed") is True
        and isinstance(history, list)
        and history
        and isinstance(history[-1], dict)
        and history[-1].get("skill") == "land"
        and history[-1].get("state") == "succeeded"
        and isinstance(skills, list)
        and skills
        and skills[-1] == "land"
    ):
        return None
    start = None
    for index in range(len(rows) - 1, -1, -1):
        if ((rows[index].get("labels") or {}).get("onboard") or {}).get("skill") == "land":
            start = index
        else:
            break
    if start is None:
        return None

    def sample(obs: Any) -> tuple[float, float | None, float | None] | None:
        obs = obs or {}
        ned = (obs.get("pose") or {}).get("local_ned_m")
        if not (isinstance(ned, (list, tuple)) and len(ned) >= 3):
            return None
        try:
            down = float(ned[2])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(down):
            return None
        vz = None
        velocity = obs.get("velocity")
        if isinstance(velocity, (list, tuple)) and len(velocity) >= 3:
            try:
                vz = float(velocity[2])
            except (TypeError, ValueError):
                vz = None
            if vz is not None and not math.isfinite(vz):
                vz = None
        t_ms = obs.get("pose_time_boot_ms")
        t_s = float(t_ms) / 1000.0 if isinstance(t_ms, (int, float)) else None
        return (-down, vz, t_s)

    pre = []
    for row in rows[:start]:
        for key in ("obs_before", "obs_after"):
            item = sample(row.get(key))
            if item is not None:
                pre.append(item[0])
    first = sample(rows[start].get("obs_before"))
    if first is not None:
        pre.append(first[0])
    landing = []
    for offset, row in enumerate(rows[start:]):
        for key in ("obs_before", "obs_after"):
            if key == "obs_before" and offset == 0:
                continue
            item = sample(row.get(key))
            if item is not None:
                landing.append(item)
    last_obs = rows[-1].get("obs_after") or rows[-1].get("obs_before") or {}
    landed_state = (last_obs.get("flight_state") or {}).get("landed_state")
    on_ground = landed_state == 1 and not isinstance(landed_state, bool)

    window = float(safety_cfg.get("touchdown_window_m", 3.0))
    limit = float(safety_cfg.get("touchdown_max_descent_mps", 1.5))
    rates: list[float] = []
    for index, (alt, vz, t) in enumerate(landing):
        if alt <= window and vz is not None:
            rates.append(vz)
        if index:
            prev_alt, _pv, prev_t = landing[index - 1]
            if (
                min(alt, prev_alt) <= window
                and prev_t is not None
                and t is not None
                and t - prev_t >= 0.2
            ):
                rates.append((prev_alt - alt) / (t - prev_t))
    max_descent = max(rates) if rates else None
    pre_min = min(pre) if pre else None
    pre_contact = pre_min is None or pre_min <= ground_limit
    descent_ok = max_descent is not None and max_descent <= limit
    return {
        "pre_landing_min_alt": pre_min,
        "pre_landing_contact": pre_contact,
        "landed_on_ground": on_ground,
        "touchdown_max_descent_mps": None if max_descent is None else round(max_descent, 3),
        "touchdown_descent_limit_mps": limit,
        "landing_pass": bool(not pre_contact and on_ground and descent_ok),
    }


def _flight_safety_metrics(
    rows: list[dict[str, Any]],
    mission_metrics: dict[str, Any] | None = None,
    mission_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive airframe-agnostic flight safety facts from recorded poses.

    ``local_ned_m[2]`` is down, therefore relative altitude is ``-z``.  The
    metrics intentionally use every recorded step (including the final one),
    so a transient ground strike cannot be hidden by a later return-home row.
    Thresholds are configurable per mission under ``expect.safety`` or
    ``safety``; old multirotor callers retain the same report shape and no
    safety gate is applied unless requested.
    """
    poses = []
    for row in rows:
        for key in ("obs_before", "obs_after"):
            pose = (row.get(key) or {}).get("pose") or {}
            ned = pose.get("local_ned_m")
            if isinstance(ned, (list, tuple)) and len(ned) >= 3:
                try:
                    n, e, down = (float(ned[0]), float(ned[1]), float(ned[2]))
                except (TypeError, ValueError):
                    continue
                if all(math.isfinite(v) for v in (n, e, down)):
                    poses.append((n, e, -down))
    if not poses:
        return {
            "safety_data_available": False,
            "safety_pass": False,
            "ground_contact_detected": None,
            "min_relative_altitude_m": None,
            "max_relative_altitude_m": None,
            "return_home_distance_m": None,
        }
    alts = [p[2] for p in poses]
    # Mission labels carry the authoritative completion state when present.
    completed = bool((mission_metrics or {}).get("mission_completed"))
    home = (0.0, 0.0)
    for row in reversed(rows):
        onboard = ((row.get("labels") or {}).get("onboard") or {})
        candidate = onboard.get("home_ne")
        if isinstance(candidate, (list, tuple)) and len(candidate) >= 2:
            try:
                home = (float(candidate[0]), float(candidate[1]))
                break
            except (TypeError, ValueError):
                pass
    final_n, final_e, _ = poses[-1]
    return_distance = math.hypot(final_n - home[0], final_e - home[1])
    safety_cfg: dict[str, Any] = {}
    if isinstance(mission_config, dict):
        candidate = mission_config.get("safety")
        if not isinstance(candidate, dict):
            candidate = (mission_config.get("expect") or {}).get("safety")
        if isinstance(candidate, dict):
            safety_cfg = candidate
    ground_limit = float(safety_cfg.get("ground_contact_altitude_m", 1.0))
    min_required = safety_cfg.get("min_relative_altitude_m")
    max_return = safety_cfg.get("max_return_distance_m")
    min_required = float(min_required) if min_required is not None else None
    max_return = float(max_return) if max_return is not None else None
    ground = min(alts) <= ground_limit
    landing = _terminal_landing_assessment(rows, mission_metrics, ground_limit, safety_cfg)
    landing_ok = True
    landing_fields: dict[str, Any] = {}
    gate_min = min(alts)
    if landing is not None:
        # Completed terminal ``land``: the touchdown itself is expected ground
        # contact.  Contact before landing, a hard touchdown or a missing
        # ON_GROUND confirmation still fail (``landing_pass``).
        ground = landing["pre_landing_contact"]
        landing_ok = landing["landing_pass"]
        gate_min = landing["pre_landing_min_alt"]
        landing_fields = {
            "terminal_landing_assessed": True,
            "landing_pass": landing["landing_pass"],
            "landing_on_ground_confirmed": landing["landed_on_ground"],
            "landing_touchdown_max_descent_mps": landing["touchdown_max_descent_mps"],
            "landing_touchdown_descent_limit_mps": landing["touchdown_descent_limit_mps"],
        }
    return {
        **landing_fields,
        "safety_data_available": True,
        "min_relative_altitude_m": round(min(alts), 3),
        "max_relative_altitude_m": round(max(alts), 3),
        "ground_contact_altitude_m": ground_limit,
        "ground_contact_detected": ground,
        "return_home_distance_m": round(return_distance, 3),
        "return_home_completed": completed,
        "safety_min_altitude_pass": min_required is None or gate_min >= min_required,
        "safety_return_distance_pass": max_return is None or return_distance <= max_return,
        "safety_pass": bool(
            not ground
            and landing_ok
            and (min_required is None or gate_min >= min_required)
            and (max_return is None or return_distance <= max_return)
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
        follow_cfg = {}
        if isinstance(mission_config, dict):
            expected_cfg = mission_config.get("expect")
            if not isinstance(expected_cfg, dict):
                expected_cfg = {}
            candidate = mission_config.get("follow") or expected_cfg.get("follow")
            if isinstance(candidate, dict):
                follow_cfg = candidate
        result.update(
            _moving_track_metrics(
                rows,
                _true_target(truth),
                radius_m,
                duration,
                visibility_threshold=float(follow_cfg.get("min_target_in_frame_fraction", 0.8)),
                max_lost_count=int(follow_cfg.get("max_lost_count", 3)),
                max_reacquire_s=float(follow_cfg.get("max_reacquire_s", 5.0)),
            )
        )
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


def _geographic_skills(value: Any) -> bool:
    return (
        isinstance(value, list) and bool(value)
        and all(skill in ("survey", "return_home") for skill in value)
    )


def _geographic_plan_skills(raw: Any) -> list[str]:
    """Check the recorded diagnostic summary or the existing full plan DSL."""
    from multidetect.autonomy.mission_plan import validate_mission_plan

    if not isinstance(raw, dict):
        raise ValueError("target-free mission needs a declared plan")
    summary_keys = {"skills", "repairs", "error", "latency_s"}
    if raw.get("error") is not None:
        raise ValueError("target-free mission plan records an error")
    if "repairs" in raw and (
        not isinstance(raw["repairs"], list) or any(not isinstance(value, str) for value in raw["repairs"])
    ):
        raise ValueError("invalid target-free plan repair summary")
    if "latency_s" in raw and (
        isinstance(raw["latency_s"], bool) or not isinstance(raw["latency_s"], (int, float))
        or not math.isfinite(raw["latency_s"]) or raw["latency_s"] < 0
    ):
        raise ValueError("invalid target-free plan latency summary")
    if "steps" in raw:
        plan = validate_mission_plan({k: v for k, v in raw.items() if k not in summary_keys})
        skills = [step.skill for step in plan.steps]
        if "skills" in raw and raw["skills"] != skills:
            raise ValueError("plan skills disagree with actual steps")
    else:
        if set(raw) - summary_keys:
            raise ValueError("unknown target-free plan summary fields")
        skills = raw.get("skills")
    if not _geographic_skills(skills):
        raise ValueError("declared plan requires target ground truth")
    return skills


def _validate_target_free_declarations(
    episode: Path, rows: list[dict[str, Any]], mission_config: dict[str, Any] | None,
    skills: list[str], evidence: dict[str, Any] | None,
) -> None:
    plans = [mission["plan"] for row in rows
             if isinstance(mission := ((row.get("labels") or {}).get("onboard") or {}).get("mission"), dict)
             and "plan" in mission]
    if not plans or any(_geographic_plan_skills(plan) != skills for plan in plans):
        raise ValueError("target-free mission plan declarations disagree")
    package_raw = (mission_config or {}).get("mission_package")
    if package_raw is None:
        return
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.autonomy.mission_plan import validate_mission_plan

    if isinstance(package_raw, (str, Path)):
        package_raw = Path(package_raw)
        if not package_raw.is_absolute():
            package_raw = ROOT / package_raw
    package = load_mission_package(package_raw)
    package_skills = [step.skill for step in package.plan.steps] if package.plan else []
    if not _geographic_skills(package_skills) or package_skills != skills:
        raise ValueError("uploaded package requires a different or target-required plan")
    if not evidence or evidence.get("onboard_log_exists") is not True or evidence.get("mission_plan_skills") != skills:
        raise ValueError("target-free package needs valid durable plan evidence")
    path = episode.parent.parent / f"{episode.name}-onboard-frames" / "mission_log/mission.jsonl"
    starts = [record["plan"] for line in path.read_text(encoding="utf-8").splitlines()
              if (record := json.loads(line)).get("kind") == "mission_started"]
    if not starts:
        raise ValueError("target-free package has no durable started plan")
    for raw in starts:
        plan = validate_mission_plan(raw)
        package.validate_plan(plan)
        if [step.skill for step in plan.steps] != skills:
            raise ValueError("durable package steps disagree with target-free plan")


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
    # Geographic missions may intentionally have no identity ground truth.
    # Keep malformed / untagged target sets on the existing strict path.
    target = None if truth["targets"] == [] else _true_target(truth)
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
    mission_metrics = _mission_metrics(rows, truth, radius_m, mission_config)
    result.update(mission_metrics)
    result.update(_flight_safety_metrics(rows, mission_metrics, mission_config))
    # A negative area-search task is successful when the requested object was
    # not confirmed and the vehicle completed a safe return.  This is opt-in so
    # legacy suites keep their original confirmation-only semantics.
    expect = mission_config.get("expect") if isinstance(mission_config, dict) else None
    if not isinstance(expect, dict):
        expect = {}
    negative = bool(
        expect.get("not_found")
        or expect.get("target_found") is False
        or (isinstance(mission_config, dict) and mission_config.get("not_found"))
    )
    if negative:
        result["negative_task"] = True
        result["negative_task_passed"] = bool(
            not result["confirmed"]
            and result.get("mission_alert_correct_count", 0) == 0
            and result.get("mission_completed") is True
            and result.get("safety_pass") is True
        )
        result["success"] = result["negative_task_passed"]
    evidence = None
    if mission_config and mission_config.get("mission_package") is not None:
        evidence = _supervisor_evidence(episode)
        result["mission_supervisor"] = evidence
        if evidence["mission_plan_skills"] is not None:
            result["mission_plan_skills"] = evidence["mission_plan_skills"]
        if evidence["original_mission_completed"] is not None:
            result["mission_completed"] = evidence["original_mission_completed"]
    if target is None:
        skills = result.get("mission_plan_skills")
        expected_skills = expect.get("skills")
        configured_mission = (mission_config or {}).get("mission")
        if (
            negative
            or not _geographic_skills(skills)
            or (expected_skills is not None and not _geographic_skills(expected_skills))
            or configured_mission not in (None, "from_text", "survey", "return_home", "package")
            or (configured_mission == "package" and (mission_config or {}).get("mission_package") is None)
        ):
            _true_target(truth)  # Preserve rejection for target-required missions.
        _validate_target_free_declarations(episode, rows, mission_config, skills, evidence)
        result["target_scoring_applicable"] = False
        # ``success`` retains its legacy identity-confirmation meaning. Mission
        # completion, coverage, safety and supervisor expectations stay separate.
        # A target-free plan has no legitimate identity to carry into returning.
        # Do not let the legacy returning-state filter hide an unexpected claim.
        unexpected_confirmation = any(
            ((row.get("labels") or {}).get("onboard") or {}).get("confirmed")
            for row in rows
        )
        result["confirmed"] = unexpected_confirmation
        result["false_confirm"] = unexpected_confirmation
        return result
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
