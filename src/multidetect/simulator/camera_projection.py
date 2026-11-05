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
from dataclasses import replace
from typing import Any

from multidetect.autonomy.ground_model import GroundModel
from multidetect.camera_mount import CameraMount, GimbalLimits, compose_camera_pose

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


def _recorded_camera_pose(
    pose: Mapping[str, Any], *, camera_mount: CameraMount | None = None
) -> tuple[tuple[float, float, float], tuple[float, ...]] | None:
    """Return a frame camera pose, composing legacy gimbal feedback when needed.

    Recorded ``camera_pose`` is authoritative.  Older gimbal recordings may
    contain only ``gimbal_state`` (or its joint-state spelling), so compose the
    feedback against the measured vehicle pose before falling back to the
    historical fixed-camera geometry.
    """
    camera = pose.get("camera_pose")
    if not isinstance(camera, Mapping):
        camera = None
    center = camera.get("center_ned") if camera is not None else None
    rotation = camera.get("optical_to_world") if camera is not None else None
    if not isinstance(center, Sequence) or isinstance(center, (str, bytes)) or len(center) != 3:
        center = rotation = None
    if not isinstance(rotation, Sequence) or isinstance(rotation, (str, bytes)) or len(rotation) != 9:
        center = rotation = None
    if center is not None and rotation is not None:
        values = tuple(_finite(value) for value in (*center, *rotation))
        if not any(value is None for value in values):
            return tuple(float(value) for value in center), tuple(float(value) for value in rotation)

    state = pose.get("gimbal_state")
    angle_names = ("pitch_rad", "yaw_rad", "roll_rad")
    if not isinstance(state, Mapping):
        joint = pose.get("gimbal_joint_state")
        state = (
            joint.get("position_rad")
            if isinstance(joint, Mapping) and joint.get("source") not in {"fixed_mount", "unavailable"}
            else None
        )
        angle_names = ("pitch", "yaw", "roll")
    if not isinstance(state, Mapping):
        return None
    angles = tuple(_finite(state.get(name)) for name in angle_names)
    if any(value is None for value in angles):
        return None
    values = _pose_values(pose)
    if values is None:
        return None
    base = camera_mount if camera_mount is not None and camera_mount.kind == "gimbal" else CameraMount.gimbal()
    yaw_frame = state.get("yaw_frame")
    stabilization = (
        "lock-horizon" if yaw_frame == "world" else "follow-body"
        if yaw_frame == "body" else base.stabilization
    )
    if not (
        base.limits.pitch_min_rad <= float(angles[0]) <= base.limits.pitch_max_rad
        and base.limits.yaw_min_rad <= float(angles[1]) <= base.limits.yaw_max_rad
        and base.limits.roll_min_rad <= float(angles[2]) <= base.limits.roll_max_rad
    ):
        # Keep replay geometry available for recorded feedback marked
        # out-of-limits; the observation is still the measured pose.
        base = replace(
            base,
            limits=GimbalLimits(
                pitch_min_rad=-math.pi,
                pitch_max_rad=math.pi,
                yaw_min_rad=-math.pi,
                yaw_max_rad=math.pi,
                roll_min_rad=-math.pi,
                roll_max_rad=math.pi,
            ),
        )
    mount = replace(
        base,
        pitch_rad=float(angles[0]),
        yaw_rad=float(angles[1]),
        roll_rad=float(angles[2]),
        stabilization=stabilization,
    )
    rotation_override = state.get("rotation")
    if isinstance(rotation_override, Sequence) and not isinstance(rotation_override, (str, bytes)) and len(rotation_override) == 9:
        parsed = tuple(_finite(value) for value in rotation_override)
        if all(value is not None for value in parsed):
            rotation_frame = yaw_frame if yaw_frame in {"body", "world"} else "body"
            composed = compose_camera_pose(
                tuple(values[:3]),
                vehicle_roll_rad=math.radians(values[3]),
                vehicle_pitch_rad=math.radians(values[4]),
                vehicle_yaw_rad=math.radians(values[5]),
                mount=mount,
                rotation_override=tuple(float(value) for value in parsed),
                rotation_frame=rotation_frame,
            )
            return composed.center_ned, composed.optical_to_world
    composed = compose_camera_pose(
        tuple(values[:3]),
        vehicle_roll_rad=math.radians(values[3]),
        vehicle_pitch_rad=math.radians(values[4]),
        vehicle_yaw_rad=math.radians(values[5]),
        mount=mount,
    )
    return composed.center_ned, composed.optical_to_world


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


