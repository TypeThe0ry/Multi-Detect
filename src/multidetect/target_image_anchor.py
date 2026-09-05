"""Semantic image anchors for terrain-backed target geolocation.

An object detector reports a region, not a surveyed point.  Using the centre
of every bounding box is appropriate for a bearing-only display, but it is a
systematic ranging error for an object that stands on the terrain: the ray
must originate from an image point near the object's ground contact.  This
module keeps that semantic choice explicit and adds an uncertainty term for
the detector-to-contact-point approximation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import BoundingBox
from .multimodal_ranging import TargetImageObservation

GROUND_CONTACT_LABELS = frozenset(
    {
        "animal",
        "bear",
        "bicycle",
        "building",
        "bus",
        "car",
        "cat",
        "cow",
        "dog",
        "giraffe",
        "horse",
        "motorcycle",
        "pedestrian",
        "people",
        "person",
        "person_sitting",
        "sheep",
        "structure",
        "train",
        "truck",
        "van",
        "vehicle",
        "zebra",
    }
)


@dataclass(frozen=True, slots=True)
class TargetImageAnchor:
    """One traceable image point used by calibrated range/geolocation math."""

    kind: str
    x: float
    y: float
    sigma_px: float
    label: str
    vertical_fraction: float

    def __post_init__(self) -> None:
        if self.kind not in {"bbox_center", "ground_contact"}:
            raise ValueError("target image anchor kind is unsupported")
        if not self.label:
            raise ValueError("target image anchor label cannot be empty")
        if not all(
            math.isfinite(value)
            for value in (self.x, self.y, self.sigma_px, self.vertical_fraction)
        ):
            raise ValueError("target image anchor values must be finite")
        if not 0.0 <= self.x <= 1.0 or not 0.0 <= self.y <= 1.0:
            raise ValueError("target image anchor must be normalized")
        if self.sigma_px <= 0.0:
            raise ValueError("target image anchor uncertainty must be positive")
        if not 0.0 <= self.vertical_fraction <= 1.0:
            raise ValueError("target image anchor vertical fraction must be normalized")

    def observation(
        self,
        *,
        target_id: str,
        frame_id: str,
        captured_at_s: float,
    ) -> TargetImageObservation:
        return TargetImageObservation(
            target_id=target_id,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            center_x=self.x,
            center_y=self.y,
            center_sigma_px=self.sigma_px,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "label": self.label,
            "normalized_x": self.x,
            "normalized_y": self.y,
            "vertical_fraction": self.vertical_fraction,
            "sigma_px": self.sigma_px,
        }


def target_image_anchor(
    *,
    label: str,
    bbox: BoundingBox,
    image_width_px: int,
    image_height_px: int,
    minimum_sigma_px: float,
    ground_contact_vertical_fraction: float = 0.97,
    ground_contact_sigma_fraction: float = 0.03,
) -> TargetImageAnchor:
    """Resolve a detector box to a range-bearing image point.

    Known terrain-supported objects use the horizontal box centre and a point
    close to the lower edge.  The small inward offset prevents a one-sigma
    perturbation from immediately leaving the image when a detector box
    touches its bottom boundary.  The matching fraction of box height becomes
    an additional semantic uncertainty rather than pretending the contact
    point is pixel-perfect.  Volumetric or ambiguous labels (for example fire,
    smoke, or a manual region) retain their geometric centre.
    """

    if not isinstance(bbox, BoundingBox):
        raise TypeError("bbox must be BoundingBox")
    normalized_label = label.strip().lower()
    if not normalized_label:
        raise ValueError("target image anchor label cannot be empty")
    for value, name in (
        (image_width_px, "image_width_px"),
        (image_height_px, "image_height_px"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    numeric = (
        minimum_sigma_px,
        ground_contact_vertical_fraction,
        ground_contact_sigma_fraction,
    )
    if not all(math.isfinite(value) for value in numeric):
        raise ValueError("target image anchor policy values must be finite")
    if minimum_sigma_px <= 0.0:
        raise ValueError("minimum_sigma_px must be positive")
    if not 0.5 < ground_contact_vertical_fraction < 1.0:
        raise ValueError("ground-contact vertical fraction must be in (0.5, 1.0)")
    if not 0.0 < ground_contact_sigma_fraction <= 0.25:
        raise ValueError("ground-contact sigma fraction must be in (0, 0.25]")

    center_x, center_y = bbox.center
    if normalized_label not in GROUND_CONTACT_LABELS:
        return TargetImageAnchor(
            kind="bbox_center",
            x=center_x,
            y=center_y,
            sigma_px=float(minimum_sigma_px),
            label=normalized_label,
            vertical_fraction=0.5,
        )

    box_height = bbox.y2 - bbox.y1
    anchor_y = bbox.y1 + ground_contact_vertical_fraction * box_height
    semantic_sigma_px = ground_contact_sigma_fraction * box_height * image_height_px
    return TargetImageAnchor(
        kind="ground_contact",
        x=center_x,
        y=anchor_y,
        sigma_px=max(float(minimum_sigma_px), semantic_sigma_px),
        label=normalized_label,
        vertical_fraction=ground_contact_vertical_fraction,
    )


__all__ = [
    "GROUND_CONTACT_LABELS",
    "TargetImageAnchor",
    "target_image_anchor",
]
