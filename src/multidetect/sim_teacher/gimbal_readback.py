"""Command readback in MAVLink degrees (pitch positive up).

Normal MAVLink feedback uses horizon roll/pitch and either vehicle or earth
yaw. The PX4 Gazebo C11 bridge instead reports a world IMU quaternion while
executing body joint setpoints, ignoring the requested stabilization flags.
That compatibility mode must be selected explicitly by the simulation profile.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from multidetect.camera_mount import _mat_mul, _mat_transpose, rpy_rotation

YAW_LOCK = 16
YAW_IN_VEHICLE_FRAME = 32
YAW_IN_EARTH_FRAME = 64
GAZEBO_BODY_JOINTS = "gazebo_world_quaternion_body_joints"


def _field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _wrap(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def attitude_readback(
    status: Any, target: Mapping[str, float], *,
    vehicle_attitude: Any = None, vehicle_yaw_deg: float | None = None,
    yaw_correction_deg: float = 0.0, gazebo_body_joints: bool = False,
) -> dict[str, Any]:
    """Return raw evidence and a comparison in one command reference frame.

    ``vehicle_attitude`` is an ATTITUDE message in radians. In bridge mode it
    must contain the full attitude; yaw subtraction alone is not a rotation
    from earth to body, especially near tailsitter hover.
    """
    raw_q = _field(status, "q")
    result = {
        "raw_q": list(raw_q) if isinstance(raw_q, (list, tuple)) else raw_q,
        "flags": _field(status, "flags"),
        "gimbal_device_id": _field(status, "gimbal_device_id"),
        "time_boot_ms": _field(status, "time_boot_ms"),
        "failure_flags": _field(status, "failure_flags", 0),
        "target_deg": dict(target), "actual_deg": None, "error_deg": None,
        "comparison_frame": "body_FRD_C11_ZXY" if gazebo_body_joints else "horizon_roll_pitch_vehicle_yaw_ZYX",
        "euler_order": "ZXY" if gazebo_body_joints else "ZYX",
        "pitch_convention": "positive up",
    }
    try:
        flags = int(_field(status, "flags", 0) or 0)
        if flags & YAW_IN_VEHICLE_FRAME and flags & YAW_IN_EARTH_FRAME:
            raise ValueError("contradictory yaw frame flags")
        if _field(status, "failure_flags", 0):
            raise ValueError("gimbal device reports failure_flags")
        if not isinstance(raw_q, (list, tuple)) or len(raw_q) != 4:
            raise ValueError("missing quaternion")
        q = tuple(float(v) for v in raw_q)
        norm = math.hypot(*q)
        if not math.isfinite(norm) or norm <= 1e-9:
            raise ValueError("invalid quaternion")
        w, x, y, z = (v / norm for v in q)
        rotation = (
            1 - 2 * (y*y + z*z), 2 * (x*y - w*z), 2 * (x*z + w*y),
            2 * (x*y + w*z), 1 - 2 * (x*x + z*z), 2 * (y*z - w*x),
            2 * (x*z - w*y), 2 * (y*z + w*x), 1 - 2 * (x*x + y*y),
        )
        # Explicit yaw frame bits take precedence over legacy YAW_LOCK.
        earth = bool(flags & YAW_IN_EARTH_FRAME or
                     (not flags & YAW_IN_VEHICLE_FRAME and flags & YAW_LOCK))
        correction = float(yaw_correction_deg or 0.0)
        if not math.isfinite(correction):
            raise ValueError("invalid yaw correction")
        body = None
        if vehicle_attitude is not None:
            body = {axis: float(_field(vehicle_attitude, axis)) for axis in ("roll", "pitch", "yaw")}
            if not all(math.isfinite(v) for v in body.values()):
                raise ValueError("invalid vehicle attitude")
            vehicle_yaw_deg = math.degrees(body["yaw"])
            result["vehicle_attitude_deg"] = {axis: math.degrees(v) for axis, v in body.items()}
            result["vehicle_attitude_time_boot_ms"] = _field(vehicle_attitude, "time_boot_ms")
        if earth:
            # The opt-in Gazebo IMU origin offset is an earth Z rotation,
            # applied BEFORE the full earth -> body transform.
            rotation = _mat_mul(rpy_rotation(roll_rad=0, pitch_rad=0,
                                            yaw_rad=math.radians(correction)), rotation)
        if gazebo_body_joints:
            if earth:
                if body is None:
                    raise ValueError("earth to body readback requires full vehicle attitude")
                body_rotation = rpy_rotation(**{axis + "_rad": value for axis, value in body.items()})
                rotation = _mat_mul(_mat_transpose(body_rotation), rotation)
            # C11 physical joint chain: Rz(yaw) Rx(roll) Ry(pitch).
            # Its roll is limited to +/-45 deg, so this branch is unique.
            roll = math.asin(max(-1.0, min(1.0, rotation[7])))
            pitch = math.atan2(-rotation[6], rotation[8])
            yaw = math.atan2(-rotation[1], rotation[4])
        else:
            # MAVLink's roll/pitch reference is the horizon even in the
            # vehicle-yaw frame; never apply body tilt to this feedback.
            roll = math.atan2(rotation[7], rotation[8])
            pitch = math.asin(max(-1.0, min(1.0, -rotation[6])))
            yaw = math.atan2(rotation[3], rotation[0])
            if earth:
                if vehicle_yaw_deg is None or not math.isfinite(float(vehicle_yaw_deg)):
                    raise ValueError("earth yaw readback requires vehicle heading")
                yaw -= math.radians(float(vehicle_yaw_deg))
        actual = dict(zip(("yaw", "roll", "pitch"),
                          (_wrap(math.degrees(yaw)), math.degrees(roll), math.degrees(pitch))))
        errors = {axis: abs(_wrap(actual[axis] - float(target[axis]))) for axis in actual}
        if not all(math.isfinite(v) for v in (*actual.values(), *errors.values())):
            raise ValueError("nonfinite attitude comparison")
        result.update(actual_deg=actual, error_deg=errors)
    except (TypeError, ValueError, OverflowError) as exc:
        result["readback_error"] = str(exc)
    return result
