from __future__ import annotations

from multidetect.approach_acceptance import run_mode3_approach_hil_acceptance


def test_mode3_arbitrary_targets_reach_advice_and_unsafe_evidence_aborts() -> None:
    report = run_mode3_approach_hil_acceptance()

    assert report["event"] == "mode3_approach_hil_acceptance_passed"
    for label in ("vehicle", "person"):
        result = report["arbitrary_targets"][label]
        assert result["label"] == label
        assert result["selection_acknowledged"] is True
        assert result["continuous_slide_acknowledged"] is True
        assert result["phases_after_slide"] == [
            "centering",
            "centering",
            "aiming",
        ]
        assert result["final_phase"] == "aiming"
        assert result["status_received"] is True
        assert result["advisory_only"] is True
        assert result["flight_control_enabled"] is False
        assert result["physical_release_enabled"] is False
        assert result["session_elapsed_ms"] > 0.0

    aborts = report["abort_cases"]
    assert aborts["occluded"]["reason"] == "target_occluded"
    assert aborts["lost"]["reason"] == "target_lost"
    assert aborts["avoidance_avoid"]["reason"] == "avoidance_avoid"
    assert aborts["avoidance_invalid"]["reason"] == "avoidance_invalid"
    for name in ("occluded", "lost", "avoidance_avoid", "avoidance_invalid"):
        assert aborts[name]["abort_phase"] == "abort"
        assert aborts[name]["bounded_climb_advice"] == 8.0
        assert aborts[name]["blind_approach_continued"] is False
    for name in ("occluded", "lost"):
        assert aborts[name]["abort_latched"] is False
        assert aborts[name]["rearmed_with_fresh_challenge"] is True
        assert aborts[name]["recovery_phase"] == "slide_confirm_required"
    for name in ("avoidance_avoid", "avoidance_invalid"):
        assert aborts[name]["abort_latched"] is True
        assert aborts[name]["rearmed_with_fresh_challenge"] is False
        assert aborts[name]["recovery_phase"] == "abort"
    assert aborts["target_switch_requires_new_slide"] is True
    assert aborts["old_confirmation_rejected"] is True
    assert report["advisory_only"] is True
    assert report["sitl_hil_only"] is True
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False
    assert report["real_actuator_interface_present"] is False
