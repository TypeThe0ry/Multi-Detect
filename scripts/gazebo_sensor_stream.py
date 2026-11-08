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

try:  # ``python scripts/gazebo_sensor_stream.py`` inside the container.
    from scripts.gazebo_depth_wire import ROTOR_VELOCITY_SLOWDOWN, build_depth_wire_record
except ModuleNotFoundError:  # pragma: no cover - standalone helper launch
    from gazebo_depth_wire import ROTOR_VELOCITY_SLOWDOWN, build_depth_wire_record

WIRE_MAGIC = b"MDGZSIM3"
WIRE_VERSION = 3
GAZEBO_SOURCE_ROW_ORDER = "top_to_bottom"
WIRE_ROW_ORDER = "top_to_bottom"
GAZEBO_VERTICAL_FLIP_APPLIED = False

# Fixed v2 layout, in order:
# magic, version, header bytes, sequence, sensor timestamp, width, height,
# packed BGR stride, payload bytes, CameraInfo timestamp, NavSat timestamp,
# Odometry timestamp, latitude, longitude, absolute MSL altitude, Gazebo ENU
# position, Gazebo FLU->ENU orientation quaternion, body-FLU linear velocity,
# row-major 3x3 fixed-ENU-axis attitude covariance (rad^2), flags, and
# whole-record SHA-256.  The digest is calculated with its own field
# zeroed, so both telemetry and pixels are integrity bound.
WIRE_HEADER = struct.Struct("<8sHHIQIIIIQQQ22dI32s")
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
# A 1280x960 R_FLOAT32 depth image is 4,915,200 bytes before Python object
# overhead.  The depth stream only needs to cover the 100 ms causal-skew
# window, so retaining the generic 256-sample history would keep roughly
# 1.25 GiB of payloads alive and can let the disposable Gazebo helper be OOM
# killed while writing a companion record.  Keep a small bounded history that
# covers several 30 Hz samples and leaves room for callback jitter.
MAX_DEPTH_HISTORY_SAMPLES = 8
MAX_ABS_LOCAL_POSITION_M = 1_000_000.0
MAX_ABS_LINEAR_VELOCITY_MPS = 2_000.0
# Fixed-wing models spawned on a runway can report a burst of physics-settling
# velocities before the first valid odometry; the allowance ends at that first sample.
MAX_STARTUP_ODOMETRY_VELOCITY_REJECTIONS = 64
# A fault diagnostic needs one bounded event slot per requested image plus a
# small boundary budget for drops, fresh-source callbacks, and recovery
# markers.  Keeping the extra budget explicit preserves fail-closed overflow
# detection without making the normal 900-frame combined run overflow by
# construction.
MAX_FAULT_TIMELINE_EVENTS = 64
QUATERNION_NORM_TOLERANCE = 1.0e-3
FAULT_SOURCES = ("image", "navsat", "odometry")
DEPTH_MAX_SOURCE_SKEW_NS = 100_000_000
MAX_JOINT_STATE_SKEW_NS = 100_000_000
ROTOR_JOINT_NAMES = tuple(f"rotor_{index}_joint" for index in range(4))
GIMBAL_JOINT_NAMES = {
    "pitch": "cgo3_camera_joint",
    "yaw": "cgo3_vertical_arm_joint",
    "roll": "cgo3_horizontal_arm_joint",
}
GIMBAL_JOINT_NAME_SETS = (
    GIMBAL_JOINT_NAMES,
    {
        "pitch": "c11_pitch_joint",
        "yaw": "c11_yaw_joint",
        "roll": "c11_roll_joint",
    },
)
DEPTH_CAMERA_INFO_TOPIC_SUFFIX = "/link/depth_camera_link/sensor/depth_camera/camera_info"
DEPTH_IMAGE_TOPIC_SUFFIX = "/link/depth_camera_link/sensor/depth_camera/depth_image"
# Gazebo derives the matrix from an SDF floating-point horizontal FOV.  The
# published value differs from the host-side double calculation by roughly
# 3.6e-5 px, so 1e-4 px covers transport/model quantization while remaining
# orders of magnitude below any meaningful calibration discrepancy.
CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX = 1.0e-4
CAMERA_DISTORTION_ABSOLUTE_TOLERANCE = 1.0e-12

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
    attitude_covariance_enu_rad2: tuple[float, ...]
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
        # A source epoch changes whenever a fault campaign invalidates the
        # buffered history.  Keeping the last callback stamp is intentional:
        # callbacks must remain strictly monotonic across a cache boundary,
        # while consumers must be able to distinguish pre-fault samples from
        # the first post-fault sample.
        self._epoch = 0

    @property
    def ready(self) -> bool:
        return bool(self._samples)

    @property
    def last_stamp_ns(self) -> int | None:
        return self._last_stamp_ns

    @property
    def epoch(self) -> int:
        """Return the cache epoch for this source."""

        return self._epoch

    def clear_samples(self) -> int:
        """Invalidate buffered samples without weakening timestamp ordering.

        A bounded source history is useful during normal operation, but a
        fault-injection window must not let an image consume a sample received
        before the fault.  Clearing only the deque makes that boundary
        explicit and retains ``_last_stamp_ns`` so a later callback cannot
        smuggle a duplicate or out-of-order sample across the boundary.
        """

        self._samples.clear()
        self._epoch += 1
        return self._epoch

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


def _joint_sample_from_message(message: Any) -> dict[str, object]:
    """Normalize JointStatePublisher axis-1 values using simulation time."""
    stamp_ns = _stamp_ns(message)
    joints: dict[str, dict[str, float]] = {}
    for joint in getattr(message, "joint", ()):
        name = str(getattr(joint, "name", ""))
        axis = getattr(joint, "axis1", None)
        if not name or axis is None:
            continue
        try:
            position = _finite(f"{name} position_rad", axis.position)
            velocity = _finite(f"{name} velocity_rad_s", axis.velocity)
        except (AttributeError, ValueError):
            continue
        joints[name] = {"position_rad": position, "velocity_rad_s": velocity}
    if stamp_ns <= 0 or not joints:
        raise ValueError("joint state must contain a positive timestamp and finite axis-1 samples")
    return {"stamp_ns": stamp_ns, "joints": joints}


def _frame_gimbal_state(
    sample: dict[str, object] | None, frame_stamp_ns: int
) -> dict[str, object] | None:
    """Bind a fresh, causal gimbal sample to an RGB simulation timestamp."""
    if sample is None:
        return None
    source_stamp_ns = int(sample["stamp_ns"])
    skew_ns = frame_stamp_ns - source_stamp_ns
    joints = sample["joints"]
    assert isinstance(joints, dict)
    joint_names = _complete_gimbal_joint_names(joints)
    if skew_ns < 0 or skew_ns > MAX_JOINT_STATE_SKEW_NS or joint_names is None:
        return None
    return {
        "timestamp_ns": frame_stamp_ns,
        "source_timestamp_ns": source_stamp_ns,
        "source_skew_ns": skew_ns,
        "position_rad": {
            axis: float(joints[name]["position_rad"])
            for axis, name in joint_names.items()
        },
        "velocity_rad_s": {
            axis: float(joints[name]["velocity_rad_s"])
            for axis, name in joint_names.items()
        },
        "source": "gazebo_joint_state",
        "fresh": True,
    }


