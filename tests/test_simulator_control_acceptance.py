from __future__ import annotations

from scripts.run_simulator_control_acceptance import run_acceptance


def test_simulator_control_acceptance_fixture_passes() -> None:
    result = run_acceptance()
    assert result["gate_passed"] is True
    assert result["manual_took_over"] is True
    assert result["ai_blocked_after_manual"] is True
    assert result["watchdog_failsafe"] is True
