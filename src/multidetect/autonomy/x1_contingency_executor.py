"""Opt-in X1 onboard decision ingress. Only Altair selects the requested action."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .airframe_profile import AirframeProfile
from .mission_package import MissionPackage, check_plan_against_package
from .mission_plan import MissionPlan, validate_mission_plan
from .serialization import canonical_sha256, require_sha256, strict_json_loads
from .x1_contingency_state import STATE_SCHEMA, require_token

REQUEST_SCHEMA = "x1.contingency.request.v1"
DECISION_SCHEMA = "x1.contingency.decision.v1"
MAX_DECISION_BYTES = 6144
MAX_DECISION_AGE_MS = 3000
# Cadence replies keep the v1 positive event_id field without inventing a state event.
CADENCE_EVENT_ID_BASE = 2**52
MAX_FRAME_AGE_MS = 500
_FIELDS = {
    "continue": set(), "retask": {"plan"}, "loiter": {"hold_ref"},
    "return": set(), "land-now": set(), "abort-transition": set(),
    "gimbal-retarget": {"pitch_deg", "yaw_deg"},
}
_STEADY = {"mc", "fw"}
_TRANSITION = {"front_transition", "back_transition"}
_NATIVE = {"loiter", "return", "land-now", "abort-transition"}


def _number(value: object, lower: float, upper: float = math.inf) -> bool:
    return type(value) in (float, int) and lower <= value <= upper and abs(value) <= 1e12


@dataclass(frozen=True, slots=True)
class Decision:
    action: str
    request_id: str
    event_id: int
    mission_id: str
    revision: int
    digest: str
    params: Mapping
    plan: MissionPlan | None = None


def decode_decision(payload: str | bytes, *, capabilities: frozenset[str] = frozenset()) -> Decision:
    wire = payload.encode("utf-8") if isinstance(payload, str) else payload
    if not isinstance(wire, bytes) or len(wire) > MAX_DECISION_BYTES:
        raise ValueError("invalid decision size")
    obj = strict_json_loads(wire, name="Altair X1 decision")
    if not isinstance(obj, dict) or set(obj) != {"schema", "request_id", "event_id", "mission_id", "revision", "action", "params"}:
        raise ValueError("invalid decision keys")
    if obj["schema"] != DECISION_SCHEMA or type(obj["revision"]) is not int or not 1 <= obj["revision"] <= 2**31 - 1:
        raise ValueError("invalid decision schema or revision")
    if type(obj["event_id"]) is not int or not 1 <= obj["event_id"] <= 2**53 - 1:
        raise ValueError("invalid event id")
    action, params = obj["action"], obj["params"]
    if not isinstance(action, str) or action not in _FIELDS or not isinstance(params, dict) or set(params) != _FIELDS[action]:
        raise ValueError("unknown action or invalid params")
    request_id, mission_id = require_token(obj["request_id"]), require_token(obj["mission_id"])
    plan = None
    if action == "retask":
        if not isinstance(params["plan"], dict):
            raise ValueError("retask requires an embedded MissionPlan object")
        plan = validate_mission_plan(params["plan"], capabilities=capabilities)
    if action == "loiter":
        require_token(params["hold_ref"])
    if action == "gimbal-retarget" and not all(_number(angle, -360, 360) for angle in params.values()):
        raise ValueError("invalid gimbal angle")
    return Decision(action, request_id, obj["event_id"], mission_id, obj["revision"], canonical_sha256(obj), params, plan)


@dataclass(frozen=True, slots=True)
class HoldSite:
    # A host-defined hold option already authorized by the original mission area.
    # MC uses only a named current-position hold; FW uses a prebound WGS84 orbit.
    mode: str
    lat_deg: float | None = None
    lon_deg: float | None = None
    alt_amsl_m: float | None = None
    radius_m: float | None = None
    tangential_speed_mps: float | None = None


@dataclass(frozen=True, slots=True)
class RuntimeFrame:
    """Trusted vehicle/evidence tick, not the model's rounded telemetry JSON."""

    frame_id: int
    t_ms: int
    mission_id: str
    revision: int
    event_id: int
    px4_failsafe: bool | None
    armed_in_air: bool | None
    vtol_state: str | None
    agl_m: float | None
    airspeed_mps: float | None
    position_ok: bool | None
    hold_sites: Mapping[str, HoldSite]
    # Only true if the pinned transport/airframe has admitted front-abort to MC.
    front_abort_enabled: bool = False
    gimbal_available: bool = False
    position_ne_amsl: tuple[float, float, float] | None = None
    active_motion_index: int | None = None
    terrain_conflict: bool | None = None
    # Same-tick lower bounds, accounting for consumption and mission deadlines.
    remaining_energy_wh: float | None = None
    remaining_time_s: float | None = None


