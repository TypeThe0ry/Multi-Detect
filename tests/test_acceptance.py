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
    assert report["manual_target_tracking"] == {
        "selected_state": "tracking",
        "selected_label": "flame",
        "person_track_ignored": True,
        "continuous_update_state": "tracking",
        "lost_state": "lost",
        "reacquired_state": "tracking",
        "reacquired_with_new_track_id": True,
        "reacquisition_timeout_state": "rejected",
        "manual_tracker_state_without_detection": "tracking",
        "manual_tracker_bbox_changed": True,
        "detector_initial_state": "initializing",
        "detector_reacquired_after_manual_hint": "tracking",
        "selection_guard_accepted": True,
        "selection_replay_rejected": True,
        "selection_is_payload_authorization": False,
    }
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False
    assert report["model_training_executed"] is False
    assert report["model_inference_executed"] is False
