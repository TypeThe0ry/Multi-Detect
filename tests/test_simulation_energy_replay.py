from copy import deepcopy

import pytest

from multidetect.simulation.energy_replay import CumulativeSimulationEnergy, replay_energy_inputs
from multidetect.simulator.flight_model import SimulationEnergyProfile

PROFILE = SimulationEnergyProfile(10, 36, 324, "test assumption")


def row(kind, field, value, at):
    return dict(type=kind, field=field, value=value, at_s=at, source_system=1, source_component=1)


def history():
    samples = [
        row("HEARTBEAT", "base_mode", 128, 0),
        row("VFR_HUD", "throttle", 100, 0),
        row("GLOBAL_POSITION_INT", "time_boot_ms", 1000, 0),
        row("HEARTBEAT", "base_mode", 0, 0.5),
        row("VFR_HUD", "throttle", 0, 0.5),
        row("GLOBAL_POSITION_INT", "time_boot_ms", 1500, 1),
        row("GLOBAL_POSITION_INT", "time_boot_ms", 2000, 1.5),
    ]
    return dict(samples=samples, samples_seen=len(samples), samples_dropped=0)


def test_replay_uses_left_load_and_boot_time_not_receive_duration():
    result = replay_energy_inputs(history(), PROFILE)
    assert result["segments"] == 2
    assert result["covered_boot_time_s"] == 1
    assert result["consumed_wh"] == pytest.approx(0.055)
    assert result["authorizes_next_task"] is False
    assert result["full_mission_coverage_claimed"] is False
    assert replay_energy_inputs(history(), PROFILE) == result


def test_replay_ignores_duplicate_px4_boot_timestamp_without_adding_energy():
    data = history()
    data["samples"].append(row("GLOBAL_POSITION_INT", "time_boot_ms", 2000, 1.51))
    data["samples_seen"] = len(data["samples"])
    result = replay_energy_inputs(data, PROFILE)
    baseline = replay_energy_inputs(history(), PROFILE)
    for key in ("segments", "covered_boot_time_s", "consumed_wh", "authorizes_next_task"):
        assert result[key] == baseline[key]


@pytest.mark.parametrize(
    "index,key,value,reason",
    [
        (5, "value", 900, "boot_time_not_strictly_increasing"),
        (5, "value", 3000, "boot_time_gap"),
        (5, "at_s", 3, "load_or_receive_gap"),
        (5, "source_system", 2, "unexpected_source"),
        (5, "source_system", True, "unexpected_source"),
        (5, "value", True, "invalid_field"),
        (5, "at_s", 0.1, "receive_time_regression"),
    ],
)
def test_bad_interval_stops_before_energy_is_counted(index, key, value, reason):
    data = deepcopy(history())
    data["samples"][index][key] = value
    result = replay_energy_inputs(data, PROFILE)
    assert result["reason"] == reason
    assert result["segments"] == 0
    assert result["consumed_wh"] == 0


def test_truncated_history_cannot_be_silently_used():
    data = history()
    data["samples_dropped"] = 1
    with pytest.raises(ValueError, match="untruncated"):
        replay_energy_inputs(data, PROFILE)


def test_cumulative_gap_error_includes_freshness_diagnostics():
    data = cumulative_history()
    data["samples"][5]["at_s"] = 3
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture-a", initial_remaining_wh=5)
    with pytest.raises(ValueError, match="gap_details=.*receive_gap_s"):
        state.observe(data, session_id="capture-a")


def cumulative_history():
    data = history()
    data.update(
        receive_origin_host_monotonic_s=100, receive_clock="host_monotonic_since_collector_start"
    )
    return data


def qpc_history():
    data = cumulative_history()
    data["receive_clock"] = "host_perf_counter_since_collector_start"
    data["host_clock_identity"] = dict(
        schema_version=1,
        source="python.perf_counter",
        session_id="host-run-a",
        implementation="fixture QPC",
        resolution_ns=100,
        accuracy_verified=False,
        legacy_monotonic_equivalent=False,
    )
    return data


def test_qpc_cumulative_energy_matches_legacy_boot_time_replay():
    data = qpc_history()
    original = deepcopy(data)
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture-a", initial_remaining_wh=5)
    result = state.observe(data, session_id="capture-a")
    assert result["consumed_wh"] == pytest.approx(0.055)
    assert result["host_clock_identity"] == data["host_clock_identity"]
    result["host_clock_identity"]["session_id"] = "mutated-return"
    assert state.observe(data, session_id="capture-a")["newly_consumed_wh"] == 0
    assert data == original


