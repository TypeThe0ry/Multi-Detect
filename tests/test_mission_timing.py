from dataclasses import replace

import pytest
from test_mission_planning import PROFILE_HASH, _context, _geometry_and_policy, _task

from multidetect.autonomy.contracts import MissionCommand
from multidetect.mission_planning import compile_planned_task_mission
from multidetect.simulation.mission_timing import PhaseTimingAssumptions, estimate_mission_phases

PROFILE = PhaseTimingAssumptions(20, 8, 3, 2, 12, 9, 4, 5, 2, "synthetic test, uncalibrated")


def mission_for(vehicle):
    geometry, policy = _geometry_and_policy(vehicle)
    return compile_planned_task_mission(
        _task(),
        _context(vehicle),
        vehicle,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    ).compiled_mission.executable_mission


def estimate(mission, vehicle="vtol", profile=PROFILE, wind=0):
    return estimate_mission_phases(
        mission,
        vehicle_class=vehicle,
        assumptions=profile,
        wind_north_mps=wind,
        wind_east_mps=wind,
    )


@pytest.mark.parametrize("vehicle", ["vtol", "fixed-wing", "multirotor"])
def test_every_real_compiler_item_accounted_once(vehicle):
    mission = mission_for(vehicle)
    result = estimate(mission, vehicle)
    assert result["executable_mission_hash"] == mission.canonical_hash
    assert [p["sequence"] for p in result["phases"]] == list(range(len(mission.items)))
    assert result["modeled_total_s"] == pytest.approx(
        sum(
            sum(
                p[name]
                for name in ("horizontal_s", "vertical_s", "hold_s", "transition_s", "allowance_s")
            )
            for p in result["phases"]
        )
    )
    assert sum(p["hold_s"] for p in result["phases"]) == sum(
        item.param1 for item in mission.items if item.command is MissionCommand.LOITER_TIME
    )
    assert result["phases"][0]["vertical_s"] == mission.items[0].altitude_m / 3
    assert result["physical_calibration_proven"] is False
    assert result["flight_authorized"] is False


def test_vtol_transition_changes_speed_and_landing_phase():
    mission = mission_for("vtol")
    result = estimate(mission)
    transitions = [p for p in result["phases"] if p["transition_s"]]
    assert [(p["mode_before"], p["mode_after"], p["transition_s"]) for p in transitions] == [
        ("mc", "fw", 12),
        ("fw", "mc", 9),
    ]
    assert result["phases"][-1]["mode_before"] == "mc"
    slower = estimate(mission, profile=replace(PROFILE, multirotor_airspeed_mps=4))
    assert slower["phases"][-1]["horizontal_s"] == pytest.approx(
        2 * result["phases"][-1]["horizontal_s"],
    )
    # MC speed must not accidentally change the FW route sections.
    for first, second in zip(result["phases"], slower["phases"], strict=True):
        if first["mode_before"] == "fw":
            assert first["horizontal_s"] == second["horizontal_s"]


def test_loiter_timer_and_transition_cost_not_double_counted():
    mission = mission_for("vtol")
    items = tuple(
        replace(i, param1=i.param1 + 7) if i.command is MissionCommand.LOITER_TIME else i
        for i in mission.items
    )
    original = estimate(mission)
    changed = estimate(replace(mission, items=items))
    assert changed["modeled_total_s"] - original["modeled_total_s"] == pytest.approx(7)
    changed = estimate(mission, profile=replace(PROFILE, transition_to_fw_s=17))
    assert changed["modeled_total_s"] - original["modeled_total_s"] == pytest.approx(5)


@pytest.mark.parametrize("vehicle", ["vtol", "fixed-wing", "multirotor"])
def test_infeasible_wind_prevents_partial_sum_from_becoming_total(vehicle):
    result = estimate(mission_for(vehicle), vehicle, wind=50)
    assert not result["modeled_phases_feasible"]
    assert result["modeled_total_s"] is None
    assert any(p["infeasible_reason"] for p in result["phases"])


@pytest.mark.parametrize(
    "field",
    [
        "fixed_wing_airspeed_mps",
        "multirotor_airspeed_mps",
        "climb_rate_mps",
        "descent_rate_mps",
        "transition_to_fw_s",
        "transition_to_mc_s",
        "takeoff_settle_s",
        "landing_settle_s",
        "waypoint_turn_allowance_s",
    ],
)
@pytest.mark.parametrize("value", [-1, float("nan"), True])
def test_invalid_profile_values_rejected(field, value):
    with pytest.raises(ValueError):
        replace(PROFILE, **{field: value})


def test_missing_provenance_and_wrong_vehicle_rejected():
    with pytest.raises(ValueError, match="provenance"):
        replace(PROFILE, provenance=" ")
    with pytest.raises(ValueError, match="lifecycle"):
        estimate(mission_for("vtol"), "fixed-wing")


def test_waypoint_hold_timer_is_preserved():
    mission = mission_for("vtol")
    index = next(i.sequence for i in mission.items if i.command is MissionCommand.WAYPOINT)
    items = tuple(replace(i, param1=6) if i.sequence == index else i for i in mission.items)
    assert estimate(replace(mission, items=items))["modeled_total_s"] == pytest.approx(
        estimate(mission)["modeled_total_s"] + 6,
    )


def test_redundant_transition_rejected():
    mission = mission_for("vtol")
    items = tuple(replace(i, param1=3) if i.sequence == 1 else i for i in mission.items)
    with pytest.raises(ValueError, match="redundant transition"):
        estimate(replace(mission, items=items))