def _complete_gimbal_joint_names(
    joints: dict[str, object],
) -> dict[str, str] | None:
    """Return one complete gimbal naming scheme present in a joint sample."""
    for names in GIMBAL_JOINT_NAME_SETS:
        if all(name in joints for name in names.values()):
            return names
    return None


def _frame_joint_state(
    sample: dict[str, object] | None,
    frame_stamp_ns: int,
    *,
    require_complete: bool = False,
) -> dict[str, object] | None:
    """Bind causal rotor/gimbal state to one RGB simulation timestamp.

    Rotor state is optional for fixed/legacy callers.  Gimbal callers pass
    ``require_complete`` so RGB is accepted only with all three gimbal axes
    and all four rotor joints.
    """
    if sample is None:
        return None
    source_stamp_ns = int(sample["stamp_ns"])
    skew_ns = int(frame_stamp_ns) - source_stamp_ns
    if skew_ns < 0 or skew_ns > MAX_JOINT_STATE_SKEW_NS:
        return None
    joints = sample["joints"]
    assert isinstance(joints, dict)
    result: dict[str, object] = {
        "timestamp_ns": int(frame_stamp_ns),
        "source_timestamp_ns": source_stamp_ns,
        "source_skew_ns": skew_ns,
        "gimbal_joint_state": None,
        "propeller_joint_state": None,
    }
    if all(name in joints for name in ROTOR_JOINT_NAMES):
        rotor_samples = [joints[name] for name in ROTOR_JOINT_NAMES]
        result["propeller_joint_state"] = {
            "timestamp_ns": int(frame_stamp_ns),
            "joint_names": list(ROTOR_JOINT_NAMES),
            "angles_rad": [float(value["position_rad"]) for value in rotor_samples],
            "spin_rad_s": [float(value["velocity_rad_s"]) for value in rotor_samples],
            "velocity_slowdown": ROTOR_VELOCITY_SLOWDOWN,
            "source": "gazebo_joint_state",
        }
    gimbal_joint_names = _complete_gimbal_joint_names(joints)
    if gimbal_joint_names is not None:
        result["gimbal_joint_state"] = {
            "timestamp_ns": int(frame_stamp_ns),
            "position_rad": {
                axis: float(joints[name]["position_rad"])
                for axis, name in gimbal_joint_names.items()
            },
            "velocity_rad_s": {
                axis: float(joints[name]["velocity_rad_s"])
                for axis, name in gimbal_joint_names.items()
            },
            "source": "gazebo_joint_state",
            "fresh": True,
        }
    if require_complete and (
        result["gimbal_joint_state"] is None
        or result["propeller_joint_state"] is None
    ):
        return None
    if result["gimbal_joint_state"] is None and result["propeller_joint_state"] is None:
        return None
    return result


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


def validate_camera_info_semantics(
    message: Any,
    *,
    expected_width: int,
    expected_height: int,
    expected_fx_px: float,
    expected_fy_px: float,
    expected_cx_px: float,
    expected_cy_px: float,
    expected_distortion: tuple[float, float, float, float, float],
) -> dict[str, object]:
    """Validate Gazebo CameraInfo against the repository pinhole calibration."""

    width = _uint("CameraInfo width", int(message.width), bits=32)
    height = _uint("CameraInfo height", int(message.height), bits=32)
    if (width, height) != (expected_width, expected_height):
        raise ValueError("CameraInfo geometry does not match the expected calibration")
    k = tuple(
        _finite(f"CameraInfo K[{index}]", value) for index, value in enumerate(message.intrinsics.k)
    )
    p = tuple(
        _finite(f"CameraInfo P[{index}]", value) for index, value in enumerate(message.projection.p)
    )
    r = tuple(
        _finite(f"CameraInfo R[{index}]", value)
        for index, value in enumerate(message.rectification_matrix)
    )
    distortion = tuple(
        _finite(f"CameraInfo D[{index}]", value) for index, value in enumerate(message.distortion.k)
    )
    if len(k) != 9 or len(p) != 12 or len(r) != 9 or len(distortion) != 5:
        raise ValueError("CameraInfo must contain K[9], P[12], R[9], and D[5]")
    if int(message.distortion.model) != 0:
        raise ValueError("CameraInfo distortion model must be PLUMB_BOB")
    expected_k = (
        expected_fx_px,
        0.0,
        expected_cx_px,
        0.0,
        expected_fy_px,
        expected_cy_px,
        0.0,
        0.0,
        1.0,
    )
    expected_p = (
        expected_fx_px,
        0.0,
        expected_cx_px,
        0.0,
        0.0,
        expected_fy_px,
        expected_cy_px,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
    )
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    for name, actual, expected, tolerance in (
        ("K", k, expected_k, CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX),
        ("P", p, expected_p, CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX),
        ("R", r, identity, CAMERA_DISTORTION_ABSOLUTE_TOLERANCE),
        ("D", distortion, expected_distortion, CAMERA_DISTORTION_ABSOLUTE_TOLERANCE),
    ):
        mismatch = next(
            (
                (index, observed, required)
                for index, (observed, required) in enumerate(zip(actual, expected, strict=True))
                if not math.isclose(observed, required, rel_tol=0.0, abs_tol=tolerance)
            ),
            None,
        )
        if mismatch is not None:
            index, observed, required = mismatch
            raise ValueError(
                f"CameraInfo {name}[{index}] does not match the expected calibration: "
                f"actual={observed!r}, expected={required!r}, absolute_tolerance={tolerance!r}"
            )
    return {
        "status": "validated_k_p_rectification_plumb_bob_distortion",
        "width": width,
        "height": height,
        "distortion_model": "PLUMB_BOB",
        "k": list(k),
        "p": list(p),
        "r": list(r),
        "d": list(distortion),
    }


