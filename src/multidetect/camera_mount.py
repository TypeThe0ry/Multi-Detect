"""Camera installation and gimbal pose composition helpers.

Angles use the simulator's FRD/NED convention: optical ``+X`` is forward,
``+Y`` is image right and ``+Z`` is image down.  A mount rotation maps an
optical-frame vector into the vehicle body frame.  The default fixed mount is
kept deliberately small and is compatible with the historical camera model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

MountKind = Literal["fixed", "gimbal"]
StabilizationMode = Literal["follow-body", "lock-horizon", "follow-yaw"]
RotationFrame = Literal["body", "world"]


def _finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _mat_mul(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        sum(a[row * 3 + k] * b[k * 3 + col] for k in range(3))
        for row in range(3)
        for col in range(3)
    )


def _mat_transpose(a: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(a[col * 3 + row] for row in range(3) for col in range(3))


def _mat_vec(a: tuple[float, ...], v: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(
        a[row * 3] * v[0] + a[row * 3 + 1] * v[1] + a[row * 3 + 2] * v[2]
        for row in range(3)
    )


def rpy_rotation(*, roll_rad: float, pitch_rad: float, yaw_rad: float) -> tuple[float, ...]:
    """Return ``Rz(yaw) Ry(pitch) Rx(roll)`` in row-major form."""
    roll_rad = _finite(roll_rad, "roll_rad")
    pitch_rad = _finite(pitch_rad, "pitch_rad")
    yaw_rad = _finite(yaw_rad, "yaw_rad")
    cr, sr = math.cos(roll_rad), math.sin(roll_rad)
    cp, sp = math.cos(pitch_rad), math.sin(pitch_rad)
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    return (
        cy * cp,
        cy * sp * sr - sy * cr,
        cy * sp * cr + sy * sr,
        sy * cp,
        sy * sp * sr + cy * cr,
        sy * sp * cr - cy * sr,
        -sp,
        cp * sr,
        cp * cr,
    )


def _mount_rotation(*, roll_rad: float, pitch_rad: float, yaw_rad: float) -> tuple[float, ...]:
    """Return optical-to-frame rotation; mount pitch is positive toward NED down."""
    return rpy_rotation(roll_rad=roll_rad, pitch_rad=-pitch_rad, yaw_rad=yaw_rad)


@dataclass(frozen=True, slots=True)
class GimbalLimits:
    """Inclusive angle limits in radians."""

    pitch_min_rad: float = -math.pi
    pitch_max_rad: float = math.pi
    yaw_min_rad: float = -math.pi
    yaw_max_rad: float = math.pi
    roll_min_rad: float = -math.pi
    roll_max_rad: float = math.pi

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.pitch_min_rad > self.pitch_max_rad:
            raise ValueError("pitch limits are reversed")
        if self.yaw_min_rad > self.yaw_max_rad:
            raise ValueError("yaw limits are reversed")
        if self.roll_min_rad > self.roll_max_rad:
            raise ValueError("roll limits are reversed")


@dataclass(frozen=True, slots=True)
class CameraMount:
    """Camera mount state and the policy used to compose its orientation.

    The single internal angle convention is: ``pitch_rad > 0`` means the
    optical axis points toward NED ``+Z`` (down).  MAVLink feedback/commands and
    airframe profile limits use positive-up pitch and are converted at their
    boundaries before reaching this class.

    ``pitch_rad/yaw_rad/roll_rad`` describe the mount relative to the body for
    ``fixed`` and ``follow-body``.  For stabilized modes they describe the
    requested camera attitude in the world frame: ``lock-horizon`` keeps the
    requested yaw independent of vehicle yaw, while ``follow-yaw`` adds the
    vehicle yaw to it.  The translation remains a body-frame lever arm.
    """

    kind: MountKind = "fixed"
    pitch_rad: float = 0.0
    yaw_rad: float = 0.0
    roll_rad: float = 0.0
    offset_body_ned_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    limits: GimbalLimits = GimbalLimits()
    rate_rad_s: float | None = None
    stabilization: StabilizationMode = "follow-body"

    def __post_init__(self) -> None:
        if self.kind not in {"fixed", "gimbal"}:
            raise ValueError("kind must be fixed or gimbal")
        if self.stabilization not in {"follow-body", "lock-horizon", "follow-yaw"}:
            raise ValueError("unsupported gimbal stabilization mode")
        if self.kind == "fixed" and self.stabilization != "follow-body":
            raise ValueError("fixed mounts must use follow-body stabilization")
        for name in ("pitch_rad", "yaw_rad", "roll_rad"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if len(self.offset_body_ned_m) != 3:
            raise ValueError("offset_body_ned_m must contain three values")
        object.__setattr__(
            self,
            "offset_body_ned_m",
            tuple(_finite(value, "offset_body_ned_m[]") for value in self.offset_body_ned_m),
        )
        if self.kind == "gimbal":
            for axis, value, lower, upper in (
                ("pitch", self.pitch_rad, self.limits.pitch_min_rad, self.limits.pitch_max_rad),
                ("yaw", self.yaw_rad, self.limits.yaw_min_rad, self.limits.yaw_max_rad),
                ("roll", self.roll_rad, self.limits.roll_min_rad, self.limits.roll_max_rad),
            ):
                if not lower <= value <= upper:
                    raise ValueError(f"{axis}_rad is outside the gimbal limits")
        if self.rate_rad_s is not None:
            rate = _finite(self.rate_rad_s, "rate_rad_s")
            if rate <= 0.0:
                raise ValueError("rate_rad_s must be positive")
            object.__setattr__(self, "rate_rad_s", rate)

    @classmethod
    def fixed(
        cls,
        *,
        pitch_rad: float = 0.0,
        yaw_rad: float = 0.0,
        roll_rad: float = 0.0,
        offset_body_ned_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> CameraMount:
        return cls(
            kind="fixed",
            pitch_rad=pitch_rad,
            yaw_rad=yaw_rad,
            roll_rad=roll_rad,
            offset_body_ned_m=offset_body_ned_m,
        )

    @classmethod
    def gimbal(
        cls,
        *,
        pitch_rad: float = 0.0,
        yaw_rad: float = 0.0,
        roll_rad: float = 0.0,
        offset_body_ned_m: tuple[float, float, float] = (0.0, 0.0, 0.0),
        limits: GimbalLimits | None = None,
        rate_rad_s: float | None = None,
        stabilization: StabilizationMode = "lock-horizon",
    ) -> CameraMount:
        return cls(
            kind="gimbal",
            pitch_rad=pitch_rad,
            yaw_rad=yaw_rad,
            roll_rad=roll_rad,
            offset_body_ned_m=offset_body_ned_m,
            limits=limits or GimbalLimits(),
            rate_rad_s=rate_rad_s,
            stabilization=stabilization,
        )

    def optical_to_body_rotation(
        self, *, vehicle_roll_rad: float, vehicle_pitch_rad: float, vehicle_yaw_rad: float
    ) -> tuple[float, ...]:
        """Compose the optical-frame-to-body rotation for this vehicle attitude."""
        body_world = rpy_rotation(
            roll_rad=vehicle_roll_rad, pitch_rad=vehicle_pitch_rad, yaw_rad=vehicle_yaw_rad
        )
        if self.kind == "fixed" or self.stabilization == "follow-body":
            return _mount_rotation(
                roll_rad=self.roll_rad, pitch_rad=self.pitch_rad, yaw_rad=self.yaw_rad
            )
        if self.stabilization == "lock-horizon":
            world_camera = _mount_rotation(
                roll_rad=self.roll_rad, pitch_rad=self.pitch_rad, yaw_rad=self.yaw_rad
            )
        else:  # follow-yaw: stabilize pitch/roll while following vehicle heading.
            world_camera = _mount_rotation(
                roll_rad=self.roll_rad,
                pitch_rad=self.pitch_rad,
                yaw_rad=vehicle_yaw_rad + self.yaw_rad,
            )
        return _mat_mul(_mat_transpose(body_world), world_camera)


@dataclass(frozen=True, slots=True)
class ComposedCameraPose:
    """Camera origin and optical axes in world NED coordinates."""

    center_ned: tuple[float, float, float]
    optical_to_world: tuple[float, ...]


def compose_camera_pose(
    vehicle_position_ned: tuple[float, float, float],
    *,
    vehicle_roll_rad: float,
    vehicle_pitch_rad: float,
    vehicle_yaw_rad: float,
    mount: CameraMount,
    rotation_override: tuple[float, ...] | None = None,
    rotation_frame: RotationFrame = "body",
) -> ComposedCameraPose:
    """Compose ``vehicle attitude x gimbal attitude x mount offset``.

    ``rotation_override`` is used for a feedback quaternion.  It is an optical
    frame rotation expressed in either the vehicle body or world frame and lets
    callers preserve the physical attitude when Euler decomposition wraps at
    nadir.
    """
    if len(vehicle_position_ned) != 3:
        raise ValueError("vehicle_position_ned must contain three values")
    position = tuple(_finite(value, "vehicle_position_ned[]") for value in vehicle_position_ned)
    if rotation_frame not in {"body", "world"}:
        raise ValueError("rotation_frame must be body or world")
    if rotation_override is not None and len(rotation_override) != 9:
        raise ValueError("rotation_override must contain nine values")
    body_world = rpy_rotation(
        roll_rad=vehicle_roll_rad, pitch_rad=vehicle_pitch_rad, yaw_rad=vehicle_yaw_rad
    )
    offset_world = _mat_vec(body_world, mount.offset_body_ned_m)
    center = tuple(position[index] + offset_world[index] for index in range(3))
    if rotation_override is None:
        relative = mount.optical_to_body_rotation(
            vehicle_roll_rad=vehicle_roll_rad,
            vehicle_pitch_rad=vehicle_pitch_rad,
            vehicle_yaw_rad=vehicle_yaw_rad,
        )
        optical_to_world = _mat_mul(body_world, relative)
    elif rotation_frame == "body":
        optical_to_world = _mat_mul(body_world, tuple(float(v) for v in rotation_override))
    else:
        optical_to_world = tuple(float(v) for v in rotation_override)
    return ComposedCameraPose(center_ned=center, optical_to_world=optical_to_world)


__all__ = [
    "CameraMount",
    "ComposedCameraPose",
    "GimbalLimits",
    "compose_camera_pose",
    "rpy_rotation",
]
