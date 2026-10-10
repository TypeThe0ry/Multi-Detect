#!/usr/bin/env python3
"""Build a world-disjoint YOLO dataset from SIM teacher campaigns.

Labels are generated from privileged scenario truth and are used only while
building the training artifact.  Every frame is assigned to the split of its
world (from generated ``index.json``); no episode or frame can cross splits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2

try:
    from scripts.autolabel_sim_teacher_frames import project_target_bbox
except ModuleNotFoundError:
    from autolabel_sim_teacher_frames import project_target_bbox

CLASS_NAMES = ("vehicle", "rover", "cargo_cart", "orange_inspection_marker", "blue_survey_pylon")
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
        if part.startswith("gen_w") or part.startswith("urban_"):
            return part
    # campaign layouts occasionally put the world in episode.json only
    try:
        value = _load(ep / "episode.json")
        text = str(value.get("world", ""))
        if text:
            return text.split(".")[0]
    except Exception:
        pass
    return ep.parent.name


def _find_truth(world: str, roots: list[Path]) -> Path | None:
    names = [f"{world}.truth.json"]
    if world.startswith("urban_"):
        names.append("urban_inspection_multirotor_001.truth.json")
    for root in roots:
        for base in (root, root / "configs", root.parent, Path.cwd()):
            for name in names:
                for candidate in (
                    base / name,
                    base / "scenarios" / name,
                    base / "scenarios" / "generated" / name,
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


def _split_map(index_path: Path) -> dict[str, str]:
    try:
        doc = _load(index_path)
    except Exception:
        return {}
    return {
        str(w.get("world_id")): str(w.get("split", "train"))
        for w in doc.get("worlds", [])
        if isinstance(w, dict) and w.get("world_id")
    }


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
    index_path: Path = Path("configs/scenarios/generated/index.json"),
    min_box_px: float = 4.0,
    negative_ratio: float = 0.15,
    seed: int = 20261011,
    preview: int = 0,
    max_sim_skew_ms: float = 250.0,
    skip_over_skew: bool = True,
) -> dict[str, Any]:
    if not math.isfinite(float(max_sim_skew_ms)) or float(max_sim_skew_ms) < 0.0:
        raise ValueError("max_sim_skew_ms must be finite and non-negative")
    rng = random.Random(seed)
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
    seen_sha: set[str] = set()
    seen_perceptual: set[str] = set()
    for campaign in campaign_roots:
        for episode in _episode_dirs(campaign):
            metadata_path = episode / "episode.json"
            if not metadata_path.is_file() or _load(metadata_path).get("status") != "complete":
                continue
            world = _world_id(episode)
            if world.startswith("gen_w") and world not in splits:
                raise ValueError(f"world {world} is missing from {index_path}")
            split = splits.get(world, "train")
            if split not in ("train", "val", "test"):
                raise ValueError(f"invalid split {split!r} for {world}")
            truth_path = _find_truth(world, [campaign, Path.cwd()])
            truth = _load(truth_path) if truth_path else {}
            sdf_path = _find_sdf(world, [campaign, Path.cwd()])
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
                    digest = _sha(rgb)
                    pkey = _perceptual_key(image)
                    if digest in seen_sha or pkey in seen_perceptual:
                        continue
                    seen_sha.add(digest)
                    seen_perceptual.add(pkey)
                    h, w = image.shape[:2]
                    labels: list[str] = []
                    classes: list[str] = []
                    objects: list[dict[str, Any]] = []
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        metric = _target_metric(target)
                        if metric is None:
                            continue
                        name = str(target.get("class") or target.get("label") or "vehicle")
                        name = CLASS_ALIASES.get(name, name)
                        if name not in CLASS_NAMES:
                            name = "vehicle"
                        pose = {**obs.get("pose", {}), "attitude": obs.get("attitude", {})}
                        size = tuple(
                            target.get("size_m")
                            or sdf.get(
                                str(target.get("target_id")),
                                DEFAULT_SIZES.get(name, (3.0, 1.8, 1.5)),
                            )
                        )
                        yaw = _num((target.get("pose") or {}).get("yaw_rad")) or 0.0
                        box = project_target_bbox(
                            target_ned=metric,
                            pose=pose,
                            camera_info=obs.get("camera_info", {}),
                            image_size=(w, h),
                            target_size_m=size,
                            target_yaw_rad=yaw,
                        )
                        if (
                            box is None
                            or box[2] - box[0] < min_box_px
                            or box[3] - box[1] < min_box_px
                        ):
                            continue
                        labels.append(_label_line(CLASS_NAMES.index(name), box, (w, h)))
                        classes.append(name)
                        objects.append(
                            {
                                "class": name,
                                "target_id": target.get("target_id"),
                                "bbox_xyxy": [float(value) for value in box],
                                "target_ned": [float(value) for value in metric],
                                "size_m": [float(value) for value in size],
                            }
                        )
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
        + "\n".join(f"  {i}: {name}" for i, name in enumerate(CLASS_NAMES))
        + "\n"
    )
    (output / "data.yaml").write_text(data_yaml, encoding="utf-8", newline="\n")
    manifest = {
        "schema_version": 1,
        "classes": list(CLASS_NAMES),
        "negative_ratio": negative_ratio,
        "min_box_px": min_box_px,
        "sim_skew": {
            "limit_ms": float(max_sim_skew_ms),
            "skip_over_limit": bool(skip_over_skew),
            "skipped_steps": skipped_sim_skew_steps,
        },
        "index_sha256": _sha(index_path) if index_path.is_file() else None,
        "counts": {
            "frames": len(manifest_rows),
            "classes": dict(counts),
            "splits": {
                s: {k: v for (ss, k), v in split_counts.items() if ss == s}
                for s in ("train", "val", "test")
            },
        },
        "frames": manifest_rows,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--index", type=Path, default=Path("configs/scenarios/generated/index.json")
    )
    parser.add_argument("--min-box-px", type=float, default=4.0)
    parser.add_argument("--negative-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--preview", type=int, default=0)
    parser.add_argument(
        "--max-sim-skew-ms", type=float, default=250.0,
        help="skip steps whose observation sim_skew_ms exceeds this limit (default: 250)",
    )
    parser.add_argument(
        "--no-skip-over-skew", action="store_true",
        help="retain steps over the simulation-skew limit",
    )
    args = parser.parse_args(argv)
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
