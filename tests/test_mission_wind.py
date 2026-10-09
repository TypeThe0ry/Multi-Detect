import pytest
from test_mission_planning import PROFILE_HASH, _context, _geometry_and_policy, _task

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.mission_planning import compile_planned_task_mission
from multidetect.simulation.mission_wind import compare_mission_wind


@pytest.mark.parametrize("vehicle", ["vtol", "fixed-wing", "multirotor"])
@pytest.mark.parametrize("north,east", [(0, 0), (5, 0), (0, 5), (30, 30)])
def test_comparison_uses_actual_compiler_coordinate_items(vehicle, north, east):
    geometry, policy = _geometry_and_policy(vehicle)
    compiled = compile_planned_task_mission(
        _task(),
        _context(vehicle),
        vehicle,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )
    mission = compiled.compiled_mission.executable_mission
    result = compare_mission_wind(
        mission,
        airspeed_mps=20,
        wind_north_mps=north,
        wind_east_mps=east,
    )
    coordinates = [item for item in mission.items if item.latitude_deg is not None]
    assert result["executable_mission_hash"] == mission.canonical_hash
    assert [(leg["from_sequence"], leg["to_sequence"]) for leg in result["legs"]] == [
        (start.sequence, end.sequence)
        for start, end in zip(coordinates[:-1], coordinates[1:], strict=True)
    ]
    assert result["legs"][-1]["to_sequence"] == mission.items[-1].sequence
    if north == east == 0:
        assert result["steady_connector_duration_s"] == pytest.approx(
            result["coordinate_connector_distance_m"] / 20,
        )
    if north == east == 30:
        assert not result["steady_connectors_feasible"]
        assert result["steady_connector_duration_s"] is None
    assert result["total_mission_duration_proven"] is False
    assert result["flight_authorized"] is False


@pytest.mark.parametrize(
    "command,message",
    [
        (MissionCommand.RETURN_TO_LAUNCH, "RTL"),
        (MissionCommand.VTOL_TRANSITION, "two coordinate"),
    ],
)
def test_implicit_or_missing_route_is_not_invented(command, message):
    mission = ExecutableMission(
        mission_id="no-route",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
        airframe_adapter_id="fixture",
        airframe_adapter_hash="c" * 64,
        items=(
            MissionItem(
                sequence=0,
                command=command,
                frame=MissionFrame.MISSION,
                param1=4.0 if command is MissionCommand.VTOL_TRANSITION else 0.0,
            ),
        ),
    )
    with pytest.raises(ValueError, match=message):
        compare_mission_wind(mission, airspeed_mps=20, wind_north_mps=0, wind_east_mps=0)
