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
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.camera_projection import project_target_bbox  # noqa: E402
from multidetect.simulator.task_semantics import task_equivalent  # noqa: E402


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _true_target(truth: dict[str, Any]) -> dict[str, Any]:
    return next(t for t in truth["targets"] if t.get("true_target"))


def _nearest_object(truth: dict[str, Any], north_east, radius_m: float):
    best, best_distance = None, radius_m
    for obj in truth["targets"]:
        distance = math.hypot(
            north_east[0] - float(obj["pose"]["y_north_m"]),
            north_east[1] - float(obj["pose"]["x_east_m"]),
        )
        if distance <= best_distance:
            best, best_distance = obj, distance
    return best


def _true_box(episode: Path, observation: dict[str, Any], truth: dict[str, Any]):
    target = _true_target(truth)
    seg_name = observation.get("segmentation_path")
    if seg_name and truth.get("segmentation_labels"):
        from multidetect.sim_teacher.segmentation import boxes_from_segmentation, load_labels_map

        labels = load_labels_map(episode / seg_name.replace("\\", "/"))
        boxes = boxes_from_segmentation(labels, truth, min_area_px=4, min_box_px=2)
        hit = boxes.get(str(target["target_id"]))
        return (list(hit["box"]) if hit else None), "segmentation"
    pose = target["pose"]
    info = observation["camera_info"]
    box = project_target_bbox(
        target_ned=(pose["y_north_m"], pose["x_east_m"], pose["z_up_m"]),
        pose=observation,
        camera_info=info,
        image_size=(int(info["width"]), int(info["height"])),
        target_size_m=tuple(target.get("size_m", (3.0, 1.8, 1.5))),
        target_yaw_rad=pose.get("yaw_rad", 0.0),
        target_bottom_offset_m=target.get("bottom_offset_m"),
    )
    return (list(box) if box else None), "projection"


def score_episode(
    episode: Path, truth: dict[str, Any], *, radius_m: float = 10.0
) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    meta = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    target = _true_target(truth)
    true_ne = (float(target["pose"]["y_north_m"]), float(target["pose"]["x_east_m"]))
    confirmed_row = None
    slow_calls, slow_latency, fast_latency, flown = 0, [], [], 0.0
    previous = None
    for row in rows:
        onboard = (row.get("labels") or {}).get("onboard") or {}
        slow_calls += len(onboard.get("slow_calls", []))
        slow_latency += [
            c["latency_s"] for c in onboard.get("slow_calls", []) if c.get("latency_s") is not None
        ]
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
    if confirmed_row is None:
        return result
    confirmed = confirmed_row["labels"]["onboard"]["confirmed"]
    estimate = confirmed.get("position_estimate_ne")
    distance = (
        math.hypot(estimate[0] - true_ne[0], estimate[1] - true_ne[1]) if estimate else math.inf
    )
    true_box, source = _true_box(episode, confirmed_row["obs_before"], truth)
    overlap = _iou(confirmed["bbox"], true_box) if true_box else 0.0
    result.update(
        confirm_step=confirmed_row["step_index"],
        confirm_rule=confirmed.get("rule"),
        position_error_m=round(distance, 2) if math.isfinite(distance) else None,
        box_iou_with_true_target=round(overlap, 3),
        identity_source=source,
        success=bool(distance <= radius_m and overlap >= 0.1),
    )
    result["false_confirm"] = not result["success"]
    if result["false_confirm"] and estimate:
        hit = _nearest_object(truth, estimate, radius_m)
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
