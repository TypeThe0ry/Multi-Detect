"""CPU-only lowering of an onboard mission package to a bounded X1 SIM sortie."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .airframe_profile import AirframeProfile
from .mission_package import MAX_NO_FLY_ZONES, MissionPackage, effective_no_fly_radius
from .mission_plan import MissionPlan
from .motion_intent import MotionCommand
from .serialization import require_real

if TYPE_CHECKING:
    from multidetect.terrain_dem import LocalDemGrid

X1_PROFILE_ID = "delta_tailsitter_gimbal"
AGL_MIN_M, AGL_MAX_M = 40.0, 120.0
MC_RETURN_SPEED_MPS = 8.0  # LongRangePolicy hover return.
# Same passage and position allowances as x1_scenario_class.py FLY-FIX-3.
FW_PASS_CROSS_M, FW_PASS_OUTWARD_M, FW_PASS_MAX_STEP_M = 25.0, 45.0, 75.0
POSITION_MARGIN_M = 10.0


class X1PlanError(ValueError):
    """A plan that must be refused before arming, with a stable reason code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class X1MissionStep:
    primitive: str
    source_step_id: str
    command: MotionCommand | None = None
    # Lifecycle actions are owned by the collector. The initial transition also
    # requires the collector to align and settle in MC on the outbound heading.
    takeoff_height_m: float | None = None
    required_mc_heading_deg: float | None = None


def x1_place_from_wgs84(
    terrain: LocalDemGrid, latitude_deg: float, longitude_deg: float
) -> tuple[float, float]:
    """Ground a structured WGS-84 destination in the package/DEM N/E frame."""
    return terrain.local_from_wgs84(
        require_real(latitude_deg, "latitude_deg", minimum=-90, maximum=90),
        require_real(longitude_deg, "longitude_deg", minimum=-180, maximum=180),
    )


def x1_place_from_bearing(
    home_ne: tuple[float, float], distance_m: float, bearing_deg: float
) -> tuple[float, float]:
    """Ground a relative destination; bearings are clockwise from true north."""
    distance = require_real(distance_m, "distance_m", minimum=1e-6, maximum=100_000)
    bearing = math.radians(require_real(bearing_deg, "bearing_deg", minimum=0, maximum=360))
    return home_ne[0] + distance * math.cos(bearing), home_ne[1] + distance * math.sin(bearing)


def _adapt_plan(package: MissionPackage, plan: MissionPlan):
    """Bridge the shared v1 package graph to this X1 slice's inputs."""
    skills = tuple(step.skill for step in plan.steps)
    if plan.plan_version != 1 or skills not in {("goto", "return_home"), ("goto", "land")}:
        raise X1PlanError("UNSUPPORTED_PLAN", "expected goto followed by a terminal return or home landing")
    outbound, finish = plan.steps
    if (outbound.id == finish.id or outbound.on_success != finish.id or outbound.on_fail != finish.id
            or finish.on_success is not None or finish.on_fail is not None):
        raise X1PlanError("UNSUPPORTED_BRANCH", "goto success and failure must lead to the terminal step")
    if set(outbound.params) != {"target_ref", "altitude_m"}:
        raise X1PlanError("UNSUPPORTED_PARAMS", "goto requires target_ref and altitude_m")
    if finish.skill == "land" and finish.params != {"target_ref": "home"}:
        raise X1PlanError("LANDING_SITE", "land must explicitly target home")
    if finish.skill == "return_home" and finish.params:
        raise X1PlanError("UNSUPPORTED_PARAMS", "return_home must inherit the outbound cruise altitude")
    if package.document["oversight_steps"]:
        raise X1PlanError("OVERSIGHT_REQUIRED", "this sortie cannot execute operator-gated steps")
    ref = outbound.params["target_ref"]
    if not isinstance(ref, str) or ref not in package.document["places"]:
        raise X1PlanError("UNGROUNDED_DESTINATION", "goto.target_ref must identify a packaged POI")
    altitude = outbound.params["altitude_m"]
    if type(altitude) not in (float, int) or not AGL_MIN_M <= altitude <= AGL_MAX_M:
        raise X1PlanError("AGL_OUT_OF_BOUNDS", "goto.altitude_m must be within 40–120 m AGL")
    try:
        package.validate_plan(plan)
    except ValueError as exc:
        raise X1PlanError("PACKAGE_CONSTRAINT", str(exc)) from exc
    return outbound.id, finish.id, package.refs[ref], float(altitude)


