from __future__ import annotations

from collections.abc import Sequence

from .domain import Detection, SensorKind

_FIRE_LIKE_LABELS = frozenset({"flame", "hotspot", "smoldering_area"})


def _labels_are_compatible(rgb_label: str, thermal_label: str) -> bool:
    """Return whether two detections may describe the same fire-related object."""

    return rgb_label == thermal_label or {
        rgb_label,
        thermal_label,
    }.issubset(_FIRE_LIKE_LABELS)


def fuse_rgb_thermal(
    rgb_detections: Sequence[Detection],
    thermal_detections: Sequence[Detection],
    *,
    iou_threshold: float = 0.3,
) -> tuple[Detection, ...]:
    """Annotate RGB detections with one-to-one thermal IoU corroboration.

    The RGB detection remains the spatial and confidence authority. A matching
    thermal detection changes the sensor kind to ``FUSED`` and adds explicit
    corroboration evidence to metadata; it does not inflate model confidence.
    Thermal-only detections are intentionally not emitted, which prevents one
    modality from silently changing the mission's target population.
    """

    if not 0.0 < iou_threshold <= 1.0:
        raise ValueError("iou_threshold must be in (0, 1]")

    candidates: list[tuple[float, int, int]] = []
    for rgb_index, rgb in enumerate(rgb_detections):
        for thermal_index, thermal in enumerate(thermal_detections):
            if not _labels_are_compatible(rgb.label, thermal.label):
                continue
            overlap = rgb.bbox.iou(thermal.bbox)
            if overlap >= iou_threshold:
                candidates.append((overlap, rgb_index, thermal_index))

    # Global greedy matching avoids one large thermal box corroborating several
    # RGB targets in the same frame.
    matches: dict[int, tuple[int, float]] = {}
    used_thermal: set[int] = set()
    for overlap, rgb_index, thermal_index in sorted(
        candidates,
        key=lambda item: (-item[0], item[1], item[2]),
    ):
        if rgb_index in matches or thermal_index in used_thermal:
            continue
        matches[rgb_index] = (thermal_index, overlap)
        used_thermal.add(thermal_index)

    fused: list[Detection] = []
    for rgb_index, rgb in enumerate(rgb_detections):
        metadata = dict(rgb.metadata)
        match = matches.get(rgb_index)
        if match is None:
            metadata["thermal_corroborated"] = False
            fused.append(
                Detection(
                    label=rgb.label,
                    confidence=rgb.confidence,
                    bbox=rgb.bbox,
                    sensor=rgb.sensor,
                    model_version=rgb.model_version,
                    metadata=metadata,
                )
            )
            continue

        thermal_index, overlap = match
        thermal = thermal_detections[thermal_index]
        metadata.update(
            {
                "thermal_corroborated": True,
                "thermal_iou": overlap,
                "thermal_confidence": thermal.confidence,
                "thermal_label": thermal.label,
                "thermal_model_version": thermal.model_version,
            }
        )
        fused.append(
            Detection(
                label=rgb.label,
                confidence=rgb.confidence,
                bbox=rgb.bbox,
                sensor=SensorKind.FUSED,
                model_version=rgb.model_version,
                metadata=metadata,
            )
        )

    return tuple(fused)
