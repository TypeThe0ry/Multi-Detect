from __future__ import annotations

import math
from dataclasses import dataclass

from .compat import StrEnum
from .domain import BoundingBox
from .operator_link import VideoGeometry


class VideoScaleMode(StrEnum):
    FIT = "fit"
    CROP = "crop"


@dataclass(frozen=True, slots=True)
class PixelRect:
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        values = (self.x1, self.y1, self.x2, self.y2)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("pixel rectangle coordinates must be finite")
        if self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError("pixel rectangle must have positive area")

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1


@dataclass(frozen=True, slots=True)
class ViewportSelection:
    allowed: bool
    source_bbox: BoundingBox | None
    reasons: tuple[str, ...]


class VideoViewportTransform:
    """Maps G20 display pixels to source-normalized boxes and back.

    Rotation is clockwise and follows ``VideoGeometry.rotation_degrees``. ``FIT``
    rejects selections crossing letterbox/pillarbox regions; ``CROP`` maps only
    the visible center-cropped source region.
    """

    def __init__(
        self,
        geometry: VideoGeometry,
        *,
        display_width: int,
        display_height: int,
        scale_mode: VideoScaleMode = VideoScaleMode.FIT,
    ) -> None:
        if isinstance(display_width, bool) or not isinstance(display_width, int):
            raise ValueError("display width must be a positive integer")
        if isinstance(display_height, bool) or not isinstance(display_height, int):
            raise ValueError("display height must be a positive integer")
        if display_width <= 0 or display_height <= 0:
            raise ValueError("display dimensions must be positive")
        if not isinstance(scale_mode, VideoScaleMode):
            raise ValueError("video scale mode must be FIT or CROP")
        self.geometry = geometry
        self.display_width = display_width
        self.display_height = display_height
        self.scale_mode = scale_mode
        if geometry.rotation_degrees in {90, 270}:
            self.oriented_width = geometry.height
            self.oriented_height = geometry.width
        else:
            self.oriented_width = geometry.width
            self.oriented_height = geometry.height
        width_scale = display_width / self.oriented_width
        height_scale = display_height / self.oriented_height
        self.scale = (
            min(width_scale, height_scale)
            if scale_mode is VideoScaleMode.FIT
            else max(width_scale, height_scale)
        )
        rendered_width = self.oriented_width * self.scale
        rendered_height = self.oriented_height * self.scale
        self.video_rect = PixelRect(
            (display_width - rendered_width) / 2.0,
            (display_height - rendered_height) / 2.0,
            (display_width + rendered_width) / 2.0,
            (display_height + rendered_height) / 2.0,
        )

    def display_bbox_to_source(self, rect: PixelRect) -> ViewportSelection:
        display_bounds = PixelRect(0.0, 0.0, float(self.display_width), float(self.display_height))
        reasons: list[str] = []
        if not _contains(display_bounds, rect):
            reasons.append("selection extends outside the display surface")
        if self.scale_mode is VideoScaleMode.FIT and not _contains(self.video_rect, rect):
            reasons.append("selection intersects a letterbox or pillarbox region")
        if reasons:
            return ViewportSelection(False, None, tuple(reasons))
        source_points = tuple(
            self._display_point_to_source(x, y)
            for x, y in (
                (rect.x1, rect.y1),
                (rect.x2, rect.y1),
                (rect.x2, rect.y2),
                (rect.x1, rect.y2),
            )
        )
        x_values = [point[0] for point in source_points]
        y_values = [point[1] for point in source_points]
        source_bbox = BoundingBox(
            _clamp01(min(x_values)),
            _clamp01(min(y_values)),
            _clamp01(max(x_values)),
            _clamp01(max(y_values)),
        )
        return ViewportSelection(True, source_bbox, ())

    def source_bbox_to_display(self, bbox: BoundingBox) -> PixelRect:
        display_points = tuple(
            self._source_point_to_display(x, y)
            for x, y in (
                (bbox.x1, bbox.y1),
                (bbox.x2, bbox.y1),
                (bbox.x2, bbox.y2),
                (bbox.x1, bbox.y2),
            )
        )
        x_values = [point[0] for point in display_points]
        y_values = [point[1] for point in display_points]
        return PixelRect(min(x_values), min(y_values), max(x_values), max(y_values))

    def _display_point_to_source(self, x: float, y: float) -> tuple[float, float]:
        oriented_x = (x - self.video_rect.x1) / self.video_rect.width
        oriented_y = (y - self.video_rect.y1) / self.video_rect.height
        rotation = self.geometry.rotation_degrees
        if rotation == 0:
            return oriented_x, oriented_y
        if rotation == 90:
            return oriented_y, 1.0 - oriented_x
        if rotation == 180:
            return 1.0 - oriented_x, 1.0 - oriented_y
        return 1.0 - oriented_y, oriented_x

    def _source_point_to_display(self, x: float, y: float) -> tuple[float, float]:
        rotation = self.geometry.rotation_degrees
        if rotation == 0:
            oriented_x, oriented_y = x, y
        elif rotation == 90:
            oriented_x, oriented_y = 1.0 - y, x
        elif rotation == 180:
            oriented_x, oriented_y = 1.0 - x, 1.0 - y
        else:
            oriented_x, oriented_y = y, 1.0 - x
        return (
            self.video_rect.x1 + oriented_x * self.video_rect.width,
            self.video_rect.y1 + oriented_y * self.video_rect.height,
        )


def _contains(outer: PixelRect, inner: PixelRect, *, tolerance: float = 1e-9) -> bool:
    return (
        inner.x1 >= outer.x1 - tolerance
        and inner.y1 >= outer.y1 - tolerance
        and inner.x2 <= outer.x2 + tolerance
        and inner.y2 <= outer.y2 + tolerance
    )


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


__all__ = [
    "PixelRect",
    "VideoScaleMode",
    "VideoViewportTransform",
    "ViewportSelection",
]
