"""Deterministic, platform-neutral task planning.

The planner is the boundary between a resolved operator task and the
platform-neutral plan consumed by the PX4 mission compilers.  It deliberately
does not know about MAVLink, Gazebo, QGC or evaluator truth.  REPLAY, SIM, HIL
and REAL therefore execute the same validation and step construction; only the
runtime context and the downstream airframe adapter vary.

The implementation is intentionally conservative.  A plan is rejected when
the context cannot prove a usable GNSS fix, map/terrain/perception resources,
geofence state or enough energy.  Route distance is computed on WGS-84 and the
declared geometry length is never allowed to understate the sampled centerline.
No estimate or plan produced here can emit a flight-control command by itself.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..geolocation_accuracy import wgs84_distance_m
from .contracts import (
    AutonomyContractError,
    ContextSnapshot,
    PlanAction,
    PlanStep,
    PlatformNeutralPlan,
    ResolvedTaskSpec,
    TaskType,
)
from .serialization import require_int, require_real, require_sha256, require_text, strict_object


class PlannerError(AutonomyContractError):
    """Raised when a task cannot be planned from the supplied context."""


_VEHICLE_ALIASES: Mapping[str, str] = {
    "vtol": "vtol",
    "v-tol": "vtol",
    "vertical-takeoff-and-landing": "vtol",
    "vertical-takeoff-landing": "vtol",
    "fixed-wing": "fixed-wing",
    "fixedwing": "fixed-wing",
    "fixed_wing": "fixed-wing",
    "plane": "fixed-wing",
    "airplane": "fixed-wing",
    "multirotor": "multirotor",
    "multi-rotor": "multirotor",
    "multicopter": "multirotor",
    "quadcopter": "multirotor",
    "quad-copter": "multirotor",
    "quad": "multirotor",
    "copter": "multirotor",
    "x500": "multirotor",
}

_TASK_ACTIONS: Mapping[TaskType, PlanAction] = {
    TaskType.AREA_SEARCH: PlanAction.SEARCH_AREA,
    TaskType.CORRIDOR_VISUAL_SEARCH: PlanAction.SEARCH_CORRIDOR,
    TaskType.TARGET_OBSERVATION: PlanAction.OBSERVE_TARGET,
    TaskType.SURVEY: PlanAction.SURVEY,
}

# Lifecycle/navigation actions are never valid as a reaction to a visual
# detection.  Keeping this allow-list here prevents a malformed task from
# smuggling a return, land or divert into the observation callback path.
_DETECTION_ACTIONS = frozenset(
    {
        PlanAction.OBSERVE_TARGET,
        PlanAction.CAPTURE_EVIDENCE,
        PlanAction.REPORT_GEOLOCATION,
        PlanAction.REVISIT,
    }
)

_DETECTION_OVERHEAD_S: Mapping[PlanAction, float] = {
    PlanAction.OBSERVE_TARGET: 5.0,
    PlanAction.CAPTURE_EVIDENCE: 10.0,
    PlanAction.REPORT_GEOLOCATION: 2.0,
    PlanAction.REVISIT: 15.0,
}


def normalize_vehicle_class(value: object) -> str:
    """Normalize the airframe token used by all three PX4 mission paths."""

    if not isinstance(value, str) or not value.strip():
        raise PlannerError("vehicle_class must be one of 'vtol', 'fixed-wing' or 'multirotor'")
    normalized = "-".join(
        part
        for part in value.strip().lower().replace("_", "-").replace(" ", "-").split("-")
        if part
    )
    try:
        return _VEHICLE_ALIASES[normalized]
    except KeyError as exc:
        raise PlannerError(
            "vehicle_class must be one of 'vtol', 'fixed-wing' or 'multirotor'"
        ) from exc


@dataclass(frozen=True, slots=True)
class PlannerPolicy:
    """Versioned planning constants and fail-closed resource thresholds.

    These values are policy, not sensor truth.  A deployment can construct a
    different policy after its airframe and operating envelope have been
    reviewed, while the planner algorithm and plan contract remain identical.
    """

    minimum_gps_fix_type: int = 3
    maximum_gps_horizontal_accuracy_m: float = 6.0
    minimum_geometry_confidence: float = 0.5
    minimum_battery_margin_pct: float = 5.0
    minimum_speed_mps: float = 1.0
    maximum_planning_speed_mps: float = 35.0
    fixed_wing_cruise_speed_mps: float = 18.0
    vtol_cruise_speed_mps: float = 15.0
    multirotor_cruise_speed_mps: float = 8.0
    takeoff_duration_s: float = 30.0
    landing_duration_s: float = 30.0
    vtol_transition_duration_s: float = 45.0
    duration_safety_factor: float = 1.25

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "minimum_gps_fix_type",
            require_int(self.minimum_gps_fix_type, "minimum_gps_fix_type", minimum=0, maximum=6),
        )
        for name in (
            "maximum_gps_horizontal_accuracy_m",
            "minimum_geometry_confidence",
            "minimum_battery_margin_pct",
            "minimum_speed_mps",
            "maximum_planning_speed_mps",
            "fixed_wing_cruise_speed_mps",
            "vtol_cruise_speed_mps",
            "multirotor_cruise_speed_mps",
            "takeoff_duration_s",
            "landing_duration_s",
            "vtol_transition_duration_s",
            "duration_safety_factor",
        ):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.0))
        if self.maximum_gps_horizontal_accuracy_m <= 0.0:
            raise PlannerError("maximum_gps_horizontal_accuracy_m must be positive")
        if not 0.0 <= self.minimum_geometry_confidence <= 1.0:
            raise PlannerError("minimum_geometry_confidence must be within [0, 1]")
        if self.minimum_battery_margin_pct > 100.0:
            raise PlannerError("minimum_battery_margin_pct must be at most 100")
        if self.minimum_speed_mps <= 0.0:
            raise PlannerError("minimum_speed_mps must be positive")
        if self.maximum_planning_speed_mps < self.minimum_speed_mps:
            raise PlannerError("maximum_planning_speed_mps must cover minimum_speed_mps")
        for name in (
            "fixed_wing_cruise_speed_mps",
            "vtol_cruise_speed_mps",
            "multirotor_cruise_speed_mps",
        ):
            if getattr(self, name) <= 0.0:
                raise PlannerError(f"{name} must be positive")
        if self.duration_safety_factor < 1.0:
            raise PlannerError("duration_safety_factor must be at least 1")


@dataclass(frozen=True, slots=True)
class PlanningEstimate:
    """Auditable distance/time result used by the planner's safety gates."""

    task_id: str
    vehicle_class: str
    outbound_distance_m: float
    task_distance_m: float
    return_distance_m: float
    total_distance_m: float
    cruise_speed_mps: float
    estimated_duration_s: float
    available_duration_s: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", require_text(self.task_id, "task_id"))
        object.__setattr__(self, "vehicle_class", normalize_vehicle_class(self.vehicle_class))
        for name in (
            "outbound_distance_m",
            "task_distance_m",
            "return_distance_m",
            "total_distance_m",
            "cruise_speed_mps",
            "estimated_duration_s",
            "available_duration_s",
        ):
            object.__setattr__(self, name, require_real(getattr(self, name), name, minimum=0.0))
        if self.cruise_speed_mps <= 0.0:
            raise PlannerError("cruise_speed_mps must be positive")
        if self.total_distance_m + 1.0e-9 < (
            self.outbound_distance_m + self.task_distance_m + self.return_distance_m
        ):
            raise PlannerError("total_distance_m is smaller than its route segments")

    @classmethod
    def from_mapping(cls, raw: object) -> PlanningEstimate:
        """Re-open a CLI estimate with the same strict field contract."""

        value = strict_object(
            raw,
            name="PlanningEstimate",
            required=frozenset(
                {
                    "task_id",
                    "vehicle_class",
                    "outbound_distance_m",
                    "task_distance_m",
                    "return_distance_m",
                    "total_distance_m",
                    "cruise_speed_mps",
                    "estimated_duration_s",
                    "available_duration_s",
                }
            ),
        )
        return cls(
            task_id=require_text(value["task_id"], "task_id"),
            vehicle_class=require_text(value["vehicle_class"], "vehicle_class"),
            outbound_distance_m=require_real(
                value["outbound_distance_m"], "outbound_distance_m", minimum=0.0
            ),
            task_distance_m=require_real(value["task_distance_m"], "task_distance_m", minimum=0.0),
            return_distance_m=require_real(
                value["return_distance_m"], "return_distance_m", minimum=0.0
            ),
            total_distance_m=require_real(
                value["total_distance_m"], "total_distance_m", minimum=0.0
            ),
            cruise_speed_mps=require_real(
                value["cruise_speed_mps"], "cruise_speed_mps", minimum=0.0
            ),
            estimated_duration_s=require_real(
                value["estimated_duration_s"], "estimated_duration_s", minimum=0.0
            ),
            available_duration_s=require_real(
                value["available_duration_s"], "available_duration_s", minimum=0.0
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        """Return stable JSON-compatible diagnostics without a new wire contract."""

        return {
            "task_id": self.task_id,
            "vehicle_class": self.vehicle_class,
            "outbound_distance_m": self.outbound_distance_m,
            "task_distance_m": self.task_distance_m,
            "return_distance_m": self.return_distance_m,
            "total_distance_m": self.total_distance_m,
            "cruise_speed_mps": self.cruise_speed_mps,
            "estimated_duration_s": self.estimated_duration_s,
            "available_duration_s": self.available_duration_s,
        }


@dataclass(frozen=True, slots=True)
class RouteAcquisitionRequirements:
    """A bound v2 MR survey needs camera capture, not an active detector.

    This is a task capability declaration, not a skip-all safety option. GPS,
    geofence, map/terrain, camera, wind and conservative energy/speed gates stay
    enabled. A future static-scene clearance substitute needs a separate verified
    contract; file presence alone is not obstacle-clearance evidence.
    """

    task_spec_sha256: str
    context_snapshot_sha256: str
    modeled_endurance_receive_age_s: float = 0.0

    def __post_init__(self) -> None:
        for name in ("task_spec_sha256", "context_snapshot_sha256"):
            object.__setattr__(self, name, require_sha256(getattr(self, name), name))
        object.__setattr__(
            self,
            "modeled_endurance_receive_age_s",
            require_real(
                self.modeled_endurance_receive_age_s,
                "modeled_endurance_receive_age_s",
                minimum=0.0,
                maximum=2.0,
            ),
        )

    def validate_for(
        self, task: ResolvedTaskSpec, context: ContextSnapshot, vehicle_class: str
    ) -> None:
        if (
            context.schema_version != 2
            or vehicle_class != "multirotor"
            or task.task_type is not TaskType.SURVEY
            or task.on_detection
            or task.completion_action is not PlanAction.RETURN
            or not task.constraints.remain_inside_geofence
            or task.canonical_hash != self.task_spec_sha256
            or context.canonical_hash != self.context_snapshot_sha256
        ):
            raise PlannerError("route acquisition requirements do not match a bounded v2 MR survey")
        if self.modeled_endurance_receive_age_s and (
            context.provenance is None
            or context.provenance.runtime_kind != "SIM"
            or context.provenance.endurance.kind != "explicit_SIM_energy_model"
        ):
            raise PlannerError("receive-age correction requires the explicit SIM energy model")


def estimate_task(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    policy: PlannerPolicy | None = None,
    resource_requirements: RouteAcquisitionRequirements | None = None,
) -> PlanningEstimate:
    """Validate planning prerequisites and return a conservative WGS-84 estimate."""

    selected_policy = PlannerPolicy() if policy is None else policy
    if not isinstance(selected_policy, PlannerPolicy):
        raise PlannerError("policy must be a PlannerPolicy")
    normalized_vehicle = _validate_inputs(
        task, context, vehicle_class, selected_policy, resource_requirements=resource_requirements
    )
    outbound_distance_m, task_distance_m, return_distance_m = _route_distances(task, context)
    if task.completion_action is not PlanAction.RETURN:
        # A LAND completion ends at the task geometry.  Keep the home distance
        # out of the required budget instead of silently charging a return leg
        # that the generated plan does not contain.
        return_distance_m = 0.0
    total_distance_m = outbound_distance_m + task_distance_m + return_distance_m

    nominal_speed = {
        "fixed-wing": selected_policy.fixed_wing_cruise_speed_mps,
        "vtol": selected_policy.vtol_cruise_speed_mps,
        "multirotor": selected_policy.multirotor_cruise_speed_mps,
    }[normalized_vehicle]
    if context.schema_version == 1:
        # Preserve legacy measured-speed semantics for the unchanged v1 contract.
        assert context.airspeed_mps is not None
        measured_speed = max(context.ground_speed_mps, context.airspeed_mps)
        planning_speed = min(
            selected_policy.maximum_planning_speed_mps,
            max(nominal_speed, measured_speed),
        )
    else:
        # Instantaneous ground/air speed is not a sustainable air-relative
        # capability. v2 must use the caller's separately bound conservative model.
        assert context.flight_capability is not None
        planning_speed = min(
            selected_policy.maximum_planning_speed_mps,
            context.flight_capability.cruise_airspeed_mps,
        )
    route_legs = _route_legs(
        task,
        context,
        include_return=task.completion_action is PlanAction.RETURN,
    )
    assert context.wind_north_mps is not None and context.wind_east_mps is not None
    travel_duration_s = _directional_travel_duration(
        route_legs,
        airspeed_mps=planning_speed,
        wind_north_mps=context.wind_north_mps,
        wind_east_mps=context.wind_east_mps,
        minimum_progress_mps=selected_policy.minimum_speed_mps,
    )
    # This is the route-wide harmonic ground speed implied by the individual
    # wind-triangle solutions.  It is deliberately not used as a speed floor:
    # an infeasible leg has already failed closed above.
    effective_speed = total_distance_m / travel_duration_s
    overhead_s = selected_policy.takeoff_duration_s + selected_policy.landing_duration_s
    if normalized_vehicle == "vtol":
        # The platform-neutral plan remains identical; the VTOL compiler owns
        # the actual transition items.  Budget both transitions here.
        overhead_s += 2.0 * selected_policy.vtol_transition_duration_s
    overhead_s += sum(_DETECTION_OVERHEAD_S[action] for action in task.on_detection)
    estimated_duration_s = (travel_duration_s + overhead_s) * selected_policy.duration_safety_factor

    # ContextSnapshot.estimated_flight_time_s is the remaining endurance at
    # the sampled battery level.  Scale it by the energy left above the task's
    # reserve and explicit policy margin so a mission cannot consume the
    # reserve merely because the raw endurance number looked large enough.
    reserve_pct = task.constraints.minimum_return_battery_pct
    energy_headroom_pct = (
        context.battery_remaining_pct - reserve_pct - selected_policy.minimum_battery_margin_pct
    )
    usable_fraction = max(0.0, energy_headroom_pct / max(context.battery_remaining_pct, 1.0e-9))
    assert context.estimated_flight_time_s is not None
    available_duration_s = context.estimated_flight_time_s * usable_fraction
    if resource_requirements is not None:
        # The explicit model already subtracts receive age from total endurance,
        # while sampled battery remains unmodified. Scaling that aged total would
        # incorrectly reduce the reserve as well. Charge the missing fraction of
        # elapsed time: (E-age)*f-age*(1-f) = E*f-age, deducting reserve only once.
        available_duration_s = max(
            0.0,
            available_duration_s
            - resource_requirements.modeled_endurance_receive_age_s * (1.0 - usable_fraction),
        )
    if estimated_duration_s > available_duration_s + 1.0e-9:
        raise PlannerError("estimated mission duration exceeds energy budget above return reserve")
    if estimated_duration_s > task.constraints.maximum_duration_s + 1.0e-9:
        raise PlannerError("estimated mission duration exceeds task maximum_duration_s")

    return PlanningEstimate(
        task_id=task.task_id,
        vehicle_class=normalized_vehicle,
        outbound_distance_m=outbound_distance_m,
        task_distance_m=task_distance_m,
        return_distance_m=return_distance_m,
        total_distance_m=total_distance_m,
        cruise_speed_mps=effective_speed,
        estimated_duration_s=estimated_duration_s,
        available_duration_s=available_duration_s,
    )


def plan_task(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    policy: PlannerPolicy | None = None,
    resource_requirements: RouteAcquisitionRequirements | None = None,
) -> PlatformNeutralPlan:
    """Compile a resolved task into one deterministic platform-neutral plan.

    ``vehicle_class`` is used only for the conservative readiness/energy
    estimate.  The resulting steps and hashes are intentionally independent of
    the airframe class, so a SIM plan can be compared byte-for-byte with the
    HIL/REAL plan before a vehicle-specific PX4 compiler is selected.
    """

    estimate = estimate_task(
        task, context, vehicle_class, policy=policy, resource_requirements=resource_requirements
    )
    primary_action = _TASK_ACTIONS[task.task_type]
    feature_id = task.geometry.resolved_feature_id
    steps: list[PlanStep] = [PlanStep(0, PlanAction.TAKEOFF)]
    steps.append(
        PlanStep(
            len(steps),
            PlanAction.TRANSIT,
            f"{feature_id}:approach",
            estimate.outbound_distance_m,
        )
    )
    # Keep the executable plan's task quantity aligned with the conservative
    # WGS-84 estimate.  A sparse centerline must never make the plan appear
    # shorter than the distance used by the energy/time gates.
    steps.append(PlanStep(len(steps), primary_action, feature_id, estimate.task_distance_m))

    for action in task.on_detection:
        if action is primary_action:
            continue
        reference = {
            PlanAction.OBSERVE_TARGET: f"{feature_id}:target",
            PlanAction.CAPTURE_EVIDENCE: f"{feature_id}:evidence",
            PlanAction.REPORT_GEOLOCATION: f"{feature_id}:geolocation",
            PlanAction.REVISIT: f"{feature_id}:revisit",
        }[action]
        quantity = task.geometry.buffer_m if action is PlanAction.REVISIT else None
        steps.append(PlanStep(len(steps), action, reference, quantity))

    if task.completion_action is PlanAction.RETURN:
        steps.append(
            PlanStep(
                len(steps),
                PlanAction.RETURN,
                f"{feature_id}:home",
                estimate.return_distance_m,
            )
        )
        steps.append(PlanStep(len(steps), PlanAction.LAND))
    else:
        steps.append(PlanStep(len(steps), PlanAction.LAND))

    return PlatformNeutralPlan(
        plan_id=f"{task.task_id}:platform-neutral-v1",
        task_spec_hash=task.canonical_hash,
        context_snapshot_hash=context.canonical_hash,
        steps=tuple(steps),
    )


# Verbose alias for callers that describe the operation as compilation.
compile_platform_neutral_plan = plan_task


def _validate_inputs(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    vehicle_class: str,
    policy: PlannerPolicy,
    *,
    resource_requirements: RouteAcquisitionRequirements | None = None,
) -> str:
    if not isinstance(task, ResolvedTaskSpec):
        raise PlannerError("task must be a ResolvedTaskSpec")
    if not isinstance(context, ContextSnapshot):
        raise PlannerError("context must be a ContextSnapshot")
    normalized_vehicle = normalize_vehicle_class(vehicle_class)
    if resource_requirements is not None:
        if not isinstance(resource_requirements, RouteAcquisitionRequirements):
            raise PlannerError("resource_requirements must be RouteAcquisitionRequirements")
        resource_requirements.validate_for(task, context, normalized_vehicle)
    if not task.ready_for_planning:
        raise PlannerError("task contains unresolved assumptions")
    if task.geometry.resolution_confidence < policy.minimum_geometry_confidence:
        raise PlannerError("task geometry confidence is below the planning threshold")
    if context.gps_fix_type < policy.minimum_gps_fix_type:
        raise PlannerError("GPS fix does not meet the planning threshold")
    if context.gps_horizontal_accuracy_m > policy.maximum_gps_horizontal_accuracy_m:
        raise PlannerError("GPS horizontal accuracy exceeds the planning threshold")
    if not context.geofence_valid:
        raise PlannerError("geofence is not valid")
    for name, available in (
        ("camera", context.camera_available),
        ("perception", context.perception_available or resource_requirements is not None),
        ("map cache", context.map_cache_available),
        ("DEM", context.dem_available),
    ):
        if not available:
            raise PlannerError(f"{name} resource is unavailable")
    if context.battery_remaining_pct <= (
        task.constraints.minimum_return_battery_pct + policy.minimum_battery_margin_pct
    ):
        raise PlannerError("battery is not above return reserve plus planning margin")
    if context.estimated_flight_time_s is None or context.estimated_flight_time_s <= 0.0:
        raise PlannerError("estimated flight time must be positive")
    if context.schema_version == 2:
        _validate_v2_planning_sources(context, normalized_vehicle)
    seen_actions: set[PlanAction] = set()
    for action in task.on_detection:
        if action not in _DETECTION_ACTIONS:
            raise PlannerError(f"on_detection action {action.value!r} is not allowed")
        if action in seen_actions:
            raise PlannerError("on_detection actions must be unique")
        seen_actions.add(action)
    return normalized_vehicle


def _validate_v2_planning_sources(context: ContextSnapshot, vehicle_class: str) -> None:
    """Accept only declared, exact-scope v2 inputs; SHA is not certification."""
    provenance = context.provenance
    assert provenance is not None
    if context.wind_north_mps is None or context.wind_east_mps is None:
        raise PlannerError("v2 planning requires a known wind model or observation")
    for source in (provenance.endurance, provenance.wind):
        if source.kind.startswith("explicit_SIM_") and provenance.runtime_kind != "SIM":
            raise PlannerError("explicit SIM models cannot authorize non-SIM planning")
    capability = context.flight_capability
    if capability is None:
        raise PlannerError("v2 planning requires an explicit conservative flight capability")
    if (
        capability.airframe_profile_id != context.airframe_profile_id
        or capability.airframe_profile_hash != context.airframe_profile_hash
        or capability.vehicle_class != vehicle_class
        or capability.runtime_kind != provenance.runtime_kind
    ):
        raise PlannerError(
            "flight capability does not match context airframe, vehicle, or runtime scope"
        )
    if vehicle_class in {"fixed-wing", "vtol"} and provenance.airspeed.kind not in {
        "air_relative_sensor",
        "air_relative_estimator",
    }:
        raise PlannerError("fixed-wing cruise requires a qualified air-relative airspeed source")


def _route_distances(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
) -> tuple[float, float, float]:
    points = task.geometry.centerline
    try:
        outbound = wgs84_distance_m(
            latitude1_deg=context.aircraft_position.latitude_deg,
            longitude1_deg=context.aircraft_position.longitude_deg,
            latitude2_deg=points[0].latitude_deg,
            longitude2_deg=points[0].longitude_deg,
        )
        polyline = sum(
            wgs84_distance_m(
                latitude1_deg=first.latitude_deg,
                longitude1_deg=first.longitude_deg,
                latitude2_deg=second.latitude_deg,
                longitude2_deg=second.longitude_deg,
            )
            for first, second in zip(points[:-1], points[1:], strict=True)
        )
        return_distance = wgs84_distance_m(
            latitude1_deg=points[-1].latitude_deg,
            longitude1_deg=points[-1].longitude_deg,
            latitude2_deg=context.home_position.latitude_deg,
            longitude2_deg=context.home_position.longitude_deg,
        )
    except (TypeError, ValueError) as exc:
        raise PlannerError(f"WGS-84 route distance cannot be computed: {exc}") from exc
    # A declared length may include a buffered or otherwise denser path than
    # the two-point sample.  Use the larger value so a sparse centerline can
    # never make the energy/time estimate optimistic.
    return outbound, max(task.geometry.length_m, polyline), return_distance


def _route_legs(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    *,
    include_return: bool,
) -> tuple[tuple[float, float], ...]:
    """Return ``(distance_m, initial_bearing_deg)`` for every route leg.

    The sampled centerline can be shorter than its declared geometry length.
    In that case its segment distances are scaled proportionally while their
    WGS-84 bearings remain unchanged.  This preserves the existing
    conservative distance rule without inventing a new heading.
    """

    points = task.geometry.centerline
    centerline_distances = [
        wgs84_distance_m(
            latitude1_deg=left.latitude_deg,
            longitude1_deg=left.longitude_deg,
            latitude2_deg=right.latitude_deg,
            longitude2_deg=right.longitude_deg,
        )
        for left, right in zip(points[:-1], points[1:], strict=True)
    ]
    sampled_length = sum(centerline_distances)
    if sampled_length <= 1.0e-9:
        raise PlannerError("task centerline has no usable direction")
    task_distance = max(task.geometry.length_m, sampled_length)
    scale = task_distance / sampled_length

    # Build outbound and centerline legs separately so the declared-vs-sampled
    # scale cannot accidentally stretch the outbound leg.
    scaled: list[tuple[float, float]] = []
    outbound = wgs84_distance_m(
        latitude1_deg=context.aircraft_position.latitude_deg,
        longitude1_deg=context.aircraft_position.longitude_deg,
        latitude2_deg=points[0].latitude_deg,
        longitude2_deg=points[0].longitude_deg,
    )
    if outbound > 1.0e-9:
        scaled.append((outbound, _initial_bearing_deg(context.aircraft_position, points[0])))
    for distance, left, right in zip(
        centerline_distances,
        points[:-1],
        points[1:],
        strict=True,
    ):
        if distance > 1.0e-9:
            scaled.append((distance * scale, _initial_bearing_deg(left, right)))

    if include_return:
        last = points[-1]
        return_distance = wgs84_distance_m(
            latitude1_deg=last.latitude_deg,
            longitude1_deg=last.longitude_deg,
            latitude2_deg=context.home_position.latitude_deg,
            longitude2_deg=context.home_position.longitude_deg,
        )
        if return_distance > 1.0e-9:
            scaled.append((return_distance, _initial_bearing_deg(last, context.home_position)))
    if not scaled:
        raise PlannerError("route contains no usable distance")
    return tuple(scaled)


def _directional_travel_duration(
    route_legs: tuple[tuple[float, float], ...],
    *,
    airspeed_mps: float,
    wind_north_mps: float,
    wind_east_mps: float,
    minimum_progress_mps: float,
) -> float:
    """Compute route time from a steady wind-triangle solution per leg."""

    duration_s = 0.0
    for distance_m, track_deg in route_legs:
        track = math.radians(track_deg % 360.0)
        north, east = math.cos(track), math.sin(track)
        wind_along = wind_north_mps * north + wind_east_mps * east
        wind_cross = wind_east_mps * north - wind_north_mps * east
        if abs(wind_cross) > airspeed_mps + 1.0e-9:
            raise PlannerError("crosswind exceeds planning airspeed")
        forward_air = airspeed_mps * math.sqrt(max(0.0, 1.0 - (wind_cross / airspeed_mps) ** 2))
        # Do not use favourable wind as extra endurance.  A deployment may
        # later adopt a reviewed tailwind policy, but the common planner must
        # remain conservative across SIM, PX4 and REAL until that policy is
        # explicitly versioned and validated.
        progress_mps = min(airspeed_mps, forward_air + wind_along)
        if progress_mps <= minimum_progress_mps:
            raise PlannerError("wind leaves insufficient forward progress")
        duration_s += distance_m / progress_mps
    if not math.isfinite(duration_s) or duration_s <= 0.0:
        raise PlannerError("wind-aware route duration is invalid")
    return duration_s


def _initial_bearing_deg(left: object, right: object) -> float:
    """Return the clockwise WGS-84 initial bearing between two GeoPoints."""

    lat1 = math.radians(left.latitude_deg)
    lat2 = math.radians(right.latitude_deg)
    delta_lon = math.radians(right.longitude_deg - left.longitude_deg)
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    if abs(x) < 1.0e-15 and abs(y) < 1.0e-15:
        raise PlannerError("route bearing is undefined for coincident points")
    return math.degrees(math.atan2(y, x)) % 360.0


__all__ = [
    "PlannerError",
    "PlannerPolicy",
    "PlanningEstimate",
    "compile_platform_neutral_plan",
    "estimate_task",
    "normalize_vehicle_class",
    "plan_task",
]
