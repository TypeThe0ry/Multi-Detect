"""Visible object bounds from lossless Gazebo panoptic labels maps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


def load_labels_map(path: str | Path) -> np.ndarray:
    """Read RGB bytes unchanged: R/G encode instance, B encodes semantic label."""
    with Image.open(path) as opened:
        labels = np.asarray(opened)
    if labels.dtype != np.uint8 or labels.ndim != 3 or labels.shape[2] != 3:
        raise ValueError("panoptic labels map must be an RGB uint8 PNG")
    return labels


def boxes_from_segmentation(
    labels: np.ndarray,
    truth: dict[str, Any],
    *,
    min_area_px: int = 4,
    min_box_px: float = 4.0,
) -> dict[str, dict[str, Any]]:
    """Union visible pixels by unique object label, retaining instance ids.

    Multiple visible components from occlusion belong to one object. Bounds
    are half-open xyxy, matching detector labels and image crops.
    """
    if labels.dtype != np.uint8 or labels.ndim != 3 or labels.shape[2] != 3:
        raise ValueError("panoptic labels map must be RGB uint8")
    if min_area_px < 1 or min_box_px < 0:
        raise ValueError("segmentation thresholds must be non-negative (area at least 1)")
    result = {}
    mapping = truth.get("segmentation_labels")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("truth has no segmentation_labels mapping")
    for numeric_label, entry in mapping.items():
        if not isinstance(entry, dict) or not entry.get("target_id"):
            raise ValueError("segmentation_labels must map label ids to object records")
        label = int(numeric_label)
        if not 1 <= label <= 254:
            raise ValueError("panoptic semantic labels must lie in [1, 254]")
        mask = labels[..., 2] == label
        count, components, stats, _ = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8), connectivity=8
        )
        accepted = [
            index for index in range(1, count) if stats[index, cv2.CC_STAT_AREA] >= min_area_px
        ]
        mask = np.isin(components, accepted)
        ys, xs = np.nonzero(mask)
        if len(xs) < min_area_px:
            continue
        box = (float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1))
        if box[2] - box[0] < min_box_px or box[3] - box[1] < min_box_px:
            continue
        ids = labels[..., 0].astype(np.uint16) + (labels[..., 1].astype(np.uint16) << 8)
        result[str(entry["target_id"])] = {
            "box": box,
            "visible_pixels": len(xs),
            "segmentation_label": label,
            "instance_ids": sorted(int(value) for value in np.unique(ids[mask])),
        }
    return result