def _parse_expected_camera_calibration(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("expected camera calibration is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "width_px",
        "height_px",
        "fx_px",
        "fy_px",
        "cx_px",
        "cy_px",
        "distortion",
    }:
        raise ValueError("expected camera calibration has an invalid field set")
    distortion = document["distortion"]
    if not isinstance(distortion, list) or len(distortion) != 5:
        raise ValueError("expected camera calibration distortion must contain five values")
    return {
        "expected_width": _uint("expected width", document["width_px"], bits=32),
        "expected_height": _uint("expected height", document["height_px"], bits=32),
        "expected_fx_px": _finite("expected fx", document["fx_px"]),
        "expected_fy_px": _finite("expected fy", document["fy_px"]),
        "expected_cx_px": _finite("expected cx", document["cx_px"]),
        "expected_cy_px": _finite("expected cy", document["cy_px"]),
        "expected_distortion": tuple(
            _finite(f"expected distortion[{index}]", value)
            for index, value in enumerate(distortion)
        ),
    }


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"depth contract JSON does not allow {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"depth contract JSON contains duplicate field: {key}")
        result[key] = value
    return result


def _depth_topic(*, world: str, model: str, suffix: str) -> str:
    if not isinstance(world, str) or not world or "/" in world:
        raise ValueError("world must be a single Gazebo name")
    if not isinstance(model, str) or not model or "/" in model:
        raise ValueError("model-instance must be a single Gazebo name")
    return f"/world/{world}/model/{model}{suffix}"


def _parse_depth_contract(
    raw: str | None,
    *,
    world: str,
    model: str,
) -> dict[str, object] | None:
    """Parse the optional depth binding without inventing a transport source.

    The contract is deliberately exact.  It is a run-time binding between one
    depth camera and the RGB camera; no sensor truth or flight-control field is
    accepted here.
    """

    if raw is None:
        return None
    try:
        document = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("depth contract is not strict JSON") from exc
    required = {
        "sensor_id",
        "source_topic",
        "calibration_id",
        "calibration_sha256",
        "registration_mode",
        "coordinate_space",
        "encoding",
        "distance_semantics",
        "width",
        "height",
        "minimum_depth_m",
        "maximum_depth_m",
        "fx_px",
        "fy_px",
        "cx_px",
        "cy_px",
    }
    if not isinstance(document, dict) or set(document) != required:
        missing = (
            sorted(required - set(document))
            if isinstance(document, dict)
            else sorted(required)
        )
        unknown = sorted(set(document) - required) if isinstance(document, dict) else []
        raise ValueError(
            "depth contract field set mismatch: "
            f"missing={missing}, unknown={unknown}"
        )
    text_fields = ("sensor_id", "source_topic", "calibration_id")
    for name in text_fields:
        value = document[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"depth contract {name} must be a non-empty string")
    expected_topic = _depth_topic(world=world, model=model, suffix=DEPTH_IMAGE_TOPIC_SUFFIX)
    if document["source_topic"] != expected_topic:
        raise ValueError(
            "depth contract source_topic must be the exact scoped depth topic: "
            f"{expected_topic}"
        )
    calibration_sha256 = document["calibration_sha256"]
    if (
        not isinstance(calibration_sha256, str)
        or len(calibration_sha256) != 64
        or calibration_sha256 != calibration_sha256.lower()
        or any(character not in "0123456789abcdef" for character in calibration_sha256)
    ):
        raise ValueError("depth contract calibration_sha256 must be lowercase SHA-256")
    exact_text = {
        "registration_mode": "registered_to_rgb",
        "coordinate_space": "rgb_pixel_aligned",
        "encoding": "FLOAT32_M",
        "distance_semantics": "optical_axis_z_m",
    }
    for name, expected in exact_text.items():
        if document[name] != expected:
            raise ValueError(f"depth contract {name} must be {expected!r}")
    normalized = dict(document)
    for name in ("width", "height"):
        value = document[name]
        normalized[name] = _uint(f"depth contract {name}", value, bits=32)
        if normalized[name] <= 0 or normalized[name] > MAX_DIMENSION:
            raise ValueError(f"depth contract {name} is outside the safety bound")
    for name in (
        "minimum_depth_m",
        "maximum_depth_m",
        "fx_px",
        "fy_px",
        "cx_px",
        "cy_px",
    ):
        normalized[name] = _finite(f"depth contract {name}", document[name])
    if not 0.0 < normalized["minimum_depth_m"] < normalized["maximum_depth_m"]:
        raise ValueError("depth contract depth range is invalid")
    if normalized["fx_px"] <= 0.0 or normalized["fy_px"] <= 0.0:
        raise ValueError("depth contract focal lengths must be positive")
    normalized["depth_camera_info_topic"] = _depth_topic(
        world=world, model=model, suffix=DEPTH_CAMERA_INFO_TOPIC_SUFFIX
    )
    return normalized


def _validate_depth_contract_matches_rgb(
    contract: dict[str, object] | None,
    expected_camera_calibration: dict[str, object] | None,
) -> None:
    if contract is None or expected_camera_calibration is None:
        return
    pairs = (
        ("width", "expected_width"),
        ("height", "expected_height"),
        ("fx_px", "expected_fx_px"),
        ("fy_px", "expected_fy_px"),
        ("cx_px", "expected_cx_px"),
        ("cy_px", "expected_cy_px"),
    )
    for depth_name, rgb_name in pairs:
        if not math.isclose(
            float(contract[depth_name]),
            float(expected_camera_calibration[rgb_name]),
            rel_tol=0.0,
            abs_tol=CAMERA_INTRINSICS_ABSOLUTE_TOLERANCE_PX,
        ):
            raise ValueError(
                "depth contract geometry/intrinsics do not match the RGB calibration: "
                f"{depth_name}={contract[depth_name]!r}, "
                f"{rgb_name}={expected_camera_calibration[rgb_name]!r}"
            )


def _runtime_pixel_format_name(message: Any) -> str:
    """Resolve ``Image.pixel_format_type`` through Gazebo's runtime descriptor."""

    descriptor = getattr(message, "DESCRIPTOR", None)
    fields = getattr(descriptor, "fields_by_name", None)
    field = fields.get("pixel_format_type") if fields is not None else None
    enum_type = getattr(field, "enum_type", None)
    values_by_number = getattr(enum_type, "values_by_number", None)
    value = None
    if values_by_number is not None:
        value = values_by_number.get(int(message.pixel_format_type))
    name = getattr(value, "name", None)
    if not isinstance(name, str) or not name:
        raise ValueError("depth Image pixel_format_type has no runtime descriptor name")
    return name


def _normalize_depth_float32(message: Any, *, contract: dict[str, object]) -> bytes:
    """Copy a packed little-endian R_FLOAT32 image without filling invalid pixels."""

    if _runtime_pixel_format_name(message) != "R_FLOAT32":
        raise ValueError("depth Image pixel_format_type must be R_FLOAT32")
    width = _uint("depth image width", int(message.width), bits=32)
    height = _uint("depth image height", int(message.height), bits=32)
    if (width, height) != (contract["width"], contract["height"]):
        raise ValueError("depth Image geometry does not match the depth contract")
    expected_stride = width * 4
    if int(message.step) != expected_stride:
        raise ValueError(
            "depth Image step must be packed little-endian FLOAT32: "
            f"expected {expected_stride}, received {message.step}"
        )
    try:
        view = memoryview(message.data)
    except (TypeError, ValueError) as exc:
        raise ValueError("depth Image data must be bytes-like") from exc
    if view.ndim != 1 or view.itemsize != 1:
        view = view.cast("B")
    if not view.contiguous:
        raise ValueError("depth Image data must be contiguous")
    expected_bytes = expected_stride * height
    if expected_bytes > MAX_PAYLOAD_BYTES or view.nbytes != expected_bytes:
        raise ValueError("depth Image payload does not match packed geometry")
    # The exact bytes are retained, including zero/invalid pixels.  Replacing
    # them with a fabricated value would turn a missing measurement into data.
    return bytes(view)


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


def _validate_attitude_covariance(values: object) -> tuple[float, ...]:
    """Validate one symmetric positive-semidefinite 3x3 covariance."""

    try:
        covariance = tuple(values)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("Odometry attitude covariance must be an iterable") from exc
    if len(covariance) != 9:
        raise ValueError("Odometry attitude covariance must contain exactly 9 values")
    normalized = tuple(
        _finite(f"attitude_covariance[{index}]", value) for index, value in enumerate(covariance)
    )
    scale = max(1.0, *(abs(value) for value in normalized))
    tolerance = 1.0e-10 * scale
    for row in range(3):
        if normalized[row * 3 + row] < 0.0:
            raise ValueError("Odometry attitude covariance diagonal cannot be negative")
        for column in range(row + 1, 3):
            if abs(normalized[row * 3 + column] - normalized[column * 3 + row]) > tolerance:
                raise ValueError("Odometry attitude covariance must be symmetric")
    # Sylvester's criterion for PSD 3x3 matrices: every principal minor is
    # non-negative.  This catches impossible cross-correlation even when each
    # diagonal is individually plausible.
    a, b, c, _d, e, f, _g, _h, i = normalized
    principal_minors = (a, e, i, a * e - b * b, a * i - c * c, e * i - f * f)
    determinant = a * (e * i - f * f) - b * (b * i - c * f) + c * (b * f - c * e)
    if any(value < -tolerance for value in (*principal_minors, determinant)):
        raise ValueError("Odometry attitude covariance must be positive semidefinite")
    return normalized


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
    attitude_covariance = _validate_attitude_covariance(header.attitude_covariance_enu_rad2)
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
        attitude_covariance_enu_rad2=attitude_covariance,
        flags=flags,
        sha256=digest,
    )


