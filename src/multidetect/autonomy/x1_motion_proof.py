"""Reject-only X1 motion proof over a trusted, bounded execution trajectory."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

from multidetect.terrain_dem import LocalDemGrid

from .airframe_profile import AirframeProfile
from .mission_package import MissionPackage, effective_no_fly_radius
from .x1_contingency_executor import Decision, MotionProof, RuntimeFrame, X1AirbornePlanAdapter
from .x1_corridor import CORRIDOR_TUBE_HALF_WIDTH_M, X1CorridorLowering
from .x1_mission import (
    AGL_MAX_M, AGL_MIN_M, FW_PASS_CROSS_M, FW_PASS_MAX_STEP_M, FW_PASS_OUTWARD_M,
    POSITION_MARGIN_M, X1MissionStep, _terrain_bounds,
)

Point = tuple[float, float, float]  # DEM-local north/east and metres AMSL, never PX4 NED.
Box = tuple[float, float, float, float, float, float]  # N min/max, E min/max, AMSL min/max.
_PHASES = frozenset({"mc", "fw", "transition", "landing"})
_ANCHOR_TOL_M = 0.01  # Numeric agreement of lowered endpoints, not a tracking allowance.


@dataclass(frozen=True, slots=True)
class SweptLeg:
    """Certified tracking extent around a segment, including any entry and exit transients.

    The box is deliberately conservative. Split long or diagonal legs when
    their box would cover unrelated terrain or violate a corner of the fence.
    Duration is an upper bound through this leg, not a nominal ETA.
    """

    phase: str
    start: Point
    end: Point
    padding_ne_m: float
    padding_alt_m: float
    duration_s: float
    peak_power_w: float
    # True only for the final slice of a command. Interior slice ends are not
    # command anchors and must not be compared as route waypoints.
    command_end: bool = True

    def box(self) -> Box:
        n, e, z = zip(self.start, self.end, strict=True)
        h, v = self.padding_ne_m, self.padding_alt_m
        return min(n) - h, max(n) + h, min(e) - h, max(e) + h, min(z) - v, max(z) + v


@dataclass(frozen=True, slots=True)
class LandingPad:
    """Surveyed touchdown disk with a hard terrain-elevation error/relief bound.

    The host vouches that the whole disk is landable, not just its centre.
    """

    point: Point
    radius_m: float
    elevation_error_m: float


@dataclass(frozen=True, slots=True)
class ExecutionSweep:
    """Host-only evidence from the exact runner/native lowering for this tick.

    Includes every branch through landing or an enforced revalidation point.
    For this slice only a single path (no divergent motion branch) is supported.
    The producer must bound actual motion (incl. wind and tracking dynamics),
    not copy the airframe's admission limits or planner's requested setpoints.
    """

    frame_id: int
    decision_digest: str
    legs: tuple[SweptLeg, ...]
    max_bank_deg: float | None
    max_climb_mps: float | None
    max_descent_mps: float | None
    max_acceleration_mps2: float | None
    max_angular_rate_dps: float | None
    min_airspeed_mps: float | None = None
    max_airspeed_mps: float | None = None
    landing_pad: LandingPad | None = None
    # Native Hold/orbit otherwise has no upper bound on residence time.
    hold_revalidation_enforced: bool = False
    # On an off-nominal FW work edge, no second path may execute under this proof.
    failure_revalidation_enforced: bool = False


def _number(value, minimum=-100_000.0, maximum=1e6):
    return type(value) in (int, float) and minimum <= value <= maximum


def _nonnegative(value):
    return value if _number(value, 0.0) else None


def _point(value):
    return isinstance(value, tuple) and len(value) == 3 and all(_number(x) for x in value)


def _same(a, b):
    return math.dist(a, b) <= _ANCHOR_TOL_M


def _phase_groups(legs):
    groups = []
    for leg in legs:
        if not groups or groups[-1][0].phase != leg.phase:
            groups.append([])
        groups[-1].append(leg)
    return groups


def _corners(box):
    return tuple((n, e) for n in box[:2] for e in box[2:4])


def _corridor_box_samples(box):
    """Corners, edge midpoints and centre of a box (corridor mode only; stricter than corners)."""
    n0, n1, e0, e1, *_ = box
    n_mid, e_mid = (n0 + n1) / 2.0, (e0 + e1) / 2.0
    return (*_corners(box), (n_mid, e0), (n_mid, e1), (n0, e_mid), (n1, e_mid), (n_mid, e_mid))


def _box_distance(box, point):
    n0, n1, e0, e1, *_ = box
    n, e = point
    return math.hypot(max(n0 - n, 0, n - n1), max(e0 - e, 0, e - e1))


def _segment_distance(start, end, point):
    """Distance from a 2-D point to a closed segment (same as x1_plan's helper)."""
    dn, de = end[0] - start[0], end[1] - start[1]
    length2 = dn * dn + de * de
    t = 0.0 if not length2 else min(1.0, max(0.0, ((point[0] - start[0]) * dn
                                                  + (point[1] - start[1]) * de) / length2))
    return math.dist(point, (start[0] + t * dn, start[1] + t * de))


class X1MotionProofBuilder:
    """Bind the original lowering, DEM and obstacle coverage before flight."""

    def __init__(self, package: MissionPackage, profile: AirframeProfile, terrain: LocalDemGrid,
                 steps: tuple[X1MissionStep, ...], *, mission_radius_m: float,
                 terrain_error_m: float | None = None,
                 obstacle_clearance: Callable[[Box], float | None] | None = None,
                 execution_sweep: Callable[[Decision, RuntimeFrame], ExecutionSweep | None] | None = None,
                 corridor: X1CorridorLowering | None = None):
        self.package, self.profile, self.terrain = package, profile, terrain
        self.terrain_error_m, self.obstacle_clearance = terrain_error_m, obstacle_clearance
        self.execution_sweep = execution_sweep
        self._corridor = corridor
        if corridor is None:
            if tuple(s.primitive for s in steps) != (
                "vtol_takeoff", "fw_transition", "fw_goto_waypoint", "back_transition", "mc_return_to_home", "vtol_land"
            ) or not _number(mission_radius_m, 0.001, 100_000):
                raise ValueError("expected the admitted X1 one-waypoint lowering and radius")
            self.home_ne = tuple(package.document["home"])
            self.home_z = terrain.sample_local(*self.home_ne).elevation_m
            outbound, inbound = steps[2].command, steps[4].command
            n, e, down = outbound.target_ned
            if inbound.target_ned != (0.0, 0.0, down) or not math.hypot(n, e) > 0:
                raise ValueError("expected a common cruise height and a home terminal")
            length = math.hypot(n, e)
            self.outbound = self.home_ne[0] + n, self.home_ne[1] + e, self.home_z - down
            self.home_arrival = (*self.home_ne, self.home_z - down)
            self.tangent = n / length, e / length
            self.runout_end = length + FW_PASS_MAX_STEP_M + POSITION_MARGIN_M + profile.transition.timeout_s * (
                profile.max_airspeed_mps + profile.endurance.wind_margin_mps
            )
        else:
            if not _number(mission_radius_m, 0.001, 100_000):
                raise ValueError("expected the admitted X1 corridor lowering and radius")
            self.home_ne = tuple(package.document["home"])
            self.home_z = terrain.sample_local(*self.home_ne).elevation_m
            self._validate_corridor_route(steps, corridor)
        self.area = dict(package.document["operating_area"])
        context = package.document.get("x1_planning")
        self.corridor = dict(context["corridor"]) if context else self.area
        self.radius = min(mission_radius_m, context["max_distance_from_home_m"]) if context else mission_radius_m
        self.zones = package.no_fly_zones

    def _validate_corridor_route(self, steps, corridor):
        """Bind the proof to the exact corridor lowering; any mismatch is fatal."""
        bounds = self.package.assumed_sim_corridor
        if not isinstance(corridor, X1CorridorLowering) or bounds is None or self.package.mode != "sim":
            raise ValueError("corridor proof requires the assumed SIM corridor package")
        legs = corridor.legs
        n = len(legs)
        if tuple(s.primitive for s in steps) != corridor.pattern or len(steps) != n + 5:
            raise ValueError("steps do not match the corridor lowering pattern")
        if not 2 <= n <= bounds.max_fw_legs:
            raise ValueError("corridor leg count outside max_fw_legs")
        if sum(1 for s in steps if s.primitive == "fw_goto_waypoint") != n:
            raise ValueError("corridor pattern and leg count disagree")
        expected_runout = FW_PASS_MAX_STEP_M + POSITION_MARGIN_M + self.profile.transition.timeout_s * (
            self.profile.max_airspeed_mps + self.profile.endurance.wind_margin_mps)
        if not _number(corridor.runout_m, 0.0) \
                or abs(corridor.runout_m - expected_runout) > _ANCHOR_TOL_M:
            raise ValueError("corridor runout differs from the profile back-transition bound")
        if type(corridor.max_sweep_legs) is not int or not 0 < corridor.max_sweep_legs <= bounds.max_sweep_legs:
            raise ValueError("invalid corridor sweep cap")
        if bool(corridor.standoff_gate_enabled) != bool(bounds.standoff_gate_enabled):
            raise ValueError("standoff gate setting differs from the package corridor")
        downs = []
        for index, leg in enumerate(legs):
            if leg.index != index + 1 or not _point(leg.start_amsl) or not _point(leg.end_amsl) \
                    or abs(leg.tube_half_width_m - CORRIDOR_TUBE_HALF_WIDTH_M) > _ANCHOR_TOL_M \
                    or not _number(leg.vertex_turn_deg, 0.0):
                raise ValueError("invalid corridor leg geometry")
            if index and not _same(legs[index - 1].end_amsl, leg.start_amsl):
                raise ValueError("corridor legs are not head-to-tail")
            command = steps[2 + index].command
            if command is None or not _point(command.target_ned):
                raise ValueError("corridor FW command is missing its target")
            n_offset, e_offset, down = command.target_ned
            target = (self.home_ne[0] + n_offset, self.home_ne[1] + e_offset, self.home_z - down)
            if not _same(target, leg.end_amsl):
                raise ValueError("corridor FW target does not match the lowered vertex")
            downs.append(down)
        if any(abs(down - downs[0]) > _ANCHOR_TOL_M for down in downs):
            raise ValueError("corridor FW commands do not share one cruise height")
        recovery = steps[3 + n].command
        if recovery is None or not _point(recovery.target_ned) \
                or not _same(recovery.target_ned, (0.0, 0.0, downs[0])):
            raise ValueError("corridor MC return is not the home terminal at cruise height")
        last_fw = steps[1 + n].command
        gate_marked = last_fw is not None and bool(last_fw.metadata.get("corridor_gate"))
        if bounds.standoff_gate_enabled:
            if not gate_marked or not (isinstance(corridor.gate_ne, tuple) and len(corridor.gate_ne) == 2
                                       and all(_number(value) for value in corridor.gate_ne)):
                raise ValueError("corridor gate is not bound to the final FW command")
            if not _same((*corridor.gate_ne, legs[-1].cruise_msl), legs[-1].end_amsl):
                raise ValueError("corridor gate does not match the final FW vertex")
            if abs(math.dist(corridor.gate_ne, self.home_ne) - bounds.standoff_m) > _ANCHOR_TOL_M:
                raise ValueError("corridor gate is not at the template standoff distance")
        elif gate_marked or corridor.gate_ne is not None:
            raise ValueError("standoff gate is present but disabled in the corridor template")

    def __call__(self, decision: Decision, frame: RuntimeFrame) -> MotionProof:
        sweep = self.execution_sweep(decision, frame) if self.execution_sweep is not None else None
        return self.build(decision, frame, sweep)

    def _in_original_corridor(self, boxes):
        tn, te = self.tangent
        for box in boxes:
            for n, e in _corners(box):
                n, e = n - self.home_ne[0], e - self.home_ne[1]
                if not (-POSITION_MARGIN_M <= n * tn + e * te <= self.runout_end
                        and abs(-n * te + e * tn) <= FW_PASS_OUTWARD_M + POSITION_MARGIN_M):
                    return False
        return True

    def _corridor_turn_pad_m(self):
        bounds = self.package.assumed_sim_corridor
        r_min = math.ceil(
            self.profile.max_airspeed_mps ** 2
            / (9.80665 * math.tan(math.radians(self.profile.max_bank_deg))))
        wind_drift = (self.profile.endurance.wind_margin_mps * math.pi * r_min
                      / self.profile.cruise_airspeed_mps)
        return 2.0 * r_min + FW_PASS_CROSS_M + bounds.fw_tracking_m + wind_drift

    def _corridor_segments(self):
        """Leg tubes plus the final back-transition overshoot; never the raw AABB."""
        corridor = self._corridor
        segments = [(leg.start_ne, leg.end_ne, leg.tube_half_width_m) for leg in corridor.legs]
        last = corridor.legs[-1]
        if corridor.gate_ne is not None:
            distance = math.dist(corridor.gate_ne, self.home_ne)
            if distance > 0:
                ux = (self.home_ne[0] - corridor.gate_ne[0]) / distance
                uy = (self.home_ne[1] - corridor.gate_ne[1]) / distance
            else:
                ux = uy = 0.0
            length = max(distance, corridor.runout_m)
            end = (corridor.gate_ne[0] + ux * length, corridor.gate_ne[1] + uy * length)
        else:
            length = math.dist(last.start_ne, last.end_ne)
            if length > 0:
                ux = (last.end_ne[0] - last.start_ne[0]) / length
                uy = (last.end_ne[1] - last.start_ne[1]) / length
            else:
                ux = uy = 0.0
            end = (last.end_ne[0] + ux * corridor.runout_m,
                   last.end_ne[1] + uy * corridor.runout_m)
        segments.append((last.end_ne, end, last.tube_half_width_m))
        turn_pad = self._corridor_turn_pad_m()
        pads = [(leg.start_ne, turn_pad) for leg in corridor.legs[1:] if leg.vertex_turn_deg > 1e-6]
        return segments, pads

    def _in_corridor_union(self, boxes):
        """Every box corner must lie in the union of leg tubes and vertex turn pads."""
        segments, pads = self._corridor_segments()
        for box in boxes:
            # Corners alone can miss a concave union, so edge midpoints and the centre must pass too.
            for n, e in _corridor_box_samples(box):
                if any(math.dist((n, e), centre) <= pad for centre, pad in pads):
                    continue
                if min(_segment_distance(start, end, (n, e)) - width
                       for start, end, width in segments) > 1e-9:
                    return False
        return True

    def _position_in_leg(self, point, index):
        leg = self._corridor.legs[index - 1]
        return _segment_distance(leg.start_ne, leg.end_ne, point[:2]) <= leg.tube_half_width_m + 1e-9

    def _route_matches_corridor(self, d, frame, sweep, boxes):
        """Continue-only general corridor route; retask/hold/native RTL are later steps."""
        corridor = self._corridor
        n = len(corridor.legs)
        if d.action != "continue" or type(frame.active_motion_index) is not int:
            return False
        groups = _phase_groups(sweep.legs)
        phases = tuple(group[0].phase for group in groups)
        last = sweep.legs[-1].end
        if not _same(last, (*self.home_ne, self.home_z)):
            return False
        index, mode = frame.active_motion_index, frame.vtol_state
        if 1 <= index <= n:
            if mode != "fw" or sweep.failure_revalidation_enforced is not True:
                return False
            if not self._position_in_leg(frame.position_ne_amsl, index):
                return False
            prefix = ("fw", "transition")
        elif index == n + 1:
            if mode != "fw":
                return False
            prefix = ("transition",)
        elif index == n + 2:
            if mode != "mc":
                return False
            prefix = ()
        else:
            return False
        if not self._in_corridor_union(boxes) or phases != (*prefix, "mc", "landing"):
            return False
        if not all(_same(leg.start[:2], self.home_ne) and _same(leg.end[:2], self.home_ne)
                   for leg in groups[-1]):
            return False
        if 1 <= index <= n:
            fw = groups[0]
            if not _same(fw[-1].end, corridor.legs[-1].end_amsl):
                return False
            anchors = [leg.end for leg in fw if leg.command_end is True]
            expected = [leg.end_amsl for leg in corridor.legs[index - 1:]]
            if len(anchors) != len(expected) or any(
                    not _same(anchor, target) for anchor, target in zip(anchors, expected, strict=True)):
                return False
        return True

    def _route_matches(self, d, frame, sweep, boxes, *, native_fw_rtl=False):
        if self._corridor is not None:
            return self._route_matches_corridor(d, frame, sweep, boxes)
        groups = _phase_groups(sweep.legs)
        phases = tuple(group[0].phase for group in groups)
        first, last = sweep.legs[0].start, sweep.legs[-1].end
        if native_fw_rtl:
            # Direct native RTL uses the mission package corridor (checked by
            # _fence_clear), not the narrow compiled straight-line connector.
            return (d.action == "return" and frame.vtol_state == "fw" and frame.native_hold_active is True
                    and phases == ("fw", "transition", "mc", "landing")
                    and _same(last, (*self.home_ne, self.home_z))
                    and all(_same(x.start[:2], self.home_ne) and _same(x.end[:2], self.home_ne)
                            for x in groups[-1]))
        if d.action == "loiter":
            return self._hold_matches(d, frame, sweep, phases, boxes[-1])

        if d.action == "land-now":
            return (frame.vtol_state == "mc" and phases == ("landing",)
                    and all(_same(first[:2], x.end[:2]) for x in sweep.legs))

        if d.action not in {"continue", "return", "retask"} or not _same(last, (*self.home_ne, self.home_z)):
            return False
        mode = frame.vtol_state
        prefix = ()
        if d.action == "continue":
            if not self._in_original_corridor(boxes):
                return False
            # Indices are of command-bearing steps in the admitted lowering.
            stage = (frame.active_motion_index, mode)
            if stage == (1, "fw"):
                if sweep.failure_revalidation_enforced is not True or phases[:1] != ("fw",):
                    return False
                prefix = "fw", "transition"
                n, e = first[0] - self.outbound[0], first[1] - self.outbound[1]
                passed = n * self.tangent[0] + e * self.tangent[1] >= 0
                if not passed and not _same(groups[0][-1].end, self.outbound):
                    return False
            elif stage == (2, "fw"):
                prefix = ("transition",)
            elif stage != (3, "mc"):
                return False
        elif d.action == "return":
            if mode not in {"fw", "mc"} or not self._in_original_corridor(boxes):
                return False
            prefix = ("transition",) if mode == "fw" else ()
        else:
            if d.plan is None or X1AirbornePlanAdapter.check_airborne_plan(d.plan, frame) is not None:
                return False
            self.package.validate_plan(d.plan)
            if mode == "fw":
                work = d.plan.steps[:-2]
                prefix = ("transition",)
                if work:
                    if (len(work) != 1 or work[0].skill != "fw_goto_waypoint" or phases[:1] != ("fw",)
                            or sweep.failure_revalidation_enforced is not True):
                        return False
                    params = work[0].params
                    if params["target_ref"] not in self.package.document["places"]:
                        return False
                    target = self.package.refs[params["target_ref"]]
                    altitude = self.terrain.sample_local(*target).elevation_m + params["altitude_agl_m"]
                    if not _same(groups[0][-1].end, (*target, altitude)):
                        return False
                    prefix = "fw", "transition"
        if phases != (*prefix, "mc", "landing"):
            return False
        if d.action == "return" and not _same(groups[-1][0].start, self.home_arrival):
            return False
        # Require an explicit home arrival before beginning the vertical landing.
        return all(_same(leg.start[:2], self.home_ne) and _same(leg.end[:2], self.home_ne) for leg in groups[-1])

    def _hold_matches(self, d, frame, sweep, phases, orbit_box):
        hold = frame.hold_sites.get(d.params["hold_ref"])
        if not hold or hold.mode != frame.vtol_state or sweep.hold_revalidation_enforced is not True:
            return False
        first, last = sweep.legs[0].start, sweep.legs[-1].end
        if frame.vtol_state == "mc":
            return phases == ("mc",) and all(_same(first, x.end) for x in sweep.legs)
        if phases != ("fw",) or not _number(hold.radius_m, 0.001) or not all(
            _number(value) for value in (hold.lat_deg, hold.lon_deg, hold.alt_amsl_m)
        ):
            return False
        n, e = self.terrain.local_from_wgs84(hold.lat_deg, hold.lon_deg)
        r = hold.radius_m
        n0, n1, e0, e1, z0, z1 = orbit_box
        # A centre within anchoring tolerance does not imply that the disk fits.
        # Keep the explicit padding requirement as well as all four disk extents.
        return (_same(last, (n, e, hold.alt_amsl_m)) and sweep.legs[-1].padding_ne_m >= r
                and n0 <= n - r and n + r <= n1 and e0 <= e - r and e + r <= e1
                and z0 <= hold.alt_amsl_m <= z1)

    def _fence_clear(self, leg, box):
        if any(not (area["north_min_m"] <= n <= area["north_max_m"]
                    and area["east_min_m"] <= e <= area["east_max_m"])
               for n, e in _corners(box) for area in (self.area, self.corridor)):
            return False
        if any(math.dist(corner, self.home_ne) > self.radius for corner in _corners(box)):
            return False
        if any(_box_distance(box, centre) <= effective_no_fly_radius(radius) for centre, radius in self.zones):
            return False
        # Terminal contact is the only exception to the package altitude floor.
        return (box[5] - self.home_z <= self.area["max_altitude_m"]
                and (leg.phase == "landing" or box[4] - self.home_z >= self.area["min_altitude_m"]))

    def _terrain_clearance(self, legs, boxes, pad, *, all_airborne=False):
        margins, transition_agl = [], []
        landed = False
        for index, (leg, box) in enumerate(zip(legs, boxes, strict=True)):
            low, high = _terrain_bounds(self.terrain, box[:4])
            error = self.terrain_error_m
            agl = box[4] - high - error
            if leg.phase == "landing":
                # Check the entire touchdown footprint. The surveyed disk is
                # the only volume in which contact with the surface is allowed.
                if not (isinstance(pad, LandingPad) and _point(pad.point) and _number(pad.radius_m, 0.001)
                        and _number(pad.elevation_error_m, error) and _same(legs[-1].end, pad.point)
                        and all(math.dist(corner, pad.point[:2]) <= pad.radius_m for corner in _corners(box))
                        and pad.point[2] - pad.elevation_error_m <= low - error
                        and high + error <= pad.point[2] + pad.elevation_error_m
                        and leg.start[2] >= leg.end[2]
                        and box[4] >= pad.point[2] - pad.elevation_error_m):
                    return None, None, False
                if index < len(legs) - 1:
                    if agl <= 0:  # Only the last segment may enter the surveyed contact region.
                        return None, None, False
                    margins.append(agl)
                elif leg.start[2] <= leg.end[2] or leg.start[2] - leg.padding_alt_m <= high + error:
                    return None, None, False
                else:
                    landed = True
                continue
            margins.append(agl)
            if all_airborne or leg.phase in {"fw", "transition"}:
                margins.extend((agl - AGL_MIN_M, AGL_MAX_M - (box[5] - low + error)))
            if leg.phase == "transition":
                transition_agl.append(agl)
        return min(margins, default=0.0), min(transition_agl, default=None), landed

    def build(self, decision: Decision, frame: RuntimeFrame, sweep: ExecutionSweep | None,
              *, retask_anchors=None, installed_continue=False, native_fw_rtl=False) -> MotionProof:
        closed = MotionProof(
            frame.frame_id, decision.digest, geofence_clear=False, obstacle_margin_m=None,
            terrain_margin_m=None, required_energy_wh=None, available_energy_wh=None,
            min_airspeed_mps=None, max_airspeed_mps=None, max_bank_deg=None, max_climb_mps=None,
            max_descent_mps=None, max_acceleration_mps2=None, max_angular_rate_dps=None,
            required_time_s=None, available_time_s=None,
        )
        if (not isinstance(sweep, ExecutionSweep) or type(frame.frame_id) is not int or type(sweep.frame_id) is not int
                or sweep.frame_id != frame.frame_id or sweep.decision_digest != decision.digest
                or frame.position_ok is not True or frame.px4_failsafe is not False or frame.armed_in_air is not True
                or frame.mission_id != self.package.mission_id or decision.mission_id != frame.mission_id
                or decision.revision != frame.revision or decision.event_id != frame.event_id
                or not _point(frame.position_ne_amsl) or not isinstance(sweep.legs, tuple)
                or not 1 <= len(sweep.legs) <= (
                    128 if self._corridor is None else self._corridor.max_sweep_legs)):
            return closed
        legs = sweep.legs
        if self._corridor is not None and (installed_continue or retask_anchors is not None or native_fw_rtl):
            # Installed continue, live retask and native RTL are later steps.
            return closed
        if (not all(isinstance(x, SweptLeg) and isinstance(x.phase, str) and x.phase in _PHASES
                    and _point(x.start) and _point(x.end)
                    and _number(x.padding_ne_m, 0.0, 100_000) and _number(x.padding_alt_m, 0.0)
                    and _number(x.duration_s, 0.001) and _number(x.peak_power_w, 0.001) for x in legs)
                or not _same(frame.position_ne_amsl, legs[0].start)
                or any(a.end != b.start for a, b in zip(legs, legs[1:]))
                or any(v is not None and _nonnegative(v) is None
                       for v in (sweep.min_airspeed_mps, sweep.max_airspeed_mps))):
            return closed
        try:
            boxes = tuple(leg.box() for leg in legs)
            if installed_continue:
                phases = tuple(group[0].phase for group in _phase_groups(legs))
                legal = {"fw": {("fw", "transition", "mc", "landing"), ("transition", "mc", "landing")},
                         "back_transition": {("transition", "mc", "landing")},
                         "mc": {("mc", "landing"), ("landing",)}}
                if decision.action != "continue" or phases not in legal.get(frame.vtol_state, ()):
                    return closed
            elif retask_anchors is None:
                if not self._route_matches(decision, frame, sweep, boxes, native_fw_rtl=native_fw_rtl):
                    return closed
            elif decision.action != "retask" or frame.retask_boundary is not True:
                return closed
            anchors = legs if self._corridor is None else tuple(
                leg for leg in legs if leg.command_end is True)
            if (installed_continue or retask_anchors is not None) and (
                sweep.failure_revalidation_enforced is not True
                or tuple((leg.phase, leg.end) for leg in anchors) != retask_anchors
                or legs[-1].phase != "landing" or legs[-1].end != (*self.home_ne, self.home_z)
                or legs[-1].start[:2] != self.home_ne
            ):
                return closed
            fence = all(self._fence_clear(leg, box) for leg, box in zip(legs, boxes, strict=True))
            terrain, transition, landed = None, None, False
            if _number(self.terrain_error_m, 0.0) and frame.terrain_conflict is not True:
                terrain, transition, landed = self._terrain_clearance(
                    legs, boxes, sweep.landing_pad, all_airborne=retask_anchors is not None or native_fw_rtl)
            obstacle = None
            if self.obstacle_clearance is not None:
                margins = tuple(self.obstacle_clearance(box) for box in boxes)
                if all(_number(margin) for margin in margins):
                    obstacle = min(margins)
        except (ValueError, OSError, RuntimeError):
            return closed

        power = self.profile.endurance
        required_wh = 0.0
        for group in _phase_groups(legs):
            phase = group[0].phase
            if phase == "transition":
                required_wh += max(power.transition_energy_wh, sum(
                    max(x.peak_power_w, power.climb_power_w) * x.duration_s / 3600 for x in group
                ))
            else:
                watt = (max(power.cruise_power_w, power.loiter_power_w) if phase == "fw"
                        else max(power.climb_power_w, power.hover_power_w, power.descent_power_w))
                required_wh += sum(max(x.peak_power_w, watt) * x.duration_s / 3600 for x in group)
        return replace(
            closed, geofence_clear=fence, obstacle_margin_m=obstacle, terrain_margin_m=terrain,
            required_energy_wh=required_wh,
            available_energy_wh=_nonnegative(frame.remaining_energy_wh),
            min_airspeed_mps=_nonnegative(sweep.min_airspeed_mps), max_airspeed_mps=_nonnegative(sweep.max_airspeed_mps),
            max_bank_deg=_nonnegative(sweep.max_bank_deg), max_climb_mps=_nonnegative(sweep.max_climb_mps),
            max_descent_mps=_nonnegative(sweep.max_descent_mps),
            max_acceleration_mps2=_nonnegative(sweep.max_acceleration_mps2),
            max_angular_rate_dps=_nonnegative(sweep.max_angular_rate_dps), required_time_s=sum(x.duration_s for x in legs),
            available_time_s=_nonnegative(frame.remaining_time_s),
            landing_site_clear=landed, min_transition_agl_m=transition,
        )
