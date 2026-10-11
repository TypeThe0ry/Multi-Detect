#!/usr/bin/env python3
# ruff: noqa: E501
"""Offline YOLOE benchmark on aligned SIM teacher frames.

Only completed, generated-world episodes are read. No truth enters inference.
Set YOLO_AUTOINSTALL=false before importing Ultralytics: missing text encoders
must be reported rather than fetched from the network.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

os.environ["YOLO_AUTOINSTALL"] = "false"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import cv2  # noqa: E402
import torch  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ultralytics import YOLOE  # noqa: E402

from multidetect.simulator.camera_projection import project_target_bbox  # noqa: E402

CAMPAIGNS = ("campaign-20261011-r2", "campaign-20261011-r3", "campaign-20261011-r4")
MODEL_DIR = ROOT / "artifacts/cache/yoloe"
CLASSES = ("vehicle", "rover", "cargo_cart")
SIZES = {"vehicle": (3.0, 1.8, 1.5), "rover": (3.8, 1.6, 1.7), "cargo_cart": (2.5, 1.5, 1.5)}
ALIASES = {"car": "vehicle", "truck": "cargo_cart", "red_vehicle": "vehicle", "blue_rover": "rover"}
PF_ALIASES = {
    "vehicle": "vehicle", "motor vehicle": "vehicle", "land vehicle": "vehicle", "car": "vehicle",
    "rover": "rover", "off-road vehicle": "rover", "off road vehicle": "rover", "buggy": "rover",
    "cargo cart": "cargo_cart", "cart": "cargo_cart", "golf cart": "cargo_cart", "truck": "cargo_cart",
}
COLORS = ("red", "blue", "green", "yellow", "orange", "white", "black")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    x = (len(vals) - 1) * pct / 100.0
    lo, hi = math.floor(x), math.ceil(x)
    return vals[lo] + (vals[hi] - vals[lo]) * (x - lo)


def task_prompts(task_text: str) -> list[str]:
    """Translate the task's requested attribute to YOLOE text, plus generic aliases."""
    lower = task_text.casefold()
    color = next((c for c in COLORS if re.search(rf"\b{c}\b|{c}_", lower)), None)
    if color is None:
        color = next((v for k, v in (("红", "red"), ("蓝", "blue"), ("绿", "green"), ("黄", "yellow"), ("橙", "orange")) if k in task_text), None)
    cls = next((v for k, v in (("cargo_cart", "cargo cart"), ("rover", "rover"), ("vehicle", "vehicle"), ("cart", "cargo cart")) if k in lower), None)
    if cls is None:
        cls = "cargo cart" if "货车" in task_text else "rover" if "探测车" in task_text else "vehicle" if "车辆" in task_text or "车" in task_text else None
    prompts = [f"{color} {cls}"] if color and cls else [cls] if cls else []
    return list(dict.fromkeys(prompts + ["vehicle", "rover", "cargo cart", "car", "truck"]))


def class_from_prompt(prompt: str) -> str | None:
    clean = prompt.casefold().strip()
    if clean in PF_ALIASES:
        return PF_ALIASES[clean]
    for color in COLORS:
        if clean.startswith(color + " "):
            return PF_ALIASES.get(clean[len(color) + 1 :])
    return None


