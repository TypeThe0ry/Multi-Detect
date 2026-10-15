#!/usr/bin/env python3
"""Build an offline identity set from slow calls recorded during onboard flights.

The builder only reads flight logs and scenario truth.  It never starts a model,
simulator, llama-server, or container, and image files are referenced in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.onboard_decision import (  # noqa: E402
    identity_of,
    parse_onboard_decision,
)
from multidetect.simulator.task_semantics import CATEGORY_ZH, task_equivalent  # noqa: E402
from scripts.replay_alert_votes import _track_position  # noqa: E402
from scripts.score_onboard_loop import (  # noqa: E402
    _nearest_object,
    _object_matches_query,
    episode_world_id,
    truth_for_episode,
)

MEMORY = "No prior confirmed target; search in progress."
DEFAULT_OUT = ROOT / "artifacts/datasets/flight-identity-eval-v1"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _relative(path: Path) -> str:
    resolved = path.resolve(strict=True)
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _world_split(world: str, indexes: list[Path]) -> str | None:
    matches: list[str] = []
    for index in indexes:
        try:
            value = _json(index)
        except (OSError, json.JSONDecodeError):
            continue
        for item in value.get("worlds", []):
            if item.get("world_id") == world or Path(str(item.get("world_file", ""))).stem == world:
                split = item.get("split")
                if split:
                    matches.append(str(split))
    if len(set(matches)) > 1:
        raise ValueError(f"Conflicting index splits for {world}: {sorted(set(matches))}")
    return matches[0] if matches else None


def _episodes(inputs: list[Path]) -> list[Path]:
    result: set[Path] = set()
    for item in inputs:
        item = item.resolve()
        if item.is_file() and item.name == "steps.jsonl":
            result.add(item.parent)
        elif (item / "steps.jsonl").is_file():
            result.add(item)
        elif item.is_dir():
            result.update(p.parent for p in item.rglob("steps.jsonl"))
    return sorted(result)


def _campaign_name(episode: Path) -> str:
    parts = episode.parts
    if "campaign" in parts:
        i = parts.index("campaign")
        return _relative(Path(*parts[: i + 1]))
    if "onboard-loop" in parts:
        i = parts.index("onboard-loop")
        return _relative(Path(*parts[: i + 2]))
    return episode.parent.parent.name


def _config_for_episode(episode: Path) -> dict[str, Any]:
    for parent in (episode, *episode.parents):
        config = parent / "onboard.json"
        if config.is_file():
            try:
                return _json(config)
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def _model_for_episode(episode: Path) -> str | None:
    config = _config_for_episode(episode)
    return next(
        (str(config[key]) for key in ("gguf", "model", "slow_model") if config.get(key)), None
    )


def _task_for_call(
    row: dict[str, Any],
    call: dict[str, Any],
    config: dict[str, Any],
    plan: dict[str, Any],
    alerts: dict[str, str],
) -> tuple[str, str]:
    """Recover recorded query text; identify every inferred fallback explicitly."""
    onboard = (row.get("labels") or {}).get("onboard") or {}
    original = str(config.get("task_text") or row.get("task_text") or "")
    for source, name in ((call, "call"), (onboard, "onboard")):
        for key in ("perception_task_text", "task_text"):
            if isinstance(source.get(key), str) and source[key]:
                return source[key], f"recorded_{name}_{key}"
    mission = onboard.get("mission")
    if not mission and not config.get("mission"):
        return original, "onboard_config" if config.get("task_text") else "recorded_task_text"
    if config.get("mission") in {"find", "count", "find_and_inspect"}:
        return original, "onboard_config_template"
    # next_action may advance the step before labels are written. If history
    # grew on this cycle, the final completed step is the one that made calls.
    step_id = onboard.get("step")
    if row.get("_completed_call_step"):
        step_id = row["_completed_call_step"]
    for step in plan.get("steps", []):
        if step.get("id") == step_id:
            query = (step.get("params") or {}).get("query")
            if isinstance(query, str):
                if query.isascii():
                    return f"Find the {query} and record its position.", "recorded_plan_query"
                return original, "onboard_config_non_ascii_query"
    query = alerts.get(str(step_id))
    if query:
        return f"Find the {query} and record its position.", "recorded_alert_query"
    # Old mission artifacts retained skills but omitted planner queries. The
    # configured task is better evidence than scenario truth for these calls.
    patterns = (
        r"\bfind\s+(?:the\s+)?(.+?)(?:,|\s+and\s+|$)",
        r"\bcount\s+(?:the\s+)?(.+?)(?:\s+in\s+|\s+and\s+|$)",
        r"\b(?:watching\s+)?for\s+(.+?)(?:\s+for\s+|,|\s+and\s+|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, original, re.I)
        if match:
            query = match.group(1).rstrip(". ")
            # Catalog plurals in these historical single-query campaigns.
            query = re.sub(r"\b(backpacks|cones|hydrants|persons)\b", lambda m: m[0][:-1], query)
            return f"Find the {query} and record its position.", "inferred_config_query"
    return original, "unrecoverable_mission_query"


def _task_from_plan(row: dict[str, Any], original: str) -> str:
    onboard = (row.get("labels") or {}).get("onboard") or {}
    mission = onboard.get("mission") or {}
    plan = mission.get("plan") if isinstance(mission.get("plan"), dict) else {}
    step_id = onboard.get("step")
    for step in plan.get("steps", []):
        if not isinstance(step, dict) or (step_id is not None and step.get("id") != step_id):
            continue
        params = step.get("params") or {}
        query = params.get("query")
        if isinstance(query, str) and query.isascii():
            return f"Find the {query} and record its position."
    return original


def _task_query(text: str) -> str:
    # Keep the exact text for model inputs. This fallback is only used by the
    # truth matcher, whose helper intentionally uses the final noun.
    match = re.search(
        r"find(?: and record)?\s+(?:the\s+)?(.+?)(?:\s+and record.*)?[.!?]?$", text, re.I
    )
    query = match.group(1).strip() if match else text
    chinese_color = next(
        (
            color
            for word, color in (
                ("红色", "red"),
                ("蓝色", "blue"),
                ("白色", "white"),
                ("橙色", "orange"),
                ("棕色", "brown"),
            )
            if word in query
        ),
        None,
    )
    for category, chinese in CATEGORY_ZH.items():
        if chinese.casefold() in query.casefold():
            return " ".join(filter(None, [chinese_color, category.replace("_", " ")]))
    return query


def _truth_match(nearest: dict[str, Any] | None, truth: dict[str, Any], query: str) -> bool:
    if nearest is None:
        return False
    query = query.casefold().strip()
    chinese_color = next(
        (
            color
            for word, color in (
                ("红色", "red"),
                ("蓝色", "blue"),
                ("白色", "white"),
                ("橙色", "orange"),
                ("棕色", "brown"),
            )
            if word in query
        ),
        None,
    )
    for category, chinese in CATEGORY_ZH.items():
        if chinese.casefold() in query:
            query = " ".join(filter(None, [chinese_color, category.replace("_", " ")]))
            break
    query = re.sub(r"\b(backpacks|cones|hydrants|persons)\b", lambda m: m[0][:-1], query)
    objects = truth.get("targets", [])
    # The shared helper uses the final noun. Prefer the longest complete phrase
    # when available, so "electrical box" cannot also name "cardboard box".
    lengths = []
    for obj in objects:
        phrases = [*(obj.get("category_words") or obj.get("semantic_words") or [])]
        phrases.append(str(obj.get("class") or obj.get("category") or "").replace("_", " "))
        lengths.append(max((len(p) for p in phrases if p and p.casefold() in query), default=0))
    longest = max(lengths, default=0)
    color_match = re.search(r"\b(red|blue|white|orange|brown|green|yellow|black)\b", query)
    color = color_match[0] if color_match else None
    for obj, length in zip(objects, lengths, strict=True):
        if (longest and length != longest) or not (length or _object_matches_query(obj, query)):
            continue
        requested = {**obj, "color": color}
        if task_equivalent(requested, nearest):
            return True
    return False


def _row_for_call(
    episode: Path,
    row: dict[str, Any],
    call: dict[str, Any],
    truth: dict[str, Any],
    radius_m: float,
    split: str,
    model: str | None,
    query_text: str | None = None,
    task_text: str | None = None,
    task_text_source: str = "recorded_task_text",
) -> tuple[dict[str, Any] | None, str]:
    onboard = (row.get("labels") or {}).get("onboard") or {}
    track_id = str(call.get("track_id") or "")
    tracks = [
        track for track in onboard.get("tracks", []) if isinstance(track, dict) and track.get("id")
    ]
    focus = next((track for track in tracks if str(track["id"]) == track_id), None)
    if focus is None or not isinstance(focus.get("bbox"), list):
        return None, "missing_track"
    # The transient model row contained all shown tracks, but steps.jsonl only
    # records the track table and the call's focus id. Reconstructing the rest
    # would claim candidates the model may not have seen, so preserve focus only.
    ordered = [focus]
    image_size = (row.get("obs_before") or {}).get("camera_info") or {}
    width, height = int(image_size.get("width", 1280)), int(image_size.get("height", 960))
    candidates = []
    for index, track in enumerate(ordered):
        box = track.get("bbox")
        if not isinstance(box, list) or len(box) != 4:
            continue
        candidates.append(
            {
                "track_id": f"track-{index + 1}",
                "box_xyxy_normalized": [
                    round(max(0.0, min(1.0, float(value) / (width if i % 2 == 0 else height))), 4)
                    for i, value in enumerate(box)
                ],
            }
        )
    if not candidates:
        return None, "missing_box"
    focus_model_id = candidates[0]["track_id"]
    point = focus.get("ground_ne")
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        point = _track_position(row, focus)
    if point is not None and not all(math.isfinite(float(v)) for v in point[:2]):
        point = None
    nearest = _nearest_object(truth, point, float("inf")) if point is not None else None
    distance = None
    if nearest is not None and point is not None:
        pose = nearest.get("pose") or {}
        distance = math.hypot(
            float(point[0]) - float(pose["y_north_m"]), float(point[1]) - float(pose["x_east_m"])
        )
    recorded_task_text = str(row.get("task_text") or "Find the target.")
    task_text = task_text or _task_from_plan(row, recorded_task_text)
    query = _task_query(query_text or task_text)
    matched = (
        nearest is not None
        and distance is not None
        and distance <= radius_m
        and _truth_match(nearest, truth, query)
    )
    label_type = (
        "truth_match"
        if matched
        else (
            "truth_distractor"
            if nearest is not None and distance is not None and distance <= radius_m
            else "unmatched"
        )
    )
    target = {
        "decision": "confirm_target" if matched else "reject_track",
        "args": {"track_id": focus_model_id, "region": None, "duration_s": None},
        "reason": "Candidate class and color match the task."
        if matched
        else (
            f"Candidate is {nearest.get('label') or nearest.get('class')}; task asks for {query}."
            if nearest is not None and label_type != "unmatched"
            else "Candidate has no reliable nearby scenario truth object."
        ),
    }
    parse_onboard_decision(target)
    rgb_name = str((row.get("obs_before") or {}).get("rgb_path") or "").replace("\\", "/")
    rgb_path = (episode / rgb_name).resolve()
    if not rgb_path.is_file():
        return None, "missing_image"
    sample_key = json.dumps(
        [episode_world_id(episode), _relative(rgb_path), task_text, candidates, target],
        sort_keys=True,
        ensure_ascii=False,
    )
    result = {
        "sample_id": hashlib.sha256(sample_key.encode("utf-8")).hexdigest()[:24],
        "world": episode_world_id(episode),
        "split": "test",
        "source_split": split,
        "image_path": _relative(rgb_path),
        "task_text": task_text,
        "recorded_task_text": recorded_task_text,
        "task_query": query,
        "task_text_source": task_text_source,
        "task_text_exact": not task_text_source.startswith(("inferred_", "unrecoverable_")),
        "candidates": candidates,
        "focus_track_id": focus_model_id,
        "memory": MEMORY,
        "assistant_target": target,
        "campaign": _campaign_name(episode),
        "label_type": label_type,
        "recorded_decision": call.get("decision"),
        "recorded_identity": call.get("identity"),
        "recorded_reason": call.get("reason"),
        "recorded_error": call.get("error"),
        "truth_category": (nearest.get("class") or nearest.get("category") or nearest.get("label"))
        if nearest
        else None,
        "truth_distance_m": round(distance, 4) if distance is not None else None,
        "model": model,
        "model_source": "onboard_config_gguf" if model else "unavailable_in_recording",
        "truth_target_id": nearest.get("target_id") if nearest else None,
        "truth_method": "nearest_ground_position",
        "episode": _relative(episode),
        "step_index": row.get("step_index"),
        "recorded_track_id": track_id,
        "ground_ne": list(point) if point is not None else None,
        "candidate_boxes_source": (
            "focus box from recorded track table; original candidate list was not logged"
        ),
    }
    return result, label_type


def build(
    inputs: list[Path],
    out: Path,
    *,
    indexes: list[Path],
    allow_train: bool = False,
    radius_m: float = 4.0,
    include_unmatched: bool = False,
) -> dict[str, Any]:
    if not math.isfinite(radius_m) or radius_m <= 0:
        raise ValueError("radius_m must be finite and positive")
    rows: list[dict[str, Any]] = []
    unmatched_rows: list[dict[str, Any]] = []
    stats = Counter()
    pair_counts = Counter()
    confusion_counts = Counter()
    old_correct = old_reject = reject_total = 0
    seen: set[tuple[str, str]] = set()
    skipped = Counter()
    episodes = _episodes(inputs)
    for episode in episodes:
        world = episode_world_id(episode)
        if not world:
            skipped["missing_world"] += 1
            continue
        split = _world_split(world, indexes)
        if split != "test" and not (split == "train" and allow_train):
            skipped[f"world_split_{split or 'unknown'}"] += 1
            continue
        scenario_dir = next(
            (index.parent for index in indexes if (index.parent / f"{world}.truth.json").is_file()),
            None,
        )
        _, truth = truth_for_episode(episode, scenario_dir)
        if not truth:
            skipped["missing_truth"] += 1
            continue
        model = _model_for_episode(episode)
        config = _config_for_episode(episode)
        try:
            steps = [
                json.loads(line)
                for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            skipped["bad_steps"] += 1
            continue
        plan = {}
        alerts = {}
        for step in steps:
            mission = ((step.get("labels") or {}).get("onboard") or {}).get("mission") or {}
            if (mission.get("plan") or {}).get("steps"):
                plan = mission["plan"]
            for alert in mission.get("alerts") or []:
                if isinstance(alert, str) and ":alert:" in alert:
                    step_id, payload = alert.split(":alert:", 1)
                    query, separator, _ = payload.rpartition("@")
                    if separator and query:
                        alerts[step_id] = query
        history_count = 0
        for step in steps:
            onboard = (step.get("labels") or {}).get("onboard") or {}
            history = (onboard.get("mission") or {}).get("history") or []
            if len(history) > history_count:
                step["_completed_call_step"] = history[-1].get("step")
            history_count = len(history)
            for call in onboard.get("slow_calls") or []:
                if not isinstance(call, dict):
                    skipped["bad_call"] += 1
                    continue
                if call.get("remembered"):
                    stats["remembered_skipped"] += 1
                    continue
                stats["real_calls"] += 1
                task_text, task_source = _task_for_call(step, call, config, plan, alerts)
                if task_source == "unrecoverable_mission_query" or not task_text:
                    skipped["unrecoverable_task"] += 1
                    continue
                item, kind = _row_for_call(
                    episode,
                    step,
                    call,
                    truth,
                    radius_m,
                    split or "test",
                    model,
                    task_text=task_text,
                    task_text_source=task_source,
                )
                if item is None:
                    skipped[kind] += 1
                    continue
                stats[kind] += 1
                key = (
                    item["image_path"],
                    json.dumps(item["candidates"][0]["box_xyxy_normalized"], sort_keys=True),
                )
                if key in seen:
                    stats["dedupe_skipped"] += 1
                    continue
                seen.add(key)
                if item["label_type"] == "unmatched":
                    unmatched_rows.append(item)
                if item["label_type"] == "unmatched" and not include_unmatched:
                    stats["unmatched_excluded"] += 1
                    continue
                rows.append(item)
                pair = (item["task_query"], item["truth_category"] or "unmatched")
                pair_counts[pair] += 1
                expected = item["assistant_target"]["decision"]
                recorded = item.get("recorded_decision")
                correct = identity_of(str(recorded)) == identity_of(expected)
                old_correct += correct
                if not correct:
                    confusion_counts[pair] += 1
                if identity_of(str(recorded)) is None:
                    stats["recorded_identity_unavailable"] += 1
                if expected == "reject_track":
                    reject_total += 1
                    old_reject += recorded == "reject_track"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "test.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for item in rows:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    with (out / "unmatched.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for item in unmatched_rows:
            stream.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1,
        "dataset": "flight-identity-eval-v1",
        "split": "test",
        "crop_policy": "candidate_zoom_v2",
        "radius_m": radius_m,
        "unmatched_excluded_by_default": not include_unmatched,
        "counts": {
            **dict(stats),
            "rows_written": len(rows),
            "episodes_seen": len(episodes),
            "match_rows": sum(r["label_type"] == "truth_match" for r in rows),
            "distractor_rows": sum(r["label_type"] == "truth_distractor" for r in rows),
            "inferred_task_rows": sum(not r["task_text_exact"] for r in rows),
            "unknown_model_rows": sum(r["model"] is None for r in rows),
        },
        "pair_counts": {
            f"{task} || {category}": count
            for (task, category), count in sorted(pair_counts.items())
        },
        "top_confusion_pairs": [
            {
                "task_query": task,
                "truth_category": category,
                "error_count": count,
                "pair_count": pair_counts[(task, category)],
            }
            for (task, category), count in confusion_counts.most_common(20)
        ],
        "recorded_old_model": {
            "identity_accuracy": old_correct / len(rows) if rows else None,
            "identity_correct_count": old_correct,
            "distractor_rejection_rate": old_reject / reject_total if reject_total else None,
            "rows_considered": len(rows),
            "distractor_rows": reject_total,
            "distractor_rejection_count": old_reject,
        },
        "skipped": dict(skipped),
        "inputs": [str(path) for path in inputs],
        "model_paths": sorted({item["model"] for item in rows if item.get("model")}),
        "task_text_sources": dict(Counter(r["task_text_source"] for r in rows)),
        "campaign_counts": dict(Counter(r["campaign"] for r in rows)),
        "notes": (
            "Frame is obs_before.rgb_path. Only the focus box is retained because "
            "the original candidate list was not logged. Mission per-step query "
            "text is recovered from recorded call/plan/alert when available, otherwise "
            "inferred from the configured task and marked task_text_exact=false. "
            "Standalone task text is preserved. Models missing from copied configs "
            "are null. Baseline errors include missing/invalid recorded decisions."
        ),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--index", action="append", type=Path, dest="indexes")
    parser.add_argument("--allow-train", action="store_true")
    parser.add_argument("--radius-m", type=float, default=4.0)
    parser.add_argument("--include-unmatched", action="store_true")
    args = parser.parse_args(argv)
    onboard_eval_root = ROOT / "artifacts/datasets/onboard-loop"
    inputs = args.inputs or [
        ROOT / "artifacts/benchmarks/mission-suite-v1-r8",
        ROOT / "artifacts/benchmarks/mission-suite-v1-r9",
        *sorted(path for path in onboard_eval_root.glob("eval-*") if path.is_dir()),
    ]
    indexes = args.indexes or [path for path in (ROOT / "configs/scenarios").glob("*/index.json")]
    print(
        json.dumps(
            build(
                inputs,
                args.out,
                indexes=indexes,
                allow_train=args.allow_train,
                radius_m=args.radius_m,
                include_unmatched=args.include_unmatched,
            ),
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
