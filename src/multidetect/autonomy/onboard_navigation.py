"""Compile bounded onboard navigation intent through the shared Mission path.

The realtime model is allowed to suggest a bounded local N/E displacement.  It
is not allowed to turn that displacement into MAVLink or actuator output.  This
module is the adapter-owned authoring seam: it projects the displacement on
WGS-84, replaces only the runtime waypoint, then runs the existing planner and
PX4 compiler.  SIM, HIL and REAL callers therefore receive the same
``PlannedMissionCompilation`` for the same context, task, runtime and model
intent.
"""

from __future__ import annotations

from dataclasses import replace

from ..geolocation_accuracy import wgs84_distance_m
from ..mission_planning import (
    PlannedMissionCompilation,
    compile_planned_task_mission,
    parse_mission_compile_runtime,
)
from ..px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy
from ..target_geolocation import wgs84_forward_from_ned
from .contracts import ContextSnapshot, GeoPoint, ResolvedTaskSpec
from .onboard_agent import OnboardFlightAction, OnboardFlightPlan
from .planner import normalize_vehicle_class


class OnboardNavigationCompileError(ValueError):
    """Raised when model navigation cannot be proven safe and compilable."""


_MOVEMENT_ACTIONS = frozenset(
    {
        OnboardFlightAction.MOVE_RELATIVE,
        OnboardFlightAction.GOTO_RELATIVE,
        OnboardFlightAction.SEARCH,
        OnboardFlightAction.FOLLOW_TARGET,
    }
)


def compile_model_flight_plan(
    task: ResolvedTaskSpec,
    context: ContextSnapshot,
    vehicle_class: str,
    *,
    runtime: object,
    flight_plan: OnboardFlightPlan,
    airframe_profile_hash: str,
    mission_id: str,
) -> PlannedMissionCompilation:
    """Compile one model navigation proposal as a new hash-bound Mission.

    Only the multirotor compiler is enabled for this first shared model
    contract.  VTOL/fixed-wing planners remain available through their normal
    task path until their vehicle-specific turn envelopes are modelled.  The
    requested offsets are bounded by :class:`OnboardFlightPlan`, projected
    using the same WGS-84 primitive as outdoor target geolocation, and then
    checked again by the ordinary energy/geofence/altitude compiler gates.
    """

    if not isinstance(task, ResolvedTaskSpec):
        raise TypeError("task must be a ResolvedTaskSpec")
    if not isinstance(context, ContextSnapshot):
        raise TypeError("context must be a ContextSnapshot")
    if not isinstance(flight_plan, OnboardFlightPlan):
        raise TypeError("flight_plan must be an OnboardFlightPlan")
    normalized_vehicle = normalize_vehicle_class(vehicle_class)
    if normalized_vehicle != "multirotor":
        raise OnboardNavigationCompileError(
            "model navigation compilation currently requires a multirotor profile"
        )
    if flight_plan.action not in _MOVEMENT_ACTIONS:
        raise OnboardNavigationCompileError(
            "safety flight actions must use the executor safety path"
        )
    if not isinstance(mission_id, str) or not mission_id.strip():
        raise ValueError("mission_id must be non-empty")

    geometry, policy = parse_mission_compile_runtime(runtime, normalized_vehicle)
    if not isinstance(geometry, MultirotorMissionGeometry):
        raise OnboardNavigationCompileError("runtime did not provide multirotor geometry")
    if not isinstance(policy, Px4MultirotorSafetyPolicy):
        raise OnboardNavigationCompileError("runtime did not provide multirotor safety policy")

    target_latitude, target_longitude = wgs84_forward_from_ned(
        latitude_deg=context.aircraft_position.latitude_deg,
        longitude_deg=context.aircraft_position.longitude_deg,
        north_offset_m=flight_plan.north_offset_m,
        east_offset_m=flight_plan.east_offset_m,
    )
    requested_altitude = (
        geometry.takeoff_altitude_m
        if flight_plan.altitude_m is None
        else flight_plan.altitude_m
    )
    if requested_altitude is None:  # pragma: no cover - guarded by geometry contract
        raise OnboardNavigationCompileError("model target altitude is unavailable")
    # The model may regress the generic [0, 300] m output range, while the
    # selected airframe has a narrower relative-altitude envelope.  Project
    # that one field into the already-reviewed runtime policy; coordinates,
    # energy and geofence are still rejected by the canonical compiler.
    target_altitude = min(
        policy.maximum_relative_altitude_m,
        max(policy.minimum_relative_altitude_m, float(requested_altitude)),
    )
    target = GeoPoint(
        target_latitude,
        target_longitude,
        float(target_altitude),
        "relative_home",
    )

    # Mission geometry is intentionally reduced to the current model target;
    # retaining stale prompt waypoints would make the apparent model decision
    # diverge from the uploaded route.  The safety geometry/policy remains the
    # same runtime document and is revalidated by the canonical compiler.
    compiled_geometry = replace(geometry, waypoints=(target,))
    displacement_m = wgs84_distance_m(
        latitude1_deg=context.aircraft_position.latitude_deg,
        longitude1_deg=context.aircraft_position.longitude_deg,
        latitude2_deg=target.latitude_deg,
        longitude2_deg=target.longitude_deg,
    )
    # The planner requires a two-point centerline with a usable direction.  A
    # zero-offset HOLD-like movement is not accepted by the model adapter; a
    # one-metre north sample keeps the route explicit and bounded instead of
    # inventing a stale destination.
    if displacement_m <= 1.0e-6:
        second_latitude, second_longitude = wgs84_forward_from_ned(
            latitude_deg=target.latitude_deg,
            longitude_deg=target.longitude_deg,
            north_offset_m=1.0,
            east_offset_m=0.0,
        )
        centerline = (
            context.aircraft_position,
            GeoPoint(second_latitude, second_longitude, target.altitude_m, "relative_home"),
        )
        declared_length_m = 1.0
    else:
        centerline = (context.aircraft_position, target)
        declared_length_m = displacement_m
    task_geometry = replace(
        task.geometry,
        centerline=centerline,
        # The replacement route is the model's local movement, not the stale
        # prompt corridor.  Keeping the old declared length would make the
        # platform plan claim a 400 m search while the wire Mission flies 14 m.
        length_m=max(0.001, declared_length_m),
    )
    model_task = replace(task, geometry=task_geometry)
    return compile_planned_task_mission(
        model_task,
        context,
        normalized_vehicle,
        geometry=compiled_geometry,
        policy=policy,
        airframe_profile_hash=airframe_profile_hash,
        mission_id=mission_id,
    )


__all__ = ["OnboardNavigationCompileError", "compile_model_flight_plan"]
