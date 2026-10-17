"""Airframe aware motion intent boundary.

The planner and perception loop describe *what* the vehicle should do.  This
module is the only place that turns that description into a multirotor
primitive or a fixed-wing/VTOL guided command.  It deliberately has no MAVLink
dependency so both the SIM executor and the onboard runtime can consume the
same command objects.
"""

from __future__ import annotations

import math
from dataclasses import replace, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .airframe_profile import AirframeProfile, load_airframe_profile
from .energy_estimator import EnergyParams
from .fw_geometry import (
    ConfirmationResult,
    FlyByPlan,
    ObservationOrbit,
    SearchPath,
    evaluate_fixed_wing_confirmation,
    fly_by_passes,
    lawnmower_search,
    observation_orbit,
)
from .safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig, VehicleDynamics
from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned


class MotionIntentError(ValueError):
    """Raised when an intent cannot be represented safely by an airframe."""


def _mat_vec(matrix: Sequence[float], vector: Sequence[float]) -> tuple[float, float, float]:
    return tuple(
        float(matrix[row * 3]) * float(vector[0])
        + float(matrix[row * 3 + 1]) * float(vector[1])
        + float(matrix[row * 3 + 2]) * float(vector[2])
        for row in range(3)
    )


def _mat_transpose(matrix: Sequence[float]) -> tuple[float, ...]:
    return tuple(float(matrix[col * 3 + row]) for row in range(3) for col in range(3))


def camera_ray_from_profile(
    u: float, v: float, *, fx: float, fy: float, cx: float, cy: float,
    attitude_deg: Sequence[float], profile: AirframeProfile, mode: str | None = None,
) -> tuple[float, float, float]:
    """Return a world-NED optical ray using the profile's full mount attitude.

    ``body_to_camera_rotation`` is stored row-major.  Tailsitters may provide
    separate MC/FW matrices; the selected matrix is applied before vehicle
    roll/pitch/yaw, so a 180-degree FW attitude cannot silently reuse MC rays.
    """
    selected = (mode or "mc").lower()
    mount = profile.camera.attitude_rotations.get(selected, profile.camera.body_to_camera_rotation)
    camera = (1.0, (float(u) - cx) / fx, (float(v) - cy) / fy)
    body = _mat_vec(_mat_transpose(mount), camera)
    roll, pitch, yaw = (math.radians(float(value)) for value in attitude_deg)
    cr, sr = math.cos(roll), math.sin(roll)
    y1, z1 = cr * body[1] - sr * body[2], sr * body[1] + cr * body[2]
    cp, sp = math.cos(pitch), math.sin(pitch)
    x2, z2 = cp * body[0] + sp * z1, -sp * body[0] + cp * z1
    cyaw, syaw = math.cos(yaw), math.sin(yaw)
    world = (cyaw * x2 - syaw * y1, syaw * x2 + cyaw * y1, z2)
    norm = math.sqrt(sum(value * value for value in world))
    if norm <= 1e-9:
        raise MotionIntentError("camera ray is degenerate")
    return tuple(value / norm for value in world)