@dataclass(frozen=True, slots=True)
class MotionProof:
    """Estimates from trusted route/clearance analysis of the exact candidate.

    Extents cover the current connector, all admitted branches up to their next
    revalidation boundary, turns/swept volume, plus native-mode trajectory and
    terminal corridor where applicable. Do not use planner claims as evidence.
    """

    frame_id: int
    decision_digest: str
    geofence_clear: bool
    obstacle_margin_m: float | None
    terrain_margin_m: float | None
    required_energy_wh: float | None
    available_energy_wh: float | None
    min_airspeed_mps: float | None
    max_airspeed_mps: float | None
    max_bank_deg: float | None
    max_climb_mps: float | None
    max_descent_mps: float | None
    max_acceleration_mps2: float | None
    max_angular_rate_dps: float | None
    required_time_s: float | None
    available_time_s: float | None
    # Clearance margins above exclude authorized terminal terrain contact.
    landing_site_clear: bool = False
    # Minimum predicted AGL over any transition in the certified trajectory.
    min_transition_agl_m: float | None = None
    basis: str | None = None


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """A native port's COMMAND_ACK outcome for exactly the command it was asked to send."""

    ok: bool
    ack_result: int | None


@dataclass(frozen=True, slots=True)
class Admission:
    status: str
    reason: str
    action: str | None = None