def _terrain_bounds(terrain, bounds):
    """Bound bilinear DEM elevations using vertices of the intersecting cells."""
    n_min, n_max, e_min, e_max = bounds
    if not terrain.contains_local(n_min, e_min) or not terrain.contains_local(n_max, e_max):
        raise X1PlanError("DEM_COVERAGE", "DEM does not cover the cruise and transition corridor")
    r0 = math.floor((n_min - terrain.north_origin_m) / terrain.north_spacing_m)
    r1 = math.ceil((n_max - terrain.north_origin_m) / terrain.north_spacing_m)
    c0 = math.floor((e_min - terrain.east_origin_m) / terrain.east_spacing_m)
    c1 = math.ceil((e_max - terrain.east_origin_m) / terrain.east_spacing_m)
    low, high = math.inf, -math.inf
    for row in terrain.elevations_m[max(0, r0):min(terrain.row_count, r1 + 1)]:
        values = row[max(0, c0):min(terrain.column_count, c1 + 1)]
        low, high = min(low, min(values)), max(high, max(values))
    return low, high


def _check_corridor(package, terrain, *, home, tangent, end_m, mission_radius_m, cruise_msl):
    """Check the inbound/return corridor including back-transition overshoot."""
    tn, te = tangent
    width = FW_PASS_OUTWARD_M + POSITION_MARGIN_M
    start_m = -POSITION_MARGIN_M

    def corners(start, end):
        return tuple((home[0] + along * tn - cross * te, home[1] + along * te + cross * tn)
                     for along in (start, end) for cross in (-width, width))

    envelope = corners(start_m, end_m)
    area = package.document["operating_area"]
    if any(not (area["north_min_m"] <= n <= area["north_max_m"]
                and area["east_min_m"] <= e <= area["east_max_m"]
                and math.dist((n, e), home) <= mission_radius_m) for n, e in envelope):
        raise X1PlanError("RUNOUT_OUTSIDE_FENCE", "corridor including FW runout exits the package fence or mission radius")
    zones = package.no_fly_zones
    if len(zones) > MAX_NO_FLY_ZONES:
        raise X1PlanError("NO_FLY_ZONE_LIMIT", "package has too many no-fly zones")
    for centre, radius in zones:
        n, e = centre[0] - home[0], centre[1] - home[1]
        along, cross = n * tn + e * te, -n * te + e * tn
        if math.hypot(max(start_m - along, 0, along - end_m), max(abs(cross) - width, 0)) <= effective_no_fly_radius(radius):
            raise X1PlanError("NO_FLY_ZONE", f"corridor including FW runout overlaps no-fly circle at {centre}")

    # Short slices avoid pessimistically including the entire bounding box of a
    # long diagonal. All clipped cells contribute their full elevation range.
    segment_count = math.ceil((end_m - start_m) / 50.0)
    for index in range(segment_count):
        start = start_m + 50.0 * index
        north, east = zip(*corners(start, min(start + 50.0, end_m)), strict=True)
        low, high = _terrain_bounds(terrain, (min(north), max(north), min(east), max(east)))
        min_agl, max_agl = cruise_msl - high, cruise_msl - low
        if min_agl < AGL_MIN_M or max_agl > AGL_MAX_M:
            raise X1PlanError("CORRIDOR_AGL", f"corridor at {start:.0f} m along-track spans {min_agl:.1f}–{max_agl:.1f} m AGL; expected 40–120 m")


def _check_budget(package, profile, *, height_m, leg_m, runout_m):
    budget = package.document["budget"]
    endurance = profile.endurance
    climb_s = height_m / profile.max_climb_rate_mps
    descent_s = height_m / profile.max_descent_rate_mps
    fw_s = leg_m / (profile.cruise_airspeed_mps - endurance.wind_margin_mps)
    mc_s = (leg_m + runout_m) / (MC_RETURN_SPEED_MPS - endurance.wind_margin_mps)
    transition_s = 2 * profile.transition.timeout_s
    if climb_s + fw_s + transition_s + mc_s + descent_s > budget["time_s"] - budget["reserve_time_s"]:
        raise X1PlanError("TIME_BUDGET", "X1 sortie estimate exceeds time budget after reserve")
    energy_wh = endurance.transition_energy_wh + (
        climb_s * endurance.climb_power_w + fw_s * endurance.cruise_power_w
        + mc_s * endurance.hover_power_w + descent_s * endurance.descent_power_w
    ) / 3600.0
    available_wh = min(budget["energy_wh"] - budget["reserve_energy_wh"],
                       endurance.battery_wh * (1.0 - endurance.reserve_fraction))
    if energy_wh > available_wh:
        raise X1PlanError("ENERGY_BUDGET", "X1 sortie estimate exceeds energy budget after reserve")


