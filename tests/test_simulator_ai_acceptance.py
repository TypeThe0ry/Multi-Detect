from __future__ import annotations

from scripts.run_simulator_ai_acceptance import run_acceptance


def test_simulator_ai_acceptance_fixture_passes() -> None:
    result = run_acceptance()
    assert result["gate_passed"] is True
    assert result["claims"]["reasoned_policy_used"] is True
    assert result["claims"]["shared_manual_ai_authority"] is True
    assert result["claims"]["truth_free_input"] is True
    assert [item["mode"] for item in result["decision_sequence"]] == [
        "hover",
        "track",
        "suppressed",
        "return",
        "failsafe",
    ]
