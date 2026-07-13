from __future__ import annotations

import json
from pathlib import Path

from multidetect.dataset_plan import build_dataset_plan


def _pair(root: Path, split: str, name: str) -> None:
    image_dir = root / split / "images"
    label_dir = root / split / "labels"
    image_dir.mkdir(parents=True, exist_ok=True)
    label_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / f"{name}.jpg").write_bytes(b"image")
    (label_dir / f"{name}.txt").write_text("", encoding="utf-8")


def test_multisource_plan_drops_train_variant_when_same_source_is_in_val(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _pair(source, "train", "scene_jpg.rf.aaaaaaaa")
    _pair(source, "val", "scene_jpg.rf.bbbbbbbb")
    _pair(source, "test", "test-only")
    hardneg = tmp_path / "hardneg"
    _pair(hardneg, "train", "negative")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "class_names": ["Fire", "smoke"],
                "sources": [
                    {
                        "id": "base",
                        "root": str(source),
                        "splits": [
                            {"role": "train", "images": "train/images", "labels": "train/labels"},
                            {"role": "val", "images": "val/images", "labels": "val/labels"},
                            {"role": "test", "images": "test/images", "labels": "test/labels"},
                        ],
                    },
                    {
                        "id": "negative",
                        "root": str(hardneg),
                        "splits": [
                            {"role": "train", "images": "train/images", "labels": "train/labels"}
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dataset_plan(plan, tmp_path / "out")

    assert report["output_role_counts"] == {"test": 1, "train": 1, "val": 1}
    assert report["cross_role_source_variants_dropped"] == 1
    train_lines = (tmp_path / "out/train.txt").read_text(encoding="utf-8").splitlines()
    assert len(train_lines) == 1
    assert train_lines[0].endswith("negative.jpg")
    assert (tmp_path / "out/data.yaml").is_file()


def test_multisource_plan_writes_external_evaluation_yaml(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for split in ("train", "val", "test", "external"):
        _pair(source, split, split)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "class_names": ["Fire", "smoke"],
                "sources": [
                    {
                        "id": "base",
                        "root": str(source),
                        "splits": [
                            {"role": "train", "images": "train/images", "labels": "train/labels"},
                            {"role": "val", "images": "val/images", "labels": "val/labels"},
                            {"role": "test", "images": "test/images", "labels": "test/labels"},
                            {
                                "role": "external_public",
                                "images": "external/images",
                                "labels": "external/labels",
                            },
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dataset_plan(plan, tmp_path / "out")

    assert report["output_role_counts"]["external_public"] == 1
    assert (tmp_path / "out/external_public.yaml").is_file()


def test_multisource_plan_repeats_only_training_samples(tmp_path: Path) -> None:
    source = tmp_path / "source"
    for split in ("train", "val", "test"):
        _pair(source, split, split)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "class_names": ["Fire", "smoke"],
                "sources": [
                    {
                        "id": "weighted",
                        "root": str(source),
                        "train_repeat": 3,
                        "splits": [
                            {"role": "train", "images": "train/images", "labels": "train/labels"},
                            {"role": "val", "images": "val/images", "labels": "val/labels"},
                            {"role": "test", "images": "test/images", "labels": "test/labels"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_dataset_plan(plan, tmp_path / "out")

    assert report["output_role_counts"] == {"test": 1, "train": 3, "val": 1}
    assert report["output_unique_role_counts"] == {"test": 1, "train": 1, "val": 1}
    assert len((tmp_path / "out/train.txt").read_text().splitlines()) == 3
