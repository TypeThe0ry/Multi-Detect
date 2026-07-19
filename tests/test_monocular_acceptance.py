from __future__ import annotations

import pytest

from multidetect.monocular_acceptance import (
    MonocularAvoidanceAcceptanceConfig,
    run_monocular_avoidance_acceptance,
)


def test_image_level_avoidance_acceptance_exercises_risk_and_compensation() -> None:
    report = run_monocular_avoidance_acceptance(
        MonocularAvoidanceAcceptanceConfig(benchmark_frames=60)
    )

    assert report["static_scene_state"] == "clear"
    assert report["camera_translation_state"] == "clear"
    assert report["camera_motion_dx"] > 0.0
    assert report["camera_motion_confidence"] >= 0.5
    assert report["approaching_obstacle_state"] == "avoid"
    assert report["approaching_center_zone_state"] == "avoid"
    assert report["stale_evidence_state"] == "invalid"
    assert report["stale_evidence_reason"] == "STALE_FRAME"
    assert report["state_counts"]["avoid"] > 0
    assert report["state_counts"]["clear"] > 0
    assert report["state_counts"]["invalid"] == 0
    assert report["processing_latency_p95_ms"] <= 66.7
    assert report["end_to_end_rate_hz"] >= 15.0
    assert report["all_outputs_advisory_only"] is True
    assert report["metric_depth_available"] is False
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("benchmark_frames", 59),
        ("analysis_width", 159),
        ("frame_rate_hz", 0.0),
        ("maximum_processing_latency_p95_ms", float("nan")),
        ("minimum_end_to_end_rate_hz", -1.0),
    ),
)
def test_avoidance_acceptance_config_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        MonocularAvoidanceAcceptanceConfig(**{field: value})
