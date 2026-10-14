"""Replay patrol/monitor identity votes by ground-position clusters."""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.camera_projection import pixel_ground_point  # noqa: E402
from scripts.score_onboard_loop import (  # noqa: E402
    _mission_metrics,
    _nearest_object,
    _object_matches_query,
    episode_world_id,
    truth_for_episode,
)

CONFIGS = ((1, False), (2, False), (3, False), (2, True), (1, True))


def _episode_paths(inputs: list[Path]) -> list[Path]:
    episodes: list[Path] = []
    for item in inputs:
        if (item / "steps.jsonl").is_file():
            episodes.append(item)
        else:
            episodes.extend(sorted(p.parent for p in item.rglob("steps.jsonl")))
    return sorted(set(episodes))


def _rows(episode: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _track_position(row: dict[str, Any], track: dict[str, Any]) -> tuple[float, float] | None:
    box = track.get("bbox")
    obs = row.get("obs_before") or {}
    camera = obs.get("camera_info") or {}
    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        u = (float(box[0]) + float(box[2])) / 2.0
        v = float(box[3])
        point = pixel_ground_point(
            u,
            v,
            pose=obs,
            camera_info=camera,
            image_size=(int(camera.get("width", 1280)), int(camera.get("height", 960))),
        )
    except (TypeError, ValueError, KeyError):
        return None
    return point if point and all(math.isfinite(value) for value in point) else None


def _query_from_rows(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        mission = ((row.get("labels") or {}).get("onboard") or {}).get("mission") or {}
        for alert in mission.get("alerts") or []:
            if isinstance(alert, str) and ":alert:" in alert:
                query, _, _ = alert.split(":alert:", 1)[1].rpartition("@")
                if query.strip():
                    return query.strip()
    for row in rows:
        task = row.get("task_text")
        if isinstance(task, str):
            match = re.search(
                r"(?:for|find|monitor|patrol)\s+([\w -]+?)(?:\s+and|\s+for\s+|\s*$)",
                task,
                re.I,
            )
            if match:
                return match.group(1).strip()
    return "object"


def _vote_kind(call: dict[str, Any]) -> str | None:
    identity = str(call.get("identity") or "").casefold()
    decision = str(call.get("decision") or "").casefold()
    if identity in {"match", "confirm", "approach", "yes", "positive"} or decision in {
        "confirm_target",
        "approach_track",
        "confirm",
        "approach",
    }:
        return "positive"
    if identity in {"no_match", "reject", "negative", "no"} or decision in {
        "reject_track",
        "reject",
    }:
        return "negative"
    return None


def _replay(
    rows: list[dict[str, Any]],
    minimum: int,
    majority: bool,
    cluster_radius_m: float = 4.0,
) -> tuple[list[str], list[dict[str, Any]]]:
    if cluster_radius_m <= 0:
        raise ValueError("cluster_radius_m must be positive")
    positions: dict[str, tuple[float, float]] = {}
    clusters: list[dict[str, Any]] = []
    query = _query_from_rows(rows)
    for row in rows:
        onboard = (row.get("labels") or {}).get("onboard") or {}
        for track in onboard.get("tracks") or []:
            if isinstance(track, dict) and track.get("id"):
                point = _track_position(row, track)
                if point is not None:
                    positions[str(track["id"])] = point
        for call in onboard.get("slow_calls") or []:
            if not isinstance(call, dict) or not call.get("track_id"):
                continue
            kind = _vote_kind(call)
            point = positions.get(str(call["track_id"]))
            if kind is None or point is None:
                continue
            cluster = min(
                clusters,
                key=lambda item: math.dist(point, item["center"]),
                default=None,
            )
            if cluster is None or math.dist(point, cluster["center"]) > cluster_radius_m:
                cluster = {
                    "center": point,
                    "members": 0,
                    "positive": 0,
                    "negative": 0,
                    "alerted": False,
                }
                clusters.append(cluster)
            count = cluster["members"]
            cluster["center"] = (
                (cluster["center"][0] * count + point[0]) / (count + 1),
                (cluster["center"][1] * count + point[1]) / (count + 1),
            )
            cluster["members"] = count + 1
            cluster[kind] += 1
            if (
                not cluster["alerted"]
                and cluster["positive"] >= minimum
                and (not majority or cluster["positive"] > cluster["negative"])
            ):
                cluster["alerted"] = True
    alerts = [
        f"s1:alert:{query}@{item['center'][0]:.1f},{item['center'][1]:.1f}"
        for item in clusters
        if item["alerted"]
    ]
    return alerts, clusters


def _world_id(episode: Path) -> str | None:
    return episode_world_id(episode)


def _truth_for(
    episode: Path,
    direct: Path | None,
    truth_dir: Path | None,
    scenario_index: Path,
) -> dict[str, Any] | None:
    if direct is not None:
        return json.loads(direct.read_text(encoding="utf-8"))
    scenarios = truth_dir or scenario_index.parent
    _, truth = truth_for_episode(episode, scenarios)
    return truth


def _set_alerts(rows: list[dict[str, Any]], alerts: list[str]) -> list[dict[str, Any]]:
    replay_rows = copy.deepcopy(rows)
    if not replay_rows:
        return replay_rows
    mission = (
        replay_rows[-1]
        .setdefault("labels", {})
        .setdefault("onboard", {})
        .setdefault("mission", {})
    )
    mission["alerts"] = alerts
    return replay_rows


def _false_alerts(
    alerts: list[str],
    clusters: list[dict[str, Any]],
    truth: dict[str, Any],
    query: str,
    radius_m: float,
) -> list[dict[str, Any]]:
    details = []
    for alert, cluster in zip(
        alerts, [item for item in clusters if item["alerted"]], strict=True
    ):
        nearest = _nearest_object(truth, cluster["center"], float("inf"))
        distance = None
        matches = False
        if nearest is not None:
            pose = nearest.get("pose") or {}
            distance = math.hypot(
                cluster["center"][0] - float(pose["y_north_m"]),
                cluster["center"][1] - float(pose["x_east_m"]),
            )
            matches = distance <= radius_m and _object_matches_query(nearest, query)
        if matches:
            continue
        details.append(
            {
                "alert": alert,
                "north_east": [round(cluster["center"][0], 2), round(cluster["center"][1], 2)],
                "positive": cluster["positive"],
                "negative": cluster["negative"],
                "nearest_distance_m": round(distance, 2) if distance is not None else None,
                "nearest_label": nearest.get("label") if nearest else None,
                "nearest_category_words": nearest.get("category_words") if nearest else None,
            }
        )
    return details


def replay_episode(
    episode: Path,
    *,
    truth: dict[str, Any] | None,
    radius_m: float,
    cluster_radius_m: float,
) -> dict[str, Any]:
    rows = _rows(episode)
    result = {
        "episode": str(episode),
        "world": _world_id(episode),
        "truth_available": truth is not None,
    }
    reason = None if truth is not None else "truth JSON could not be resolved for this world"
    for minimum, majority in CONFIGS:
        key = f"min_{minimum}_majority_{str(majority).lower()}"
        alerts, clusters = _replay(rows, minimum, majority, cluster_radius_m)
        entry = {
            "alerts": alerts,
            "alert_count": len(alerts),
            "clusters": [
                {
                    "center": [round(item["center"][0], 3), round(item["center"][1], 3)],
                    "positive": item["positive"],
                    "negative": item["negative"],
                    "members": item["members"],
                    "alerted": item["alerted"],
                }
                for item in clusters
            ],
        }
        if truth is None:
            entry.update(precision=None, recall=None, unavailable_reason=reason)
        else:
            metrics = _mission_metrics(_set_alerts(rows, alerts), truth, radius_m)
            entry.update(
                precision=metrics.get("mission_alert_precision"),
                recall=metrics.get("mission_alert_recall"),
                correct_alert_count=metrics.get("mission_alert_correct_count", 0),
                eligible_count=metrics.get("mission_alert_eligible_count", 0),
                hit_count=metrics.get("mission_alert_hit_count", 0),
            )
            if (minimum, majority) == (1, False):
                entry["false_alerts"] = _false_alerts(
                    alerts, clusters, truth, _query_from_rows(rows), radius_m
                )
        result[key] = entry
    return result


def _pooled(results: list[dict[str, Any]]) -> dict[str, Any]:
    pooled = {}
    for minimum, majority in CONFIGS:
        key = f"min_{minimum}_majority_{str(majority).lower()}"
        total_alerts = total_correct = total_hits = total_eligible = 0
        for result in results:
            entry = result[key]
            if not result["truth_available"]:
                continue
            total_alerts += int(entry["alert_count"])
            if entry.get("precision") is not None:
                total_correct += int(entry.get("correct_alert_count", 0))
            if entry.get("recall") is not None:
                total_eligible += int(entry.get("eligible_count", 0))
                total_hits += int(entry.get("hit_count", 0))
        pooled[key] = {
            "alert_count": total_alerts,
            "precision": total_correct / total_alerts if total_alerts else None,
            "recall": total_hits / total_eligible if total_eligible else None,
        }
    return pooled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episodes", nargs="+", type=Path)
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--truth-dir", type=Path)
    parser.add_argument(
        "--scenario-index",
        type=Path,
        default=ROOT / "configs/scenarios/generated-count-site/index.json",
    )
    parser.add_argument("--radius-m", type=float, default=10.0)
    parser.add_argument("--cluster-radius-m", type=float, default=4.0)
    args = parser.parse_args(argv)
    episodes = _episode_paths(args.episodes)
    if not episodes:
        parser.error("no steps.jsonl episodes found")
    output = []
    for episode in episodes:
        truth = _truth_for(episode, args.truth, args.truth_dir, args.scenario_index)
        result = replay_episode(
            episode,
            truth=truth,
            radius_m=args.radius_m,
            cluster_radius_m=args.cluster_radius_m,
        )
        output.append(result)
    print(json.dumps({"flights": output, "pooled": _pooled(output)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
