from __future__ import annotations

import hashlib
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp"})
_ROBOFLOW_SUFFIX = re.compile(r"(?i)(?:_jpe?g|_png)?\.rf\.[0-9a-f]+$")


@dataclass(frozen=True, slots=True)
class YoloSplit:
    name: str
    image_dir: Path
    label_dir: Path


def discover_yolo_splits(root: Path) -> tuple[YoloSplit, ...]:
    root = root.resolve()
    candidates = (
        ("train", root / "images" / "train", root / "labels" / "train"),
        ("val", root / "images" / "val", root / "labels" / "val"),
        ("test", root / "images" / "test", root / "labels" / "test"),
        ("train", root / "train" / "images", root / "train" / "labels"),
        ("val", root / "valid" / "images", root / "valid" / "labels"),
        ("val", root / "val" / "images", root / "val" / "labels"),
        ("test", root / "test" / "images", root / "test" / "labels"),
    )
    discovered: dict[str, YoloSplit] = {}
    for name, image_dir, label_dir in candidates:
        if name not in discovered and image_dir.is_dir() and label_dir.is_dir():
            discovered[name] = YoloSplit(name, image_dir.resolve(), label_dir.resolve())
    if not discovered:
        raise ValueError(f"no supported YOLO split layout found below {root}")
    return tuple(discovered[name] for name in ("train", "val", "test") if name in discovered)


def source_stem(path: Path) -> str:
    """Group Roboflow variants under their pre-augmentation source stem."""

    return _ROBOFLOW_SUFFIX.sub("", path.stem)


def audit_yolo_dataset(
    root: Path,
    *,
    class_count: int,
    hash_images: bool = True,
    maximum_examples: int = 20,
) -> dict[str, Any]:
    if class_count <= 0:
        raise ValueError("class_count must be positive")
    if maximum_examples <= 0:
        raise ValueError("maximum_examples must be positive")
    splits = discover_yolo_splits(root)
    exact_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    stem_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    split_reports: dict[str, dict[str, Any]] = {}
    total_images = 0
    total_boxes = 0
    total_backgrounds = 0
    all_invalid: list[dict[str, object]] = []

    for split in splits:
        images = sorted(
            path
            for path in split.image_dir.iterdir()
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
        )
        labels = {path.stem: path for path in split.label_dir.glob("*.txt") if path.is_file()}
        image_stems = {path.stem for path in images}
        missing_labels: list[str] = []
        invalid_labels: list[dict[str, object]] = []
        class_counts = {class_id: 0 for class_id in range(class_count)}
        raw_class_counts = {class_id: 0 for class_id in range(class_count)}
        box_count = 0
        background_count = 0

        for image in images:
            item = {"split": split.name, "name": image.name}
            stem_groups[source_stem(image)].append(item)
            if hash_images:
                exact_groups[_sha256(image)].append(item)
            label = labels.get(image.stem)
            if label is None:
                missing_labels.append(image.name)
                continue
            label_text = label.read_text(encoding="utf-8")
            valid_boxes, errors = _audit_label_text(label_text, class_count=class_count)
            for class_id in _raw_class_ids(label_text, class_count=class_count):
                raw_class_counts[class_id] += 1
            if not valid_boxes and not errors:
                background_count += 1
            for class_id in valid_boxes:
                class_counts[class_id] += 1
                box_count += 1
            invalid_labels.extend(
                {"label": label.name, "line": line_number, "reason": reason}
                for line_number, reason in errors
            )

        orphan_labels = sorted(
            label.name for stem, label in labels.items() if stem not in image_stems
        )
        split_reports[split.name] = {
            "image_directory": str(split.image_dir),
            "label_directory": str(split.label_dir),
            "images": len(images),
            "labels": len(labels),
            "background_images": background_count,
            "boxes": box_count,
            "class_box_counts": {str(key): value for key, value in class_counts.items()},
            "raw_class_box_counts": {str(key): value for key, value in raw_class_counts.items()},
            "missing_label_count": len(missing_labels),
            "missing_label_examples": missing_labels[:maximum_examples],
            "orphan_label_count": len(orphan_labels),
            "orphan_label_examples": orphan_labels[:maximum_examples],
            "invalid_label_count": len(invalid_labels),
            "invalid_label_examples": invalid_labels[:maximum_examples],
        }
        total_images += len(images)
        total_boxes += box_count
        total_backgrounds += background_count
        all_invalid.extend({"split": split.name, **item} for item in invalid_labels)

    exact_duplicates = [items for items in exact_groups.values() if len(items) > 1]
    repeated_stems = [items for items in stem_groups.values() if len(items) > 1]
    cross_exact = [items for items in exact_duplicates if _crosses_splits(items)]
    cross_stems = [items for items in repeated_stems if _crosses_splits(items)]
    missing_total = sum(report["missing_label_count"] for report in split_reports.values())
    orphan_total = sum(report["orphan_label_count"] for report in split_reports.values())
    return {
        "schema_version": 1,
        "root": str(root.resolve()),
        "class_count": class_count,
        "splits": split_reports,
        "totals": {
            "images": total_images,
            "background_images": total_backgrounds,
            "boxes": total_boxes,
            "missing_labels": missing_total,
            "orphan_labels": orphan_total,
            "invalid_labels": len(all_invalid),
        },
        "duplicates": {
            "hashing_enabled": hash_images,
            "exact_duplicate_groups": len(exact_duplicates),
            "cross_split_exact_groups": len(cross_exact),
            "cross_split_exact_examples": cross_exact[:maximum_examples],
            "repeated_source_stem_groups": len(repeated_stems),
            "cross_split_source_stem_groups": len(cross_stems),
            "cross_split_source_stem_examples": cross_stems[:maximum_examples],
        },
        "clean": not (missing_total or orphan_total or all_invalid or cross_exact or cross_stems),
    }


