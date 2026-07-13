from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from multidetect.dataset_prepare import scenario_repeat_decision


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build an auditable YOLO train list with explicit scenario repeats."
    )
    parser.add_argument("--base-train-list", type=Path, required=True)
    parser.add_argument("--ascii-local-root", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--dark-threshold", type=float, default=70.0)
    parser.add_argument("--small-box-area-threshold", type=float, default=0.015)
    parser.add_argument("--maximum-repeat", type=int, default=2)
    parser.add_argument("--background-repeat", type=int, default=1)
    args = parser.parse_args()

    if not 1 <= args.background_repeat <= 10:
        raise ValueError("background-repeat must be in [1, 10]")

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("OpenCV and NumPy are required for scenario balancing") from exc

    local_alias = args.ascii_local_root.absolute()
    local_target = local_alias.resolve()
    base_paths = tuple(
        Path(line.strip())
        for line in args.base_train_list.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not base_paths:
        raise ValueError("base training list is empty")
    output_lines: list[str] = []
    reason_counts: Counter[str] = Counter()
    repeat_histogram: Counter[int] = Counter()
    positive_images = background_images = 0
    for original_path in base_paths:
        image_path = _rewrite_under_alias(original_path, alias=local_alias, target=local_target)
        label_path = _label_path_for_image(image_path)
        if not image_path.is_file() or not label_path.is_file():
            raise FileNotFoundError(f"missing image/label pair: {image_path} / {label_path}")
        label_text = label_path.read_text(encoding="utf-8")
        if label_text.strip():
            positive_images += 1
            encoded = np.fromfile(str(image_path), dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"cannot decode training image: {image_path}")
            decision = scenario_repeat_decision(
                label_text,
                mean_brightness=float(image.mean()),
                dark_threshold=args.dark_threshold,
                small_box_area_threshold=args.small_box_area_threshold,
                maximum_repeat=args.maximum_repeat,
            )
        else:
            background_images += 1
            repeat = args.background_repeat
            reasons = ("background_oversample",) if repeat > 1 else ()
            output_lines.extend(f"{image_path.as_posix()}\n" for _ in range(repeat))
            repeat_histogram[repeat] += 1
            reason_counts.update(reasons)
            continue
        output_lines.extend(f"{image_path.as_posix()}\n" for _ in range(decision.repeat))
        repeat_histogram[decision.repeat] += 1
        reason_counts.update(decision.reasons)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    train_list = args.out_dir / "train.txt"
    train_list.write_text("".join(output_lines), encoding="utf-8")
    val_dir = local_alias / "images" / "val"
    test_dir = local_alias / "images" / "test"
    data_yaml = args.out_dir / "data.yaml"
    data_yaml.write_text(
        "\n".join(
            (
                f'train: "{train_list.resolve().as_posix()}"',
                f'val: "{val_dir.as_posix()}"',
                f'test: "{test_dir.as_posix()}"',
                "names:",
                "  0: Fire",
                "  1: smoke",
                "",
            )
        ),
        encoding="utf-8",
    )
    report = {
        "schema_version": 1,
        "base_train_list": str(args.base_train_list.resolve()),
        "ascii_local_root": str(local_alias),
        "unique_input_images": len(base_paths),
        "effective_train_images": len(output_lines),
        "positive_images": positive_images,
        "background_images": background_images,
        "policy": {
            "dark_threshold": args.dark_threshold,
            "small_box_area_threshold": args.small_box_area_threshold,
            "maximum_repeat": args.maximum_repeat,
            "background_repeat": args.background_repeat,
        },
        "repeat_histogram": dict(sorted(repeat_histogram.items())),
        "reason_counts": dict(sorted(reason_counts.items())),
        "data_yaml": str(data_yaml.resolve()),
        "images_copied": False,
        "labels_modified": False,
    }
    (args.out_dir / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


def _rewrite_under_alias(path: Path, *, alias: Path, target: Path) -> Path:
    absolute = path.absolute()
    try:
        relative = absolute.relative_to(target)
    except ValueError:
        return absolute
    return alias / relative


def _label_path_for_image(image_path: Path) -> Path:
    parts = list(image_path.parts)
    image_indexes = [index for index, part in enumerate(parts) if part.lower() == "images"]
    if not image_indexes:
        raise ValueError(f"image path has no images directory: {image_path}")
    parts[image_indexes[-1]] = "labels"
    return Path(*parts).with_suffix(".txt")


if __name__ == "__main__":
    raise SystemExit(main())
