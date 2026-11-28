"""CPU-only lowering for the X1 multi-leg FW corridor (DESIGN-corridor step 4).

This module only builds the ordered primitive list and its sliced FW evidence.
It does not modify, wrap or bypass :class:`X1MotionProofBuilder`: the builder in
``x1_motion_proof.py`` still enforces its single-waypoint route identity, so
these products are intentionally **for step 5 (motion-proof generalisation)
only**. Nothing here is emitted to a vehicle and no executor decision is
changed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .mission_package import MAX_NO_FLY_ZONES, effective_no_fly_radius
from .motion_intent import MotionCommand
from .serialization import require_real
from .x1_mission import (
    AGL_MAX_M,
    AGL_MIN_M,
    FW_PASS_CROSS_M,
    FW_PASS_MAX_STEP_M,
    FW_PASS_OUTWARD_M,
    MC_RETURN_SPEED_MPS,
    POSITION_MARGIN_M,
    X1_PROFILE_ID,
    X1MissionStep,
    X1PlanError,
    _terrain_bounds,
)

# The tube is the same passage envelope used by the single-waypoint lowering.
CORRIDOR_TUBE_HALF_WIDTH_M = FW_PASS_OUTWARD_M + POSITION_MARGIN_M

# Explicit marker for reviewers and step 5: these steps are not accepted by the
# current MotionProof builder yet and must not be treated as proof evidence.
LOWERING_PRODUCT_FOR_STEP5_ONLY = True

Point = tuple[float, float]
Point3 = tuple[float, float, float]


@dataclass(frozen=True, slots=True)
class CorridorLeg:
    """One lowered FW leg, from a vertex to the next vertex (or the gate)."""

    index: int
    source_step_id: str
    start_ne: Point
    end_ne: Point
    cruise_msl: float
    heading_deg: float
    vertex_turn_deg: float
    kind: str
    tube_half_width_m: float
    duration_bound_s: float
    peak_power_w: float

    @property
    def length_m(self) -> float:
        return math.dist(self.start_ne, self.end_ne)

    @property
    def start_amsl(self) -> Point3:
        return (self.start_ne[0], self.start_ne[1], self.cruise_msl)

    @property
    def end_amsl(self) -> Point3:
        return (self.end_ne[0], self.end_ne[1], self.cruise_msl)


@dataclass(frozen=True, slots=True)
class CorridorSlice:
    """AABB slice of one command, matching the fields used by SweptLeg.

    ``command_end`` is true only for the last slice of a command so step 5 can
    compare command anchors without treating interior slice ends as anchors.
    """

    phase: str
    start: Point3
    end: Point3
    padding_ne_m: float
    padding_alt_m: float
    duration_s: float
    peak_power_w: float
    command_end: bool = False

    def box(self) -> tuple[float, float, float, float, float, float]:
        n, e, z = zip(self.start, self.end, strict=True)
        h, v = self.padding_ne_m, self.padding_alt_m
        return min(n) - h, max(n) + h, min(e) - h, max(e) + h, min(z) - v, max(z) + v


@dataclass(frozen=True, slots=True)
class X1CorridorLowering:
    """Ordered primitives plus per-leg geometry for a multi-leg FW corridor.

    The list is ``takeoff, fw_transition, fw_goto * n, back_transition,
    mc_return_to_home, vtol_land``. Motion command indices are 0 for the
    outbound transition, 1..n for the FW legs, n+1 for back-transition and
    n+2 for the MC return; takeoff/landing have no motion command.

    Per the module docstring these steps are for step 5 only and are not proof
    evidence under the current builder.
    """

    steps: tuple[X1MissionStep, ...]
    legs: tuple[CorridorLeg, ...]
    leg_slices: tuple[tuple[CorridorSlice, ...], ...]
    cruise_msl: float
    height_m: float
    runout_m: float
    gate_ne: Point | None
    standoff_gate_enabled: bool
    max_sweep_legs: int

    @property
    def pattern(self) -> tuple[str, ...]:
        return tuple(step.primitive for step in self.steps)

    @property
    def fw_leg_count(self) -> int:
        return len(self.legs)

    @property
    def slices(self) -> tuple[CorridorSlice, ...]:
        return tuple(slice_ for group in self.leg_slices for slice_ in group)

    @property
    def command_indices(self) -> tuple[int | None, ...]:
        """Motion-command index aligned with ``steps`` (None for takeoff/land)."""
        indices: list[int | None] = [None, 0]
        indices.extend(range(1, len(self.legs) + 1))
        indices.extend((len(self.legs) + 1, len(self.legs) + 2, None))
        return tuple(indices)


def _profile_ok(profile) -> None:
    if (profile.id != X1_PROFILE_ID or profile.category != "vtol"
            or profile.subtype != "tailsitter" or profile.transition is None
            or profile.endurance is None or not profile.can_hover):
        raise X1PlanError("AIRFRAME", f"expected the {X1_PROFILE_ID} VTOL profile")
    if not 0 <= profile.endurance.wind_margin_mps < min(
        MC_RETURN_SPEED_MPS, profile.cruise_airspeed_mps
    ):
        raise X1PlanError("AIRFRAME", "wind margin leaves no positive transit speed")


def _success_path(plan):
    by_id = {step.id: step for step in plan.steps}
    path = []
    step = plan.steps[0] if plan.steps else None
    while step is not None and step.id not in {item.id for item in path}:
        path.append(step)
        step = by_id.get(step.on_success)
    return path


def _turn_angle_deg(a: Point, b: Point, c: Point) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    cross = v1[0] * v2[1] - v1[1] * v2[0]
    dot = v1[0] * v2[0] + v1[1] * v2[1]
    return math.degrees(math.atan2(abs(cross), dot))


def _unit(a: Point, b: Point) -> Point:
    length = math.dist(a, b)
    if length <= 0:
        raise X1PlanError("FW_LEG_TOO_SHORT", "corridor leg has zero length")
    return (b[0] - a[0]) / length, (b[1] - a[1]) / length


def _point_segment_distance(start: Point, end: Point, point: Point) -> float:
    dn, de = end[0] - start[0], end[1] - start[1]
    length2 = dn * dn + de * de
    if not length2:
        return math.dist(start, point)
    t = min(1.0, max(0.0, ((point[0] - start[0]) * dn
                           + (point[1] - start[1]) * de) / length2))
    return math.dist(point, (start[0] + t * dn, start[1] + t * de))


def _corners(a: Point, b: Point, width: float, *, extend_start: float = 0.0):
    ux, uy = _unit(a, b)
    px, py = -uy, ux
    ax, ay = a[0] - ux * extend_start, a[1] - uy * extend_start
    return tuple(
        (x + sx * width * px, y + sx * width * py)
        for x, y in ((ax, ay), b)
        for sx in (-1.0, 1.0)
    )


def _check_segment(package, terrain, *, a: Point, b: Point, cruise_msl: float,
                   mission_radius_m: float, area, zones, label: str) -> None:
    """Check one swept segment with the single-waypoint corridor rules."""
    width = CORRIDOR_TUBE_HALF_WIDTH_M
    start_extension = POSITION_MARGIN_M
    if any(not (area["north_min_m"] <= n <= area["north_max_m"]
                and area["east_min_m"] <= e <= area["east_max_m"]
                and math.dist((n, e), package.document["home"]) <= mission_radius_m)
           for n, e in _corners(a, b, width, extend_start=start_extension)):
        raise X1PlanError(
            "RUNOUT_OUTSIDE_FENCE",
            f"{label} including FW runout exits the package fence or mission radius")
    if len(zones) > MAX_NO_FLY_ZONES:
        raise X1PlanError("NO_FLY_ZONE_LIMIT", "package has too many no-fly zones")
    ux, uy = _unit(a, b)
    check_start = (a[0] - ux * start_extension, a[1] - uy * start_extension)
    for centre, radius in zones:
        if _point_segment_distance(check_start, b, centre) <= width + effective_no_fly_radius(radius):
            raise X1PlanError(
                "NO_FLY_ZONE", f"{label} including FW runout overlaps no-fly circle at {centre}")

    length = math.dist(a, b)
    start_m = -POSITION_MARGIN_M
    segment_count = math.ceil((length - start_m) / 50.0)
    for index in range(segment_count):
        start = start_m + 50.0 * index
        end = min(start + 50.0, length)
        px, py = -uy, ux
        points = tuple(
            (a[0] + along * ux + cross * px, a[1] + along * uy + cross * py)
            for along in (start, end) for cross in (-width, width)
        )
        north, east = zip(*points, strict=True)
        low, high = _terrain_bounds(
            terrain, (min(north), max(north), min(east), max(east)))
        min_agl, max_agl = cruise_msl - high, cruise_msl - low
        if min_agl < AGL_MIN_M or max_agl > AGL_MAX_M:
            raise X1PlanError(
                "CORRIDOR_AGL",
                f"{label} at {start:.0f} m along-track spans {min_agl:.1f}–{max_agl:.1f} m "
                "AGL; expected 40–120 m")


def _check_corridor_polyline(package, terrain, *, legs, home: Point, cruise_msl: float,
                             mission_radius_m: float, runout_m: float,
                             gate_ne: Point | None = None) -> None:
    """Check every leg plus the final back-transition overshoot.

    The per-segment DEM slicing, fence/no-fly and AGL rules are the same 50 m /
    55 m checks used by ``x1_mission._check_corridor`` for one waypoint.
    """
    area = package.document["operating_area"]
    if (context := package.document.get("x1_planning")) is not None:
        area = context["corridor"]
        mission_radius_m = min(mission_radius_m, context["max_distance_from_home_m"])
    zones = package.no_fly_zones
    for leg in legs:
        _check_segment(
            package, terrain, a=leg.start_ne, b=leg.end_ne, cruise_msl=cruise_msl,
            mission_radius_m=mission_radius_m, area=area, zones=zones,
            label=f"leg {leg.index}")
    last = legs[-1]
    if gate_ne is not None:
        # The gate ends the FW leg; the post-transition drift continues toward home.
        distance = math.dist(gate_ne, home)
        toward = _unit(gate_ne, home)
        extra = max(0.0, runout_m - distance)
        end = (gate_ne[0] + toward[0] * (distance + extra),
               gate_ne[1] + toward[1] * (distance + extra))
    else:
        ux, uy = _unit(last.start_ne, last.end_ne)
        end = (last.end_ne[0] + ux * runout_m, last.end_ne[1] + uy * runout_m)
    _check_segment(
        package, terrain, a=last.end_ne, b=end, cruise_msl=cruise_msl,
        mission_radius_m=mission_radius_m, area=area, zones=zones,
        label="final FW runout")


def _check_budget_corridor(package, profile, *, height_m: float, fw_path_m: float,
                           mc_distance_m: float) -> None:
    budget = package.document["budget"]
    endurance = profile.endurance
    climb_s = height_m / profile.max_climb_rate_mps
    descent_s = height_m / profile.max_descent_rate_mps
    fw_s = fw_path_m / (profile.cruise_airspeed_mps - endurance.wind_margin_mps)
    mc_s = mc_distance_m / (MC_RETURN_SPEED_MPS - endurance.wind_margin_mps)
    transition_s = 2 * profile.transition.timeout_s
    if climb_s + fw_s + transition_s + mc_s + descent_s > budget["time_s"] - budget["reserve_time_s"]:
        raise X1PlanError("TIME_BUDGET", "X1 corridor estimate exceeds time budget after reserve")
    energy_wh = 2 * endurance.transition_energy_wh + (
        climb_s * endurance.climb_power_w + fw_s * endurance.cruise_power_w
        + mc_s * endurance.hover_power_w + descent_s * endurance.descent_power_w
    ) / 3600.0
    available_wh = min(budget["energy_wh"] - budget["reserve_energy_wh"],
                       endurance.battery_wh * (1.0 - endurance.reserve_fraction))
    if energy_wh > available_wh:
        raise X1PlanError("ENERGY_BUDGET", "X1 corridor estimate exceeds energy budget after reserve")


def _max_slice_length(dx: float, dy: float, pad: float, max_len: float) -> float:
    """Longest slice whose padded AABB corners stay inside the corridor tube."""
    if math.hypot(dx, dy) <= 1e-12:
        return max_len

    def distance(length: float) -> float:
        ux, uy = dx / math.hypot(dx, dy), dy / math.hypot(dx, dy)
        sx, sy = ux * length, uy * length
        n0, n1 = min(0.0, sx) - pad, max(0.0, sx) + pad
        e0, e1 = min(0.0, sy) - pad, max(0.0, sy) + pad
        corners = ((n0, e0), (n0, e1), (n1, e0), (n1, e1))
        return max(_point_segment_distance((0.0, 0.0), (dx, dy), corner)
                   for corner in corners)

    if distance(max_len) <= CORRIDOR_TUBE_HALF_WIDTH_M + 1e-9:
        return max_len
    if pad * math.sqrt(2.0) > CORRIDOR_TUBE_HALF_WIDTH_M + 1e-9:
        raise X1PlanError(
            "CORRIDOR_SLICE_CAP",
            "slice padding cannot fit inside the corridor tube at any length")
    low, high = 1e-6, max_len
    for _ in range(64):
        middle = (low + high) / 2.0
        if distance(middle) <= CORRIDOR_TUBE_HALF_WIDTH_M + 1e-9:
            low = middle
        else:
            high = middle
    return low


def slice_leg(start: Point3, end: Point3, pad_ne: float, pad_alt: float,
              duration_bound: float, power: float, max_len: float, *,
              max_slices: int | None = None) -> tuple[CorridorSlice, ...]:
    """Split one FW command into AABB slices inside the 55 m corridor tube.

    Slice durations are distributed proportionally so their sum is exactly
    ``duration_bound``; only the last slice has ``command_end=True``. A slice
    may be shorter than ``max_len`` when a diagonal AABB would reach outside the
    tube, or when the cap would otherwise be exceeded.
    """
    try:
        pad_ne = require_real(pad_ne, "pad_ne", minimum=0.0)
        pad_alt = require_real(pad_alt, "pad_alt", minimum=0.0)
        duration_bound = require_real(duration_bound, "duration_bound", minimum=0.0)
        power = require_real(power, "power", minimum=1e-9)
        max_len = require_real(max_len, "max_len", minimum=1e-9)
    except ValueError as exc:
        raise X1PlanError("INVALID_SLICE", str(exc)) from exc
    if (not isinstance(start, tuple) or not isinstance(end, tuple)
            or len(start) != 3 or len(end) != 3
            or not all(type(value) in (int, float) for value in (*start, *end))):
        raise X1PlanError("INVALID_SLICE", "slice endpoints must be 3-tuples of numbers")
    if max_slices is not None and (type(max_slices) is not int or max_slices <= 0):
        raise X1PlanError("INVALID_SLICE", "max_slices must be a positive integer")

    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        return (CorridorSlice("fw", start, end, pad_ne, pad_alt, duration_bound,
                              power, command_end=True),)

    chunk = _max_slice_length(dx, dy, pad_ne, max_len)
    count = max(1, math.ceil(length / chunk))
    if max_slices is not None and count > max_slices:
        raise X1PlanError(
            "CORRIDOR_SLICE_CAP",
            f"FW leg needs {count} slices; corridor cap is {max_slices}")

    slices: list[CorridorSlice] = []
    used_duration = 0.0
    for index in range(count):
        along_start = length * index / count
        along_end = length * (index + 1) / count
        fraction = (along_end - along_start) / length
        if index == count - 1:
            slice_duration = duration_bound - used_duration
        else:
            slice_duration = duration_bound * fraction
        used_duration += slice_duration
        t0, t1 = along_start / length, along_end / length
        slices.append(CorridorSlice(
            "fw",
            (start[0] + dx * t0, start[1] + dy * t0, start[2] + (end[2] - start[2]) * t0),
            (start[0] + dx * t1, start[1] + dy * t1, start[2] + (end[2] - start[2]) * t1),
            pad_ne, pad_alt, slice_duration, power, command_end=index == count - 1,
        ))
    return tuple(slices)


def _resolve_target(package, step) -> Point:
    ref = step.params.get("target_ref")
    home = tuple(package.document["home"])
    if ref == "home":
        return home
    if not isinstance(ref, str) or ref not in package.refs:
        raise X1PlanError("UNGROUNDED_DESTINATION", "goto.target_ref must identify a packaged POI")
    if not ref.startswith("poi:"):
        raise X1PlanError("X1_TARGET", "expected a static onboard POI (or home for FW transit)")
    return tuple(package.refs[ref])


def build_x1_corridor_steps(package, *, plan=None, terrain, profile,
                            mission_radius_m: float, bounds=None,
                            fw_min_leg_m: float | None = None) -> X1CorridorLowering:
    """Lower a multi-waypoint X1 plan to corridor primitives.

    Default (``standoff_gate_enabled=False``) keeps the model's FW return leg:
    the last ``fw_goto_waypoint(home)`` flies to home at cruise height and the
    MC return uses the full 505 m runout. When the template enables the
    standoff gate, that last leg is replaced by the synthesized gate G on the
    ``P_{n-1} -> home`` line and MC return uses ``max(s, runout - s)``.
    """
    corridor = bounds if bounds is not None else package.assumed_sim_corridor
    if package.assumed_sim_corridor is None or corridor is None:
        raise X1PlanError(
            "CORRIDOR_NOT_ENABLED", "multi-waypoint X1 lowering requires the assumed SIM corridor")
    _profile_ok(profile)
    plan = package.plan if plan is None else plan
    if plan is None:
        raise X1PlanError("PLAN_REQUIRED", "compile the task with the onboard planner before lowering")
    try:
        radius = require_real(mission_radius_m, "mission_radius_m", minimum=1e-6,
                              maximum=100_000)
    except ValueError as exc:
        raise X1PlanError("INVALID_MISSION_RADIUS", str(exc)) from exc

    path = _success_path(plan)
    skills = [step.skill for step in path]
    work = {"fw_goto_waypoint"}
    if (len(path) != len(plan.steps) or not 5 <= len(path) <= 8
            or skills[:2] != ["vtol_takeoff", "fw_transition"]
            or skills[-2:] != ["back_transition", "return_home"]
            or any(item.on_fail != path[-1].id for item in path[:-1])
            or path[-1].on_success is not None or path[-1].on_fail is not None
            or path[-1].params):
        raise X1PlanError(
            "UNSUPPORTED_PLAN",
            "X1 success path must take off, transition, work, back-transition and recover")
    work_steps = path[2:-2]
    if not 2 <= len(work_steps) <= corridor.max_fw_legs:
        raise X1PlanError(
            "CORRIDOR_LEGS",
            f"corridor lowering needs 2-{corridor.max_fw_legs} FW legs")
    if any(step.skill not in work for step in work_steps):
        raise X1PlanError(
            "UNSUPPORTED_PLAN", "corridor lowering currently supports FW waypoint work only")

    altitudes = [step.params.get("altitude_agl_m") for step in work_steps]
    if any(type(value) not in (int, float) for value in altitudes):
        raise X1PlanError("UNSUPPORTED_PARAMS", "corridor FW legs require altitude_agl_m")
    altitude_agl = float(altitudes[0])
    if any(float(value) != altitude_agl for value in altitudes):
        raise X1PlanError(
            "UNSUPPORTED_PARAMS", "corridor FW legs must share one cruise altitude")
    if not AGL_MIN_M <= altitude_agl <= AGL_MAX_M:
        raise X1PlanError("AGL_OUT_OF_BOUNDS", "corridor FW altitude must be within 40–120 m AGL")

    home = tuple(package.document["home"])
    vertices = [home]
    for step in work_steps:
        vertices.append(_resolve_target(package, step))
    try:
        cruise_msl = terrain.sample_local(*vertices[1]).elevation_m + altitude_agl
        home_ground = terrain.sample_local(*home).elevation_m
    except ValueError as exc:
        raise X1PlanError("DEM_COVERAGE", str(exc)) from exc
    height_m = cruise_msl - home_ground
    if height_m < profile.transition.minimum_height_m:
        raise X1PlanError(
            "TRANSITION_HEIGHT", f"{height_m:.1f} m above home is below the profile's transition minimum")
    area = package.document["operating_area"]
    if not area["min_altitude_m"] <= height_m <= area["max_altitude_m"]:
        raise X1PlanError(
            "HOME_RELATIVE_ALTITUDE",
            f"cruise at {height_m:.1f} m above home violates the package altitude bounds")

    runout = FW_PASS_MAX_STEP_M + POSITION_MARGIN_M + profile.transition.timeout_s * (
        profile.max_airspeed_mps + profile.endurance.wind_margin_mps)
    default_min_leg = max(50.0, 3.0 * profile.cruise_airspeed_mps)
    try:
        minimum = default_min_leg if fw_min_leg_m is None else require_real(
            fw_min_leg_m, "fw_min_leg_m", minimum=default_min_leg, maximum=100_000)
    except ValueError as exc:
        raise X1PlanError("INVALID_FW_MIN_LEG", str(exc)) from exc

    last_is_home = work_steps[-1].params.get("target_ref") == "home"
    gate_ne = None
    if corridor.standoff_gate_enabled:
        if not last_is_home:
            raise X1PlanError(
                "CORRIDOR_RETURN_TOO_SHORT",
                "standoff gate requires an explicit FW return leg to home")
        standoff = float(corridor.standoff_m)
        distance = math.dist(vertices[-2], home)
        if distance < standoff + minimum:
            raise X1PlanError(
                "CORRIDOR_RETURN_TOO_SHORT",
                "FW return leg is shorter than the standoff gate plus minimum leg")
        ux, uy = _unit(home, vertices[-2])
        gate_ne = (home[0] + ux * standoff, home[1] + uy * standoff)
        vertices[-1] = gate_ne

    legs: list[CorridorLeg] = []
    for index, (step, end) in enumerate(zip(work_steps, vertices[1:], strict=True), start=1):
        start = vertices[index - 1]
        length = math.dist(start, end)
        if length < minimum:
            raise X1PlanError(
                "FW_LEG_TOO_SHORT",
                f"corridor leg {index} is {length:.1f} m, below fw_min_leg_m={minimum:g} m")
        before = next((point for point in reversed(vertices[:index - 1]) if point != start), start)
        turn = 0.0 if index == 1 else _turn_angle_deg(before, start, end)
        if index == len(work_steps) and (last_is_home or gate_ne is not None):
            kind = "return"
        elif index == 1:
            kind = "out"
        else:
            kind = "dogleg"
        heading = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) % 360.0
        duration = length / (profile.cruise_airspeed_mps - profile.endurance.wind_margin_mps)
        legs.append(CorridorLeg(
            index=index, source_step_id=step.id, start_ne=start, end_ne=end,
            cruise_msl=cruise_msl, heading_deg=heading, vertex_turn_deg=turn, kind=kind,
            tube_half_width_m=CORRIDOR_TUBE_HALF_WIDTH_M, duration_bound_s=duration,
            peak_power_w=float(corridor.fw_leg_peak_power_w),
        ))

    _check_corridor_polyline(
        package, terrain, legs=legs, home=home, cruise_msl=cruise_msl,
        mission_radius_m=radius, runout_m=runout, gate_ne=gate_ne)

    if gate_ne is not None:
        mc_distance = max(float(corridor.standoff_m),
                          runout - float(corridor.standoff_m))
    else:
        mc_distance = math.dist(vertices[-1], home) + runout
    _check_budget_corridor(
        package, profile, height_m=height_m,
        fw_path_m=sum(leg.length_m for leg in legs), mc_distance_m=mc_distance)

    leg_slices_list = []
    for leg in legs:
        leg_slices_list.append(slice_leg(
            leg.start_amsl, leg.end_amsl, corridor.fw_tracking_m, 0.0,
            leg.duration_bound_s, float(corridor.fw_leg_peak_power_w),
            float(corridor.slice_m), max_slices=corridor.max_sweep_legs))
    total_slices = sum(len(group) for group in leg_slices_list)
    if total_slices > corridor.max_sweep_legs:
        raise X1PlanError(
            "CORRIDOR_SLICE_CAP",
            f"corridor needs {total_slices} slices; cap is {corridor.max_sweep_legs}")

    first = work_steps[0]
    back = path[-2]
    finish = path[-1]
    steps: list[X1MissionStep] = [
        X1MissionStep("vtol_takeoff", first.id, takeoff_height_m=height_m),
        X1MissionStep(
            "fw_transition", first.id, MotionCommand("transition", mode="FW"),
            required_mc_heading_deg=legs[0].heading_deg),
    ]
    for leg, step in zip(legs, work_steps, strict=True):
        metadata = {"long_range_home_relative": True}
        if gate_ne is not None and leg.end_ne == gate_ne:
            metadata["corridor_gate"] = True
        steps.append(X1MissionStep(
            "fw_goto_waypoint", step.id,
            MotionCommand(
                "reposition",
                target_ned=(leg.end_ne[0] - home[0], leg.end_ne[1] - home[1], -height_m),
                speed_mps=profile.cruise_airspeed_mps, mode="FW",
                metadata=metadata, fly_through_radius_m=FW_PASS_CROSS_M,
            ),
        ))
    steps.extend((
        X1MissionStep(
            "back_transition", back.id,
            MotionCommand("transition", mode="MC", reason="back_transition")),
        X1MissionStep(
            "mc_return_to_home", finish.id,
            MotionCommand(
                "mc_reposition", target_ned=(0.0, 0.0, -height_m),
                speed_mps=MC_RETURN_SPEED_MPS, mode="MC",
                reason="hover_return_to_home")),
        X1MissionStep("vtol_land", finish.id),
    ))

    return X1CorridorLowering(
        steps=tuple(steps), legs=tuple(legs), leg_slices=tuple(leg_slices_list),
        cruise_msl=cruise_msl, height_m=height_m, runout_m=runout, gate_ne=gate_ne,
        standoff_gate_enabled=bool(corridor.standoff_gate_enabled),
        max_sweep_legs=corridor.max_sweep_legs,
    )