@dataclass(frozen=True, slots=True)
class MotionIntent:
    """Airframe independent request emitted by a planner or executor."""

    kind: str
    target_ned: tuple[float, float, float] | None = None
    yaw_deg: float | None = None
    radius_m: float | None = None
    duration_s: float | None = None
    speed_mps: float | None = None
    requested_radius_m: float | None = None
    mode: str | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise MotionIntentError("intent kind must be non-empty")
        for name in ("target_ned",):
            value = getattr(self, name)
            if value is not None and (len(value) != 3 or not all(math.isfinite(float(v)) for v in value)):
                raise MotionIntentError(f"{name} must be a finite NED triplet")
        for name in ("radius_m", "requested_radius_m", "duration_s", "speed_mps"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise MotionIntentError(f"{name} must be positive and finite")

    @classmethod
    def goto(cls, target_ned: Sequence[float], *, mode: str | None = None) -> "MotionIntent":
        return cls("goto", tuple(float(v) for v in target_ned), mode=mode)

    @classmethod
    def observe(cls, target_ned: Sequence[float], *, radius_m: float | None = None, mode: str | None = None) -> "MotionIntent":
        return cls("observe", tuple(float(v) for v in target_ned), radius_m=radius_m, mode=mode)

    @classmethod
    def wait(cls, duration_s: float = 1.0, *, target_ned: Sequence[float] | None = None) -> "MotionIntent":
        return cls("wait", None if target_ned is None else tuple(float(v) for v in target_ned), duration_s=float(duration_s))

    @classmethod
    def return_home(cls, target_ned: Sequence[float]) -> "MotionIntent":
        return cls("return_home", tuple(float(v) for v in target_ned))

    @classmethod
    def land(cls, *, target_ned: Sequence[float] | None = None) -> "MotionIntent":
        return cls("land", None if target_ned is None else tuple(float(v) for v in target_ned))

    @classmethod
    def deliver(cls, target_ned: Sequence[float], *, radius_m: float | None = None) -> "MotionIntent":
        return cls("deliver", tuple(float(v) for v in target_ned), radius_m=radius_m)


@dataclass(frozen=True, slots=True)
class MotionCommand:
    """Serializable command understood by SIM and real vehicle adapters."""

    kind: str
    target_ned: tuple[float, float, float] | None = None
    yaw_deg: float | None = None
    radius_m: float | None = None
    duration_s: float = 0.0
    speed_mps: float | None = None
    mode: str | None = None
    reason: str | None = None
    action: ActionPrimitive | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return self.kind == "reject"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "mode": self.mode, "reason": self.reason}
        if self.target_ned is not None:
            result["target_ned"] = list(self.target_ned)
        if self.yaw_deg is not None:
            result["yaw_deg"] = self.yaw_deg
        if self.radius_m is not None:
            result["radius_m"] = self.radius_m
        result.update(duration_s=self.duration_s, speed_mps=self.speed_mps, metadata=dict(self.metadata))
        if self.action is not None:
            result["action"] = self.action.to_dict()
        return result


def resolve_airframe_profile(value: str | Path | AirframeProfile | None, *, base_dir: str | Path | None = None) -> AirframeProfile:
    """Resolve an id or path.  Unknown profiles fail closed."""
    if isinstance(value, AirframeProfile):
        return value
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[3]
    profiles = root / "configs" / "airframes"
    setting = "x500_mono_cam" if value is None or str(value).strip() == "" else str(value)
    candidate = Path(setting)
    if not candidate.is_absolute():
        candidate = profiles / (setting if setting.endswith(".json") else f"{setting}.json")
    if not candidate.is_file():
        raise MotionIntentError(f"unknown airframe profile: {setting}")
    try:
        return load_airframe_profile(candidate)
    except (OSError, ValueError, TypeError) as exc:
        raise MotionIntentError(f"invalid airframe profile {setting}: {exc}") from exc


def energy_params_from_profile(profile: AirframeProfile) -> EnergyParams:
    e = profile.endurance
    return EnergyParams(
        category=profile.category,
        can_hover=profile.can_hover,
        min_airspeed_mps=profile.min_airspeed_mps,
        cruise_airspeed_mps=profile.cruise_airspeed_mps,
        max_airspeed_mps=profile.max_airspeed_mps,
        min_turn_radius_m=profile.min_turn_radius_m or 0.0,
        max_bank_deg=profile.max_bank_deg,
        climb_rate_mps=profile.max_climb_rate_mps,
        descent_rate_mps=profile.max_descent_rate_mps,
        hover_power_w=e.hover_power_w,
        cruise_power_w=e.cruise_power_w,
        loiter_power_w=e.loiter_power_w,
        climb_power_w=e.climb_power_w,
        descent_power_w=e.descent_power_w,
        transition_power_w=max(e.hover_power_w, e.cruise_power_w),
        glide_sink_rate_mps=max(e.glide_sink_rate_mps, 0.01),
        loiter_radius_m=profile.loiter_radius_m(),
        wind_margin_mps=e.wind_margin_mps,
    )


