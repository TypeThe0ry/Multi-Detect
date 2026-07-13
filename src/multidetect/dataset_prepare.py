from __future__ import annotations

import json
import math
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class LabelRepair:
    line_number: int
    action: str
    reason: str
    original: str
    repaired: str | None


@dataclass(frozen=True, slots=True)
class ScenarioRepeatDecision:
    repeat: int
    reasons: tuple[str, ...]


def scenario_repeat_decision(
    label_text: str,
    *,
    mean_brightness: float,
    dark_threshold: float = 70.0,
    small_box_area_threshold: float = 0.015,
    maximum_repeat: int = 2,
) -> ScenarioRepeatDecision:
    """Select auditable oversampling for difficult positive training images.

    Empty-label hard negatives are deliberately never repeated here; their balance
    remains controlled by the dedicated hard-negative dataset builder.
    """

    for name, value in (
        ("mean_brightness", mean_brightness),
        ("dark_threshold", dark_threshold),
        ("small_box_area_threshold", small_box_area_threshold),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if isinstance(maximum_repeat, bool) or not isinstance(maximum_repeat, int):
        raise ValueError("maximum_repeat must be an integer")
    if not 1 <= maximum_repeat <= 10:
        raise ValueError("maximum_repeat must be in [1, 10]")
    box_areas: list[float] = []
    for line_number, raw in enumerate(label_text.splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 5:
            raise ValueError(f"invalid YOLO label at line {line_number}")
        try:
            width, height = float(parts[3]), float(parts[4])
        except ValueError as exc:
            raise ValueError(f"invalid YOLO label at line {line_number}") from exc
        if not all(math.isfinite(value) and value > 0 for value in (width, height)):
            raise ValueError(f"invalid YOLO box size at line {line_number}")
        box_areas.append(width * height)
    if not box_areas:
        return ScenarioRepeatDecision(1, ())
    reasons: list[str] = []
    if mean_brightness < dark_threshold:
        reasons.append("dark_positive")
    if min(box_areas) <= small_box_area_threshold:
        reasons.append("small_positive")
    return ScenarioRepeatDecision(min(maximum_repeat, 1 + len(reasons)), tuple(reasons))


def remap_and_repair_yolo_labels(
    text: str,
    *,
    class_map: dict[int, int],
) -> tuple[str, tuple[LabelRepair, ...]]:
    """Remap classes and clip partially visible YOLO boxes with an audit trail."""

    output: list[str] = []
    repairs: list[LabelRepair] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        original = raw.strip()
        if not original:
            continue
        parts = original.split()
        if len(parts) != 5:
            repairs.append(
                LabelRepair(line_number, "dropped", "expected_five_fields", original, None)
            )
            continue
        try:
            source_class = int(parts[0])
            x_center, y_center, width, height = (float(value) for value in parts[1:])
        except ValueError:
            repairs.append(LabelRepair(line_number, "dropped", "non_numeric", original, None))
            continue
        target_class = class_map.get(source_class)
        values = (x_center, y_center, width, height)
        if target_class is None:
            repairs.append(LabelRepair(line_number, "dropped", "unmapped_class", original, None))
            continue
        if not all(math.isfinite(value) for value in values) or width <= 0.0 or height <= 0.0:
            repairs.append(
                LabelRepair(line_number, "dropped", "invalid_box_values", original, None)
            )
            continue

        x1 = x_center - width / 2.0
        y1 = y_center - height / 2.0
        x2 = x_center + width / 2.0
        y2 = y_center + height / 2.0
        clipped = (
            max(0.0, x1),
            max(0.0, y1),
            min(1.0, x2),
            min(1.0, y2),
        )
        cx1, cy1, cx2, cy2 = clipped
        if cx2 <= cx1 or cy2 <= cy1:
            repairs.append(LabelRepair(line_number, "dropped", "box_outside_image", original, None))
            continue
        repaired_values = (
            (cx1 + cx2) / 2.0,
            (cy1 + cy2) / 2.0,
            cx2 - cx1,
            cy2 - cy1,
        )
        repaired = " ".join(
            (str(target_class), *(_format_float(value) for value in repaired_values))
        )
        output.append(repaired)
        geometry_changed = any(
            abs(before - after) > 1e-9
            for before, after in zip((x1, y1, x2, y2), clipped, strict=True)
        )
        repairs.append(
            LabelRepair(
                line_number,
                "clipped_and_remapped" if geometry_changed else "remapped",
                "box_clipped_to_image" if geometry_changed else "class_mapping_applied",
                original,
                repaired,
            )
        )
    encoded = "".join(f"{line}\n" for line in output)
    return encoded, tuple(repairs)


def prepare_dfire_archive(
    archive: Path,
    out_dir: Path,
    *,
    extract_images: bool,
) -> dict[str, Any]:
    """Prepare the audited D-Fire mirror for the local fire=0/smoke=1 contract."""

    archive = archive.resolve()
    out_dir = out_dir.resolve()
    if not archive.is_file():
        raise FileNotFoundError(archive)
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "source_archive": str(archive),
        "source_class_map": {"0": "smoke", "1": "fire"},
        "output_class_map": {"0": "fire", "1": "smoke"},
        "extract_images": extract_images,
        "splits": {},
        "repairs": [],
    }
    with zipfile.ZipFile(archive) as bundle:
        entries = {entry.filename: entry for entry in bundle.infolist() if not entry.is_dir()}
        for split in ("train", "test"):
            label_entries = sorted(
                name
                for name in entries
                if name.startswith(f"{split}/labels/") and name.endswith(".txt")
            )
            image_entries = sorted(
                name
                for name in entries
                if name.startswith(f"{split}/images/")
                and Path(name).suffix.lower() in {".jpg", ".jpeg", ".png"}
            )
            image_stems = {Path(name).stem for name in image_entries}
            label_stems = {Path(name).stem for name in label_entries}
            if image_stems != label_stems:
                raise ValueError(f"D-Fire {split} image/label stems do not match")
            label_out = out_dir / "labels" / split
            label_out.mkdir(parents=True, exist_ok=True)
            image_out = out_dir / "images" / split
            if extract_images:
                image_out.mkdir(parents=True, exist_ok=True)

            backgrounds = boxes = clipped = dropped = 0
            for entry_name in label_entries:
                text = bundle.read(entry_name).decode("utf-8")
                repaired_text, repairs = remap_and_repair_yolo_labels(
                    text,
                    class_map={0: 1, 1: 0},
                )
                destination = label_out / Path(entry_name).name
                destination.write_text(repaired_text, encoding="utf-8", newline="\n")
                if not repaired_text:
                    backgrounds += 1
                boxes += len(repaired_text.splitlines())
                for repair in repairs:
                    if repair.action == "clipped_and_remapped":
                        clipped += 1
                    elif repair.action == "dropped":
                        dropped += 1
                    if repair.action != "remapped":
                        report["repairs"].append(
                            {
                                "split": split,
                                "label": Path(entry_name).name,
                                **asdict(repair),
                            }
                        )
            if extract_images:
                for entry_name in image_entries:
                    (image_out / Path(entry_name).name).write_bytes(bundle.read(entry_name))
            report["splits"][split] = {
                "images": len(image_entries),
                "labels": len(label_entries),
                "background_images": backgrounds,
                "boxes": boxes,
                "clipped_boxes": clipped,
                "dropped_boxes": dropped,
            }

    report["repair_count"] = len(report["repairs"])
    report_path = out_dir / "preparation-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if extract_images:
        (out_dir / "data.yaml").write_text(
            "\n".join(
                (
                    f'path: "{out_dir.as_posix()}"',
                    "train: images/train",
                    "test: images/test",
                    "names:",
                    "  0: Fire",
                    "  1: smoke",
                    "",
                )
            ),
            encoding="utf-8",
        )
    return report


def _format_float(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


__all__ = [
    "LabelRepair",
    "ScenarioRepeatDecision",
    "prepare_dfire_archive",
    "remap_and_repair_yolo_labels",
    "scenario_repeat_decision",
]
