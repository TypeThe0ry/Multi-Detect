from __future__ import annotations

import pytest

from multidetect.domain import BoundingBox
from multidetect.operator_link import VideoGeometry
from multidetect.video_viewport import PixelRect, VideoScaleMode, VideoViewportTransform


def test_fit_maps_display_box_to_source_and_rejects_black_bars() -> None:
    transform = VideoViewportTransform(
        VideoGeometry("camera-main", 1280, 720),
        display_width=1920,
        display_height=1200,
        scale_mode=VideoScaleMode.FIT,
    )

    assert transform.video_rect == PixelRect(0.0, 60.0, 1920.0, 1140.0)
    selection = transform.display_bbox_to_source(PixelRect(480.0, 330.0, 1440.0, 870.0))
    assert selection.allowed is True
    assert selection.source_bbox == BoundingBox(0.25, 0.25, 0.75, 0.75)

    black_bar = transform.display_bbox_to_source(PixelRect(100.0, 10.0, 200.0, 50.0))
    assert black_bar.allowed is False
    assert black_bar.source_bbox is None
    assert any("letterbox" in reason for reason in black_bar.reasons)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("scale_mode", [VideoScaleMode.FIT, VideoScaleMode.CROP])
def test_source_display_round_trip_for_every_rotation_and_scale_mode(
    rotation: int,
    scale_mode: VideoScaleMode,
) -> None:
    transform = VideoViewportTransform(
        VideoGeometry("camera-main", 1280, 720, rotation_degrees=rotation),
        display_width=1000,
        display_height=1000,
        scale_mode=scale_mode,
    )
    source = BoundingBox(0.4, 0.4, 0.6, 0.6)

    display = transform.source_bbox_to_display(source)
    restored = transform.display_bbox_to_source(display)

    assert restored.allowed is True
    assert restored.source_bbox is not None
    assert restored.source_bbox.x1 == pytest.approx(source.x1)
    assert restored.source_bbox.y1 == pytest.approx(source.y1)
    assert restored.source_bbox.x2 == pytest.approx(source.x2)
    assert restored.source_bbox.y2 == pytest.approx(source.y2)


def test_crop_maps_full_display_to_visible_center_region() -> None:
    transform = VideoViewportTransform(
        VideoGeometry("camera-main", 1920, 1080),
        display_width=1000,
        display_height=1000,
        scale_mode=VideoScaleMode.CROP,
    )

    selection = transform.display_bbox_to_source(PixelRect(0.0, 0.0, 1000.0, 1000.0))

    assert selection.allowed is True
    assert selection.source_bbox is not None
    assert selection.source_bbox.x1 == pytest.approx(0.21875)
    assert selection.source_bbox.x2 == pytest.approx(0.78125)
    assert selection.source_bbox.y1 == pytest.approx(0.0)
    assert selection.source_bbox.y2 == pytest.approx(1.0)


def test_selection_outside_display_is_rejected_in_both_modes() -> None:
    for mode in VideoScaleMode:
        transform = VideoViewportTransform(
            VideoGeometry("camera-main", 1280, 720),
            display_width=1280,
            display_height=720,
            scale_mode=mode,
        )
        result = transform.display_bbox_to_source(PixelRect(-1.0, 10.0, 100.0, 100.0))
        assert result.allowed is False
        assert any("outside" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "changes",
    [
        {"display_width": 0},
        {"display_height": -1},
        {"display_width": True},
        {"scale_mode": "fit"},
    ],
)
def test_viewport_rejects_invalid_configuration(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "display_width": 1280,
        "display_height": 720,
        "scale_mode": VideoScaleMode.FIT,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        VideoViewportTransform(VideoGeometry("camera-main", 1280, 720), **values)
