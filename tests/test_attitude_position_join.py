import copy
import math

import pytest
from test_mission_dynamics_capture import message

from multidetect.simulation.attitude_position_join import join_attitude_positions
from scripts.mission_dynamics_capture import MissionDynamicsCapture
from scripts.mission_position_capture import MissionPositionCapture


def flight():
    positions, dynamics = MissionPositionCapture(), MissionDynamicsCapture()
    for boot in (100, 120, 140):
        positions.record(
            message(
                "GLOBAL_POSITION_INT",
                time_boot_ms=boot,
                lat=13000000,
                lon=1038000000,
                relative_alt=25000,
                vx=300,
                vy=400,
                vz=0,
            ),
            boot / 1000 + 10,
        )
    for boot in (100, 121, 140):
        dynamics.record(
            message(
                time_boot_ms=boot,
                roll=math.pi / 6,
                pitch=0,
                yaw=1,
                rollspeed=0,
                pitchspeed=0,
                yawspeed=0,
            ),
            boot / 1000 + 9,
        )
    return dict(
        phase_position_observations=positions.snapshot(),
        phase_dynamics_observations=dynamics.snapshot(),
    )


def test_exact_source_join_not_nearest_or_receive_time():
    result = join_attitude_positions(flight())
    assert result["matched_count"] == 2
    assert result["unmatched_positions"] == result["unmatched_attitudes"] == 1
    assert [m["time_boot_ms"] for m in result["matches"]] == [100, 140]
    assert result["matches"][0]["roll_deg"] == pytest.approx(30)
    assert result["matches"][0]["receipt_difference_s"] == pytest.approx(1)
    assert result["matches"][0]["horizontal_groundspeed_mps"] == 5
    assert result["physical_simultaneity_proven"] is False
    assert result["flight_tracking_gate_passed"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("time_boot_ms", True),
        ("time_boot_ms", -1),
        ("time_boot_ms", 2**32),
        ("roll", math.nan),
        ("roll", math.pi + 0.1),
        ("roll", True),
        ("source_system_id", 2),
        ("source_component_id", True),
        ("received_at_s", math.inf),
        ("received_at_s", -1),
        ("invalid_or_missing_fields", ["pitch"]),
    ],
)
def test_malformed_attitude_rejected(field, value):
    data = flight()
    data["phase_dynamics_observations"]["streams"]["ATTITUDE"]["samples"][0][field] = value
    with pytest.raises(ValueError):
        join_attitude_positions(data)


@pytest.mark.parametrize(
    "field,value",
    [("truncated", True), ("samples_dropped", 1), ("samples_seen", 4), ("samples_dropped", False)],
)
def test_incomplete_capture(field, value):
    data = flight()
    data["phase_dynamics_observations"]["streams"]["ATTITUDE"][field] = value
    with pytest.raises(ValueError):
        join_attitude_positions(data)


def test_duplicate_source_timestamp_and_units_rejected():
    data = flight()
    duplicate = copy.deepcopy(data)
    samples = duplicate["phase_dynamics_observations"]["streams"]["ATTITUDE"]["samples"]
    samples[1]["time_boot_ms"] = samples[0]["time_boot_ms"]
    with pytest.raises(ValueError):
        join_attitude_positions(duplicate)
    data["phase_dynamics_observations"]["units"]["ATTITUDE"]["roll"] = "deg"
    with pytest.raises(ValueError):
        join_attitude_positions(data)


def test_no_source_overlap_does_not_fabricate_matches():
    data = flight()
    for row in data["phase_dynamics_observations"]["streams"]["ATTITUDE"]["samples"]:
        row["time_boot_ms"] += 1000
    result = join_attitude_positions(data)
    assert result["matches"] == []
    assert result["unmatched_positions"] == result["unmatched_attitudes"] == 3


def test_position_truncation_is_rejected_before_join():
    data = flight()
    data["phase_position_observations"]["truncated"] = True
    with pytest.raises(ValueError):
        join_attitude_positions(data)
