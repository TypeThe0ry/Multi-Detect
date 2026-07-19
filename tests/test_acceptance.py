from __future__ import annotations

from pathlib import Path

import pytest

from multidetect.acceptance import run_software_acceptance
from multidetect.unified_acceptance import UnifiedTrackingAcceptanceConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "changes",
    (
        {"track_count": 9},
        {"track_count": 65},
        {"benchmark_frames": 29},
        {"benchmark_frames": True},
    ),
)
def test_unified_tracking_acceptance_bounds_workload(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        UnifiedTrackingAcceptanceConfig(**changes)


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
    unified = report["unified_tracking"]
    assert unified["track_count"] == 10
    assert unified["background_locked_track_count"] == 1
    assert unified["primary_switch_latency_ms"] <= 200.0
    assert unified["maximum_repeated_switch_latency_ms"] <= 200.0
    assert unified["repeated_primary_switch_count"] >= 2
    assert unified["association_latency_p95_ms"] > 0.0
    assert unified["association_latency_p99_ms"] >= unified["association_latency_p95_ms"]
    assert unified["association_latency_maximum_ms"] >= unified["association_latency_p99_ms"]
    assert unified["benchmark_frame_count"] == 300
    assert unified["benchmark_elapsed_s"] > 0.0
    assert unified["measured_end_to_end_metadata_rate_hz"] >= 15.0
    assert unified["sustained_metadata_rate_hz_at_p95"] >= 15.0
    assert unified["repeated_switch_latency_p95_ms"] <= 200.0
    assert unified["short_occlusion"]["same_track_id"] is True
    assert unified["short_occlusion"]["recovery_s"] <= 0.5
    assert unified["lost_without_reid"] == {
        "original_state": "lost",
        "new_track_created": True,
        "same_track_id": False,
        "reid_confirmed": False,
    }
    assert unified["lost_with_strong_reid"]["same_track_id"] is True
    assert unified["lost_with_strong_reid"]["reid_confirmed"] is True
    assert unified["lost_with_strong_reid"]["recovery_s"] <= 2.0
    assert unified["crossing_identity"] == {
        "identity_switch_count": 0,
        "occluded_track_recovered": True,
        "background_locks_retained": True,
        "detector_order_reversed": True,
    }
    assert unified["ambiguous_identity"] == {
        "identity_forced": False,
        "blocked_candidate_count": 2,
        "original_state": "lost",
        "new_track_count": 2,
    }
    assert unified["kalman_prediction"]["model"] == "constant_velocity_kalman"
    assert (
        unified["kalman_prediction"]["prediction_error_normalized"]
        <= unified["kalman_prediction"]["maximum_error_normalized"]
    )
    assert unified["global_assignment"] == {
        "algorithm": "cascaded_rectangular_hungarian",
        "greedy_cost": 1.0,
        "global_cost": 0.31,
        "selected_pairs": [["track-a", 1], ["track-b", 0]],
    }
    assert unified["confidence_cascade"] == {
        "low_confidence_track_continuation": True,
        "low_confidence_new_identity_created": False,
        "suppressed_new_identity_count": 1,
        "minimum_association_confidence": 0.10,
        "minimum_new_track_confidence": 0.35,
        "high_confidence_threshold": 0.55,
    }
    assert unified["ambiguous_identity_forced"] is False
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False
    assert report["model_training_executed"] is False
    assert report["model_inference_executed"] is False