def pack_header(header: SensorWireHeader) -> bytes:
    """Validate and pack one v3 fixed header."""

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
        *value.attitude_covariance_enu_rad2,
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
            covariance_xx,
            covariance_xy,
            covariance_xz,
            covariance_yx,
            covariance_yy,
            covariance_yz,
            covariance_zx,
            covariance_zy,
            covariance_zz,
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
            attitude_covariance_enu_rad2=(
                covariance_xx,
                covariance_xy,
                covariance_xz,
                covariance_yx,
                covariance_yy,
                covariance_yz,
                covariance_zx,
                covariance_zy,
                covariance_zz,
            ),
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
    MDGZSIM3/OpenCV/QGC contract. The live callback therefore performs color
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
    attitude_covariance_enu_rad2: tuple[float, ...],
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
        attitude_covariance_enu_rad2=attitude_covariance_enu_rad2,
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
    pose_with_covariance = getattr(message, "pose_with_covariance", None)
    pose = getattr(pose_with_covariance, "pose", None)
    twist = getattr(getattr(message, "twist_with_covariance", None), "twist", None)
    position = getattr(pose, "position", None)
    orientation = getattr(pose, "orientation", None)
    linear = getattr(twist, "linear", None)
    if position is None or orientation is None or linear is None:
        raise ValueError("Odometry message is missing pose or linear velocity")
    odometry = _validate_odometry(
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
    covariance_message = getattr(pose_with_covariance, "covariance", None)
    covariance_data = getattr(covariance_message, "data", None)
    try:
        covariance_6x6 = tuple(covariance_data)
    except TypeError as exc:
        raise ValueError("Odometry pose covariance data is missing") from exc
    if len(covariance_6x6) != 36:
        raise ValueError("Odometry pose covariance must contain exactly 36 values")
    angular = tuple(
        covariance_6x6[row * 6 + column] for row in range(3, 6) for column in range(3, 6)
    )
    return (*odometry, *_validate_attitude_covariance(angular))


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
    parser.add_argument("--expected-camera-calibration-json")
    parser.add_argument(
        "--depth-contract-json",
        help=(
            "strict JSON binding for an RGB-registered Gazebo R_FLOAT32 depth camera; "
            "when configured, each accepted RGB record is followed by MDGZDEP1"
        ),
    )
    parser.add_argument(
        "--fault-source",
        choices=FAULT_SOURCES,
        help=(
            "drop one Gazebo sensor source for a bounded image-index window; "
            "used only for explicit external-SIM fault campaigns"
        ),
    )
    parser.add_argument("--fault-start-image", type=int, default=0)
    parser.add_argument("--fault-duration-images", type=int, default=0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if not 1 <= args.frames <= MAX_FRAME_COUNT:
        raise SystemExit(f"--frames must be between 1 and {MAX_FRAME_COUNT}")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be a positive finite number")
    if args.fault_source is None:
        if args.fault_start_image != 0 or args.fault_duration_images != 0:
            raise SystemExit("fault window options require --fault-source")
    else:
        if args.fault_start_image < 1:
            raise SystemExit("--fault-start-image must be positive")
        if args.fault_duration_images <= 0:
            raise SystemExit("--fault-duration-images must be positive with --fault-source")
    try:
        expected_camera_calibration = _parse_expected_camera_calibration(
            args.expected_camera_calibration_json
        )
        depth_contract = _parse_depth_contract(
            args.depth_contract_json,
            world=args.world,
            model=args.model_instance,
        )
        _validate_depth_contract_matches_rgb(depth_contract, expected_camera_calibration)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    # Gazebo bindings exist only inside the pinned runtime image.
    from gz.msgs10.camera_info_pb2 import CameraInfo
    from gz.msgs10.image_pb2 import Image
    from gz.msgs10.navsat_pb2 import NavSat
    from gz.msgs10.odometry_with_covariance_pb2 import OdometryWithCovariance
    from gz.transport13 import Node

    model = args.model_instance
    world = args.world
    gimballed_airframe = "gimbal" in model.lower()
    # x500 and x500_gimbal both expose rotor joints.  The subscription is
    # optional for non-gimbal models so fixed/older episodes remain usable
    # when the transport sidecar is absent.
    rotor_joint_state_enabled = "x500" in model.lower() or gimballed_airframe
    if rotor_joint_state_enabled:
        from gz.msgs10.model_pb2 import Model
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
    joint_state_topic = f"/world/{world}/model/{model}/joint_state"
    depth_topic = (
        str(depth_contract["source_topic"]) if depth_contract is not None else None
    )
    depth_camera_info_topic = (
        str(depth_contract["depth_camera_info_topic"]) if depth_contract is not None else None
    )

    state_lock = threading.Lock()
    image_lock = threading.Lock()
    error_event = threading.Event()
    packets: queue.Queue[bytes] = queue.Queue(maxsize=min(args.frames, 4))
    source_buffers = {
        "camera_info": CausalSourceBuffer("CameraInfo"),
        "navsat": CausalSourceBuffer("NavSat"),
        "odometry": CausalSourceBuffer("Odometry"),
    }
    if rotor_joint_state_enabled:
        source_buffers["joint"] = CausalSourceBuffer("JointStatePublisher", maximum_samples=128)
    if depth_contract is not None:
        source_buffers.update(
            {
                "depth_camera_info": CausalSourceBuffer(
                    "DepthCameraInfo", maximum_samples=MAX_DEPTH_HISTORY_SAMPLES
                ),
                "depth": CausalSourceBuffer(
                    "DepthImage", maximum_samples=MAX_DEPTH_HISTORY_SAMPLES
                ),
            }
        )
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
        "gimbal": 0,
        "gimbal_missing_or_stale_rejections": 0,
        "propeller_frames": 0,
        "propeller_missing_or_stale": 0,
        "odometry_invalid": 0,
        "odometry_startup_velocity_rejected": 0,
        "fault_dropped_image": 0,
        "fault_dropped_navsat": 0,
        "fault_dropped_odometry": 0,
        "fault_window_rejected_navsat": 0,
        "fault_window_rejected_odometry": 0,
        "fault_stale_navsat_rejections": 0,
        "fault_stale_odometry_rejections": 0,
        "depth_camera_info": 0,
        "depth": 0,
        "depth_missing_for_rgb": 0,
        "depth_stale_for_rgb": 0,
    }
    accepted = 0
    odometry_timestamp_frame_count = 0
    last_sensor_stamp_ns: int | None = None
    first_error: str | None = None
    camera_info_semantic_validation: dict[str, object] | None = None
    fault_last_dropped_source_stamp_ns: int | None = None
    fault_source_epoch: int | None = None
    fault_cache_cleared = False
    fault_recovered_frame_count = 0
    fault_window_rejected_frame_count = 0
    fault_stale_source_rejection_count = 0
    fault_emitted_during_window_count = 0
    fault_timeline: list[dict[str, object]] = []
    fault_timeline_overflow = False
    # Keep one bounded record per emitted frame plus finite diagnostic
    # headroom. Dynamic-source callback churn is counted separately and only
    # its first recovery-boundary records are retained in the timeline.
    fault_timeline_limit = MAX_FAULT_TIMELINE_EVENTS + max(0, args.frames)
    fault_fresh_source_recorded = False
    fault_window_rejection_recorded = False
    fault_waiting_rejection_recorded = False
    fault_cached_rejection_recorded = False

    def record_fault_timeline_locked(event: str, **fields: object) -> None:
        nonlocal fault_timeline_overflow
        if args.fault_source is None:
            return
        if len(fault_timeline) >= fault_timeline_limit:
            fault_timeline_overflow = True
            return
        fault_timeline.append(
            {
                "event": event,
                "source": args.fault_source,
                **fields,
            }
        )

    def fault_active(source: str) -> bool:
        """Return whether *source* is dropped for the current image window.

        The window is keyed to the received-image counter rather than wall
        time.  That keeps a campaign reproducible across host load while the
        normal causal-source freshness gates still decide whether recovery is
        acceptable.
        """

        if args.fault_source != source:
            return False
        image_index = counts["image"]
        return args.fault_start_image <= image_index < (
            args.fault_start_image + args.fault_duration_images
        )

    def fail(message: str) -> None:
        nonlocal first_error
        with state_lock:
            if first_error is None:
                first_error = message
        error_event.set()

    def on_depth_camera_info(message: CameraInfo) -> None:
        if depth_contract is None:
            return
        try:
            stamp_ns = _stamp_ns(message)
            semantic_validation = validate_camera_info_semantics(
                message,
                expected_width=int(depth_contract["width"]),
                expected_height=int(depth_contract["height"]),
                expected_fx_px=float(depth_contract["fx_px"]),
                expected_fy_px=float(depth_contract["fy_px"]),
                expected_cx_px=float(depth_contract["cx_px"]),
                expected_cy_px=float(depth_contract["cy_px"]),
                expected_distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
            )
            with state_lock:
                source_buffers["depth_camera_info"].append(
                    {
                        "stamp_ns": stamp_ns,
                        "width": int(semantic_validation["width"]),
                        "height": int(semantic_validation["height"]),
                    }
                )
                counts["depth_camera_info"] += 1
        except Exception as exc:
            fail(f"Depth CameraInfo rejected: {exc}")

    def on_depth_image(message: Image) -> None:
        if depth_contract is None:
            return
        try:
            stamp_ns = _stamp_ns(message)
            payload = _normalize_depth_float32(message, contract=depth_contract)
            with state_lock:
                source_buffers["depth"].append(
                    {
                        "stamp_ns": stamp_ns,
                        "width": int(depth_contract["width"]),
                        "height": int(depth_contract["height"]),
                        "stride_bytes": int(depth_contract["width"]) * 4,
                        "payload": payload,
                    }
                )
                counts["depth"] += 1
        except Exception as exc:
            fail(f"Depth Image rejected: {exc}")

    def on_camera_info(message: CameraInfo) -> None:
        nonlocal camera_info_semantic_validation
        try:
            stamp_ns = _stamp_ns(message)
            width = int(message.width)
            height = int(message.height)
            if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise ValueError("CameraInfo contains invalid geometry")
            semantic_validation = None
            if expected_camera_calibration is not None:
                semantic_validation = validate_camera_info_semantics(
                    message,
                    **expected_camera_calibration,
                )
            elif depth_contract is not None:
                # A depth binding is explicitly RGB-pixel registered.  When
                # no separate RGB calibration argument is supplied, use the
                # contract's intrinsic pinhole values to validate RGB too;
                # otherwise a mismatched RGB/depth pair could pass silently.
                semantic_validation = validate_camera_info_semantics(
                    message,
                    expected_width=int(depth_contract["width"]),
                    expected_height=int(depth_contract["height"]),
                    expected_fx_px=float(depth_contract["fx_px"]),
                    expected_fy_px=float(depth_contract["fy_px"]),
                    expected_cx_px=float(depth_contract["cx_px"]),
                    expected_cy_px=float(depth_contract["cy_px"]),
                    expected_distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
                )
            with state_lock:
                source_buffers["camera_info"].append(
                    {
                        "stamp_ns": stamp_ns,
                        "width": width,
                        "height": height,
                    }
                )
                counts["camera_info"] += 1
                if semantic_validation is not None:
                    camera_info_semantic_validation = semantic_validation
        except Exception as exc:  # Gazebo callbacks must fail through the main thread.
            fail(f"CameraInfo rejected: {exc}")

    def on_navsat(message: NavSat) -> None:
        nonlocal fault_last_dropped_source_stamp_ns, fault_source_epoch
        nonlocal fault_cache_cleared
        nonlocal fault_fresh_source_recorded
        try:
            stamp_ns = _stamp_ns(message)
            latitude, longitude, altitude = _validate_navsat(
                message.latitude_deg,
                message.longitude_deg,
                message.altitude,
            )
            with state_lock:
                if fault_active("navsat"):
                    previous_stamp = source_buffers["navsat"].last_stamp_ns
                    if (
                        previous_stamp is not None and stamp_ns <= previous_stamp
                    ) or (
                        fault_last_dropped_source_stamp_ns is not None
                        and stamp_ns <= fault_last_dropped_source_stamp_ns
                    ):
                        raise ValueError(
                            "NavSat timestamps must remain strictly increasing "
                            "during fault injection"
                        )
                    if not fault_cache_cleared:
                        fault_source_epoch = source_buffers["navsat"].clear_samples()
                        fault_cache_cleared = True
                    fault_last_dropped_source_stamp_ns = stamp_ns
                    counts["fault_dropped_navsat"] += 1
                    record_fault_timeline_locked(
                        "source_dropped",
                        received_image_index=counts["image"],
                        source_stamp_ns=stamp_ns,
                        fault_active=True,
                        wire_sequence=None,
                        source_epoch=source_buffers["navsat"].epoch,
                    )
                    return
                if (
                    args.fault_source == "navsat"
                    and fault_last_dropped_source_stamp_ns is not None
                    and not fault_fresh_source_recorded
                ):
                    if stamp_ns <= fault_last_dropped_source_stamp_ns:
                        raise ValueError(
                            "NavSat timestamps must be strictly newer than "
                            "the last dropped fault sample"
                        )
                    record_fault_timeline_locked(
                        "source_fresh_after_window",
                        received_image_index=counts["image"],
                        source_stamp_ns=stamp_ns,
                        fault_active=False,
                        wire_sequence=None,
                        source_epoch=source_buffers["navsat"].epoch,
                    )
                    fault_fresh_source_recorded = True
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
        nonlocal fault_last_dropped_source_stamp_ns, fault_source_epoch
        nonlocal fault_cache_cleared
        nonlocal fault_fresh_source_recorded
        try:
            stamp_ns = _stamp_ns(message)
            if stamp_ns <= 0:
                raise ValueError("Odometry timestamp must be positive when present")
            odometry = _odometry_from_message(message)
            with state_lock:
                if fault_active("odometry"):
                    previous_stamp = source_buffers["odometry"].last_stamp_ns
                    if (
                        previous_stamp is not None and stamp_ns <= previous_stamp
                    ) or (
                        fault_last_dropped_source_stamp_ns is not None
                        and stamp_ns <= fault_last_dropped_source_stamp_ns
                    ):
                        raise ValueError(
                            "Odometry timestamps must remain strictly increasing "
                            "during fault injection"
                        )
                    if not fault_cache_cleared:
                        fault_source_epoch = source_buffers["odometry"].clear_samples()
                        fault_cache_cleared = True
                    fault_last_dropped_source_stamp_ns = stamp_ns
                    counts["fault_dropped_odometry"] += 1
                    record_fault_timeline_locked(
                        "source_dropped",
                        received_image_index=counts["image"],
                        source_stamp_ns=stamp_ns,
                        fault_active=True,
                        wire_sequence=None,
                        source_epoch=source_buffers["odometry"].epoch,
                    )
                    return
                if (
                    args.fault_source == "odometry"
                    and fault_last_dropped_source_stamp_ns is not None
                    and not fault_fresh_source_recorded
                ):
                    if stamp_ns <= fault_last_dropped_source_stamp_ns:
                        raise ValueError(
                            "Odometry timestamps must be strictly newer than "
                            "the last dropped fault sample"
                        )
                    record_fault_timeline_locked(
                        "source_fresh_after_window",
                        received_image_index=counts["image"],
                        source_stamp_ns=stamp_ns,
                        fault_active=False,
                        wire_sequence=None,
                        source_epoch=source_buffers["odometry"].epoch,
                    )
                    fault_fresh_source_recorded = True
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
                        "attitude_covariance_enu_rad2": odometry[10:19],
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

    def on_joint_state(message: Model) -> None:
        try:
            sample = _joint_sample_from_message(message)
            with state_lock:
                source_buffers["joint"].append(sample)
                counts["gimbal"] += 1
        except Exception as exc:
            with state_lock:
                counts["gimbal_invalid"] = counts.get("gimbal_invalid", 0) + 1
            _diagnostic("gazebo_sensor_stream_joint_state_dropped", reason=str(exc))

    def on_image(message: Image) -> None:
        nonlocal accepted, last_sensor_stamp_ns, odometry_timestamp_frame_count
        nonlocal fault_recovered_frame_count
        nonlocal fault_window_rejected_frame_count
        nonlocal fault_stale_source_rejection_count
        nonlocal fault_emitted_during_window_count
        nonlocal fault_window_rejection_recorded
        nonlocal fault_waiting_rejection_recorded
        nonlocal fault_cached_rejection_recorded
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
                    if fault_active("image"):
                        counts["fault_dropped_image"] += 1
                        fault_window_rejected_frame_count += 1
                        record_fault_timeline_locked(
                            "frame_rejected_fault_window",
                            received_image_index=counts["image"],
                            source_stamp_ns=None,
                            fault_active=True,
                            wire_sequence=None,
                        )
                        return
                    if error_event.is_set() or accepted >= args.frames:
                        return
                    # A bounded dynamic-source fault must never silently reuse
                    # the last pre-fault sample. Reject the whole image while
                    # the window is active, then keep rejecting until a source
                    # callback with a strictly newer stamp has arrived.
                    dynamic_fault_source = args.fault_source
                    if dynamic_fault_source in {"navsat", "odometry"} and fault_active(
                        dynamic_fault_source
                    ):
                        counts[f"fault_window_rejected_{dynamic_fault_source}"] += 1
                        fault_window_rejected_frame_count += 1
                        if not fault_window_rejection_recorded:
                            record_fault_timeline_locked(
                                "frame_rejected_fault_window",
                                received_image_index=counts["image"],
                                source_stamp_ns=None,
                                fault_active=True,
                                wire_sequence=None,
                            )
                            fault_window_rejection_recorded = True
                        return
                    selected = {
                        name: buffer.latest_at_or_before(stamp_ns)
                        for name, buffer in source_buffers.items()
                        if name in {"camera_info", "navsat", "odometry"}
                    }
                    if rotor_joint_state_enabled:
                        selected["joint"] = source_buffers["joint"].latest_at_or_before(stamp_ns)
                    required_selected = {
                        name: sample
                        for name, sample in selected.items()
                        if name != "joint" or gimballed_airframe
                    }
                    if any(sample is None for sample in required_selected.values()):
                        counts["ignored_before_ready"] += 1
                        if gimballed_airframe and selected.get("joint") is None:
                            counts["gimbal_missing_or_stale_rejections"] += 1
                        if (
                            dynamic_fault_source in {"navsat", "odometry"}
                            and fault_cache_cleared
                            and fault_last_dropped_source_stamp_ns is not None
                            and selected[dynamic_fault_source] is None
                        ):
                            # Clearing the source history is stronger than
                            # merely comparing the selected sample's stamp:
                            # there is no pre-fault sample left to select.
                            # Keep this as an explicit stale-source rejection
                            # in the timeline so an external verifier can
                            # prove that recovery waited for a fresh callback.
                            counts[
                                f"fault_stale_{dynamic_fault_source}_rejections"
                            ] += 1
                            fault_stale_source_rejection_count += 1
                            if not fault_waiting_rejection_recorded:
                                record_fault_timeline_locked(
                                    "frame_rejected_waiting_for_fresh_source",
                                    received_image_index=counts["image"],
                                    source_stamp_ns=None,
                                    dropped_source_stamp_ns=(
                                        fault_last_dropped_source_stamp_ns
                                    ),
                                    fault_active=False,
                                    wire_sequence=None,
                                    source_epoch=fault_source_epoch,
                                )
                                fault_waiting_rejection_recorded = True
                        if any(
                            source_buffers[name].ready and sample is None
                            for name, sample in required_selected.items()
                        ):
                            counts["future_source_rejections"] += 1
                        return
                    camera = selected["camera_info"]
                    navsat = selected["navsat"]
                    odometry = selected["odometry"]
                    assert camera is not None and navsat is not None and odometry is not None
                    joint_state = (
                        _frame_joint_state(
                            selected.get("joint"),
                            stamp_ns,
                            require_complete=gimballed_airframe,
                        )
                        if rotor_joint_state_enabled
                        else None
                    )
                    gimbal_state = (
                        joint_state.get("gimbal_joint_state")
                        if joint_state is not None
                        else None
                    )
                    if gimballed_airframe and gimbal_state is None:
                        counts["gimbal_missing_or_stale_rejections"] += 1
                        return
                    propeller_state = (
                        joint_state.get("propeller_joint_state")
                        if joint_state is not None
                        else None
                    )
                    if propeller_state is not None:
                        counts["propeller_frames"] += 1
                    elif rotor_joint_state_enabled:
                        counts["propeller_missing_or_stale"] += 1
                    if dynamic_fault_source in {"navsat", "odometry"}:
                        selected_source = selected[dynamic_fault_source]
                        dropped_stamp = fault_last_dropped_source_stamp_ns
                        if (
                            dropped_stamp is not None
                            and int(selected_source["stamp_ns"]) <= dropped_stamp
                        ):
                            counts[f"fault_stale_{dynamic_fault_source}_rejections"] += 1
                            fault_stale_source_rejection_count += 1
                            if not fault_cached_rejection_recorded:
                                record_fault_timeline_locked(
                                    "frame_rejected_cached_source",
                                    received_image_index=counts["image"],
                                    source_stamp_ns=int(selected_source["stamp_ns"]),
                                    dropped_source_stamp_ns=dropped_stamp,
                                    fault_active=False,
                                    wire_sequence=None,
                                    source_epoch=fault_source_epoch,
                                )
                                fault_cached_rejection_recorded = True
                            return
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
                    depth_sample = None
                    if depth_contract is not None:
                        depth_sample = source_buffers["depth"].latest_at_or_before(stamp_ns)
                        if depth_sample is None:
                            counts["depth_missing_for_rgb"] += 1
                            return
                        depth_info = source_buffers["depth_camera_info"].latest_at_or_before(
                            int(depth_sample["stamp_ns"])
                        )
                        if depth_info is None:
                            counts["depth_missing_for_rgb"] += 1
                            return
                        depth_age_ns = stamp_ns - int(depth_sample["stamp_ns"])
                        if depth_age_ns > DEPTH_MAX_SOURCE_SKEW_NS:
                            counts["depth_stale_for_rgb"] += 1
                            return
                        if int(depth_sample["stamp_ns"]) > stamp_ns:
                            counts["depth_missing_for_rgb"] += 1
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
                    attitude_covariance_enu_rad2=tuple(
                        float(value) for value in odometry["attitude_covariance_enu_rad2"]
                    ),
                    bgr_payload=bgr_payload,
                )
                if depth_contract is not None:
                    if depth_sample is None:  # pragma: no cover - guarded above
                        raise ValueError("depth sample disappeared before packet assembly")
                    rgb_header = unpack_header(packet[:WIRE_HEADER_BYTES])
                    depth_metadata = {
                        "sequence": sequence,
                        "rgb_sensor_stamp_ns": stamp_ns,
                        "rgb_record_sha256": rgb_header.sha256.hex(),
                        "source_stamp_ns": int(depth_sample["stamp_ns"]),
                        "source_topic": str(depth_contract["source_topic"]),
                        "calibration_id": str(depth_contract["calibration_id"]),
                        "calibration_sha256": str(depth_contract["calibration_sha256"]),
                        "registration_mode": str(depth_contract["registration_mode"]),
                        "coordinate_space": str(depth_contract["coordinate_space"]),
                        "encoding": str(depth_contract["encoding"]),
                        "distance_semantics": str(depth_contract["distance_semantics"]),
                        "width": int(depth_sample["width"]),
                        "height": int(depth_sample["height"]),
                        "stride_bytes": int(depth_sample["stride_bytes"]),
                        "minimum_depth_m": float(depth_contract["minimum_depth_m"]),
                        "maximum_depth_m": float(depth_contract["maximum_depth_m"]),
                        "fx_px": float(depth_contract["fx_px"]),
                        "fy_px": float(depth_contract["fy_px"]),
                        "cx_px": float(depth_contract["cx_px"]),
                        "cy_px": float(depth_contract["cy_px"]),
                    }
                    if joint_state is not None:
                        depth_metadata["joint_state"] = joint_state
                    depth_record = build_depth_wire_record(
                        metadata=depth_metadata,
                        payload=bytes(depth_sample["payload"]),
                    )
                else:
                    depth_record = b""
                with state_lock:
                    if (
                        args.fault_source is not None
                        and fault_active(args.fault_source)
                    ):
                        fault_emitted_during_window_count += 1
                    if args.fault_source is not None:
                        record_fault_timeline_locked(
                            "wire_emitted",
                            received_image_index=counts["image"],
                            source_stamp_ns=(
                                None
                                if args.fault_source == "image"
                                else int(selected[args.fault_source]["stamp_ns"])
                            ),
                            dropped_source_stamp_ns=(
                                None
                                if args.fault_source == "image"
                                else fault_last_dropped_source_stamp_ns
                            ),
                            fault_active=fault_active(args.fault_source),
                            wire_sequence=sequence,
                            source_epoch=(
                                None
                                if args.fault_source == "image"
                                else source_buffers[args.fault_source].epoch
                            ),
                        )
                    if (
                        args.fault_source is not None
                        and fault_recovered_frame_count == 0
                        and counts["image"] >= (
                            args.fault_start_image + args.fault_duration_images
                        )
                        and counts[f"fault_dropped_{args.fault_source}"] > 0
                        and (
                            args.fault_source == "image"
                            or (
                                fault_last_dropped_source_stamp_ns is not None
                                and int(selected[args.fault_source]["stamp_ns"])
                                > fault_last_dropped_source_stamp_ns
                            )
                        )
                    ):
                        record_fault_timeline_locked(
                            "wire_emitted_after_recovery",
                            received_image_index=counts["image"],
                            source_stamp_ns=(
                                None
                                if args.fault_source == "image"
                                else int(selected[args.fault_source]["stamp_ns"])
                            ),
                            fault_active=False,
                            wire_sequence=sequence,
                            source_epoch=(
                                None
                                if args.fault_source == "image"
                                else source_buffers[args.fault_source].epoch
                            ),
                        )
                    accepted += 1
                    odometry_timestamp_frame_count += 1
                    if (
                        args.fault_source is not None
                        and counts["image"] >= (
                            args.fault_start_image + args.fault_duration_images
                        )
                        and counts[f"fault_dropped_{args.fault_source}"] > 0
                        and (
                            args.fault_source == "image"
                            or (
                                fault_last_dropped_source_stamp_ns is not None
                                and int(selected[args.fault_source]["stamp_ns"])
                                > fault_last_dropped_source_stamp_ns
                            )
                        )
                    ):
                        fault_recovered_frame_count += 1
                if gimballed_airframe:
                    _diagnostic(
                        "gazebo_sensor_stream_gimbal_frame_state",
                        sequence=sequence,
                        timestamp_ns=int(gimbal_state["timestamp_ns"]),
                        source_timestamp_ns=int(gimbal_state["source_timestamp_ns"]),
                        source_skew_ns=int(gimbal_state["source_skew_ns"]),
                        position_rad=gimbal_state["position_rad"],
                        velocity_rad_s=gimbal_state["velocity_rad_s"],
                        source="gazebo_joint_state",
                        fresh=True,
                    )
                if propeller_state is not None:
                    _diagnostic(
                        "gazebo_sensor_stream_propeller_frame_state",
                        sequence=sequence,
                        timestamp_ns=int(propeller_state["timestamp_ns"]),
                        source_timestamp_ns=int(joint_state["source_timestamp_ns"]),
                        source_skew_ns=int(joint_state["source_skew_ns"]),
                        joint_names=propeller_state["joint_names"],
                        angles_rad=propeller_state["angles_rad"],
                        spin_rad_s=propeller_state["spin_rad_s"],
                        velocity_slowdown=int(propeller_state["velocity_slowdown"]),
                        source="gazebo_joint_state",
                    )
                # Queue one atomic stdout blob: once an RGB frame is accepted,
                # its companion must be the immediately following record.
                packets.put(packet + depth_record)
            except Exception as exc:
                fail(f"Image rejected: {exc}")

    node = Node()
    required_subscriptions: list[tuple[type[object], str, object]] = []
    if depth_contract is not None:
        assert depth_topic is not None and depth_camera_info_topic is not None
        # Subscribe depth first so the first RGB callback cannot race ahead of
        # the optional causal source during deterministic test/container start.
        required_subscriptions.extend(
            [
                (CameraInfo, depth_camera_info_topic, on_depth_camera_info),
                (Image, depth_topic, on_depth_image),
            ]
        )
    required_subscriptions.extend(
        [
            (Image, image_topic, on_image),
            (CameraInfo, camera_info_topic, on_camera_info),
            (NavSat, navsat_topic, on_navsat),
            (OdometryWithCovariance, odometry_topic, on_odometry),
        ]
    )
    if rotor_joint_state_enabled:
        required_subscriptions.append((Model, joint_state_topic, on_joint_state))
    subscription_topics = tuple(topic for _message_type, topic, _callback in required_subscriptions)
    for message_type, topic, callback in required_subscriptions:
        if not node.subscribe(message_type, topic, callback):
            _diagnostic("gazebo_sensor_stream_subscription_failed", topic=topic)
            if topic == joint_state_topic and not gimballed_airframe:
                # Rotor state is an enrichment sidecar.  A missing topic must
                # not make fixed/legacy RGB episodes fail closed.
                continue
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
        gimballed_airframe=gimballed_airframe,
        rotor_joint_state_enabled=rotor_joint_state_enabled,
        joint_state_topic=joint_state_topic if rotor_joint_state_enabled else None,
        maximum_joint_state_skew_ns=MAX_JOINT_STATE_SKEW_NS if rotor_joint_state_enabled else None,
        rotor_joint_names=ROTOR_JOINT_NAMES if rotor_joint_state_enabled else None,
        gimbal_joint_name_sets=GIMBAL_JOINT_NAME_SETS if gimballed_airframe else None,
        depth_enabled=depth_contract is not None,
        depth_topic=depth_topic,
        depth_camera_info_topic=depth_camera_info_topic,
        depth_contract=depth_contract,
        wire_magic=WIRE_MAGIC.decode("ascii"),
        wire_version=WIRE_VERSION,
        header_bytes=WIRE_HEADER_BYTES,
        pixel_format="BGR24",
        source_row_order=GAZEBO_SOURCE_ROW_ORDER,
        wire_row_order=WIRE_ROW_ORDER,
        vertical_flip_applied=GAZEBO_VERTICAL_FLIP_APPLIED,
        required_sources=["image", "camera_info", "navsat", "odometry_pose_velocity"],
        optional_sources=(
            (["depth_r_float32"] if depth_contract is not None else [])
            + (["propeller_joint_state"] if rotor_joint_state_enabled else [])
        ),
        source_association_policy="nearest_source_at_or_before_image",
        camera_info_freshness_policy="session_static_geometry_bound_causal_timestamp",
        camera_info_intrinsics_semantic_validation=(
            "configured_fail_closed"
            if expected_camera_calibration is not None or depth_contract is not None
            else "pending_k_p_distortion_validation"
        ),
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
        fault_injection={
            "enabled": args.fault_source is not None,
            "source": args.fault_source,
            "start_image": args.fault_start_image if args.fault_source is not None else None,
            "duration_images": (
                args.fault_duration_images if args.fault_source is not None else 0
            ),
            "window_key": "received_image_index",
            "dynamic_source_policy": (
                "reject_during_window_and_until_post_window_fresh_sample"
            ),
            "cache_boundary_policy": (
                "clear_source_history_on_first_dropped_callback_keep_stamp_order"
            ),
        },
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
        camera_info_semantic_validation_snapshot = camera_info_semantic_validation
        fault_counts = {
            key: value for key, value in count_snapshot.items() if key.startswith("fault_")
        }
        fault_recovered_frame_count_snapshot = fault_recovered_frame_count
        fault_window_rejected_frame_count_snapshot = fault_window_rejected_frame_count
        fault_stale_source_rejection_count_snapshot = fault_stale_source_rejection_count
        fault_emitted_during_window_count_snapshot = fault_emitted_during_window_count
    gate_passed = error is None and emitted == args.frames and accepted_snapshot == args.frames
    _diagnostic(
        "gazebo_sensor_stream_finished",
        gate_passed=gate_passed,
        requested_frames=args.frames,
        accepted_frames=accepted_snapshot,
        emitted_frames=emitted,
        ready_sources=ready_sources,
        odometry_present="odometry" in ready_sources,
        gimballed_airframe=gimballed_airframe,
        gimbal_state_present=("joint" in ready_sources) if gimballed_airframe else None,
        gimbal_state_timestamp_count=count_snapshot.get("gimbal", 0),
        propeller_state_present=("joint" in ready_sources) if rotor_joint_state_enabled else None,
        propeller_state_timestamp_count=count_snapshot.get("propeller_frames", 0),
        gimbal_missing_or_stale_rejections=count_snapshot.get("gimbal_missing_or_stale_rejections", 0),
        gimbal_freshness_gate_passed=(
            gate_passed and count_snapshot.get("gimbal", 0) > 0
            if gimballed_airframe else None
        ),
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
        camera_info_intrinsics_semantic_validation=(
            camera_info_semantic_validation_snapshot["status"]
            if camera_info_semantic_validation_snapshot is not None
            else "pending_k_p_distortion_validation"
        ),
        camera_info_semantics=camera_info_semantic_validation_snapshot,
        depth_enabled=depth_contract is not None,
        depth_topic=depth_topic,
        depth_camera_info_topic=depth_camera_info_topic,
        depth_contract=depth_contract,
        depth_timestamp_frame_count=count_snapshot.get("depth", 0),
        message_counts=count_snapshot,
        fault_injection={
            "enabled": args.fault_source is not None,
            "source": args.fault_source,
            "start_image": args.fault_start_image if args.fault_source is not None else None,
            "duration_images": (
                args.fault_duration_images if args.fault_source is not None else 0
            ),
            "window_key": "received_image_index",
            "dynamic_source_policy": (
                "reject_during_window_and_until_post_window_fresh_sample"
            ),
            "cache_boundary_policy": (
                "clear_source_history_on_first_dropped_callback_keep_stamp_order"
            ),
            "source_epoch": fault_source_epoch,
            "cache_cleared": fault_cache_cleared,
            "dropped_counts": fault_counts,
            "window_rejected_frame_count": fault_window_rejected_frame_count_snapshot,
            "stale_source_rejection_count": fault_stale_source_rejection_count_snapshot,
            "emitted_during_window_count": fault_emitted_during_window_count_snapshot,
            "timeline": list(fault_timeline),
            "timeline_overflow": fault_timeline_overflow,
            "recovered_frame_count": fault_recovered_frame_count_snapshot,
            "recovery_observed": (
                args.fault_source is not None
                and (
                    fault_counts.get(f"fault_dropped_{args.fault_source}", 0) > 0
                    and fault_recovered_frame_count_snapshot > 0
                )
            ),
        },
        error=error,
    )
    return 0 if gate_passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