def collect_rows(roots: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    truth_dir = ROOT / "configs/scenarios/generated"
    for campaign in roots:
        for ep in sorted(p.parent for p in campaign.rglob("steps.jsonl")):
            meta_file = ep / "episode.json"
            if not meta_file.is_file():
                continue
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta.get("status") != "complete":
                continue
            world = next((part for part in ep.parts if part.startswith("gen_w")), None)
            truth_file = truth_dir / f"{world}.truth.json"
            if world is None or not truth_file.is_file():
                continue
            targets = json.loads(truth_file.read_text(encoding="utf-8")).get("targets", [])
            for line in (ep / "steps.jsonl").read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                step = json.loads(line)
                # Use one observation per step to reduce near-duplicate frames.
                obs = step.get("obs_before")
                if not isinstance(obs, dict) or abs(float(obs.get("sim_skew_ms", 0))) > 250:
                    continue
                relpath = obs.get("rgb_path")
                if not isinstance(relpath, str):
                    continue
                image_path = ep / Path(relpath.replace("\\", "/"))
                if not image_path.is_file():
                    continue
                info = obs.get("camera_info") or {}
                size = (int(info.get("width", 640)), int(info.get("height", 480)))
                pose = {**(obs.get("pose") or {}), "attitude": obs.get("attitude") or {}}
                objects = []
                for target in targets:
                    try:
                        tp = target["pose"]
                        cls = ALIASES.get(target["class"], target["class"])
                        if cls not in CLASSES:
                            continue
                        box = project_target_bbox(
                            target_ned=(float(tp["y_north_m"]), float(tp["x_east_m"]), float(tp["z_up_m"])),
                            pose=pose, camera_info=info, image_size=size,
                            target_size_m=tuple(target.get("size_m") or SIZES[cls]),
                            target_yaw_rad=float(tp.get("yaw_rad", 0)),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    if box is not None and box[2] - box[0] >= 4 and box[3] - box[1] >= 4:
                        objects.append({"class": cls, "box": box, "target_id": target.get("target_id")})
                pos = (obs.get("pose") or {}).get("local_ned_m") or [0, 0, 0]
                altitude = -float(pos[2])
                rows.append({
                    "path": str(image_path), "campaign": campaign.name, "world": world,
                    "task_text": str(step.get("task_text") or meta.get("task_text") or ""),
                    "altitude_m": altitude, "altitude_bin": "high_15m_plus" if altitude >= 10 else "low_under_10m",
                    "objects": objects,
                })
    return rows


def sample_rows(rows: list[dict[str, Any]], count: int, seed: int) -> list[dict[str, Any]]:
    if count > len(rows):
        raise ValueError(f"requested {count} frames, only {len(rows)} eligible")
    rng = random.Random(seed)
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["campaign"], bool(row["objects"]))].append(row)
    for group in groups.values():
        rng.shuffle(group)
    selected = []
    # Round robin across campaign and positive/empty strata.
    while len(selected) < count:
        advanced = False
        for key in sorted(groups):
            if groups[key] and len(selected) < count:
                selected.append(groups[key].pop())
                advanced = True
        if not advanced:
            break
    return selected


