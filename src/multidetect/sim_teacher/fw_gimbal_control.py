"""Opt-in fixed-wing X1 hand-back and waypoint-pass gimbal helpers."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from .target_tracking import WorldTarget, unit, world_target_direction


FW_TERMINAL_LOCK_ETA_S = 3.0
FW_TERMINAL_LOCK_MAX_STATE_AGE_S = 0.2
FW_HANDOFF_MAX_HORIZON_ERROR_DEG = 4.0
FW_SHORT_LEG_MAX_M = 80.0
FW_PASS_SWITCH_MIN_DEG = 8.0
FW_PASS_MAX_OLD_LOS_DEFLECTION_DEG = 6.5


def estimate_fw_terminal_eta_s(
    *,
    enabled: bool,
    has_end_tangent_plane: bool,
    position_ned_m: tuple[float, float, float] | None,
    velocity_ned_mps: tuple[float, float, float] | None,
    endpoint_ned_m: tuple[float, float, float] | None,
    tangent_ne: tuple[float, float] | None,
    position_age_s: float | None,
    velocity_age_s: float | None,
    max_age_s: float = FW_TERMINAL_LOCK_MAX_STATE_AGE_S,
) -> tuple[float, float, float] | None:
    """Return (ETA, remaining along-track metres, forward speed), only from fresh state."""
    if not enabled or not has_end_tangent_plane:
        return None
    vectors = (position_ned_m, velocity_ned_mps, endpoint_ned_m, tangent_ne)
    if any(value is None for value in vectors):
        return None
    if any(age is None or not math.isfinite(age) or age < 0 or age > max_age_s
           for age in (position_age_s, velocity_age_s)):
        return None
    try:
        position = tuple(float(v) for v in position_ned_m)
        velocity = tuple(float(v) for v in velocity_ned_mps)
        endpoint = tuple(float(v) for v in endpoint_ned_m)
        tangent = tuple(float(v) for v in tangent_ne)
    except (TypeError, ValueError):
        return None
    if (len(position) != 3 or len(velocity) != 3 or len(endpoint) != 3 or len(tangent) != 2
            or not all(math.isfinite(v) for v in (*position, *velocity, *endpoint, *tangent))):
        return None
    tangent_length = math.hypot(*tangent)
    if tangent_length <= 1e-9:
        return None
    tn, te = tangent[0] / tangent_length, tangent[1] / tangent_length
    remaining = (endpoint[0] - position[0]) * tn + (endpoint[1] - position[1]) * te
    forward_speed = velocity[0] * tn + velocity[1] * te
    if remaining < 0 or forward_speed <= 0:
        return None
    return remaining / forward_speed, remaining, forward_speed


class X1FwTerminalLockGate:
    """One-shot ETA trigger with dispatch-aware, bounded hand-off diagnostics."""

    def __init__(self, *, enabled: bool, clock=time.monotonic,
                 trigger_eta_s: float = FW_TERMINAL_LOCK_ETA_S) -> None:
        if not math.isfinite(trigger_eta_s) or trigger_eta_s <= 0:
            raise ValueError("terminal lock ETA must be positive and finite")
        self.enabled = bool(enabled)
        self.clock = clock
        self.trigger_eta_s = float(trigger_eta_s)
        self.requested = False
        self.requested_at_s: float | None = None
        self.requested_sim_stamp_ns: int | None = None
        self.dispatch_floor: int | None = None
        self.dispatch_sequence: int | None = None
        self.dispatch_vehicle_ms: int | None = None

    def observe_eta(self, eta_s: float | None, *, sim_stamp_ns: int | None,
                    dispatch_sequence: int | None) -> bool:
        if (not self.enabled or self.requested or eta_s is None or not math.isfinite(eta_s)
                or eta_s < 0 or eta_s > self.trigger_eta_s):
            return False
        self.requested = True
        self.requested_at_s = float(self.clock())
        self.requested_sim_stamp_ns = sim_stamp_ns
        self.dispatch_floor = dispatch_sequence
        return True

    def note_dispatch(self, sequence: int | None, vehicle_ms: int | None) -> None:
        if not self.requested or sequence is None:
            return
        if self.dispatch_floor is not None and sequence <= self.dispatch_floor:
            return
        self.dispatch_sequence = sequence
        self.dispatch_vehicle_ms = vehicle_ms

    def handoff_diagnostics(
        self, *, joint_fk_horizon_error_deg: float | None, hdeg: bool | None,
        meta_stamp_ns: int | None, current_sim_stamp_ns: int | None,
        max_error_deg: float = FW_HANDOFF_MAX_HORIZON_ERROR_DEG,
        max_meta_age_s: float = FW_TERMINAL_LOCK_MAX_STATE_AGE_S,
        meta_receive_age_s: float | None = None,
    ) -> dict[str, Any]:
        sequence = self.dispatch_sequence
        dispatched = (sequence is not None and self.dispatch_floor is not None
                      and sequence > self.dispatch_floor and self.dispatch_vehicle_ms is not None
                      and meta_stamp_ns is not None and meta_stamp_ns >= self.dispatch_vehicle_ms * 1_000_000)
        meta_age_s = (
            (current_sim_stamp_ns - meta_stamp_ns) / 1e9
            if current_sim_stamp_ns is not None and meta_stamp_ns is not None
            else None
        )
        meta_fresh = (meta_age_s is not None and 0 <= meta_age_s <= max_meta_age_s
                      and meta_receive_age_s is not None
                      and math.isfinite(meta_receive_age_s)
                      and 0 <= meta_receive_age_s <= max_meta_age_s
                      and (self.requested_sim_stamp_ns is None
                           or meta_stamp_ns >= self.requested_sim_stamp_ns))
        fk_valid = (joint_fk_horizon_error_deg is not None
                    and math.isfinite(joint_fk_horizon_error_deg))
        ready = (self.requested and dispatched and meta_fresh and fk_valid
                 and joint_fk_horizon_error_deg <= max_error_deg and hdeg is False)
        return {
            "ready": bool(ready),
            "lock_requested": self.requested,
            "lock_request_sim_stamp_ns": self.requested_sim_stamp_ns,
            "lock_request_wall_s": self.requested_at_s,
            "dispatch_sequence": sequence, "dispatch_before_readback": dispatched,
            "dispatch_floor_sequence": self.dispatch_floor,
            "dispatch_vehicle_ms": self.dispatch_vehicle_ms,
            "meta_stamp_ns": meta_stamp_ns,
            "meta_age_s": meta_age_s,
            "meta_receive_age_s": meta_receive_age_s,
            "meta_fresh": meta_fresh,
            "joint_fk_horizon_error_deg": joint_fk_horizon_error_deg,
            "hdeg": hdeg,
            "max_error_deg": max_error_deg,
        }


def terminal_lock_fk_horizon_error_deg(
    body_deg: Mapping[str, float],
    actual_joint_deg: Mapping[str, float],
    desired_earth_deg: Mapping[str, float],
) -> float | None:
    """Return the measured earth-frame horizon error for a lock command.

    This deliberately uses the measured joint angles, not the last host command.
    It is kept here so the bounded hand-off diagnostic can be unit-tested without
    a MAVLink or Gazebo dependency.
    """
    try:
        from .horizon_stabilization import (
            c11_rotation_deg,
            horizon_error_deg,
            rotation_deg,
        )

        values = tuple(float(mapping[axis]) for mapping in (body_deg, actual_joint_deg, desired_earth_deg)
                       for axis in ("roll", "pitch", "yaw"))
        if not all(math.isfinite(value) for value in values):
            return None
        from multidetect.camera_mount import _mat_mul
        measured = _mat_mul(rotation_deg(body_deg), c11_rotation_deg(actual_joint_deg))
        return float(horizon_error_deg(rotation_deg(desired_earth_deg), measured))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def spherical_bisector(
    old_direction: tuple[float, float, float],
    new_direction: tuple[float, float, float],
    *,
    max_old_deflection_deg: float = FW_PASS_MAX_OLD_LOS_DEFLECTION_DEG,
) -> tuple[tuple[float, float, float], float, float]:
    """Return the short-arc bisector, capped by its angular distance from old LOS."""
    if not math.isfinite(max_old_deflection_deg) or max_old_deflection_deg < 0:
        raise ValueError("bisector deflection bound must be finite and non-negative")
    old = unit(old_direction)
    new = unit(new_direction)
    dot = min(1.0, max(-1.0, sum(a * b for a, b in zip(old, new, strict=True))))
    angle = math.degrees(math.acos(dot))
    if angle <= 1e-10 or max_old_deflection_deg == 0:
        return old, angle, 0.0
    if angle < 180.0 - 1e-8:
        midpoint = unit(tuple(a + b for a, b in zip(old, new, strict=True)))
        half_angle = angle / 2.0
        if half_angle <= max_old_deflection_deg:
            return midpoint, angle, half_angle
        # The spherical midpoint is exactly half-way along the same great circle.
        theta = math.radians(max_old_deflection_deg)
        tangent = unit(tuple(b - a * dot for a, b in zip(old, new, strict=True)))
        direction = unit(tuple(math.cos(theta) * a + math.sin(theta) * b
                               for a, b in zip(old, tangent, strict=True)))
        return direction, angle, max_old_deflection_deg
    # Antipodal rays have no unique short-arc bisector. Choose a stable orthogonal arc.
    basis = (0.0, 0.0, 1.0) if abs(old[2]) < 0.9 else (0.0, 1.0, 0.0)
    tangent = unit((old[1] * basis[2] - old[2] * basis[1],
                    old[2] * basis[0] - old[0] * basis[2],
                    old[0] * basis[1] - old[1] * basis[0]))
    theta = math.radians(min(90.0, max_old_deflection_deg))
    direction = unit(tuple(math.cos(theta) * a + math.sin(theta) * b
                           for a, b in zip(old, tangent, strict=True)))
    return direction, angle, math.degrees(theta)


class X1FwPassMidpoint:
    """Per-leg control: select on planned geometry, activate on observed passage."""

    def __init__(self, *, enabled: bool, short_leg_m: float | None,
                 has_fly_through: bool, old_target: WorldTarget | None,
                 new_target: WorldTarget | None, planned_switch_angle_deg: float | None) -> None:
        self.enabled = bool(enabled and has_fly_through and short_leg_m is not None
                            and 0 < short_leg_m < FW_SHORT_LEG_MAX_M
                            and planned_switch_angle_deg is not None
                            and FW_PASS_SWITCH_MIN_DEG < planned_switch_angle_deg <= 180
                            and isinstance(old_target, WorldTarget)
                            and isinstance(new_target, WorldTarget))
        self.old_target = old_target
        self.new_target = new_target
        self.planned_switch_angle_deg = planned_switch_angle_deg
        self.observed_sim_stamp_ns: int | None = None

    def observe_crossing(self, *, sim_stamp_ns: int) -> dict[str, Any] | None:
        """Latch the first validated post-plane sample, not an interpolated ETA."""
        if (not self.enabled or self.observed_sim_stamp_ns is not None
                or type(sim_stamp_ns) is not int or sim_stamp_ns < 0):
            return None
        self.observed_sim_stamp_ns = sim_stamp_ns
        return {"sim_stamp_ns": sim_stamp_ns,
                "planned_switch_angle_deg": self.planned_switch_angle_deg,
                "max_old_los_deflection_deg": FW_PASS_MAX_OLD_LOS_DEFLECTION_DEG,
                "old_target_ref": self.old_target.reference(),
                "new_target_ref": self.new_target.reference()}

    def direction_at(self, position: WorldTarget | None) -> tuple[float, float, float] | None:
        """Return a live capped NED ray, or None to use normal old-target tracking."""
        if self.observed_sim_stamp_ns is None or position is None:
            return None
        try:
            old_los = world_target_direction(position, self.old_target)
            new_los = world_target_direction(position, self.new_target)
            return spherical_bisector(old_los, new_los)[0]
        except (TypeError, ValueError):
            return None
