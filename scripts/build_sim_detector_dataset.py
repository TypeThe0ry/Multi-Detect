#!/usr/bin/env python3
"""Build a fail-closed, world-disjoint YOLO dataset from SIM campaigns.

Labels are generated from privileged scenario truth and are used only while
building the training artifact. Every indexed world uses the split recorded in
the supplied index. Only the explicitly train-only ``gen_cfs_w`` and
``gen_cfs2_w`` confuser prefixes may be absent from the index; all other
unknown worlds fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

try:
    from multidetect.sim_teacher.policies import load_sdf_obstacles
    from multidetect.simulator.camera_projection import project_target_bbox, visible_fraction
except ModuleNotFoundError:  # pragma: no cover - direct script invocation fallback
    from multidetect.sim_teacher.policies import load_sdf_obstacles
    from multidetect.simulator.camera_projection import visible_fraction
    from scripts.autolabel_sim_teacher_frames import project_target_bbox

from multidetect.sim_teacher.segmentation import (  # noqa: E402
    boxes_from_segmentation,
    load_labels_map,
)

LEGACY_CLASS_NAMES = (
    "vehicle",
    "rover",
    "cargo_cart",
    "orange_inspection_marker",
    "blue_survey_pylon",
)
CLASS_ALIASES = {
    "car": "vehicle",
    "truck": "cargo_cart",
    "red_vehicle": "vehicle",
    "blue_rover": "rover",
}
DEFAULT_SIZES = {
    "vehicle": (3.0, 1.8, 1.5),
    "rover": (3.8, 1.6, 1.7),
    "cargo_cart": (2.5, 1.5, 1.5),
    "orange_inspection_marker": (1.0, 1.0, 2.0),
    "blue_survey_pylon": (1.0, 1.0, 2.0),
}


def _load_fuel_catalog(
    path: Path = Path("configs/sim-assets/fuel-catalog.json"),
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    """Return detector classes and asset records, tolerating catalog revisions."""
    try:
        document = _load(path)
    except (OSError, ValueError, TypeError):
        return LEGACY_CLASS_NAMES, {}
    rows = document.get("assets", document) if isinstance(document, dict) else document
    if not isinstance(rows, list):
        return LEGACY_CLASS_NAMES, {}
    records: dict[str, dict[str, Any]] = {}
    classes: list[str] = list(LEGACY_CLASS_NAMES)
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or row.get("asset_slug") or "").strip()
        words = row.get("category_words", row.get("semantic_category_words", []))
        if isinstance(words, str):
            words = [words]
        detector_class = str(
            row.get("detector_class") or row.get("category") or (words[0] if words else slug)
        ).strip()
        detector_class = detector_class.casefold().replace(" ", "_")
        if not detector_class:
            continue
        if detector_class not in classes:
            classes.append(detector_class)
        if slug:
            records[slug] = {**row, "detector_class": detector_class}
    return (tuple(classes) or LEGACY_CLASS_NAMES), records


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x if math.isfinite(x) else None


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _episode_dirs(root: Path) -> list[Path]:
    return (
        [root]
        if (root / "steps.jsonl").is_file()
        else sorted(p.parent for p in root.rglob("steps.jsonl"))
    )


def _world_id(ep: Path) -> str:
    for part in ep.parts:
        if part.startswith(("gen_", "urban_", "rw_")):
            return part
    # campaign layouts occasionally put the world in episode.json only
    try:
        value = _load(ep / "episode.json")
        text = str(value.get("world", "")) or Path(str(value.get("world_sdf", ""))).stem
        if text:
            return text.split(".")[0]
    except Exception:
        pass
    return ep.parent.name


def _find_truth(world: str, roots: list[Path]) -> Path | None:
    names = [f"{world}.truth.json"]
    if world.startswith("urban_"):
        names.append("urban_inspection_multirotor_001.truth.json")
    for root in [*roots, ROOT]:
        for base in (root, root / "configs", root.parent, Path.cwd()):
            for name in names:
                for candidate in (
                    base / name,
                    base / "scenarios" / name,
                    base / "scenarios" / "generated" / name,
                    base / "scenarios" / "generated-v2" / name,
                    base / "scenarios" / "generated-v2-appearance" / name,
                    base / "scenarios" / "generated-confusers" / name,
                    base / "scenarios" / "generated-confusers-2" / name,
                    base / "scenarios" / "generated-r15" / name,
                    base / "scenarios" / "realworld" / name,
                    base / "scenarios" / "realworld-clusters" / name,
                ):
                    if candidate.is_file():
                        return candidate
    return None


def _find_sdf(world: str, roots: list[Path]) -> Path | None:
    for root in roots:
        for candidate in (
            root / f"{world}.sdf",
            root / "simulation" / "gazebo" / "worlds" / f"{world}.sdf",
            Path.cwd() / "simulation" / "gazebo" / "worlds" / f"{world}.sdf",
        ):
            if candidate.is_file():
                return candidate
    return None


def _sdf_sizes(path: Path | None) -> dict[str, tuple[float, float, float]]:
    out: dict[str, tuple[float, float, float]] = {}
    if path is None:
        return out
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return out
    for model in root.iter("model"):
        name = model.attrib.get("name")
        if not name:
            continue
        size = model.find(".//collision/geometry/box/size")
        if size is None or not size.text:
            size = model.find(".//visual/geometry/box/size")
        if size is None or not size.text:
            continue
        try:
            values = tuple(float(v) for v in size.text.split()[:3])
            if len(values) == 3 and all(v > 0 and math.isfinite(v) for v in values):
                out[name] = values  # SDF ENU x/east, y/north, z/up
        except ValueError:
            continue
    # Included urban models are not expanded in the SDF; use documented default.
    return out


def _urban_target(world: str, sdf_path: Path | None) -> list[dict[str, Any]]:
    if not world.startswith("urban_") or sdf_path is None:
        return []
    root = ET.parse(sdf_path).getroot()
    for include in root.iter("include"):
        if include.findtext("name") == "multidetect_search_target_urban_001":
            values = [float(v) for v in (include.findtext("pose") or "").split()]
            if len(values) >= 6:
                east, north, ground, _, _, yaw = values[:6]
                return [
                    {
                        "target_id": "multidetect_search_target_urban_001",
                        "class": "orange_inspection_marker",
                        "pose": {
                            "x_east_m": east,
                            "y_north_m": north,
                            "z_up_m": ground + 1.4,
                            "yaw_rad": yaw,
                        },
                        "size_m": [2.8, 1.8, 2.8],
                    }
                ]
    return []


def _target_metric(target: dict[str, Any]) -> tuple[float, float, float] | None:
    pose = target.get("pose")
    if isinstance(pose, dict):
        east, north, up = (_num(pose.get(k)) for k in ("x_east_m", "y_north_m", "z_up_m"))
        if east is not None and north is not None and up is not None:
            return north, east, up
    return None


def _label_line(
    class_id: int, box: tuple[float, float, float, float], size: tuple[int, int]
) -> str:
    x1, y1, x2, y2 = box
    w, h = size
    values = ((x1 + x2) / (2 * w), (y1 + y2) / (2 * h), (x2 - x1) / w, (y2 - y1) / h)
    return f"{class_id} " + " ".join(f"{value:.8f}" for value in values)


def _split_map(index_paths: Path | list[Path] | tuple[Path, ...]) -> dict[str, str]:
    """Merge explicit world indexes; conflicting declarations are invalid."""
    paths = [index_paths] if isinstance(index_paths, Path) else list(index_paths)
    merged: dict[str, str] = {}
    for path in paths:
        doc = _load(path)
        for world in doc.get("worlds", []):
            if not isinstance(world, dict) or not world.get("world_id"):
                continue
            name, split = str(world["world_id"]), str(world.get("split"))
            if name in merged and merged[name] != split:
                raise ValueError(f"world {name!r} has conflicting splits in indexes")
            merged[name] = split
    return merged


def _perceptual_key(image: Any) -> str:
    small = cv2.resize(image, (32, 24), interpolation=cv2.INTER_AREA)
    return hashlib.sha1(small.tobytes()).hexdigest()


def _step_sim_skew_ms(step: dict[str, Any]) -> list[float]:
    """Return finite simulation skews carried by either observation."""
    values: list[float] = []
    for side in ("before", "after"):
        obs = step.get("obs_" + side)
        if not isinstance(obs, dict):
            continue
        value = _num(obs.get("sim_skew_ms"))
        if value is not None:
            values.append(value)
    return values


def build_dataset(
    campaign_roots: list[Path],
    output: Path,
    *,
    index_path: Path | list[Path] = Path("configs/scenarios/generated/index.json"),
    min_box_px: float = 4.0,
    negative_ratio: float = 0.15,
    seed: int = 20261011,
    preview: int = 0,
    max_sim_skew_ms: float = 250.0,
    skip_over_skew: bool = True,
    min_visible_fraction: float = 0.3,
    label_source: str = "projection",
    min_seg_area_px: int = 4,
    truth_roots: list[Path] | None = None,
    max_frames: int | None = None,
    holdout_assets: set[str] | None = None,
    include_incomplete: bool = False,
) -> dict[str, Any]:
    if not math.isfinite(float(max_sim_skew_ms)) or float(max_sim_skew_ms) < 0.0:
        raise ValueError("max_sim_skew_ms must be finite and non-negative")
    if not 0.0 <= float(min_visible_fraction) <= 1.0:
        raise ValueError("min_visible_fraction must be in [0, 1]")
    if label_source not in {"projection", "segmentation"}:
        raise ValueError("label_source must be projection or segmentation")
    rng = random.Random(seed)
    class_names, catalog = _load_fuel_catalog()
    splits = _split_map(index_path)
    output.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val", "test"):
        for kind in ("images", "labels"):
            directory = output / kind / split
            if directory.exists():
                shutil.rmtree(directory)
            directory.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    skipped_sim_skew_steps = 0
    dropped_occluded = 0
    seen_sha: set[str] = set()
    seen_perceptual: set[str] = set()
    dedupe = Counter()
    scanned_frames = 0
    heldout_asset_images = Counter()
    remaining_asset_instances = Counter()
    for campaign in campaign_roots:
        for episode in _episode_dirs(campaign):
            metadata_path = episode / "episode.json"
            if not metadata_path.is_file():
                continue
            if _load(metadata_path).get("status") != "complete" and not include_incomplete:
                # An episode that was never finalised (collector killed mid-RTL) still has usable frames.
                continue
            world = _world_id(episode)
            if world in splits:
                split = splits[world]
            else:
                raise ValueError(f"world {world!r} is missing from required indexes {index_path}")
            if split not in ("train", "val", "test"):
                raise ValueError(f"invalid split {split!r} for {world}")
            index_paths = index_path if isinstance(index_path, list) else [index_path]
            roots = [*(path.parent for path in index_paths), campaign, Path.cwd()]
            roots.extend(truth_roots or [])
            truth_path = _find_truth(world, roots)
            truth = _load(truth_path) if truth_path else {}
            sdf_path = _find_sdf(world, [campaign, Path.cwd()])
            obstacles = load_sdf_obstacles(sdf_path) if sdf_path is not None else ()
            targets = (
                _urban_target(world, sdf_path)
                if world.startswith("urban_")
                else truth.get("targets", [])
            )
            if not targets:
                continue
            sdf = _sdf_sizes(sdf_path)
            try:
                steps = [
                    json.loads(line)
                    for line in (episode / "steps.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, json.JSONDecodeError):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                skew_values = _step_sim_skew_ms(step)
                if skip_over_skew and any(
                    abs(value) > float(max_sim_skew_ms) for value in skew_values
                ):
                    skipped_sim_skew_steps += 1
                    continue
                for side in ("before", "after"):
                    obs = step.get("obs_" + side)
                    if not isinstance(obs, dict) or not isinstance(obs.get("rgb_path"), str):
                        continue
                    rgb = Path(obs["rgb_path"].replace("\\", os.sep).replace("/", os.sep))
                    rgb = rgb if rgb.is_absolute() else episode / rgb
                    image = cv2.imread(str(rgb), cv2.IMREAD_COLOR)
                    if image is None:
                        continue
                    if max_frames is not None and scanned_frames >= max_frames:
                        break
                    scanned_frames += 1
                    digest = _sha(rgb)
                    pkey = _perceptual_key(image)
                    if digest in seen_sha:
                        dedupe["exact_sha"] += 1
                        continue
                    if pkey in seen_perceptual:
                        dedupe["perceptual"] += 1
                        continue
                    seen_sha.add(digest)
                    seen_perceptual.add(pkey)
                    h, w = image.shape[:2]
                    segmentation_boxes = {}
                    if label_source == "segmentation":
                        seg_name = obs.get("segmentation_path")
                        if not isinstance(seg_name, str):
                            raise ValueError(
                                f"{episode}: segmentation labels requested "
                                "but observation has no segmentation_path"
                            )
                        seg_path = Path(seg_name.replace("\\", "/"))
                        seg_path = seg_path if seg_path.is_absolute() else episode / seg_path
                        seg_image = load_labels_map(seg_path)
                        if seg_image.shape[:2] != (h, w):
                            raise ValueError("segmentation geometry differs from RGB")
                        segmentation_boxes = boxes_from_segmentation(
                            seg_image, truth, min_area_px=min_seg_area_px, min_box_px=min_box_px
                        )
                    # Holdout is defined by the segmentation-truth asset slug.
                    # Do not infer it from the detector class: walking_person
                    # and standing_person both map to detector class person.
                    asset_slugs = {
                        str(label.get("asset_slug") or "")
                        for label in truth.get("segmentation_labels", {}).values()
                        if isinstance(label, dict) and label.get("asset_slug")
                    }
                    blocked_assets = asset_slugs & (holdout_assets or set())
                    if split in ("train", "val") and blocked_assets:
                        for asset in blocked_assets:
                            heldout_asset_images[(split, asset)] += 1
                        continue
                    labels: list[str] = []
                    classes: list[str] = []
                    objects: list[dict[str, Any]] = []
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        metric = _target_metric(target)
                        if metric is None:
                            continue
                        asset = catalog.get(
                            str(target.get("asset_slug") or target.get("asset") or ""), {}
                        )
                        name = str(
                            target.get("detector_class")
                            or asset.get("detector_class")
                            or target.get("class")
                            or target.get("label")
                            or "vehicle"
                        )
                        name = CLASS_ALIASES.get(name, name)
                        name = name.casefold().replace(" ", "_")
                        if name not in class_names:
                            name = "vehicle"
                        pose = {
                            **obs.get("pose", {}),
                            "attitude": obs.get("attitude", {}),
                            **({"camera_pose": obs["camera_pose"]} if obs.get("camera_pose") else {}),
                        }
                        raw_size = target.get("size_m") or target.get("measured_size_m")
                        if raw_size is None:
                            raw_size = sdf.get(str(target.get("target_id")))
                        if raw_size is None:
                            raw_size = asset.get("size_m")
                        if not isinstance(raw_size, (list, tuple)) or len(raw_size) != 3:
                            raw_size = DEFAULT_SIZES.get(name, (3.0, 1.8, 1.5))
                        size = tuple(float(value) for value in raw_size)
                        yaw = _num((target.get("pose") or {}).get("yaw_rad")) or 0.0
                        bottom_offset = _num(
                            target.get(
                                "bottom_offset_m",
                                target.get("mesh_bottom_offset_m", asset.get("bottom_offset_m")),
                            )
                        )
                        segmentation = segmentation_boxes.get(str(target.get("target_id")))
                        if label_source == "segmentation":
                            box = segmentation["box"] if segmentation else None
                            fraction = None
                        else:
                            box = project_target_bbox(
                                target_ned=metric,
                                pose=pose,
                                camera_info=obs.get("camera_info", {}),
                                image_size=(w, h),
                                target_size_m=size,
                                target_yaw_rad=yaw,
                                target_bottom_offset_m=bottom_offset,
                            )
                            fraction = visible_fraction(
                                pose=pose,
                                target_ned=metric,
                                target_size_m=size,
                                target_yaw_rad=yaw,
                                obstacles=obstacles,
                                target_bottom_offset_m=bottom_offset,
                            )
                            if min_visible_fraction > 0.0 and fraction < min_visible_fraction:
                                dropped_occluded += 1
                                continue
                        if (
                            box is None
                            or box[2] - box[0] < min_box_px
                            or box[3] - box[1] < min_box_px
                        ):
                            continue
                        labels.append(_label_line(class_names.index(name), box, (w, h)))
                        classes.append(name)
                        objects.append(
                            {
                                "class": name,
                                "target_id": target.get("target_id"),
                                "bbox_xyxy": [float(value) for value in box],
                                "target_ned": [float(value) for value in metric],
                                "size_m": [float(value) for value in size],
                                "visible_fraction": fraction,
                                **(segmentation or {}),
                            }
                        )
                        asset_slug = str(target.get("asset_slug") or target.get("asset") or "")
                        if asset_slug in (holdout_assets or set()):
                            remaining_asset_instances[(split, asset_slug)] += 1
                    records.append(
                        {
                            "source": str(rgb),
                            "episode_id": episode.name,
                            "world_id": world,
                            "split": split,
                            "sha256": digest,
                            "labels": labels,
                            "classes": classes,
                            "objects": objects,
                            "pose": obs.get("pose", {}),
                            "camera_info": obs.get("camera_info", {}),
                            "depth_camera_info": obs.get("depth_camera_info", {}),
                            "depth": str(
                                (
                                    episode
                                    / Path(
                                        obs["depth_path"].replace("\\", os.sep).replace("/", os.sep)
                                    )
                                ).resolve()
                            )
                            if isinstance(obs.get("depth_path"), str)
                            else None,
                            "image": image,
                        }
                    )
                if max_frames is not None and scanned_frames >= max_frames:
                    break
            if max_frames is not None and scanned_frames >= max_frames:
                break
        if max_frames is not None and scanned_frames >= max_frames:
            break
    # Keep approximately requested fraction of negatives, deterministically.
    by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_split[record["split"]].append(record)
    kept: list[dict[str, Any]] = []
    for rows in by_split.values():
        positives = [r for r in rows if r["labels"]]
        negatives = [r for r in rows if not r["labels"]]
        nneg = (
            min(
                len(negatives),
                round(len(positives) * negative_ratio / max(1e-6, 1 - negative_ratio)),
            )
            if positives
            else min(len(negatives), 1)
        )
        rng.shuffle(negatives)
        kept.extend(positives + negatives[:nneg])
    counts = Counter()
    category_size_counts = Counter()
    split_counts = Counter()
    manifest_rows = []
    for idx, record in enumerate(
        sorted(kept, key=lambda r: (r["split"], r["world_id"], r["source"]))
    ):
        stem = f"{record['world_id']}_{record['episode_id']}_{idx:06d}"
        image_path = output / "images" / record["split"] / f"{stem}.png"
        label_path = output / "labels" / record["split"] / f"{stem}.txt"
        cv2.imwrite(str(image_path), record["image"])
        label_path.write_text(
            "\n".join(record["labels"]) + ("\n" if record["labels"] else ""),
            encoding="utf-8",
            newline="\n",
        )
        for cls in record["classes"]:
            counts[cls] += 1
        for obj in record["objects"]:
            box = obj["bbox_xyxy"]
            short = min(box[2] - box[0], box[3] - box[1])
            bucket = (
                "<8"
                if short < 8
                else "8-16"
                if short < 16
                else "16-32"
                if short < 32
                else "32-64"
                if short < 64
                else ">=64"
            )
            category_size_counts[(obj["class"], bucket)] += 1
        split_counts[(record["split"], "frames")] += 1
        split_counts[(record["split"], "positives" if record["labels"] else "negatives")] += 1
        manifest_rows.append(
            {
                "image": str(image_path.relative_to(output)).replace("\\", "/"),
                "label": str(label_path.relative_to(output)).replace("\\", "/"),
                **{
                    k: record[k]
                    for k in (
                        "source",
                        "episode_id",
                        "world_id",
                        "split",
                        "sha256",
                        "classes",
                        "objects",
                        "pose",
                        "camera_info",
                        "depth_camera_info",
                        "depth",
                    )
                },
            }
        )
        if preview and idx < preview:
            canvas = record["image"].copy()
            for line in record["labels"]:
                _, xc, yc, bw, bh = (float(v) for v in line.split())
                x1, x2 = int((xc - bw / 2) * canvas.shape[1]), int((xc + bw / 2) * canvas.shape[1])
                y1, y2 = int((yc - bh / 2) * canvas.shape[0]), int((yc + bh / 2) * canvas.shape[0])
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 255, 0), 2)
            (output / "preview").mkdir(exist_ok=True)
            cv2.imwrite(str(output / "preview" / f"{stem}.png"), canvas)
    data_yaml = (
        f"path: {output.resolve().as_posix()}\n"
        "train: images/train\nval: images/val\ntest: images/test\nnames:\n"
        + "\n".join(f"  {i}: {name}" for i, name in enumerate(class_names))
        + "\n"
    )
    (output / "data.yaml").write_text(data_yaml, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": 1,
        "label_source": label_source,
        "min_seg_area_px": min_seg_area_px,
        "classes": list(class_names),
        "negative_ratio": negative_ratio,
        "min_box_px": min_box_px,
        "sim_skew": {
            "limit_ms": float(max_sim_skew_ms),
            "skip_over_limit": bool(skip_over_skew),
            "skipped_steps": skipped_sim_skew_steps,
        },
        "visibility": {
            "min_visible_fraction": float(min_visible_fraction),
            "dropped_occluded": dropped_occluded,
        },
        "index_sha256": (
            _sha(index_path)
            if isinstance(index_path, Path)
            else [_sha(path) for path in index_path]
        ),
        "counts": {
            "frames": len(manifest_rows),
            "classes": dict(counts),
            "splits": {
                s: {k: v for (ss, k), v in split_counts.items() if ss == s}
                for s in ("train", "val", "test")
            },
            "category_size_histogram": {
                category: {
                    bucket: category_size_counts[(category, bucket)]
                    for bucket in ("<8", "8-16", "16-32", "32-64", ">=64")
                }
                for category in class_names
            },
        },
        "dedupe": {
            **dict(dedupe),
            "unique_sha": len(seen_sha),
            "unique_perceptual": len(seen_perceptual),
        },
        "scanned_frames": scanned_frames,
        "holdout_assets": sorted(holdout_assets or set()),
        "heldout_asset_images": {
            split: {
                asset: heldout_asset_images[(split, asset)]
                for asset in sorted(holdout_assets or set())
            }
            for split in ("train", "val")
        },
        "remaining_asset_instances": {
            split: {
                asset: remaining_asset_instances[(split, asset)]
                for asset in sorted(holdout_assets or set())
            }
            for split in ("train", "val")
        },
        "frames": manifest_rows,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def resolve_preset(args: argparse.Namespace) -> argparse.Namespace:
    """Fill preset defaults; an explicit --negative-ratio always wins."""
    if args.negative_ratio is None:
        args.negative_ratio = 0.3 if args.preset == "cruise" else 0.15
    if args.preset == "cruise":
        args.include_incomplete = True
    return args


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--index", type=Path, action="append", required=True)
    parser.add_argument("--min-box-px", type=float, default=4.0)
    parser.add_argument("--negative-ratio", type=float, default=None, help="default 0.15 (0.3 with --preset cruise)")
    parser.add_argument(
        "--preset",
        choices=("hover", "cruise"),
        default="hover",
        help="cruise: FW-cruise episodes (high-altitude frames are mostly negatives, no target-in-view requirement): "
        "negative ratio 0.3 and unfinished episodes are salvaged",
    )
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--preview", type=int, default=0)
    parser.add_argument(
        "--max-sim-skew-ms",
        type=float,
        default=250.0,
        help="skip steps whose observation sim_skew_ms exceeds this limit (default: 250)",
    )
    parser.add_argument(
        "--no-skip-over-skew",
        action="store_true",
        help="retain steps over the simulation-skew limit",
    )
    parser.add_argument("--min-visible-fraction", type=float, default=0.3)
    parser.add_argument(
        "--label-source", choices=("projection", "segmentation"), default="projection"
    )
    parser.add_argument("--min-seg-area-px", type=int, default=4)
    parser.add_argument("--truth-root", action="append", type=Path, default=[])
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--holdout-asset", action="append", default=[])
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="also read episodes whose episode.json has no status=complete (salvage unfinished runs)",
    )
    args = parser.parse_args(argv)
    resolve_preset(args)
    report = build_dataset(
        args.campaign_root,
        args.output,
        index_path=args.index,
        min_box_px=args.min_box_px,
        negative_ratio=args.negative_ratio,
        seed=args.seed,
        preview=args.preview,
        max_sim_skew_ms=args.max_sim_skew_ms,
        skip_over_skew=not args.no_skip_over_skew,
        min_visible_fraction=args.min_visible_fraction,
        label_source=args.label_source,
        min_seg_area_px=args.min_seg_area_px,
        truth_roots=args.truth_root,
        max_frames=args.max_frames,
        holdout_assets=set(args.holdout_asset),
        include_incomplete=args.include_incomplete,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "frames": report["counts"]["frames"],
                "splits": report["counts"]["splits"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
