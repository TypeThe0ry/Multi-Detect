"""Explicit, uncalibrated phase accounting for offline Mission comparisons."""

import math
from dataclasses import asdict, dataclass

from multidetect.autonomy.contracts import ExecutableMission, MissionCommand, MissionFrame
from multidetect.simulation.mission_wind import compare_mission_wind


@dataclass(frozen=True)
class PhaseTimingAssumptions:
    fixed_wing_airspeed_mps: float
    multirotor_airspeed_mps: float
    climb_rate_mps: float
    descent_rate_mps: float
    transition_to_fw_s: float
    transition_to_mc_s: float
    takeoff_settle_s: float
    landing_settle_s: float
    waypoint_turn_allowance_s: float
    provenance: str

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "provenance":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
            if (name.endswith("mps")) and value == 0:
                raise ValueError(f"{name} must be positive")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("explicit timing assumption provenance required")


def estimate_mission_phases(
    mission: ExecutableMission,
    *,
    vehicle_class: str,
    assumptions: PhaseTimingAssumptions,
    wind_north_mps: float,
    wind_east_mps: float,
) -> dict:
    """Account once per item under a serial horizontal/vertical-motion assumption.

    Starts landed at the first takeoff coordinate at relative altitude zero.
    Uses separate assumed airspeeds across VTOL transitions. Loiter duration is
    the command's timer, not another circumference/speed charge. A per-waypoint
    allowance is explicit, not a measured turn or a flight-dynamics bound.
    """
    if vehicle_class not in {"vtol", "fixed-wing", "multirotor"}:
        raise ValueError("unknown vehicle class")
    if not isinstance(assumptions, PhaseTimingAssumptions):
        raise ValueError("PhaseTimingAssumptions required")
    if not isinstance(mission, ExecutableMission):
        raise ValueError("ExecutableMission required")
    expected_start = (
        MissionCommand.VTOL_TAKEOFF if vehicle_class == "vtol" else MissionCommand.TAKEOFF
    )
    expected_end = MissionCommand.VTOL_LAND if vehicle_class == "vtol" else MissionCommand.LAND
    if (
        mission.items[0].command is not expected_start
        or mission.items[-1].command is not expected_end
    ):
        raise ValueError("mission lifecycle does not match vehicle class")
    allowed = {expected_start, expected_end, MissionCommand.WAYPOINT, MissionCommand.LOITER_TIME}
    if vehicle_class == "vtol":
        allowed.add(MissionCommand.VTOL_TRANSITION)
    for item in mission.items:
        if item.command not in allowed:
            raise ValueError(f"unsupported timing command: {item.command.value}")
        if item.latitude_deg is not None and item.frame is not MissionFrame.GLOBAL_RELATIVE_ALT_INT:
            raise ValueError("relative-home altitude frame required")
        if item.altitude_m is not None and item.altitude_m < 0:
            raise ValueError("negative relative altitude not modeled")
    if (
        sum(item.command is expected_start for item in mission.items) != 1
        or sum(item.command is expected_end for item in mission.items) != 1
    ):
        raise ValueError("exactly one takeoff and landing required")
    # Reuse the already-bound item connector computation at each assumed speed.
    connectors = {}
    for mode, speed in (
        ("fw", assumptions.fixed_wing_airspeed_mps),
        ("mc", assumptions.multirotor_airspeed_mps),
    ):
        result = compare_mission_wind(
            mission,
            airspeed_mps=speed,
            wind_north_mps=wind_north_mps,
            wind_east_mps=wind_east_mps,
        )
        connectors[mode] = {leg["to_sequence"]: leg for leg in result["legs"]}
    mode = "fw" if vehicle_class == "fixed-wing" else "mc"
    altitude = 0.0
    phases = []
    for item in mission.items:
        horizontal = vertical = hold = transition = allowance = 0.0
        reason = None
        before = mode
        if item.command is MissionCommand.VTOL_TRANSITION:
            target = "fw" if item.param1 == 4 else "mc"
            if target == mode:
                raise ValueError("redundant transition is not a calibrated timing phase")
            transition = (
                assumptions.transition_to_fw_s if target == "fw" else assumptions.transition_to_mc_s
            )
            mode = target
        else:
            if vehicle_class == "vtol" and item.command is expected_end and mode != "mc":
                raise ValueError("VTOL landing requires prior MC transition")
            leg = connectors[mode].get(item.sequence)
            if leg is not None:
                horizontal = leg["connector_duration_s"]
                if horizontal is None:
                    reason = leg["wind_solution"]["reason"]
            delta = item.altitude_m - altitude
            vertical = abs(delta) / (
                assumptions.climb_rate_mps if delta >= 0 else assumptions.descent_rate_mps
            )
            altitude = item.altitude_m
            if item.command is MissionCommand.LOITER_TIME:
                if item.param1 < 0:
                    raise ValueError("negative loiter timer")
                hold = item.param1
            elif item.command is MissionCommand.WAYPOINT:
                if item.param1 < 0:
                    raise ValueError("negative waypoint hold timer")
                hold = item.param1
                allowance = assumptions.waypoint_turn_allowance_s
            elif item.command is expected_start:
                allowance = assumptions.takeoff_settle_s
            elif item.command is expected_end:
                allowance = assumptions.landing_settle_s
        phases.append(
            dict(
                sequence=item.sequence,
                command=item.command.value,
                mode_before=before,
                mode_after=mode,
                horizontal_s=horizontal,
                vertical_s=vertical,
                hold_s=hold,
                transition_s=transition,
                allowance_s=allowance,
                infeasible_reason=reason,
                modeled_duration_s=None
                if horizontal is None
                else horizontal + vertical + hold + transition + allowance,
            )
        )
    for phase in phases:
        duration = phase["modeled_duration_s"]
        if duration is not None and not math.isfinite(duration):
            raise ValueError("phase duration overflow")
    feasible = all(phase["modeled_duration_s"] is not None for phase in phases)
    total = sum(phase["modeled_duration_s"] for phase in phases) if feasible else None
    if total is not None and not math.isfinite(total):
        raise ValueError("total duration overflow")
    return dict(
        schema_version=1,
        executable_mission_hash=mission.canonical_hash,
        vehicle_class=vehicle_class,
        assumptions=asdict(assumptions),
        wind_north_mps=wind_north_mps,
        wind_east_mps=wind_east_mps,
        phases=phases,
        modeled_phases_feasible=feasible,
        modeled_total_s=total,
        unmodeled_effects=[
            "gusts",
            "airspeed_tracking_error",
            "loiter_path_feasibility",
            "transition_dynamics",
            "initial_position_error",
            "turn_geometry",
        ],
        horizontal_vertical_composition="serial_not_measured_bound",
        start_assumption="landed_at_first_takeoff_coordinate_relative_altitude_zero",
        physical_calibration_proven=False,
        production_budget_changed=False,
        flight_authorized=False,
    )
