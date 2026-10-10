from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ...vision import (
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxRawYoloConfig,
    OnnxRawYoloDetector,
)

COCO80_CLASS_NAMES = tuple(
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,"
    "stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,"
    "backpack,umbrella,handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,"
    "baseball bat,baseball glove,skateboard,surfboard,tennis racket,bottle,wine glass,cup,"
    "fork,knife,spoon,bowl,banana,apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,"
    "cake,chair,couch,potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,"
    "cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,scissors,teddy bear,"
    "hair drier,toothbrush".split(",")
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _camera_value(info: Mapping[str, Any] | None, *keys: str) -> float | None:
    if not isinstance(info, Mapping):
        return None
    for key in keys:
        value = info.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value) and value > 0.0:
                return value
    return None


def _camera_intrinsics(
    info: Mapping[str, Any] | None, width: int, height: int
) -> tuple[float, float, float, float]:
    fx = _camera_value(info, "fx", "fx_px") or max(1.0, float(width))
    fy = _camera_value(info, "fy", "fy_px") or max(1.0, float(height))
    cx = _camera_value(info, "cx", "cx_px")
    cy = _camera_value(info, "cy", "cy_px")
    return (
        fx,
        fy,
        float(width) / 2.0 if cx is None else cx,
        float(height) / 2.0 if cy is None else cy,
    )


def wrap_degrees(value: float) -> float:
    return (float(value) + 180.0) % 360.0 - 180.0


def bearing_from_pixel(
    x_px: float,
    *,
    camera_info: Mapping[str, Any] | None,
    image_width: int,
    image_height: int,
    attitude: Mapping[str, Any] | None = None,
) -> tuple[float, float]:
    """Return vehicle-relative and world heading bearings for one image pixel.

    The camera is assumed to be aligned with the vehicle forward axis, as in the
    simulator camera model.  ``bearing_deg`` is therefore the horizontal pixel
    ray angle relative to vehicle forward (the metric used by teacher labels),
    while the second value includes attitude yaw for diagnostics.
    """
    fx, fy, cx, cy = _camera_intrinsics(camera_info, image_width, image_height)
    # Camera optical axis is vehicle-forward (+x), image-right is +y and
    # image-down is +z in the simulator's NED convention.  Apply the complete
    # reported attitude for the world bearing while retaining a vehicle-frame
    # bearing for the policy input.
    ray_x, ray_y, ray_z = 1.0, (float(x_px) - cx) / fx, 0.0
    # A bbox center has no vertical coordinate in this helper; cy/fy is kept in
    # the signature for symmetry and callers may use ``bearing_from_pixel``
    # with horizontal-only measurements without inventing elevation.
    del fy, cy
    relative = math.degrees(math.atan2(ray_y, ray_x))
    roll = pitch = yaw = 0.0
    if isinstance(attitude, Mapping):
        for name in ("roll_deg", "pitch_deg", "yaw_deg"):
            raw = attitude.get(name, 0.0)
            if isinstance(raw, (int, float)) and math.isfinite(float(raw)):
                if name == "roll_deg":
                    roll = float(raw)
                elif name == "pitch_deg":
                    pitch = float(raw)
                else:
                    yaw = float(raw)
    rr, pp, yy = (math.radians(value) for value in (roll, pitch, yaw))
    # R = Rz(yaw) Ry(pitch) Rx(roll), applied to the camera ray.
    x1 = math.cos(pp) * ray_x + math.sin(pp) * ray_z
    y1 = (
        math.sin(rr) * math.sin(pp) * ray_x
        + math.cos(rr) * ray_y
        - math.sin(rr) * math.cos(pp) * ray_z
    )
    north = math.cos(yy) * x1 - math.sin(yy) * y1
    east = math.sin(yy) * x1 + math.cos(yy) * y1
    absolute = math.degrees(math.atan2(east, north))
    return wrap_degrees(relative), wrap_degrees(absolute)


def robust_depth_median(
    depth: Any,
    bbox_xyxy_px: Sequence[float],
    *,
    rgb_size: tuple[int, int] | None = None,
    depth_camera_info: Mapping[str, Any] | None = None,
) -> float | None:
    """Compute a robust valid-depth median for a detection box.

    RGB and depth streams can have different dimensions.  The box is mapped by
    normalized image coordinates, then finite positive values and the declared
    depth limits are retained.  Invalid Gazebo zeros, infinities and sentinels
    consequently cannot become a range measurement.
    """
    array = np.asarray(depth)
    if array.ndim != 2 or array.size == 0:
        return None
    h, w = (int(array.shape[0]), int(array.shape[1]))
    x1, y1, x2, y2 = (float(value) for value in bbox_xyxy_px[:4])
    if rgb_size is not None:
        rw, rh = rgb_size
        if rw > 0 and rh > 0:
            x1, x2 = x1 * w / rw, x2 * w / rw
            y1, y2 = y1 * h / rh, y2 * h / rh
    left = max(0, min(w, int(math.floor(min(x1, x2)))))
    right = max(0, min(w, int(math.ceil(max(x1, x2)))))
    top = max(0, min(h, int(math.floor(min(y1, y2)))))
    bottom = max(0, min(h, int(math.ceil(max(y1, y2)))))
    if right <= left or bottom <= top:
        return None
    values = array[top:bottom, left:right].astype(np.float64, copy=False).reshape(-1)
    encoding = depth_camera_info.get("encoding") if isinstance(depth_camera_info, Mapping) else None
    if (
        isinstance(encoding, str)
        and encoding.upper() in {"UINT16_MM", "16UC1_MM"}
        and np.issubdtype(array.dtype, np.integer)
    ):
        values = values / 1000.0
    values = values[np.isfinite(values) & (values > 0.0)]
    if isinstance(depth_camera_info, Mapping):
        low = depth_camera_info.get("minimum_depth_m", depth_camera_info.get("min_depth_m"))
        high = depth_camera_info.get("maximum_depth_m", depth_camera_info.get("max_depth_m"))
        if isinstance(low, (int, float)) and math.isfinite(float(low)):
            values = values[values >= float(low)]
        if isinstance(high, (int, float)) and math.isfinite(float(high)):
            values = values[values <= float(high)]
    if values.size == 0:
        return None
    # The median already rejects sparse outliers; trimming extreme values makes
    # mixed foreground/background boxes less sensitive to a few bad pixels.
    if values.size >= 9:
        lo, hi = np.percentile(values, (10.0, 90.0))
        trimmed = values[(values >= lo) & (values <= hi)]
        if trimmed.size:
            values = trimmed
    result = float(np.median(values))
    return result if math.isfinite(result) else None


