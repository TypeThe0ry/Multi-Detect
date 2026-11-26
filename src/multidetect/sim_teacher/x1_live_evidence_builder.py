"""Assemble SIM-only continue evidence from host-verified bounds."""
from __future__ import annotations

from copy import deepcopy

from multidetect.autonomy.mission_package import number
from multidetect.sim_teacher.x1_live_clearance import _box, _fields, _suffix, parse_native

_BINDINGS = ("scope_sha256", "dem_sha256", "base_mission_sha256")
_FIELDS = ("dem_error_m", "obstacle_coverage", "obstacles", "landing_pad", "battery", "suffixes")
# Optional per-action native bounds. Absent sections keep those actions rejected.
_OPTIONAL = ("native",)


def build_live_evidence(package, *, terrain, scope_sha256, base_mission_sha256, surveyed_bounds=None):
    """Bind independently verified bounds to the compiled scenario and package.

    ``surveyed_bounds`` is an optional local host input. Hashes check its
    applicability, while the operator is responsible for its provenance. The
    DEM sigma, planned route, and airframe limits cannot certify hard bounds.
    """
    bindings = dict(zip(_BINDINGS, (scope_sha256, terrain.source_sha256, base_mission_sha256), strict=True))
    evidence = {key: bindings[key] for key in _BINDINGS[:2]}
    budget = package.document["budget"]
    bounds = {} if surveyed_bounds is None else surveyed_bounds
    if surveyed_bounds is not None:
        if any(bounds.get(key) != value for key, value in bindings.items()):
            raise ValueError("host evidence bounds do not match the compiled scenario, DEM or base mission")
        if bounds.keys() - set(_BINDINGS + _FIELDS + _OPTIONAL):
            raise ValueError("unknown field in host evidence bounds")

    if "dem_error_m" in bounds:
        evidence["dem_error_m"] = number(bounds["dem_error_m"], "DEM absolute error bound")

    if "obstacle_coverage" in bounds and "obstacles" in bounds:
        evidence["obstacle_coverage"] = list(_box(bounds["obstacle_coverage"]))
        obstacles = bounds["obstacles"]
        if not isinstance(obstacles, list) or len(obstacles) > 4096:
            raise ValueError("invalid obstacle inventory")
        evidence["obstacles"] = [list(_box(box)) for box in obstacles]

    if "landing_pad" in bounds and "dem_error_m" in evidence:
        radius, error = _fields(bounds["landing_pad"], "radius_m", "elevation_error_m")
        evidence["landing_pad"] = {
            "radius_m": number(radius, "pad radius", minimum=0.001),
            "elevation_error_m": number(error, "pad error", minimum=evidence["dem_error_m"]),
        }

    if "battery" in bounds and "battery_capacity_wh" in budget:
        voltage, soc_error, consumed_error = _fields(
            bounds["battery"], "max_voltage_v", "soc_error_pct", "consumption_error_wh")
        evidence["battery"] = {
            "max_voltage_v": number(voltage, "voltage upper bound", minimum=0.001),
            "soc_error_pct": number(soc_error, "SOC error"),
            "consumption_error_wh": number(consumed_error, "consumption error"),
        }

    if "suffixes" in bounds:
        suffixes = bounds["suffixes"]
        if not isinstance(suffixes, dict) or not suffixes.keys() <= {"1", "2", "3"}:
            raise ValueError("invalid suffix indices")
        for index, suffix in suffixes.items():
            _suffix(suffix, int(index))
        if suffixes:
            evidence["suffixes"] = deepcopy(suffixes)

    if "native" in bounds and "landing_pad" in evidence:
        parse_native(bounds["native"], terrain)
        evidence["native"] = deepcopy(bounds["native"])

    missing = [key for key in _FIELDS if key not in evidence]
    if "battery_capacity_wh" not in budget:
        missing.append("budget.battery_capacity_wh")
    return evidence, missing
