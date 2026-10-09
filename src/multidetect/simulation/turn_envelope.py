"""Still-air coordinated level-turn geometry, not a flight capability gate.

Inputs are true airspeed, not CAS or groundspeed. Wind, stall margin, lift,
thrust, bank slew and controller tracking are outside this calculation.
"""

import math


def coordinated_turn_envelope(
    *, true_airspeed_mps: float, radius_m: float, bank_limit_deg: float
) -> dict:
    """Compare requested air-relative radius against a configured bank limit."""
    for name, value in (
        ("true_airspeed_mps", true_airspeed_mps),
        ("radius_m", radius_m),
        ("bank_limit_deg", bank_limit_deg),
    ):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if bank_limit_deg >= 90:
        raise ValueError("bank_limit_deg must be below 90 degrees")
    gravity = 9.80665
    try:
        speed_squared = true_airspeed_mps * true_airspeed_mps
        limit_acceleration = gravity * math.tan(math.radians(bank_limit_deg))
        requested_acceleration = speed_squared / radius_m
        minimum_radius = speed_squared / limit_acceleration
        maximum_speed = math.sqrt(radius_m * limit_acceleration)
        required_bank = math.degrees(math.atan(requested_acceleration / gravity))
        load_factor = math.hypot(1.0, requested_acceleration / gravity)
    except (OverflowError, ZeroDivisionError) as exc:
        raise ValueError("unrepresentable turn geometry") from exc
    if not all(
        math.isfinite(value) and value > 0
        for value in (
            speed_squared,
            limit_acceleration,
            requested_acceleration,
            minimum_radius,
            maximum_speed,
            required_bank,
            load_factor,
        )
    ):
        raise ValueError("unrepresentable turn geometry")
    return {
        "model": "still_air_coordinated_level_turn",
        "true_airspeed_mps": true_airspeed_mps,
        "requested_radius_m": radius_m,
        "bank_limit_deg": bank_limit_deg,
        "required_bank_deg": required_bank,
        "minimum_radius_at_bank_limit_m": minimum_radius,
        "maximum_speed_at_radius_mps": maximum_speed,
        "required_load_factor": load_factor,
        "within_configured_bank_limit": requested_acceleration <= limit_acceleration,
        "aircraft_capability_proven": False,
        "ground_track_feasibility_proven": False,
        "flight_authorized": False,
    }
