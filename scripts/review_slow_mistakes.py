"""Review slow-model calls against recorded segmentation truth."""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from slow_system_runtime import image_list_for_row  # noqa: E402

from multidetect.sim_teacher.recording import load_depth_array  # noqa: E402
from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)
from multidetect.simulator.task_semantics import task_equivalent  # noqa: E402

ACCEPT_DECISIONS = {"approach", "approach_track", "confirm", "confirm_target", "search"}


def _iou(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    ub = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    return inter / (ua + ub - inter) if ua + ub - inter > 0 else 0.0


def _intersects(a: list[float], b: list[float]) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _truth_for(world: str, explicit: Path | None) -> tuple[dict[str, Any], Path]:
    if explicit:
        return json.loads(explicit.read_text(encoding="utf-8")), explicit
    for directory in (
        ROOT / "configs/scenarios/generated-v2",
        ROOT / "configs/scenarios/generated-count-site",
        ROOT / "configs/scenarios/generated-confusers",
        ROOT / "configs/scenarios/generated-confusers-2",
    ):
        matches = list(directory.glob(f"{world}.truth.json"))
        if matches:
            path = matches[0]
            return json.loads(path.read_text(encoding="utf-8")), path
    raise FileNotFoundError(f"could not locate truth for world {world}")


def _episode_dirs(inputs: list[Path]) -> list[Path]:
    result = []
    for path in inputs:
        if (path / "steps.jsonl").is_file():
            result.append(path)
        else:
            result.extend(sorted(p.parent for p in path.rglob("steps.jsonl")))
    return result


def _target_name(obj: dict[str, Any] | None) -> str:
    if not obj:
        return "background"
    return str(obj.get("label") or obj.get("category") or obj.get("target_id") or "unknown")


def _decision_accept(decision: str) -> bool:
    lowered = decision.casefold()
    return lowered in ACCEPT_DECISIONS or lowered.startswith(("approach_", "confirm_"))


def _jpeg_data(image: Image.Image) -> str:
    stream = io.BytesIO()
    image.save(stream, format="JPEG", quality=82)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _range_estimate(call: dict[str, Any], track: dict[str, Any]) -> Any:
    for source in (call, track):
        for key in ("range_m", "distance_m", "range_estimate_m"):
            if source.get(key) is not None:
                return source[key]
    return None


def _depth_range(episode: Path, obs: dict[str, Any], box: list[float]) -> float | None:
    depth_name = obs.get("depth_path")
    if not depth_name:
        return None
    path = episode / str(depth_name).replace("\\", "/")
    if not path.is_file():
        return None
    try:
        depth = np.asarray(load_depth_array(path), dtype=float)
        height, width = depth.shape[:2]
        x1, y1, x2, y2 = (max(0, int(v)) for v in box)
        x2, y2 = min(width, int(box[2])), min(height, int(box[3]))
        values = depth[y1:y2, x1:x2]
        values = values[np.isfinite(values) & (values > 0)]
        return round(float(np.median(values)), 2) if values.size else None
    except (OSError, ValueError):
        return None


def review_episode(
    episode: Path, truth: dict[str, Any], max_per_group: int | None = None
) -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    task_obj = next((obj for obj in truth.get("targets", []) if obj.get("true_target")), None)
    world = episode.parent.parent.name if episode.parent.name == "episodes" else episode.parent.name
    records: list[dict[str, Any]] = []
    group_counts: Counter[tuple[str, str, str]] = Counter()
    seg_cache: dict[str, dict[str, Any]] = {}
    for row in rows:
        onboard = (row.get("labels") or {}).get("onboard") or {}
        calls = onboard.get("slow_calls") or []
        if not calls:
            continue
        obs = row.get("obs_before") or {}
        rgb_path = episode / str(obs.get("rgb_path", "")).replace("\\", "/")
        seg_path = episode / str(obs.get("segmentation_path", "")).replace("\\", "/")
        if not rgb_path.is_file():
            continue
        with Image.open(rgb_path) as opened:
            width, height = opened.size
        tracks = {str(track.get("id")): track for track in onboard.get("tracks") or []}
        candidates = []
        for track in onboard.get("tracks") or []:
            box = track.get("bbox")
            if box and width and height:
                candidates.append(
                    {
                        "track_id": str(track.get("id")),
                        "box_xyxy_normalized": [
                            float(box[0]) / width,
                            float(box[1]) / height,
                            float(box[2]) / width,
                            float(box[3]) / height,
                        ],
                    }
                )
        if seg_path.is_file() and str(seg_path) not in seg_cache:
            seg_cache[str(seg_path)] = boxes_from_segmentation(
                load_labels_map(seg_path), truth, min_area_px=4, min_box_px=2
            )
        objects = seg_cache.get(str(seg_path), {})
        for call in calls:
            track_id = str(call.get("track_id"))
            track = tracks.get(track_id, {})
            box = [float(v) for v in track.get("bbox", [])]
            if len(box) != 4:
                continue
            best_id, best_iou = None, 0.0
            partial = False
            for target_id, entry in objects.items():
                target_box = list(entry["box"])
                score = _iou(box, target_box)
                if score > best_iou:
                    best_id, best_iou = target_id, score
                partial = partial or _intersects(box, target_box)
            judged_obj = (
                next(
                    (
                        obj
                        for obj in truth.get("targets", [])
                        if str(obj.get("target_id")) == best_id
                    ),
                    None,
                )
                if best_id and best_iou >= 0.3
                else None
            )
            judged_label = (
                _target_name(judged_obj) if judged_obj else ("partial" if partial else "background")
            )
            fits = bool(judged_obj and task_obj and task_equivalent(task_obj, judged_obj))
            accepts = _decision_accept(str(call.get("decision", "")))
            if accepts and fits:
                classification = "correct_accept"
            elif not accepts and not fits:
                classification = "correct_reject"
            elif accepts:
                classification = "false_accept"
            else:
                classification = "false_reject"
            group = (_target_name(task_obj), judged_label, classification)
            if max_per_group is not None and group_counts[group] >= max_per_group:
                continue
            group_counts[group] += 1
            row_for_crop = {
                "image_path": str(rgb_path),
                "task_text": row.get("task_text", ""),
                "memory": "No prior confirmed target; search in progress.",
                "candidates": candidates,
                "_crop_policy": "candidate_zoom_v2",
                "focus_track_id": track_id,
            }
            with Image.open(rgb_path) as opened:
                full = opened.convert("RGB")
                drawn = full.copy()
                ImageDraw.Draw(drawn).rectangle(
                    box, outline=(255, 0, 0), width=max(2, round(width / 400))
                )
                images = image_list_for_row(
                    row_for_crop, max_side=448, crop_policy="candidate_zoom_v2"
                )
            records.append(
                {
                    "world": world,
                    "episode": str(episode),
                    "step": row.get("step_index"),
                    "task_object": _target_name(task_obj),
                    "judged_object": judged_label,
                    "judged_target_id": best_id if judged_obj else None,
                    "classification": classification,
                    "decision": call.get("decision"),
                    "reason": call.get("reason"),
                    "confidence": call.get("confidence"),
                    "range_m": _range_estimate(call, track) or _depth_range(episode, obs, box),
                    "track_id": track_id,
                    "box": box,
                    "frame_jpeg": _jpeg_data(drawn),
                    "crop_jpegs": [_jpeg_data(image) for image in images[1:]],
                    "fits_task": fits,
                }
            )
    return records


def build_report(records: list[dict[str, Any]], output: Path, summary_path: Path) -> dict[str, Any]:
    groups: defaultdict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[(record["task_object"], record["judged_object"], record["classification"])].append(
            record
        )
    confusion = Counter(
        (r["task_object"], r["judged_object"])
        for r in records
        if r["classification"] in {"false_accept", "false_reject"}
    )
    worlds = Counter((r["world"], r["classification"]) for r in records)
    summary = {
        "total_calls": len(records),
        "classification_counts": dict(Counter(r["classification"] for r in records)),
        "confusion_counts": {f"{a} -> {b}": n for (a, b), n in confusion.items()},
        "world_counts": {f"{a}:{b}": n for (a, b), n in worlds.items()},
        "groups": {"|".join(key): len(value) for key, value in groups.items()},
    }
    parts = [
        "<!doctype html><meta charset='utf-8'><title>Slow model review</title><style>body{font:14px sans-serif}article{border:1px solid #ccc;padding:8px;margin:8px}img{max-width:420px;margin:3px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:4px}</style>",  # noqa: E501
        "<h1>Slow model review</h1>",
        f"<pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>",
        "<table><tr><th>task</th><th>judged</th><th>classification</th><th>count</th></tr>",
    ]
    for key, value in sorted(groups.items()):
        parts.append(
            f"<tr><td>{html.escape(key[0])}</td><td>{html.escape(key[1])}</td><td>{html.escape(key[2])}</td><td>{len(value)}</td></tr>"
        )
    parts.append("</table>")
    for key, value in sorted(groups.items()):
        parts.append(f"<h2>{html.escape(' / '.join(key))} ({len(value)})</h2>")
        for record in value:
            parts.append(
                f"<article><b>world {html.escape(record['world'])}, step {record['step']}, track {html.escape(record['track_id'])}</b><br>decision={html.escape(str(record['decision']))}; confidence={record['confidence']}; range={record['range_m']}<br>{html.escape(str(record['reason'] or ''))}<br><img src='data:image/jpeg;base64,{record['frame_jpeg']}'>"  # noqa: E501
                + "".join(
                    f"<img src='data:image/jpeg;base64,{data}'>" for data in record["crop_jpegs"]
                )
                + "</article>"
            )
    output.write_text("\n".join(parts) + "\n", encoding="utf-8", newline="\n")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--max-per-group", type=int)
    parser.add_argument("--only-mistakes", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    all_records = []
    for episode in _episode_dirs(args.inputs):
        world = (
            episode.parent.parent.name if episode.parent.name == "episodes" else episode.parent.name
        )
        truth, _ = _truth_for(world, args.truth)
        all_records.extend(review_episode(episode, truth, args.max_per_group))
    if args.only_mistakes:
        all_records = [
            r for r in all_records if r["classification"] in {"false_accept", "false_reject"}
        ]
    args.out.mkdir(parents=True, exist_ok=True)
    build_report(all_records, args.out / "slow_mistakes.html", args.out / "slow_mistakes.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
