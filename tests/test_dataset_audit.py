from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from multidetect.dataset_audit import audit_yolo_dataset, audit_yolo_zip, source_stem


def _write_pair(
    root: Path,
    split: str,
    name: str,
    *,
    image_bytes: bytes,
    label: str,
) -> None:
    image_dir = root / "images" / split
    label_dir = root / "labels" / split
    image_dir.mkdir(parents=True, exist_ok=True)
    label_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / f"{name}.jpg").write_bytes(image_bytes)
    (label_dir / f"{name}.txt").write_text(label, encoding="utf-8")


def test_audit_reports_backgrounds_and_cross_split_source_leakage(tmp_path: Path) -> None:
    _write_pair(
        tmp_path,
        "train",
        "scene_jpg.rf.aaaaaaaa",
        image_bytes=b"train-variant",
        label="0 0.5 0.5 0.2 0.2\n",
    )
    _write_pair(
        tmp_path,
        "val",
        "scene_jpg.rf.bbbbbbbb",
        image_bytes=b"val-variant",
        label="",
    )

    report = audit_yolo_dataset(tmp_path, class_count=2)

    assert report["totals"]["images"] == 2
    assert report["totals"]["background_images"] == 1
    assert report["duplicates"]["cross_split_exact_groups"] == 0
    assert report["duplicates"]["cross_split_source_stem_groups"] == 1
    assert report["clean"] is False


def test_audit_rejects_invalid_labels_without_counting_the_box(tmp_path: Path) -> None:
    _write_pair(
        tmp_path,
        "train",
        "bad",
        image_bytes=b"fake-image",
        label="2 0.5 0.5 0.2 0.2\n0 0.99 0.5 0.2 0.2\n",
    )

    report = audit_yolo_dataset(tmp_path, class_count=2, hash_images=False)

    assert report["totals"]["boxes"] == 0
    assert report["totals"]["invalid_labels"] == 2
    assert report["clean"] is False


def test_source_stem_strips_roboflow_variant_suffix() -> None:
    path = Path("camera_jpg.rf.0123456789abcdef.jpg")

    assert source_stem(path) == "camera"


def test_zip_audit_reads_labels_without_extracting_images(tmp_path: Path) -> None:
    archive = tmp_path / "dataset.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("train/images/fire.jpg", b"not-decoded")
        bundle.writestr("train/labels/fire.txt", "0 0.5 0.5 0.2 0.2\n")
        bundle.writestr("test/images/clear.jpg", b"not-decoded")
        bundle.writestr("test/labels/clear.txt", "")

    report = audit_yolo_zip(archive, class_count=2)

    assert report["totals"] == {
        "images": 2,
        "background_images": 1,
        "boxes": 1,
        "missing_labels": 0,
        "orphan_labels": 0,
        "invalid_labels": 0,
    }
    assert report["clean"] is True