@pytest.mark.parametrize(
    "change", ["missing", "null", "session", "implementation", "resolution", "legacy", "origin"]
)
def test_qpc_clock_change_rejected_atomically(change):
    data = qpc_history()
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture-a", initial_remaining_wh=5)
    state.observe(data, session_id="capture-a")
    bad = deepcopy(data)
    if change == "missing":
        del bad["host_clock_identity"]
    elif change == "null":
        bad["host_clock_identity"] = None
    elif change == "legacy":
        bad["receive_clock"] = "host_monotonic_since_collector_start"
        bad["host_clock_identity"] = {"source": "python.monotonic", "session_id": None}
    elif change == "origin":
        bad["receive_origin_host_monotonic_s"] += 1
    else:
        key, value = {
            "session": ("session_id", "host-run-b"),
            "implementation": ("implementation", "other clock"),
            "resolution": ("resolution_ns", 200),
        }[change]
        bad["host_clock_identity"][key] = value
    with pytest.raises(ValueError):
        state.observe(bad, session_id="capture-a")
    assert state.observe(data, session_id="capture-a")["newly_consumed_wh"] == 0


def test_legacy_session_cannot_switch_to_qpc_even_at_same_origin():
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture-a", initial_remaining_wh=5)
    state.observe(cumulative_history(), session_id="capture-a")
    with pytest.raises(ValueError, match="clock changed"):
        state.observe(qpc_history(), session_id="capture-a")


def test_cumulative_energy_preserves_state_across_disarm_and_duplicate_observations():
    data = cumulative_history()
    prefix = deepcopy(data)
    prefix["samples"] = prefix["samples"][:3] + prefix["samples"][3:6]
    prefix["samples_seen"] = len(prefix["samples"])
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture-a", initial_remaining_wh=5)
    first = state.observe(prefix, session_id="capture-a")
    assert first["remaining_wh"] == pytest.approx(4.95)
    after = state.observe(data, session_id="capture-a")
    assert after["newly_consumed_wh"] == pytest.approx(0.005)
    assert after["remaining_wh"] == pytest.approx(4.945)
    assert after["battery_remaining_pct"] == pytest.approx(49.45)
    assert after["estimated_flight_time_s"] == pytest.approx(49.45)
    after["profile"]["capacity_wh"] = 99
    duplicate = state.observe(data, session_id="capture-a")
    assert duplicate["newly_consumed_wh"] == 0
    assert duplicate["remaining_wh"] == pytest.approx(4.945)
    assert duplicate["profile"]["capacity_wh"] == 10
    assert duplicate["authorizes_next_task"] is False


@pytest.mark.parametrize("change", ["session", "origin", "past", "shorter", "gap", "truncated"])
def test_failed_cumulative_updates_are_atomic(change):
    data = cumulative_history()
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture-a", initial_remaining_wh=5)
    state.observe(data, session_id="capture-a")
    bad = deepcopy(data)
    session = "capture-a"
    if change == "session":
        session = "capture-b"
    elif change == "origin":
        bad["receive_origin_host_monotonic_s"] = 200
    elif change == "past":
        bad["samples"][1]["value"] = 1
    elif change == "shorter":
        bad["samples"].pop()
        bad["samples_seen"] -= 1
    elif change == "truncated":
        bad["samples_dropped"] = 1
    else:
        bad["samples"].append(row("GLOBAL_POSITION_INT", "time_boot_ms", 4000, 4))
        bad["samples_seen"] += 1
    with pytest.raises(ValueError):
        state.observe(bad, session_id=session)
    assert state.observe(data, session_id="capture-a")["newly_consumed_wh"] == 0


def test_cumulative_depletion_is_not_a_physical_flight_claim():
    state = CumulativeSimulationEnergy(PROFILE, session_id="capture", initial_remaining_wh=0.01)
    result = state.observe(cumulative_history(), session_id="capture")
    assert result["remaining_wh"] == 0
    assert result["estimated_flight_time_s"] == 0
    assert result["unserved_energy_wh"] == pytest.approx(0.045)
    assert result["physical_calibration_claimed"] is False
