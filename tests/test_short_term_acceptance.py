from __future__ import annotations

import pytest

from multidetect.short_term_acceptance import (
    ShortTermTrackingAcceptanceConfig,
    run_short_term_tracking_acceptance,
)


def test_short_term_image_acceptance_recovers_same_id_with_bounded_cache() -> None:
    report = run_short_term_tracking_acceptance(
        ShortTermTrackingAcceptanceConfig(benchmark_frames=60)
    )

    assert report["track_count"] == 10
    assert report["retained_template_recovery_hint_observed"] is True
    assert report["recovered_same_track_id"] is True
    assert report["recovery_s"] == 0.0
    assert report["occlusion_duration_s"] == pytest.approx(13.0 / 30.0)
    assert report["status_counts"]["invalid"] == 0
    assert report["processing_latency_p95_ms"] <= 66.7
    assert report["end_to_end_rate_hz"] >= 15.0
    assert report["processed_update_rate_hz"] == pytest.approx(15.0)
    assert report["maximum_retained_template_count"] <= 16
    assert report["camera_opened"] is False
    assert report["model_inference_executed"] is False
    assert report["pixhawk_opened"] is False
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


def test_recovery_latency_does_not_include_hidden_time_at_25_hz() -> None:
    report = run_short_term_tracking_acceptance(
        ShortTermTrackingAcceptanceConfig(benchmark_frames=60, frame_rate_hz=25.0)
    )

    assert report["recovered_same_track_id"] is True
    assert report["recovery_s"] == 0.0
    assert report["occlusion_duration_s"] == pytest.approx(13.0 / 25.0)


@pytest.mark.parametrize(
    "changes",
    (
        {"track_count": 9},
        {"track_count": 17},
        {"benchmark_frames": 59},
        {"frame_stride": True},
        {"maximum_recovery_s": 0.0},
    ),
)
def test_short_term_image_acceptance_config_is_bounded(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ShortTermTrackingAcceptanceConfig(**changes)
