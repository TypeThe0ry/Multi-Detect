from __future__ import annotations

from pathlib import Path

from multidetect.payload_target_acceptance import run_mode2_payload_hil_acceptance

ROOT = Path(__file__).resolve().parents[1]


def test_mode2_payload_hil_closes_signed_loop_and_fails_closed() -> None:
    report = run_mode2_payload_hil_acceptance(ROOT)

    assert report["event"] == "mode2_payload_hil_acceptance_passed"
    assert report["positive_fire"] == {
        "eligibility": "eligible_fire",
        "aimpoint_is_confirmed_fire": True,
        "selection_acknowledged": True,
        "continuous_slide_acknowledged": True,
        "payload_status_received": True,
        "authorization_acknowledged": True,
        "authorization_was_separate": True,
        "fake_release_requests": 1,
        "remaining_payloads": 2,
        "final_phase": "searching",
    }
    transport = report["transport"]
    assert transport["loopback_udp"] is True
    assert transport["hmac_authenticated"] is True
    assert transport["mavlink2_signed"] is True
    assert 1 <= transport["payload_delivery_attempts"] <= 3
    assert 1 <= transport["authorization_delivery_attempts"] <= 3
    assert transport["round_trip_session_elapsed_ms"] > 0.0
    assert report["negative_cases"] == {
        "person_eligibility": "target_not_payload_eligible",
        "ordinary_vehicle_eligibility": "fire_evidence_unavailable",
        "target_switch_revoked": True,
        "expired_slide_revoked": True,
        "person_entry_after_slide_vetoed": True,
    }
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False
    assert report["real_payload_interface_present"] is False
    assert report["model_training_executed"] is False
    assert report["model_inference_executed"] is False
