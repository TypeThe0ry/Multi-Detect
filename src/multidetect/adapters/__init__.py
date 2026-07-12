"""Adapters for legacy perception model outputs."""

from .fire_smoke_legacy import (
    FireSmokeLegacyAdapter,
    adapt_darknet_detection,
    adapt_darknet_detections,
    adapt_yolov5_detections,
)

__all__ = [
    "FireSmokeLegacyAdapter",
    "adapt_darknet_detection",
    "adapt_darknet_detections",
    "adapt_yolov5_detections",
]