def iou(a: list[float] | tuple[float, ...], b: list[float] | tuple[float, ...]) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def score(rows: list[dict[str, Any]], predictions: list[list[dict[str, Any]]], threshold: float) -> dict[str, Any]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for row, preds in zip(rows, predictions, strict=True):
        truth = row["objects"]
        scopes = ["overall", row["altitude_bin"], row["campaign"]]
        matches = []
        for pi, pred in enumerate(preds):
            for ti, obj in enumerate(truth):
                if pred["class"] == obj["class"]:
                    overlap = iou(pred["box"], obj["box"])
                    if overlap >= threshold:
                        matches.append((overlap, pi, ti))
        used_p, used_t = set(), set()
        for _, pi, ti in sorted(matches, reverse=True):
            if pi not in used_p and ti not in used_t:
                used_p.add(pi)
                used_t.add(ti)
        for scope in scopes:
            counts[scope]["frames"] += 1
            if not truth:
                counts[scope]["empty_frames"] += 1
                counts[scope]["empty_frames_with_fp"] += bool(preds)
                counts[scope]["empty_frame_fp_boxes"] += len(preds)
            for cls in CLASSES:
                key = f"{cls}."
                counts[scope][key + "tp"] += sum(truth[ti]["class"] == cls for ti in used_t)
                counts[scope][key + "fn"] += sum(obj["class"] == cls for ti, obj in enumerate(truth) if ti not in used_t)
                counts[scope][key + "fp"] += sum(pred["class"] == cls for pi, pred in enumerate(preds) if pi not in used_p)
    report = {}
    for scope, c in counts.items():
        per_class = {}
        for cls in CLASSES:
            tp, fp, fn = (c[f"{cls}.{key}"] for key in ("tp", "fp", "fn"))
            per_class[cls] = {"tp": tp, "fp": fp, "fn": fn, "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None}
        tp = sum(v["tp"] for v in per_class.values())
        fp = sum(v["fp"] for v in per_class.values())
        fn = sum(v["fn"] for v in per_class.values())
        report[scope] = {
            "frames": c["frames"], "tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "per_class": per_class,
            "target_free_frames": c["empty_frames"],
            "target_free_frames_with_fp": c["empty_frames_with_fp"],
            "target_free_fp_boxes": c["empty_frame_fp_boxes"],
            "target_free_fp_frame_rate": c["empty_frames_with_fp"] / c["empty_frames"] if c["empty_frames"] else None,
        }
    return report


def score_class_agnostic(rows: list[dict[str, Any]], predictions: list[list[dict[str, Any]]], threshold: float) -> dict[str, Any]:
    """Measure prompt-free object proposals without pretending their labels match truth."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for row, preds in zip(rows, predictions, strict=True):
        truth = row["objects"]
        pairs = sorted(
            ((iou(pred["box"], obj["box"]), pi, ti) for pi, pred in enumerate(preds) for ti, obj in enumerate(truth)),
            reverse=True,
        )
        used_p, used_t = set(), set()
        for overlap, pi, ti in pairs:
            if overlap < threshold:
                break
            if pi not in used_p and ti not in used_t:
                used_p.add(pi)
                used_t.add(ti)
        for scope in ("overall", row["altitude_bin"], row["campaign"]):
            c = counts[scope]
            c["tp"] += len(used_t)
            c["fp"] += len(preds) - len(used_p)
            c["fn"] += len(truth) - len(used_t)
            for cls in CLASSES:
                c[f"{cls}.tp"] += sum(truth[ti]["class"] == cls for ti in used_t)
                c[f"{cls}.fn"] += sum(obj["class"] == cls for ti, obj in enumerate(truth) if ti not in used_t)
            if not truth:
                c["target_free_frames"] += 1
                c["target_free_frames_with_fp"] += bool(preds)
                c["target_free_fp_boxes"] += len(preds)
    return {
        scope: {
            **dict(c),
            "precision": c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None,
            "recall": c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None,
            "target_free_fp_frame_rate": c["target_free_frames_with_fp"] / c["target_free_frames"] if c["target_free_frames"] else None,
            "per_truth_class_recall": {
                cls: {
                    "tp": c[f"{cls}.tp"], "fn": c[f"{cls}.fn"],
                    "recall": c[f"{cls}.tp"] / (c[f"{cls}.tp"] + c[f"{cls}.fn"]) if c[f"{cls}.tp"] + c[f"{cls}.fn"] else None,
                }
                for cls in CLASSES
            },
        }
        for scope, c in counts.items()
    }


def run_model(model: YOLOE, rows: list[dict[str, Any]], *, text: bool, confidence: float) -> dict[str, Any]:
    model.to("cuda")
    latencies, inference_ms, predictions, raw_predictions = [], [], [], []
    raw_detections = 0
    raw_target_free_detections = 0
    raw_target_free_frames_with_detection = 0
    raw_labels: Counter = Counter()
    last_prompts: tuple[str, ...] | None = None
    names = model.names
    for index, row in enumerate(rows):
        image = cv2.imread(row["path"], cv2.IMREAD_COLOR)
        if image is None:
            raise OSError(f"could not decode {row['path']}")
        if text:
            prompts = tuple(task_prompts(row["task_text"]))
            if prompts != last_prompts:
                # YOLOE.set_classes rejects phrases containing spaces in 8.4.92.
                # The underlying model accepts names such as "red vehicle".
                embeddings = model.get_text_pe(list(prompts))
                model.model.set_classes(list(prompts), embeddings)
                if model.predictor:
                    model.predictor.model.names = model.model.names
                names = model.names
                last_prompts = prompts
        torch.cuda.synchronize()
        start = time.perf_counter()
        result = model.predict(image, imgsz=640, conf=confidence, device=0, half=True, verbose=False, save=False)[0]
        torch.cuda.synchronize()
        latencies.append((time.perf_counter() - start) * 1000)
        inference_ms.append(float(result.speed["inference"]))
        boxes = result.boxes
        pred = []
        raw_pred = []
        if boxes is not None and len(boxes):
            raw_detections += len(boxes)
            if not row["objects"]:
                raw_target_free_detections += len(boxes)
                raw_target_free_frames_with_detection += 1
            xyxy = boxes.xyxy.cpu().tolist()
            classes = boxes.cls.int().cpu().tolist()
            scores = boxes.conf.cpu().tolist()
            for box, cls_index, conf in zip(xyxy, classes, scores, strict=True):
                label = str(names[int(cls_index)])
                raw_labels[label] += 1
                raw_pred.append({"box": box})
                cls = class_from_prompt(label)
                if cls in CLASSES:
                    pred.append({"class": cls, "box": box, "confidence": conf})
        predictions.append(pred)
        raw_predictions.append(raw_pred)
        if (index + 1) % 50 == 0:
            print(f"{('text' if text else 'prompt_free')}: {index + 1}/{len(rows)}", file=sys.stderr, flush=True)
    return {
        "status": "measured", "frames": len(rows),
        "gpu_wall_ms_p50": percentile(latencies, 50), "gpu_wall_ms_p95": percentile(latencies, 95),
        "model_inference_ms_p50": percentile(inference_ms, 50), "model_inference_ms_p95": percentile(inference_ms, 95),
        "gpu_wall_definition": "synchronized cv2-array predict call including preprocessing, CUDA inference and NMS; excludes image file decode and prompt encoding",
        "peak_cuda_allocated_mb": torch.cuda.max_memory_allocated() / 1048576,
        "raw_detections_all_labels": raw_detections,
        "raw_detection_labels_top20": raw_labels.most_common(20),
        "raw_target_free_detections_all_labels": raw_target_free_detections,
        "raw_target_free_frames_with_detection_all_labels": raw_target_free_frames_with_detection,
        "raw_target_free_frames_all_labels": sum(not row["objects"] for row in rows),
        "scoring_filter": "Only predicted labels mapped to vehicle, rover or cargo_cart count in class precision/recall; raw counts include all prompt-free vocabulary labels.",
        "scores": score(rows, predictions, 0.3),
        "class_agnostic_proposals": score_class_agnostic(rows, raw_predictions, 0.3),
    }


def attempt_export(model_path: Path, output_dir: Path, *, text_model: YOLOE | None = None, baked_prompts: bool) -> dict[str, Any]:
    if baked_prompts and text_model is None:
        return {"status": "unavailable", "reason": "No locally installed compatible MobileCLIP-BLT text encoder; no prompt embeddings can be baked."}
    output_dir.mkdir(parents=True, exist_ok=True)
    local_pt = output_dir / model_path.name
    exported: Path | None = None
    try:
        # Exporter places the ONNX beside the source checkpoint. Work on a small copy.
        shutil.copyfile(model_path, local_pt)
        export_model = YOLOE(str(local_pt))
        if baked_prompts and text_model is not None:
            export_model.model.set_classes(list(text_model.names.values()), text_model.model.pe.detach().cpu())
        exported = Path(export_model.export(format="onnx", imgsz=640, simplify=False, dynamic=False, half=False, device="cpu"))
        digest = hashlib.sha256()
        with exported.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
        return {
            "status": "success", "size_mb": exported.stat().st_size / 1048576,
            "sha256": digest.hexdigest(), "artifact_retained": False,
            "prompts": list(text_model.names.values()) if text_model is not None and baked_prompts else None,
        }
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        if exported is not None:
            exported.unlink(missing_ok=True)
        local_pt.unlink(missing_ok=True)


def local_text_encoder_status() -> str | None:
    """Return a blocker without invoking Ultralytics' network fallback."""
    if importlib.util.find_spec("clip") is None:
        return "CLIP tokenizer module `clip` is not installed locally."
    from ultralytics.utils import SETTINGS

    weight = "mobileclip_blt.ts"
    if not Path(weight).is_file() and not (SETTINGS["weights_dir"] / weight).is_file():
        return "YOLOE-required MobileCLIP-BLT TorchScript encoder `mobileclip_blt.ts` is not local."
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, action="append", help="Campaign roots (default r2/r3/r4)")
    parser.add_argument("--frames", type=int, default=360, help="Deterministically sampled real frames (default 360)")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--output", type=Path, help="Optional compact JSON report path")
    parser.add_argument("--export-onnx", type=Path, help="Optional export directory for baked-prompt ONNX")
    parser.add_argument("--sample-only", action="store_true", help="Inspect eligible sample without inference")
    args = parser.parse_args()
    if args.frames < 1:
        parser.error("--frames must be positive")
    roots = args.campaign or [ROOT / "artifacts/datasets/sim-teacher" / n for n in CAMPAIGNS]
    rows = sample_rows(collect_rows(roots), args.frames, args.seed)
    sample = {
        "frames": len(rows), "by_campaign": dict(Counter(r["campaign"] for r in rows)),
        "by_altitude": dict(Counter(r["altitude_bin"] for r in rows)),
        "target_free_frames": sum(not r["objects"] for r in rows),
        "truth_boxes": dict(Counter(obj["class"] for r in rows for obj in r["objects"])),
        "sample_paths": [str(Path(r["path"]).relative_to(ROOT)) for r in rows],
    }
    report: dict[str, Any] = {
        "contract": "multidetect.open-vocab-detector-benchmark-v1", "seed": args.seed,
        "iou_threshold": 0.3, "confidence_threshold": args.conf,
        "image_size": 640, "precision": "PyTorch CUDA fp16", "sample": sample,
        "task_prompt_examples": {r["task_text"]: task_prompts(r["task_text"]) for r in rows},
    }
    if not args.sample_only:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA required for the requested fp16 GPU benchmark")
        torch.cuda.reset_peak_memory_stats()
        pf = YOLOE(str(MODEL_DIR / "yoloe-11s-seg-pf.pt"))
        report["prompt_free"] = run_model(pf, rows, text=False, confidence=args.conf)
        del pf
        torch.cuda.empty_cache()
        # YOLOE 8.4.92 requires its exact MobileCLIP-BLT encoder for set_classes.
        # A different CLIP checkpoint cannot be substituted for its 512-D embedding space.
        blocker = local_text_encoder_status()
        if blocker is not None:
            report["text_prompts"] = {"status": "unavailable", "reason": blocker + " Ultralytics would otherwise auto-install or download. Text-prompt accuracy and latency were not measured."}
            text_model = None
        else:
            try:
                text_model = YOLOE(str(MODEL_DIR / "yoloe-11s-seg.pt"))
                report["text_prompts"] = run_model(text_model, rows, text=True, confidence=args.conf)
            except Exception as exc:
                report["text_prompts"] = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
                text_model = None
        if args.export_onnx:
            report["text_prompt_onnx_export"] = attempt_export(MODEL_DIR / "yoloe-11s-seg.pt", args.export_onnx, text_model=text_model, baked_prompts=True)
            report["prompt_free_onnx_export"] = attempt_export(MODEL_DIR / "yoloe-11s-seg-pf.pt", args.export_onnx, baked_prompts=False)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8", newline="\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