class X1Envelope:
    """Reject-only admission. No clamping, substitutes, or contingency rules."""

    def __init__(self, profile: AirframeProfile):
        if (profile.id, profile.px4_autostart, profile.category, profile.subtype) != (
            "delta_tailsitter_gimbal", 4051, "vtol", "tailsitter"
        ) or profile.transition is None:
            raise ValueError("expected the X1 gimbal tailsitter profile")
        self.profile = profile

    def veto(self, d: Decision, frame: RuntimeFrame, proof: MotionProof | None, package: MissionPackage,
             *, executor_config=None,
             check_airborne_plan: Callable[[MissionPlan, RuntimeFrame], str | None] | None = None) -> str | None:
        action, p, mode = d.action, self.profile, frame.vtol_state
        if frame.px4_failsafe is not False or frame.armed_in_air is not True:
            return "px4_owns_or_flight_state_unknown"
        if action == "gimbal-retarget":
            gimbal = p.camera.gimbal
            limits = gimbal.get("angle_range_deg", {})
            if frame.gimbal_available is not True or gimbal.get("enabled") is not True:
                return "gimbal_unavailable"
            if not isinstance(mode, str) or mode not in _STEADY:
                return "gimbal_transition_interlock"
            if not all(axis in limits and len(limits[axis]) == 2 and _number(angle, *limits[axis])
                       for axis, angle in (("pitch", d.params["pitch_deg"]), ("yaw", d.params["yaw_deg"]), ("roll", 0.0))):
                return "gimbal_angle_limit"
            return None
        if not isinstance(mode, str) or mode not in _STEADY | _TRANSITION or frame.position_ok is not True:
            return "flight_mode_or_position_unverified"
        if action not in {"continue", "abort-transition"} and mode not in _STEADY:
            return "transition_interlock"
        if action == "abort-transition":
            if mode != "front_transition" or frame.front_abort_enabled is not True:
                return "transition_abort_not_admitted"
            if not _number(frame.agl_m, p.transition.minimum_height_m):
                return "transition_agl_limit"
        if action == "land-now" and (mode != "mc" or "vtol" not in p.landing.methods):
            return "land_now_requires_mc"
        if mode == "fw" and not _number(frame.airspeed_mps, p.min_airspeed_mps, p.max_airspeed_mps):
            return "current_fw_airspeed_limit"
        if action == "loiter":
            hold = frame.hold_sites.get(d.params["hold_ref"])
            if hold is None or hold.mode != mode:
                return "unknown_hold_site_for_mode"
            if mode == "fw":
                if not (_number(hold.lat_deg, -90, 90) and _number(hold.lon_deg, -180, 180)
                        and _number(hold.alt_amsl_m, -500, 10_000)
                        and _number(hold.tangential_speed_mps, 0.1, p.max_airspeed_mps)):
                    return "invalid_orbit_geometry"
                min_turn = hold.tangential_speed_mps**2 / (9.80665 * math.tan(math.radians(p.max_bank_deg)))
                if not _number(hold.radius_m, max(min_turn, p.loiter_radius_m())):
                    return "orbit_radius_limit"
            elif any(v is not None for v in (hold.lat_deg, hold.lon_deg, hold.alt_amsl_m, hold.radius_m, hold.tangential_speed_mps)):
                return "mc_hold_must_be_in_place"
        if action == "retask":
            if package.document["oversight_steps"]:
                return "retask_oversight_not_supported"
            try:
                package.validate_plan(d.plan)
                if check_airborne_plan is not None:
                    reason = check_airborne_plan(d.plan, frame)
                    if reason is not None:
                        return reason
                elif "x1_planning" in package.document or "airframe:x1" in package.document.get("capabilities", ()):
                    return "x1_airborne_plan_adapter_required"
                elif check_plan_against_package(d.plan, package, executor_config=executor_config):
                    return "mission_package_envelope"
            except ValueError:
                return "invalid_mission_plan_for_package"
        if proof is None or proof.frame_id != frame.frame_id or proof.decision_digest != d.digest:
            return "missing_or_unbound_motion_proof"
        evidence_basis = package.document.get("x1_live_evidence", {}).get("basis")
        if proof.basis != evidence_basis or (proof.basis is not None and (
            proof.basis != "ASSUMED_SIM" or package.mode != "sim"
        )):
            return "motion_proof_basis_mismatch"
        if (proof.geofence_clear is not True or not _number(proof.obstacle_margin_m, 0)
                or not _number(proof.terrain_margin_m, 0)):
            return "unverified_clearance"
        transition_required = mode in _TRANSITION or (mode == "fw" and action in {"retask", "return"})
        if (transition_required or proof.min_transition_agl_m is not None) and not _number(
            proof.min_transition_agl_m, p.transition.minimum_height_m
        ):
            return "predicted_transition_agl_limit"
        if action in {"return", "land-now"} and proof.landing_site_clear is not True:
            return "landing_site_unverified"
        # An immediate local landing may spend the energy reserve. Powering a
        # return trip must leave it intact, even if that trip ends in landing.
        budget = package.document["budget"]
        reserve = (0.0 if action == "land-now" else
                   max(budget["reserve_energy_wh"], p.endurance.reserve_fraction * p.endurance.battery_wh))
        time_reserve = 0.0 if action in {"return", "land-now"} else budget["reserve_time_s"]
        if not (_number(proof.required_energy_wh, 0) and _number(proof.available_energy_wh, 0)
                and min(proof.available_energy_wh, budget["energy_wh"], p.endurance.battery_wh)
                >= proof.required_energy_wh + reserve):
            return "energy_or_reserve_limit"
        if not (_number(proof.required_time_s, 0) and _number(proof.available_time_s, 0)
                and min(proof.available_time_s, budget["time_s"]) >= proof.required_time_s + time_reserve):
            return "time_or_reserve_limit"
        if mode == "fw" or proof.min_airspeed_mps is not None or proof.max_airspeed_mps is not None:
            if not (_number(proof.min_airspeed_mps, p.min_airspeed_mps, p.max_airspeed_mps)
                    and _number(proof.max_airspeed_mps, proof.min_airspeed_mps, p.max_airspeed_mps)):
                return "predicted_fw_airspeed_limit"
        for value, limit in (
            (proof.max_bank_deg, p.max_bank_deg), (proof.max_climb_mps, p.max_climb_rate_mps),
            (proof.max_descent_mps, p.max_descent_rate_mps), (proof.max_acceleration_mps2, p.limits.max_acceleration_mps2),
            (proof.max_angular_rate_dps, p.limits.max_angular_rate_dps),
        ):
            if not _number(value, 0, limit):
                return "predicted_dynamics_limit"
        return None


