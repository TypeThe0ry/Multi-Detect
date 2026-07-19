from __future__ import annotations

import cv2
import numpy as np

from multidetect.aircraft_appearance import HandcraftedAircraftAppearanceEncoder
from multidetect.domain import BoundingBox, Detection


def _aircraft_scene(
    *,
    scale: float = 1.0,
    brightness: float = 1.0,
    style: str = "winged",
) -> tuple[np.ndarray, BoundingBox]:
    image = np.full((180, 240, 3), (190, 210, 232), dtype=np.uint8)
    center_x, center_y = 120, 88
    if style == "winged":
        points = np.asarray(
            [
                (center_x - 48 * scale, center_y + 7 * scale),
                (center_x - 11 * scale, center_y + 3 * scale),
                (center_x + 38 * scale, center_y - 4 * scale),
                (center_x + 48 * scale, center_y),
                (center_x + 38 * scale, center_y + 4 * scale),
                (center_x - 11 * scale, center_y + 10 * scale),
                (center_x - 31 * scale, center_y + 33 * scale),
                (center_x - 23 * scale, center_y + 7 * scale),
                (center_x - 48 * scale, center_y + 7 * scale),
            ],
            dtype=np.int32,
        )
        color = (38, 48, 68)
    else:
        points = np.asarray(
            [
                (center_x - 29 * scale, center_y - 27 * scale),
                (center_x + 29 * scale, center_y - 27 * scale),
                (center_x + 29 * scale, center_y + 27 * scale),
                (center_x - 29 * scale, center_y + 27 * scale),
            ],
            dtype=np.int32,
        )
        color = (30, 110, 160)
    cv2.fillPoly(image, [points], color)
    if brightness != 1.0:
        image = np.clip(image.astype(np.float32) * brightness, 0, 255).astype(np.uint8)
    return image, BoundingBox(0.22, 0.22, 0.78, 0.78)


def test_aircraft_appearance_is_stable_for_scale_and_brightness() -> None:
    encoder = HandcraftedAircraftAppearanceEncoder()
    first_image, bbox = _aircraft_scene()
    second_image, _ = _aircraft_scene(scale=0.78, brightness=1.13)
    first = encoder.encode_detections(first_image, (Detection("airplane", 0.9, bbox),))[0]
    second = encoder.encode_detections(second_image, (Detection("plane", 0.9, bbox),))[0]

    assert first.appearance is not None
    assert second.appearance is not None
    assert first.appearance.cosine_distance(second.appearance) < 0.25


def test_aircraft_appearance_separates_different_shape_and_keeps_domains_disjoint() -> None:
    encoder = HandcraftedAircraftAppearanceEncoder()
    source_image, bbox = _aircraft_scene()
    distractor_image, _ = _aircraft_scene(style="rectangle")
    source = encoder.encode_detections(source_image, (Detection("airplane", 0.9, bbox),))[0]
    distractor = encoder.encode_detections(distractor_image, (Detection("aircraft", 0.9, bbox),))[0]
    non_aircraft = encoder.encode_detections(
        source_image,
        (Detection("person", 0.9, bbox),),
    )[0]

    assert source.appearance is not None
    assert distractor.appearance is not None
    assert source.appearance.cosine_distance(distractor.appearance) > 0.15
    assert non_aircraft.appearance is None
    assert non_aircraft.appearance_reliable is False


def test_aircraft_appearance_rejects_tiny_unreliable_crop() -> None:
    encoder = HandcraftedAircraftAppearanceEncoder()
    image, _ = _aircraft_scene()
    result = encoder.encode_detections(
        image,
        (Detection("airplane", 0.9, BoundingBox(0.01, 0.01, 0.02, 0.02)),),
    )[0]

    assert result.appearance is None
    assert result.appearance_reliable is False
