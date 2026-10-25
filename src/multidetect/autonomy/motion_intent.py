"""Airframe aware motion intent boundary.

The planner and perception loop describe *what* the vehicle should do.  This
module is the only place that turns that description into a multirotor
primitive or a fixed-wing/VTOL guided command.  It deliberately has no MAVLink
dependency so both the SIM executor and the onboard runtime can consume the
same command objects.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from multidetect.sim_teacher.primitives import ActionPrimitive, body_to_ned

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


class MotionIntentError(ValueError):
    """Raised when an intent cannot be represented safely by an airframe."""


def _mat_vec(matrix: Sequence[float], vector: Sequence[float]) -> tuple[float, float, float]:
    return tuple(
        float(matrix[row * 3]) * float(vector[0])
        + float(matrix[row * 3 + 1]) * float(vector[1])
        + float(matrix[row * 3 + 2]) * float(vector[2])
        for row in range(3)
    )


def profile_mount_pitch_down_rad(
    profile: AirframeProfile, default: float, *, mode: str | None = None
) -> float:
    """Camera pitch below the body x-axis encoded in ``body_to_camera_rotation``.

    The row-major matrix stores a pitch-down mount as ``[c,0,-s, 0,1,0, s,0,c]``
    (the x500 profile's 0.18 rad matches its Gazebo camera pose).  A tailsitter
    with per-mode matrices is read for ``mode`` ("mc"/"fw"); without a mode it is
    not a single pitch angle and returns ``default``.  Mounts that also roll or
    yaw return ``default``.
    """
    rotations = profile.camera.attitude_rotations
    if rotations and (mode is None or mode.lower() not in rotations):
        return float(default)
    matrix = rotations[mode.lower()] if rotations else profile.camera.body_to_camera_rotation
    r = [float(value) for value in matrix]
    if abs(r[4] - 1.0) > 1e-6 or any(abs(r[i]) > 1e-6 for i in (1, 3, 5, 7)):
        return float(default)
    return math.atan2(r[6], r[0])


def camera_ray_from_profile(
    u: float,
    v: float,
    *,
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    attitude_deg: Sequence[float],
    profile: AirframeProfile,
    mode: str | None = None,
) -> tuple[float, float, float]:
    """Return a world-NED optical ray using the profile's full mount attitude.

    ``body_to_camera_rotation`` is stored row-major and its columns are the
    camera's optical axes (forward, image right, image down) in body FRD, the
    same convention :func:`profile_mount_pitch_down_rad` reads and the profile
    tests check against the Gazebo camera poses.  Tailsitters may provide
    separate MC/FW matrices; the selected matrix is applied before vehicle
    roll/pitch/yaw, so a 180-degree FW attitude cannot silently reuse MC rays.
    """
    selected = (mode or "mc").lower()
    mount = profile.camera.attitude_rotations.get(selected, profile.camera.body_to_camera_rotation)
    camera = (1.0, (float(u) - cx) / fx, (float(v) - cy) / fy)
    body = _mat_vec(mount, camera)
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
            if value is not None and (
                len(value) != 3 or not all(math.isfinite(float(v)) for v in value)
            ):
                raise MotionIntentError(f"{name} must be a finite NED triplet")
        for name in ("radius_m", "requested_radius_m", "duration_s", "speed_mps"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) <= 0):
                raise MotionIntentError(f"{name} must be positive and finite")

    @classmethod
    def goto(cls, target_ned: Sequence[float], *, mode: str | None = None) -> MotionIntent:
        return cls("goto", tuple(float(v) for v in target_ned), mode=mode)

    @classmethod
    def observe(
        cls, target_ned: Sequence[float], *, radius_m: float | None = None, mode: str | None = None
    ) -> MotionIntent:
        return cls("observe", tuple(float(v) for v in target_ned), radius_m=radius_m, mode=mode)

    @classmethod
    def wait(
        cls, duration_s: float = 1.0, *, target_ned: Sequence[float] | None = None
    ) -> MotionIntent:
        return cls(
            "wait",
            None if target_ned is None else tuple(float(v) for v in target_ned),
            duration_s=float(duration_s),
        )

    @classmethod
    def return_home(cls, target_ned: Sequence[float]) -> MotionIntent:
        return cls("return_home", tuple(float(v) for v in target_ned))

    @classmethod
    def land(cls, *, target_ned: Sequence[float] | None = None) -> MotionIntent:
        return cls("land", None if target_ned is None else tuple(float(v) for v in target_ned))

    @classmethod
    def deliver(cls, target_ned: Sequence[float], *, radius_m: float | None = None) -> MotionIntent:
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
        result.update(
            duration_s=self.duration_s, speed_mps=self.speed_mps, metadata=dict(self.metadata)
        )
        if self.action is not None:
            result["action"] = self.action.to_dict()
        return result


def resolve_airframe_profile(
    value: str | Path | AirframeProfile | None, *, base_dir: str | Path | None = None
) -> AirframeProfile:
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

    # Ground speed of hover-mode (MC) VTOL legs: the multirotor loop's 3-15 m steps
    # are sized for a multirotor, not for the profile's fixed-wing cruise airspeed.
    MC_SPEED_MPS = 5.0

    def __init__(
        self,
        profile: AirframeProfile | str | Path | None = None,
        *,
        base_dir: str | Path | None = None,
        mode: str | None = None,
        safety: SafetyEnvelope | None = None,
        preferred_mode: str | None = None,
    ) -> None:
        self.profile = resolve_airframe_profile(profile, base_dir=base_dir)
        self.mode = (mode or ("MC" if self.profile.category == "multirotor" else "FW")).upper()
        if self.mode not in {"MC", "FW"}:
            raise MotionIntentError("mode must be MC or FW")
        # A VTOL flown by the hover (multirotor) loop: every loop step happens in
        # MC, so telemetry reporting FW first asks for a back-transition.
        self.preferred_mode = None if preferred_mode is None else str(preferred_mode).upper()
        if self.preferred_mode is not None and (
            self.profile.category != "vtol" or self.preferred_mode not in {"MC", "FW"}
        ):
            raise MotionIntentError("preferred_mode needs a VTOL profile and MC or FW")
        self.energy = energy_params_from_profile(self.profile)
        # True while telemetry reports a VTOL transition in progress.
        self.transitioning = False
        # Fixed-wing altitude reference for primitive bridging (NED down, metres).
        self._held_down: float | None = None
        self.safety = safety or SafetyEnvelope(
            SafetyEnvelopeConfig(
                vehicle=dynamics_from_profile(self.profile),
                # Multirotors keep the 15 m default; other airframes step by their profile.
                **(
                    {}
                    if self.profile.category == "multirotor"
                    else {"max_translation_m": self.profile.limits.max_step_distance_m}
                ),
            )
        )

    def set_mode(self, mode: str | None) -> None:
        """Update VTOL mode from telemetry (MC/FW); ignore absent telemetry."""
        if self.profile.category != "vtol" or mode is None:
            return
        if isinstance(mode, (int, float)) and not isinstance(mode, bool):
            # MAV_VTOL_STATE: 3 MC, 4 FW, 5 transition to FW, 6 transition to MC.
            value = {3: "MC", 4: "FW", 5: "TRANSITION_TO_FW", 6: "TRANSITION_TO_MC"}.get(
                int(mode), str(mode).upper()
            )
        else:
            value = str(mode).upper()
        if value.startswith("TRANSITION"):
            # The vehicle is converting: the mode is neither yet, and a repeated
            # transition command could restart or abort the conversion.
            self.transitioning = True
            return
        if value in {"MC", "FW"}:
            self.transitioning = False
            if value != self.mode:
                self._held_down = None
            self.mode = value

    @classmethod
    def from_profile(
        cls, profile: AirframeProfile | str | Path, **kwargs: Any
    ) -> AirframeMotionAdapter:
        return cls(profile, **kwargs)

    @property
    def is_multirotor(self) -> bool:
        return self.profile.category == "multirotor"

    @property
    def hover_ops(self) -> bool:
        """A VTOL whose loop steps (search, approach, confirmation) are flown in MC."""
        return self.preferred_mode == "MC"

    def mode_change_command(self) -> MotionCommand | None:
        """A transition to ``preferred_mode`` while telemetry reports the other mode."""
        if self.preferred_mode is None or self.mode == self.preferred_mode or self.transitioning:
            return None
        return MotionCommand(
            "transition",
            mode=self.preferred_mode,
            reason=f"VTOL loop steps fly in {self.preferred_mode}",
        )

    def _recovery(self, reason: str, pose: Sequence[float]) -> MotionCommand:
        if self.profile.category == "multirotor":
            return MotionCommand(
                "hold", duration_s=1.0, reason=reason, action=ActionPrimitive.hold(1.0)
            )
        if self.profile.category == "vtol" and self.mode == "MC":
            return MotionCommand(
                "mc_hold",
                target_ned=tuple(float(v) for v in pose),
                duration_s=1.0,
                mode="MC",
                reason=reason,
            )
        return MotionCommand(
            "loiter",
            target_ned=tuple(float(v) for v in pose),
            radius_m=self.profile.orbit_radius(),
            speed_mps=self.profile.cruise_airspeed_mps,
            reason=reason,
        )

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
        if self.is_multirotor and not isinstance(command, MotionCommand):
            if not isinstance(command, ActionPrimitive):
                return command, {}
            pose = tuple(float(v) for v in pose_ned)
            decision = self.safety.apply(
                command,
                pose_ned=pose,
                yaw_deg=float(yaw_deg),
                current_agl_m=current_agl_m,
                phase=phase,
                skill=skill,
                mode="MC",
            )
            safety = decision.info().get("safety", {})
            if decision.rejected or decision.status == "clipped":
                # A primitive cannot carry a clipped target back to the caller.
                # Hold in place and expose the exact refusal reasons to the log.
                safety = {**safety, "status": "rejected", "rejected": True}
                recovery = ActionPrimitive.stop() if decision.return_home else ActionPrimitive.hold(
                    self.safety.config.hover_seconds
                )
                return recovery, safety
            return command, safety
        if not isinstance(command, MotionCommand):
            return command, {}
        if command.rejected:
            # A deliberate fail-closed command (currently fixed-wing delivery)
            # must reach the runtime as a rejection; converting it to a
            # benign-looking loiter would hide the mission contract failure.
            return command, {"rejected": True, "reasons": ["command_rejected"]}
        pose = tuple(float(v) for v in pose_ned)
        # A transition is checked under the envelope's transition branch
        # (altitude, airspeed), not the target mode it converts to.
        mode = "TRANSITION" if command.kind == "transition" else (command.mode or self.mode).upper()
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
        if kind == "orbit" and command.target_ned is not None and radius:
            # An orbit about a distant centre (a fly-by pass) is a long path, not a
            # hold: the whole circle and its entry leg must pass the envelope.
            decision = self.safety.apply_orbit(
                command.target_ned[:2],
                radius,
                pose_ned=pose,
                down_m=command.target_ned[2],
                yaw_deg=float(yaw_deg),
                current_agl_m=current_agl_m,
                phase=phase,
                skill=skill,
                airspeed_mps=speed,
                mode=mode,
                duration_s=duration,
            )
        else:
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
            if self._held_down is not None and recovery.target_ned is not None:
                # The refused command's altitude was never flown: the recovery's is
                # now the commanded one.  Keeping the refused altitude let repeated
                # refusals walk it below ground, and the next fly-by orbit used it.
                self._held_down = float(recovery.target_ned[2])
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

    def from_primitive(
        self,
        action: ActionPrimitive,
        *,
        pose_ned: Sequence[float],
        yaw_deg: float,
        metadata: Mapping[str, Any] | None = None,
    ) -> MotionCommand | ActionPrimitive:
        """Compatibility bridge for the existing loop/executor output."""
        if self.is_multirotor:
            return action
        command = self._from_primitive(
            action, pose_ned=pose_ned, yaw_deg=yaw_deg, metadata=metadata
        )
        if isinstance(command, MotionCommand) and command.action is None:
            # Keep the primitive the command came from: step records and stop
            # detection need it, and it explains what the loop asked for.
            command = replace(command, action=action)
        return command

    def _from_primitive(
        self,
        action: ActionPrimitive,
        *,
        pose_ned: Sequence[float],
        yaw_deg: float,
        metadata: Mapping[str, Any] | None = None,
    ) -> MotionCommand | ActionPrimitive:
        pose = tuple(float(v) for v in pose_ned)
        meta = metadata or {}
        onboard = meta.get("onboard", {}) if isinstance(meta, Mapping) else {}
        skill = onboard.get("state") if isinstance(onboard, Mapping) else None
        if skill == "deliver":
            return MotionCommand(
                "reject",
                target_ned=pose,
                mode=self.mode,
                reason="fixed-wing delivery requires an explicitly validated fly-by release window",
                metadata={"fail_closed": True},
            )
        if self.profile.category == "vtol" and self.mode == "MC":
            if action.kind == "hold":
                return MotionCommand(
                    "mc_hold",
                    target_ned=pose,
                    yaw_deg=float(yaw_deg),
                    duration_s=action.seconds,
                    mode="MC",
                    metadata=meta,
                )
            if action.kind == "stop":
                if isinstance(onboard, Mapping) and onboard.get("supervisor_action") == "land_now":
                    return MotionCommand("mc_land", target_ned=pose, mode="MC", metadata=meta)
                # The loop is done (as for a multirotor): hand the flight to the
                # autopilot's return/landing instead of landing at the target.
                return MotionCommand("stop", target_ned=pose, mode="MC", metadata=meta)
            if action.kind == "turn":
                return MotionCommand(
                    "mc_reposition",
                    target_ned=pose,
                    yaw_deg=float(yaw_deg) + action.yaw_delta_deg,
                    speed_mps=self.MC_SPEED_MPS,
                    mode="MC",
                    metadata=meta,
                )
            north, east, down = body_to_ned(action.forward_m, action.right_m, action.up_m, yaw_deg)
            return MotionCommand(
                "mc_reposition",
                target_ned=(pose[0] + north, pose[1] + east, pose[2] + down),
                yaw_deg=float(yaw_deg),
                speed_mps=self.MC_SPEED_MPS,
                mode="MC",
                metadata=meta,
            )
        # A plane loses a little height in every banked turn.  Commanding the
        # current altitude each step ratchets it down: in SITL a 30 m search
        # sank to 18 m over 25 steps, below a 23 m water tower.  Hold the last
        # commanded altitude and apply only the loop's requested climb/descent.
        if self._held_down is None:
            self._held_down = pose[2]
        # The loop measures a climb/descent from the altitude it sees (search
        # altitude, cruise, a revisit's return height), so it applies to the
        # actual altitude.  Adding it to the held altitude integrated every
        # correction: in SITL a 25 -> 30 m climb asked 8 times became a 54 m
        # command, and the descents back ran the held altitude below ground.
        if not (action.kind == "translate" and abs(action.up_m) > 1e-9):
            pose = (pose[0], pose[1], self._held_down)
        if (
            not self._safe_legacy(
                ActionPrimitive.translate(action.forward_m, action.right_m, action.up_m),
                pose,
                yaw_deg,
            )
            and action.kind == "translate"
        ):
            # The recovery loiter is the altitude now commanded.
            self._held_down = pose[2]
            return self._recovery("safety envelope rejected motion", pose)
        pass_orbit = onboard.get("fw_orbit") if isinstance(onboard, Mapping) else None
        if action.kind == "hold" and isinstance(pass_orbit, Mapping):
            # The loop's fly-by pass: a guided orbit through the target, held at
            # the commanded altitude.  The guard checks the whole circle.
            centre = pass_orbit.get("centre_ne") or ()
            radius = pass_orbit.get("radius_m")
            if len(centre) >= 2 and isinstance(radius, (int, float)) and radius > 0:
                return MotionCommand(
                    "orbit",
                    target_ned=(float(centre[0]), float(centre[1]), pose[2]),
                    radius_m=max(float(radius), self.profile.loiter_radius_m()),
                    duration_s=action.seconds,
                    speed_mps=self.profile.cruise_airspeed_mps,
                    mode=self.mode,
                    reason="fixed-wing fly-by pass",
                    metadata=meta,
                )
        if action.kind == "hold":
            return MotionCommand(
                "loiter",
                target_ned=pose,
                radius_m=self.profile.orbit_radius(),
                duration_s=action.seconds,
                speed_mps=self.profile.cruise_airspeed_mps,
                mode=self.mode,
                metadata=meta,
            )
        if action.kind == "stop":
            return MotionCommand("stop", target_ned=pose, mode=self.mode, metadata=metadata or {})
        if action.kind == "turn":
            # A plane cannot turn in place: head off on the new bearing for one leg.
            heading = math.radians(float(yaw_deg) + action.yaw_delta_deg)
            leg = self.profile.limits.max_step_distance_m * 0.9
            target = (pose[0] + leg * math.cos(heading), pose[1] + leg * math.sin(heading), pose[2])
            return MotionCommand(
                "reposition",
                target_ned=target,
                yaw_deg=float(yaw_deg) + action.yaw_delta_deg,
                radius_m=self.profile.min_turn_radius_m,
                speed_mps=self.profile.cruise_airspeed_mps,
                mode=self.mode,
                metadata=metadata or {},
            )
        north, east, down = body_to_ned(action.forward_m, action.right_m, action.up_m, yaw_deg)
        self._held_down = pose[2] + down
        return MotionCommand(
            "reposition",
            target_ned=(pose[0] + north, pose[1] + east, pose[2] + down),
            yaw_deg=float(yaw_deg),
            # PX4 fixed-wing reposition enters AUTO_LOITER around the target;
            # NAV_LOITER_RAD is 80 m in the deployed Cessna setup.  Carry that
            # capture radius so the executor can use loiter semantics instead
            # of requiring a hovering point arrival.
            radius_m=max(self.profile.orbit_radius(), 80.0),
            speed_mps=self.profile.cruise_airspeed_mps,
            mode=self.mode,
            metadata=metadata or {},
        )

    def intent_to_command(
        self,
        intent: MotionIntent,
        *,
        pose_ned: Sequence[float],
        yaw_deg: float = 0.0,
        mode: str | None = None,
    ) -> MotionCommand | ActionPrimitive:
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
        if (
            active == "MC"
            and self.profile.category == "vtol"
            and intent.kind in {"observe", "wait", "deliver", "land"}
        ):
            if intent.kind == "land":
                return MotionCommand("mc_land", target_ned=intent.target_ned or pose, mode="MC")
            target = intent.target_ned or pose
            if intent.kind == "wait" and target == pose:
                return MotionCommand(
                    "mc_hold", target_ned=pose, duration_s=intent.duration_s or 1.0, mode="MC"
                )
            return MotionCommand(
                "mc_reposition", target_ned=target, yaw_deg=intent.yaw_deg or yaw_deg, mode="MC"
            )
        if intent.kind == "land":
            if self.profile.category == "vtol" and active != "MC":
                return MotionCommand("transition", mode="MC", reason="landing requires MC")
            method = self.profile.landing.methods[0]
            return MotionCommand(
                "fw_land",
                target_ned=intent.target_ned or pose,
                mode="FW",
                metadata={"method": method},
            )
        if intent.kind == "deliver":
            return MotionCommand(
                "fly_by_deliver",
                target_ned=intent.target_ned,
                radius_m=self.profile.orbit_radius(intent.radius_m),
                speed_mps=self.profile.cruise_airspeed_mps,
                mode="FW",
                reason="fixed-wing release window",
            )
        if intent.kind in {"wait", "observe"}:
            return MotionCommand(
                "orbit",
                target_ned=intent.target_ned or pose,
                radius_m=self.profile.orbit_radius(intent.radius_m),
                duration_s=intent.duration_s or 0.0,
                speed_mps=intent.speed_mps or self.profile.cruise_airspeed_mps,
                mode=active,
            )
        if intent.kind == "transition":
            return MotionCommand("transition", mode=active)
        target = intent.target_ned or pose
        return MotionCommand(
            "reposition",
            target_ned=target,
            yaw_deg=intent.yaw_deg or yaw_deg,
            speed_mps=intent.speed_mps or self.profile.cruise_airspeed_mps,
            mode=active,
        )

    def estimated_duration_s(self, intent: MotionIntent, pose_ned: Sequence[float]) -> float:
        target = intent.target_ned
        if target is None:
            return float(intent.duration_s or 0.0)
        distance = math.dist(tuple(float(v) for v in pose_ned), target)
        speed = (
            self.energy.cruise_airspeed_mps
            if self.profile.category != "multirotor"
            else max(self.energy.cruise_airspeed_mps, 1e-6)
        )
        if intent.kind in {"wait", "observe"}:
            return float(intent.duration_s or 0.0)
        return distance / max(speed, 1e-6)

    def estimated_energy_wh(self, intent: MotionIntent, pose_ned: Sequence[float]) -> float:
        seconds = self.estimated_duration_s(intent, pose_ned)
        phase = "loiter" if intent.kind in {"wait", "observe"} else "cruise"
        power = getattr(self.energy, f"{phase}_power_w")
        return power * seconds / 3600.0 + (
            self.profile.endurance.transition_energy_wh if intent.kind == "transition" else 0.0
        )

    def search_path(
        self, bounds: Sequence[float], *, footprint_width_m: float, overlap: float
    ) -> SearchPath:
        if self.profile.category == "multirotor":
            raise MotionIntentError("search_path is only for fixed-wing or VTOL-FW mode")
        return lawnmower_search(
            bounds, footprint_width_m, overlap, max(self.profile.loiter_radius_m(), 1.0)
        )

    def observation_orbit(
        self,
        centre_ned: Sequence[float],
        requested_radius_m: float,
        *,
        altitude_m: float,
        views: int = 8,
    ) -> ObservationOrbit:
        # The geometry helper derives a safe altitude from the camera FOV.  The
        # requested altitude remains metadata for callers that need to clamp it.
        result = observation_orbit(
            tuple(centre_ned),
            self.profile.orbit_radius(requested_radius_m),
            max(self.profile.loiter_radius_m(), 1.0),
            math.radians(45.0),
            math.radians(35.0),
            points=max(8, views),
        )
        return result

    def fly_by_confirmation(
        self,
        centre_ned: Sequence[float],
        *,
        visible_half_length_m: float,
        altitude_m: float,
        passes: int = 3,
    ) -> FlyByPlan:
        return fly_by_passes(
            tuple(centre_ned),
            ((centre_ned[0], centre_ned[1]), 0.0),
            passes,
            max(self.profile.loiter_radius_m(), 1.0),
            altitude_m=altitude_m,
            airspeed_mps=self.profile.cruise_airspeed_mps,
        )

    @staticmethod
    def evaluate_confirmation(
        observations: Sequence[Any],
        *,
        min_passes: int = 2,
        sigma_gate_m: float = 8.0,
        spread_gate_m: float = 12.0,
    ) -> ConfirmationResult:
        return evaluate_fixed_wing_confirmation(
            observations,
            min_passes=min_passes,
            max_sigma_m=sigma_gate_m,
            position_tolerance_m=spread_gate_m,
        )


__all__ = [
    "MotionIntent",
    "MotionCommand",
    "MotionIntentError",
    "AirframeMotionAdapter",
    "resolve_airframe_profile",
    "energy_params_from_profile",
    "dynamics_from_profile",
    "camera_ray_from_profile",
    "profile_mount_pitch_down_rad",
]
