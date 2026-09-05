from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox
from multidetect.target_image_anchor import target_image_anchor


def test_ground_object_uses_lower_contact_anchor_and_semantic_sigma() -> None:
    anchor = target_image_anchor(
        label="Car",
        bbox=BoundingBox(0.20, 0.30, 0.60, 0.80),
        image_width_px=1280,
        image_height_px=960,
        minimum_sigma_px=2.0,
    )

    assert anchor.kind == "ground_contact"
    assert anchor.label == "car"
    assert anchor.x == pytest.approx(0.40)
    assert anchor.y == pytest.approx(0.785)
    assert anchor.vertical_fraction == pytest.approx(0.97)
    assert anchor.sigma_px == pytest.approx(14.4)

    observation = anchor.observation(
        target_id="vehicle-4",
        frame_id="frame-17",
        captured_at_s=8.5,
    )
    assert observation.center_x == anchor.x
    assert observation.center_y == anchor.y
    assert observation.center_sigma_px == anchor.sigma_px


@pytest.mark.parametrize("label", ["fire", "smoke", "manual", "unknown"])
def test_volumetric_or_ambiguous_target_keeps_bbox_center(label: str) -> None:
    anchor = target_image_anchor(
        label=label,
        bbox=BoundingBox(0.10, 0.20, 0.50, 0.70),
        image_width_px=640,
        image_height_px=480,
        minimum_sigma_px=2.5,
    )

    assert anchor.kind == "bbox_center"
    assert anchor.x == pytest.approx(0.30)
    assert anchor.y == pytest.approx(0.45)
    assert anchor.sigma_px == pytest.approx(2.5)
    assert anchor.vertical_fraction == pytest.approx(0.5)


def test_small_ground_box_retains_configured_minimum_sigma() -> None:
    anchor = target_image_anchor(
        label="person",
        bbox=BoundingBox(0.48, 0.48, 0.52, 0.52),
        image_width_px=640,
        image_height_px=480,
        minimum_sigma_px=3.0,
    )

    assert anchor.kind == "ground_contact"
    assert anchor.sigma_px == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"image_width_px": 0}, "image_width_px"),
        ({"minimum_sigma_px": 0.0}, "minimum_sigma_px"),
        ({"ground_contact_vertical_fraction": 1.0}, "vertical fraction"),
        ({"ground_contact_sigma_fraction": 0.0}, "sigma fraction"),
    ],
)
def test_anchor_policy_rejects_invalid_geometry(
    changes: dict[str, float | int],
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "label": "car",
        "bbox": BoundingBox(0.1, 0.2, 0.4, 0.6),
        "image_width_px": 640,
        "image_height_px": 480,
        "minimum_sigma_px": 2.0,
    }
    kwargs.update(changes)
    with pytest.raises(ValueError, match=message):
        target_image_anchor(**kwargs)  # type: ignore[arg-type]
