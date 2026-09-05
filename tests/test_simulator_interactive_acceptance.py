from __future__ import annotations

from multidetect.simulator.interactive_acceptance import run_acceptance


def test_native_qgc_interactive_acceptance_covers_both_vehicle_modes() -> None:
    result = run_acceptance()

    assert result["gate_passed"] is True
    assert [item["mode"] for item in result["modes"]] == ["vtol", "fixed-wing"]
    for item in result["modes"]:
        assert item["gate_passed"] is True
        gate = item["gate"]
        assert gate["ai_track_accepted"] is True
        assert gate["ai_advanced_vehicle"] is True
        assert gate["qgc_manual_accepted"] is True
        assert gate["manual_took_over"] is True
        assert gate["manual_advanced_vehicle"] is True
        assert gate["manual_suppressed_ai"] is True
        assert gate["ai_resumed_after_release"] is True
        assert gate["watchdog_failsafe"] is True
        assert gate["neutral_watchdog_frame"] is True
        assert gate["telemetry_forwarded_byte_for_byte"] is True
        assert gate["readonly_qgc_frame_forwarded"] is True
        assert gate["mission_write_blocked"] is True
        assert gate["truth_free_closed_loop"] is True
        assert gate["loopback_and_identity_counters"] is True

        router = item["router"]
        assert router["qgc_manual_frames_accepted"] == 1
        assert router["qgc_read_only_frames_forwarded"] == 1
        assert router["qgc_flight_commands_allowed"] is False
        assert router["qgc_mission_writes_allowed"] is False
        assert router["px4_frames_forwarded_to_qgc"] == 1
        assert router["telemetry_frames_rewritten"] is False
        assert router["literal_loopback_only"] is True
        assert router["manual_watchdog_s"] == 0.5
        assert router["manual_watchdog_neutral_count"] == 0


def test_interactive_acceptance_can_run_one_mode() -> None:
    result = run_acceptance(mode="fixed_wing")
    assert result["gate_passed"] is True
    assert [item["mode"] for item in result["modes"]] == ["fixed-wing"]


def test_interactive_acceptance_can_run_multirotor_mode() -> None:
    result = run_acceptance(mode="quadcopter")
    assert result["gate_passed"] is True
    assert [item["mode"] for item in result["modes"]] == ["multirotor"]
    assert result["modes"][0]["gate"]["ai_advanced_vehicle"] is True


def test_interactive_acceptance_all_covers_three_airframe_modes() -> None:
    result = run_acceptance(mode="all")
    assert result["gate_passed"] is True
    assert [item["mode"] for item in result["modes"]] == [
        "vtol",
        "fixed-wing",
        "multirotor",
    ]


def test_interactive_acceptance_is_reproducible_and_descriptor_bound() -> None:
    first = run_acceptance(mode="vtol")
    second = run_acceptance(mode="vtol")

    assert first == second
    item = first["modes"][0]
    descriptor = item["runtime_descriptor"]
    assert descriptor["mode"] == "sim"
    assert descriptor["deterministic"] is True
    assert descriptor["scenario_id"] == "interactive-acceptance-vtol"
    assert item["runtime_descriptor_hash"] == item["closed_loop"]["runtime_descriptor_hash"]
    assert item["router"]["started_monotonic_s"] == 0.0
