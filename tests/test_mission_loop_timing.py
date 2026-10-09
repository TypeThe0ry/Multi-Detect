import pytest

from scripts.mission_loop_timing import MissionLoopTiming


def test_bounded_phase_accounting_and_copy_isolation():
    identity = {"source": "test", "session_id": "a"}
    timing = MissionLoopTiming(identity)
    identity["session_id"] = "changed"
    for i in range(100):
        timing.record("pump", i, i + 0.25)
    timing.record("shadow", 101, 101.5)
    timing.record("other", 102, 104)
    result = timing.snapshot()
    assert result["host_clock_identity"]["session_id"] == "a"
    assert result["phases"]["pump"]["count"] == 100
    assert result["phases"]["pump"]["total_s"] == 25
    assert len(result["phases"]["pump"]["slowest"]) == 32
    result["phases"].clear()
    assert timing.snapshot()["phases"]["other"]["total_s"] == 2


@pytest.mark.parametrize(
    "phase,start,end", [("bad", 1, 2), ("pump", 2, 1), ("pump", True, 2), ("pump", 1, float("nan"))]
)
def test_invalid_interval_rejected(phase, start, end):
    timing = MissionLoopTiming({})
    with pytest.raises(ValueError):
        timing.record(phase, start, end)
    assert timing.snapshot()["phases"] == {}
