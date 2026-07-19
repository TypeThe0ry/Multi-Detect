from __future__ import annotations

import pytest

from multidetect.domain import VehicleTelemetry
from multidetect.patrol_reacquisition_acceptance import (
    PatrolReacquisitionAcceptanceConfig,
    run_patrol_reacquisition_acceptance,
)


def _telemetry(*, link_healthy: bool | None = True) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=60.0,
        roll_deg=1.0,
        pitch_deg=-2.0,
        ground_speed_mps=20.0,
        in_allowed_zone=None,
        geofence_healthy=None,
        position_healthy=True,
        link_healthy=link_healthy,
        flight_mode_allows_deploy=None,
        release_zone_clear=None,
        armed=True,
        flight_mode="MISSION",
        mission_sequence=2,
    )


def test_patrol_reacquisition_acceptance_preserves_identity_and_is_read_only() -> None:
    report = run_patrol_reacquisition_acceptance(_telemetry())

    assert report["track_count"] == 10
    assert report["state_sequence"] == [
        "detected",
        "locked",
        "tracking",
        "occluded",
        "reacquiring",
        "recovered",
    ]
    assert report["lost_branch_state_sequence"] == [
        "tracking",
        "occluded",
        "reacquiring",
        "lost",
    ]
    assert report["short_occlusion_recovery_s"] <= 0.5
    assert report["lost_reacquisition_s"] <= 2.0
    assert report["same_identity_after_short_occlusion"] is True
    assert report["same_identity_after_lost_reid"] is True
    assert report["reid_confirmed_after_lost"] is True
    assert report["background_lock_retained"] is True
    assert report["primary_switch_latency_ms"] <= 200.0
    revisit = report["return_to_observe"]
    assert revisit["phase"] == "lost"
    assert revisit["direction"] == "left"
    assert revisit["validity"] == "degraded"
    assert revisit["estimated_minimum_turn_radius_m"] is not None
    assert revisit["operator_confirmation_required"] is True
    assert revisit["sitl_validation_required"] is True
    assert revisit["advisory_only"] is True
    assert revisit["flight_control_enabled"] is False
    assert report["camera_opened"] is False
    assert report["model_inference_executed"] is False
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


def test_unhealthy_link_invalidates_but_never_removes_revisit_safety_gates() -> None:
    report = run_patrol_reacquisition_acceptance(_telemetry(link_healthy=False))

    revisit = report["return_to_observe"]
    assert revisit["validity"] == "invalid"
    assert "data link health is false" in revisit["reasons"]
    assert revisit["operator_confirmation_required"] is True
    assert revisit["flight_control_enabled"] is False


@pytest.mark.parametrize("track_count", (9, 65, True))
def test_patrol_reacquisition_acceptance_rejects_invalid_pool_size(track_count: object) -> None:
    with pytest.raises(ValueError):
        PatrolReacquisitionAcceptanceConfig(track_count=track_count)  # type: ignore[arg-type]
