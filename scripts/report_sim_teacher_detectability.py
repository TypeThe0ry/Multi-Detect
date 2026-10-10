#!/usr/bin/env python3
# ruff: noqa: E501
"""Report detector detectability against evaluator-only SIM teacher labels.

``perception.jsonl`` is the observation side of the bridge.  This script joins
it to ``steps.jsonl`` only for evaluation and never passes labels to a detector.
Rows without labels, missing/incomplete episodes, and missing perception files
are counted explicitly so a partial campaign cannot look like a zero-recall
result.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _angle_error(left: float, right: float) -> float:
    return (float(left) - float(right) + 180.0) % 360.0 - 180.0


def _labels(step: dict[str, Any]) -> dict[str, Any]:
    raw = step.get("labels")
    if not isinstance(raw, dict):
        return {}
    return raw


def _target_class(episode: Path, task_text: str | None = None) -> str:
    """Use scenario truth when adjacent to an episode, then task text fallback."""
    # A recording does not copy truth into the episode.  Resolve conventional
    # scenario truth locations without making the evaluator label part of the
    # perception input contract.
    candidates = [Path("configs/scenarios/urban_inspection_multirotor_001.truth.json")]
    for parent in (episode, *episode.parents):
        candidates.append(
            parent / "configs" / "scenarios" / "urban_inspection_multirotor_001.truth.json"
        )
    for candidate in candidates:
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        targets = document.get("targets") if isinstance(document, dict) else None
        if isinstance(targets, list) and targets and isinstance(targets[0], dict):
            target = targets[0]
            for key in ("class", "label", "target_id"):
                value = target.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    text = (task_text or "").strip()
    if text:
        # Keep the free-form task visible and deterministic when no scenario
        # truth is available; do not pretend an arbitrary language token is a
        # detector class.
        match = re.search(r"([A-Za-z][A-Za-z0-9_-]*(?:_[A-Za-z0-9_-]+)+)", text)
        if match:
            return match.group(1)
    return "unknown"


def _episode_dirs(root: Path) -> list[Path]:
    if (root / "steps.jsonl").is_file():
        return [root]
    return sorted({path.parent for path in root.rglob("steps.jsonl")})


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
            rows.append(value)
    return rows


def _scenario_name(episode: Path, dataset_root: Path) -> str:
    try:
        relative = episode.relative_to(dataset_root)
    except ValueError:
        relative = episode
    parts = relative.parts
    for part in parts:
        if part != "episodes":
            return part
    return dataset_root.name


def evaluate_episode(
    episode: Path,
    *,
    dataset_root: Path | None = None,
    tolerance_deg: float = 5.0,
    include_incomplete: bool = False,
) -> dict[str, Any]:
    """Evaluate one episode and return JSON-compatible aggregate metrics."""
    dataset_root = dataset_root or episode
    episode_meta: dict[str, Any] = {}
    try:
        episode_meta = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    status = episode_meta.get("status")
    if not include_incomplete and status not in {None, "complete"}:
        return {
            "scenario": _scenario_name(episode, dataset_root),
            "episode": str(episode),
            "status": "skipped_incomplete",
            "observations": 0,
            "labeled_observations": 0,
            "reason": status or "missing_status",
        }
    perception_path = episode / "perception.jsonl"
    if not perception_path.is_file():
        return {
            "scenario": _scenario_name(episode, dataset_root),
            "episode": str(episode),
            "status": "skipped_missing_perception",
            "observations": 0,
            "labeled_observations": 0,
        }
    steps = _read_jsonl(episode / "steps.jsonl")
    by_step = {int(item.get("step_index", index)): item for index, item in enumerate(steps)}
    rows = _read_jsonl(perception_path)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    labeled = 0
    total_positive = 0
    total_negative = 0
    detector_ids = {
        str(detection.get("detector_id", "unknown"))
        for row in rows
        if row.get("observation", "after") == "after"
        for detection in (
            row.get("detections", []) if isinstance(row.get("detections", []), list) else []
        )
        if isinstance(detection, dict)
    }
    if not detector_ids:
        detector_ids = {"none"}
    for row in rows:
        # The collector's evaluator label describes obs_after.  A bridge emits
        # both sensor payloads, but joining obs_before to that label would
        # double-count every step and silently shift the timing contract.
        if row.get("observation", "after") != "after":
            continue
        step = by_step.get(int(row.get("step_index", -1)))
        labels = _labels(step or {})
        target_in_fov = labels.get("target_in_fov")
        bearing = _number(labels.get("target_bearing_deg", labels.get("target_bearing")))
        target_range = _number(labels.get("target_range_m", labels.get("target_range")))
        if not isinstance(target_in_fov, bool) or bearing is None:
            continue
        labeled += 1
        if target_in_fov:
            total_positive += 1
        else:
            total_negative += 1
        target_class = _target_class(episode, (step or {}).get("task_text"))
        detections = row.get("detections", [])
        if not isinstance(detections, list):
            detections = []
        # One aggregate per detector and per emitted detector class.  This
        # keeps COCO class recall and detector-level false-positive rates both
        # inspectable while preserving an ``all`` record for the headline.
        detector_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for detection in detections:
            if isinstance(detection, dict):
                detector_records[str(detection.get("detector_id", "unknown"))].append(detection)
        detector_records = {
            detector_id: detector_records.get(detector_id, []) for detector_id in detector_ids
        }
        for detector_id, detector_detections in detector_records.items():
            all_key = (target_class, detector_id, "__all__")
            keys = {all_key}
            for detection in detector_detections:
                keys.add(
                    (
                        target_class,
                        detector_id,
                        str(detection.get("class", detection.get("label", "unknown"))),
                    )
                )
            for key in keys:
                target_class_key, detector_key, emitted_class = key
                metric = groups.setdefault(
                    key,
                    {
                        "scenario": _scenario_name(episode, dataset_root),
                        "target_class": target_class_key,
                        "detector_id": detector_key,
                        "detected_class": emitted_class,
                        "positive_frames": 0,
                        "matched_positive_frames": 0,
                        "negative_frames": 0,
                        "false_positive_frames": 0,
                        "range_error_count": 0,
                        "range_error_mae_m": None,
                        "range_error_rmse_m": None,
                        "range_error_bias_m": None,
                    },
                )
                candidate = (
                    detector_detections
                    if emitted_class == "__all__"
                    else [
                        detection
                        for detection in detector_detections
                        if str(detection.get("class", detection.get("label", "unknown")))
                        == emitted_class
                    ]
                )
                if target_in_fov:
                    metric["positive_frames"] += 1
                else:
                    metric["negative_frames"] += 1
                matched = False
                range_errors: list[float] = []
                for detection in candidate:
                    detected_bearing = _number(detection.get("bearing_deg"))
                    if detected_bearing is None:
                        continue
                    if (
                        target_in_fov
                        and abs(_angle_error(detected_bearing, bearing)) <= tolerance_deg
                    ):
                        matched = True
                        detected_range = _number(detection.get("range_m"))
                        if detected_range is not None and target_range is not None:
                            range_errors.append(detected_range - target_range)
                if target_in_fov and matched:
                    metric["matched_positive_frames"] += 1
                if not target_in_fov and candidate:
                    metric["false_positive_frames"] += 1
                if range_errors:
                    existing = metric.setdefault("_range_errors", [])
                    existing.extend(range_errors)
    for metric in groups.values():
        # Every detector/class metric shares the complete labeled denominator;
        # a class absent on one frame is a miss, not an omitted denominator.
        metric["positive_frames"] = total_positive
        metric["negative_frames"] = total_negative
        positive = total_positive
        negative = total_negative
        metric["recall"] = metric["matched_positive_frames"] / positive if positive else None
        metric["false_positive_rate"] = (
            metric["false_positive_frames"] / negative if negative else None
        )
        errors = metric.pop("_range_errors", [])
        metric["range_error_count"] = len(errors)
        if errors:
            metric["range_error_mae_m"] = sum(abs(item) for item in errors) / len(errors)
            metric["range_error_rmse_m"] = math.sqrt(
                sum(item * item for item in errors) / len(errors)
            )
            metric["range_error_bias_m"] = sum(errors) / len(errors)
    return {
        "scenario": _scenario_name(episode, dataset_root),
        "episode": str(episode),
        "status": "ok",
        "observations": len(rows),
        "labeled_observations": labeled,
        "metrics": sorted(
            groups.values(), key=lambda item: (item["detector_id"], item["detected_class"])
        ),
    }


def evaluate_dataset(
    root: Path, *, tolerance_deg: float = 5.0, include_incomplete: bool = False
) -> dict[str, Any]:
    episodes = _episode_dirs(root)
    reports = [
        evaluate_episode(
            episode,
            dataset_root=root,
            tolerance_deg=tolerance_deg,
            include_incomplete=include_incomplete,
        )
        for episode in episodes
    ]
    metrics = [metric for report in reports for metric in report.get("metrics", [])]
    return {
        "schema": "multidetect.sim-teacher-detectability-v1",
        "dataset": str(root),
        "episodes_found": len(episodes),
        "episodes_ok": sum(report.get("status") == "ok" for report in reports),
        "episodes_skipped": sum(report.get("status") != "ok" for report in reports),
        "observations": sum(int(report.get("observations", 0)) for report in reports),
        "labeled_observations": sum(
            int(report.get("labeled_observations", 0)) for report in reports
        ),
        "episodes": reports,
        "metrics": metrics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, nargs="+")
    parser.add_argument("--tolerance-deg", type=float, default=5.0)
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.tolerance_deg) or args.tolerance_deg < 0:
        raise ValueError("--tolerance-deg must be finite and non-negative")
    reports = [
        evaluate_dataset(
            path, tolerance_deg=args.tolerance_deg, include_incomplete=args.include_incomplete
        )
        for path in args.dataset
    ]
    output: Any = (
        reports[0]
        if len(reports) == 1
        else {"schema": "multidetect.sim-teacher-detectability-v1", "datasets": reports}
    )
    encoded = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
