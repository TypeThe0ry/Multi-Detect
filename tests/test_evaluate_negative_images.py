from __future__ import annotations

from pathlib import Path

import pytest

from scripts.evaluate_negative_images import select_negative_images


def test_select_negative_images_uses_empty_yolo_labels(tmp_path: Path) -> None:
    images = tmp_path / "images"
    labels = tmp_path / "labels"
    images.mkdir()
    labels.mkdir()
    for name in ("background.jpg", "fire.png"):
        (images / name).write_bytes(b"image")
    (labels / "background.txt").write_text("\n", encoding="utf-8")
    (labels / "fire.txt").write_text("0 0.5 0.5 0.2 0.2\n", encoding="utf-8")

    assert select_negative_images(images, labels) == [images / "background.jpg"]


def test_select_negative_images_rejects_missing_labels(tmp_path: Path) -> None:
    images = tmp_path / "images"
    labels = tmp_path / "labels"
    images.mkdir()
    labels.mkdir()
    (images / "unknown.jpg").write_bytes(b"image")

    with pytest.raises(ValueError, match="missing YOLO label"):
        select_negative_images(images, labels)
