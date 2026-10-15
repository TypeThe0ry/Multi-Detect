"""Replay logged candidate boxes and poses through old/new CPU association.

No images, simulator, model, or truth are used. Ground clusters are geometric
proxies for objects, not semantic truth. Motion and detections remain fixed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_loop import (  # noqa: E402
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
)
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402
from scripts.replay_alert_votes import _episode_paths, _rows, _track_position  # noqa: E402
from scripts.replay_place_verdict_memory import _task_key, default_inputs  # noqa: E402

SKILLS = {"count", "patrol", "monitor", "area_search", "inspect"}
CONTINUING = {"count", "patrol", "monitor"}


def _loop(camera: CameraModel, config: LoopConfig) -> OnboardLoop:
    return OnboardLoop(
        camera,
        lambda pose, yaw: ActionPrimitive.hold(1),
        lambda pose, n, e: True,
        config,
    )


def _camera(row: dict[str, Any]) -> CameraModel:
    info = row["obs_before"]["camera_info"]
    return CameraModel(
        float(info["fx"]), float(info["fy"]), float(info["cx"]), float(info["cy"]),
        int(info["width"]), int(info["height"]),
    )


def _clipped(box, camera: CameraModel) -> bool:
    return box[0] <= 2 or box[1] <= 2 or box[2] >= camera.width - 2 or box[3] >= camera.height - 2


def replay_rows(
    rows: list[dict[str, Any]], *, radius_m: float = 4.0,
    reid_steps: int | None = None, reid_radius_m: float = 3.0,
) -> dict[str, Any]:
    """Compare association with re-id disabled versus the proposed configuration.

    Candidate cost uses first-look plus TTL scheduling with neutral rejections:
    no invented model positives, approach focus, second look, or exhaustion.
    Each scheduled candidate is marked judged at that frame for both variants.
    """
    if not math.isfinite(radius_m) or radius_m <= 0:
        raise ValueError("radius_m must be finite and positive")
    result: dict[str, Any] = {
        "rows": len(rows), "frames": 0, "logged_boxes": 0,
        "raw_detection_count": 0, "filtered_replay_boxes": 0,
        "unprojectable_boxes": 0,
        "baseline_exact_id_boxes": 0, "logged_slow_calls": 0,
        "causes": Counter(), "cause_flags": Counter(), "fragment_events": [],
        "skills": {}, "clusters": [],
    }
    clusters: list[dict[str, Any]] = []
    loops = {}
    old_task = None
    history_len = 0
    seen = set()
    recorded_latest = {}
    step = 0
    for row_index, row in enumerate(rows):
        ob = (row.get("labels") or {}).get("onboard") or {}
        history = (ob.get("mission") or {}).get("history") or []
        completed = history[history_len:]
        history_len = len(history)
        task = _task_key(ob, completed)
        skill = completed[0].get("skill") if completed else ob.get("skill")
        if skill not in SKILLS:
            continue
        camera = _camera(row)
        if task != old_task:
            config = LoopConfig(
                stop_on_confirm=skill not in CONTINUING,
                ground_reid_max_steps=reid_steps if skill in CONTINUING else 0,
                ground_reid_radius_m=reid_radius_m,
            )
            loops = {
                "old": _loop(camera, replace(config, ground_reid_max_steps=0)),
                "new": _loop(camera, config),
            }
            seen = set()
            recorded_latest = {}
            step = 0
            old_task = task
        result["frames"] += 1
        result["raw_detection_count"] += int(ob.get("detections") or 0)
        result["logged_slow_calls"] += len(ob.get("slow_calls") or [])
        stats = result["skills"].setdefault(skill, {
            "frames": 0, "old_ids": 0, "new_ids": 0,
            "recorded_slow_calls": 0,
            "old_slow_candidates": 0, "new_slow_candidates": 0,
            "old_first_look_candidates": 0, "new_first_look_candidates": 0,
        })
        stats["frames"] += 1
        stats["recorded_slow_calls"] += len(ob.get("slow_calls") or [])
        obs = row["obs_before"]
        pose = tuple(obs["pose"]["local_ned_m"])
        yaw = obs["attitude"]["yaw_deg"]
        tracks = ob.get("tracks") or []
        result["logged_boxes"] += len(tracks)
        # Logs retain score-sorted, filtered boxes rounded to 0.1 pixel, but no
        # raw detections/scores. Equal replay scores preserve this recorded order.
        detections = [Detection(tuple(t["bbox"])) for t in tracks]
        points = [_track_position(row, t) for t in tracks]
        sample_clusters = []
        for track, point in zip(tracks, points, strict=True):
            if point is None:
                result["unprojectable_boxes"] += 1
                sample_clusters.append(None)
                continue
            cluster = min(
                (c for c in clusters if c["task"] == task),
                key=lambda c: math.dist(point, c["center"]), default=None,
            )
            if cluster is None or math.dist(point, cluster["center"]) > radius_m:
                cluster = {
                    "task": task, "skill": skill, "center": point, "samples": 0,
                    "recorded_ids": set(), "old_ids": set(), "new_ids": set(),
                    "recorded_slow_calls": 0, "old_slow_candidates": 0,
                    "new_slow_candidates": 0, "members": {},
                }
                clusters.append(cluster)
            sample_clusters.append(cluster)
            tid = str(track["id"])
            previous = cluster["members"]
            if tid not in seen and previous:
                # Prefer the most recently observed compatible id. A close stale
                # sample alone must not hide a still-active alternative.
                near = []
                reach = math.dist(point, pose[:2])
                gate = 0.8 * max(4.0, 0.15 * reach)
                for prior_id in previous:
                    prior = recorded_latest[prior_id]
                    if math.dist(point, prior["point"]) <= gate:
                        near.append(prior)
                prior = max(near or list(previous.values()), key=lambda p: p["step"])
                age = step - prior["step"]
                distance = math.dist(point, prior["point"])
                latest = recorded_latest[prior["id"]]
                displaced = (
                    latest["step"] > prior["step"]
                    and math.dist(point, latest["point"]) > gate
                )
                if age == 0:
                    cause = "same_frame_multiple_boxes"
                elif displaced:
                    cause = "prior_id_ground_drift"
                elif age > 4:
                    cause = "lost_within_gate" if distance <= gate else "lost_and_outside_gate"
                elif distance > gate:
                    cause = "active_ground_gate"
                else:
                    cause = "active_competition_or_log_precision"
                result["causes"][cause] += 1
                flags = []
                if _clipped(track["bbox"], camera) or _clipped(prior["bbox"], camera):
                    flags.append("edge_clipped")
                if abs(pose[2] - prior["pose"][2]) > 1:
                    flags.append("altitude_change_gt_1m")
                pitch = obs["attitude"].get("pitch_deg", 0)
                if abs(pitch - prior["pitch"]) > 2:
                    flags.append("pitch_change_gt_2deg")
                if distance > gate:
                    flags.append("ground_jump_outside_gate")
                result["cause_flags"].update(flags)
                result["fragment_events"].append({
                    "row": row_index, "skill": skill, "new_id": tid,
                    "old_id": prior["id"], "cause": cause, "gap_steps": age,
                    "distance_m": round(distance, 3), "effective_gate_m": round(gate, 3),
                    "flags": flags,
                })
            seen.add(tid)
            cluster["recorded_ids"].add(tid)
            recorded_latest[tid] = {
                "id": tid, "step": step, "point": point, "bbox": track["bbox"],
                "pose": pose, "pitch": obs["attitude"].get("pitch_deg", 0),
            }
            cluster["members"][tid] = recorded_latest[tid]
            count = cluster["samples"]
            cluster["center"] = tuple((a * count + b) / (count + 1)
                                      for a, b in zip(cluster["center"], point, strict=True))
            cluster["samples"] += 1
            cluster["recorded_slow_calls"] += sum(
                call.get("track_id") == tid for call in ob.get("slow_calls") or []
            )
        by_box = {tuple(t["bbox"]): (t, c) for t, c in zip(tracks, sample_clusters, strict=True)}

        def ground(u, v, sample=row):
            return _track_position(sample, {"bbox": [u, v, u, v]})

        for variant, loop in loops.items():
            loop.camera = camera
            loop.step_index = step
            before = loop._next_id
            current = loop.update_tracks(
                detections, pose_ned=pose, yaw_deg=yaw, ground_point=ground
            )
            stats[f"{variant}_ids"] += loop._next_id - before
            needing = loop.tracks_needing_verdict(current)
            stats[f"{variant}_slow_candidates"] += len(needing)
            for track in current:
                recorded, cluster = by_box[track.bbox]
                if variant == "old":
                    result["filtered_replay_boxes"] += 1
                    result["baseline_exact_id_boxes"] += track.track_id == recorded["id"]
                if cluster is not None:
                    cluster[f"{variant}_ids"].add(track.track_id)
            for track in needing:
                stats[f"{variant}_first_look_candidates"] += track.verdict is None
                _, cluster = by_box[track.bbox]
                if cluster is not None:
                    cluster[f"{variant}_slow_candidates"] += 1
                loop.apply_verdict(track.track_id, "reject_track")
        step += 1
    for cluster in clusters:
        cluster.pop("members")
        for key in ("recorded_ids", "old_ids", "new_ids"):
            cluster[key] = sorted(cluster[key], key=lambda value: int(value.lstrip("t")))
        cluster["center"] = [round(value, 3) for value in cluster["center"]]
    result["clusters"] = clusters
    for key in ("old_ids", "new_ids", "old_slow_candidates", "new_slow_candidates",
                "old_first_look_candidates", "new_first_look_candidates"):
        result[key] = sum(stats[key] for stats in result["skills"].values())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cluster-radius-m", type=float, default=4.0)
    parser.add_argument("--reid-steps", type=int)
    parser.add_argument("--reid-radius-m", type=float, default=3.0)
    args = parser.parse_args()
    inputs = args.inputs or [p for p in default_inputs(args.source) if p.name == "campaign"]
    episodes = []
    totals = Counter()
    causes = Counter()
    flags = Counter()
    skill_totals: dict[str, Counter] = defaultdict(Counter)
    for episode in _episode_paths(inputs):
        result = replay_rows(
            _rows(episode), radius_m=args.cluster_radius_m,
            reid_steps=args.reid_steps, reid_radius_m=args.reid_radius_m,
        )
        result["episode"] = str(episode)
        result["steps_sha256"] = hashlib.sha256((episode / "steps.jsonl").read_bytes()).hexdigest()
        episodes.append(result)
        totals.update({key: value for key, value in result.items() if isinstance(value, int)})
        causes.update(result["causes"])
        flags.update(result["cause_flags"])
        for skill, counts in result["skills"].items():
            skill_totals[skill].update(counts)
    report = {
        "settings": {"cluster_radius_m": args.cluster_radius_m,
                     "reid_max_steps": args.reid_steps, "reid_radius_m": args.reid_radius_m},
        "totals": dict(totals), "causes": dict(causes), "cause_flags": dict(flags),
        "skills": dict(skill_totals), "episodes": episodes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({key: value for key, value in report.items() if key != "episodes"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