def _body_to_world_ned(
    vector_body_ned: tuple[float, float, float], *, roll_rad: float, pitch_rad: float,
    yaw_rad: float,
) -> tuple[float, float, float]:
    """Apply Rz(yaw) Ry(pitch) Rx(roll), matching ``_world_to_body_ned``."""
    x, y, z = vector_body_ned
    cr, sr = math.cos(roll_rad), math.sin(roll_rad)
    y1, z1 = cr * y - sr * z, sr * y + cr * z
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    x2, z2 = cp * x + sp * z1, -sp * x + cp * z1
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    return cy * x2 - sy * y1, sy * x2 + cy * y1, z2


def camera_center_ned_up(
    pose: Mapping[str, Any], *, camera_mount: CameraMount | None = None
) -> tuple[float, float, float] | None:
    """Return camera centre as ``(north_m, east_m, up_m)`` for a recorded pose."""
    recorded = _recorded_camera_pose(pose, camera_mount=camera_mount)
    if recorded is not None:
        return recorded[0][0], recorded[0][1], -recorded[0][2]
    values = _pose_values(pose)
    if values is None:
        return None
    north, east, down, roll_deg, pitch_deg, yaw_deg = values
    if camera_mount is not None:
        composed = compose_camera_pose(
            (north, east, down),
            vehicle_roll_rad=math.radians(roll_deg),
            vehicle_pitch_rad=math.radians(pitch_deg),
            vehicle_yaw_rad=math.radians(yaw_deg),
            mount=camera_mount,
        )
        return composed.center_ned[0], composed.center_ned[1], -composed.center_ned[2]
    roll, pitch, yaw = (math.radians(value) for value in (roll_deg, pitch_deg, yaw_deg))
    mount_world = _body_to_world_ned(
        (CAMERA_MOUNT_X_M, 0.0, -CAMERA_MOUNT_Z_DOWN_M),
        roll_rad=roll,
        pitch_rad=pitch,
        yaw_rad=yaw,
    )
    return north + mount_world[0], east + mount_world[1], -(down + mount_world[2])