@dataclass(frozen=True, slots=True)
class DetectorSpec:
    model_path: Path
    detector_id: str
    class_names: tuple[str, ...] = COCO80_CLASS_NAMES
    format: str = "raw"
    confidence_threshold: float = 0.25
    providers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.format not in {"raw", "nx6"}:
            raise ValueError("detector format must be raw or nx6")
        if not self.detector_id.strip():
            raise ValueError("detector_id cannot be empty")


def build_detector(spec: DetectorSpec) -> Any:
    common = dict(
        model_path=Path(spec.model_path),
        class_names=tuple(spec.class_names),
        confidence_threshold=float(spec.confidence_threshold),
        providers=tuple(spec.providers),
        model_version=spec.detector_id,
    )
    if spec.format == "nx6":
        return OnnxNx6Detector(OnnxNx6Config(**common))
    return OnnxRawYoloDetector(OnnxRawYoloConfig(**common))


class PerceptionBridge:
    """Offline bridge from SIM RGB/depth observations to structured detections."""

    def __init__(self, detectors: Sequence[tuple[DetectorSpec, Any]] | Sequence[Any]):
        self._detectors: list[tuple[str, Any, str]] = []
        for item in detectors:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], DetectorSpec):
                spec, detector = item
                digest = sha256_file(Path(spec.model_path))
                self._detectors.append((spec.detector_id, detector, digest))
            else:
                detector = item
                self._detectors.append(
                    (str(getattr(detector, "model_version", "detector")), detector, "")
                )

    def annotate_observation(
        self,
        observation: Mapping[str, Any],
        rgb: Any,
        depth: Any | None = None,
        *,
        rgb_path: str | None = None,
        depth_path: str | None = None,
    ) -> dict[str, Any]:
        image = np.asarray(rgb)
        if image.ndim != 3 or image.shape[2] < 3:
            raise ValueError("RGB image must have shape HxWx3")
        height, width = int(image.shape[0]), int(image.shape[1])
        camera_info = observation.get("camera_info")
        depth_info = observation.get("depth_camera_info")
        attitude = observation.get("attitude")
        records: list[dict[str, Any]] = []
        for detector_id, detector, model_sha in self._detectors:
            detections = detector.detect(image)
            for detection in detections:
                bbox = detection.bbox
                px = [bbox.x1 * width, bbox.y1 * height, bbox.x2 * width, bbox.y2 * height]
                center_x = (px[0] + px[2]) / 2.0
                bearing, absolute = bearing_from_pixel(
                    center_x,
                    camera_info=camera_info if isinstance(camera_info, Mapping) else None,
                    image_width=width,
                    image_height=height,
                    attitude=attitude if isinstance(attitude, Mapping) else None,
                )
                range_m = robust_depth_median(
                    depth,
                    px,
                    rgb_size=(width, height),
                    depth_camera_info=depth_info if isinstance(depth_info, Mapping) else None,
                ) if depth is not None else None
                records.append(
                    {
                        "class": detection.label,
                        "label": detection.label,
                        "confidence": float(detection.confidence),
                        "bbox": [float(bbox.x1), float(bbox.y1), float(bbox.x2), float(bbox.y2)],
                        "bbox_xyxy_px": [float(value) for value in px],
                        "bearing_deg": bearing,
                        "absolute_bearing_deg": absolute,
                        "range_m": range_m,
                        "detector_id": detector_id,
                        "model_sha256": model_sha,
                    }
                )
        frame_id = observation.get("frame_id", observation.get("frame_sensor_stamp_ns"))
        return {
            "schema": "multidetect.sim-teacher-perception-v1",
            "frame_id": None if frame_id is None else str(frame_id),
            "rgb_path": rgb_path,
            "depth_path": depth_path,
            "camera_info": camera_info,
            "depth_camera_info": depth_info,
            "detections": records,
        }


def annotate_observation(
    observation: Mapping[str, Any],
    rgb: Any,
    depth: Any | None = None,
    *,
    detectors: Sequence[tuple[DetectorSpec, Any]] | Sequence[Any] = (),
    **kwargs: Any,
) -> dict[str, Any]:
    """Functional convenience wrapper around :class:`PerceptionBridge`."""
    return PerceptionBridge(detectors).annotate_observation(observation, rgb, depth, **kwargs)


__all__ = [
    "COCO80_CLASS_NAMES",
    "DetectorSpec",
    "PerceptionBridge",
    "annotate_observation",
    "bearing_from_pixel",
    "build_detector",
    "robust_depth_median",
    "sha256_file",
    "wrap_degrees",
]

