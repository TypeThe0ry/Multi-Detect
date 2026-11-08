"""Replay recorded count confirmations against their frame camera-pose validity."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from multidetect.simulator.camera_projection import pixel_camera_ray  # noqa: E402
from scripts.score_onboard_loop import episode_camera_pitch_rad  # noqa: E402


def _distance(first: Iterable[float], second: Iterable[float]) -> float:
    a = tuple(float(value) for value in first)
    b = tuple(float(value) for value in second)
    return math.dist(a[:2], b[:2])


def _counted_metadata(episode: Path) -> dict[str, dict[str, Any]]:
    """Read the recorded count snapshots without rerunning the policy."""
    log = episode.parent.parent / f"{episode.name}-onboard-frames" / "mission_log" / "mission.jsonl"
    if not log.is_file():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        counted = (row.get("state") or {}).get("counted")
        if isinstance(counted, dict) and counted.get("track_id"):
            result[str(counted["track_id"])] = counted
    return result


def _truth_members(truth: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(truth, dict):
        return []
    return [target for target in truth.get("targets", []) if target.get("count_member")]


def _identity_history(rows: list[dict[str, Any]], track_id: str | None) -> list[dict[str, Any]]:
    if not track_id:
        return []
    history: list[dict[str, Any]] = []
    for row in rows:
        onboard = ((row.get("labels") or {}).get("onboard") or {})
        for call in onboard.get("slow_calls") or []:
            if call.get("track_id") != track_id:
                continue
            history.append(
                {
                    "step": int(row["step_index"]),
                    "frame_id": f"frame_{int(row['step_index']):04d}.png",
                    "identity": call.get("identity"),
                    "decision": call.get("decision"),
                    "score": call.get("confidence"),
                }
            )
    return history


def _event_analysis(
    position: list[float] | None,
    track_id: str | None,
    prior_events: list[dict[str, Any]],
    members: list[dict[str, Any]],
    counted_metadata: dict[str, dict[str, Any]],
    dedupe_radius_m: float,
) -> dict[str, Any]:
    analysis: dict[str, Any] = {
        "dedupe_radius_m": dedupe_radius_m,
        "nearest_prior_confirmation": None,
        "would_merge_by_position": None,
        "co_visibility_proves_distinct": None,
        "nearest_truth_member": None,
        "truth_distances_m": {},
    }
    if position is None:
        return analysis
    positioned_prior_events = [
        event for event in prior_events if event.get("position_ne") is not None
    ]
    if positioned_prior_events:
        nearest = min(
            positioned_prior_events, key=lambda event: _distance(position, event["position_ne"])
        )
        distance = _distance(position, nearest["position_ne"])
        analysis["nearest_prior_confirmation"] = {
            "track_id": nearest.get("track_id"),
            "distance_m": round(distance, 3),
        }
        analysis["would_merge_by_position"] = distance <= dedupe_radius_m
        current_snapshot = counted_metadata.get(str(track_id), {})
        prior_snapshot = counted_metadata.get(str(nearest.get("track_id")), {})
        current_ids = set(current_snapshot.get("co_visible_with") or ())
        prior_ids = set(prior_snapshot.get("co_visible_with") or ())
        analysis["co_visibility_proves_distinct"] = bool(
            str(nearest.get("track_id")) in current_ids
            or str(track_id) in prior_ids
        )
    for member in members:
        pose = member.get("pose") or {}
        truth_position = [float(pose["y_north_m"]), float(pose["x_east_m"])]
        distance = _distance(position, truth_position)
        target_id = str(member.get("target_id"))
        analysis["truth_distances_m"][target_id] = round(distance, 3)
    if analysis["truth_distances_m"]:
        target_id = min(analysis["truth_distances_m"], key=analysis["truth_distances_m"].get)
        member = next(item for item in members if str(item.get("target_id")) == target_id)
        analysis["nearest_truth_member"] = {
            "target_id": target_id,
            "class": member.get("class"),
            "label": member.get("label"),
            "distance_m": analysis["truth_distances_m"][target_id],
        }
        earlier = next(
            (
                event
                for event in positioned_prior_events
                if (event.get("analysis") or {}).get("nearest_truth_member", {}).get("target_id")
                == target_id
            ),
            None,
        )
        if earlier is not None:
            analysis["duplicate_of_prior_track"] = earlier.get("track_id")
    return analysis


def replay_episode(episode: Path, *, truth: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep the recorded flight and decisions; reject count events lacking a frame ray."""
    rows_path = episode / "steps.jsonl"
    rows = [
        json.loads(line)
        for line in rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise ValueError(f"empty episode: {episode}")

    count_by_step: dict[str, int] = {}
    previous: dict[str, int] = {}
    events: list[dict[str, Any]] = []
    prior_events: list[dict[str, Any]] = []
    members = _truth_members(truth)
    counted_metadata = _counted_metadata(episode)
    identity_histories: dict[str, list[dict[str, Any]]] = {}
    pitch = episode_camera_pitch_rad(episode)
    for row in rows:
        onboard = ((row.get("labels") or {}).get("onboard") or {})
        mission = onboard.get("mission") or {}
        counts = mission.get("counts") or {}
        for step_id, value in counts.items():
            current = int(value)
            count_by_step[step_id] = max(count_by_step.get(step_id, 0), current)
            if current <= previous.get(step_id, 0):
                continue
            if current != previous.get(step_id, 0) + 1:
                raise ValueError(f"non-unit count increment at step {row.get('step_index')}")
            previous[step_id] = current

            confirmation = onboard.get("confirmed") or {}
            observation = row.get("obs_before") or {}
            bbox = confirmation.get("bbox")
            position = confirmation.get("position_estimate_ne")
            track_snapshot = next(
                (
                    track
                    for track in onboard.get("tracks") or []
                    if track.get("id") == confirmation.get("track_id")
                ),
                None,
            )
            track_ground = (track_snapshot or {}).get("ground_ne")
            ray = None
            angle_deg = None
            if isinstance(bbox, list) and len(bbox) == 4:
                camera_info = observation.get("camera_info") or {}
                image_size = (
                    int(camera_info.get("width", 1280)),
                    int(camera_info.get("height", 960)),
                )
                ray = pixel_camera_ray(
                    (float(bbox[0]) + float(bbox[2])) / 2.0,
                    float(bbox[3]),
                    pose=observation,
                    camera_info=camera_info,
                    image_size=image_size,
                    camera_pitch_rad=pitch,
                )
                if ray is not None:
                    direction = ray[1]
                    angle_deg = math.degrees(
                        math.atan2(direction[2], math.hypot(direction[0], direction[1]))
                    )
            events.append(
                {
                    "step": int(row["step_index"]),
                    "frame_id": f"frame_{int(row['step_index']):04d}.png",
                    "track_id": confirmation.get("track_id"),
                    "position_ne": position,
                    "track_ground_ne": track_ground,
                    "position_source": confirmation.get("position_source"),
                    "position_sigma_m": confirmation.get("position_sigma_m"),
                    "position_view_count": confirmation.get("position_view_count"),
                    "position_baseline_m": confirmation.get("position_baseline_m"),
                    "position_ray_angle_deg": confirmation.get("position_ray_angle_deg"),
                    "position_consistent": confirmation.get("position_consistent"),
                    "position_consistency_note": confirmation.get("position_consistency_note"),
                    "position_shift_from_track_ground_m": (
                        round(_distance(position, track_ground), 3)
                        if position is not None and track_ground is not None
                        else None
                    ),
                    "camera_ray_available": ray is not None,
                    "position_available": position is not None,
                    "accepted_after_pose_gate": ray is not None and position is not None,
                    "ground_ray_below_horizon_deg": angle_deg,
                    "sim_skew_ms": observation.get("sim_skew_ms"),
                    "gimbal_feedback_age_s": (observation.get("gimbal_state") or {}).get("age_s"),
                    "gimbal_feedback_fresh": (observation.get("gimbal_state") or {}).get("fresh"),
                    "camera_pose_present": isinstance(observation.get("camera_pose"), dict),
                }
            )
            event = events[-1]
            track_id = event.get("track_id")
            if track_id is not None:
                identity_histories.setdefault(str(track_id), _identity_history(rows, str(track_id)))
            event["identity_history"] = identity_histories.get(str(track_id), [])
            event["identity_score"] = next(
                (
                    call["score"]
                    for call in reversed(event["identity_history"])
                    if call.get("step") == event["step"] and call.get("score") is not None
                ),
                None,
            )
            event["analysis"] = _event_analysis(
                position,
                track_id,
                prior_events,
                members,
                counted_metadata,
                4.0,
            )
            prior_events.append(event)

    return {
        "episode": str(episode),
        "recorded_count": count_by_step,
        "recorded_confirmation_events": len(events),
        "pose_gated_count": sum(event["accepted_after_pose_gate"] for event in events),
        "events": events,
        "method": "fixed recorded path; replay logged count confirmations through current camera projection; no detector, action, or track association rerun",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path)
    parser.add_argument("--truth", type=Path, help="count truth JSON for member and duplicate analysis")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    truth = json.loads(args.truth.read_text(encoding="utf-8")) if args.truth else None
    result = {"episodes": [replay_episode(path, truth=truth) for path in args.episodes]}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