def build_x1_mission_steps(
    package: MissionPackage, *, plan: MissionPlan | None = None,
    terrain: LocalDemGrid, profile: AirframeProfile, mission_radius_m: float,
    fw_min_leg_m: float | None = None,
) -> tuple[X1MissionStep, ...]:
    """Check a model-compiled or precompiled package and lower its nominal path.

    Home and POIs are DEM-local N/E. Motion target coordinates are home-relative
    NED for the long-range collector. No command is emitted on refusal.
    """
    if (profile.id != X1_PROFILE_ID or profile.category != "vtol" or profile.subtype != "tailsitter"
            or profile.transition is None or profile.endurance is None or not profile.can_hover):
        raise X1PlanError("AIRFRAME", f"expected the {X1_PROFILE_ID} VTOL profile")
    if not 0 <= profile.endurance.wind_margin_mps < min(MC_RETURN_SPEED_MPS, profile.cruise_airspeed_mps):
        raise X1PlanError("AIRFRAME", "wind margin leaves no positive transit speed")
    plan = package.plan if plan is None else plan
    if plan is None:
        raise X1PlanError("PLAN_REQUIRED", "compile the task with the onboard planner before lowering")
    outbound_id, finish_id, target, altitude_agl = _adapt_plan(package, plan)
    radius = require_real(mission_radius_m, "mission_radius_m", minimum=1e-6, maximum=100_000)
    default_min_leg = max(50.0, 3.0 * profile.cruise_airspeed_mps)
    minimum = default_min_leg if fw_min_leg_m is None else require_real(
        fw_min_leg_m, "fw_min_leg_m", minimum=default_min_leg,
    )
    home = package.document["home"]
    dn, de = target[0] - home[0], target[1] - home[1]
    leg_m = math.hypot(dn, de)
    if leg_m < minimum:
        raise X1PlanError("FW_LEG_TOO_SHORT", f"outbound leg {leg_m:.1f} m is below fw_min_leg_m={minimum:g} m")
    try:
        cruise_msl = terrain.sample_local(*target).elevation_m + altitude_agl
        height_m = cruise_msl - terrain.sample_local(*home).elevation_m
    except ValueError as exc:
        raise X1PlanError("DEM_COVERAGE", str(exc)) from exc
    if height_m < profile.transition.minimum_height_m:
        raise X1PlanError("TRANSITION_HEIGHT", f"{height_m:.1f} m above home is below the profile's transition minimum")
    area = package.document["operating_area"]
    if not area["min_altitude_m"] <= height_m <= area["max_altitude_m"]:
        raise X1PlanError("HOME_RELATIVE_ALTITUDE", f"cruise at {height_m:.1f} m above home violates the package altitude bounds")
    tangent = dn / leg_m, de / leg_m
    # Bounded passage detection can consume one full telemetry bracket. Keep
    # room beyond it for back-transition under the configured wind margin.
    runout = FW_PASS_MAX_STEP_M + POSITION_MARGIN_M + profile.transition.timeout_s * (
        profile.max_airspeed_mps + profile.endurance.wind_margin_mps
    )
    _check_corridor(package, terrain, home=home, tangent=tangent, end_m=leg_m + runout,
                    mission_radius_m=radius, cruise_msl=cruise_msl)
    _check_budget(package, profile, height_m=height_m, leg_m=leg_m, runout_m=runout)

    return (
        X1MissionStep("vtol_takeoff", outbound_id, takeoff_height_m=height_m),
        X1MissionStep("fw_transition", outbound_id, MotionCommand("transition", mode="FW"),
                      required_mc_heading_deg=math.degrees(math.atan2(de, dn)) % 360.0),
        X1MissionStep("fw_goto_waypoint", outbound_id, MotionCommand(
            "reposition", target_ned=(dn, de, -height_m), speed_mps=profile.cruise_airspeed_mps,
            mode="FW", metadata={"long_range_home_relative": True}, fly_through_radius_m=FW_PASS_CROSS_M,
        )),
        X1MissionStep("back_transition", finish_id, MotionCommand("transition", mode="MC", reason="back_transition")),
        X1MissionStep("mc_return_to_home", finish_id, MotionCommand(
            "mc_reposition", target_ned=(0.0, 0.0, -height_m), speed_mps=MC_RETURN_SPEED_MPS,
            mode="MC", reason="hover_return_to_home",
        )),
        X1MissionStep("vtol_land", finish_id),
    )
