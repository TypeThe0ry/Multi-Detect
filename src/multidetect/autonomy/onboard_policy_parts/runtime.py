"""Runtime assembly helpers for the onboard policy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig


def build_safety_envelope(
    options: Mapping[str, Any],
    airframe_profile: Any,
    motion_adapter: Any,
    mission_package: Any,
    home_ned: tuple[float, float, float] | None,
    ground_projection: Any,
) -> SafetyEnvelope:
    """Build the final policy envelope from airframe and package constraints."""
    # Package geometry is authoritative for home and operating area; explicit
    # safety settings may tighten those limits further.
    envelope_source = dict(options)
    if airframe_profile.category != "multirotor":
        dynamics = motion_adapter.safety.vehicle
        envelope_source["vehicle"] = {
            "category": dynamics.category,
            "can_hover": dynamics.can_hover,
            "min_airspeed_mps": dynamics.min_airspeed_mps,
            "cruise_airspeed_mps": dynamics.cruise_airspeed_mps,
            "max_airspeed_mps": dynamics.max_airspeed_mps,
            "min_turn_radius_m": dynamics.min_turn_radius_m,
            "max_bank_deg": dynamics.max_bank_deg,
            "climb_rate_mps": dynamics.climb_rate_mps,
            "descent_rate_mps": dynamics.descent_rate_mps,
        }
        # The multirotor-sized default clips fixed-wing search and turn-off legs.
        envelope_source.setdefault(
            "max_translation_m", airframe_profile.limits.max_step_distance_m
        )
    if mission_package is not None:
        area = mission_package.document["operating_area"]
        package_polygon = (
            (float(area["north_min_m"]), float(area["east_min_m"])),
            (float(area["north_max_m"]), float(area["east_min_m"])),
            (float(area["north_max_m"]), float(area["east_max_m"])),
            (float(area["north_min_m"]), float(area["east_max_m"])),
        )
        envelope_source.setdefault("operating_polygon", package_polygon)
        envelope_source.setdefault("min_altitude_m", float(area["min_altitude_m"]))
        envelope_source.setdefault("max_altitude_m", float(area["max_altitude_m"]))
        raw_home = tuple(float(value) for value in mission_package.document["home"])
        package_home = raw_home if len(raw_home) == 3 else (*raw_home, 0.0)
    else:
        package_home = None
    return SafetyEnvelope(
        SafetyEnvelopeConfig.from_mapping(envelope_source),
        home_ned=(tuple(float(value) for value in home_ned) if home_ned is not None else package_home),
        ground_model=getattr(ground_projection, "model", None),
    )
