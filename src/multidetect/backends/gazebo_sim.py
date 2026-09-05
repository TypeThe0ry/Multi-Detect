"""Live Gazebo raw-sensor adapter for the shared production mission core.

The adapter deliberately exposes only camera pixels and normalized read-only
telemetry.  It does not import scenario truth, detections, expected answers or
flight-control executors.  A small helper runs inside the already-owned PX4 /
Gazebo container and emits an integrity-framed BGR24 stream over ``docker
exec`` stdout.  The host validates every header and payload before presenting
it through the normal :class:`~multidetect.vision.FrameSource` boundary.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import struct
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from threading import RLock, Thread
from typing import BinaryIO

from ..adaptive_ranging import assess_gps_quality
from ..autonomy.serialization import canonical_sha256, require_int, require_real, require_sha256
from ..clock import Clock, SystemClock
from ..domain import VehicleTelemetry
from ..multimodal_ranging import CameraCalibration
from ..runtime import RuntimeCapability, RuntimeDescriptor, RuntimeMode, RuntimeProfile
from ..simulation.runtime_input import ScenarioRuntimeInput
from ..terrain_dem import (
    LocalDemGrid,
    TerrainDemError,
    dem_gps_terrain_observation,
)
from ..vision import CapturedFrame

GAZEBO_SIM_WIRE_MAGIC = b"MDGZSIM3"
GAZEBO_SIM_WIRE_VERSION = 3
GAZEBO_SIM_WIRE_HEADER = struct.Struct("<8sHHIQIIIIQQQ22dI32s")
GAZEBO_SIM_WIRE_HEADER_BYTES = GAZEBO_SIM_WIRE_HEADER.size
GAZEBO_SIM_FLAG_CAMERA_INFO = 1 << 0
GAZEBO_SIM_FLAG_NAVSAT = 1 << 1
GAZEBO_SIM_FLAG_ODOMETRY = 1 << 2
GAZEBO_SIM_REQUIRED_FLAGS = (
    GAZEBO_SIM_FLAG_CAMERA_INFO | GAZEBO_SIM_FLAG_NAVSAT | GAZEBO_SIM_FLAG_ODOMETRY
)
GAZEBO_SIM_OPTIONAL_FLAGS = 0
GAZEBO_SIM_PIXEL_FORMAT_BGR24 = 1
GAZEBO_SIM_PIXEL_FORMAT_SHIFT = 16
GAZEBO_SIM_PIXEL_FORMAT_MASK = 0x00FF0000
GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG = GAZEBO_SIM_PIXEL_FORMAT_BGR24 << GAZEBO_SIM_PIXEL_FORMAT_SHIFT
GAZEBO_SIM_KNOWN_FLAGS = (
    GAZEBO_SIM_REQUIRED_FLAGS | GAZEBO_SIM_OPTIONAL_FLAGS | GAZEBO_SIM_PIXEL_FORMAT_MASK
)
GAZEBO_SIM_READY_BGR24_FLAGS = GAZEBO_SIM_REQUIRED_FLAGS | GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG
GAZEBO_SIM_MAX_FRAME_BYTES = 64 * 1024 * 1024
GAZEBO_SIM_MAX_SOURCE_SKEW_NS = 1_500_000_000
GAZEBO_SIM_MAX_ABS_LOCAL_POSITION_M = 1_000_000.0
GAZEBO_SIM_MAX_ABS_LINEAR_VELOCITY_MPS = 2_000.0
GAZEBO_SIM_QUATERNION_NORM_TOLERANCE = 1.0e-3
GAZEBO_SIM_TERRAIN_AGL_REFERENCE = "GAZEBO_NAVSAT_MSL_MINUS_SCENARIO_DEM_MSL"
GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION = (
    "validated_k_p_rectification_plumb_bob_distortion"
)
GAZEBO_SIM_SOURCE_ROW_ORDER = "top_to_bottom"
GAZEBO_SIM_WIRE_ROW_ORDER = "top_to_bottom"
GAZEBO_SIM_VERTICAL_FLIP_APPLIED = False
GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE = (
    "gazebo_odometry_covariance_with_configured_floor"
)
GAZEBO_SIM_DEFAULT_MINIMUM_ROLL_SIGMA_DEG = 0.3
GAZEBO_SIM_DEFAULT_MINIMUM_PITCH_SIGMA_DEG = 0.3
GAZEBO_SIM_DEFAULT_MINIMUM_HEADING_SIGMA_DEG = 1.0

_SAFE_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SAFE_GAZEBO_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_STDERR_BYTES = 64 * 1024


def _gazebo_sensor_topics(
    *,
    world: str,
    model_instance: str,
) -> tuple[str, str, str, str]:
    """Derive the only accepted Gazebo topics for one bound model instance."""

    for value, name in ((world, "world"), (model_instance, "model_instance")):
        if _SAFE_GAZEBO_NAME.fullmatch(value) is None:
            raise ValueError(f"{name} contains unsupported characters")
    return (
        f"/world/{world}/model/{model_instance}/link/camera_link/sensor/camera/image",
        f"/world/{world}/model/{model_instance}/link/camera_link/sensor/camera/camera_info",
        f"/world/{world}/model/{model_instance}/link/base_link/sensor/navsat_sensor/navsat",
        f"/model/{model_instance}/odometry_with_covariance",
    )


@dataclass(frozen=True, slots=True)
class GazeboSimWireFrame:
    """One validated camera/NavSat/full-odometry/covariance wire-v3 frame."""

    sequence: int
    sensor_stamp_ns: int
    width: int
    height: int
    stride: int
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
    record_sha256: str
    unsigned_header: bytes
    payload_bgr24: bytes

    @property
    def odometry_present(self) -> bool:
        """Whether this frame carries a synchronized odometry timestamp."""

        return bool(self.flags & GAZEBO_SIM_FLAG_ODOMETRY)


@dataclass(frozen=True, slots=True)
class GazeboSimWireTelemetryProvenance:
    """Human-readable copy of the telemetry values covered by the wire digest."""

    latitude_deg: float
    longitude_deg: float
    gps_absolute_altitude_m: float
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

    @classmethod
    def from_wire(cls, wire: GazeboSimWireFrame) -> GazeboSimWireTelemetryProvenance:
        return cls(
            latitude_deg=wire.latitude_deg,
            longitude_deg=wire.longitude_deg,
            gps_absolute_altitude_m=wire.altitude_m,
            position_east_m=wire.position_east_m,
            position_north_m=wire.position_north_m,
            position_up_m=wire.position_up_m,
            orientation_x=wire.orientation_x,
            orientation_y=wire.orientation_y,
            orientation_z=wire.orientation_z,
            orientation_w=wire.orientation_w,
            velocity_forward_mps=wire.velocity_forward_mps,
            velocity_left_mps=wire.velocity_left_mps,
            velocity_up_mps=wire.velocity_up_mps,
            attitude_covariance_enu_rad2=wire.attitude_covariance_enu_rad2,
        )

    def to_mapping(self) -> dict[str, object]:
        mapping = asdict(self)
        # Keep the evidence representation JSON-native so a manifest survives
        # serialize/reload without tuple-versus-list identity drift.
        mapping["attitude_covariance_enu_rad2"] = list(self.attitude_covariance_enu_rad2)
        return mapping


@dataclass(frozen=True, slots=True)
class GazeboSimFrameTelemetryProvenance:
    """Derived NED/GPS state evaluated at the exact accepted camera frame."""

    latitude_deg: float
    longitude_deg: float
    gps_absolute_altitude_m: float
    altitude_agl_m: float | None
    altitude_reference: str | None
    local_north_m: float
    local_east_m: float
    local_down_m: float
    roll_deg: float
    pitch_deg: float
    heading_deg: float
    attitude_roll_sigma_deg: float
    attitude_pitch_sigma_deg: float
    attitude_heading_sigma_deg: float
    attitude_uncertainty_source: str
    velocity_north_mps: float
    velocity_east_mps: float
    gps_fix_type: int
    satellites_visible: int
    gps_horizontal_accuracy_m: float
    gps_vertical_accuracy_m: float
    gps_observed_at_s: float
    attitude_observed_at_s: float
    velocity_observed_at_s: float
    altitude_observed_at_s: float | None

    @classmethod
    def from_telemetry(
        cls,
        telemetry: VehicleTelemetry,
    ) -> GazeboSimFrameTelemetryProvenance:
        def finite_or_none(value: float) -> float | None:
            return float(value) if math.isfinite(value) else None

        required = (
            telemetry.latitude_deg,
            telemetry.longitude_deg,
            telemetry.gps_absolute_altitude_m,
            telemetry.local_north_m,
            telemetry.local_east_m,
            telemetry.local_down_m,
            telemetry.roll_deg,
            telemetry.pitch_deg,
            telemetry.heading_deg,
            telemetry.attitude_roll_sigma_deg,
            telemetry.attitude_pitch_sigma_deg,
            telemetry.attitude_heading_sigma_deg,
            telemetry.velocity_north_mps,
            telemetry.velocity_east_mps,
            telemetry.gps_horizontal_accuracy_m,
            telemetry.gps_vertical_accuracy_m,
            telemetry.gps_observed_at_s,
            telemetry.attitude_observed_at_s,
            telemetry.velocity_observed_at_s,
        )
        if any(value is None or not math.isfinite(float(value)) for value in required):
            raise RuntimeError("Gazebo SIM per-frame telemetry is incomplete or non-finite")
        if telemetry.gps_fix_type is None or telemetry.satellites_visible is None:
            raise RuntimeError("Gazebo SIM per-frame GPS quality is incomplete")
        if telemetry.attitude_uncertainty_source != GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE:
            raise RuntimeError("Gazebo SIM per-frame attitude uncertainty source is invalid")
        return cls(
            latitude_deg=float(telemetry.latitude_deg),
            longitude_deg=float(telemetry.longitude_deg),
            gps_absolute_altitude_m=float(telemetry.gps_absolute_altitude_m),
            altitude_agl_m=finite_or_none(telemetry.altitude_agl_m),
            altitude_reference=telemetry.altitude_reference,
            local_north_m=float(telemetry.local_north_m),
            local_east_m=float(telemetry.local_east_m),
            local_down_m=float(telemetry.local_down_m),
            roll_deg=float(telemetry.roll_deg),
            pitch_deg=float(telemetry.pitch_deg),
            heading_deg=float(telemetry.heading_deg),
            attitude_roll_sigma_deg=float(telemetry.attitude_roll_sigma_deg),
            attitude_pitch_sigma_deg=float(telemetry.attitude_pitch_sigma_deg),
            attitude_heading_sigma_deg=float(telemetry.attitude_heading_sigma_deg),
            attitude_uncertainty_source=telemetry.attitude_uncertainty_source,
            velocity_north_mps=float(telemetry.velocity_north_mps),
            velocity_east_mps=float(telemetry.velocity_east_mps),
            gps_fix_type=int(telemetry.gps_fix_type),
            satellites_visible=int(telemetry.satellites_visible),
            gps_horizontal_accuracy_m=float(telemetry.gps_horizontal_accuracy_m),
            gps_vertical_accuracy_m=float(telemetry.gps_vertical_accuracy_m),
            gps_observed_at_s=float(telemetry.gps_observed_at_s),
            attitude_observed_at_s=float(telemetry.attitude_observed_at_s),
            velocity_observed_at_s=float(telemetry.velocity_observed_at_s),
            altitude_observed_at_s=finite_or_none(telemetry.altitude_observed_at_s),
        )

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GazeboSimFrameProvenance:
    """Immutable, pixel-free provenance for one accepted wire-v3 frame.

    The image array returned by :meth:`GazeboSimFrameSource.read` continues to
    reference the one validated wire payload.  This record deliberately keeps
    only hashes, geometry and causal timestamps, so exposing capture evidence
    never retains a second copy of the pixels.
    """

    frame_id: str
    sequence: int
    captured_at_s: float
    captured_at_clock: str
    sensor_stamp_ns: int
    camera_info_stamp_ns: int
    navsat_stamp_ns: int
    odometry_stamp_ns: int
    camera_info_source_skew_ns: int
    navsat_source_skew_ns: int
    odometry_source_skew_ns: int
    record_sha256: str
    payload_sha256: str
    width: int
    height: int
    stride: int
    payload_bytes: int
    flags: int
    pixel_format: str
    wire_unsigned_header_hex: str | None = None
    wire_telemetry: GazeboSimWireTelemetryProvenance | None = None
    frame_telemetry: GazeboSimFrameTelemetryProvenance | None = None

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-ready copy without exposing mutable source state."""

        result = asdict(self)
        if self.wire_telemetry is not None:
            result["wire_telemetry"] = self.wire_telemetry.to_mapping()
        for optional_field in (
            "wire_unsigned_header_hex",
            "wire_telemetry",
            "frame_telemetry",
        ):
            if result[optional_field] is None:
                result.pop(optional_field)
        return result


