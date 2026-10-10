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
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_artifact(manifest: dict[str, Any], path: Path) -> tuple[Path, tuple[str, ...]]:
    export = manifest.get("export", {})
    artifact = export.get("artifact_path")
    if not isinstance(artifact, str) or not artifact:
        raise ValueError("model manifest has no export.artifact_path")
    model_path = Path(artifact)
    if not model_path.is_absolute():
        model_path = (Path.cwd() / model_path).resolve()
        if not model_path.is_file():
            model_path = (path.parent / artifact).resolve()
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    expected = export.get("artifact_sha256")
    if expected and _sha256(model_path) != expected:
        raise ValueError(f"ONNX sha256 mismatch: {model_path}")
    classes = manifest.get("classes", [])
    names = tuple(item["source_name"] for item in classes)
    if not names:
        raise ValueError("model manifest has no classes")
    return model_path, names


def _bbox_iou(left: list[float], right: list[float]) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_left = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    area_right = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = area_left + area_right - intersection
    return intersection / union if union > 0 else 0.0


def _source_path(root: Path, value: str) -> Path:
    path = Path(value.replace("\\", "/"))
    return path if path.is_absolute() else root / path


def evaluate_onnx_test_worlds(
    dataset_manifest: Path,
    model_manifest: Path,
    *,
    confidence: float = 0.25,
    iou_threshold: float = 0.5,
) -> dict[str, Any]:
    """Run the production raw/Nx6 wrapper on held-out world frames only."""
    import cv2
    import numpy as np

    from multidetect.autonomy.policy_v2.perception_bridge import (
        bearing_from_pixel,
        robust_depth_median,
    )
    from multidetect.vision import (
        OnnxNx6Config,
        OnnxNx6Detector,
        OnnxRawYoloConfig,
        OnnxRawYoloDetector,
    )

    data = json.loads(dataset_manifest.read_text(encoding="utf-8"))
    model = json.loads(model_manifest.read_text(encoding="utf-8"))
    onnx_path, classes = _manifest_artifact(model, model_manifest)
    native = model.get("output", {}).get("native_export", {})
    if native.get("format") == "ultralytics_raw_xywh_class_scores":
        detector = OnnxRawYoloDetector(
            OnnxRawYoloConfig(
                model_path=onnx_path,
                class_names=classes,
                confidence_threshold=confidence,
                model_version=str(model.get("model_version", model_manifest.stem)),
            )
        )
    elif model.get("output", {}).get("adapter_contract", {}).get("format") == "N_x_6":
        detector = OnnxNx6Detector(
            OnnxNx6Config(
                model_path=onnx_path,
                class_names=classes,
                confidence_threshold=confidence,
                model_version=str(model.get("model_version", model_manifest.stem)),
            )
        )
    else:
        raise ValueError("model manifest output is not compatible with a production wrapper")
    frames = [item for item in data.get("frames", []) if item.get("split") == "test"]
    if not frames:
        return {
            "schema": "multidetect.sim-detector-test-v1",
            "dataset_manifest": str(dataset_manifest.resolve()),
            "model_manifest": str(model_manifest.resolve()),
            "test_frames": 0,
            "test_worlds": [],
            "status": "no_test_frames",
            "metrics": [],
        }
    by_class: dict[str, dict[str, Any]] = {
        label: {"class": label, "tp": 0, "fp": 0, "fn": 0, "bearing_errors_deg": [], "range_errors_m": []}
        for label in classes
    }
    root = dataset_manifest.parent
    worlds: set[str] = set()
    for frame in frames:
        worlds.add(str(frame.get("world_id", "unknown")))
        image_path = _source_path(root, frame["image"])
        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None:
            raise FileNotFoundError(image_path)
        height, width = image.shape[:2]
        depth = None
        depth_value = frame.get("depth")
        if isinstance(depth_value, str) and depth_value:
            depth_path = _source_path(root, depth_value)
            if depth_path.is_file():
                depth = np.load(depth_path)
        detections = detector.detect(image)
        truths = [
            item for item in frame.get("objects", [])
            if item.get("class") in by_class and isinstance(item.get("bbox_xyxy"), list)
        ]
        for label in classes:
            metric = by_class[label]
            label_truths = [item for item in truths if item["class"] == label]
            label_predictions = [item for item in detections if item.label == label]
            pairs = []
            for prediction_index, prediction in enumerate(label_predictions):
                bbox = prediction.bbox
                px = [bbox.x1 * width, bbox.y1 * height, bbox.x2 * width, bbox.y2 * height]
                for truth_index, truth in enumerate(label_truths):
                    overlap = _bbox_iou(px, truth["bbox_xyxy"])
                    if overlap >= iou_threshold:
                        pairs.append((overlap, prediction_index, truth_index, px))
            used_predictions: set[int] = set()
            used_truths: set[int] = set()
            for _, prediction_index, truth_index, px in sorted(pairs, reverse=True):
                if prediction_index in used_predictions or truth_index in used_truths:
                    continue
                used_predictions.add(prediction_index)
                used_truths.add(truth_index)
                metric["tp"] += 1
                truth = label_truths[truth_index]
                camera = frame.get("camera_info")
                truth_box = truth["bbox_xyxy"]
                predicted_bearing = bearing_from_pixel(
                    (px[0] + px[2]) / 2,
                    camera_info=camera,
                    image_width=width,
                    image_height=height,
                )[0]
                true_bearing = bearing_from_pixel(
                    (truth_box[0] + truth_box[2]) / 2,
                    camera_info=camera,
                    image_width=width,
                    image_height=height,
                )[0]
                metric["bearing_errors_deg"].append(
                    abs(_angle_error(predicted_bearing, true_bearing))
                )
                target_ned = truth.get("target_ned")
                pose = frame.get("pose", {})
                position = pose.get("local_ned_m") if isinstance(pose, dict) else None
                if depth is not None and isinstance(target_ned, (list, tuple)) and isinstance(position, (list, tuple)) and len(target_ned) >= 3 and len(position) >= 3:
                    range_m = robust_depth_median(
                        depth,
                        px,
                        rgb_size=(width, height),
                        depth_camera_info=frame.get("depth_camera_info"),
                    )
                    if range_m is not None:
                        true_range = math.dist(
                            [float(position[0]), float(position[1]), -float(position[2])],
                            [float(target_ned[0]), float(target_ned[1]), float(target_ned[2])],
                        )
                        metric["range_errors_m"].append(range_m - true_range)
            metric["fp"] += len(label_predictions) - len(used_predictions)
            metric["fn"] += len(label_truths) - len(used_truths)
    metrics = []
    for metric in by_class.values():
        tp, fp, fn = metric["tp"], metric["fp"], metric["fn"]
        bearings = metric.pop("bearing_errors_deg")
        ranges = metric.pop("range_errors_m")
        metric["precision"] = tp / (tp + fp) if tp + fp else None
        metric["recall"] = tp / (tp + fn) if tp + fn else None
        metric["bearing_error_count"] = len(bearings)
        metric["bearing_mae_deg"] = sum(bearings) / len(bearings) if bearings else None
        metric["range_error_count"] = len(ranges)
        metric["range_mae_m"] = sum(map(abs, ranges)) / len(ranges) if ranges else None
        metrics.append(metric)
    return {
        "schema": "multidetect.sim-detector-test-v1",
        "dataset_manifest": str(dataset_manifest.resolve()),
        "model_manifest": str(model_manifest.resolve()),
        "model_sha256": _sha256(onnx_path),
        "test_frames": len(frames),
        "test_worlds": sorted(worlds),
        "iou_threshold": iou_threshold,
        "confidence_threshold": confidence,
        "status": "ok",
        "metrics": metrics,
    }


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
    parser.add_argument(
        "--dataset-manifest",
        type=Path,
        help="builder manifest; with --model-manifest, evaluate only split=test frames",
    )
    parser.add_argument(
        "--model-manifest",
        type=Path,
        help="quarantined ONNX model manifest for held-out frame evaluation",
    )
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--tolerance-deg", type=float, default=5.0)
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not math.isfinite(args.tolerance_deg) or args.tolerance_deg < 0:
        raise ValueError("--tolerance-deg must be finite and non-negative")
    if args.dataset_manifest or args.model_manifest:
        if args.dataset_manifest is None or args.model_manifest is None:
            raise ValueError("--dataset-manifest and --model-manifest must be supplied together")
        if not 0.0 <= args.confidence <= 1.0:
            raise ValueError("--confidence must be in [0, 1]")
        if not 0.0 < args.iou_threshold <= 1.0:
            raise ValueError("--iou-threshold must be in (0, 1]")
        output = evaluate_onnx_test_worlds(
            args.dataset_manifest,
            args.model_manifest,
            confidence=args.confidence,
            iou_threshold=args.iou_threshold,
        )
        encoded = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8", newline="\n")
        print(encoded, end="")
        return 0
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
