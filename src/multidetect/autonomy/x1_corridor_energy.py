"""Pure energy models for the X1 multi-leg FW corridor.

Two budgets are intentionally separate (DESIGN-corridor.md section 3.1):

* The planning layer is ``x1_plan.x1_static_violations`` and uses the nominal
  profile powers (cruise/hover/climb/descent) with a 3 m/s ground-speed margin.
* The proof layer is ``x1_motion_proof`` and uses per-leg peak powers: twice
  the loiter power for FW, the highest MC power for MC/landing, and a fixed
  transition energy.

The functions here reproduce those two calculations without performing I/O.
All airframe numbers are loaded from ``configs/airframes/delta_tailsitter_gimbal.json``
and ``configs/onboard/x1-planner.json`` by :func:`default_constants`.
Load once at the call site and pass the result explicitly to the calculations.

Nothing in this module changes an existing admission, proof or executor decision.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .x1_mission import AGL_MAX_M, AGL_MIN_M, MC_RETURN_SPEED_MPS

AIRFRAME_PATH = Path("configs/airframes/delta_tailsitter_gimbal.json")
PLANNER_CONFIG_PATH = Path("configs/onboard/x1-planner.json")

# Design-owned corridor constants that do not exist in the baseline config yet
# (DESIGN-corridor.md 3.6 / 2.2). They become template values in step 2.
DEFAULT_WIND_SIGMA_MPS = 2.0
DEFAULT_MIN_RETURN_GROUND_SPEED_MPS = 6.0


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True, slots=True)
class X1EnergyConstants:
    """Pinned numbers shared by the planning and proof layer models.

    ``runout_m`` is the same expression used in ``x1_plan.x1_static_violations``
    and ``x1_mission.build_x1_mission_steps``:
    ``(max_airspeed + wind_margin) * transition_timeout + transition_position_margin``.
    """

    transition_energy_wh: float
    cruise_power_w: float
    hover_power_w: float
    loiter_power_w: float
    climb_power_w: float
    descent_power_w: float
    max_climb_rate_mps: float
    max_descent_rate_mps: float
    cruise_airspeed_mps: float
    max_airspeed_mps: float
    mc_return_speed_mps: float
    wind_margin_mps: float
    transition_timeout_s: float
    transition_position_margin_m: float
    battery_wh: float
    reserve_fraction: float
    ground_fw_plan_mps: float
    ground_mc_plan_mps: float
    fw_peak_power_w: float
    mc_peak_power_w: float
    runout_m: float

    @property
    def usable_energy_wh(self) -> float:
        """Airframe-only usable energy; a package budget can only lower this."""
        return self.battery_wh * (1.0 - self.reserve_fraction)

    @property
    def agl_min_m(self) -> float:
        return AGL_MIN_M

    @property
    def agl_max_m(self) -> float:
        return AGL_MAX_M

    @classmethod
    def from_configs(
        cls,
        *,
        airframe_path: str | Path | None = None,
        planner_config_path: str | Path | None = None,
    ) -> X1EnergyConstants:
        root = _root()
        planner_path = Path(planner_config_path or root / PLANNER_CONFIG_PATH)
        planner_raw = json.loads(planner_path.read_text(encoding="utf-8"))
        airframe_path = Path(airframe_path or root / planner_raw["airframe_profile"])
        profile = json.loads(airframe_path.read_text(encoding="utf-8"))
        endurance = profile["endurance"]
        transition = profile["transition"]
        wind = endurance["wind_margin_mps"]
        cruise = profile["cruise_airspeed_mps"]
        # Mirrors multidetect.sim_teacher.x1_fw_native_rtl.suffix_power_w; the
        # regression test asserts both derivations stay identical.
        fw_peak = 2.0 * endurance["loiter_power_w"]
        mc_peak = max(
            endurance[name] for name in ("climb_power_w", "hover_power_w", "descent_power_w"))
        runout = (profile["max_airspeed_mps"] + wind) * transition["timeout_s"]
        runout += planner_raw["transition_position_margin_m"]
        return cls(
            transition_energy_wh=endurance["transition_energy_wh"],
            cruise_power_w=endurance["cruise_power_w"],
            hover_power_w=endurance["hover_power_w"],
            loiter_power_w=endurance["loiter_power_w"],
            climb_power_w=endurance["climb_power_w"],
            descent_power_w=endurance["descent_power_w"],
            max_climb_rate_mps=profile["max_climb_rate_mps"],
            max_descent_rate_mps=profile["max_descent_rate_mps"],
            cruise_airspeed_mps=cruise,
            max_airspeed_mps=profile["max_airspeed_mps"],
            mc_return_speed_mps=MC_RETURN_SPEED_MPS,
            wind_margin_mps=wind,
            transition_timeout_s=transition["timeout_s"],
            transition_position_margin_m=planner_raw["transition_position_margin_m"],
            battery_wh=endurance["battery_wh"],
            reserve_fraction=endurance["reserve_fraction"],
            ground_fw_plan_mps=cruise - wind,
            ground_mc_plan_mps=MC_RETURN_SPEED_MPS - wind,
            fw_peak_power_w=fw_peak,
            mc_peak_power_w=mc_peak,
            runout_m=runout,
        )


@lru_cache(maxsize=1)
def default_constants() -> X1EnergyConstants:
    """Load the pinned configuration once for callers that need it."""
    return X1EnergyConstants.from_configs()


def _mc_return_distance(
    *, mc_return_m: float | None, standoff_m: float | None, constants: X1EnergyConstants
) -> float:
    if mc_return_m is not None:
        return float(mc_return_m)
    if standoff_m is not None:
        # DESIGN-corridor.md 2.4: the post-transition drift points at home, so
        # the worst MC leg is max(s, runout - s) instead of the full runout.
        return max(float(standoff_m), constants.runout_m - float(standoff_m))
    return constants.runout_m


def _validate_one_way(one_way_m: float) -> float:
    if type(one_way_m) not in (int, float) or not math.isfinite(one_way_m) or one_way_m < 0:
        raise ValueError("one_way_m must be a finite non-negative distance")
    return float(one_way_m)


def plan_layer_energy_wh(
    one_way_m: float,
    *,
    agl_m: float,
    ground_high_m: float = 0.0,
    return_mode: str = "fw",
    mc_return_m: float | None = None,
    standoff_m: float | None = None,
    observation_s: float = 0.0,
    constants: X1EnergyConstants,
) -> float:
    """Planning-layer sortie energy estimate in Wh (``x1_static_violations``).

    ``return_mode="fw"`` is the corridor design: the outbound and return FW
    distance is ``2 * one_way_m`` and the MC leg starts at the standoff/home
    transition point. ``return_mode="mc"`` is today's single-waypoint plan: the
    FW leg is outbound only and the MC return from ``one_way_m`` home includes
    the transition runout. Both mirror the real static admission formula.
    """
    c = constants
    one_way = _validate_one_way(one_way_m)
    if return_mode not in {"fw", "mc"}:
        raise ValueError("return_mode must be 'fw' or 'mc'")
    height = float(agl_m) + float(ground_high_m)
    if return_mode == "fw":
        fw_distance_m, mc_distance_m = 2.0 * one_way, _mc_return_distance(
            mc_return_m=mc_return_m, standoff_m=standoff_m, constants=c)
    else:
        mc_distance_m = one_way + _mc_return_distance(
            mc_return_m=mc_return_m, standoff_m=standoff_m, constants=c)
        fw_distance_m = one_way
    climb_s = height / c.max_climb_rate_mps
    descent_s = height / c.max_descent_rate_mps
    fw_s = fw_distance_m / c.ground_fw_plan_mps
    mc_s = mc_distance_m / c.ground_mc_plan_mps
    return 2.0 * c.transition_energy_wh + (
        climb_s * c.climb_power_w
        + descent_s * c.descent_power_w
        + fw_s * c.cruise_power_w
        + mc_s * c.hover_power_w
        + float(observation_s) * c.loiter_power_w
    ) / 3600.0


def proof_layer_available_wh(
    *,
    agl_m: float,
    ground_high_m: float = 0.0,
    battery_wh: float | None = None,
    constants: X1EnergyConstants,
) -> float:
    """Energy left at the first FW cruise tick after climb and transition."""
    c = constants
    battery = c.battery_wh if battery_wh is None else float(battery_wh)
    height = float(agl_m) + float(ground_high_m)
    climb_wh = height / c.max_climb_rate_mps * c.mc_peak_power_w / 3600.0
    return battery - climb_wh - c.transition_energy_wh


def proof_layer_reserve_wh(
    *,
    battery_wh: float | None = None,
    package_reserve_wh: float | None = None,
    constants: X1EnergyConstants,
) -> float:
    """Executor reserve: ``max(package reserve, 0.2 * battery)``."""
    c = constants
    battery = c.battery_wh if battery_wh is None else float(battery_wh)
    floor = c.reserve_fraction * battery
    return max(0.0 if package_reserve_wh is None else float(package_reserve_wh), floor)


def proof_layer_requirement_wh(
    one_way_m: float,
    *,
    agl_m: float,
    ground_high_m: float = 0.0,
    return_mode: str = "fw",
    mc_return_m: float | None = None,
    standoff_m: float | None = None,
    fw_peak_power_w: float | None = None,
    fw_return_ground_speed_mps: float | None = None,
    fw_outbound_remaining_m: float | None = None,
    constants: X1EnergyConstants,
) -> float:
    """Remaining energy for an FW corridor continue or immediate return.

    FW legs use ``fw_peak_power_w`` (344 W by default), transitions use
    ``max(6 Wh, mc_peak * duration)``, MC/landing use ``mc_peak_power_w``. The
    default ground-speed bounds are the planning bounds. For FW return,
    ``one_way_m`` is the remaining return distance and also defaults the
    remaining outbound distance. Pass zero outbound distance for an immediate
    return, or pass a directional ground-speed bound from the wind model.
    """
    c = constants
    one_way = _validate_one_way(one_way_m)
    if return_mode not in {"fw", "mc"}:
        raise ValueError("return_mode must be 'fw' or 'mc'")
    height = float(agl_m) + float(ground_high_m)
    fw_power = c.fw_peak_power_w if fw_peak_power_w is None else float(fw_peak_power_w)
    fw_s = one_way / c.ground_fw_plan_mps
    mc_distance_m = _mc_return_distance(
        mc_return_m=mc_return_m, standoff_m=standoff_m, constants=c)
    if return_mode == "fw":
        outbound = one_way if fw_outbound_remaining_m is None else _validate_one_way(
            fw_outbound_remaining_m)
        return_speed = (c.ground_fw_plan_mps if fw_return_ground_speed_mps is None
                        else float(fw_return_ground_speed_mps))
        if not math.isfinite(return_speed) or return_speed <= 0:
            raise ValueError("fw_return_ground_speed_mps must be finite and positive")
        fw_s = outbound / c.ground_fw_plan_mps + one_way / return_speed
    else:
        mc_distance_m += one_way
    fw_wh = fw_power * fw_s / 3600.0
    transition_wh = max(c.transition_energy_wh, c.mc_peak_power_w * c.transition_timeout_s / 3600.0)
    mc_wh = c.mc_peak_power_w * (mc_distance_m / c.ground_mc_plan_mps) / 3600.0
    landing_wh = c.mc_peak_power_w * (height / c.max_descent_rate_mps) / 3600.0
    return fw_wh + transition_wh + mc_wh + landing_wh


def _linear_max_one_way(requirement, budget_wh: float) -> float:
    """Solve a linear ``requirement(d) <= budget`` for the largest distance."""
    at_zero = requirement(0.0)
    slope = requirement(1.0) - at_zero
    if slope <= 0:
        return math.inf
    return max(0.0, (budget_wh - at_zero) / slope)


def max_one_way_m(
    layer: str,
    *,
    agl_m: float,
    ground_high_m: float = 0.0,
    return_mode: str = "fw",
    usable_wh: float | None = None,
    battery_wh: float | None = None,
    package_reserve_wh: float | None = None,
    mc_return_m: float | None = None,
    standoff_m: float | None = None,
    fw_peak_power_w: float | None = None,
    fw_return_ground_speed_mps: float | None = None,
    constants: X1EnergyConstants,
) -> float:
    """Largest one-way distance admitted by a layer.

    Planning layer compares total energy to ``usable_wh`` (default
    ``battery * (1 - reserve_fraction)`` = 59.2 Wh). The proof layer compares
    the first-tick requirement to the energy left after climb and transition
    minus the executor reserve.
    """
    c = constants
    battery = c.battery_wh if battery_wh is None else float(battery_wh)
    if layer == "plan":
        budget = c.usable_energy_wh if usable_wh is None else float(usable_wh)

        def requirement(distance: float) -> float:
            return plan_layer_energy_wh(
                distance, agl_m=agl_m, ground_high_m=ground_high_m, return_mode=return_mode,
                mc_return_m=mc_return_m, standoff_m=standoff_m, constants=c)

        return _linear_max_one_way(requirement, budget)
    if layer == "proof":
        available = proof_layer_available_wh(
            agl_m=agl_m, ground_high_m=ground_high_m, battery_wh=battery, constants=c)
        reserve = proof_layer_reserve_wh(
            battery_wh=battery, package_reserve_wh=package_reserve_wh, constants=c)
        budget = float(usable_wh) if usable_wh is not None else available - reserve

        def requirement(distance: float) -> float:
            return proof_layer_requirement_wh(
                distance, agl_m=agl_m, ground_high_m=ground_high_m, return_mode=return_mode,
                mc_return_m=mc_return_m, standoff_m=standoff_m,
                fw_peak_power_w=fw_peak_power_w,
                fw_return_ground_speed_mps=fw_return_ground_speed_mps, constants=c)

        return _linear_max_one_way(requirement, budget)
    raise ValueError("layer must be 'plan' or 'proof'")


def reverse_wind_ground_speed(
    airspeed_mps: float | None = None,
    *,
    outbound_tailwind_mps: float | None = None,
    wind_margin_mps: float | None = None,
    wind_sigma_mps: float = DEFAULT_WIND_SIGMA_MPS,
    min_ground_speed_mps: float | None = DEFAULT_MIN_RETURN_GROUND_SPEED_MPS,
    constants: X1EnergyConstants,
) -> float:
    """Return-leg ground speed under the reverse-wind bound (DESIGN 3.6).

    ``outbound_tailwind_mps`` is positive when the outbound leg is downwind.
    The return headwind is therefore ``max(wind_margin, tailwind + sigma)`` so
    an outbound gain is never booked as a return credit. Pass ``None`` when
    the wind estimate is missing or stale to use the static margin. A return ground speed
    below ``min_ground_speed_mps`` raises ``ValueError("WIND_EXCEEDS_RETURN")``.
    """
    c = constants
    airspeed = c.cruise_airspeed_mps if airspeed_mps is None else float(airspeed_mps)
    margin = c.wind_margin_mps if wind_margin_mps is None else float(wind_margin_mps)
    headwind = margin
    if outbound_tailwind_mps is not None:
        headwind = max(margin, float(outbound_tailwind_mps) + float(wind_sigma_mps))
    ground_speed = airspeed - headwind
    if min_ground_speed_mps is not None and ground_speed < float(min_ground_speed_mps):
        raise ValueError(
            f"WIND_EXCEEDS_RETURN: return ground speed {ground_speed:.3f} m/s is below "
            f"{float(min_ground_speed_mps):.3f} m/s")
    return ground_speed