def dynamics_from_profile(profile: AirframeProfile) -> VehicleDynamics:
    return VehicleDynamics(
        category=profile.category,
        can_hover=profile.can_hover,
        min_airspeed_mps=profile.min_airspeed_mps,
        cruise_airspeed_mps=profile.cruise_airspeed_mps,
        max_airspeed_mps=profile.max_airspeed_mps,
        min_turn_radius_m=profile.min_turn_radius_m or 0.0,
        max_bank_deg=profile.max_bank_deg,
        climb_rate_mps=profile.max_climb_rate_mps,
        descent_rate_mps=profile.max_descent_rate_mps,
    )


class AirframeMotionAdapter:
    """Map :class:`MotionIntent` (and legacy primitives) to vehicle commands."""

    def __init__(self, profile: AirframeProfile | str | Path | None = None, *, base_dir: str | Path | None = None, mode: str | None = None, safety: SafetyEnvelope | None = None) -> None:
        self.profile = resolve_airframe_profile(profile, base_dir=base_dir)
        self.mode = (mode or ("MC" if self.profile.category == "multirotor" else "FW")).upper()
        if self.mode not in {"MC", "FW"}:
            raise MotionIntentError("mode must be MC or FW")
        self.energy = energy_params_from_profile(self.profile)
        self.safety = safety or SafetyEnvelope(SafetyEnvelopeConfig(
            vehicle=dynamics_from_profile(self.profile),
            # Multirotors keep the 15 m default; other airframes step by their profile.
            **({} if self.profile.category == "multirotor"
               else {"max_translation_m": self.profile.limits.max_step_distance_m}),
        ))

    def set_mode(self, mode: str | None) -> None:
        """Update VTOL mode from telemetry (MC/FW); ignore absent telemetry."""
        if self.profile.category != "vtol" or mode is None:
            return
        if isinstance(mode, (int, float)) and not isinstance(mode, bool):
            value = {3: "MC", 4: "FW"}.get(int(mode), str(mode).upper())
        else:
            value = str(mode).upper()
        if value in {"MC", "FW"}:
            self.mode = value

    @classmethod
    def from_profile(cls, profile: AirframeProfile | str | Path, **kwargs: Any) -> "AirframeMotionAdapter":
        return cls(profile, **kwargs)

    @property
    def is_multirotor(self) -> bool:
        return self.profile.category == "multirotor"

    def _recovery(self, reason: str, pose: Sequence[float]) -> MotionCommand:
        if self.profile.category == "multirotor":
            return MotionCommand("hold", duration_s=1.0, reason=reason, action=ActionPrimitive.hold(1.0))
        if self.profile.category == "vtol" and self.mode == "MC":
            return MotionCommand(
                "mc_hold", target_ned=tuple(float(v) for v in pose),
                duration_s=1.0, mode="MC", reason=reason,
            )
        return MotionCommand("loiter", target_ned=tuple(float(v) for v in pose), radius_m=self.profile.orbit_radius(), speed_mps=self.profile.cruise_airspeed_mps, reason=reason)

    def _safe_legacy(self, action: ActionPrimitive, pose: Sequence[float], yaw_deg: float) -> bool:
        decision = self.safety.apply(
            action,
            pose_ned=tuple(float(v) for v in pose),
            yaw_deg=float(yaw_deg),
            # Fixed-wing envelope checks require an explicit airspeed.  The
            # profile cruise value is the command speed used below.
            airspeed_mps=(
                self.profile.cruise_airspeed_mps
                if self.profile.category in {"fixed_wing", "vtol"} and self.mode == "FW"
                else None
            ),
            mode=self.mode,
        )
        return not decision.rejected

    def guard_command(
        self,
        command: MotionCommand | ActionPrimitive,
        *,
        pose_ned: Sequence[float],
        yaw_deg: float,
        current_agl_m: float | None = None,
        phase: str | None = None,
        skill: str | None = None,
    ) -> tuple[MotionCommand | ActionPrimitive, dict[str, Any]]:
        """Run a high-level command through the primitive safety envelope.

        ``SafetyEnvelope`` intentionally accepts ``ActionPrimitive`` only.  A
        high-level fixed-wing command is therefore represented by a probe for
        the same path: a relative translation for reposition, a hold for
        loiter/transition, and a stop for terminal landing.  The probe is
        never sent to the vehicle.  A rejected probe cannot be represented by
        a partially clipped autopilot command, so the airframe recovery policy
        is used immediately (loiter for FW/VTOL).
        """
        if self.is_multirotor or not isinstance(command, MotionCommand):
            return command, {}
        if command.rejected:
            # A deliberate fail-closed command (currently fixed-wing delivery)
            # must reach the runtime as a rejection; converting it to a
            # benign-looking loiter would hide the mission contract failure.
            return command, {"rejected": True, "reasons": ["command_rejected"]}
        pose = tuple(float(v) for v in pose_ned)
        mode = (command.mode or self.mode).upper()
        probe = ActionPrimitive.hold(max(command.duration_s, 0.01))
        speed: float | None = None
        radius: float | None = None
        duration = command.duration_s or None
        kind = command.kind
        if kind in {"stop", "mc_land", "fw_land"}:
            probe = ActionPrimitive.stop()
        elif kind in {"reposition", "mc_reposition"}:
            target = command.target_ned or pose
            dn, de, dd = (target[index] - pose[index] for index in range(3))
            yaw = math.radians(float(yaw_deg))
            forward = dn * math.cos(yaw) + de * math.sin(yaw)
            right = -dn * math.sin(yaw) + de * math.cos(yaw)
            probe = ActionPrimitive.translate(forward, right, -dd)
            speed = command.speed_mps or self.profile.cruise_airspeed_mps
        elif kind in {"loiter", "orbit", "mc_hold"}:
            speed = command.speed_mps
            radius = command.radius_m
        elif kind == "transition":
            # A transition has no displacement, but still needs altitude and
            # airspeed validation under the transition branch of the envelope.
            speed = self.profile.cruise_airspeed_mps
        decision = self.safety.apply(
            probe,
            pose_ned=pose,
            yaw_deg=float(yaw_deg),
            current_agl_m=current_agl_m,
            phase=phase,
            skill=skill,
            airspeed_mps=speed,
            turn_radius_m=radius,
            mode=mode,
            duration_s=duration,
        )
        safety = decision.info().get("safety", {})
        # A high-level command cannot carry the envelope's clipped primitive
        # back to PX4.  Treat clipping as a refusal too, otherwise a long
        # reposition would silently bypass the configured per-step bound.
        if decision.rejected or decision.status == "clipped":
            reason = "safety envelope rejected motion"
            if decision.reasons:
                reason += ": " + ", ".join(decision.reasons)
            recovery = self._recovery(reason, pose)
            recovery = MotionCommand(
                recovery.kind,
                target_ned=recovery.target_ned,
                yaw_deg=recovery.yaw_deg,
                radius_m=recovery.radius_m,
                duration_s=recovery.duration_s,
                speed_mps=recovery.speed_mps,
                mode=recovery.mode,
                reason=recovery.reason,
                action=recovery.action,
                metadata={**dict(recovery.metadata), "safety": safety},
            )
            return recovery, safety
        return command, safety

    def from_primitive(self, action: ActionPrimitive, *, pose_ned: Sequence[float], yaw_deg: float, metadata: Mapping[str, Any] | None = None) -> MotionCommand | ActionPrimitive:
        """Compatibility bridge for the existing loop/executor output."""
        if self.is_multirotor:
            return action
        command = self._from_primitive(action, pose_ned=pose_ned, yaw_deg=yaw_deg, metadata=metadata)
        if isinstance(command, MotionCommand) and command.action is None:
            # Keep the primitive the command came from: step records and stop
            # detection need it, and it explains what the loop asked for.
            command = replace(command, action=action)
        return command

    def _from_primitive(self, action: ActionPrimitive, *, pose_ned: Sequence[float], yaw_deg: float, metadata: Mapping[str, Any] | None = None) -> MotionCommand | ActionPrimitive:
        pose = tuple(float(v) for v in pose_ned)
        meta = metadata or {}
        onboard = meta.get("onboard", {}) if isinstance(meta, Mapping) else {}
        skill = onboard.get("state") if isinstance(onboard, Mapping) else None
        if skill == "deliver":
            return MotionCommand(
                "reject", target_ned=pose, mode=self.mode,
                reason="fixed-wing delivery requires an explicitly validated fly-by release window",
                metadata={"fail_closed": True},
            )
        if self.profile.category == "vtol" and self.mode == "MC":
            if action.kind == "hold":
                return MotionCommand("mc_hold", target_ned=pose, duration_s=action.seconds, mode="MC", metadata=meta)
            if action.kind == "stop":
                return MotionCommand("mc_land", target_ned=pose, mode="MC", metadata=meta)
            if action.kind == "turn":
                return MotionCommand("mc_reposition", target_ned=pose, yaw_deg=float(yaw_deg) + action.yaw_delta_deg, mode="MC", metadata=meta)
            north, east, down = body_to_ned(action.forward_m, action.right_m, action.up_m, yaw_deg)
            return MotionCommand("mc_reposition", target_ned=(pose[0] + north, pose[1] + east, pose[2] + down), yaw_deg=float(yaw_deg), mode="MC", metadata=meta)
        if not self._safe_legacy(ActionPrimitive.translate(action.forward_m, action.right_m, action.up_m), pose, yaw_deg) and action.kind == "translate":
            return self._recovery("safety envelope rejected motion", pose)
        if action.kind == "hold":
            return MotionCommand("loiter", target_ned=pose, radius_m=self.profile.orbit_radius(), duration_s=action.seconds, speed_mps=self.profile.cruise_airspeed_mps, mode=self.mode, metadata=meta)
        if action.kind == "stop":
            return MotionCommand("stop", target_ned=pose, mode=self.mode, metadata=metadata or {})
        if action.kind == "turn":
            # A plane cannot turn in place: head off on the new bearing for one leg.
            heading = math.radians(float(yaw_deg) + action.yaw_delta_deg)
            leg = self.profile.limits.max_step_distance_m * 0.9
            target = (pose[0] + leg * math.cos(heading), pose[1] + leg * math.sin(heading), pose[2])
            return MotionCommand(
                "reposition", target_ned=target,
                yaw_deg=float(yaw_deg) + action.yaw_delta_deg,
                radius_m=self.profile.min_turn_radius_m,
                speed_mps=self.profile.cruise_airspeed_mps,
                mode=self.mode, metadata=metadata or {},
            )
        north, east, down = body_to_ned(action.forward_m, action.right_m, action.up_m, yaw_deg)
        return MotionCommand("reposition", target_ned=(pose[0] + north, pose[1] + east, pose[2] + down), yaw_deg=float(yaw_deg), speed_mps=self.profile.cruise_airspeed_mps, mode=self.mode, metadata=metadata or {})

    def intent_to_command(self, intent: MotionIntent, *, pose_ned: Sequence[float], yaw_deg: float = 0.0, mode: str | None = None) -> MotionCommand | ActionPrimitive:
        pose = tuple(float(v) for v in pose_ned)
        active = (mode or intent.mode or self.mode).upper()
        if self.is_multirotor:
            if intent.kind in {"wait", "observe"} and intent.target_ned is None:
                return ActionPrimitive.hold(intent.duration_s or 1.0)
            target = intent.target_ned or pose
            dn, de, dd = target[0] - pose[0], target[1] - pose[1], target[2] - pose[2]
            if intent.kind == "land":
                return ActionPrimitive.stop()
            distance = math.hypot(dn, de)
            if distance <= 1e-6 and abs(dd) <= 1e-6:
                return ActionPrimitive.hold(intent.duration_s or 1.0)
            bearing = math.degrees(math.atan2(de, dn))
            delta = (bearing - yaw_deg + 180.0) % 360.0 - 180.0
            if abs(delta) > 20.0:
                return ActionPrimitive.turn(delta)
            return ActionPrimitive.translate(distance, 0.0, -dd)
        if active == "MC" and self.profile.category == "vtol" and intent.kind in {"observe", "wait", "deliver", "land"}:
            if intent.kind == "land":
                return MotionCommand("mc_land", target_ned=intent.target_ned or pose, mode="MC")
            target = intent.target_ned or pose
            if intent.kind == "wait" and target == pose:
                return MotionCommand("mc_hold", target_ned=pose, duration_s=intent.duration_s or 1.0, mode="MC")
            return MotionCommand("mc_reposition", target_ned=target, yaw_deg=intent.yaw_deg or yaw_deg, mode="MC")
        if intent.kind == "land":
            if self.profile.category == "vtol" and active != "MC":
                return MotionCommand("transition", mode="MC", reason="landing requires MC")
            method = self.profile.landing.methods[0]
            return MotionCommand("fw_land", target_ned=intent.target_ned or pose, mode="FW", metadata={"method": method})
        if intent.kind == "deliver":
            return MotionCommand("fly_by_deliver", target_ned=intent.target_ned, radius_m=self.profile.orbit_radius(intent.radius_m), speed_mps=self.profile.cruise_airspeed_mps, mode="FW", reason="fixed-wing release window")
        if intent.kind in {"wait", "observe"}:
            return MotionCommand("orbit", target_ned=intent.target_ned or pose, radius_m=self.profile.orbit_radius(intent.radius_m), duration_s=intent.duration_s or 0.0, speed_mps=intent.speed_mps or self.profile.cruise_airspeed_mps, mode=active)
        if intent.kind == "transition":
            return MotionCommand("transition", mode=active)
        target = intent.target_ned or pose
        return MotionCommand("reposition", target_ned=target, yaw_deg=intent.yaw_deg or yaw_deg, speed_mps=intent.speed_mps or self.profile.cruise_airspeed_mps, mode=active)

    def estimated_duration_s(self, intent: MotionIntent, pose_ned: Sequence[float]) -> float:
        target = intent.target_ned
        if target is None:
            return float(intent.duration_s or 0.0)
        distance = math.dist(tuple(float(v) for v in pose_ned), target)
        speed = self.energy.cruise_airspeed_mps if self.profile.category != "multirotor" else max(self.energy.cruise_airspeed_mps, 1e-6)
        if intent.kind in {"wait", "observe"}:
            return float(intent.duration_s or 0.0)
        return distance / max(speed, 1e-6)

    def estimated_energy_wh(self, intent: MotionIntent, pose_ned: Sequence[float]) -> float:
        seconds = self.estimated_duration_s(intent, pose_ned)
        phase = "loiter" if intent.kind in {"wait", "observe"} else "cruise"
        power = getattr(self.energy, f"{phase}_power_w")
        return power * seconds / 3600.0 + (self.profile.endurance.transition_energy_wh if intent.kind == "transition" else 0.0)

    def search_path(self, bounds: Sequence[float], *, footprint_width_m: float, overlap: float) -> SearchPath:
        if self.profile.category == "multirotor":
            raise MotionIntentError("search_path is only for fixed-wing or VTOL-FW mode")
        return lawnmower_search(bounds, footprint_width_m, overlap, max(self.profile.loiter_radius_m(), 1.0))

    def observation_orbit(self, centre_ned: Sequence[float], requested_radius_m: float, *, altitude_m: float, views: int = 8) -> ObservationOrbit:
        # The geometry helper derives a safe altitude from the camera FOV.  The
        # requested altitude remains metadata for callers that need to clamp it.
        result = observation_orbit(
            tuple(centre_ned), self.profile.orbit_radius(requested_radius_m),
            max(self.profile.loiter_radius_m(), 1.0), math.radians(45.0),
            math.radians(35.0), points=max(8, views),
        )
        return result

    def fly_by_confirmation(self, centre_ned: Sequence[float], *, visible_half_length_m: float, altitude_m: float, passes: int = 3) -> FlyByPlan:
        return fly_by_passes(
            tuple(centre_ned), ((centre_ned[0], centre_ned[1]), 0.0), passes,
            max(self.profile.loiter_radius_m(), 1.0), altitude_m=altitude_m,
            airspeed_mps=self.profile.cruise_airspeed_mps,
        )

    @staticmethod
    def evaluate_confirmation(observations: Sequence[Any], *, min_passes: int = 2, sigma_gate_m: float = 8.0, spread_gate_m: float = 12.0) -> ConfirmationResult:
        return evaluate_fixed_wing_confirmation(
            observations, min_passes=min_passes, max_sigma_m=sigma_gate_m,
            position_tolerance_m=spread_gate_m,
        )


__all__ = [
    "MotionIntent", "MotionCommand", "MotionIntentError", "AirframeMotionAdapter",
    "resolve_airframe_profile", "energy_params_from_profile", "dynamics_from_profile",
    "camera_ray_from_profile",
]
