from __future__ import annotations

from pathlib import Path

from multidetect.acceptance import run_software_acceptance

ROOT = Path(__file__).resolve().parents[1]


def test_software_acceptance_covers_both_modes_and_person_veto() -> None:
    report = run_software_acceptance(ROOT)

    assert report["event"] == "software_acceptance_passed"
    assert report["patrol_only"] == {
        "alerts": 1,
        "authorization_challenges": 0,
        "fake_release_requests": 0,
        "final_phase": "searching",
    }
    assert report["fixed_wing_payload_hil"]["release_window"] == "ready"
    assert report["fixed_wing_payload_hil"]["advisory_only"] is True
    assert report["fixed_wing_payload_hil"]["fake_release_requests"] == 1
    assert report["fixed_wing_payload_hil"]["final_phase"] == "return_requested"
    assert report["g20_authorization"] == {
        "decision": "approve",
        "binding_accepted": True,
        "nonce_transmitted": False,
        "phase_after_approval": "deployment_ready",
        "fake_release_requests_after_approval": 0,
    }
    assert report["person_veto"]["authorization_challenges"] == 0
    assert report["person_veto"]["fake_release_requests"] == 0
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False
    assert report["model_training_executed"] is False
    assert report["model_inference_executed"] is False