@dataclass(frozen=True, slots=True)
class GazeboSimTransportStats:
    expected_frame_count: int
    received_frame_count: int
    odometry_timestamp_frame_count: int
    first_sensor_stamp_ns: int | None
    last_sensor_stamp_ns: int | None
    payload_bytes: int
    stderr_tail: str
    helper_exit_code: int | None
    complete: bool
    sensor_id: str
    calibration_id: str
    stream_uri: str
    image_topic: str
    camera_info_topic: str
    navsat_topic: str
    odometry_topic: str
    topic_binding_sha256: str
    sensor_binding_sha256: str
    camera_info_intrinsics_semantic_validation: str
    source_row_order: str = GAZEBO_SIM_SOURCE_ROW_ORDER
    wire_row_order: str = GAZEBO_SIM_WIRE_ROW_ORDER
    vertical_flip_applied: bool = GAZEBO_SIM_VERTICAL_FLIP_APPLIED
    odometry_required: bool = True
    full_odometry_pose_velocity_fusion_claimed: bool = False
    simulated_gps_fix_type: int = 3
    simulated_satellites_visible: int = 12
    simulated_gps_horizontal_accuracy_m: float = 1.5
    simulated_gps_vertical_accuracy_m: float = 2.5

    def to_mapping(self) -> dict[str, object]:
        return asdict(self)


def _matmul3(
    left: tuple[tuple[float, float, float], ...],
    right: tuple[tuple[float, float, float], ...],
) -> tuple[tuple[float, float, float], ...]:
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )


