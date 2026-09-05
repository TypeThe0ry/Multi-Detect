"""Stream bounded Gazebo camera and vehicle telemetry frames over stdout.

The script is intended to run inside the pinned Gazebo Harmonic container.  It
subscribes only to sensor-facing Image, CameraInfo, NavSat, and repository-owned
3D OdometryPublisher messages.  Once all four sources are synchronized, exactly
``--frames`` records are written to ``stdout.buffer``.  Human-readable
diagnostics are confined to stderr so a host reader can treat stdout as a
fail-closed binary channel.  Wire v2 carries the complete ENU/FLU pose and
linear-velocity sample; the host performs the canonical NED/FRD conversion.

The wire helpers deliberately have no Gazebo imports.  Host-side tests and the
SIM backend can import this module to validate the exact same format without
installing Gazebo's Python bindings.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import json
import math
import queue
import struct
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, BinaryIO

WIRE_MAGIC = b"MDGZSIM2"
WIRE_VERSION = 2
GAZEBO_SOURCE_ROW_ORDER = "top_to_bottom"
WIRE_ROW_ORDER = "top_to_bottom"
GAZEBO_VERTICAL_FLIP_APPLIED = False

# Fixed v2 layout, in order:
# magic, version, header bytes, sequence, sensor timestamp, width, height,
# packed BGR stride, payload bytes, CameraInfo timestamp, NavSat timestamp,
# Odometry timestamp, latitude, longitude, absolute MSL altitude, Gazebo ENU
# position, Gazebo FLU->ENU orientation quaternion, body-FLU linear velocity,
# flags, and whole-record SHA-256.  The digest is calculated with its own field
# zeroed, so both telemetry and pixels are integrity bound.
WIRE_HEADER = struct.Struct("<8sHHIQIIIIQQQ13dI32s")
WIRE_HEADER_BYTES = WIRE_HEADER.size

FLAG_CAMERA_INFO = 1 << 0
FLAG_NAVSAT = 1 << 1
FLAG_ODOMETRY = 1 << 2
CAMERA_INFO = FLAG_CAMERA_INFO
NAVSAT = FLAG_NAVSAT
ODOMETRY = FLAG_ODOMETRY
WIRE_FLAG_CAMERA_INFO = FLAG_CAMERA_INFO
WIRE_FLAG_NAVSAT = FLAG_NAVSAT
WIRE_FLAG_ODOMETRY = FLAG_ODOMETRY
WIRE_REQUIRED_PRESENCE_FLAGS = FLAG_CAMERA_INFO | FLAG_NAVSAT | FLAG_ODOMETRY
WIRE_OPTIONAL_PRESENCE_FLAGS = 0

# The locked v2 struct has one flags field.  Its low 16 bits are presence
# flags and bits 16..23 carry an explicit pixel-format identifier.
PIXEL_FORMAT_BGR24 = 1
PIXEL_FORMAT_SHIFT = 16
PIXEL_FORMAT_MASK = 0x00FF0000
WIRE_PIXEL_FORMAT_BGR24_FLAG = PIXEL_FORMAT_BGR24 << PIXEL_FORMAT_SHIFT
WIRE_KNOWN_FLAGS = WIRE_REQUIRED_PRESENCE_FLAGS | WIRE_OPTIONAL_PRESENCE_FLAGS | PIXEL_FORMAT_MASK
WIRE_READY_BGR24_FLAGS = WIRE_REQUIRED_PRESENCE_FLAGS | WIRE_PIXEL_FORMAT_BGR24_FLAG

MAX_DIMENSION = 16_384
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_INPUT_BYTES = MAX_PAYLOAD_BYTES * 2
MAX_FRAME_COUNT = 100_000
# NavSat and Odometry are dynamic flight state and must remain close to the
# image that consumes them. Gazebo CameraInfo is calibration metadata: the
# Harmonic camera republishes it while retaining the original sensor stamp.
# Its geometry is checked against every image, and its timestamp must remain
# causal, but it does not expire like dynamic telemetry.
MAX_SOURCE_SKEW_NS = 1_500_000_000
MAX_SOURCE_HISTORY_SAMPLES = 256
MAX_ABS_LOCAL_POSITION_M = 1_000_000.0
MAX_ABS_LINEAR_VELOCITY_MPS = 2_000.0
MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS = 8
QUATERNION_NORM_TOLERANCE = 1.0e-3

# gz.msgs PixelFormatType values used by Gazebo Harmonic.
GAZEBO_PIXEL_FORMAT_L_INT8 = 1
GAZEBO_PIXEL_FORMAT_RGB_INT8 = 3
GAZEBO_PIXEL_FORMAT_RGBA_INT8 = 4
GAZEBO_PIXEL_FORMAT_BGR_INT8 = 7
GAZEBO_PIXEL_FORMAT_BGRA_INT8 = 8


@dataclass(frozen=True, slots=True)
class SensorWireHeader:
    """Validated semantic representation of one fixed v2 wire header."""

    sequence: int
    sensor_stamp_ns: int
    width: int
    height: int
    stride: int
    payload_bytes: int
    camera_info_stamp_ns: int
    navsat_stamp_ns: int
    odometry_stamp_ns: int
    latitude_deg: float
    longitude_deg: float
    altitude_m: float
    position_east_m: float
    position_north_m: float
    position_up_m: float
    orientation_x: float
    orientation_y: float
    orientation_z: float
    orientation_w: float
    velocity_forward_mps: float
    velocity_left_mps: float
    velocity_up_mps: float
    flags: int
    sha256: bytes

    @property
    def pixel_format(self) -> int:
        return (self.flags & PIXEL_FORMAT_MASK) >> PIXEL_FORMAT_SHIFT

    @property
    def camera_info_present(self) -> bool:
        return bool(self.flags & FLAG_CAMERA_INFO)

    @property
    def navsat_present(self) -> bool:
        return bool(self.flags & FLAG_NAVSAT)

    @property
    def odometry_present(self) -> bool:
        return bool(self.flags & FLAG_ODOMETRY)


@dataclass(slots=True)
class WireOrderValidator:
    """Reject missing, duplicate, or out-of-order frames before state advances."""

    next_sequence: int = 0
    previous_sensor_stamp_ns: int | None = None

    def accept(self, header: SensorWireHeader) -> None:
        if header.sequence != self.next_sequence:
            raise ValueError(
                f"wire sequence mismatch: expected {self.next_sequence}, received {header.sequence}"
            )
        if (
            self.previous_sensor_stamp_ns is not None
            and header.sensor_stamp_ns <= self.previous_sensor_stamp_ns
        ):
            raise ValueError(
                "sensor timestamps must be strictly increasing: "
                f"previous={self.previous_sensor_stamp_ns}, "
                f"received={header.sensor_stamp_ns}"
            )
        self.next_sequence += 1
        self.previous_sensor_stamp_ns = header.sensor_stamp_ns


class CausalSourceBuffer:
    """Bounded, strictly ordered history for one Gazebo telemetry topic.

    Gazebo transport callbacks are asynchronous.  Keeping only the last
    callback lets a sample whose simulation timestamp is newer than an image
    become attached to that earlier image.  A small ordered history instead
    lets the image callback select the newest sample that is actually causal.

    Repeated use of one slower telemetry sample by several camera frames is
    valid.  Duplicate or out-of-order *callbacks* are not: accepting those
    would make "newest causal sample" dependent on callback arrival order.
    """

    def __init__(
        self,
        source_name: str,
        *,
        maximum_samples: int = MAX_SOURCE_HISTORY_SAMPLES,
    ) -> None:
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("source_name cannot be empty")
        if isinstance(maximum_samples, bool) or not isinstance(maximum_samples, int):
            raise ValueError("maximum_samples must be an integer")
        if maximum_samples <= 0:
            raise ValueError("maximum_samples must be positive")
        self.source_name = source_name.strip()
        self.maximum_samples = maximum_samples
        self._samples: deque[dict[str, object]] = deque(maxlen=maximum_samples)
        self._last_stamp_ns: int | None = None

    @property
    def ready(self) -> bool:
        return bool(self._samples)

    @property
    def last_stamp_ns(self) -> int | None:
        return self._last_stamp_ns

    def append(self, sample: dict[str, object]) -> None:
        if not isinstance(sample, dict):
            raise TypeError("sample must be a dictionary")
        stamp_ns = _uint(
            f"{self.source_name} timestamp",
            sample.get("stamp_ns"),
            bits=64,
        )
        if stamp_ns <= 0:
            raise ValueError(f"{self.source_name} timestamp must be positive")
        if self._last_stamp_ns is not None and stamp_ns <= self._last_stamp_ns:
            raise ValueError(
                f"{self.source_name} timestamps must be strictly increasing: "
                f"previous={self._last_stamp_ns}, received={stamp_ns}"
            )
        stored = dict(sample)
        stored["stamp_ns"] = stamp_ns
        self._samples.append(stored)
        self._last_stamp_ns = stamp_ns

    def latest_at_or_before(self, image_stamp_ns: int) -> dict[str, object] | None:
        image_stamp = _uint("image timestamp", image_stamp_ns, bits=64)
        if image_stamp <= 0:
            raise ValueError("image timestamp must be positive")
        for sample in reversed(self._samples):
            if int(sample["stamp_ns"]) <= image_stamp:
                return dict(sample)
        return None


def _uint(name: str, value: int, *, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0 or value > (1 << bits) - 1:
        raise ValueError(f"{name} is outside unsigned {bits}-bit range")
    return value


def _finite(name: str, value: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    return normalized


def _validate_navsat(
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float,
) -> tuple[float, float, float]:
    latitude = _finite("latitude_deg", latitude_deg)
    longitude = _finite("longitude_deg", longitude_deg)
    altitude = _finite("altitude_m", altitude_m)
    # The wire feeds the shared local-N/E geolocation path; exact poles have
    # no stable longitude direction and must fail before a frame is emitted.
    if not -90.0 < latitude < 90.0:
        raise ValueError("latitude_deg is outside (-90, 90)")
    if not -180.0 <= longitude <= 180.0:
        raise ValueError("longitude_deg is outside [-180, 180]")
    if not -1_000.0 <= altitude <= 100_000.0:
        raise ValueError("altitude_m exceeds the wire safety bound [-1000, 100000]")
    return latitude, longitude, altitude


def _validate_source_skew(
    sensor_stamp_ns: int,
    *,
    camera_info_stamp_ns: int,
    navsat_stamp_ns: int,
    odometry_stamp_ns: int,
) -> None:
    for name, stamp_ns in (
        ("CameraInfo", camera_info_stamp_ns),
        ("NavSat", navsat_stamp_ns),
        ("Odometry", odometry_stamp_ns),
    ):
        if stamp_ns > sensor_stamp_ns:
            raise ValueError(
                f"wire {name} timestamp is from the future: "
                f"image={sensor_stamp_ns}, source={stamp_ns}"
            )
        age_ns = sensor_stamp_ns - stamp_ns
        if name != "CameraInfo" and age_ns > MAX_SOURCE_SKEW_NS:
            raise ValueError(
                f"wire {name} timestamp skew exceeds {MAX_SOURCE_SKEW_NS} ns: {age_ns}"
            )


def _validate_odometry(
    *,
    position_east_m: float,
    position_north_m: float,
    position_up_m: float,
    orientation_x: float,
    orientation_y: float,
    orientation_z: float,
    orientation_w: float,
    velocity_forward_mps: float,
    velocity_left_mps: float,
    velocity_up_mps: float,
) -> tuple[float, ...]:
    position = tuple(
        _finite(name, value)
        for name, value in (
            ("position_east_m", position_east_m),
            ("position_north_m", position_north_m),
            ("position_up_m", position_up_m),
        )
    )
    if any(abs(value) > MAX_ABS_LOCAL_POSITION_M for value in position):
        raise ValueError("wire Odometry position exceeds the local safety bound")

    quaternion = tuple(
        _finite(name, value)
        for name, value in (
            ("orientation_x", orientation_x),
            ("orientation_y", orientation_y),
            ("orientation_z", orientation_z),
            ("orientation_w", orientation_w),
        )
    )
    norm = math.sqrt(sum(value * value for value in quaternion))
    if norm <= 0.0 or abs(norm - 1.0) > QUATERNION_NORM_TOLERANCE:
        raise ValueError(
            f"wire Odometry quaternion is not normalized within {QUATERNION_NORM_TOLERANCE}"
        )
    normalized_quaternion = tuple(value / norm for value in quaternion)

    velocity = tuple(
        _finite(name, value)
        for name, value in (
            ("velocity_forward_mps", velocity_forward_mps),
            ("velocity_left_mps", velocity_left_mps),
            ("velocity_up_mps", velocity_up_mps),
        )
    )
    if any(abs(value) > MAX_ABS_LINEAR_VELOCITY_MPS for value in velocity):
        raise ValueError("wire Odometry velocity exceeds the safety bound")
    return (*position, *normalized_quaternion, *velocity)


def _validate_geometry(
    *,
    width: int,
    height: int,
    stride: int,
    payload_bytes: int,
) -> tuple[int, int, int, int]:
    width = _uint("width", width, bits=32)
    height = _uint("height", height, bits=32)
    stride = _uint("stride", stride, bits=32)
    payload_bytes = _uint("payload_bytes", payload_bytes, bits=32)
    if width == 0 or height == 0:
        raise ValueError("wire geometry must have positive width and height")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError("wire geometry exceeds the dimension safety bound")
    expected_stride = width * 3
    if stride != expected_stride:
        raise ValueError(
            f"BGR24 stride must equal width * 3 ({expected_stride}), received {stride}"
        )
    expected_payload = expected_stride * height
    if expected_payload > MAX_PAYLOAD_BYTES:
        raise ValueError("BGR24 payload exceeds the hard safety bound")
    if payload_bytes != expected_payload:
        raise ValueError(
            f"payload length does not match geometry: expected {expected_payload}, "
            f"received {payload_bytes}"
        )
    return width, height, stride, payload_bytes


def _validate_header(header: SensorWireHeader) -> SensorWireHeader:
    sequence = _uint("sequence", header.sequence, bits=32)
    sensor_stamp_ns = _uint("sensor_stamp_ns", header.sensor_stamp_ns, bits=64)
    camera_stamp = _uint("camera_info_stamp_ns", header.camera_info_stamp_ns, bits=64)
    navsat_stamp = _uint("navsat_stamp_ns", header.navsat_stamp_ns, bits=64)
    odometry_stamp = _uint("odometry_stamp_ns", header.odometry_stamp_ns, bits=64)
    if min(sensor_stamp_ns, camera_stamp, navsat_stamp, odometry_stamp) <= 0:
        raise ValueError("wire sensor and required readiness timestamps must be positive")
    _validate_source_skew(
        sensor_stamp_ns,
        camera_info_stamp_ns=camera_stamp,
        navsat_stamp_ns=navsat_stamp,
        odometry_stamp_ns=odometry_stamp,
    )
    width, height, stride, payload_bytes = _validate_geometry(
        width=header.width,
        height=header.height,
        stride=header.stride,
        payload_bytes=header.payload_bytes,
    )
    latitude, longitude, altitude = _validate_navsat(
        header.latitude_deg,
        header.longitude_deg,
        header.altitude_m,
    )
    flags = _uint("flags", header.flags, bits=32)
    if flags & ~WIRE_KNOWN_FLAGS:
        raise ValueError(f"wire header contains unknown flags: 0x{flags:08x}")
    if flags & WIRE_REQUIRED_PRESENCE_FLAGS != WIRE_REQUIRED_PRESENCE_FLAGS:
        raise ValueError("wire header is missing required synchronized telemetry")
    pixel_format = (flags & PIXEL_FORMAT_MASK) >> PIXEL_FORMAT_SHIFT
    if pixel_format != PIXEL_FORMAT_BGR24:
        raise ValueError(f"unsupported wire pixel format: {pixel_format}")
    digest = bytes(header.sha256)
    if len(digest) != hashlib.sha256().digest_size:
        raise ValueError("wire SHA-256 field must contain exactly 32 bytes")
    odometry = _validate_odometry(
        position_east_m=header.position_east_m,
        position_north_m=header.position_north_m,
        position_up_m=header.position_up_m,
        orientation_x=header.orientation_x,
        orientation_y=header.orientation_y,
        orientation_z=header.orientation_z,
        orientation_w=header.orientation_w,
        velocity_forward_mps=header.velocity_forward_mps,
        velocity_left_mps=header.velocity_left_mps,
        velocity_up_mps=header.velocity_up_mps,
    )
    return SensorWireHeader(
        sequence=sequence,
        sensor_stamp_ns=sensor_stamp_ns,
        width=width,
        height=height,
        stride=stride,
        payload_bytes=payload_bytes,
        camera_info_stamp_ns=camera_stamp,
        navsat_stamp_ns=navsat_stamp,
        odometry_stamp_ns=odometry_stamp,
        latitude_deg=latitude,
        longitude_deg=longitude,
        altitude_m=altitude,
        position_east_m=odometry[0],
        position_north_m=odometry[1],
        position_up_m=odometry[2],
        orientation_x=odometry[3],
        orientation_y=odometry[4],
        orientation_z=odometry[5],
        orientation_w=odometry[6],
        velocity_forward_mps=odometry[7],
        velocity_left_mps=odometry[8],
        velocity_up_mps=odometry[9],
        flags=flags,
        sha256=digest,
    )


def pack_header(header: SensorWireHeader) -> bytes:
    """Validate and pack one v2 fixed header."""

    value = _validate_header(header)
    return WIRE_HEADER.pack(
        WIRE_MAGIC,
        WIRE_VERSION,
        WIRE_HEADER_BYTES,
        value.sequence,
        value.sensor_stamp_ns,
        value.width,
        value.height,
        value.stride,
        value.payload_bytes,
        value.camera_info_stamp_ns,
        value.navsat_stamp_ns,
        value.odometry_stamp_ns,
        value.latitude_deg,
        value.longitude_deg,
        value.altitude_m,
        value.position_east_m,
        value.position_north_m,
        value.position_up_m,
        value.orientation_x,
        value.orientation_y,
        value.orientation_z,
        value.orientation_w,
        value.velocity_forward_mps,
        value.velocity_left_mps,
        value.velocity_up_mps,
        value.flags,
        value.sha256,
    )


def unpack_header(data: bytes) -> SensorWireHeader:
    """Unpack one header and reject any incompatible or unsafe field."""

    raw = bytes(data)
    if len(raw) != WIRE_HEADER_BYTES:
        raise ValueError(
            f"wire header must be exactly {WIRE_HEADER_BYTES} bytes, received {len(raw)}"
        )
    try:
        (
            magic,
            version,
            header_bytes,
            sequence,
            sensor_stamp_ns,
            width,
            height,
            stride,
            payload_bytes,
            camera_info_stamp_ns,
            navsat_stamp_ns,
            odometry_stamp_ns,
            latitude_deg,
            longitude_deg,
            altitude_m,
            position_east_m,
            position_north_m,
            position_up_m,
            orientation_x,
            orientation_y,
            orientation_z,
            orientation_w,
            velocity_forward_mps,
            velocity_left_mps,
            velocity_up_mps,
            flags,
            digest,
        ) = WIRE_HEADER.unpack(raw)
    except struct.error as exc:  # pragma: no cover - guarded by exact length
        raise ValueError("invalid wire header") from exc
    if magic != WIRE_MAGIC:
        raise ValueError("wire magic mismatch")
    if version != WIRE_VERSION:
        raise ValueError(f"unsupported wire version: {version}")
    if header_bytes != WIRE_HEADER_BYTES:
        raise ValueError(
            f"wire header-size mismatch: expected {WIRE_HEADER_BYTES}, received {header_bytes}"
        )
    return _validate_header(
        SensorWireHeader(
            sequence=sequence,
            sensor_stamp_ns=sensor_stamp_ns,
            width=width,
            height=height,
            stride=stride,
            payload_bytes=payload_bytes,
            camera_info_stamp_ns=camera_info_stamp_ns,
            navsat_stamp_ns=navsat_stamp_ns,
            odometry_stamp_ns=odometry_stamp_ns,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            altitude_m=altitude_m,
            position_east_m=position_east_m,
            position_north_m=position_north_m,
            position_up_m=position_up_m,
            orientation_x=orientation_x,
            orientation_y=orientation_y,
            orientation_z=orientation_z,
            orientation_w=orientation_w,
            velocity_forward_mps=velocity_forward_mps,
            velocity_left_mps=velocity_left_mps,
            velocity_up_mps=velocity_up_mps,
            flags=flags,
            sha256=digest,
        )
    )


def _pixel_layout(pixel_format_type: int | str) -> tuple[int, str]:
    if isinstance(pixel_format_type, str):
        normalized = pixel_format_type.strip().upper()
        aliases = {
            "L_INT8": (1, "MONO"),
            "L8": (1, "MONO"),
            "MONO8": (1, "MONO"),
            "RGB_INT8": (3, "RGB"),
            "RGB8": (3, "RGB"),
            "RGBA_INT8": (4, "RGBA"),
            "RGBA8": (4, "RGBA"),
            "BGR_INT8": (3, "BGR"),
            "BGR8": (3, "BGR"),
            "BGRA_INT8": (4, "BGRA"),
            "BGRA8": (4, "BGRA"),
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"unsupported Gazebo pixel format: {pixel_format_type!r}") from exc
    if isinstance(pixel_format_type, bool) or not isinstance(pixel_format_type, int):
        raise ValueError("pixel_format_type must be an integer enum or supported name")
    layouts = {
        GAZEBO_PIXEL_FORMAT_L_INT8: (1, "MONO"),
        GAZEBO_PIXEL_FORMAT_RGB_INT8: (3, "RGB"),
        GAZEBO_PIXEL_FORMAT_RGBA_INT8: (4, "RGBA"),
        GAZEBO_PIXEL_FORMAT_BGR_INT8: (3, "BGR"),
        GAZEBO_PIXEL_FORMAT_BGRA_INT8: (4, "BGRA"),
    }
    try:
        return layouts[pixel_format_type]
    except KeyError as exc:
        raise ValueError(f"unsupported Gazebo pixel-format enum: {pixel_format_type}") from exc


def normalize_bgr24(
    data: bytes | bytearray | memoryview,
    *,
    width: int,
    height: int,
    step: int,
    pixel_format_type: int | str,
    flip_vertical: bool = False,
) -> bytes:
    """Normalize supported Gazebo pixels and row padding to packed BGR24.

    The pure helper preserves source row order by default. Gazebo Harmonic's
    camera transport already emits top-to-bottom rows, which is also the
    MDGZSIM2/OpenCV/QGC contract. The live callback therefore performs color
    conversion and padding removal without an extra vertical flip.
    """

    width = _uint("width", width, bits=32)
    height = _uint("height", height, bits=32)
    step = _uint("step", step, bits=32)
    if not isinstance(flip_vertical, bool):
        raise ValueError("flip_vertical must be a boolean")
    if width == 0 or height == 0:
        raise ValueError("image geometry must have positive width and height")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValueError("image geometry exceeds the dimension safety bound")
    channels, order = _pixel_layout(pixel_format_type)
    packed_input_row = width * channels
    if step < packed_input_row:
        raise ValueError(
            f"image step is shorter than one packed row: minimum {packed_input_row}, "
            f"received {step}"
        )
    output_stride = width * 3
    output_bytes = output_stride * height
    input_bytes = step * height
    if output_bytes > MAX_PAYLOAD_BYTES:
        raise ValueError("normalized BGR24 payload exceeds the hard safety bound")
    if input_bytes > MAX_INPUT_BYTES:
        raise ValueError("Gazebo image payload exceeds the input safety bound")
    try:
        view = memoryview(data)
        if view.ndim != 1 or view.itemsize != 1:
            view = view.cast("B")
    except (TypeError, ValueError) as exc:
        raise ValueError("image data must be a contiguous bytes-like object") from exc
    if not view.contiguous:
        raise ValueError("image data must be contiguous")
    if view.nbytes != input_bytes:
        raise ValueError(
            f"image payload length does not match step * height: expected {input_bytes}, "
            f"received {view.nbytes}"
        )
    raw = view.cast("B")
    output = bytearray(output_bytes)
    for row_index in range(height):
        source_start = row_index * step
        source = raw[source_start : source_start + packed_input_row]
        target_row_index = height - 1 - row_index if flip_vertical else row_index
        target_start = target_row_index * output_stride
        target_end = target_start + output_stride
        if order == "BGR":
            output[target_start:target_end] = source
        elif order == "RGB":
            output[target_start:target_end:3] = source[2:packed_input_row:3]
            output[target_start + 1 : target_end : 3] = source[1:packed_input_row:3]
            output[target_start + 2 : target_end : 3] = source[0:packed_input_row:3]
        elif order == "MONO":
            output[target_start:target_end:3] = source
            output[target_start + 1 : target_end : 3] = source
            output[target_start + 2 : target_end : 3] = source
        elif order == "RGBA":
            output[target_start:target_end:3] = source[2:packed_input_row:4]
            output[target_start + 1 : target_end : 3] = source[1:packed_input_row:4]
            output[target_start + 2 : target_end : 3] = source[0:packed_input_row:4]
        else:  # BGRA
            output[target_start:target_end:3] = source[0:packed_input_row:4]
            output[target_start + 1 : target_end : 3] = source[1:packed_input_row:4]
            output[target_start + 2 : target_end : 3] = source[2:packed_input_row:4]
    return bytes(output)


def build_wire_frame(
    *,
    sequence: int,
    sensor_stamp_ns: int,
    width: int,
    height: int,
    camera_info_stamp_ns: int,
    navsat_stamp_ns: int,
    odometry_stamp_ns: int,
    latitude_deg: float,
    longitude_deg: float,
    altitude_m: float,
    position_east_m: float,
    position_north_m: float,
    position_up_m: float,
    orientation_x: float,
    orientation_y: float,
    orientation_z: float,
    orientation_w: float,
    velocity_forward_mps: float,
    velocity_left_mps: float,
    velocity_up_mps: float,
    bgr_payload: bytes | bytearray | memoryview,
) -> bytes:
    """Build a complete integrity-bound frame from telemetry and BGR24 pixels."""

    payload = bytes(bgr_payload)
    stride = width * 3 if isinstance(width, int) and not isinstance(width, bool) else 0
    header = SensorWireHeader(
        sequence=sequence,
        sensor_stamp_ns=sensor_stamp_ns,
        width=width,
        height=height,
        stride=stride,
        payload_bytes=len(payload),
        camera_info_stamp_ns=camera_info_stamp_ns,
        navsat_stamp_ns=navsat_stamp_ns,
        odometry_stamp_ns=odometry_stamp_ns,
        latitude_deg=latitude_deg,
        longitude_deg=longitude_deg,
        altitude_m=altitude_m,
        position_east_m=position_east_m,
        position_north_m=position_north_m,
        position_up_m=position_up_m,
        orientation_x=orientation_x,
        orientation_y=orientation_y,
        orientation_z=orientation_z,
        orientation_w=orientation_w,
        velocity_forward_mps=velocity_forward_mps,
        velocity_left_mps=velocity_left_mps,
        velocity_up_mps=velocity_up_mps,
        flags=WIRE_READY_BGR24_FLAGS,
        sha256=b"\x00" * hashlib.sha256().digest_size,
    )
    normalized = _validate_header(header)
    digest = hashlib.sha256(pack_header(normalized) + payload).digest()
    final_header = replace(normalized, sha256=digest)
    return pack_header(final_header) + payload


def parse_wire_frame(
    data: bytes | bytearray | memoryview,
    *,
    order: WireOrderValidator | None = None,
) -> tuple[SensorWireHeader, bytes]:
    """Parse exactly one complete record and verify its payload digest."""

    raw = bytes(data)
    if len(raw) < WIRE_HEADER_BYTES:
        raise ValueError("wire frame is truncated before the fixed header")
    header = unpack_header(raw[:WIRE_HEADER_BYTES])
    expected_total = WIRE_HEADER_BYTES + header.payload_bytes
    if len(raw) != expected_total:
        raise ValueError(
            f"wire frame length mismatch: expected {expected_total}, received {len(raw)}"
        )
    payload = raw[WIRE_HEADER_BYTES:]
    digest_input = raw[: WIRE_HEADER_BYTES - hashlib.sha256().digest_size]
    digest_input += b"\x00" * hashlib.sha256().digest_size
    if hashlib.sha256(digest_input + payload).digest() != header.sha256:
        raise ValueError("wire record SHA-256 mismatch")
    if order is not None:
        order.accept(header)
    return header, payload


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise ValueError(
                f"wire stream ended early: expected {size} bytes, received {len(chunks)}"
            )
        chunks.extend(chunk)
    return bytes(chunks)


def read_wire_frame(
    stream: BinaryIO,
    *,
    order: WireOrderValidator | None = None,
) -> tuple[SensorWireHeader, bytes]:
    """Read one binary record from a pipe without accepting short reads."""

    header_bytes = _read_exact(stream, WIRE_HEADER_BYTES)
    header = unpack_header(header_bytes)
    payload = _read_exact(stream, header.payload_bytes)
    digest_input = header_bytes[: WIRE_HEADER_BYTES - hashlib.sha256().digest_size]
    digest_input += b"\x00" * hashlib.sha256().digest_size
    if hashlib.sha256(digest_input + payload).digest() != header.sha256:
        raise ValueError("wire record SHA-256 mismatch")
    if order is not None:
        order.accept(header)
    return header, payload


def decode_wire_blob(data: bytes, *, expected_frames: int) -> list[tuple[SensorWireHeader, bytes]]:
    """Decode a bounded blob and reject missing frames or trailing stdout bytes."""

    expected_frames = _uint("expected_frames", expected_frames, bits=32)
    stream = io.BytesIO(data)
    order = WireOrderValidator()
    frames = [read_wire_frame(stream, order=order) for _ in range(expected_frames)]
    if stream.read(1):
        raise ValueError("wire stream contains trailing bytes after the bounded frame count")
    return frames


def _stamp_ns(message: Any) -> int:
    stamp = getattr(getattr(message, "header", None), "stamp", None)
    if stamp is None:
        raise ValueError("Gazebo message is missing header.stamp")
    sec = int(stamp.sec)
    nsec = int(stamp.nsec)
    if sec < 0 or not 0 <= nsec < 1_000_000_000:
        raise ValueError(f"invalid Gazebo timestamp: sec={sec}, nsec={nsec}")
    return _uint("Gazebo timestamp", sec * 1_000_000_000 + nsec, bits=64)


def _odometry_from_message(message: Any) -> tuple[float, ...]:
    pose = getattr(getattr(message, "pose_with_covariance", None), "pose", None)
    twist = getattr(getattr(message, "twist_with_covariance", None), "twist", None)
    position = getattr(pose, "position", None)
    orientation = getattr(pose, "orientation", None)
    linear = getattr(twist, "linear", None)
    if position is None or orientation is None or linear is None:
        raise ValueError("Odometry message is missing pose or linear velocity")
    return _validate_odometry(
        position_east_m=position.x,
        position_north_m=position.y,
        position_up_m=position.z,
        orientation_x=orientation.x,
        orientation_y=orientation.y,
        orientation_z=orientation.z,
        orientation_w=orientation.w,
        velocity_forward_mps=linear.x,
        velocity_left_mps=linear.y,
        velocity_up_mps=linear.z,
    )


def _diagnostic(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr, flush=True)


def _shutdown_transport_node(
    node: Any,
    topics: tuple[str, ...],
    *,
    drain_lock: Any = None,
) -> tuple[str, ...]:
    """Stop Gazebo transport callbacks before the Python process exits.

    ``gz.transport13.Node`` owns a C++ callback executor.  Letting the
    interpreter decref a live node at process teardown can race that executor
    and terminate with ``std::terminate`` even after every requested frame was
    emitted.  Drain the image callback, explicitly unsubscribe each topic,
    then give the transport thread a short settling window.  The helper is
    deliberately defensive so the pinned runtime reports a structured,
    fail-closed error instead of silently accepting an unsafe teardown.
    """

    errors: list[str] = []
    if drain_lock is not None:
        try:
            acquired = bool(drain_lock.acquire(timeout=2.0))
        except (AttributeError, TypeError):
            acquired = False
        if not acquired:
            errors.append("timed out draining Gazebo image callback")
        else:
            drain_lock.release()

    unsubscribe = getattr(node, "unsubscribe", None)
    if not callable(unsubscribe):
        errors.append("gz.transport13.Node.unsubscribe is unavailable")
    else:
        # Reverse order keeps the required image callback alive until the
        # readiness sources have been detached.  De-duplicate defensively in
        # case a caller supplied the same topic for an optional subscription.
        for topic in reversed(tuple(dict.fromkeys(topics))):
            try:
                result = unsubscribe(topic)
                if result is False:
                    errors.append(f"unsubscribe returned false for {topic}")
            except Exception as exc:  # pragma: no cover - runtime binding only
                errors.append(f"unsubscribe failed for {topic}: {exc}")

    # The C++ transport callback executor is asynchronous.  A short bounded
    # settle period prevents its joinable worker from racing Node destruction.
    time.sleep(0.1)
    return tuple(errors)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    parser.add_argument("--world", default="default")
    parser.add_argument("--model-instance", default="standard_vtol_mono_cam_0")
    parser.add_argument("--image-topic")
    parser.add_argument("--camera-info-topic")
    parser.add_argument("--navsat-topic")
    parser.add_argument("--odometry-topic")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not 1 <= args.frames <= MAX_FRAME_COUNT:
        raise SystemExit(f"--frames must be between 1 and {MAX_FRAME_COUNT}")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be a positive finite number")

    # Gazebo bindings exist only inside the pinned runtime image.
    from gz.msgs10.camera_info_pb2 import CameraInfo
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.navsat_pb2 import NavSat
    from gz.msgs10.odometry_with_covariance_pb2 import OdometryWithCovariance
    from gz.transport13 import Node

    model = args.model_instance
    world = args.world
    image_topic = args.image_topic or (
        f"/world/{world}/model/{model}/link/camera_link/sensor/camera/image"
    )
    camera_info_topic = args.camera_info_topic or (
        f"/world/{world}/model/{model}/link/camera_link/sensor/camera/camera_info"
    )
    navsat_topic = args.navsat_topic or (
        f"/world/{world}/model/{model}/link/base_link/sensor/navsat_sensor/navsat"
    )
    odometry_topic = args.odometry_topic or f"/model/{model}/odometry_with_covariance"

    state_lock = threading.Lock()
    image_lock = threading.Lock()
    error_event = threading.Event()
    packets: queue.Queue[bytes] = queue.Queue(maxsize=min(args.frames, 4))
    source_buffers = {
        "camera_info": CausalSourceBuffer("CameraInfo"),
        "navsat": CausalSourceBuffer("NavSat"),
        "odometry": CausalSourceBuffer("Odometry"),
    }
    counts = {
        "image": 0,
        "ignored_before_ready": 0,
        "future_source_rejections": 0,
        "stale_dynamic_source_rejections": 0,
        "stale_navsat_rejections": 0,
        "stale_odometry_rejections": 0,
        "camera_info": 0,
        "navsat": 0,
        "odometry": 0,
        "odometry_invalid": 0,
        "odometry_startup_velocity_rejected": 0,
    }
    accepted = 0
    odometry_timestamp_frame_count = 0
    last_sensor_stamp_ns: int | None = None
    first_error: str | None = None

    def fail(message: str) -> None:
        nonlocal first_error
        with state_lock:
            if first_error is None:
                first_error = message
        error_event.set()

    def on_camera_info(message: CameraInfo) -> None:
        try:
            stamp_ns = _stamp_ns(message)
            width = int(message.width)
            height = int(message.height)
            if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise ValueError("CameraInfo contains invalid geometry")
            with state_lock:
                source_buffers["camera_info"].append(
                    {
                        "stamp_ns": stamp_ns,
                        "width": width,
                        "height": height,
                    }
                )
                counts["camera_info"] += 1
        except Exception as exc:  # Gazebo callbacks must fail through the main thread.
            fail(f"CameraInfo rejected: {exc}")

    def on_navsat(message: NavSat) -> None:
        try:
            stamp_ns = _stamp_ns(message)
            latitude, longitude, altitude = _validate_navsat(
                message.latitude_deg,
                message.longitude_deg,
                message.altitude,
            )
            with state_lock:
                source_buffers["navsat"].append(
                    {
                        "stamp_ns": stamp_ns,
                        "latitude_deg": latitude,
                        "longitude_deg": longitude,
                        "altitude_m": altitude,
                    }
                )
                counts["navsat"] += 1
        except Exception as exc:
            fail(f"NavSat rejected: {exc}")

    def on_odometry(message: OdometryWithCovariance) -> None:
        try:
            stamp_ns = _stamp_ns(message)
            if stamp_ns <= 0:
                raise ValueError("Odometry timestamp must be positive when present")
            odometry = _odometry_from_message(message)
            with state_lock:
                source_buffers["odometry"].append(
                    {
                        "stamp_ns": stamp_ns,
                        "position_east_m": odometry[0],
                        "position_north_m": odometry[1],
                        "position_up_m": odometry[2],
                        "orientation_x": odometry[3],
                        "orientation_y": odometry[4],
                        "orientation_z": odometry[5],
                        "orientation_w": odometry[6],
                        "velocity_forward_mps": odometry[7],
                        "velocity_left_mps": odometry[8],
                        "velocity_up_mps": odometry[9],
                    }
                )
                counts["odometry"] += 1
        except Exception as exc:
            with state_lock:
                counts["odometry_invalid"] += 1
                startup_velocity_transient = bool(
                    str(exc) == "wire Odometry velocity exceeds the safety bound"
                    and counts["odometry"] == 0
                    and accepted == 0
                    and counts["odometry_startup_velocity_rejected"]
                    < MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS
                )
                if startup_velocity_transient:
                    counts["odometry_startup_velocity_rejected"] += 1
            if startup_velocity_transient:
                return
            fail(f"Odometry rejected: {exc}")

    def on_image(message: Image) -> None:
        nonlocal accepted, last_sensor_stamp_ns, odometry_timestamp_frame_count
        with image_lock:
            try:
                stamp_ns = _stamp_ns(message)
                if stamp_ns <= 0:
                    raise ValueError("Image timestamp must be positive")
                if last_sensor_stamp_ns is not None and stamp_ns <= last_sensor_stamp_ns:
                    raise ValueError(
                        "Image timestamps are not strictly increasing: "
                        f"previous={last_sensor_stamp_ns}, received={stamp_ns}"
                    )
                # Advance on every received image, including one rejected for
                # missing causal telemetry.  Otherwise a duplicate of an
                # ignored image could later be accepted after readiness.
                last_sensor_stamp_ns = stamp_ns
                with state_lock:
                    counts["image"] += 1
                    if error_event.is_set() or accepted >= args.frames:
                        return
                    selected = {
                        name: buffer.latest_at_or_before(stamp_ns)
                        for name, buffer in source_buffers.items()
                    }
                    if any(sample is None for sample in selected.values()):
                        counts["ignored_before_ready"] += 1
                        if any(
                            source_buffers[name].ready and sample is None
                            for name, sample in selected.items()
                        ):
                            counts["future_source_rejections"] += 1
                        return
                    camera = selected["camera_info"]
                    navsat = selected["navsat"]
                    odometry = selected["odometry"]
                    assert camera is not None and navsat is not None and odometry is not None
                    stale_dynamic_sources = tuple(
                        source
                        for source, sample in (("navsat", navsat), ("odometry", odometry))
                        if stamp_ns - int(sample["stamp_ns"]) > MAX_SOURCE_SKEW_NS
                    )
                    if stale_dynamic_sources:
                        # Gazebo transport dispatches sensor callbacks on
                        # independent workers.  A camera callback can briefly
                        # overtake NavSat/Odometry even though fresh samples
                        # are already in flight.  Drop that image instead of
                        # binding stale flight state; a persistent gap still
                        # fails at the bounded helper timeout.
                        counts["stale_dynamic_source_rejections"] += 1
                        for source in stale_dynamic_sources:
                            counts[f"stale_{source}_rejections"] += 1
                        return
                    sequence = accepted
                width = int(message.width)
                height = int(message.height)
                if int(camera["width"]) != width or int(camera["height"]) != height:
                    raise ValueError(
                        "Image geometry does not match the synchronized CameraInfo: "
                        f"image={width}x{height}, "
                        f"camera_info={camera['width']}x{camera['height']}"
                    )
                bgr_payload = normalize_bgr24(
                    message.data,
                    width=width,
                    height=height,
                    step=int(message.step),
                    pixel_format_type=int(message.pixel_format_type),
                    flip_vertical=GAZEBO_VERTICAL_FLIP_APPLIED,
                )
                packet = build_wire_frame(
                    sequence=sequence,
                    sensor_stamp_ns=stamp_ns,
                    width=width,
                    height=height,
                    camera_info_stamp_ns=int(camera["stamp_ns"]),
                    navsat_stamp_ns=int(navsat["stamp_ns"]),
                    odometry_stamp_ns=int(odometry["stamp_ns"]),
                    latitude_deg=float(navsat["latitude_deg"]),
                    longitude_deg=float(navsat["longitude_deg"]),
                    altitude_m=float(navsat["altitude_m"]),
                    position_east_m=float(odometry["position_east_m"]),
                    position_north_m=float(odometry["position_north_m"]),
                    position_up_m=float(odometry["position_up_m"]),
                    orientation_x=float(odometry["orientation_x"]),
                    orientation_y=float(odometry["orientation_y"]),
                    orientation_z=float(odometry["orientation_z"]),
                    orientation_w=float(odometry["orientation_w"]),
                    velocity_forward_mps=float(odometry["velocity_forward_mps"]),
                    velocity_left_mps=float(odometry["velocity_left_mps"]),
                    velocity_up_mps=float(odometry["velocity_up_mps"]),
                    bgr_payload=bgr_payload,
                )
                with state_lock:
                    accepted += 1
                    odometry_timestamp_frame_count += 1
                packets.put(packet)
            except Exception as exc:
                fail(f"Image rejected: {exc}")

    node = Node()
    subscription_topics = (
        image_topic,
        camera_info_topic,
        navsat_topic,
        odometry_topic,
    )
    required_subscriptions = [
        (Image, image_topic, on_image),
        (CameraInfo, camera_info_topic, on_camera_info),
        (NavSat, navsat_topic, on_navsat),
        (OdometryWithCovariance, odometry_topic, on_odometry),
    ]
    for message_type, topic, callback in required_subscriptions:
        if not node.subscribe(message_type, topic, callback):
            _diagnostic("gazebo_sensor_stream_subscription_failed", topic=topic)
            cleanup_errors = _shutdown_transport_node(
                node,
                subscription_topics,
                drain_lock=image_lock,
            )
            if cleanup_errors:
                _diagnostic(
                    "gazebo_sensor_stream_teardown_failed",
                    errors=list(cleanup_errors),
                )
            node = None
            gc.collect()
            return 2
    _diagnostic(
        "gazebo_sensor_stream_started",
        requested_frames=args.frames,
        image_topic=image_topic,
        camera_info_topic=camera_info_topic,
        navsat_topic=navsat_topic,
        odometry_topic=odometry_topic,
        wire_magic=WIRE_MAGIC.decode("ascii"),
        wire_version=WIRE_VERSION,
        header_bytes=WIRE_HEADER_BYTES,
        pixel_format="BGR24",
        source_row_order=GAZEBO_SOURCE_ROW_ORDER,
        wire_row_order=WIRE_ROW_ORDER,
        vertical_flip_applied=GAZEBO_VERTICAL_FLIP_APPLIED,
        required_sources=["image", "camera_info", "navsat", "odometry_pose_velocity"],
        optional_sources=[],
        source_association_policy="nearest_source_at_or_before_image",
        camera_info_freshness_policy="session_static_geometry_bound_causal_timestamp",
        maximum_dynamic_source_skew_ns=MAX_SOURCE_SKEW_NS,
        stale_dynamic_image_policy="discard_until_fresh_sources_or_bounded_timeout",
        source_callback_order="strictly_increasing_per_topic",
        future_source_samples_allowed=False,
        odometry_subscription_active=True,
        odometry_required=True,
        odometry_pose_velocity_fusion=False,
        full_odometry_pose_velocity_fusion_claimed=False,
        full_odometry_pose_velocity_fusion_gate="pending_synchronized_frames",
        startup_odometry_velocity_policy=(
            "discard_before_first_valid_odometry_and_before_first_accepted_frame"
        ),
        maximum_startup_odometry_velocity_rejections=(MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS),
    )
    emitted = 0
    deadline = time.monotonic() + args.timeout_seconds
    try:
        while emitted < args.frames:
            if error_event.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail("timed out waiting for synchronized Gazebo sensor frames")
                break
            try:
                packet = packets.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            sys.stdout.buffer.write(packet)
            sys.stdout.buffer.flush()
            emitted += 1
    except (BrokenPipeError, OSError) as exc:
        fail(f"binary stdout write failed: {exc}")

    cleanup_errors = _shutdown_transport_node(
        node,
        subscription_topics,
        drain_lock=image_lock,
    )
    if cleanup_errors:
        fail("Gazebo transport teardown failed: " + "; ".join(cleanup_errors))
        _diagnostic(
            "gazebo_sensor_stream_teardown_failed",
            errors=list(cleanup_errors),
        )
    # Force the C++ Node destructor while the interpreter and callback
    # machinery are still fully alive, rather than during Python shutdown.
    node = None
    gc.collect()

    with state_lock:
        error = first_error
        count_snapshot = dict(counts)
        ready_sources = sorted(name for name, buffer in source_buffers.items() if buffer.ready)
        accepted_snapshot = accepted
        odometry_timestamp_frame_count_snapshot = odometry_timestamp_frame_count
    gate_passed = error is None and emitted == args.frames and accepted_snapshot == args.frames
    _diagnostic(
        "gazebo_sensor_stream_finished",
        gate_passed=gate_passed,
        requested_frames=args.frames,
        accepted_frames=accepted_snapshot,
        emitted_frames=emitted,
        ready_sources=ready_sources,
        odometry_present="odometry" in ready_sources,
        odometry_required=True,
        odometry_timestamp_frame_count=odometry_timestamp_frame_count_snapshot,
        odometry_pose_velocity_fusion=gate_passed,
        full_odometry_pose_velocity_fusion_claimed=gate_passed,
        causal_source_association_claimed=gate_passed,
        source_association_policy="nearest_source_at_or_before_image",
        future_source_samples_allowed=False,
        source_row_order=GAZEBO_SOURCE_ROW_ORDER,
        wire_row_order=WIRE_ROW_ORDER,
        vertical_flip_applied=GAZEBO_VERTICAL_FLIP_APPLIED,
        message_counts=count_snapshot,
        error=error,
    )
    return 0 if gate_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