def _pixel_camera_ray_raw(
    u: float,
    v: float,
    *,
    pose: Mapping[str, Any],
    camera_info: Mapping[str, Any] | None,
    image_size: tuple[int, int],
    camera_pitch_rad: float = CAMERA_MOUNT_PITCH_RAD,
    camera_mount: CameraMount | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Shared inverse projection; retain the legacy unnormalized ground ray."""
    recorded = _recorded_camera_pose(pose, camera_mount=camera_mount)
    if recorded is not None:
        fx, fy, cx, cy = _intrinsics(camera_info, image_size)
        x, y, z = 1.0, (u - cx) / fx, (v - cy) / fy
        center, rotation = recorded
        ray = tuple(rotation[row * 3] * x + rotation[row * 3 + 1] * y + rotation[row * 3 + 2] * z for row in range(3))
        return (center[0], center[1], -center[2]), ray
    values = _pose_values(pose)
    centre = camera_center_ned_up(pose, camera_mount=camera_mount)
    if values is None or centre is None:
        return None
    _, _, _, roll_deg, pitch_deg, yaw_deg = values
    fx, fy, cx, cy = _intrinsics(camera_info, image_size)
    # Optical frame of _body_to_camera_optical: +X forward, +Y right, +Z down.
    x, y, z = 1.0, (u - cx) / fx, (v - cy) / fy
    if camera_mount is not None:
        composed = compose_camera_pose(
            (values[0], values[1], values[2]),
            vehicle_roll_rad=math.radians(values[3]),
            vehicle_pitch_rad=math.radians(values[4]),
            vehicle_yaw_rad=math.radians(values[5]),
            mount=camera_mount,
        )
        optical_to_world = composed.optical_to_world
        north = optical_to_world[0] * x + optical_to_world[1] * y + optical_to_world[2] * z
        east = optical_to_world[3] * x + optical_to_world[4] * y + optical_to_world[5] * z
        down = optical_to_world[6] * x + optical_to_world[7] * y + optical_to_world[8] * z
        return centre, (north, east, down)
    cp, sp = math.cos(camera_pitch_rad), math.sin(camera_pitch_rad)
    body = (cp * x - sp * z, y, sp * x + cp * z)
    north, east, down = _body_to_world_ned(
        body,
        roll_rad=math.radians(roll_deg),
        pitch_rad=math.radians(pitch_deg),
        yaw_rad=math.radians(yaw_deg),
    )
    return centre, (north, east, down)


def pixel_camera_ray(
    u: float,
    v: float,
    *,
    pose: Mapping[str, Any],
    camera_info: Mapping[str, Any] | None,
    image_size: tuple[int, int],
    camera_pitch_rad: float = CAMERA_MOUNT_PITCH_RAD,
    camera_mount: CameraMount | None = None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Return the camera centre and unit pixel ray, both in true N/E/down.

    Shares every camera operation with ``pixel_ground_point``. No simulator
    truth is used; the inputs are only the measured pose and camera calibration.
    """
    result = _pixel_camera_ray_raw(
        u, v, pose=pose, camera_info=camera_info, image_size=image_size,
        camera_pitch_rad=camera_pitch_rad, camera_mount=camera_mount,
    )
    if result is None:
        return None
    centre, ray = result
    norm = math.sqrt(sum(value * value for value in ray))
    if not math.isfinite(norm) or norm <= 0 or not all(math.isfinite(x) for x in centre):
        return None
    return (centre[0], centre[1], -centre[2]), tuple(value / norm for value in ray)


def pixel_ground_point(
    u: float,
    v: float,
    *,
    pose: Mapping[str, Any],
    camera_info: Mapping[str, Any] | None,
    image_size: tuple[int, int],
    ground_up_m: float = 0.0,
    camera_pitch_rad: float = CAMERA_MOUNT_PITCH_RAD,
    ground_model: GroundModel | None = None,
    camera_mount: CameraMount | None = None,
) -> tuple[float, float] | None:
    """Invert ``project_target_bbox`` on a surface, preserving legacy arithmetic."""
    result = _pixel_camera_ray_raw(
        u, v, pose=pose, camera_info=camera_info, image_size=image_size,
        camera_pitch_rad=camera_pitch_rad, camera_mount=camera_mount,
    )
    if result is None:
        return None
    centre, (north, east, down) = result
    if ground_model is not None:
        return ground_model.intersect_ray(centre, (north, east, down))
    height = centre[2] - ground_up_m
    if down <= 1e-6 or height <= 0:
        return None
    t = height / down
    return centre[0] + north * t, centre[1] + east * t


def _segment_intersects_obstacle(
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    obstacle: Any,
) -> bool:
    """Segment/slab test against an oriented N/E/up box from ground to top."""
    c, s = math.cos(float(obstacle.yaw_rad)), math.sin(float(obstacle.yaw_rad))
    # SDF yaw rotates ENU local x/east, y/north. Convert N/E into local y/x.
    def local(point: tuple[float, float, float]) -> tuple[float, float, float]:
        dn, de = point[0] - float(obstacle.north_m), point[1] - float(obstacle.east_m)
        return c * de + s * dn, -s * de + c * dn, point[2]

    a, b = local(start), local(end)
    direction = tuple(b[i] - a[i] for i in range(3))
    half = (float(obstacle.size_east_m) / 2.0, float(obstacle.size_north_m) / 2.0)
    bounds = ((-half[0], half[0]), (-half[1], half[1]), (0.0, float(obstacle.top_m)))
    t_min, t_max = 0.0, 1.0
    for index, (lo, hi) in enumerate(bounds):
        if abs(direction[index]) <= 1e-12:
            if a[index] < lo or a[index] > hi:
                return False
            continue
        t1, t2 = (lo - a[index]) / direction[index], (hi - a[index]) / direction[index]
        if t1 > t2:
            t1, t2 = t2, t1
        t_min, t_max = max(t_min, t1), min(t_max, t2)
        if t_min > t_max:
            return False
    return True


def visible_fraction(
    *,
    pose: Mapping[str, Any],
    target_ned: tuple[float, float, float],
    target_size_m: tuple[float, float, float],
    target_yaw_rad: float = 0.0,
    obstacles: Sequence[Any] = (),
    target_bottom_offset_m: float | None = None,
) -> float:
    """Estimate target visibility by casting a deterministic 3x3x3 target grid.

    Coordinates are N/E/up, while ``pose`` retains the projection API's NED
    vehicle pose. Obstacles must expose ``north_m/east_m/top_m/size_*_m/yaw_rad``.
    """
    if len(target_size_m) != 3 or any(float(value) <= 0.0 for value in target_size_m):
        raise ValueError("target_size_m must contain three positive dimensions")
    camera = camera_center_ned_up(pose)
    if camera is None:
        return 0.0
    sx, sy, sz = (float(value) for value in target_size_m)
    centre_up = float(target_ned[2]) + (
        float(target_bottom_offset_m) if target_bottom_offset_m is not None else 0.0
    )
    if target_bottom_offset_m is not None:
        centre_up += sz / 2.0
    c, s = math.cos(float(target_yaw_rad)), math.sin(float(target_yaw_rad))
    blocked = 0
    total = 0
    for ix in (-1, 0, 1):
        for iy in (-1, 0, 1):
            for iz in (-1, 0, 1):
                local_x, local_y = ix * sx / 2.0, iy * sy / 2.0
                east = float(target_ned[1]) + c * local_x - s * local_y
                north = float(target_ned[0]) + s * local_x + c * local_y
                point = (north, east, centre_up + iz * sz / 2.0)
                total += 1
                if any(
                    _segment_intersects_obstacle(camera, point, obstacle)
                    for obstacle in obstacles
                ):
                    blocked += 1
    return max(0.0, min(1.0, 1.0 - blocked / total))


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
    target_bottom_offset_m: float | None = None,
    camera_mount: CameraMount | None = None,
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
    recorded = _recorded_camera_pose(pose, camera_mount=camera_mount)
    target_north, target_east, target_up = target_ned
    if not all(
        math.isfinite(float(value))
        for value in (target_north, target_east, target_up, target_yaw_rad)
    ):
        return None
    # Legacy truth stores the target centre in ``target_up``.  Asset truth can
    # instead store the model-origin height and the measured mesh bottom
    # offset; in that case derive the centre from bottom + half-height.
    sx, sy, sz = target_size_m
    if target_bottom_offset_m is None:
        centre_up = float(target_up)
    else:
        bottom_offset = _finite(target_bottom_offset_m)
        if bottom_offset is None:
            return None
        centre_up = float(target_up) + bottom_offset + sz / 2.0
    roll, pitch, yaw = (math.radians(value) for value in (roll_deg, pitch_deg, yaw_deg))
    fx, fy, cx, cy = _intrinsics(camera_info, image_size)
    composed = None
    if camera_mount is not None:
        composed = compose_camera_pose(
            (pn, pe, p_down),
            vehicle_roll_rad=roll,
            vehicle_pitch_rad=pitch,
            vehicle_yaw_rad=yaw,
            mount=camera_mount,
        )
    corners: list[tuple[float, float]] = []
    cos_target, sin_target = math.cos(target_yaw_rad), math.sin(target_yaw_rad)
    for dx in (-sx / 2.0, sx / 2.0):
        for dy in (-sy / 2.0, sy / 2.0):
            for dz in (-sz / 2.0, sz / 2.0):
                # Generated SDF target x is east and y is north.
                offset_east = cos_target * dx - sin_target * dy
                offset_north = sin_target * dx + cos_target * dy
                world_point = (
                    float(target_north) + offset_north,
                    float(target_east) + offset_east,
                    -(centre_up + dz),
                )
                if recorded is not None:
                    vector_ned = tuple(world_point[index] - recorded[0][index] for index in range(3))
                    optical_to_world = recorded[1]
                    cam_x = sum(optical_to_world[3 * row] * vector_ned[row] for row in range(3))
                    cam_y = sum(optical_to_world[3 * row + 1] * vector_ned[row] for row in range(3))
                    cam_z = sum(optical_to_world[3 * row + 2] * vector_ned[row] for row in range(3))
                elif composed is None:
                    vector_ned = (
                        world_point[0] - pn,
                        world_point[1] - pe,
                        world_point[2] - p_down,
                    )
                    body = _world_to_body_ned(
                        vector_ned, roll_rad=roll, pitch_rad=pitch, yaw_rad=yaw
                    )
                    cam_x, cam_y, cam_z = _body_to_camera_optical(
                        body, mount_pitch_rad=camera_pitch_rad
                    )
                else:
                    vector_ned = tuple(
                        world_point[index] - composed.center_ned[index] for index in range(3)
                    )
                    optical_to_world = composed.optical_to_world
                    cam_x = (
                        optical_to_world[0] * vector_ned[0]
                        + optical_to_world[3] * vector_ned[1]
                        + optical_to_world[6] * vector_ned[2]
                    )
                    cam_y = (
                        optical_to_world[1] * vector_ned[0]
                        + optical_to_world[4] * vector_ned[1]
                        + optical_to_world[7] * vector_ned[2]
                    )
                    cam_z = (
                        optical_to_world[2] * vector_ned[0]
                        + optical_to_world[5] * vector_ned[1]
                        + optical_to_world[8] * vector_ned[2]
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


__all__ = [
    "camera_center_ned_up", "pixel_camera_ray", "pixel_ground_point",
    "project_target_bbox", "visible_fraction",
]
