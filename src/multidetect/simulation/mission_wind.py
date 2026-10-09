"""Offline wind comparison of the coordinate connectors in an executable Mission."""

import math
from dataclasses import asdict

from multidetect.autonomy.contracts import ExecutableMission, GeoPoint, MissionCommand
from multidetect.geolocation_accuracy import wgs84_distance_m
from multidetect.px4_mission import _initial_bearing_deg
from multidetect.simulation.wind_triangle import solve_wind_triangle


def compare_mission_wind(
    mission: ExecutableMission,
    *,
    airspeed_mps: float,
    wind_north_mps: float,
    wind_east_mps: float,
) -> dict:
    """Keep item identities; never synthesize a return point or skip an RTL.

    Distances use WGS84. Bearings reuse the compiler's spherical initial-bearing
    approximation. Each connector is treated as steady flight at the supplied
    airspeed, regardless of vehicle phase. This is intentionally NOT a total
    mission duration: vertical motion, turns, loiter and transitions are omitted.
    """
    if not isinstance(mission, ExecutableMission):
        raise ValueError("ExecutableMission required")
    solve_wind_triangle(
        track_deg=0,
        airspeed_mps=airspeed_mps,
        wind_north_mps=wind_north_mps,
        wind_east_mps=wind_east_mps,
    )
    if any(item.command is MissionCommand.RETURN_TO_LAUNCH for item in mission.items):
        raise ValueError("RTL has no explicit coordinate path; cannot infer its route")
    coordinates = [item for item in mission.items if item.latitude_deg is not None]
    if len(coordinates) < 2:
        raise ValueError("at least two coordinate items required")
    legs = []
    for start, end in zip(coordinates[:-1], coordinates[1:], strict=True):
        distance = wgs84_distance_m(
            latitude1_deg=start.latitude_deg,
            longitude1_deg=start.longitude_deg,
            latitude2_deg=end.latitude_deg,
            longitude2_deg=end.longitude_deg,
        )
        leg = dict(
            from_sequence=start.sequence,
            to_sequence=end.sequence,
            from_command=start.command.value,
            to_command=end.command.value,
            distance_m=distance,
        )
        if distance == 0:
            leg.update(track_deg=None, wind_solution=None, connector_duration_s=0.0)
        else:
            bearing = _initial_bearing_deg(
                GeoPoint(start.latitude_deg, start.longitude_deg),
                GeoPoint(end.latitude_deg, end.longitude_deg),
            )
            solution = solve_wind_triangle(
                track_deg=bearing,
                airspeed_mps=airspeed_mps,
                wind_north_mps=wind_north_mps,
                wind_east_mps=wind_east_mps,
            )
            leg.update(
                track_deg=bearing,
                wind_solution=asdict(solution),
                connector_duration_s=(
                    distance / solution.along_track_speed_mps if solution.feasible else None
                ),
            )
        legs.append(leg)
    feasible = all(leg["connector_duration_s"] is not None for leg in legs)
    distance = sum(leg["distance_m"] for leg in legs)
    conservative_speed = airspeed_mps - math.hypot(wind_north_mps, wind_east_mps)
    return dict(
        schema_version=1,
        executable_mission_hash=mission.canonical_hash,
        airspeed_assumption_mps=airspeed_mps,
        wind_north_mps=wind_north_mps,
        wind_east_mps=wind_east_mps,
        legs=legs,
        coordinate_connector_distance_m=distance,
        steady_connectors_feasible=feasible,
        steady_connector_duration_s=(
            sum(leg["connector_duration_s"] for leg in legs) if feasible else None
        ),
        uniform_headwind_connector_duration_s=(
            distance / conservative_speed if conservative_speed > 0 else None
        ),
        bearing_model="compiler_spherical_initial_bearing_approximation",
        omitted_effects=[
            "initial_position_to_first_item",
            "climb_descent",
            "turns",
            "loiter",
            "transitions",
            "gusts",
            "airspeed_tracking_error",
        ],
        total_mission_duration_proven=False,
        production_budget_changed=False,
        physical_energy_calibrated=False,
        flight_authorized=False,
    )
