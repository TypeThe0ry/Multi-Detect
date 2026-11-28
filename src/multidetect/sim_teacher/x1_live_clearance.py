"""Build X1 continue proofs from scoped SITL evidence and one fresh PX4 frame."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from multidetect.autonomy.mission_package import number
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_contingency_executor import HoldSite, X1AirbornePlanAdapter
from multidetect.autonomy.x1_corridor import (
    CORRIDOR_TUBE_HALF_WIDTH_M,
    X1CorridorLowering,
    slice_leg,
)
from multidetect.autonomy.x1_mission import MC_RETURN_SPEED_MPS, X1PlanError, lower_airborne_suffix
from multidetect.autonomy.x1_motion_proof import ExecutionSweep, LandingPad, SweptLeg, X1MotionProofBuilder
from multidetect.autonomy.x1_contingency_state import require_token
from multidetect.autonomy.x1_wind_estimate import WindReading
from multidetect.target_geolocation import wgs84_forward_from_ned

from .x1_fw_native_rtl import FwRtlBounds

MOTION_SAMPLE_MAX_AGE_MS = 500
_CORRIDOR_WIND_MAX_AGE_MS = 1500
_CORRIDOR_FW_GROUND_SPEED_FLOOR_MPS = 6.0
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


def _segment_distance(start, end, point):
    """Distance from a 2-D point to a closed segment (same as x1_plan's helper)."""
    dn, de = end[0] - start[0], end[1] - start[1]
    length2 = dn * dn + de * de
    t = 0.0 if not length2 else min(1.0, max(
        0.0, ((point[0] - start[0]) * dn + (point[1] - start[1]) * de) / length2))
    return math.dist(point, (start[0] + t * dn, start[1] + t * de))


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
# The separate version-bound FW proof does not use these MC suffixes.
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


def _hold(raw, terrain, *, corridor_fw_legs=None):
    mode = raw.get("mode") if isinstance(raw, Mapping) else None
    keys = ["mode", "steps", "entry", "max_entry_speed_mps", "padding_ne_m", "padding_alt_m", "duration_s",
            "peak_power_w", "dynamics"]
    if mode == "fw":
        keys += ["centre", "radius_m", "tangential_speed_mps", "airspeed"]
    elif mode != "mc":
        raise ValueError("hold mode must be mc or fw")
    values = dict(zip(keys, _fields(raw, *keys), strict=True))
    steps = values["steps"]
    allowed = {1, 2, 3}
    if corridor_fw_legs is not None:
        if not isinstance(steps, list) or any(type(step) is not int for step in steps):
            raise ValueError("invalid corridor hold step indices")
        if mode == "fw":
            allowed = set(range(1, corridor_fw_legs + 1))
        else:
            # The existing reviewed template labels its terminal MC slot 3.
            # Map this semantic slot only after the full corridor is lowered.
            allowed = {corridor_fw_legs + 2}
            if steps == [3]:
                steps = [corridor_fw_legs + 2]
    if not isinstance(steps, list) or not steps or not set(steps) <= allowed or len(set(steps)) != len(steps):
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


def parse_native(raw, terrain, *, corridor_fw_legs=None):
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
    if corridor_fw_legs is None:
        # Single-waypoint path is left exactly as it was before the corridor
        # step: manifest keys are used verbatim and holds are still 1..3.
        return ({int(k): _suffix(v, int(k)) for k, v in returns.items()},
                {int(k): _land_now(v) for k, v in landings.items()},
                {require_token(ref): _hold(v, terrain) for ref, v in holds.items()})
    if any(not isinstance(ref, str) or not ref.startswith("hold:") for ref in holds):
        raise ValueError("corridor Hold sites must use hold: references")
    # Corridor recovery is only ever bounded at the terminal MC return slot.
    # The reviewed template keeps the single-waypoint label "3"; remap it here
    # once the actual FW leg count is known.
    mc_index = corridor_fw_legs + 2
    return ({mc_index: _suffix(v, 3) for v in returns.values()},
            {mc_index: _land_now(v) for v in landings.values()},
            {require_token(ref): _hold(v, terrain, corridor_fw_legs=corridor_fw_legs)
             for ref, v in holds.items()})


class LiveContinueEvidence:
    """The package supplies surveyed geometry and certified bounds for this lowering.

    A suffix bound covers the actual execution from any eligible entry sample,
    including tracking error and branches up to touchdown. Telemetry only gates
    the current entry and budgets. It cannot certify future aircraft dynamics.
    """

    def __init__(self, package, profile, terrain, steps, *, scope, assumed_sim=None,
                 corridor: X1CorridorLowering | None = None):
        self.assumed_sim = assumed_sim
        self.corridor = corridor
        self._corridor_rtl = None
        self._corridor_hold_exits = {}
        if corridor is not None:
            self._corridor_wind = None
        self.retask_bounds = (assumed_sim or {}).get("airborne_retask")
        self.stage_retask = None
        self.installed_at = None
        self.budget = package.document["budget"]
        self.suffixes = {}
        self.native_returns, self.native_landings, self.holds = {}, {}, {}
        self._entry = None
        self._fw_rtl_entry = None
        self.fw_rtl = None
        self.fw_rtl_ready = False
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
                    options = {"corridor_fw_legs": len(corridor.legs)} if corridor is not None else {}
                    self.native_returns, self.native_landings, self.holds = parse_native(native, terrain, **options)
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
            corridor=corridor,
        )
        fw_rtl = (assumed_sim or {}).get("fw_rtl")
        if (self.pad is not None and package.mode == "sim" and self.basis == "ASSUMED_SIM"
                and isinstance(fw_rtl, FwRtlBounds) and fw_rtl.image_id != assumed_sim.get("image_id")
                and fw_rtl.valid_for(scope, profile, corridor=corridor is not None)):
            if corridor is None:
                self.fw_rtl = fw_rtl
            elif fw_rtl.reviewed_corridor(assumed_sim.get("fw_rtl_review"), assumed_sha256=assumed_sim["sha256"]):
                params = fw_rtl.params
                if (fw_rtl.corridor_ready(min_cruise_rel_home_m=self.builder.corridor_min_cruise_rel_home_m)
                        and self.builder.corridor_rtl_ready(
                            return_alt_m=params["RTL_RETURN_ALT"], descend_alt_m=params["RTL_DESCEND_ALT"],
                            loiter_radius_m=params["RTL_LOITER_RAD"] + fw_rtl.fw_tracking_m + params["NAV_ACC_RAD"],
                            pad=self.pad)):
                    self.fw_rtl = self._corridor_rtl = fw_rtl
                    for ref, hold in self.holds.items():
                        for index in sorted(hold.steps):
                            exit_box = self._corridor_hold_exit(hold, index)
                            if exit_box is not None:
                                self._corridor_hold_exits[index, ref] = exit_box

    def _corridor_rtl_available(self):
        return self._corridor_rtl is not None and self.fw_rtl_ready is True

    def _corridor_hold_exit(self, bound, index):
        if bound.site.mode != "fw":
            return None
        rtl = self._corridor_rtl
        speed = max(bound.site.tangential_speed_mps, bound.airspeed[1])
        bank = min(bound.dynamics["max_bank_deg"], rtl.params["FW_R_LIM"])
        if not 0 < bank < 90 or bound.site.radius_m < speed ** 2 / (9.80665 * math.tan(math.radians(bank))):
            return None
        if (bound.site.radius_m > rtl.params["NAV_LOITER_RAD"]
                or bound.padding_ne_m < bound.site.radius_m + rtl.fw_tracking_m):
            return None
        return self.builder.corridor_hold_exit(
            bound, index, min_return_alt_m=self.pad.point[2] + rtl.params["RTL_RETURN_ALT"]
            + self.pad.elevation_error_m, tracking_m=rtl.fw_tracking_m)

    def _corridor_hold_clear(self, ref, index):
        return self._corridor_rtl_available() and (index, ref) in self._corridor_hold_exits

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

    def _corridor_airframe_bound(self):
        """Return the scoped FW phase bounds used with freshly rebuilt geometry."""
        if self.corridor is None:
            return None
        return self.suffixes.get(1)

    def _corridor_retask_bound(self, index):
        if self.retask_bounds is not None and type(index) is int:
            phase_index = index - len(self.corridor.legs)
            if phase_index in (1, 2):
                return self.suffixes.get(phase_index + 1)
        return None

    def _corridor_admits(self, index, point, speed):
        """Entry gate for a corridor continue tick from this live position."""
        if self.corridor is None or type(index) is not int or point is None:
            return False
        home = self.builder.home_ne
        legs = self.corridor.legs
        n = len(legs)
        bound = self.suffixes.get(3) if index == n + 2 else self._corridor_airframe_bound()
        if bound is None or speed > bound.max_entry_speed_mps:
            return False
        if 1 <= index <= n:
            leg = legs[index - 1]
            return _segment_distance(leg.start_ne, leg.end_ne, point[:2]) <= leg.tube_half_width_m + 1e-9
        if index == n + 1:
            return math.dist(point[:2], legs[-1].end_ne) <= (
                self.corridor.runout_m + CORRIDOR_TUBE_HALF_WIDTH_M + 1e-9)
        if index == n + 2:
            return math.dist(point[:2], home) <= (
                self.corridor.runout_m + CORRIDOR_TUBE_HALF_WIDTH_M + 1e-9)
        return False

    def _corridor_runout_end(self):
        """Final back-transition/overshoot endpoint used by the corridor union."""
        corridor = self.corridor
        last = corridor.legs[-1]
        home = self.builder.home_ne
        if corridor.gate_ne is not None:
            anchor = corridor.gate_ne
            distance = math.dist(anchor, home)
            ux = (home[0] - anchor[0]) / distance if distance else 0.0
            uy = (home[1] - anchor[1]) / distance if distance else 0.0
            length = max(distance, corridor.runout_m)
            return (anchor[0] + ux * length, anchor[1] + uy * length, corridor.cruise_msl)
        length = math.dist(last.start_ne, last.end_ne)
        ux = (last.end_ne[0] - last.start_ne[0]) / length if length else 0.0
        uy = (last.end_ne[1] - last.start_ne[1]) / length if length else 0.0
        return (last.end_ne[0] + ux * corridor.runout_m,
                last.end_ne[1] + uy * corridor.runout_m, last.cruise_msl)

    def _installed_key(self, index):
        route = self.installed_at(index) if self.installed_at is not None else None
        return ("installed-continue", route.revision, route.plan_sha256, index) if route is not None else None

    @staticmethod
    def _hold_key(ref, installed_key):
        return ("installed-hold", installed_key, ref) if installed_key is not None else ("hold", ref)

    def check_entry(self, index, point, local, *, installed_key=None, confirmed_hold_ref=None, observed_home=None,
                    corridor_wind=None):
        """Return the point if any applicable bound admits it; remember which ones do."""
        self._entry = None
        self._fw_rtl_entry = None
        if self.corridor is not None:
            self._corridor_wind = corridor_wind
        if point is None:
            return None
        try:
            _vector([local.get(key) for key in ("x", "y", "z")], 3)
            velocity = _vector([local.get(key) for key in ("vx", "vy", "vz")], 3)
        except ValueError:
            return None
        speed = math.hypot(*velocity)
        fw_ready = self.fw_rtl_ready is True and self.fw_rtl is not None
        if installed_key is not None:
            if (self.retask_bounds is None or speed > self.retask_bounds["max_ground_speed_mps"]
                    or installed_key != self._installed_key(index)):
                return None
            admitted = {installed_key}
            if fw_ready and self.corridor is None:
                admitted.update(self._hold_key(ref, installed_key) for ref, bound in self.holds.items()
                                if bound.site.mode == "fw" and _contains(bound.entry, point)
                                and speed <= bound.max_entry_speed_mps)
        elif self.corridor is not None:
            admitted = set()
            if self._corridor_admits(index, point, speed):
                admitted.add(("continue", index))
                bound = self._corridor_retask_bound(index)
                if (bound is not None and _contains(bound.entry, point)
                        and speed <= min(bound.max_entry_speed_mps, self.retask_bounds["max_ground_speed_mps"])):
                    admitted.add(("retask", index))
            if type(index) is int:
                n = len(self.corridor.legs)
                admitted.update(
                    ("hold", ref) for ref, bound in self.holds.items()
                    if index in bound.steps and _contains(bound.entry, point) and speed <= bound.max_entry_speed_mps
                    and ((index == n + 2 and bound.site.mode == "mc")
                         or (1 <= index <= n and self._corridor_hold_clear(ref, index))))
                if index == n + 2:
                    land = self.native_landings.get(index)
                    if land and speed <= land.max_entry_speed_mps and self._land_pad(point, land) is not None:
                        admitted.add(("land-now", index))
                    rtl = self.native_returns.get(index)
                    if (self._corridor_rtl_available() and rtl and _contains(rtl.entry, point)
                            and speed <= rtl.max_entry_speed_mps):
                        admitted.add(("return", index))
        else:
            admitted = {key for key, bound in self._entries(index)
                        if _contains(bound.entry, point) and speed <= bound.max_entry_speed_mps}
            land = self.native_landings.get(index)
            if land is not None and speed <= land.max_entry_speed_mps and self._land_pad(point, land) is not None:
                admitted.add(("land-now", index))
        hold = self.holds.get(confirmed_hold_ref)
        if (self.corridor is None and fw_ready and hold is not None and hold.site.mode == "fw"
                and observed_home is not None
                and (installed_key is not None or index in hold.steps)
                and speed <= min(hold.max_entry_speed_mps, self.fw_rtl.max_ground_speed_mps)):
            # Once the orbit is confirmed, the RTL sweep starts at the live
            # fix, which need not still be inside the original Hold entry box.
            self._fw_rtl_entry = point, index, installed_key, hold, observed_home
        elif (self.corridor is not None and installed_key is None and self._corridor_rtl_available()
                and hold is not None and hold.site.mode == "fw" and observed_home is not None
                and type(index) is int and 1 <= index <= len(self.corridor.legs)
                and index in hold.steps and self._corridor_hold_clear(confirmed_hold_ref, index)
                and speed <= min(hold.max_entry_speed_mps, self.fw_rtl.max_ground_speed_mps)
                and self.builder.corridor_rtl_clear(index, point, hold_exit=
                    self._corridor_hold_exits.get((index, confirmed_hold_ref)))):
            # The precomputed chord from this slice start (not just the leg
            # tube) must be fence/terrain/obstacle clear before offering return.
            self._fw_rtl_entry = point, index, installed_key, hold, observed_home, confirmed_hold_ref
            admitted.add(("return", index))
        if not admitted and self._fw_rtl_entry is None:
            return None
        self._entry = point, frozenset(admitted)
        return point

    def _entered(self, key, start):
        return self._entry is not None and self._entry[0] == start and key in self._entry[1]

    def hold_sites(self, index, vtol_state, *, installed_key=None, point=None):
        """Host-defined hold options for this step and steady mode (offered to Altair by reference)."""
        if self.corridor is not None:
            # Corridor Holds are only offered once a reviewed v2 corridor RTL
            # manifest is installed and read back; each disk must fit the fence.
            if installed_key is not None or type(index) is not int:
                return {}
            n = len(self.corridor.legs)
            return {ref: bound.site for ref, bound in sorted(self.holds.items())
                    if vtol_state == bound.site.mode and index in bound.steps
                    and ((index == n + 2 and vtol_state == "mc")
                         or (1 <= index <= n and vtol_state == "fw" and self._corridor_hold_clear(ref, index)))}
        if installed_key is not None:
            return {ref: bound.site for ref, bound in sorted(self.holds.items())
                    if vtol_state == bound.site.mode == "fw"
                    and self._entered(self._hold_key(ref, installed_key), point)}
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
        if self.corridor is not None:
            return self._corridor_proof(decision, frame)
        if action == "retask" and self.retask_bounds is not None:
            return self._retask_proof(decision, frame)
        installed = frame.installed_plan_sha256 is not None or (frame.revision > 1 and self.stage_retask is not None)
        installed_key = None
        if installed:
            if action == "continue":
                return self._installed_continue_proof(decision, frame)
            if frame.installed_plan_sha256 is not None:
                installed_key = ("installed-continue", frame.revision, frame.installed_plan_sha256, index)
            if installed_key is None or installed_key != self._installed_key(index):
                return replace(self.builder.build(decision, frame, None), basis=self.basis)
        if action == "return" and frame.vtol_state == "fw":
            entry = self._fw_rtl_entry
            if (entry is not None and start is not None and frame.native_hold_active is True
                    and entry[:3] == (start, index, installed_key) and self.fw_rtl_ready is True
                    and self._airspeed_ok(frame, *entry[3].airspeed)):
                sweep = self.fw_rtl.sweep(decision, frame, entry[3], entry[4], self.pad, self.builder.profile)
            return replace(self.builder.build(decision, frame, sweep, native_fw_rtl=True), basis=self.basis)
        if installed and not (action == "loiter" and frame.vtol_state == "fw"):
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
        elif action == "loiter" and start is not None and not (frame.vtol_state == "fw" and frame.native_hold_active):
            ref = decision.params["hold_ref"]
            bound = self.holds.get(ref)
            if (bound is not None and frame.vtol_state == bound.site.mode
                    and frame.hold_sites.get(ref) == bound.site and _contains(bound.entry, start)
                    and self._entered(self._hold_key(ref, installed_key), start)
                    and self._airspeed_ok(frame, *bound.airspeed)):
                end = bound.centre if bound.centre is not None else start
                leg = SweptLeg(bound.site.mode, start, end, bound.padding_ne_m, bound.padding_alt_m,
                               bound.duration_s, bound.peak_power_w)
                low, high = bound.airspeed
                # The collector re-requests a decision before this residence bound ends.
                sweep = self._sweep(decision, frame, (leg,), bound, low=low, high=high,
                                    hold_revalidation_enforced=True)
        return replace(self.builder.build(decision, frame, sweep), basis=self.basis)

    def _corridor_proof(self, decision, frame):
        """Build command-bound evidence; recovery stays closed without a v2 manifest."""
        if decision.action == "retask" and self.retask_bounds is not None:
            return self._retask_proof(decision, frame)
        closed = replace(self.builder.build(decision, frame, None), basis=self.basis)
        if self.basis != "ASSUMED_SIM":
            return closed
        if decision.action in {"loiter", "return", "land-now"} and frame.vtol_state == "mc":
            return self._corridor_mc_proof(decision, frame)
        if decision.action == "loiter":
            return self._corridor_hold_proof(decision, frame)
        if decision.action == "return" and frame.vtol_state == "fw":
            return self._corridor_return_proof(decision, frame)
        if decision.action != "continue":
            return closed
        installed = (frame.installed_plan_sha256 is not None
                     or (frame.revision > 1 and self.stage_retask is not None))
        if installed:
            return self._installed_continue_proof(decision, frame)
        index, start = frame.active_motion_index, frame.position_ne_amsl
        if start is None or not self._entered(("continue", index), start):
            return closed
        sweep = self._corridor_continue_sweep(decision, frame, index, start)
        if sweep is None:
            return closed
        return replace(self.builder.build(decision, frame, sweep), basis=self.basis)

    def _corridor_mc_proof(self, decision, frame):
        index, start = frame.active_motion_index, frame.position_ne_amsl
        sweep = None
        if (type(index) is int and index == len(self.corridor.legs) + 2 and start is not None
                and frame.installed_plan_sha256 is None):
            action = decision.action
            if action == "land-now" and self._entered((action, index), start):
                bound = self.native_landings[index]
                pad = self._land_pad(start, bound)
                if pad is not None:
                    leg = SweptLeg("landing", start, pad.point, bound.padding_ne_m, bound.padding_alt_m,
                                   bound.duration_s, bound.peak_power_w)
                    sweep = self._sweep(decision, frame, (leg,), bound, pad=pad)
            elif action == "return" and self._corridor_rtl_available() and self._entered((action, index), start):
                bound = self.native_returns[index]
                if self._airspeed_ok(frame, *bound.airspeed):
                    legs = []
                    for leg in bound.legs:
                        legs.append(SweptLeg(leg.phase, start, leg.end, leg.padding_ne_m, leg.padding_alt_m,
                                             leg.duration_s, leg.peak_power_w))
                        start = leg.end
                    sweep = self._sweep(decision, frame, legs, bound, pad=self.pad,
                                        low=bound.airspeed[0], high=bound.airspeed[1])
            elif action == "loiter" and not frame.native_hold_active:
                ref = decision.params.get("hold_ref")
                bound = self.holds.get(ref)
                if (bound is not None and bound.site.mode == "mc" and frame.hold_sites.get(ref) == bound.site
                        and self._entered(self._hold_key(ref, None), start)):
                    leg = SweptLeg("mc", start, start, bound.padding_ne_m, bound.padding_alt_m,
                                   bound.duration_s, bound.peak_power_w)
                    sweep = self._sweep(decision, frame, (leg,), bound, hold_revalidation_enforced=True)
        return replace(self.builder.build(decision, frame, sweep), basis=self.basis)

    @staticmethod
    def _combined_hold_sweep(first, second, legs, *, hold_revalidation_enforced):
        """Report both phases' greatest dynamic demands and their common entry airspeed."""
        def low(a, b):
            return None if a is None or b is None else max(a, b)

        def high(a, b):
            return None if a is None or b is None else min(a, b)

        def cap(a, b):
            return max(a, b) if a is not None and b is not None else (a if b is None else b)

        return ExecutionSweep(
            first.frame_id, first.decision_digest, tuple(legs),
            max_bank_deg=cap(first.max_bank_deg, second.max_bank_deg),
            max_climb_mps=cap(first.max_climb_mps, second.max_climb_mps),
            max_descent_mps=cap(first.max_descent_mps, second.max_descent_mps),
            max_acceleration_mps2=cap(first.max_acceleration_mps2, second.max_acceleration_mps2),
            max_angular_rate_dps=cap(first.max_angular_rate_dps, second.max_angular_rate_dps),
            min_airspeed_mps=low(first.min_airspeed_mps, second.min_airspeed_mps),
            max_airspeed_mps=high(first.max_airspeed_mps, second.max_airspeed_mps),
            landing_pad=second.landing_pad,
            hold_revalidation_enforced=hold_revalidation_enforced,
            failure_revalidation_enforced=False,
        )

    def _corridor_native_sweep(self, decision, frame, *, start, orbit, observed_home, hold_vertical_m=None):
        """One reviewed v2 native RTL sweep from a live fix or Hold centre."""
        if self._corridor_rtl is None or self.pad is None:
            return None
        if frame.vtol_state != "fw" or not self._airspeed_ok(
                frame, self.builder.profile.min_airspeed_mps, self.builder.profile.max_airspeed_mps):
            return None
        bounds = self.builder.package.assumed_sim_corridor
        rtl = self._corridor_rtl
        wind = self._corridor_wind
        # The review covers the scoped fault schedule. Fresh telemetry outside
        # its horizontal wind bound invalidates that proof for any heading.
        if (isinstance(wind, WindReading) and 0 <= wind.age_ms(frame.t_ms) <= _CORRIDOR_WIND_MAX_AGE_MS
                and math.hypot(*wind.ned_mps[:2]) + bounds.wind_sigma_mps
                > rtl.params["FW_AIRSPD_TRIM"] - rtl.connector_ground_speed_min_mps):
            return None
        return rtl.corridor_sweep(
            decision, frame, start=start, orbit=orbit, observed_home=observed_home,
            pad=self.pad, profile=self.builder.profile,
            fw_power_floor_w=bounds.fw_leg_peak_power_w, hold_vertical_m=hold_vertical_m)

    def _corridor_hold_proof(self, decision, frame):
        closed = replace(self.builder.build(decision, frame, None), basis=self.basis)
        index, start = frame.active_motion_index, frame.position_ne_amsl
        if (not self._corridor_rtl_available() or start is None or frame.vtol_state != "fw"
                or frame.native_hold_active):
            return closed
        ref = decision.params.get("hold_ref")
        bound = self.holds.get(ref)
        if (bound is None or bound.site.mode != "fw" or bound.centre is None
                or type(index) is not int or index not in bound.steps or frame.installed_plan_sha256 is not None
                or frame.hold_sites.get(ref) != bound.site
                or not _contains(bound.entry, start)
                or not self._entered(self._hold_key(ref, None), start)
                or not self._airspeed_ok(frame, *bound.airspeed)
                or not self._corridor_hold_clear(ref, index)):
            return closed
        hold_leg = SweptLeg("fw", start, bound.centre, bound.padding_ne_m, bound.padding_alt_m,
                            bound.duration_s, max(bound.peak_power_w, self.builder.package.assumed_sim_corridor.fw_leg_peak_power_w))
        native = self._corridor_native_sweep(
            decision, frame, start=bound.centre, orbit=bound.centre, observed_home=self.pad.point,
            hold_vertical_m=bound.padding_alt_m)
        if native is None:
            return closed
        hold_sweep = self._sweep(decision, frame, (hold_leg,), bound, low=bound.airspeed[0],
                                 high=bound.airspeed[1], pad=self.pad, hold_revalidation_enforced=True)
        combined = self._combined_hold_sweep(
            hold_sweep, native, (*hold_sweep.legs, *native.legs),
            hold_revalidation_enforced=True)
        return replace(self.builder.build(decision, frame, combined), basis=self.basis)

    def _corridor_return_proof(self, decision, frame):
        closed = replace(self.builder.build(decision, frame, None), basis=self.basis)
        index, start = frame.active_motion_index, frame.position_ne_amsl
        entry = self._fw_rtl_entry
        if (not self._corridor_rtl_available() or entry is None or start is None
                or frame.native_hold_active is not True):
            return closed
        entry_point, entry_index, entry_key, hold, observed_home, ref = entry
        if (entry_point != start or entry_index != index or entry_key is not None
                or hold.site.mode != "fw" or hold.centre is None or frame.installed_plan_sha256 is not None
                or math.dist(start[:2], hold.centre[:2]) > hold.padding_ne_m
                or abs(start[2] - hold.centre[2]) > hold.padding_alt_m
                or not self._airspeed_ok(frame, *hold.airspeed)
                or not self.builder.corridor_rtl_clear(index, start, hold_exit=
                    self._corridor_hold_exits.get((index, ref)))):
            return closed
        sweep = self._corridor_native_sweep(
            decision, frame, start=start, orbit=hold.centre, observed_home=observed_home)
        if sweep is None:
            return closed
        return replace(self.builder.build(decision, frame, sweep, native_fw_rtl=True), basis=self.basis)

    def _corridor_continue_sweep(self, decision, frame, index, start):
        """Rebuild the remaining corridor from this live fix using slice_leg."""
        corridor = self.corridor
        bounds = self.builder.package.assumed_sim_corridor
        if (corridor is None or bounds is None or type(index) is not int
                or not isinstance(start, tuple) or len(start) != 3):
            return None
        legs = corridor.legs
        n = len(legs)
        if frame.vtol_state == "fw":
            if not 1 <= index <= n + 1:
                return None
        elif frame.vtol_state != "mc" or index != n + 2:
            return None
        bound = self._corridor_airframe_bound()
        if bound is None or (frame.vtol_state == "fw" and not self._airspeed_ok(frame, *bound.airspeed)):
            return None
        profile = self.builder.profile
        power = profile.endurance
        high_power = max(power.hover_power_w, power.climb_power_w, power.descent_power_w)
        phase_bounds = {leg.phase: leg for leg in reversed(bound.legs)}
        if frame.vtol_state == "mc":
            bound = self.suffixes.get(3)
            if bound is None:
                return None
            phase_bounds.update({leg.phase: leg for leg in reversed(bound.legs)})
        landing = phase_bounds["landing"]
        point = start
        sweep_legs = []

        def append_slices(phase, end, pad_ne, pad_alt, seconds, watts):
            nonlocal point
            slices = slice_leg(point, end, pad_ne, pad_alt, seconds, watts, bounds.slice_m,
                               max_slices=corridor.max_sweep_legs)
            sweep_legs.extend(SweptLeg(phase, item.start, item.end, item.padding_ne_m,
                                       item.padding_alt_m, item.duration_s, item.peak_power_w,
                                       item.command_end) for item in slices)
            point = end

        try:
            if 1 <= index <= n:
                fw = phase_bounds["fw"]
                for leg in legs[index - 1:]:
                    duration = self._corridor_fw_duration(frame, point, leg.end_amsl)
                    append_slices("fw", leg.end_amsl, max(bounds.fw_tracking_m, fw.padding_ne_m),
                                  fw.padding_alt_m, max(leg.duration_bound_s, duration),
                                  max(bounds.fw_leg_peak_power_w, leg.peak_power_w, fw.peak_power_w))
            if index <= n + 1:
                transition = phase_bounds["transition"]
                append_slices("transition", self._corridor_runout_end(),
                              max(bounds.fw_tracking_m, transition.padding_ne_m), transition.padding_alt_m,
                              max(profile.transition.timeout_s, transition.duration_s),
                              max(high_power, transition.peak_power_w))
            mc = phase_bounds["mc"]
            mc_end = (*self.builder.home_ne, corridor.cruise_msl)
            mc_distance = math.dist(point[:2], mc_end[:2])
            mc_seconds = max((self.retask_bounds or {}).get("mc_s", 0.0),
                             18.0 + mc_distance / (MC_RETURN_SPEED_MPS - profile.endurance.wind_margin_mps))
            append_slices("mc", mc_end, max(bounds.fw_tracking_m, mc.padding_ne_m),
                          mc.padding_alt_m, mc_seconds, max(high_power, bounds.mc_peak_power_w, mc.peak_power_w))
        except X1PlanError:
            return None
        if self.pad is None:
            return None
        landing_s = max(landing.duration_s, max(0.0, corridor.cruise_msl - self.pad.point[2])
                        / profile.max_descent_rate_mps)
        sweep_legs.append(SweptLeg("landing", point, self.pad.point, landing.padding_ne_m,
                                   landing.padding_alt_m, landing_s, max(high_power, landing.peak_power_w)))
        low, high = bound.airspeed
        return self._sweep(decision, frame, tuple(sweep_legs), bound, low=low, high=high,
                           pad=self.pad, failure_revalidation_enforced=bound.failure_revalidation_enforced)

    def _corridor_fw_duration(self, frame, start, end):
        """Charge each heading its own headwind, never crediting a tailwind."""
        profile = self.builder.profile
        bounds = self.builder.package.assumed_sim_corridor
        headwind = profile.endurance.wind_margin_mps
        distance = math.dist(start[:2], end[:2])
        wind = self._corridor_wind
        if (distance > 0 and isinstance(wind, WindReading)
                and 0 <= wind.age_ms(frame.t_ms) <= _CORRIDOR_WIND_MAX_AGE_MS):
            projected = sum(w * (a - b) / distance for w, a, b in
                            zip(wind.ned_mps[:2], start[:2], end[:2], strict=True))
            headwind = max(headwind, projected + bounds.wind_sigma_mps)
        speed = min(bounds.min_ground_speed_mps, profile.cruise_airspeed_mps - headwind)
        if speed < _CORRIDOR_FW_GROUND_SPEED_FLOOR_MPS:
            raise X1PlanError("WIND_EXCEEDS_RETURN", "FW corridor ground speed below proof bound")
        return distance / speed

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
        if self.corridor is not None:
            bound = self.suffixes.get(3 if frame.vtol_state == "mc" else 1)
        elif frame.vtol_state == "mc":
            bound = self.suffixes.get(3)
        elif frame.vtol_state in {"fw", "back_transition"}:
            bound = self.suffixes.get(2)
        else:
            return closed
        if bound is None or (frame.vtol_state == "fw" and not self._airspeed_ok(frame, *bound.airspeed)):
            return closed
        try:
            legs, anchors = self._airborne_legs(start, steps, route.finding_step_ids, route.finding_error_m,
                                               active=True, **({"frame": frame} if self.corridor is not None else {}))
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
                or not self._entered(("retask", index), start) or self.stage_retask is None):
            return closed
        profile = self.builder.profile
        if self.corridor is not None:
            n = len(self.corridor.legs)
            bound = self._corridor_retask_bound(index)
            if (bound is None or (frame.vtol_state, index) not in {("fw", n + 1), ("mc", n + 2)}
                    or X1AirbornePlanAdapter.check_airborne_plan(
                        decision.plan, frame, package=self.builder.package) is not None
                    or (frame.vtol_state == "fw" and not self._airspeed_ok(frame, *bound.airspeed))):
                return closed
        else:
            if X1AirbornePlanAdapter.check_airborne_plan(decision.plan, frame) is not None:
                return closed
            bound = self.suffixes[index]
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
        legs, anchors = self._airborne_legs(start, steps, finding_ids, error if finding is not None else None,
                                            **({"frame": frame} if self.corridor is not None else {}))
        low, high = bound.airspeed
        sweep = self._sweep(decision, frame, legs, bound, low=low, high=high, pad=self.pad,
                            failure_revalidation_enforced=True)
        proof = self.builder.build(decision, frame, sweep, retask_anchors=anchors)
        if installed_steps is None:
            self.stage_retask(decision.plan, decision.revision, steps,
                              plan_sha256=canonical_sha256(decision.params["plan"]),
                              finding_error_m=error if finding is not None else None)
        return replace(proof, basis=self.basis)

    def _airborne_legs(self, start, steps, finding_ids, error, *, active=False, frame=None):
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

        def append_fw_sliced(end, width, vertical, seconds, watts):
            # Keep the conservative lateral width, but split the along-track box
            # so a multi-kilometre FW command does not claim unrelated terrain.
            nonlocal point
            bounds = self.builder.package.assumed_sim_corridor
            # The original-corridor tube does not constrain an installed retask.
            # Use the slicing length bound and preserve the full installed padding.
            seconds = max(seconds, self._corridor_fw_duration(frame, point, end))
            watts = max(watts, bounds.fw_leg_peak_power_w)
            slices = slice_leg(point, end, 0.0, vertical, seconds, watts, bounds.slice_m,
                               max_slices=self.corridor.max_sweep_legs)
            for item in slices:
                legs.append(SweptLeg(item.phase, item.start, item.end, width, vertical,
                                     item.duration_s, item.peak_power_w, item.command_end))
            anchors.append(("fw", end))
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
                    width = max(incoming, fw_tracking)
                    if self.corridor is not None:
                        append_fw_sliced(end, width, z_margin, seconds, 2 * p.loiter_power_w)
                    else:
                        append("fw", end, width, z_margin, seconds, 2 * p.loiter_power_w)
                    incoming = fw_tracking
                elif cmd.kind == "orbit":
                    # The padded connector covers the whole orbit disk at its centre,
                    # not just the chord from the current point to the centre.
                    incoming = max(incoming, cmd.radius_m + orbit_tracking + uncertainty)
                    if self.corridor is not None:
                        append_fw_sliced(end, incoming, z_margin, seconds, 2 * p.loiter_power_w)
                    else:
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