def audit_yolo_zip(
    archive: Path,
    *,
    class_count: int,
    maximum_examples: int = 20,
) -> dict[str, Any]:
    """Audit labels and split leakage directly from a YOLO ZIP without extracting images."""

    if class_count <= 0:
        raise ValueError("class_count must be positive")
    if maximum_examples <= 0:
        raise ValueError("maximum_examples must be positive")
    archive = archive.resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    split_entries: dict[str, dict[str, dict[str, str]]] = defaultdict(
        lambda: {"images": {}, "labels": {}}
    )
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if entry.is_dir():
                continue
            parts = Path(entry.filename).parts
            if len(parts) != 3:
                continue
            raw_split, kind, filename = parts
            split = "val" if raw_split.lower() in {"val", "valid"} else raw_split.lower()
            if split not in {"train", "val", "test"} or kind not in {"images", "labels"}:
                continue
            suffix = Path(filename).suffix.lower()
            if kind == "images" and suffix not in _IMAGE_SUFFIXES:
                continue
            if kind == "labels" and suffix != ".txt":
                continue
            split_entries[split][kind][Path(filename).stem] = entry.filename

        split_reports: dict[str, dict[str, Any]] = {}
        stem_groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        total_images = total_backgrounds = total_boxes = 0
        invalid_total = missing_total = orphan_total = 0
        for split in ("train", "val", "test"):
            if split not in split_entries:
                continue
            images = split_entries[split]["images"]
            labels = split_entries[split]["labels"]
            class_counts = {class_id: 0 for class_id in range(class_count)}
            raw_class_counts = {class_id: 0 for class_id in range(class_count)}
            missing = sorted(stem for stem in images if stem not in labels)
            orphan = sorted(stem for stem in labels if stem not in images)
            invalid: list[dict[str, object]] = []
            backgrounds = boxes = 0
            for stem, image_entry in images.items():
                stem_groups[source_stem(Path(image_entry))].append(
                    {"split": split, "name": Path(image_entry).name}
                )
                label_entry = labels.get(stem)
                if label_entry is None:
                    continue
                try:
                    text = bundle.read(label_entry).decode("utf-8")
                except UnicodeDecodeError:
                    invalid.append(
                        {
                            "label": Path(label_entry).name,
                            "line": 0,
                            "reason": "not UTF-8",
                        }
                    )
                    continue
                for class_id in _raw_class_ids(text, class_count=class_count):
                    raw_class_counts[class_id] += 1
                valid_boxes, errors = _audit_label_text(text, class_count=class_count)
                if not valid_boxes and not errors:
                    backgrounds += 1
                for class_id in valid_boxes:
                    class_counts[class_id] += 1
                    boxes += 1
                invalid.extend(
                    {"label": Path(label_entry).name, "line": line, "reason": reason}
                    for line, reason in errors
                )
            split_reports[split] = {
                "images": len(images),
                "labels": len(labels),
                "background_images": backgrounds,
                "boxes": boxes,
                "class_box_counts": {str(key): value for key, value in class_counts.items()},
                "raw_class_box_counts": {
                    str(key): value for key, value in raw_class_counts.items()
                },
                "missing_label_count": len(missing),
                "missing_label_examples": missing[:maximum_examples],
                "orphan_label_count": len(orphan),
                "orphan_label_examples": orphan[:maximum_examples],
                "invalid_label_count": len(invalid),
                "invalid_label_examples": invalid[:maximum_examples],
            }
            total_images += len(images)
            total_backgrounds += backgrounds
            total_boxes += boxes
            missing_total += len(missing)
            orphan_total += len(orphan)
            invalid_total += len(invalid)

    repeated_stems = [items for items in stem_groups.values() if len(items) > 1]
    cross_stems = [items for items in repeated_stems if _crosses_splits(items)]
    return {
        "schema_version": 1,
        "archive": str(archive),
        "class_count": class_count,
        "splits": split_reports,
        "totals": {
            "images": total_images,
            "background_images": total_backgrounds,
            "boxes": total_boxes,
            "missing_labels": missing_total,
            "orphan_labels": orphan_total,
            "invalid_labels": invalid_total,
        },
        "duplicates": {
            "hashing_enabled": False,
            "exact_duplicate_groups": None,
            "cross_split_exact_groups": None,
            "repeated_source_stem_groups": len(repeated_stems),
            "cross_split_source_stem_groups": len(cross_stems),
            "cross_split_source_stem_examples": cross_stems[:maximum_examples],
        },
        "clean": not (missing_total or orphan_total or invalid_total or cross_stems),
    }