def _matvec3(
    matrix: tuple[tuple[float, float, float], ...],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    return (
        sum(matrix[0][index] * vector[index] for index in range(3)),
        sum(matrix[1][index] * vector[index] for index in range(3)),
        sum(matrix[2][index] * vector[index] for index in range(3)),
    )


def _quaternion_rotation_matrix(
    x: float,
    y: float,
    z: float,
    w: float,
) -> tuple[tuple[float, float, float], ...]:
    return (
        (
            1.0 - 2.0 * (y * y + z * z),
            2.0 * (x * y - z * w),
            2.0 * (x * z + y * w),
        ),
        (
            2.0 * (x * y + z * w),
            1.0 - 2.0 * (x * x + z * z),
            2.0 * (y * z - x * w),
        ),
        (
            2.0 * (x * z - y * w),
            2.0 * (y * z + x * w),
            1.0 - 2.0 * (x * x + y * y),
        ),
    )


def _ned_frd_kinematics(
    wire: GazeboSimWireFrame,
) -> tuple[float, float, float, float, float, float]:
    """Convert Gazebo ENU pose/body-FLU velocity to aerospace NED/FRD."""

    enu_from_flu = _quaternion_rotation_matrix(
        wire.orientation_x,
        wire.orientation_y,
        wire.orientation_z,
        wire.orientation_w,
    )
    ned_from_enu = (
        (0.0, 1.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, -1.0),
    )
    flu_from_frd = (
        (1.0, 0.0, 0.0),
        (0.0, -1.0, 0.0),
        (0.0, 0.0, -1.0),
    )
    ned_from_frd = _matmul3(_matmul3(ned_from_enu, enu_from_flu), flu_from_frd)

    sin_pitch = max(-1.0, min(1.0, -ned_from_frd[2][0]))
    pitch_rad = math.asin(sin_pitch)
    if abs(math.cos(pitch_rad)) > 1.0e-9:
        roll_rad = math.atan2(ned_from_frd[2][1], ned_from_frd[2][2])
        yaw_rad = math.atan2(ned_from_frd[1][0], ned_from_frd[0][0])
    else:
        # At pitch +/-90 deg only the coupled roll/yaw angle is observable.
        # Choose the canonical roll=0 representation so the conversion stays
        # deterministic and finite through the singularity.
        roll_rad = 0.0
        yaw_rad = math.atan2(-ned_from_frd[0][1], ned_from_frd[1][1])

    velocity_enu = _matvec3(
        enu_from_flu,
        (
            wire.velocity_forward_mps,
            wire.velocity_left_mps,
            wire.velocity_up_mps,
        ),
    )
    velocity_ned = _matvec3(ned_from_enu, velocity_enu)
    heading_deg = math.degrees(yaw_rad) % 360.0
    if math.isclose(heading_deg, 360.0, abs_tol=1.0e-12):
        heading_deg = 0.0
    return (
        math.degrees(roll_rad),
        math.degrees(pitch_rad),
        heading_deg,
        velocity_ned[0],
        velocity_ned[1],
        velocity_ned[2],
    )


def _rotation_about_axis(axis: int, angle_rad: float) -> tuple[tuple[float, float, float], ...]:
    cosine, sine = math.cos(angle_rad), math.sin(angle_rad)
    if axis == 0:
        return ((1.0, 0.0, 0.0), (0.0, cosine, -sine), (0.0, sine, cosine))
    if axis == 1:
        return ((cosine, 0.0, sine), (0.0, 1.0, 0.0), (-sine, 0.0, cosine))
    return ((cosine, -sine, 0.0), (sine, cosine, 0.0), (0.0, 0.0, 1.0))


def _fixed_axis_euler_radians(
    rotation: tuple[tuple[float, float, float], ...],
) -> tuple[float, float, float] | None:
    """Extract fixed-axis X/Y/Z angles represented by Rz(yaw)Ry(pitch)Rx(roll)."""

    pitch = math.asin(max(-1.0, min(1.0, -rotation[2][0])))
    if abs(math.cos(pitch)) <= 1.0e-6:
        return None
    return (
        math.atan2(rotation[2][1], rotation[2][2]),
        pitch,
        math.atan2(rotation[1][0], rotation[0][0]),
    )


def _rotation_from_fixed_axis_euler(
    roll: float,
    pitch: float,
    yaw: float,
) -> tuple[tuple[float, float, float], ...]:
    return _matmul3(
        _matmul3(_rotation_about_axis(2, yaw), _rotation_about_axis(1, pitch)),
        _rotation_about_axis(0, roll),
    )


def _ned_frd_euler_radians(
    enu_from_flu: tuple[tuple[float, float, float], ...],
) -> tuple[float, float, float] | None:
    ned_from_enu = ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, -1.0))
    flu_from_frd = ((1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    ned_from_frd = _matmul3(_matmul3(ned_from_enu, enu_from_flu), flu_from_frd)
    pitch = math.asin(max(-1.0, min(1.0, -ned_from_frd[2][0])))
    if abs(math.cos(pitch)) <= 1.0e-6:
        return None
    return (
        math.atan2(ned_from_frd[2][1], ned_from_frd[2][2]),
        pitch,
        math.atan2(ned_from_frd[1][0], ned_from_frd[0][0]),
    )


def _wrapped_radian_delta(left: float, right: float) -> float:
    return (left - right + math.pi) % (2.0 * math.pi) - math.pi


def gazebo_sim_attitude_sigmas_deg(wire: GazeboSimWireFrame) -> tuple[float, float, float]:
    """Transform Gazebo fixed-axis X/Y/Z covariance into NED/FRD Euler sigmas."""

    covariance = _validate_attitude_covariance(wire.attitude_covariance_enu_rad2)
    rotation = _quaternion_rotation_matrix(
        wire.orientation_x, wire.orientation_y, wire.orientation_z, wire.orientation_w
    )
    source_euler = _fixed_axis_euler_radians(rotation)
    if source_euler is None:
        return (180.0, 90.0, 180.0)
    epsilon = 1.0e-6
    jacobian = [[0.0] * 3 for _ in range(3)]
    for axis in range(3):
        plus_angles = list(source_euler)
        minus_angles = list(source_euler)
        plus_angles[axis] += epsilon
        minus_angles[axis] -= epsilon
        plus = _ned_frd_euler_radians(_rotation_from_fixed_axis_euler(*plus_angles))
        minus = _ned_frd_euler_radians(_rotation_from_fixed_axis_euler(*minus_angles))
        if plus is None or minus is None:
            # Euler roll/yaw are unobservable at gimbal lock.  Preserve the
            # canonical mean pose but publish a deliberately unbounded angular
            # budget so ranging cannot mistake that representation for precise.
            return (180.0, 90.0, 180.0)
        for output_axis in range(3):
            jacobian[output_axis][axis] = (
                _wrapped_radian_delta(plus[output_axis], minus[output_axis])
                / (2.0 * epsilon)
            )
    variances: list[float] = []
    for output_axis in range(3):
        variance = sum(
            jacobian[output_axis][row]
            * covariance[row * 3 + column]
            * jacobian[output_axis][column]
            for row in range(3)
            for column in range(3)
        )
        if variance < -1.0e-10:
            raise RuntimeError("Gazebo SIM transformed attitude covariance is negative")
        variances.append(max(0.0, variance))
    return tuple(math.degrees(math.sqrt(value)) for value in variances)  # type: ignore[return-value]


def _validate_attitude_covariance(values: object) -> tuple[float, ...]:
    try:
        covariance = tuple(float(value) for value in values)  # type: ignore[union-attr]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Gazebo SIM attitude covariance is not numeric") from exc
    if len(covariance) != 9 or not all(math.isfinite(value) for value in covariance):
        raise RuntimeError("Gazebo SIM attitude covariance must contain 9 finite values")
    scale = max(1.0, *(abs(value) for value in covariance))
    tolerance = 1.0e-10 * scale
    for row in range(3):
        if covariance[row * 3 + row] < 0.0:
            raise RuntimeError("Gazebo SIM attitude covariance diagonal is negative")
        for column in range(row + 1, 3):
            if abs(covariance[row * 3 + column] - covariance[column * 3 + row]) > tolerance:
                raise RuntimeError("Gazebo SIM attitude covariance is not symmetric")
    a, b, c, _d, e, f, _g, _h, i = covariance
    minors = (a, e, i, a * e - b * b, a * i - c * c, e * i - f * f)
    determinant = a * (e * i - f * f) - b * (b * i - c * f) + c * (b * f - c * e)
    if any(value < -tolerance for value in (*minors, determinant)):
        raise RuntimeError("Gazebo SIM attitude covariance is not positive semidefinite")
    return covariance


def gazebo_sim_ned_frd_kinematics(
    wire: GazeboSimWireFrame,
) -> tuple[float, float, float, float, float, float]:
    """Public deterministic ENU/FLU to NED/FRD conversion for evidence replay."""

    if not isinstance(wire, GazeboSimWireFrame):
        raise TypeError("wire must be a GazeboSimWireFrame")
    return _ned_frd_kinematics(wire)


def _source_time_in_host_domain(
    *,
    source_name: str,
    sensor_stamp_ns: int,
    source_stamp_ns: int,
    frame_observed_at_s: float,
) -> tuple[float, int]:
    """Map one causal Gazebo timestamp into the host monotonic domain.

    The image sensor timestamp is the bridge between Gazebo simulation time
    and the host capture clock.  Preserving the source-to-image age prevents a
    delayed NavSat or Odometry sample from being made fresh merely because its
    containing wire record was read now.
    """

    for value, field_name in (
        (sensor_stamp_ns, "image sensor timestamp"),
        (source_stamp_ns, f"{source_name} timestamp"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"Gazebo SIM {field_name} must be a positive integer")
    if source_stamp_ns > sensor_stamp_ns:
        raise RuntimeError(
            f"Gazebo SIM {source_name} timestamp is from the future: "
            f"image={sensor_stamp_ns}, source={source_stamp_ns}"
        )
    source_age_ns = sensor_stamp_ns - source_stamp_ns
    if source_age_ns > GAZEBO_SIM_MAX_SOURCE_SKEW_NS:
        raise RuntimeError(
            f"Gazebo SIM {source_name} timestamp skew exceeds "
            f"{GAZEBO_SIM_MAX_SOURCE_SKEW_NS} ns: {source_age_ns}"
        )
    mapped = frame_observed_at_s - source_age_ns / 1_000_000_000.0
    if mapped < 0.0:
        raise RuntimeError(
            f"Gazebo SIM {source_name} timestamp cannot be represented in the host monotonic domain"
        )
    return mapped, source_age_ns


class GazeboSimTelemetryProvider:
    """Thread-safe, fail-closed telemetry snapshot paired with the latest frame."""

    def __init__(
        self,
        *,
        maximum_age_s: float = 1.0,
        simulated_gps_fix_type: int = 3,
        simulated_satellites_visible: int = 12,
        simulated_gps_horizontal_accuracy_m: float = 1.5,
        simulated_gps_vertical_accuracy_m: float = 2.5,
        minimum_roll_sigma_deg: float = GAZEBO_SIM_DEFAULT_MINIMUM_ROLL_SIGMA_DEG,
        minimum_pitch_sigma_deg: float = GAZEBO_SIM_DEFAULT_MINIMUM_PITCH_SIGMA_DEG,
        minimum_heading_sigma_deg: float = GAZEBO_SIM_DEFAULT_MINIMUM_HEADING_SIGMA_DEG,
        terrain_dem: LocalDemGrid | None = None,
    ) -> None:
        self.maximum_age_s = require_real(maximum_age_s, "maximum_age_s", minimum=0.0)
        self.simulated_gps_fix_type = require_int(
            simulated_gps_fix_type,
            "simulated_gps_fix_type",
            minimum=0,
            maximum=8,
        )
        self.simulated_satellites_visible = require_int(
            simulated_satellites_visible,
            "simulated_satellites_visible",
            minimum=0,
            maximum=254,
        )
        self.simulated_gps_horizontal_accuracy_m = require_real(
            simulated_gps_horizontal_accuracy_m,
            "simulated_gps_horizontal_accuracy_m",
            minimum=0.0,
            maximum=1_000.0,
        )
        self.simulated_gps_vertical_accuracy_m = require_real(
            simulated_gps_vertical_accuracy_m,
            "simulated_gps_vertical_accuracy_m",
            minimum=0.0,
            maximum=1_000.0,
        )
        self.minimum_roll_sigma_deg = require_real(
            minimum_roll_sigma_deg, "minimum_roll_sigma_deg", minimum=0.0, maximum=180.0
        )
        self.minimum_pitch_sigma_deg = require_real(
            minimum_pitch_sigma_deg, "minimum_pitch_sigma_deg", minimum=0.0, maximum=90.0
        )
        self.minimum_heading_sigma_deg = require_real(
            minimum_heading_sigma_deg, "minimum_heading_sigma_deg", minimum=0.0, maximum=180.0
        )
        if terrain_dem is not None and not isinstance(terrain_dem, LocalDemGrid):
            raise TypeError("terrain_dem must be a LocalDemGrid or None")
        self.terrain_dem = terrain_dem
        self._latest: VehicleTelemetry | None = None
        self._observed_at_s: float | None = None
        self._last_update_at_s: float | None = None
        self._closed = False
        self._lock = RLock()

    def update(self, wire: GazeboSimWireFrame, *, observed_at_s: float) -> None:
        observed = require_real(observed_at_s, "observed_at_s", minimum=0.0)
        if not isinstance(wire, GazeboSimWireFrame):
            raise TypeError("wire must be a GazeboSimWireFrame")
        roll_deg, pitch_deg, heading_deg, velocity_north, velocity_east, _velocity_down = (
            _ned_frd_kinematics(wire)
        )
        roll_sigma_deg, pitch_sigma_deg, heading_sigma_deg = (
            gazebo_sim_attitude_sigmas_deg(wire)
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("GazeboSimTelemetryProvider is closed")
            if self._last_update_at_s is not None and observed < self._last_update_at_s:
                raise RuntimeError("Gazebo SIM telemetry clock moved backwards")
            gps_observed, gps_age_ns = _source_time_in_host_domain(
                source_name="NavSat",
                sensor_stamp_ns=wire.sensor_stamp_ns,
                source_stamp_ns=wire.navsat_stamp_ns,
                frame_observed_at_s=observed,
            )
            odometry_observed, odometry_age_ns = _source_time_in_host_domain(
                source_name="Odometry",
                sensor_stamp_ns=wire.sensor_stamp_ns,
                source_stamp_ns=wire.odometry_stamp_ns,
                frame_observed_at_s=observed,
            )
            oldest_required_observed = min(gps_observed, odometry_observed)
            oldest_required_age_ns = max(gps_age_ns, odometry_age_ns)
            if oldest_required_age_ns > self.maximum_age_s * 1_000_000_000.0:
                raise RuntimeError(
                    "Gazebo SIM telemetry source is stale at frame capture: "
                    f"{oldest_required_age_ns / 1_000_000_000.0:.9f}s > "
                    f"{self.maximum_age_s:.9f}s"
                )
            telemetry = VehicleTelemetry(
                # Gazebo odometry reports position in the world's ENU frame.
                # Its local-up coordinate is not height above terrain once the
                # world is non-flat, so it must never be exposed as AGL.  AGL
                # is populated below only from an MSL-aligned scenario DEM.
                altitude_agl_m=float("nan"),
                roll_deg=roll_deg,
                pitch_deg=pitch_deg,
                ground_speed_mps=math.hypot(velocity_north, velocity_east),
                in_allowed_zone=True,
                geofence_healthy=True,
                position_healthy=True,
                link_healthy=True,
                flight_mode_allows_deploy=False,
                release_zone_clear=None,
                person_detector_healthy=None,
                latitude_deg=wire.latitude_deg,
                longitude_deg=wire.longitude_deg,
                heading_deg=heading_deg,
                battery_remaining_pct=float("nan"),
                satellites_visible=self.simulated_satellites_visible,
                gps_fix_type=self.simulated_gps_fix_type,
                gps_horizontal_accuracy_m=self.simulated_gps_horizontal_accuracy_m,
                gps_vertical_accuracy_m=self.simulated_gps_vertical_accuracy_m,
                gps_observed_at_s=gps_observed,
                attitude_observed_at_s=odometry_observed,
                attitude_roll_sigma_deg=max(
                    roll_sigma_deg, self.minimum_roll_sigma_deg
                ),
                attitude_pitch_sigma_deg=max(
                    pitch_sigma_deg, self.minimum_pitch_sigma_deg
                ),
                attitude_heading_sigma_deg=max(
                    heading_sigma_deg, self.minimum_heading_sigma_deg
                ),
                attitude_uncertainty_source=GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE,
                position_observed_at_s=gps_observed,
                velocity_north_mps=velocity_north,
                velocity_east_mps=velocity_east,
                airspeed_mps=float("nan"),
                wind_north_mps=float("nan"),
                wind_east_mps=float("nan"),
                velocity_observed_at_s=odometry_observed,
                airspeed_observed_at_s=float("nan"),
                wind_observed_at_s=float("nan"),
                altitude_observed_at_s=float("nan"),
                local_north_m=wire.position_north_m,
                local_east_m=wire.position_east_m,
                local_down_m=-wire.position_up_m,
                local_position_observed_at_s=odometry_observed,
                flight_mode="SIM",
                armed=None,
                altitude_reference=None,
                mission_sequence=None,
                gps_absolute_altitude_m=wire.altitude_m,
            )
            if (
                self.terrain_dem is not None
                and assess_gps_quality(
                    telemetry,
                    now_s=observed,
                ).qualified
            ):
                try:
                    terrain_observation = dem_gps_terrain_observation(
                        self.terrain_dem,
                        latitude_deg=wire.latitude_deg,
                        longitude_deg=wire.longitude_deg,
                        gps_absolute_altitude_m=wire.altitude_m,
                        gps_horizontal_sigma_m=self.simulated_gps_horizontal_accuracy_m,
                        gps_vertical_sigma_m=self.simulated_gps_vertical_accuracy_m,
                        captured_at_s=gps_observed,
                    )
                except TerrainDemError:
                    # Coverage, datum and non-positive-height failures are an
                    # AGL-quality failure, not permission to reuse local-up.
                    pass
                else:
                    telemetry = replace(
                        telemetry,
                        altitude_agl_m=terrain_observation.height_m,
                        altitude_observed_at_s=gps_observed,
                        altitude_reference=GAZEBO_SIM_TERRAIN_AGL_REFERENCE,
                    )
            self._latest = telemetry
            self._observed_at_s = oldest_required_observed
            self._last_update_at_s = observed

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        now = require_real(now_s, "now_s", minimum=0.0)
        with self._lock:
            if self._closed:
                raise RuntimeError("GazeboSimTelemetryProvider is closed")
            if self._latest is None or self._observed_at_s is None:
                raise RuntimeError("Gazebo SIM telemetry is not ready")
            age_s = now - self._observed_at_s
            if age_s < -1e-3:
                raise RuntimeError("Gazebo SIM telemetry is from the future")
            if age_s > self.maximum_age_s:
                raise RuntimeError(
                    f"Gazebo SIM telemetry is stale: {age_s:.6f}s > {self.maximum_age_s:.6f}s"
                )
            return self._latest

    def close(self) -> None:
        with self._lock:
            self._closed = True


class GazeboSimFrameSource:
    """Read the bounded integrity-framed sensor stream from ``docker exec``."""

    def __init__(
        self,
        *,
        docker_executable: str | Path,
        container_name: str,
        frame_count: int,
        telemetry_provider: GazeboSimTelemetryProvider,
        clock: Clock,
        stream_script: str = "/workspace/scripts/gazebo_sensor_stream.py",
        model_instance: str = "standard_vtol_mono_cam_0",
        world: str = "default",
        image_topic: str,
        camera_info_topic: str,
        navsat_topic: str,
        odometry_topic: str,
        sensor_id: str,
        calibration_id: str,
        camera_calibration: CameraCalibration,
        stream_uri: str,
        topic_binding_sha256: str,
        sensor_binding_sha256: str,
        timeout_s: float = 60.0,
        expected_width: int = 1280,
        expected_height: int = 960,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        docker = Path(docker_executable)
        if not docker.is_absolute() or not docker.is_file():
            raise ValueError("docker_executable must be an existing absolute file")
        if _SAFE_CONTAINER_NAME.fullmatch(container_name) is None:
            raise ValueError("container_name contains unsupported characters")
        script = PurePosixPath(stream_script)
        if not script.is_absolute() or ".." in script.parts:
            raise ValueError("stream_script must be a contained absolute POSIX path")
        for value, name in ((model_instance, "model_instance"), (world, "world")):
            if _SAFE_GAZEBO_NAME.fullmatch(value) is None:
                raise ValueError(f"{name} contains unsupported characters")
        expected_topics = _gazebo_sensor_topics(world=world, model_instance=model_instance)
        supplied_topics = (image_topic, camera_info_topic, navsat_topic, odometry_topic)
        if supplied_topics != expected_topics:
            raise ValueError("Gazebo SIM helper topics do not match the bound world/model_instance")
        self.sensor_id = sensor_id
        self.calibration_id = calibration_id
        if not isinstance(camera_calibration, CameraCalibration):
            raise TypeError("camera_calibration must be a CameraCalibration")
        if camera_calibration.calibration_id != calibration_id:
            raise ValueError("camera calibration ID does not match the scenario sensor")
        if (camera_calibration.width_px, camera_calibration.height_px) != (
            expected_width,
            expected_height,
        ):
            raise ValueError("camera calibration geometry does not match the wire geometry")
        self.camera_calibration = camera_calibration
        self.stream_uri = stream_uri
        self.image_topic = image_topic
        self.camera_info_topic = camera_info_topic
        self.navsat_topic = navsat_topic
        self.odometry_topic = odometry_topic
        self.topic_binding_sha256 = require_sha256(topic_binding_sha256, "topic_binding_sha256")
        self.sensor_binding_sha256 = require_sha256(sensor_binding_sha256, "sensor_binding_sha256")
        self.frame_count = require_int(frame_count, "frame_count", minimum=1)
        self.expected_width = require_int(expected_width, "expected_width", minimum=1)
        self.expected_height = require_int(expected_height, "expected_height", minimum=1)
        self.timeout_s = require_real(timeout_s, "timeout_s", minimum=0.001)
        if not isinstance(telemetry_provider, GazeboSimTelemetryProvider):
            raise TypeError("telemetry_provider must be GazeboSimTelemetryProvider")
        self.telemetry_provider = telemetry_provider
        self.clock = clock
        self._command = (
            str(docker),
            "exec",
            container_name,
            "python3",
            script.as_posix(),
            "--frames",
            str(self.frame_count),
            "--timeout-seconds",
            f"{self.timeout_s:.6f}",
            "--model-instance",
            model_instance,
            "--world",
            world,
            "--image-topic",
            image_topic,
            "--camera-info-topic",
            camera_info_topic,
            "--navsat-topic",
            navsat_topic,
            "--odometry-topic",
            odometry_topic,
            "--expected-camera-calibration-json",
            json.dumps(
                {
                    "width_px": camera_calibration.width_px,
                    "height_px": camera_calibration.height_px,
                    "fx_px": camera_calibration.fx_px,
                    "fy_px": camera_calibration.fy_px,
                    "cx_px": camera_calibration.cx_px,
                    "cy_px": camera_calibration.cy_px,
                    "distortion": [
                        camera_calibration.k1,
                        camera_calibration.k2,
                        camera_calibration.p1,
                        camera_calibration.p2,
                        camera_calibration.k3,
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self._popen_factory = popen_factory
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_thread: Thread | None = None
        self._stderr = bytearray()
        self._next_sequence = 0
        self._odometry_timestamp_frame_count = 0
        self._first_sensor_stamp_ns: int | None = None
        self._last_sensor_stamp_ns: int | None = None
        self._payload_bytes = 0
        self._frame_provenance: list[GazeboSimFrameProvenance] = []
        self._helper_exit_code: int | None = None
        self._opened = False
        self._closed = False

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    @property
    def frame_provenance(self) -> tuple[GazeboSimFrameProvenance, ...]:
        """Return immutable evidence records for all successfully read frames."""

        return tuple(self._frame_provenance)

    @property
    def latest_frame_provenance(self) -> GazeboSimFrameProvenance:
        """Return the evidence record paired with the most recent frame."""

        if not self._frame_provenance:
            raise RuntimeError("Gazebo SIM frame provenance is not ready")
        return self._frame_provenance[-1]

    def open(self) -> None:
        if self._closed:
            raise RuntimeError("GazeboSimFrameSource is closed")
        if self._opened:
            raise RuntimeError("GazeboSimFrameSource can only be opened once")
        process = self._popen_factory(
            list(self._command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise RuntimeError("Gazebo SIM helper pipes were not created")
        self._process = process
        self._stderr_thread = Thread(target=self._drain_stderr, args=(process.stderr,), daemon=True)
        self._stderr_thread.start()
        self._opened = True

    def read(self) -> CapturedFrame:
        process = self._require_process()
        if self._next_sequence >= self.frame_count:
            raise RuntimeError("GazeboSimFrameSource is exhausted")
        stdout = process.stdout
        if stdout is None:  # pragma: no cover - established by open
            raise RuntimeError("Gazebo SIM helper stdout is unavailable")
        try:
            wire = read_gazebo_sim_wire_frame(
                stdout,
                expected_sequence=self._next_sequence,
                expected_width=self.expected_width,
                expected_height=self.expected_height,
                previous_sensor_stamp_ns=self._last_sensor_stamp_ns,
            )
        except RuntimeError as exc:
            # A helper can close stdout before ``poll`` observes its exit.  In
            # that race the wire reader reports only EOF and the diagnostic
            # emitted by the Gazebo callback remains stranded in the stderr
            # drain thread.  Reap the now-closed helper and retain its bounded
            # stderr so a real combined-SIM failure identifies the rejected
            # sensor field instead of degenerating into an opaque pipe error.
            return_code = process.poll()
            if return_code is None:
                try:
                    return_code = process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    return_code = process.poll()
            if return_code is not None:
                self._helper_exit_code = return_code
            if self._stderr_thread is not None:
                self._stderr_thread.join(timeout=1.0)
            stderr_tail = self.stats.stderr_tail
            if return_code is not None or stderr_tail:
                raise RuntimeError(
                    "Gazebo SIM helper closed its framed stream: "
                    f"exit={return_code}, stderr={stderr_tail!r}, "
                    f"wire_error={exc}"
                ) from exc
            raise
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - vision extra is required in production
            raise RuntimeError("Gazebo SIM frames require numpy") from exc
        image = np.frombuffer(wire.payload_bgr24, dtype=np.uint8).reshape(
            (wire.height, wire.width, 3)
        )
        captured_at_s = self.clock.monotonic()
        self.telemetry_provider.update(wire, observed_at_s=captured_at_s)
        frame_telemetry = GazeboSimFrameTelemetryProvenance.from_telemetry(
            self.telemetry_provider.snapshot(now_s=captured_at_s)
        )
        frame_id = f"gazebo-sim-{wire.sequence:06d}"
        provenance = GazeboSimFrameProvenance(
            frame_id=frame_id,
            sequence=wire.sequence,
            captured_at_s=captured_at_s,
            captured_at_clock="host_monotonic",
            sensor_stamp_ns=wire.sensor_stamp_ns,
            camera_info_stamp_ns=wire.camera_info_stamp_ns,
            navsat_stamp_ns=wire.navsat_stamp_ns,
            odometry_stamp_ns=wire.odometry_stamp_ns,
            camera_info_source_skew_ns=(wire.sensor_stamp_ns - wire.camera_info_stamp_ns),
            navsat_source_skew_ns=wire.sensor_stamp_ns - wire.navsat_stamp_ns,
            odometry_source_skew_ns=wire.sensor_stamp_ns - wire.odometry_stamp_ns,
            record_sha256=wire.record_sha256,
            payload_sha256=hashlib.sha256(wire.payload_bgr24).hexdigest(),
            width=wire.width,
            height=wire.height,
            stride=wire.stride,
            payload_bytes=len(wire.payload_bgr24),
            flags=wire.flags,
            pixel_format="BGR24",
            wire_unsigned_header_hex=wire.unsigned_header.hex(),
            wire_telemetry=GazeboSimWireTelemetryProvenance.from_wire(wire),
            frame_telemetry=frame_telemetry,
        )
        self._next_sequence += 1
        self._odometry_timestamp_frame_count += int(wire.odometry_present)
        self._payload_bytes += len(wire.payload_bgr24)
        self._frame_provenance.append(provenance)
        if self._first_sensor_stamp_ns is None:
            self._first_sensor_stamp_ns = wire.sensor_stamp_ns
        self._last_sensor_stamp_ns = wire.sensor_stamp_ns
        return CapturedFrame(
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            image_bgr=image,
            width=wire.width,
            height=wire.height,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None:
            if self._next_sequence == self.frame_count:
                try:
                    self._helper_exit_code = process.wait(timeout=max(2.0, self.timeout_s))
                except subprocess.TimeoutExpired:
                    process.kill()
                    self._helper_exit_code = process.wait(timeout=2.0)
            else:
                process.terminate()
                try:
                    self._helper_exit_code = process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    self._helper_exit_code = process.wait(timeout=2.0)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)

    def require_complete(self) -> GazeboSimTransportStats:
        stats = self.stats
        if not stats.complete:
            raise RuntimeError(
                "Gazebo SIM sensor stream did not complete: "
                f"frames={stats.received_frame_count}/{stats.expected_frame_count}, "
                f"helper_exit={stats.helper_exit_code}, stderr={stats.stderr_tail!r}"
            )
        return stats

    @property
    def stats(self) -> GazeboSimTransportStats:
        return GazeboSimTransportStats(
            expected_frame_count=self.frame_count,
            received_frame_count=self._next_sequence,
            odometry_timestamp_frame_count=self._odometry_timestamp_frame_count,
            first_sensor_stamp_ns=self._first_sensor_stamp_ns,
            last_sensor_stamp_ns=self._last_sensor_stamp_ns,
            payload_bytes=self._payload_bytes,
            stderr_tail=bytes(self._stderr).decode("utf-8", errors="replace"),
            helper_exit_code=self._helper_exit_code,
            complete=(
                self._closed
                and self._next_sequence == self.frame_count
                and self._helper_exit_code == 0
                and self._first_sensor_stamp_ns is not None
                and self._last_sensor_stamp_ns is not None
                and self._odometry_timestamp_frame_count == self.frame_count
            ),
            sensor_id=self.sensor_id,
            calibration_id=self.calibration_id,
            stream_uri=self.stream_uri,
            image_topic=self.image_topic,
            camera_info_topic=self.camera_info_topic,
            navsat_topic=self.navsat_topic,
            odometry_topic=self.odometry_topic,
            topic_binding_sha256=self.topic_binding_sha256,
            sensor_binding_sha256=self.sensor_binding_sha256,
            camera_info_intrinsics_semantic_validation=(
                GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
            ),
            odometry_required=True,
            full_odometry_pose_velocity_fusion_claimed=(
                self._closed
                and self._next_sequence == self.frame_count
                and self._helper_exit_code == 0
                and self._odometry_timestamp_frame_count == self.frame_count
            ),
            simulated_gps_fix_type=self.telemetry_provider.simulated_gps_fix_type,
            simulated_satellites_visible=self.telemetry_provider.simulated_satellites_visible,
            simulated_gps_horizontal_accuracy_m=(
                self.telemetry_provider.simulated_gps_horizontal_accuracy_m
            ),
            simulated_gps_vertical_accuracy_m=(
                self.telemetry_provider.simulated_gps_vertical_accuracy_m
            ),
        )

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._closed:
            raise RuntimeError("GazeboSimFrameSource is closed")
        if not self._opened or self._process is None:
            raise RuntimeError("GazeboSimFrameSource is not open")
        if self._process.poll() is not None and self._next_sequence < self.frame_count:
            self._helper_exit_code = self._process.returncode
            raise RuntimeError(
                "Gazebo SIM helper exited before the requested frame count: "
                f"exit={self._helper_exit_code}, stderr={self.stats.stderr_tail!r}"
            )
        return self._process

    def _drain_stderr(self, stream: BinaryIO) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > _MAX_STDERR_BYTES:
                del self._stderr[: len(self._stderr) - _MAX_STDERR_BYTES]


class GazeboSimBackend:
    """Concrete live SIM binding for one scenario-owned Gazebo container."""

    def __init__(
        self,
        *,
        scenario_input: ScenarioRuntimeInput,
        camera_calibration: CameraCalibration,
        docker_executable: str | Path,
        container_name: str,
        frame_count: int,
        airframe_profile_hash: str,
        container_image_sha256: str,
        stream_script_sha256: str,
        model_assets_sha256: str,
        stream_script: str = "/workspace/scripts/gazebo_sensor_stream.py",
        model_instance: str = "standard_vtol_mono_cam_0",
        world: str = "default",
        timeout_s: float = 60.0,
        expected_width: int = 1280,
        expected_height: int = 960,
        maximum_telemetry_age_s: float = 1.0,
        simulated_gps_fix_type: int = 3,
        simulated_satellites_visible: int = 12,
        simulated_gps_horizontal_accuracy_m: float = 1.5,
        simulated_gps_vertical_accuracy_m: float = 2.5,
        terrain_dem: LocalDemGrid | None = None,
        clock: Clock | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        if not isinstance(scenario_input, ScenarioRuntimeInput):
            raise TypeError("scenario_input must be ScenarioRuntimeInput")
        if not isinstance(camera_calibration, CameraCalibration):
            raise TypeError("camera_calibration must be a CameraCalibration")
        rgb_sensors = tuple(
            sensor for sensor in scenario_input.sensors if sensor.kind == "rgb_camera"
        )
        if len(rgb_sensors) != 1:
            raise ValueError("Gazebo SIM backend requires exactly one scenario RGB camera")
        rgb_sensor = rgb_sensors[0]
        if rgb_sensor.sensor_id != "front-camera":
            raise ValueError("Gazebo SIM RGB camera sensor_id must be 'front-camera'")
        if rgb_sensor.calibration_id is None:
            raise ValueError("Gazebo SIM RGB camera must bind a calibration_id")
        if camera_calibration.calibration_id != rgb_sensor.calibration_id:
            raise ValueError("camera calibration ID does not match the scenario RGB sensor")
        if (camera_calibration.width_px, camera_calibration.height_px) != (
            expected_width,
            expected_height,
        ):
            raise ValueError("camera calibration geometry does not match Gazebo wire geometry")
        expected_stream_uri = f"gazebo://{scenario_input.scenario_id}/{rgb_sensor.sensor_id}"
        if rgb_sensor.stream_uri != expected_stream_uri:
            raise ValueError(
                "Gazebo SIM RGB camera stream_uri does not match its scenario/sensor binding: "
                f"{rgb_sensor.stream_uri!r} != {expected_stream_uri!r}"
            )
        topics = _gazebo_sensor_topics(world=world, model_instance=model_instance)
        topic_binding = {
            "schema_version": 1,
            "world": world,
            "model_instance": model_instance,
            "image_topic": topics[0],
            "camera_info_topic": topics[1],
            "navsat_topic": topics[2],
            "odometry_topic": topics[3],
        }
        self.topic_binding_hash = canonical_sha256(topic_binding)
        self._sensor_binding = {
            "schema_version": 1,
            "sensor_id": rgb_sensor.sensor_id,
            "kind": rgb_sensor.kind,
            "calibration_id": rgb_sensor.calibration_id,
            "stream_uri": rgb_sensor.stream_uri,
            "topic_binding": topic_binding,
            "topic_binding_sha256": self.topic_binding_hash,
            "source_row_order": GAZEBO_SIM_SOURCE_ROW_ORDER,
            "wire_row_order": GAZEBO_SIM_WIRE_ROW_ORDER,
            "vertical_flip_applied": GAZEBO_SIM_VERTICAL_FLIP_APPLIED,
            "camera_calibration_sha256": canonical_sha256(asdict(camera_calibration)),
            "camera_info_intrinsics_semantic_validation": (
                GAZEBO_SIM_CAMERA_INTRINSICS_SEMANTIC_VALIDATION
            ),
        }
        selected_clock = clock or SystemClock()
        if not isinstance(selected_clock, Clock):
            raise TypeError("clock must implement Clock")
        image_hash = require_sha256(
            container_image_sha256.removeprefix("sha256:"), "container_image_sha256"
        )
        stream_hash = require_sha256(stream_script_sha256, "stream_script_sha256")
        model_hash = require_sha256(model_assets_sha256, "model_assets_sha256")
        airframe_hash = require_sha256(airframe_profile_hash, "airframe_profile_hash")
        gps_fix_type = require_int(
            simulated_gps_fix_type,
            "simulated_gps_fix_type",
            minimum=0,
            maximum=8,
        )
        satellites_visible = require_int(
            simulated_satellites_visible,
            "simulated_satellites_visible",
            minimum=0,
            maximum=254,
        )
        gps_horizontal_accuracy_m = require_real(
            simulated_gps_horizontal_accuracy_m,
            "simulated_gps_horizontal_accuracy_m",
            minimum=0.0,
            maximum=1_000.0,
        )
        gps_vertical_accuracy_m = require_real(
            simulated_gps_vertical_accuracy_m,
            "simulated_gps_vertical_accuracy_m",
            minimum=0.0,
            maximum=1_000.0,
        )
        if terrain_dem is not None and not isinstance(terrain_dem, LocalDemGrid):
            raise TypeError("terrain_dem must be a LocalDemGrid or None")
        if terrain_dem is not None and terrain_dem.scenario_id != scenario_input.scenario_id:
            raise ValueError(
                "terrain_dem scenario_id does not match the validated ScenarioRuntimeInput"
            )
        self.scenario_input = scenario_input
        self.sensor_binding_hash = canonical_sha256(
            {
                "schema_version": 3,
                "wire_version": GAZEBO_SIM_WIRE_VERSION,
                "scenario_manifest_hash": scenario_input.scenario_manifest_hash,
                "scenario_seed": scenario_input.seed,
                "container_image_sha256": image_hash,
                "stream_script_sha256": stream_hash,
                "model_assets_sha256": model_hash,
                "model_instance": model_instance,
                "world": world,
                "scenario_rgb_camera": self._sensor_binding,
                "frame_count": frame_count,
                "width": expected_width,
                "height": expected_height,
                "simulated_gps_quality": {
                    "fix_type": gps_fix_type,
                    "satellites_visible": satellites_visible,
                    "horizontal_accuracy_m": gps_horizontal_accuracy_m,
                    "vertical_accuracy_m": gps_vertical_accuracy_m,
                },
                "terrain_agl": {
                    "terrain_dem_sha256": (
                        terrain_dem.source_sha256 if terrain_dem is not None else None
                    ),
                    "altitude_reference": (
                        GAZEBO_SIM_TERRAIN_AGL_REFERENCE if terrain_dem is not None else None
                    ),
                    "odometry_local_up_used_as_agl": False,
                },
            }
        )
        self.descriptor = RuntimeDescriptor(
            mode=RuntimeMode.SIM,
            profile=RuntimeProfile.sim(),
            backend_id=f"gazebo-live:{model_instance}:{self.sensor_binding_hash[:16]}",
            airframe_profile_hash=airframe_hash,
            scenario_id=scenario_input.scenario_id,
            scenario_manifest_hash=scenario_input.scenario_manifest_hash,
            scenario_seed=scenario_input.seed,
            recording_hash=None,
            deterministic=False,
            fault_injection_enabled=False,
        )
        if RuntimeCapability.FLIGHT_CONTROL_OUTPUT in self.descriptor.profile.capabilities:
            raise ValueError("Gazebo SIM backend cannot expose flight-control output")
        self.clock = selected_clock
        self.telemetry_provider = GazeboSimTelemetryProvider(
            maximum_age_s=maximum_telemetry_age_s,
            simulated_gps_fix_type=gps_fix_type,
            simulated_satellites_visible=satellites_visible,
            simulated_gps_horizontal_accuracy_m=gps_horizontal_accuracy_m,
            simulated_gps_vertical_accuracy_m=gps_vertical_accuracy_m,
            terrain_dem=terrain_dem,
        )
        self.frame_source = GazeboSimFrameSource(
            docker_executable=docker_executable,
            container_name=container_name,
            frame_count=frame_count,
            telemetry_provider=self.telemetry_provider,
            clock=self.clock,
            stream_script=stream_script,
            model_instance=model_instance,
            world=world,
            image_topic=topics[0],
            camera_info_topic=topics[1],
            navsat_topic=topics[2],
            odometry_topic=topics[3],
            sensor_id=rgb_sensor.sensor_id,
            calibration_id=rgb_sensor.calibration_id,
            camera_calibration=camera_calibration,
            stream_uri=rgb_sensor.stream_uri,
            topic_binding_sha256=self.topic_binding_hash,
            sensor_binding_sha256=self.sensor_binding_hash,
            timeout_s=timeout_s,
            expected_width=expected_width,
            expected_height=expected_height,
            popen_factory=popen_factory,
        )
        self._closed = False

    @property
    def frame_count(self) -> int:
        return self.frame_source.frame_count

    @property
    def sensor_binding(self) -> dict[str, object]:
        """Return a detached, JSON-ready copy of the scenario/topic binding."""

        binding = dict(self._sensor_binding)
        topic_binding = binding.get("topic_binding")
        if isinstance(topic_binding, dict):
            binding["topic_binding"] = dict(topic_binding)
        return binding

    @property
    def input_hash(self) -> str:
        return self.sensor_binding_hash

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.frame_source.close()
        finally:
            self.telemetry_provider.close()


def read_gazebo_sim_wire_frame(
    stream: BinaryIO,
    *,
    expected_sequence: int,
    expected_width: int,
    expected_height: int,
    previous_sensor_stamp_ns: int | None,
) -> GazeboSimWireFrame:
    """Read and fail-closed validate exactly one wire frame."""

    header = _read_exact(stream, GAZEBO_SIM_WIRE_HEADER_BYTES, "Gazebo SIM header")
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
    ) = GAZEBO_SIM_WIRE_HEADER.unpack(header)
    if magic != GAZEBO_SIM_WIRE_MAGIC:
        raise RuntimeError("Gazebo SIM wire magic mismatch")
    if version != GAZEBO_SIM_WIRE_VERSION or header_bytes != GAZEBO_SIM_WIRE_HEADER_BYTES:
        raise RuntimeError("Gazebo SIM wire version/header size mismatch")
    if sequence != expected_sequence:
        raise RuntimeError(
            f"Gazebo SIM sequence mismatch: expected {expected_sequence}, observed {sequence}"
        )
    if width != expected_width or height != expected_height:
        raise RuntimeError(
            "Gazebo SIM geometry mismatch: "
            f"expected {expected_width}x{expected_height}, observed {width}x{height}"
        )
    if stride != width * 3 or payload_bytes != stride * height:
        raise RuntimeError("Gazebo SIM BGR24 stride/payload geometry is invalid")
    if payload_bytes <= 0 or payload_bytes > GAZEBO_SIM_MAX_FRAME_BYTES:
        raise RuntimeError("Gazebo SIM payload size is outside the bounded limit")
    if sensor_stamp_ns <= 0 or (
        previous_sensor_stamp_ns is not None and sensor_stamp_ns <= previous_sensor_stamp_ns
    ):
        raise RuntimeError("Gazebo SIM sensor timestamps must be positive and strictly increasing")
    if flags & ~GAZEBO_SIM_KNOWN_FLAGS:
        raise RuntimeError("Gazebo SIM frame contains unknown flags")
    if flags & GAZEBO_SIM_REQUIRED_FLAGS != GAZEBO_SIM_REQUIRED_FLAGS:
        raise RuntimeError("Gazebo SIM frame was emitted before sensor readiness")
    pixel_format = (flags & GAZEBO_SIM_PIXEL_FORMAT_MASK) >> GAZEBO_SIM_PIXEL_FORMAT_SHIFT
    if pixel_format != GAZEBO_SIM_PIXEL_FORMAT_BGR24:
        raise RuntimeError(
            "Gazebo SIM pixel format mismatch: "
            f"expected BGR24/{GAZEBO_SIM_PIXEL_FORMAT_BGR24}, observed {pixel_format}"
        )
    if min(camera_info_stamp_ns, navsat_stamp_ns, odometry_stamp_ns) <= 0:
        raise RuntimeError("Gazebo SIM required readiness timestamps must be positive")
    for source, stamp_ns in (
        ("CameraInfo", camera_info_stamp_ns),
        ("NavSat", navsat_stamp_ns),
        ("Odometry", odometry_stamp_ns),
    ):
        if stamp_ns > sensor_stamp_ns:
            raise RuntimeError(
                f"Gazebo SIM {source} timestamp is from the future: "
                f"image={sensor_stamp_ns}, source={stamp_ns}"
            )
        age_ns = sensor_stamp_ns - stamp_ns
        if source != "CameraInfo" and age_ns > GAZEBO_SIM_MAX_SOURCE_SKEW_NS:
            raise RuntimeError(
                f"Gazebo SIM {source} timestamp skew exceeds "
                f"{GAZEBO_SIM_MAX_SOURCE_SKEW_NS} ns: {age_ns}"
            )
    for value, name, minimum, maximum in (
        (latitude_deg, "latitude_deg", -90.0, 90.0),
        (longitude_deg, "longitude_deg", -180.0, 180.0),
        (altitude_m, "altitude_m", -1000.0, 100000.0),
    ):
        if (
            not math.isfinite(value)
            or (name == "latitude_deg" and not minimum < value < maximum)
            or (name != "latitude_deg" and not minimum <= value <= maximum)
        ):
            raise RuntimeError(f"Gazebo SIM {name} is invalid")
    odometry_values = (
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
    )
    if not all(math.isfinite(value) for value in odometry_values):
        raise RuntimeError("Gazebo SIM Odometry contains a non-finite value")
    if any(
        abs(value) > GAZEBO_SIM_MAX_ABS_LOCAL_POSITION_M
        for value in (position_east_m, position_north_m, position_up_m)
    ):
        raise RuntimeError("Gazebo SIM Odometry position exceeds the local safety bound")
    if any(
        abs(value) > GAZEBO_SIM_MAX_ABS_LINEAR_VELOCITY_MPS
        for value in (velocity_forward_mps, velocity_left_mps, velocity_up_mps)
    ):
        raise RuntimeError("Gazebo SIM Odometry velocity exceeds the safety bound")
    quaternion_norm = math.sqrt(
        orientation_x * orientation_x
        + orientation_y * orientation_y
        + orientation_z * orientation_z
        + orientation_w * orientation_w
    )
    if quaternion_norm <= 0.0 or abs(quaternion_norm - 1.0) > GAZEBO_SIM_QUATERNION_NORM_TOLERANCE:
        raise RuntimeError("Gazebo SIM Odometry quaternion is not normalized")
    orientation_x /= quaternion_norm
    orientation_y /= quaternion_norm
    orientation_z /= quaternion_norm
    orientation_w /= quaternion_norm
    attitude_covariance = _validate_attitude_covariance(
        (
            covariance_xx,
            covariance_xy,
            covariance_xz,
            covariance_yx,
            covariance_yy,
            covariance_yz,
            covariance_zx,
            covariance_zy,
            covariance_zz,
        )
    )
    payload = _read_exact(stream, payload_bytes, "Gazebo SIM BGR24 payload")
    digest_input = header[: GAZEBO_SIM_WIRE_HEADER_BYTES - hashlib.sha256().digest_size]
    digest_input += b"\x00" * hashlib.sha256().digest_size
    observed_digest = hashlib.sha256(digest_input + payload).digest()
    if observed_digest != digest:
        raise RuntimeError("Gazebo SIM record SHA-256 mismatch")
    return GazeboSimWireFrame(
        sequence=sequence,
        sensor_stamp_ns=sensor_stamp_ns,
        width=width,
        height=height,
        stride=stride,
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
        attitude_covariance_enu_rad2=attitude_covariance,
        flags=flags,
        record_sha256=observed_digest.hex(),
        unsigned_header=digest_input,
        payload_bgr24=payload,
    )


def reconstruct_gazebo_sim_wire_frame(
    *,
    unsigned_header_hex: str,
    payload_bgr24: bytes | bytearray | memoryview,
    expected_sequence: int,
    expected_width: int,
    expected_height: int,
    previous_sensor_stamp_ns: int | None = None,
) -> GazeboSimWireFrame:
    """Revalidate one persisted frame against its exact zero-digest wire header.

    The persisted BGR24 file and this fixed v3 header are sufficient to
    reconstruct the original MDGZSIM3 record digest.  Reusing the production
    reader means timestamp, coordinate, odometry, quaternion, pixel-format and
    geometry rules are checked again instead of trusting a re-hashed JSON
    manifest.
    """

    if not isinstance(unsigned_header_hex, str):
        raise RuntimeError("Gazebo SIM unsigned wire header must be hexadecimal text")
    try:
        unsigned_header = bytes.fromhex(unsigned_header_hex)
    except ValueError as exc:
        raise RuntimeError("Gazebo SIM unsigned wire header is not valid hexadecimal") from exc
    if len(unsigned_header) != GAZEBO_SIM_WIRE_HEADER_BYTES:
        raise RuntimeError("Gazebo SIM unsigned wire header size mismatch")
    fields = GAZEBO_SIM_WIRE_HEADER.unpack(unsigned_header)
    if fields[-1] != b"\x00" * hashlib.sha256().digest_size:
        raise RuntimeError("Gazebo SIM unsigned wire header digest field is not zero")
    try:
        payload = bytes(payload_bgr24)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Gazebo SIM persisted payload is not byte-addressable") from exc
    digest = hashlib.sha256(unsigned_header + payload).digest()
    signed_header = unsigned_header[:- len(digest)] + digest
    return read_gazebo_sim_wire_frame(
        io.BytesIO(signed_header + payload),
        expected_sequence=expected_sequence,
        expected_width=expected_width,
        expected_height=expected_height,
        previous_sensor_stamp_ns=previous_sensor_stamp_ns,
    )


def _read_exact(stream: BinaryIO, count: int, name: str) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = stream.read(count - len(chunks))
        if not chunk:
            raise RuntimeError(f"unexpected EOF while reading {name}")
        chunks.extend(chunk)
    return bytes(chunks)


__all__ = [
    "GAZEBO_SIM_ATTITUDE_UNCERTAINTY_SOURCE",
    "GAZEBO_SIM_DEFAULT_MINIMUM_HEADING_SIGMA_DEG",
    "GAZEBO_SIM_DEFAULT_MINIMUM_PITCH_SIGMA_DEG",
    "GAZEBO_SIM_DEFAULT_MINIMUM_ROLL_SIGMA_DEG",
    "GAZEBO_SIM_FLAG_CAMERA_INFO",
    "GAZEBO_SIM_FLAG_NAVSAT",
    "GAZEBO_SIM_FLAG_ODOMETRY",
    "GAZEBO_SIM_KNOWN_FLAGS",
    "GAZEBO_SIM_MAX_FRAME_BYTES",
    "GAZEBO_SIM_OPTIONAL_FLAGS",
    "GAZEBO_SIM_PIXEL_FORMAT_BGR24",
    "GAZEBO_SIM_PIXEL_FORMAT_BGR24_FLAG",
    "GAZEBO_SIM_PIXEL_FORMAT_MASK",
    "GAZEBO_SIM_PIXEL_FORMAT_SHIFT",
    "GAZEBO_SIM_READY_BGR24_FLAGS",
    "GAZEBO_SIM_REQUIRED_FLAGS",
    "GAZEBO_SIM_SOURCE_ROW_ORDER",
    "GAZEBO_SIM_TERRAIN_AGL_REFERENCE",
    "GAZEBO_SIM_VERTICAL_FLIP_APPLIED",
    "GAZEBO_SIM_WIRE_ROW_ORDER",
    "GAZEBO_SIM_WIRE_HEADER",
    "GAZEBO_SIM_WIRE_HEADER_BYTES",
    "GAZEBO_SIM_WIRE_MAGIC",
    "GAZEBO_SIM_WIRE_VERSION",
    "GazeboSimBackend",
    "GazeboSimFrameTelemetryProvenance",
    "GazeboSimFrameProvenance",
    "GazeboSimFrameSource",
    "GazeboSimTelemetryProvider",
    "GazeboSimTransportStats",
    "GazeboSimWireFrame",
    "GazeboSimWireTelemetryProvenance",
    "gazebo_sim_ned_frd_kinematics",
    "gazebo_sim_attitude_sigmas_deg",
    "read_gazebo_sim_wire_frame",
    "reconstruct_gazebo_sim_wire_frame",
]
