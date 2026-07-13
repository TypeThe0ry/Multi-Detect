from __future__ import annotations

import json

from multidetect.domain import BoundingBox
from multidetect.operator_link import VideoGeometry
from multidetect.video_viewport import PixelRect, VideoScaleMode, VideoViewportTransform


def main() -> int:
    geometry = VideoGeometry("camera-main", 1280, 720)
    fit = VideoViewportTransform(
        geometry,
        display_width=1920,
        display_height=1200,
        scale_mode=VideoScaleMode.FIT,
    )
    source_box = BoundingBox(0.32, 0.21, 0.61, 0.72)
    display_box = fit.source_bbox_to_display(source_box)
    restored = fit.display_bbox_to_source(display_box)
    black_bar = fit.display_bbox_to_source(PixelRect(100.0, 10.0, 200.0, 50.0))
    rotation_errors: dict[str, float] = {}
    for rotation in (0, 90, 180, 270):
        transform = VideoViewportTransform(
            VideoGeometry("camera-main", 1280, 720, rotation_degrees=rotation),
            display_width=1000,
            display_height=1000,
            scale_mode=VideoScaleMode.FIT,
        )
        round_trip = transform.display_bbox_to_source(transform.source_bbox_to_display(source_box))
        if round_trip.source_bbox is None:
            raise RuntimeError("viewport round trip unexpectedly failed")
        rotation_errors[str(rotation)] = max(
            abs(before - after)
            for before, after in zip(
                (source_box.x1, source_box.y1, source_box.x2, source_box.y2),
                (
                    round_trip.source_bbox.x1,
                    round_trip.source_bbox.y1,
                    round_trip.source_bbox.x2,
                    round_trip.source_bbox.y2,
                ),
                strict=True,
            )
        )
    print(
        json.dumps(
            {
                "event": "g20_viewport_mapping_demo_finished",
                "source_geometry": {
                    "width": geometry.width,
                    "height": geometry.height,
                },
                "display_geometry": {"width": 1920, "height": 1200},
                "fit_video_rect": [
                    fit.video_rect.x1,
                    fit.video_rect.y1,
                    fit.video_rect.x2,
                    fit.video_rect.y2,
                ],
                "source_bbox": source_box.rounded(),
                "display_bbox": [
                    display_box.x1,
                    display_box.y1,
                    display_box.x2,
                    display_box.y2,
                ],
                "round_trip_allowed": restored.allowed,
                "black_bar_selection_rejected": not black_bar.allowed,
                "maximum_round_trip_error_by_rotation": rotation_errors,
                "wire_bbox_is_source_normalized": True,
                "selection_is_payload_authorization": False,
                "flight_control_enabled": False,
                "physical_release_enabled": False,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
