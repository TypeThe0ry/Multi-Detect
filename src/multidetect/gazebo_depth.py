"""Truth-free contract for binding a Gazebo depth sample to one camera frame.

The live Gazebo transport currently ships wire-v3 RGB/NavSat/odometry only.
This module is the deliberately independent seam for a future depth or range
topic: it validates the sample before it can enter the existing
``DirectRangeMeasurement``/``MultiModalRangingEngine`` path.  It never reads
scenario truth and it does not manufacture a range from a target annotation.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .multimodal_ranging import DirectRangeMeasurement, DirectRangeSource

GAZEBO_DEPTH_SCHEMA = "multidetect.gazebo-depth-range-sample"
GAZEBO_DEPTH_SCHEMA_VERSION = 1
GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS = 1_500_000_000
GAZEBO_DEPTH_MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
GAZEBO_DEPTH_MAX_DIMENSION = 4096
GAZEBO_DEPTH_ENCODINGS = frozenset({"UINT16_MM", "FLOAT32_M"})
GAZEBO_DEPTH_DISTANCE_SEMANTICS = frozenset({"ray_slant_m", "optical_axis_z_m"})


class GazeboDepthContractError(ValueError):
    """Raised when a Gazebo depth/range sample is not safe to fuse."""


@dataclass(frozen=True, slots=True)
class GazeboDepthSensorBinding:
    """A scenario-declared depth sensor, without claiming transport readiness."""

    sensor_id: str
    stream_uri: str
    calibration_id: str

    def __post_init__(self) -> None:
        for name in ("sensor_id", "stream_uri", "calibration_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GazeboDepthContractError(f"{name} must be a non-empty string")
        if not self.stream_uri.startswith("gazebo://"):
            raise GazeboDepthContractError("depth stream_uri must use the gazebo:// scheme")

    def to_mapping(self) -> dict[str, object]:
        return {
            "sensor_id": self.sensor_id,
            "stream_uri": self.stream_uri,
            "calibration_id": self.calibration_id,
            "transport_status": "declared_only_until_bound_to_a_real_gazebo_topic",
        }


@dataclass(frozen=True, slots=True)
class GazeboDepthFrame:
    """One hash-verified metric depth image from a Gazebo side channel.

    This is intentionally separate from the legacy MDGZSIM3 RGB payload.  A
    future helper may populate it from a Gazebo ``Image`` topic, but a caller
    must still provide the exact source bytes and timestamp before sampling a
    target.  ``UINT16_MM`` is little-endian millimetres; ``FLOAT32_M`` is
    little-endian IEEE-754 metres.  Zero/non-finite pixels are invalid.
    """

    sensor_id: str
    calibration_id: str
    frame_id: str
    source_topic: str
    width: int
    height: int
    stride_bytes: int
    encoding: str
    payload: bytes
    payload_sha256: str
    source_stamp_ns: int
    frame_sensor_stamp_ns: int
    captured_at_s: float
    minimum_depth_m: float = 0.4
    maximum_depth_m: float = 800.0
    distance_semantics: str = "ray_slant_m"
    fx_px: float | None = None
    fy_px: float | None = None
    cx_px: float | None = None
    cy_px: float | None = None

    def __post_init__(self) -> None:
        for name in ("sensor_id", "calibration_id", "frame_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GazeboDepthContractError(f"{name} must be a non-empty string")
        if not isinstance(self.source_topic, str) or not self.source_topic.startswith("/"):
            raise GazeboDepthContractError("source_topic must be an absolute Gazebo topic")
        if self.encoding not in GAZEBO_DEPTH_ENCODINGS:
            raise GazeboDepthContractError("unsupported depth encoding")
        if self.distance_semantics not in GAZEBO_DEPTH_DISTANCE_SEMANTICS:
            raise GazeboDepthContractError("unsupported depth distance semantics")
        for name in ("width", "height", "stride_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise GazeboDepthContractError(f"{name} must be a positive integer")
        if self.width > GAZEBO_DEPTH_MAX_DIMENSION or self.height > GAZEBO_DEPTH_MAX_DIMENSION:
            raise GazeboDepthContractError("depth geometry exceeds the safety bound")
        bytes_per_pixel = 2 if self.encoding == "UINT16_MM" else 4
        expected_stride = self.width * bytes_per_pixel
        if self.stride_bytes != expected_stride:
            raise GazeboDepthContractError("depth stride does not match encoding geometry")
        if not isinstance(self.payload, (bytes, bytearray, memoryview)):
            raise GazeboDepthContractError("depth payload must be bytes-like")
        payload = bytes(self.payload)
        object.__setattr__(self, "payload", payload)
        expected_bytes = self.stride_bytes * self.height
        if expected_bytes > GAZEBO_DEPTH_MAX_PAYLOAD_BYTES:
            raise GazeboDepthContractError("depth payload exceeds the safety bound")
        if len(payload) != expected_bytes:
            raise GazeboDepthContractError("depth payload length does not match geometry")
        if (
            not isinstance(self.payload_sha256, str)
            or len(self.payload_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.payload_sha256)
            or hashlib.sha256(payload).hexdigest() != self.payload_sha256
        ):
            raise GazeboDepthContractError("depth payload SHA-256 does not match bytes")
        for name in ("source_stamp_ns", "frame_sensor_stamp_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise GazeboDepthContractError(f"{name} must be a positive integer")
        if self.source_stamp_ns > self.frame_sensor_stamp_ns:
            raise GazeboDepthContractError("depth source timestamp is from the future")
        if self.frame_sensor_stamp_ns - self.source_stamp_ns > GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS:
            raise GazeboDepthContractError("depth source timestamp skew exceeds the safety bound")
        numeric = (self.captured_at_s, self.minimum_depth_m, self.maximum_depth_m)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
            raise GazeboDepthContractError("depth timing/range limits must be numeric")
        if not all(math.isfinite(float(value)) for value in numeric):
            raise GazeboDepthContractError("depth timing/range limits must be finite")
        if self.captured_at_s < 0.0 or not (0.0 < self.minimum_depth_m < self.maximum_depth_m):
            raise GazeboDepthContractError("depth timing/range limits are invalid")
        intrinsics = (self.fx_px, self.fy_px, self.cx_px, self.cy_px)
        if any(value is not None for value in intrinsics):
            if any(
                value is None
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in intrinsics
            ):
                raise GazeboDepthContractError("depth camera intrinsics must be finite together")
            if self.fx_px <= 0.0 or self.fy_px <= 0.0:
                raise GazeboDepthContractError("depth camera focal lengths must be positive")
        if self.distance_semantics == "optical_axis_z_m" and any(
            value is None for value in intrinsics
        ):
            raise GazeboDepthContractError(
                "optical-axis depth requires complete camera intrinsics"
            )

    def _values_for_bbox(
        self,
        bbox: tuple[float, float, float, float],
        *,
        center_fraction: float,
    ) -> list[float]:
        if len(bbox) != 4 or not all(math.isfinite(float(value)) for value in bbox):
            raise GazeboDepthContractError("depth target bbox must contain four finite values")
        left, top, right, bottom = bbox
        if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
            raise GazeboDepthContractError("depth target bbox must be normalized and ordered")
        if not 0.1 <= center_fraction <= 1.0:
            raise GazeboDepthContractError("depth center_fraction must be in [0.1, 1]")
        center_x, center_y = (left + right) / 2.0, (top + bottom) / 2.0
        half_width = (right - left) * center_fraction / 2.0
        half_height = (bottom - top) * center_fraction / 2.0
        left, right = center_x - half_width, center_x + half_width
        top, bottom = center_y - half_height, center_y + half_height
        x0 = max(0, min(self.width - 1, math.floor(left * self.width)))
        x1 = max(x0 + 1, min(self.width, math.ceil(right * self.width)))
        y0 = max(0, min(self.height - 1, math.floor(top * self.height)))
        y1 = max(y0 + 1, min(self.height, math.ceil(bottom * self.height)))
        values: list[float] = []
        for row in range(y0, y1):
            offset = row * self.stride_bytes
            for column in range(x0, x1):
                pixel_offset = offset + column * (2 if self.encoding == "UINT16_MM" else 4)
                if self.encoding == "UINT16_MM":
                    value = struct.unpack_from("<H", self.payload, pixel_offset)[0] / 1000.0
                else:
                    value = struct.unpack_from("<f", self.payload, pixel_offset)[0]
                if self.distance_semantics == "optical_axis_z_m":
                    assert self.fx_px is not None
                    assert self.fy_px is not None
                    assert self.cx_px is not None
                    assert self.cy_px is not None
                    ray_x = (column + 0.5 - self.cx_px) / self.fx_px
                    ray_y = (row + 0.5 - self.cy_px) / self.fy_px
                    value *= math.sqrt(1.0 + ray_x * ray_x + ray_y * ray_y)
                if math.isfinite(value) and self.minimum_depth_m <= value <= self.maximum_depth_m:
                    values.append(value)
        return values

    def sample_target(
        self,
        *,
        target_id: str,
        bbox: tuple[float, float, float, float],
        center_fraction: float = 0.50,
        minimum_valid_pixels: int = 16,
        sigma_floor_m: float = 0.50,
        relative_sigma: float = 0.25,
    ) -> GazeboDepthRangeSample:
        """Robustly sample a target box and emit the shared range contract."""

        if not isinstance(target_id, str) or not target_id.strip():
            raise GazeboDepthContractError("target_id must be a non-empty string")
        if isinstance(minimum_valid_pixels, bool) or not isinstance(minimum_valid_pixels, int):
            raise GazeboDepthContractError("minimum_valid_pixels must be an integer")
        if minimum_valid_pixels < 1:
            raise GazeboDepthContractError("minimum_valid_pixels must be positive")
        if (
            not math.isfinite(float(sigma_floor_m))
            or sigma_floor_m <= 0.0
            or not math.isfinite(float(relative_sigma))
            or relative_sigma <= 0.0
        ):
            raise GazeboDepthContractError("depth sigma limits must be positive and finite")
        values = self._values_for_bbox(bbox, center_fraction=center_fraction)
        if len(values) < minimum_valid_pixels:
            raise GazeboDepthContractError(
                "depth target has too few valid pixels: "
                f"valid={len(values)}, minimum={minimum_valid_pixels}"
            )
        values.sort()
        middle = len(values) // 2
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0
        deviations = sorted(abs(value - median) for value in values)
        mad_middle = len(deviations) // 2
        mad = (
            deviations[mad_middle]
            if len(deviations) % 2
            else (deviations[mad_middle - 1] + deviations[mad_middle]) / 2.0
        )
        sigma = max(float(sigma_floor_m), median * float(relative_sigma), mad * 1.4826 * 2.0)
        return GazeboDepthRangeSample(
            sensor_id=self.sensor_id,
            calibration_id=self.calibration_id,
            frame_id=self.frame_id,
            target_id=target_id,
            source_topic=self.source_topic,
            source_stamp_ns=self.source_stamp_ns,
            frame_sensor_stamp_ns=self.frame_sensor_stamp_ns,
            captured_at_s=self.captured_at_s,
            slant_range_m=median,
            sigma_m=sigma,
            valid_pixel_count=len(values),
            absolute_scale_valid=True,
            distance_semantics="ray_slant_m",
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "sensor_id": self.sensor_id,
            "calibration_id": self.calibration_id,
            "frame_id": self.frame_id,
            "source_topic": self.source_topic,
            "width": self.width,
            "height": self.height,
            "stride_bytes": self.stride_bytes,
            "encoding": self.encoding,
            "payload_bytes": len(self.payload),
            "payload_sha256": self.payload_sha256,
            "source_stamp_ns": self.source_stamp_ns,
            "frame_sensor_stamp_ns": self.frame_sensor_stamp_ns,
            "captured_at_s": self.captured_at_s,
            "minimum_depth_m": self.minimum_depth_m,
            "maximum_depth_m": self.maximum_depth_m,
            "distance_semantics": self.distance_semantics,
            "fx_px": self.fx_px,
            "fy_px": self.fy_px,
            "cx_px": self.cx_px,
            "cy_px": self.cy_px,
            "truth_free": True,
        }


@dataclass(frozen=True, slots=True)
class GazeboDepthRangeSample:
    """One metric range measured from a real Gazebo depth/range source.

    ``source_stamp_ns`` and ``frame_sensor_stamp_ns`` are Gazebo simulation
    timestamps, not host wall-clock values.  The source must be causal and no
    older than ``GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS`` relative to the RGB frame.
    ``captured_at_s`` is the host capture time passed to the shared ranging
    engine, matching the timestamp semantics of ``DirectRangeMeasurement``.
    """

    sensor_id: str
    calibration_id: str
    frame_id: str
    target_id: str
    source_topic: str
    source_stamp_ns: int
    frame_sensor_stamp_ns: int
    captured_at_s: float
    slant_range_m: float
    sigma_m: float
    valid_pixel_count: int
    absolute_scale_valid: bool = True
    distance_semantics: str = "ray_slant_m"

    def __post_init__(self) -> None:
        for name in (
            "sensor_id",
            "calibration_id",
            "frame_id",
            "target_id",
            "source_topic",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GazeboDepthContractError(f"{name} must be a non-empty string")
        if not self.source_topic.startswith("/"):
            raise GazeboDepthContractError("source_topic must be an absolute Gazebo topic")
        for name in ("source_stamp_ns", "frame_sensor_stamp_ns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise GazeboDepthContractError(f"{name} must be a positive integer")
        if self.source_stamp_ns > self.frame_sensor_stamp_ns:
            raise GazeboDepthContractError("depth source timestamp is from the future")
        source_age_ns = self.frame_sensor_stamp_ns - self.source_stamp_ns
        if source_age_ns > GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS:
            raise GazeboDepthContractError(
                "depth source timestamp skew exceeds "
                f"{GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS} ns"
            )
        numeric = (
            self.captured_at_s,
            self.slant_range_m,
            self.sigma_m,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in numeric):
            raise GazeboDepthContractError("depth range values must be numeric")
        if not all(math.isfinite(float(value)) for value in numeric):
            raise GazeboDepthContractError("depth range values must be finite")
        if self.captured_at_s < 0.0:
            raise GazeboDepthContractError("captured_at_s cannot be negative")
        if self.slant_range_m <= 0.0 or self.sigma_m <= 0.0:
            raise GazeboDepthContractError("depth range and sigma must be positive")
        if self.distance_semantics != "ray_slant_m":
            raise GazeboDepthContractError(
                "range samples must be normalized to ray_slant_m"
            )
        if (
            isinstance(self.valid_pixel_count, bool)
            or not isinstance(self.valid_pixel_count, int)
            or self.valid_pixel_count < 1
        ):
            raise GazeboDepthContractError("valid_pixel_count must be a positive integer")
        if not isinstance(self.absolute_scale_valid, bool):
            raise GazeboDepthContractError("absolute_scale_valid must be boolean")

    @property
    def source_age_ns(self) -> int:
        return self.frame_sensor_stamp_ns - self.source_stamp_ns

    def measurement(self) -> DirectRangeMeasurement:
        """Convert to the shared production range-fusion input."""

        return DirectRangeMeasurement(
            source=DirectRangeSource.GAZEBO_DEPTH,
            target_id=self.target_id,
            slant_range_m=self.slant_range_m,
            sigma_m=self.sigma_m,
            captured_at_s=self.captured_at_s,
            absolute_scale_valid=self.absolute_scale_valid,
        )

    def to_mapping(self) -> dict[str, object]:
        result = asdict(self)
        result.update(
            {
                "schema": GAZEBO_DEPTH_SCHEMA,
                "schema_version": GAZEBO_DEPTH_SCHEMA_VERSION,
                "source_age_ns": self.source_age_ns,
                "measurement_source": DirectRangeSource.GAZEBO_DEPTH.value,
                "truth_free": True,
                "flight_control_enabled": False,
            }
        )
        return result

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> GazeboDepthRangeSample:
        if not isinstance(raw, Mapping):
            raise GazeboDepthContractError("depth sample must be an object")
        required = {
            "schema",
            "schema_version",
            "sensor_id",
            "calibration_id",
            "frame_id",
            "target_id",
            "source_topic",
            "source_stamp_ns",
            "frame_sensor_stamp_ns",
            "captured_at_s",
            "slant_range_m",
            "sigma_m",
            "valid_pixel_count",
            "distance_semantics",
        }
        optional = {
            "absolute_scale_valid",
            "source_age_ns",
            "measurement_source",
            "truth_free",
            "flight_control_enabled",
        }
        keys = set(raw)
        if keys - required - optional:
            raise GazeboDepthContractError("depth sample contains unknown fields")
        if keys & required != required:
            raise GazeboDepthContractError("depth sample is missing required fields")
        if raw.get("schema") != GAZEBO_DEPTH_SCHEMA:
            raise GazeboDepthContractError("depth sample schema is unsupported")
        if raw.get("schema_version") != GAZEBO_DEPTH_SCHEMA_VERSION:
            raise GazeboDepthContractError("depth sample schema_version is unsupported")
        if "source_age_ns" in raw and raw["source_age_ns"] != (
            raw.get("frame_sensor_stamp_ns", 0) - raw.get("source_stamp_ns", 0)
        ):
            raise GazeboDepthContractError("depth sample source_age_ns does not match timestamps")
        if "measurement_source" in raw and raw["measurement_source"] != (
            DirectRangeSource.GAZEBO_DEPTH.value
        ):
            raise GazeboDepthContractError("depth sample measurement_source is unsupported")
        if "truth_free" in raw and raw["truth_free"] is not True:
            raise GazeboDepthContractError("depth sample must be truth-free")
        if "flight_control_enabled" in raw and raw["flight_control_enabled"] is not False:
            raise GazeboDepthContractError("depth sample cannot enable flight control")
        try:
            return cls(
                sensor_id=raw["sensor_id"],
                calibration_id=raw["calibration_id"],
                frame_id=raw["frame_id"],
                target_id=raw["target_id"],
                source_topic=raw["source_topic"],
                source_stamp_ns=raw["source_stamp_ns"],
                frame_sensor_stamp_ns=raw["frame_sensor_stamp_ns"],
                captured_at_s=raw["captured_at_s"],
                slant_range_m=raw["slant_range_m"],
                sigma_m=raw["sigma_m"],
                valid_pixel_count=raw["valid_pixel_count"],
                absolute_scale_valid=raw.get("absolute_scale_valid", True),
                distance_semantics=raw["distance_semantics"],
            )
        except (KeyError, TypeError) as exc:
            raise GazeboDepthContractError("depth sample fields have invalid types") from exc


def resolve_declared_gazebo_depth_sensor(
    sensors: Sequence[object],
) -> GazeboDepthSensorBinding | None:
    """Resolve one manifest-declared depth camera without claiming it is live.

    The runtime manifest may declare a future sensor before a matching SDF
    topic exists.  Returning a binding with an explicit ``declared_only``
    status keeps that distinction visible and lets the external runner fail
    closed until a real topic is wired into the transport.
    """

    candidates = [
        sensor
        for sensor in sensors
        if getattr(sensor, "kind", None) == "depth_camera"
    ]
    if len(candidates) > 1:
        raise GazeboDepthContractError("at most one Gazebo depth camera is supported")
    if not candidates:
        return None
    sensor = candidates[0]
    return GazeboDepthSensorBinding(
        sensor_id=getattr(sensor, "sensor_id", ""),
        stream_uri=getattr(sensor, "stream_uri", ""),
        calibration_id=getattr(sensor, "calibration_id", ""),
    )


__all__ = [
    "GAZEBO_DEPTH_ENCODINGS",
    "GAZEBO_DEPTH_MAX_DIMENSION",
    "GAZEBO_DEPTH_MAX_PAYLOAD_BYTES",
    "GAZEBO_DEPTH_MAX_SOURCE_SKEW_NS",
    "GAZEBO_DEPTH_SCHEMA",
    "GAZEBO_DEPTH_SCHEMA_VERSION",
    "GazeboDepthContractError",
    "GazeboDepthFrame",
    "GazeboDepthRangeSample",
    "GazeboDepthSensorBinding",
    "resolve_declared_gazebo_depth_sensor",
]
