from types import SimpleNamespace

import pytest

from scripts.gazebo_clock_capture import clock_record, describe_clock_progress, time_ns


def message(sim=1, real=2):
    return SimpleNamespace(
        sim=SimpleNamespace(sec=sim, nsec=0),
        real=SimpleNamespace(sec=real, nsec=0),
        system=SimpleNamespace(sec=100, nsec=0),
        HasField=lambda field: True,
    )


def test_explicit_clock_sources_and_ratio():
    rows = [
        clock_record(message(i, i * 2), sequence=i, received_ns=i * 3, topic="/world/default/clock")
        for i in range(3)
    ]
    result = describe_clock_progress(rows)
    assert result["observed_sim_per_gazebo_real"] == 0.5
    assert not result["host_px4_alignment_claimed"]
    assert not result["calibration_eligible"]


@pytest.mark.parametrize("sec,nsec", [(-1, 0), (0, -1), (0, 1000000000), (True, 0)])
def test_bad_time_rejected(sec, nsec):
    with pytest.raises(ValueError):
        time_ns(SimpleNamespace(sec=sec, nsec=nsec))


def test_missing_field_not_silently_zero():
    value = message()
    value.HasField = lambda field: field != "sim"
    with pytest.raises(ValueError, match="missing"):
        clock_record(value, sequence=0, received_ns=0, topic="/world/default/clock")


@pytest.mark.parametrize("fault", ["reset", "sequence", "topic", "frozen"])
def test_bad_progress_rejected(fault):
    rows = [
        clock_record(message(i + 1, i + 1), sequence=i, received_ns=i, topic="/world/default/clock")
        for i in range(2)
    ]
    if fault == "reset":
        rows[1]["sim_ns"] = 0
    elif fault == "sequence":
        rows[1]["sequence"] = 4
    elif fault == "topic":
        rows[1]["topic"] = "/world/other/clock"
    else:
        rows[1]["real_ns"] = rows[0]["real_ns"]
    with pytest.raises(ValueError):
        describe_clock_progress(rows)


def test_paused_simulation_is_zero_not_failure_or_extrapolation():
    rows = [
        clock_record(message(1, i + 1), sequence=i, received_ns=i, topic="/world/default/clock")
        for i in range(2)
    ]
    assert describe_clock_progress(rows)["observed_sim_per_gazebo_real"] == 0