class X1AirbornePlanAdapter:
    """Bridge a validated airborne suffix to the X1 primitive runner's plan slot.

    The runner owns lowering and replacing the remaining primitives. A false
    return must leave its active plan untouched. Both calls run on its serial
    control tick. The motion proof must cover the runner's exact lowering.
    """

    def __init__(self, plan_version: int, *, replace_remaining: Callable[[MissionPlan, int], bool]):
        self.plan_version = plan_version
        self.replace_remaining = replace_remaining

    @staticmethod
    def check_airborne_plan(plan: MissionPlan, frame: RuntimeFrame) -> str | None:
        steps = plan.steps
        skills = [step.skill for step in steps]
        # No ground lifecycle primitive may re-arm or take off in flight.
        if frame.vtol_state == "fw":
            work = {"fw_goto_waypoint", "loiter_observe", "gimbal_stare_at", "find_object_then_return"}
            if not (2 <= len(steps) <= 6 and skills[-2:] == ["back_transition", "return_home"]
                    and set(skills[:-2]) <= work):
                return "invalid_fw_retask_suffix"
        elif frame.vtol_state == "mc":
            if skills != ["return_home"]:
                return "invalid_mc_retask_suffix"
        else:
            return "transition_interlock"
        terminal = steps[-1]
        if terminal.params or terminal.on_success is not None or terminal.on_fail is not None:
            return "invalid_retask_terminal"
        for step, successor in zip(steps, steps[1:]):
            # Keep the graph schema valid. The runtime reports off-nominal
            # failures to Altair before traversing a declared failure edge.
            if step.on_success != successor.id or step.on_fail != terminal.id:
                return "invalid_retask_edges"
        return None

    def apply_onboard_replan(self, plan: MissionPlan, *, expected_version: int) -> None:
        if self.plan_version != expected_version or self.replace_remaining(plan, expected_version) is not True:
            raise ValueError("X1 runner declined airborne replacement")
        self.plan_version += 1


@dataclass(frozen=True, slots=True)
class _Ticket:
    request_id: str
    revision: int
    t_ms: int
    event_id: int
    deadline_ms: int
    hold_refs: tuple[str, ...]


