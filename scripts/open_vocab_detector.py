"""Model-free dataset and scoring helpers for open-vocabulary experiments."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS = ROOT / "artifacts/cache/yoloe/yoloe-11s-seg.pt"
DEFAULT_HOLDOUT = "stop_sign,postbox,pickup"
PHRASES = {
    "vehicle": "car",
    "suv": "SUV",
    "pickup": "pickup truck",
    "fire_truck": "fire truck",
    "traffic_cone": "traffic cone",
    "jersey_barrier": "concrete barrier",
    "fire_hydrant": "fire hydrant",
    "electrical_box": "electrical box",
    "cardboard_box": "cardboard box",
    "stop_sign": "stop sign",
    "postbox": "post box",
    "cargo_cart": "cargo cart",
    "mailbox": "mailbox",
    "rover": "rover",
    "person": "person",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}


def prompt_for(name: str) -> str:
    return PHRASES.get(name, name.replace("_", " "))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path: Path, value: dict) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_dataset(data: Path) -> tuple[list[str], dict[str, list[Path]]]:
    data = data.resolve()
    config = yaml.safe_load(data.read_text(encoding="utf-8"))
    raw = config["names"]
    names = list(raw) if isinstance(raw, list) else [raw[i] for i in range(len(raw))]
    if len(set(names)) != len(names):
        raise ValueError("Dataset names must be unique")
    root = Path(config.get("path", data.parent))
    if not root.is_absolute():
        root = data.parent / root
    splits = {}
    for split in ("train", "val", "test"):
        images = []
        entries = config.get(split, [])
        for entry in entries if isinstance(entries, list) else [entries]:
            path = Path(entry)
            path = path if path.is_absolute() else root / path
            if path.is_dir():
                images.extend(
                    p.resolve() for p in path.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES
                )
            elif path.suffix == ".txt":
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        image = Path(line.strip())
                        images.append(
                            (image if image.is_absolute() else path.parent / image).resolve()
                        )
            else:
                raise FileNotFoundError(path)
        splits[split] = sorted(set(images))
        for image in splits[split]:
            if not image.is_file():
                raise FileNotFoundError(image)
    return names, splits


def label_path(image: Path) -> Path:
    parts = list(image.parts)
    indices = [i for i, part in enumerate(parts) if part == "images"]
    if not indices:
        raise ValueError(f"Image path must contain an images directory: {image}")
    parts[indices[-1]] = "labels"
    return Path(*parts).with_suffix(".txt")


def read_labels(image: Path, names: list[str]) -> list[tuple[int, list[float]]]:
    path = label_path(image)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(f"Expected detection labels (class cx cy w h): {path}")
        cls, box = int(fields[0]), [float(v) for v in fields[1:]]
        if not 0 <= cls < len(names) or not all(math.isfinite(v) and 0 <= v <= 1 for v in box):
            raise ValueError(f"Invalid label: {path}: {line}")
        rows.append((cls, box))
    return rows


HOLDOUT_POLICIES = ("background", "drop-images")


def build_dataset_view(
    data: Path, out: Path, holdout: list[str], holdout_policy: str = "background"
) -> dict:
    """Derive a train/val view without holdout labels.

    ``background`` keeps images that contain holdout objects, which then act as
    negatives for those objects; ``drop-images`` removes such images so the
    held-out categories are never shown at all during fine-tuning.
    """
    if holdout_policy not in HOLDOUT_POLICIES:
        raise ValueError(f"Unknown holdout policy: {holdout_policy}")
    names, splits = load_dataset(data)
    unknown = set(holdout) - set(names)
    if unknown:
        raise ValueError(f"Unknown holdout classes: {sorted(unknown)}")
    observed = {
        names[cls]
        for split in ("train", "val")
        for image in splits[split]
        for cls, _ in read_labels(image, names)
    }
    remaining = [name for name in names if name not in holdout and name in observed]
    if not remaining:
        raise ValueError("Holdout removes all classes")
    remap = {i: remaining.index(name) for i, name in enumerate(names) if name in remaining}
    dataset = out.resolve() / "dataset"
    if (dataset / "manifest.json").exists():
        previous = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
        if (
            previous["source_data"] != str(data.resolve())
            or previous["holdout_classes"] != holdout
            or previous.get("holdout_policy", "background") != holdout_policy
        ):
            raise ValueError(
                "Output dataset belongs to a different experiment; choose a fresh --out"
            )
    hashes = {str(data.resolve()): sha256(data)}
    source_manifest = data.resolve().parent / "manifest.json"
    if source_manifest.exists():
        hashes[str(source_manifest)] = sha256(source_manifest)
    manifest = {
        "source_data": str(data.resolve()),
        "holdout_classes": holdout,
        "holdout_policy": holdout_policy,
        "names": remaining,
        "excluded_unobserved_classes": [
            name for name in names if name not in holdout and name not in observed
        ],
        "class_id_map": remap,
        "splits": {},
        "source_hashes": hashes,
        "test_policy": "Original test remains untouched; evaluate original data.yaml",
    }
    for split, images in splits.items():
        source, retained, removed = Counter(), Counter(), Counter()
        backgrounds = 0
        dropped = 0
        for index, image in enumerate(images):
            label = label_path(image)
            hashes[str(label)] = sha256(label) if label.exists() else None
            # Hash image bytes too, so later source mutations are detectable.
            hashes[str(image)] = sha256(image)
            rows = read_labels(image, names)
            source.update(names[cls] for cls, _ in rows)
            filtered = [(cls, box) for cls, box in rows if cls in remap]
            if split == "test":
                retained.update(names[cls] for cls, _ in rows)
                continue
            if holdout_policy == "drop-images" and len(filtered) != len(rows):
                removed.update(names[cls] for cls, _ in rows if cls not in remap)
                dropped += 1
                continue
            retained.update(names[cls] for cls, _ in filtered)
            removed.update(names[cls] for cls, _ in rows if cls not in remap)
            backgrounds += not filtered
            target = dataset / "images" / split / f"{index:08d}{image.suffix.lower()}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if not os.path.samefile(image, target):
                    raise ValueError(f"Existing image is not the intended hardlink: {target}")
            else:
                # Fail rather than silently copy images across volumes.
                os.link(image, target)
            write_text(
                label_path(target),
                "".join(
                    f"{remap[cls]} {' '.join(format(v, '.10g') for v in box)}\n"
                    for cls, box in filtered
                ),
            )
        manifest["splits"][split] = {
            "images": len(images),
            "source_counts": {n: source[n] for n in names},
            "retained_counts": {n: retained[n] for n in names},
            "removed_counts": {n: removed[n] for n in names},
            "background_images": backgrounds if split != "test" else None,
            "dropped_images": dropped if split != "test" else None,
        }
    config = {
        "path": dataset.as_posix(),
        "train": "images/train",
        "val": "images/val",
        "names": dict(enumerate(remaining)),
    }
    write_text(dataset / "data.yaml", yaml.safe_dump(config, sort_keys=False))
    write_json(dataset / "manifest.json", manifest)
    return manifest


def box_iou(a: list[float], b: list[float]) -> float:
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )

    def area(box):
        return max(0, box[2] - box[0]) * max(0, box[3] - box[1])

    union = area(a) + area(b) - intersection
    return intersection / union if union > 0 else 0.0


def average_precision(events: list[tuple[float, bool]], total: int) -> float | None:
    """101-point interpolated AP over the global confidence ranking."""
    if not total:
        return None
    tp = 0
    curve = []
    for rank, (_, hit) in enumerate(sorted(events, key=lambda row: -row[0]), 1):
        tp += hit
        curve.append((tp / total, tp / rank))
    return (
        sum(
            max((p for r, p in curve if r >= threshold / 100), default=0.0)
            for threshold in range(101)
        )
        / 101
    )


def score_predictions(
    truth: list[list[dict]],
    predictions: list[list[dict]],
    classes: list[str],
    holdout: list[str],
    iou_threshold: float = 0.5,
    confidence: float = 0.25,
) -> dict:
    events = {name: [] for name in classes}
    totals = Counter(obj["class"] for frame in truth for obj in frame)
    for gt, pred in zip(truth, predictions, strict=True):
        used = set()
        for obj in sorted(pred, key=lambda row: -row["confidence"]):
            name = obj["class"]
            candidates = [
                (box_iou(obj["box"], target["box"]), i)
                for i, target in enumerate(gt)
                if i not in used and target["class"] == name
            ]
            overlap, index = max(candidates, default=(0.0, -1))
            hit = overlap >= iou_threshold and index >= 0
            if hit:
                used.add(index)
            events[name].append((obj["confidence"], hit))
    per_class = {}
    for name in classes:
        selected = [hit for score, hit in events[name] if score >= confidence]
        tp, fp = sum(selected), len(selected) - sum(selected)
        per_class[name] = {
            "gt": totals[name],
            "tp": tp,
            "fp": fp,
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / totals[name] if totals[name] else None,
            "ap50": average_precision(events[name], totals[name]),
        }
    aggregates = {}
    for scope, subset in {
        "all": classes,
        "seen": [n for n in classes if n not in holdout],
        "holdout": [n for n in classes if n in holdout],
    }.items():
        rows = [per_class[n] for n in subset]
        tp, fp, total = (sum(row[k] for row in rows) for k in ("tp", "fp", "gt"))
        aps = [row["ap50"] for row in rows if row["ap50"] is not None]
        aggregates[scope] = {
            "classes": subset,
            "gt": total,
            "tp": tp,
            "fp": fp,
            "precision": tp / (tp + fp) if tp + fp else 0.0,
            "recall": tp / total if total else None,
            "map50": sum(aps) / len(aps) if aps else None,
        }
    return {
        "per_class": per_class,
        "aggregates": aggregates,
        "iou_threshold": iou_threshold,
        "pr_confidence": confidence,
        "ap_definition": "101-point interpolated precision envelope; global confidence ranking",
    }


def configure_offline(weights: Path, weights_dir: Path) -> None:
    for key, value in {
        "YOLO_OFFLINE": "true",
        "YOLO_AUTOINSTALL": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }.items():
        os.environ[key] = value
    if not weights.is_file():
        raise FileNotFoundError(weights)
    if not (weights_dir / "mobileclip_blt.ts").is_file():
        raise FileNotFoundError(weights_dir / "mobileclip_blt.ts")


def holdout_list(value: str) -> list[str]:
    return list(dict.fromkeys(n.strip() for n in value.split(",") if n.strip()))


def configure_ultralytics(weights_dir: Path) -> None:
    """Resolve assets locally and disable auxiliary downloads in this process."""
    from ultralytics.data import utils as data_utils
    from ultralytics.utils import SETTINGS, downloads

    dict.__setitem__(SETTINGS, "weights_dir", str(weights_dir.resolve()))
    for key in ("sync", "wandb", "comet", "clearml", "neptune", "mlflow", "hub"):
        if key in SETTINGS:
            dict.__setitem__(SETTINGS, key, False)

    def local_asset(file, *args, **kwargs):
        path = Path(file)
        for candidate in (path, weights_dir / path):
            if candidate.is_file():
                return str(candidate.resolve())
        raise FileNotFoundError(f"Offline asset missing: {file}")

    def no_download(*args, **kwargs):
        raise RuntimeError("Downloads are disabled for this experiment")

    downloads.attempt_download_asset = local_asset
    downloads.safe_download = no_download
    # check_det_dataset unconditionally checks/downloads fonts, even with plots=False.
    data_utils.check_font = lambda *args, **kwargs: None
