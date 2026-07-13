from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a fire/smoke training list with local hard-negative augmentations."
    )
    parser.add_argument("--base-root", type=Path, required=True)
    parser.add_argument(
        "--hard-negative-root",
        type=Path,
        required=True,
        action="append",
        help="repeat for each hard-negative source; accepts images/train or images",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    base_root = args.base_root.resolve()
    hard_roots = tuple(path.resolve() for path in args.hard_negative_root)
    out_dir = args.out_dir.resolve()
    base_train = sorted((base_root / "images" / "train").glob("*.jpg"))
    negative_sources: list[tuple[Path, Path]] = []
    for hard_root in hard_roots:
        image_dir = hard_root / "images" / "train"
        if not image_dir.is_dir():
            image_dir = hard_root / "images"
        negative_sources.extend(
            (hard_root, image_path) for image_path in sorted(image_dir.glob("*.jpg"))
        )
    negative_train = [image_path for _, image_path in negative_sources]
    if not base_train:
        raise ValueError("base dataset has no training images")
    if not negative_train:
        raise ValueError("hard-negative dataset has no training images")

    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("OpenCV is required to build hard-negative variants") from exc

    augmented_images = out_dir / "hard_negative_augmented" / "images" / "train"
    augmented_labels = out_dir / "hard_negative_augmented" / "labels" / "train"
    augmented_images.mkdir(parents=True, exist_ok=True)
    augmented_labels.mkdir(parents=True, exist_ok=True)
    augmented: list[Path] = []
    for index, (hard_root, source) in enumerate(negative_sources):
        image = cv2.imread(str(source))
        if image is None:
            raise ValueError(f"cannot read hard-negative image: {source}")
        variants = (
            ("flip", cv2.flip(image, 1)),
            (
                "exposure",
                cv2.convertScaleAbs(
                    image,
                    alpha=0.82 if index % 2 == 0 else 1.18,
                    beta=-4 if index % 2 == 0 else 4,
                ),
            ),
        )
        for suffix, variant in variants:
            destination = augmented_images / f"{hard_root.name}_{source.stem}_{suffix}.jpg"
            if not cv2.imwrite(str(destination), variant, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise OSError(f"cannot write augmented hard-negative image: {destination}")
            (augmented_labels / f"{destination.stem}.txt").write_text("", encoding="utf-8")
            augmented.append(destination)

    train_images = base_train + negative_train + augmented
    out_dir.mkdir(parents=True, exist_ok=True)
    train_list = out_dir / "train.txt"
    train_list.write_text(
        "".join(f"{path.resolve().as_posix()}\n" for path in train_images),
        encoding="utf-8",
    )
    yaml_path = out_dir / "data.yaml"
    yaml_path.write_text(
        "\n".join(
            (
                f'train: "{train_list.as_posix()}"',
                f'val: "{(base_root / "images" / "val").as_posix()}"',
                f'test: "{(base_root / "images" / "test").as_posix()}"',
                "names:",
                "  0: Fire",
                "  1: smoke",
                "",
            )
        ),
        encoding="utf-8",
    )
    print(
        f"base_train={len(base_train)} hard_negative_train={len(negative_train)} "
        f"augmented={len(augmented)} combined_train={len(train_images)} yaml={yaml_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
