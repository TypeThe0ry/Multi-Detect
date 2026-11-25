"""Build X1 continue proofs from scoped SITL evidence and one fresh PX4 frame."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass

from multidetect.autonomy.mission_package import number
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.x1_motion_proof import ExecutionSweep, LandingPad, SweptLeg, X1MotionProofBuilder

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


class LiveContinueEvidence:
    """The package supplies surveyed geometry and certified bounds for this lowering.

    A suffix bound covers the actual execution from any eligible entry sample,
    including tracking error and branches up to touchdown. Telemetry only gates
    the current entry and budgets. It cannot certify future aircraft dynamics.
    """

    def __init__(self, package, profile, terrain, steps, *, scope):
        self.budget = package.document["budget"]
        self.suffixes = {}
        self.battery_bounds = None
        self.consumption_high_water = {}
        terrain_error, obstacle_clearance, self.pad = None, None, None
        config = package.document.get("x1_live_evidence")
        if config is not None:
            try:
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
                self.pad, self.battery_bounds = landing_pad, battery_bounds
            except ValueError:
                terrain_error, obstacle_clearance = None, None
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
        if not measured or any(value < self.consumption_high_water.get(unit, 0.0)
                               for unit, value in measured.items()):
            return None
        self.consumption_high_water.update(measured)
        used = max(self.consumption_high_water.values())
        return max(0.0, min(min(capacity, self.budget["energy_wh"]) - used - counter_error,
                            capacity * max(0.0, pct - soc_error) / 100.0))

    def remaining_time_s(self, boot_ms):
        # A fresh disposable PX4 boots before mission start. Counting all of
        # its uptime conservatively charges the pre-route flight as well.
        if type(boot_ms) is not int or not 0 < boot_ms < 2**32:
            return None
        return max(0.0, self.budget["time_s"] - boot_ms / 1000.0)

    def check_entry(self, index, point, local):
        bound = self.suffixes.get(index)
        if bound is None or point is None or not _contains(bound.entry, point):
            return None
        try:
            _vector([local.get(key) for key in ("x", "y", "z")], 3)
            velocity = _vector([local.get(key) for key in ("vx", "vy", "vz")], 3)
        except ValueError:
            return None
        return point if math.hypot(*velocity) <= bound.max_entry_speed_mps else None

    def build_proof(self, decision, frame):
        sweep = None
        index, start = frame.active_motion_index, frame.position_ne_amsl
        bound = self.suffixes.get(index)
        if (decision.action == "continue" and bound is not None and frame.vtol_state == _SUFFIXES[index][0]
                and start is not None and _contains(bound.entry, start)):
            low, high = bound.airspeed
            if low is None or (frame.airspeed_mps is not None and low <= frame.airspeed_mps <= high):
                legs = []
                for leg in bound.legs:
                    legs.append(SweptLeg(leg.phase, start, leg.end, leg.padding_ne_m, leg.padding_alt_m,
                                         leg.duration_s, leg.peak_power_w))
                    start = leg.end
                sweep = ExecutionSweep(frame.frame_id, decision.digest, tuple(legs),
                                       min_airspeed_mps=low, max_airspeed_mps=high, landing_pad=self.pad,
                                       failure_revalidation_enforced=bound.failure_revalidation_enforced,
                                       **bound.dynamics)
        return self.builder.build(decision, frame, sweep)