def _audit_label_text(
    text: str,
    *,
    class_count: int,
) -> tuple[list[int], list[tuple[int, str]]]:
    classes: list[int] = []
    errors: list[tuple[int, str]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 5:
            errors.append((line_number, "expected five YOLO fields"))
            continue
        try:
            class_id = int(parts[0])
            x_center, y_center, width, height = (float(value) for value in parts[1:])
        except ValueError:
            errors.append((line_number, "fields are not numeric"))
            continue
        if not 0 <= class_id < class_count:
            errors.append((line_number, "class id is out of range"))
            continue
        if not (
            0.0 <= x_center <= 1.0
            and 0.0 <= y_center <= 1.0
            and 0.0 < width <= 1.0
            and 0.0 < height <= 1.0
        ):
            errors.append((line_number, "normalized box values are out of range"))
            continue
        if (
            x_center - width / 2.0 < -1e-6
            or x_center + width / 2.0 > 1.0 + 1e-6
            or y_center - height / 2.0 < -1e-6
            or y_center + height / 2.0 > 1.0 + 1e-6
        ):
            errors.append((line_number, "box extends outside the normalized image"))
            continue
        classes.append(class_id)
    return classes, errors


def _raw_class_ids(text: str, *, class_count: int) -> list[int]:
    class_ids: list[int] = []
    for raw in text.splitlines():
        parts = raw.strip().split()
        if not parts:
            continue
        try:
            class_id = int(parts[0])
        except ValueError:
            continue
        if 0 <= class_id < class_count:
            class_ids.append(class_id)
    return class_ids


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _crosses_splits(items: list[dict[str, str]]) -> bool:
    return len({item["split"] for item in items}) > 1


__all__ = [
    "YoloSplit",
    "audit_yolo_dataset",
    "audit_yolo_zip",
    "discover_yolo_splits",
    "source_stem",
]
