"""Project generated-world target boxes into the Gazebo camera image.

The x500 camera is mounted in ``base_link`` at ``(0.18, 0, -0.12)`` with a
0.18 radian downward pitch (``simulation/gazebo/models/x500_mono_cam/model.sdf``).
Gazebo's camera optical axis is +X.  The projection below keeps the simulator's
NED body convention (X forward, Y right, Z down) until the final pinhole step,
where +Y projects right and +Z projects down in the image.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

CAMERA_MOUNT_X_M = 0.18
CAMERA_MOUNT_Z_DOWN_M = 0.12
CAMERA_MOUNT_PITCH_RAD = 0.18


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _intrinsics(
    camera_info: Mapping[str, Any] | None, image_size: tuple[int, int]
) -> tuple[float, float, float, float]:
    """Read pinhole intrinsics and scale only when metadata/image sizes differ."""
    width, height = image_size
    info = camera_info if isinstance(camera_info, Mapping) else {}

    def value(*names: str) -> float | None:
        for name in names:
            parsed = _finite(info.get(name))
            if parsed is not None and parsed > 0.0:
                return parsed
        return None

    fx = value("fx", "fx_px") or float(width)
    fy = value("fy", "fy_px") or float(height)
    cx = value("cx", "cx_px")
    cy = value("cy", "cy_px")
    metadata_width = value("width", "image_width")
    metadata_height = value("height", "image_height")
    if metadata_width is not None and metadata_height is not None:
        scale_x = float(width) / metadata_width
        scale_y = float(height) / metadata_height
        fx *= scale_x
        fy *= scale_y
        if cx is not None:
            cx *= scale_x
        if cy is not None:
            cy *= scale_y
    return (
        fx,
        fy,
        float(width) / 2.0 if cx is None else cx,
        float(height) / 2.0 if cy is None else cy,
    )


def _pose_values(pose: Mapping[str, Any]) -> tuple[float, float, float, float, float, float] | None:
    position = pose.get("local_ned_m")
    if (
        not isinstance(position, Sequence)
        or isinstance(position, (str, bytes))
        or len(position) < 3
    ):
        nested = pose.get("pose")
        if isinstance(nested, Mapping):
            position = nested.get("local_ned_m")
    if (
        not isinstance(position, Sequence)
        or isinstance(position, (str, bytes))
        or len(position) < 3
    ):
        return None
    north, east, down = (_finite(position[index]) for index in range(3))
    attitude = pose.get("attitude")
    if not isinstance(attitude, Mapping) and isinstance(pose.get("pose"), Mapping):
        attitude = pose["pose"].get("attitude")
    if not isinstance(attitude, Mapping):
        attitude = {}
    roll = _finite(attitude.get("roll_deg", 0.0))
    pitch = _finite(attitude.get("pitch_deg", 0.0))
    yaw = _finite(attitude.get("yaw_deg", 0.0))
    if None in (north, east, down, roll, pitch, yaw):
        return None
    return north, east, down, roll, pitch, yaw


def _world_to_body_ned(
    vector_ned: tuple[float, float, float], *, roll_rad: float, pitch_rad: float, yaw_rad: float
) -> tuple[float, float, float]:
    """Apply the inverse of Rz(yaw) Ry(pitch) Rx(roll)."""
    north, east, down = vector_ned
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    # Inverse yaw (world N/E to body forward/right).
    x1 = cy * north + sy * east
    y1 = -sy * north + cy * east
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    # Inverse pitch, then inverse roll.
    x2 = cp * x1 - sp * down
    z2 = sp * x1 + cp * down
    cr, sr = math.cos(roll_rad), math.sin(roll_rad)
    y3 = cr * y1 + sr * z2
    z3 = -sr * y1 + cr * z2
    return x2, y3, z3


def _body_to_camera_optical(
    vector_body_ned: tuple[float, float, float], *,
    mount_pitch_rad: float,
) -> tuple[float, float, float]:
    """Apply the camera translation and downward mount pitch.

    The returned coordinates use the image convention +X forward, +Y right,
    +Z down.  A point on the body forward axis therefore moves upward in the
    image when the camera is pitched down, as it does in the SDF.
    """
    body_x, body_y, body_z = vector_body_ned
    body_x -= CAMERA_MOUNT_X_M
    # The SDF camera is 0.12 m above base_link (z_down=-0.12), so the
    # target-to-camera vector gains +0.12 m in the down direction.
    body_z += CAMERA_MOUNT_Z_DOWN_M
    cp, sp = math.cos(mount_pitch_rad), math.sin(mount_pitch_rad)
    return cp * body_x + sp * body_z, body_y, -sp * body_x + cp * body_z


def project_target_bbox(
    *,
    target_ned: tuple[float, float, float],
    pose: Mapping[str, Any],
    camera_info: Mapping[str, Any] | None,
    image_size: tuple[int, int],
    target_size_m: tuple[float, float, float] = (3.0, 1.8, 1.5),
    camera_pitch_rad: float = CAMERA_MOUNT_PITCH_RAD,
    target_yaw_rad: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Project an upright SDF box and return clipped pixel ``(x1,y1,x2,y2)``.

    ``target_ned`` is ordered ``(north_m, east_m, z_up_m)`` for compatibility
    with the recorded teacher truth.  Its third component is relative to the
    Gazebo WORLD origin; it is never interpreted as AMSL.
    """
    if len(target_size_m) != 3 or any(
        size <= 0.0 or not math.isfinite(size) for size in target_size_m
    ):
        raise ValueError("target_size_m must contain three finite positive dimensions")
    if len(image_size) != 2 or image_size[0] <= 0 or image_size[1] <= 0:
        raise ValueError("image_size must be positive (width, height)")
    values = _pose_values(pose)
    if values is None:
        return None
    pn, pe, p_down, roll_deg, pitch_deg, yaw_deg = values
    target_north, target_east, target_up = target_ned
    if not all(
        math.isfinite(float(value))
        for value in (target_north, target_east, target_up, target_yaw_rad)
    ):
        return None
    relative_up = float(target_up) + p_down
    roll, pitch, yaw = (math.radians(value) for value in (roll_deg, pitch_deg, yaw_deg))
    fx, fy, cx, cy = _intrinsics(camera_info, image_size)
    sx, sy, sz = target_size_m
    corners: list[tuple[float, float]] = []
    cos_target, sin_target = math.cos(target_yaw_rad), math.sin(target_yaw_rad)
    for dx in (-sx / 2.0, sx / 2.0):
        for dy in (-sy / 2.0, sy / 2.0):
            for dz in (-sz / 2.0, sz / 2.0):
                # Generated SDF target x is east and y is north.
                offset_east = cos_target * dx - sin_target * dy
                offset_north = sin_target * dx + cos_target * dy
                vector_ned = (
                    float(target_north) + offset_north - pn,
                    float(target_east) + offset_east - pe,
                    -relative_up - dz,
                )
                body = _world_to_body_ned(vector_ned, roll_rad=roll, pitch_rad=pitch, yaw_rad=yaw)
                cam_x, cam_y, cam_z = _body_to_camera_optical(
                    body, mount_pitch_rad=camera_pitch_rad
                )
                if cam_x <= 1e-6:
                    continue
                corners.append((cx + fx * cam_y / cam_x, cy + fy * cam_z / cam_x))
    if not corners:
        return None
    width, height = image_size
    x1 = max(0.0, min(point[0] for point in corners))
    y1 = max(0.0, min(point[1] for point in corners))
    x2 = min(float(width), max(point[0] for point in corners))
    y2 = min(float(height), max(point[1] for point in corners))
    return None if x2 <= x1 or y2 <= y1 else (x1, y1, x2, y2)


__all__ = ["project_target_bbox"]
