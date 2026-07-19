from __future__ import annotations

from dataclasses import replace

import pytest

from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.fixed_camera_observation import (
    FixedCameraObservationEngine,
    FixedCameraObservationState,
)
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


def _track(
    *, bbox: BoundingBox, locked: bool = True, actionable: bool = True
) -> UnifiedTrackSnapshot:
    return UnifiedTrackSnapshot(
        track_id="track-1",
        state=UnifiedTrackState.TRACKING,
        label="vehicle",
        bbox=bbox,
        predicted_bbox=bbox,
        first_seen_at_s=0.0,
        last_seen_at_s=1.0,
        state_changed_at_s=0.5,
        observation_count=10,
        missed_frame_count=0,
        confidence=0.9,
        tracking_quality=0.9,
        velocity_x_s=0.0,
        velocity_y_s=0.0,
        appearance_sample_count=0,
        last_appearance_distance=None,
        reid_confirmed=False,
        locked=locked,
        primary=locked,
        actionable=locked and actionable,
    )


def _telemetry() -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=50.0,
        roll_deg=2.0,
        pitch_deg=-1.0,
        heading_deg=361.0,
        ground_speed_mps=20.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=False,
        release_zone_clear=False,
        attitude_observed_at_s=1.05,
    )


def test_primary_lck_uses_fixed_optical_axis_and_real_attitude() -> None:
    result = FixedCameraObservationEngine().evaluate(
        track=_track(bbox=BoundingBox(0.65, 0.35, 0.85, 0.55)),
        telemetry=_telemetry(),
        now_s=1.10,
    )

    assert result.state is FixedCameraObservationState.LCK
    assert result.reason == "optical_axis_offset"
    assert result.error_x_fraction == 0.25
    assert result.error_y_fraction == pytest.approx(-0.05)
    assert result.heading_deg == 1.0
    assert result.fixed_camera is True


def test_centered_lck_is_aligned_and_trk_never_becomes_lck() -> None:
    engine = FixedCameraObservationEngine()
    centered = engine.evaluate(
        track=_track(bbox=BoundingBox(0.48, 0.48, 0.52, 0.52)),
        telemetry=_telemetry(),
        now_s=1.10,
    )
    trk = engine.evaluate(
        track=_track(bbox=BoundingBox(0.48, 0.48, 0.52, 0.52), locked=False),
        telemetry=_telemetry(),
        now_s=1.10,
    )

    assert centered.state is FixedCameraObservationState.ALIGNED and centered.aligned
    assert trk.state is FixedCameraObservationState.TRK and not trk.aligned


def test_stale_target_or_attitude_fails_closed() -> None:
    engine = FixedCameraObservationEngine()
    track = _track(bbox=BoundingBox(0.4, 0.4, 0.6, 0.6))

    assert engine.evaluate(track=track, telemetry=_telemetry(), now_s=1.5).reason == "target_stale"
    stale_attitude = replace(_telemetry(), attitude_observed_at_s=0.0)
    assert engine.evaluate(track=track, telemetry=stale_attitude, now_s=1.1).reason == (
        "attitude_stale"
    )