class X1ContingencyExecutor:
    """Use on the serialized control tick, never directly on the model worker.

    The caller must not send mission motion while ``native_owner`` is set.
    Native ownership is not released implicitly, even on a timeout or NACK.
    Re-entry requires a separate verified handoff/re-admission by the runtime.
    """

    def __init__(self, mission, vehicle, package: MissionPackage, envelope: X1Envelope, *,
                 claim_native: Callable[[], bool],
                 capabilities: frozenset[str] = frozenset()):
        self.mission = mission
        self.vehicle = vehicle
        self.package = package  # Original operator-admitted package (never from the model).
        self.envelope = envelope
        self.claim_native = claim_native
        self.capabilities = capabilities
        self._seq = 0
        self._last_event_id = 0
        self._ticket: _Ticket | None = None
        self._native_owner: str | None = None

    @property
    def native_owner(self) -> str | None:
        return self._native_owner

    def open_request(self, snapshot: Mapping, *, run_id: str, hold_refs: Sequence[str] = (),
                     remaining_plan_sha256: str | None = None, mission_progress: Mapping | None = None) -> dict:
        """Issue a single-use ticket for a current event or a periodic observation."""
        require_token(run_id)
        if len(run_id) > 24:
            raise ValueError("run_id exceeds 24 characters")
        if (snapshot["schema"] != STATE_SCHEMA or snapshot["mission_id"] != self.package.mission_id
                or snapshot["revision"] != self.mission.plan_version):
            raise ValueError("snapshot does not identify active mission")
        if isinstance(hold_refs, (str, bytes)) or len(hold_refs) > 8:
            raise ValueError("expected at most 8 hold references")
        holds = [require_token(ref) for ref in hold_refs]
        t_ms = snapshot["t_ms"]
        if mission_progress is None:
            event_id = snapshot["latest_event_id"]
            event = next((item for item in snapshot["events"] if item["id"] == event_id), None)
            if (type(t_ms) is not int or type(event_id) is not int
                    or not self._last_event_id < event_id < CADENCE_EVENT_ID_BASE
                    or event is None or type(event["t_ms"]) is not int
                    or not 0 <= event["t_ms"] <= t_ms <= event["t_ms"] + MAX_DECISION_AGE_MS):
                raise ValueError("request requires a current event on the monotonic clock")
            deadline = event["t_ms"] + MAX_DECISION_AGE_MS
        else:
            event_id = CADENCE_EVENT_ID_BASE + self._seq + 1
            if type(t_ms) is not int or t_ms < 0 or event_id >= 2**53:
                raise ValueError("invalid cadence clock or sequence")
            deadline = t_ms + MAX_DECISION_AGE_MS
        if remaining_plan_sha256 is not None:
            remaining_plan_sha256 = require_sha256(remaining_plan_sha256, "remaining_plan_sha256")
        self._seq += 1
        ticket = _Ticket(require_token(f"{run_id}:{self._seq}"), snapshot["revision"], t_ms, event_id,
                         deadline, tuple(holds))
        self._ticket = ticket
        request = {"schema": REQUEST_SCHEMA, "request_id": ticket.request_id, "event_id": ticket.event_id,
                   "deadline_ms": ticket.deadline_ms,
                   "native_owner": self.native_owner, "hold_refs": holds, "state": snapshot,
                   "remaining_plan_sha256": remaining_plan_sha256, "actions": sorted(_FIELDS),
                   "capabilities": sorted(self.capabilities)}
        if mission_progress is None:
            self._last_event_id = ticket.event_id
        else:
            request.update(trigger="cadence", mission_progress=dict(mission_progress))
        return request

    def discard_request(self, request_id: str) -> None:
        if self._ticket is not None and self._ticket.request_id == request_id:
            self._ticket = None

    def apply(self, wire: str | bytes, frame: RuntimeFrame, *, now_ms: int,
              proof: MotionProof | None = None,
              build_proof: Callable[[Decision, RuntimeFrame], MotionProof] | None = None) -> Admission:
        """Authorize and dispatch one exact action. Rejections send no command."""
        try:
            d = decode_decision(wire, capabilities=self.capabilities)
        except (ValueError, RecursionError):
            return Admission("rejected", "invalid_decision")
        ticket = self._ticket
        if ticket is None or ticket.request_id != d.request_id:
            return Admission("rejected", "unmatched_or_consumed_request", d.action)
        self._ticket = None  # Matching response is single-use, including a veto.
        if (d.mission_id != self.package.mission_id or frame.mission_id != d.mission_id
                or d.revision != ticket.revision or frame.revision != d.revision
                or self.mission.plan_version != d.revision or d.event_id != ticket.event_id
                or frame.event_id != ticket.event_id):
            return Admission("rejected", "superseded_mission_or_event", d.action)
        if (type(now_ms) is not int or type(frame.t_ms) is not int
                or not ticket.t_ms <= now_ms <= ticket.deadline_ms
                or not ticket.t_ms <= frame.t_ms <= now_ms or now_ms - frame.t_ms > MAX_FRAME_AGE_MS):
            return Admission("rejected", "decision_or_frame_stale", d.action)
        if d.action == "loiter" and d.params["hold_ref"] not in ticket.hold_refs:
            return Admission("rejected", "hold_ref_not_offered", d.action)
        if self.native_owner is not None and d.action in {"continue", "retask"}:
            return Admission("rejected", "native_handoff_required", d.action)
        try:
            if proof is None and build_proof is not None and d.action != "gimbal-retarget":
                proof = build_proof(d, frame)
            reason = self.envelope.veto(
                d, frame, proof, self.package, executor_config=getattr(self.mission, "config", None),
                check_airborne_plan=getattr(self.mission, "check_airborne_plan", None),
            )
        except (ValueError, RuntimeError, OSError):
            return Admission("rejected", "proof_or_envelope_error", d.action)
        if reason is not None:
            return Admission("rejected", reason, d.action)
        try:
            if d.action in _NATIVE:
                # Take away the ordinary writer before racing an active PX4 mode.
                # This is an interlock, not permission to send another action.
                if self.native_owner is None and self.claim_native() is not True:
                    return Admission("rejected", "native_handoff_not_available", d.action)
                self._native_owner = d.action
            return self._dispatch(d, frame)
        except (ValueError, OSError, RuntimeError):
            return Admission("transport_uncertain" if d.action in _NATIVE | {"gimbal-retarget"} else "executor_failed",
                             "dispatch_exception", d.action)

    def _dispatch(self, d: Decision, frame: RuntimeFrame) -> Admission:
        action = d.action
        if action == "continue":
            return Admission("continued", "no_new_command", action)
        if action == "retask":
            self.mission.apply_onboard_replan(d.plan, expected_version=d.revision)
            return Admission("installed", "mission_plan_replaced", action)
        result = None
        if action == "return":
            result = self.vehicle.rtl()
        elif action == "land-now":
            result = self.vehicle.land()
        elif action == "loiter":
            hold = frame.hold_sites[d.params["hold_ref"]]
            if hold.mode == "mc":
                result = self.vehicle.set_mode("HOLD")
            else:
                return self._receipt(action, self.vehicle.loiter_at((hold.lat_deg, hold.lon_deg), hold.radius_m,
                    hold.alt_amsl_m, speed=hold.tangential_speed_mps))
        elif action == "abort-transition":
            return self._receipt(action, self.vehicle.vtol_transition("mc"))
        elif action == "gimbal-retarget":
            return self._receipt(action, self.vehicle.gimbal_control(**d.params))
        # A port that reports its COMMAND_ACK gets the same receipt mapping as
        # loiter_at(). A send-only port can only claim that the command left.
        if isinstance(result, CommandReceipt):
            return self._receipt(action, result)
        return Admission("sent_unconfirmed", "readback_required", action)

    @staticmethod
    def _receipt(action, result) -> Admission:
        if result.ok:
            return Admission("accepted_unconfirmed", "readback_required", action)
        # ACK/result code may report a rejection, but no command is retried or substituted.
        status = "transport_denied" if result.ack_result in {1, 2, 3, 4, 6} else "transport_uncertain"
        return Admission(status, "no_positive_acceptance", action)
