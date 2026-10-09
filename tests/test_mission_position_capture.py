from types import SimpleNamespace

import pytest

from scripts.mission_position_capture import MissionPositionCapture


def message(**fields):
    return SimpleNamespace(
        get_type=lambda: "GLOBAL_POSITION_INT",
        get_srcSystem=lambda: 1,
        get_srcComponent=lambda: 1,
        **fields,
    )


def test_raw_source_time_altitude_and_ned_velocity_retained():
    capture = MissionPositionCapture()
    capture.record(
        message(
            time_boot_ms=100, lat=13000000, lon=1038005000, relative_alt=-145, vx=-10, vy=20, vz=30
        ),
        1.23456789,
    )
    data = capture.snapshot()
    sample = data["samples"][0]
    assert sample["relative_alt"] == -145
    assert sample["vz"] == 30
    assert sample["time_boot_ms"] == 100
    assert sample["received_at_s"] == 1.23456789
    assert sample["missing_integer_fields"] == []
    assert data["units"]["vz"] == "cm/s_down"
    assert data["source_clock_mapping_proven"] is False


def test_capacity_truncation_is_explicit_and_preserves_start():
    capture = MissionPositionCapture(2)
    for index in range(4):
        capture.record(message(time_boot_ms=index), index)
    data = capture.snapshot()
    assert data["samples_seen"] == 4
    assert data["samples_dropped"] == 2
    assert data["truncated"] is True
    assert [s["time_boot_ms"] for s in data["samples"]] == [0, 1]


def test_missing_values_and_snapshot_mutation_do_not_corrupt_capture():
    capture = MissionPositionCapture()
    capture.record(message(vz=True, relative_alt=float("nan")), 0)
    snapshot = capture.snapshot()
    assert snapshot["samples"][0]["vz"] is None
    assert "relative_alt" in snapshot["samples"][0]["missing_integer_fields"]
    snapshot["samples"][0]["missing_integer_fields"].clear()
    snapshot["samples"][0]["vz"] = 5
    assert capture.snapshot()["samples"][0]["vz"] is None
    assert capture.snapshot()["samples"][0]["missing_integer_fields"]


@pytest.mark.parametrize("capacity", [0, -1, 32769, True, 1.5])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        MissionPositionCapture(capacity)


@pytest.mark.parametrize("at", [-1, True, float("nan"), float("inf")])
def test_invalid_time(at):
    capture = MissionPositionCapture()
    with pytest.raises(ValueError):
        capture.record(message(), at)


def test_other_messages_not_recorded():
    capture = MissionPositionCapture()
    capture.record(SimpleNamespace(get_type=lambda: "HEARTBEAT"), 0)
    assert capture.snapshot()["samples_seen"] == 0
