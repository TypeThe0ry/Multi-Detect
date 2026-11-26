"""Build X1 continue proofs from scoped SITL evidence and one fresh PX4 frame."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from multidetect.autonomy.mission_package import number
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_executor import HoldSite, X1AirbornePlanAdapter
from multidetect.autonomy.x1_mission import X1PlanError, lower_airborne_suffix
from multidetect.autonomy.x1_motion_proof import ExecutionSweep, LandingPad, SweptLeg, X1MotionProofBuilder
from multidetect.autonomy.x1_contingency_state import require_token
from multidetect.target_geolocation import wgs84_forward_from_ned

MOTION_SAMPLE_MAX_AGE_MS = 500
_SUFFIXES = {1: ("fw", ("fw", "transition", "mc", "landing")),
             2: ("fw", ("transition", "mc", "landing")),
             3: ("mc", ("mc", "landing"))}


def clearance_scope(steps, faults, *, profile_sha256, world_sha256, image, wind_plugin_sha256):
    return canonical_sha256({"steps": [asdict(step) for step in steps], "faults": [asdict(f) for f in faults],
                             "profile_sha256": profile_sha256, "world_sha256": world_sha256,
                             "image": image, "wind_plugin_sha256": wind_plugin_sha256})


def _fields(raw, *keys):
    if not isinstance(raw, Mapping) or set(raw) != set(keys):
        raise ValueError(f"expected fields: {', '.join(keys)}")
    return [raw[key] for key in keys]


def _vector(raw, size):
    if not isinstance(raw, (tuple, list)) or len(raw) != size:
        raise ValueError(f"expected {size} coordinates")
    return tuple(number(value, "coordinate", minimum=-math.inf) for value in raw)


def _box(raw):
    box = _vector(raw, 6)
    if any(low >= high for low, high in zip(box[::2], box[1::2], strict=True)):
        raise ValueError("coverage box must have positive extent")
    return box


def _contains(box, point):
    return all(low <= value <= high for low, high, value in zip(box[::2], box[1::2], point, strict=True))


def _contains_box(outer, inner):
    return _contains(outer, inner[::2]) and _contains(outer, inner[1::2])


def _clearance(box, coverage, obstacles):
    if not _contains_box(coverage, box):
        return None
    # Touching an occupied bounding volume is a violation. With no surveyed
    # obstacle near this box, its distance to the survey boundary is a lower bound.
    lower_margin = min(low - limit for low, limit in zip(box[::2], coverage[::2], strict=True))
    upper_margin = min(limit - high for high, limit in zip(box[1::2], coverage[1::2], strict=True))
    margin = min(lower_margin, upper_margin)
    for occupied in obstacles:
        distance = math.hypot(*(max(0.0, a - d, c - b) for a, b, c, d in
                                zip(box[::2], box[1::2], occupied[::2], occupied[1::2], strict=True)))
        if distance == 0:
            return -1.0
        margin = min(margin, distance)
    return margin


@dataclass(frozen=True)
class _LegBound:
    phase: str
    end: tuple[float, float, float]
    padding_ne_m: float
    padding_alt_m: float
    duration_s: float
    peak_power_w: float


@dataclass(frozen=True)
class _SuffixBound:
    entry: tuple[float, ...]
    max_entry_speed_mps: float
    legs: tuple[_LegBound, ...]
    dynamics: dict[str, float]
    airspeed: tuple[float | None, float | None]
    failure_revalidation_enforced: bool


def _suffix(raw, index):
    entry, speed, legs, dynamics, airspeed, failure = _fields(
        raw, "entry", "max_entry_speed_mps", "legs", "dynamics", "airspeed", "failure_revalidation_enforced")
    if type(failure) is not bool:
        raise ValueError("expected explicit failure revalidation flag")
    if not isinstance(legs, list) or not 1 <= len(legs) <= 128:
        raise ValueError("expected bounded suffix legs")
    bounds = []
    phases = []
    for leg in legs:
        phase, end, padding_ne, padding_alt, duration, power = _fields(
            leg, "phase", "end", "padding_ne_m", "padding_alt_m", "duration_s", "peak_power_w")
        if not phases or phase != phases[-1]:
            phases.append(phase)
        bounds.append(_LegBound(phase, _vector(end, 3), number(padding_ne, "horizontal padding"),
                                number(padding_alt, "vertical padding"), number(duration, "duration", minimum=0.001),
                                number(power, "power", minimum=0.001)))
    if tuple(phases) != _SUFFIXES[index][1]:
        raise ValueError("suffix phases differ from compiled continue")
    if airspeed is None and index == 3:
        airspeed = (None, None)
    else:
        airspeed = _vector(airspeed, 2)
        if not 0 < airspeed[0] <= airspeed[1]:
            raise ValueError("invalid airspeed bounds")
    keys = ("max_bank_deg", "max_climb_mps", "max_descent_mps", "max_acceleration_mps2", "max_angular_rate_dps")
    values = _fields(dynamics, *keys)
    dynamics = {key: number(value, key) for key, value in zip(keys, values, strict=True)}
    return _SuffixBound(_box(entry), number(speed, "entry speed", minimum=0.001), tuple(bounds), dynamics,
                        airspeed, failure)


_DYNAMICS = ("max_bank_deg", "max_climb_mps", "max_descent_mps", "max_acceleration_mps2", "max_angular_rate_dps")
# PX4 RTL from FW loiters around home before its back-transition (rtl_direct.cpp
# MOVE_TO_LOITER/LOITER_DOWN/MOVE_TO_LAND), which is not the compiled corridor.
# Only the MC return (direct to home, then LAND) is modelled in this slice.
_NATIVE_RETURN_STEPS = frozenset({"3"})
_NATIVE_LAND_STEPS = frozenset({"3"})


def _dynamics(raw):
    return {key: number(value, key) for key, value in zip(_DYNAMICS, _fields(raw, *_DYNAMICS), strict=True)}


@dataclass(frozen=True)
class _LandNowBound:
    max_entry_speed_mps: float
    padding_ne_m: float
    padding_alt_m: float
    duration_s: float
    peak_power_w: float
    dynamics: dict[str, float]


@dataclass(frozen=True)
class _HoldBound:
    site: HoldSite
    steps: frozenset
    entry: tuple[float, ...]
    max_entry_speed_mps: float
    centre: tuple[float, float, float] | None
    padding_ne_m: float
    padding_alt_m: float
    duration_s: float
    peak_power_w: float
    dynamics: dict[str, float]
    airspeed: tuple[float | None, float | None]


def _land_now(raw):
    speed, padding_ne, padding_alt, duration, power, dynamics = _fields(
        raw, "max_entry_speed_mps", "padding_ne_m", "padding_alt_m", "duration_s", "peak_power_w", "dynamics")
    return _LandNowBound(number(speed, "entry speed", minimum=0.001), number(padding_ne, "horizontal padding"),
                         number(padding_alt, "vertical padding"), number(duration, "duration", minimum=0.001),
                         number(power, "power", minimum=0.001), _dynamics(dynamics))


def _hold(raw, terrain):
    mode = raw.get("mode") if isinstance(raw, Mapping) else None
    keys = ["mode", "steps", "entry", "max_entry_speed_mps", "padding_ne_m", "padding_alt_m", "duration_s",
            "peak_power_w", "dynamics"]
    if mode == "fw":
        keys += ["centre", "radius_m", "tangential_speed_mps", "airspeed"]
    elif mode != "mc":
        raise ValueError("hold mode must be mc or fw")
    values = dict(zip(keys, _fields(raw, *keys), strict=True))
    steps = values["steps"]
    if not isinstance(steps, list) or not steps or not set(steps) <= {1, 2, 3} or len(set(steps)) != len(steps):
        raise ValueError("invalid hold step indices")
    common = dict(steps=frozenset(steps), entry=_box(values["entry"]),
                  max_entry_speed_mps=number(values["max_entry_speed_mps"], "entry speed", minimum=0.001),
                  padding_ne_m=number(values["padding_ne_m"], "horizontal padding"),
                  padding_alt_m=number(values["padding_alt_m"], "vertical padding"),
                  duration_s=number(values["duration_s"], "hold residence bound", minimum=0.001),
                  peak_power_w=number(values["peak_power_w"], "power", minimum=0.001),
                  dynamics=_dynamics(values["dynamics"]))
    if mode == "mc":
        return _HoldBound(site=HoldSite("mc"), centre=None, airspeed=(None, None), **common)
    north, east, altitude = _vector(values["centre"], 3)
    radius = number(values["radius_m"], "orbit radius", minimum=0.001)
    speed = number(values["tangential_speed_mps"], "orbit speed", minimum=0.001)
    if common["padding_ne_m"] < radius:
        raise ValueError("orbit padding must cover the whole disk")
    lat, lon = wgs84_forward_from_ned(latitude_deg=terrain.origin_latitude_deg,
                                      longitude_deg=terrain.origin_longitude_deg,
                                      north_offset_m=north, east_offset_m=east)
    # Anchor the proof on the exact point the WGS-84 command will denote.
    n, e = terrain.local_from_wgs84(lat, lon)
    if math.dist((n, e), (north, east)) > 0.05:
        raise ValueError("orbit centre does not round-trip through WGS-84")
    airspeed = _vector(values["airspeed"], 2)
    if not 0 < airspeed[0] <= airspeed[1]:
        raise ValueError("invalid airspeed bounds")
    return _HoldBound(site=HoldSite("fw", lat, lon, altitude, radius, speed), centre=(n, e, altitude),
                      airspeed=airspeed, **common)


def parse_native(raw, terrain):
    """Validate host bounds for native return, land-now and hold sites."""
    if not isinstance(raw, Mapping) or not set(raw) <= {"return", "land-now", "holds"} or not raw:
        raise ValueError("invalid native bound sections")
    returns, landings, holds = raw.get("return", {}), raw.get("land-now", {}), raw.get("holds", {})
    if not isinstance(returns, Mapping) or not set(returns) <= _NATIVE_RETURN_STEPS:
        raise ValueError("native return is bounded only for the MC return step")
    if not isinstance(landings, Mapping) or not set(landings) <= _NATIVE_LAND_STEPS:
        raise ValueError("native land-now is bounded only for the MC return step")
    if not isinstance(holds, Mapping) or len(holds) > 8:
        raise ValueError("expected at most 8 hold sites")
    return ({int(k): _suffix(v, int(k)) for k, v in returns.items()},
            {int(k): _land_now(v) for k, v in landings.items()},
            {require_token(ref): _hold(v, terrain) for ref, v in holds.items()})


class LiveContinueEvidence:
    """The package supplies surveyed geometry and certified bounds for this lowering.

    A suffix bound covers the actual execution from any eligible entry sample,
    including tracking error and branches up to touchdown. Telemetry only gates
    the current entry and budgets. It cannot certify future aircraft dynamics.
    """

    def __init__(self, package, profile, terrain, steps, *, scope, assumed_sim=None):
        self.assumed_sim = assumed_sim
        self.retask_bounds = (assumed_sim or {}).get("airborne_retask")
        self.stage_retask = None
        self.installed_at = None
        self.budget = package.document["budget"]
        self.suffixes = {}
        self.native_returns, self.native_landings, self.holds = {}, {}, {}
        self._entry = None
        self.terrain = terrain
        self.battery_bounds = None
        self.consumption_high_water = {}
        terrain_error, obstacle_clearance, self.pad = None, None, None
        config = package.document.get("x1_live_evidence")
        self.basis = config.get("basis") if config is not None else None
        if self.basis is not None or assumed_sim is not None:
            if (self.basis != "ASSUMED_SIM" or package.mode != "sim" or assumed_sim is None
                    or config.get("assumed_bounds_sha256") != assumed_sim["sha256"]
                    or self.budget.get("battery_capacity_wh") != assumed_sim["capacity_wh"]):
                raise ValueError("ASSUMED_SIM proof requires the scoped SIM configuration")
            config = {k: v for k, v in config.items() if k not in {"basis", "assumed_bounds_sha256"}}
        if config is not None:
            try:
                native = config.get("native")
                config = {k: v for k, v in config.items() if k != "native"}
                (bound_scope, dem_digest, dem_error, coverage, obstacles, pad, battery, suffixes) = _fields(
                    config, "scope_sha256", "dem_sha256", "dem_error_m", "obstacle_coverage",
                    "obstacles", "landing_pad", "battery", "suffixes")
                if (not isinstance(scope, str) or re.fullmatch(r"[a-f0-9]{64}", scope) is None
                        or bound_scope != scope or dem_digest != terrain.source_sha256):
                    raise ValueError("motion evidence scope differs from the active route, DEM or scenario")
                terrain_error = number(dem_error, "DEM error")
                coverage = _box(coverage)
                if not isinstance(obstacles, list) or len(obstacles) > 4096:
                    raise ValueError("invalid obstacle inventory")
                obstacles = tuple(_box(box) for box in obstacles)
                obstacle_clearance = lambda box: _clearance(box, coverage, obstacles)
                radius, pad_error = _fields(pad, "radius_m", "elevation_error_m")
                home = tuple(package.document["home"])
                landing_pad = LandingPad((*home, terrain.sample_local(*home).elevation_m),
                                         number(radius, "pad radius", minimum=0.001),
                                         number(pad_error, "pad error", minimum=terrain_error))
                voltage, soc_error, consumed_error = _fields(
                    battery, "max_voltage_v", "soc_error_pct", "consumption_error_wh")
                battery_bounds = (number(voltage, "voltage upper bound", minimum=0.001),
                                  number(soc_error, "SOC error"), number(consumed_error, "consumption error"))
                if not isinstance(suffixes, Mapping) or not set(suffixes) <= {"1", "2", "3"}:
                    raise ValueError("invalid suffix indices")
                self.suffixes = {int(index): _suffix(raw, int(index)) for index, raw in suffixes.items()}
                if native is not None:
                    self.native_returns, self.native_landings, self.holds = parse_native(native, terrain)
                self.pad, self.battery_bounds = landing_pad, battery_bounds
            except ValueError:
                if self.assumed_sim is not None:
                    raise
                terrain_error, obstacle_clearance = None, None
                self.suffixes, self.native_returns, self.native_landings, self.holds = {}, {}, {}, {}
        self.builder = X1MotionProofBuilder(
            package, profile, terrain, steps,
            mission_radius_m=package.document["x1_planning"]["max_distance_from_home_m"],
            terrain_error_m=terrain_error, obstacle_clearance=obstacle_clearance,
        )

    def remaining_energy_wh(self, row):
        capacity = self.budget.get("battery_capacity_wh")
        if self.battery_bounds is None or capacity is None or row is None:
            return None
        pct, consumed, charge = (row.get(key) for key in ("battery_remaining", "energy_consumed", "current_consumed"))
        if (type(row.get("id")) is not int or row["id"] != 0 or type(row.get("fault_bitmask")) is not int
                or row["fault_bitmask"] != 0 or type(row.get("charge_state")) is not int
                or row["charge_state"] not in (1, 2, 3)
                or type(pct) is not int or not 0 <= pct <= 100):
            return None
        voltage, soc_error, counter_error = self.battery_bounds
        # PX4 can leave energy_consumed unknown. mAh times a calibrated upper
        # pack voltage gives an upper bound on discharged Wh in that case.
        measured = {}
        if type(consumed) is int and 0 <= consumed < 2**31:
            measured["hJ"] = consumed / 36.0  # hectojoules to Wh
        if type(charge) is int and 0 < charge < 2**31:
            measured["mAh"] = charge * voltage / 1000.0
        if any(value < self.consumption_high_water.get(unit, 0.0) for unit, value in measured.items()):
            return None
        self.consumption_high_water.update(measured)
        from_soc = capacity * max(0.0, pct - soc_error) / 100.0
        budget_cap = min(capacity, self.budget["energy_wh"])
        if not measured:
            # Archived SIM publishes -1 hJ and 0 mAh. Only this exact model may
            # use the marked SOC lower bound; other absent/invalid counters fail closed.
            if (getattr(self, "assumed_sim", None) is None or type(consumed) is not int
                    or type(charge) is not int or (consumed, charge) != (-1, 0) or self.consumption_high_water):
                return None
            return min(budget_cap, from_soc)
        used = max(self.consumption_high_water.values())
        return max(0.0, min(budget_cap - used - counter_error, from_soc))

    def remaining_time_s(self, boot_ms):
        # A fresh disposable PX4 boots before mission start. Counting all of
        # its uptime conservatively charges the pre-route flight as well.
        if type(boot_ms) is not int or not 0 < boot_ms < 2**32:
            return None
        return max(0.0, self.budget["time_s"] - boot_ms / 1000.0)

    def _entries(self, index):
        """Every bound whose entry gate applies at this compiled step."""
        entries = []
        if index in self.suffixes:
            entries.append((("continue", index), self.suffixes[index]))
        if index in self.native_returns:
            entries.append((("return", index), self.native_returns[index]))
        entries += [(("hold", ref), bound) for ref, bound in sorted(self.holds.items()) if index in bound.steps]
        if self.retask_bounds is not None and index in (2, 3):
            entries.append((("retask", index), self.suffixes[index]))
        return entries

    def check_entry(self, index, point, local, *, installed_key=None):
        """Return the point if any applicable bound admits it; remember which ones do."""
        self._entry = None
        if point is None:
            return None
        try:
            _vector([local.get(key) for key in ("x", "y", "z")], 3)
            velocity = _vector([local.get(key) for key in ("vx", "vy", "vz")], 3)
        except ValueError:
            return None
        speed = math.hypot(*velocity)
        if installed_key is not None:
            if self.retask_bounds is None or speed > self.retask_bounds["max_ground_speed_mps"]:
                return None
            self._entry = point, frozenset({installed_key})
            return point
        admitted = {key for key, bound in self._entries(index)
                    if _contains(bound.entry, point) and speed <= bound.max_entry_speed_mps}
        land = self.native_landings.get(index)
        if land is not None and speed <= land.max_entry_speed_mps and self._land_pad(point, land) is not None:
            admitted.add(("land-now", index))
        if not admitted:
            return None
        self._entry = point, frozenset(admitted)
        return point

    def _entered(self, key, start):
        return self._entry is not None and self._entry[0] == start and key in self._entry[1]

    def hold_sites(self, index, vtol_state):
        """Host-defined hold options for this step and steady mode (offered to Altair by reference)."""
        return {ref: bound.site for ref, bound in sorted(self.holds.items())
                if index in bound.steps and bound.site.mode == vtol_state}

    def hold_residence_s(self, ref):
        bound = self.holds.get(ref)
        return bound.duration_s if bound is not None else None

    def _land_pad(self, point, bound):
        # A disk inside the surveyed home pad is itself surveyed landable.
        if self.pad is None:
            return None
        radius = self.pad.radius_m - math.dist(point[:2], self.pad.point[:2])
        if radius <= bound.padding_ne_m:
            return None
        return LandingPad((*point[:2], self.pad.point[2]), radius, self.pad.elevation_error_m)

    def _sweep(self, decision, frame, legs, bound, *, low=None, high=None, pad=None, **options):
        return ExecutionSweep(frame.frame_id, decision.digest, tuple(legs), min_airspeed_mps=low,
                              max_airspeed_mps=high, landing_pad=pad, **options, **bound.dynamics)

    def _airspeed_ok(self, frame, low, high):
        return low is None or (frame.airspeed_mps is not None and low <= frame.airspeed_mps <= high)

    def build_proof(self, decision, frame):
        sweep = None
        index, start = frame.active_motion_index, frame.position_ne_amsl
        action = decision.action
        steady = _SUFFIXES[index][0] if index in _SUFFIXES else None
        if action == "retask" and self.retask_bounds is not None:
            return self._retask_proof(decision, frame)
        if frame.installed_plan_sha256 is not None or (
            frame.revision > 1 and self.stage_retask is not None
        ):
            if action == "continue":
                return self._installed_continue_proof(decision, frame)
            return replace(self.builder.build(decision, frame, None), basis=self.basis)
        if action in {"continue", "return"} and start is not None:
            bound = (self.suffixes if action == "continue" else self.native_returns).get(index)
            if (bound is not None and frame.vtol_state == steady and _contains(bound.entry, start)
                    and self._entered((action, index), start)
                    and self._airspeed_ok(frame, *bound.airspeed)):
                legs = []
                for leg in bound.legs:
                    legs.append(SweptLeg(leg.phase, start, leg.end, leg.padding_ne_m, leg.padding_alt_m,
                                         leg.duration_s, leg.peak_power_w))
                    start = leg.end
                low, high = bound.airspeed
                sweep = self._sweep(decision, frame, legs, bound, low=low, high=high, pad=self.pad,
                                    failure_revalidation_enforced=bound.failure_revalidation_enforced)
        elif action == "land-now" and start is not None and self._entered(("land-now", index), start):
            bound = self.native_landings[index]
            pad = self._land_pad(start, bound)
            if pad is not None and frame.vtol_state == "mc":
                leg = SweptLeg("landing", start, pad.point, bound.padding_ne_m, bound.padding_alt_m,
                               bound.duration_s, bound.peak_power_w)
                sweep = self._sweep(decision, frame, (leg,), bound, pad=pad)
        elif action == "loiter" and start is not None:
            ref = decision.params["hold_ref"]
            bound = self.holds.get(ref)
            if (bound is not None and index in bound.steps and frame.vtol_state == bound.site.mode
                    and frame.hold_sites.get(ref) == bound.site and _contains(bound.entry, start)
                    and self._entered(("hold", ref), start) and self._airspeed_ok(frame, *bound.airspeed)):
                end = bound.centre if bound.centre is not None else start
                leg = SweptLeg(bound.site.mode, start, end, bound.padding_ne_m, bound.padding_alt_m,
                               bound.duration_s, bound.peak_power_w)
                low, high = bound.airspeed
                # The collector re-requests a decision before this residence bound ends.
                sweep = self._sweep(decision, frame, (leg,), bound, low=low, high=high,
                                    hold_revalidation_enforced=True)
        return replace(self.builder.build(decision, frame, sweep), basis=self.basis)

    def _installed_continue_proof(self, decision, frame):
        closed = replace(self.builder.build(decision, frame, None), basis=self.basis)
        index, start = frame.active_motion_index, frame.position_ne_amsl
        sha = frame.installed_plan_sha256
        if self.basis != "ASSUMED_SIM" or self.retask_bounds is None or self.installed_at is None or sha is None:
            return closed
        route = self.installed_at(index)
        if (route is None or route.plan_sha256 != sha or route.revision != frame.revision
                or not self._entered(("installed-continue", frame.revision, sha, index), start)):
            return closed
        steps = route.steps[index - route.boundary:]
        if frame.vtol_state == "mc":
            bound = self.suffixes.get(3)
        elif frame.vtol_state in {"fw", "back_transition"}:
            bound = self.suffixes.get(2)
        else:
            return closed
        if bound is None or (frame.vtol_state == "fw" and not self._airspeed_ok(frame, *bound.airspeed)):
            return closed
        try:
            legs, anchors = self._airborne_legs(start, steps, route.finding_step_ids, route.finding_error_m,
                                               active=True)
        except X1PlanError:
            return closed
        low, high = bound.airspeed
        sweep = self._sweep(decision, frame, legs, bound, low=low, high=high, pad=self.pad,
                            failure_revalidation_enforced=True)
        proof = self.builder.build(decision, frame, sweep, retask_anchors=anchors, installed_continue=True)
        return replace(proof, basis=self.basis, plan_revision=route.revision, plan_sha256=route.plan_sha256)

    def _retask_proof(self, decision, frame, *, installed_steps=None):
        closed = replace(self.builder.build(decision, frame, None), basis=self.basis)
        index, start = frame.active_motion_index, frame.position_ne_amsl
        if (self.basis != "ASSUMED_SIM" or frame.retask_boundary is not True or frame.revision != 1
                or not self._entered(("retask", index), start) or self.stage_retask is None
                or X1AirbornePlanAdapter.check_airborne_plan(decision.plan, frame) is not None):
            return closed
        bound = self.suffixes[index]
        profile = self.builder.profile
        if frame.vtol_state == "fw" and (index != 2 or not self._airspeed_ok(frame, *bound.airspeed)):
            return closed
        if frame.vtol_state == "mc" and index != 3:
            return closed
        self.builder.package.validate_plan(decision.plan)
        finding = frame.retask_finding_ne
        error = frame.retask_finding_error_m
        if finding is not None and (error is None or not 0 < error <= self.retask_bounds["finding_error_m"]):
            finding = None
        if installed_steps is None:
            try:
                steps = lower_airborne_suffix(
                    self.builder.package, decision.plan, frame, terrain=self.terrain,
                    profile=profile, limits=self.retask_bounds, finding=finding)
            except X1PlanError:
                raise
            except ValueError as exc:
                raise X1PlanError("retask_geometry_unverified", str(exc)) from exc
        else:
            # Recheck the connector to the installed commands. Re-lowering here
            # would move a waypoint whose altitude was inherited at admission.
            steps = installed_steps
            if any(step.gimbal_target_ned is not None for step in steps) and not frame.retask_gimbal_ready:
                raise X1PlanError("retask_gimbal_unavailable", "runner cannot track a point")
        finding_ids = {s.id for s in decision.plan.steps if s.params.get("target_ref") == "finding:1"}
        legs, anchors = self._airborne_legs(start, steps, finding_ids, error if finding is not None else None)
        low, high = bound.airspeed
        sweep = self._sweep(decision, frame, legs, bound, low=low, high=high, pad=self.pad,
                            failure_revalidation_enforced=True)
        proof = self.builder.build(decision, frame, sweep, retask_anchors=anchors)
        if installed_steps is None:
            self.stage_retask(decision.plan, decision.revision, steps,
                              plan_sha256=canonical_sha256(decision.params["plan"]),
                              finding_error_m=error if finding is not None else None)
        return replace(proof, basis=self.basis)

    def _airborne_legs(self, start, steps, finding_ids, error, *, active=False):
        profile = self.builder.profile
        p = profile.endurance
        high_power = max(p.hover_power_w, p.climb_power_w, p.descent_power_w)
        z_margin = self.retask_bounds["vertical_tracking_m"]
        orbit_tracking = self.retask_bounds["orbit_tracking_m"]
        home_n, home_e, home_z = self.pad.point
        point = start
        incoming = 0.0
        legs = []
        anchors = []

        def append(phase, end, width, vertical, seconds, watts):
            nonlocal point
            legs.append(SweptLeg(phase, point, end, width, vertical, seconds, watts))
            anchors.append((phase, end))
            point = end

        for i, step in enumerate(steps):
            cmd = step.command
            seconds = cmd.metadata["x1_retask_timeout_s"]
            is_finding = step.source_step_id in finding_ids
            if is_finding and (error is None or not 0 < error <= self.retask_bounds["finding_error_m"]):
                raise X1PlanError("retask_geometry_unverified", "missing bound for installed finding")
            uncertainty = error if is_finding else 0.0
            if cmd.kind == "land":
                append("landing", self.pad.point, 5, 8, seconds, high_power)
            elif cmd.kind == "transition":
                # Cover every exit heading from an orbit and the full back-transition time.
                incoming += self.retask_bounds["max_ground_speed_mps"] * seconds
                append("transition", point, incoming, z_margin, seconds, high_power)
            elif cmd.target_ned is not None:
                n, e, down = cmd.target_ned
                end = home_n + n, home_e + e, home_z - down
                if cmd.kind == "reposition":
                    length = math.dist(point[:2], end[:2])
                    if not (active and i == 0) and length < max(50.0, profile.cruise_airspeed_mps * 3):
                        raise X1PlanError("retask_fw_leg_too_short", "insufficient entry-to-waypoint distance")
                    fw_tracking = self.retask_bounds["fw_tracking_m"] + uncertainty
                    append("fw", end, max(incoming, fw_tracking), z_margin, seconds, 2 * p.loiter_power_w)
                    incoming = fw_tracking
                elif cmd.kind == "orbit":
                    # The padded connector covers the whole orbit disk at its centre,
                    # not just the chord from the current point to the centre.
                    incoming = max(incoming, cmd.radius_m + orbit_tracking + uncertainty)
                    append("fw", end, incoming, z_margin, seconds, 2 * p.loiter_power_w)
                elif cmd.kind == "mc_reposition":
                    # First half of the straight return tube conservatively retains
                    # entry uncertainty. Narrow the final segment to the held home fix.
                    if step.primitive == "mc_return_to_home" and incoming > 0:
                        length = math.dist(point[:2], end[:2])
                        final = min(45.0, length / 2)
                        f = final / length if length else 0.0
                        approach = tuple(b + f * (a - b) for a, b in zip(point, end, strict=True))
                        append("mc", approach, incoming + 15, z_margin, seconds - 18, high_power)
                        append("mc", end, incoming * f + 8, z_margin, 18, high_power)
                    else:
                        append("mc", end, max(incoming, 15), z_margin, seconds, high_power)
                    incoming = 15.0
                else:
                    raise X1PlanError("retask_geometry_unverified", "unsupported installed command")
            else:
                raise X1PlanError("retask_geometry_unverified", "missing installed command target")
        return legs, tuple(anchors)

    def reprove_installed_retask(self, decision, frame, steps):
        """Validate the installed suffix again, without staging or mutating it."""
        return self._retask_proof(decision, frame, installed_steps=steps)
