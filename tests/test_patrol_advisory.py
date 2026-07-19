from __future__ import annotations

import math

import pytest

from multidetect.domain import BoundingBox, VehicleTelemetry
from multidetect.operator_status import build_patrol_status_message
from multidetect.patrol_advisory import (
    AdvisoryValidity,
    PatrolAdvisoryConfig,
    PatrolAdvisoryEngine,
    PatrolPhase,
    ReturnObserveDirection,
)
from multidetect.unified_tracking import UnifiedTrackSnapshot, UnifiedTrackState


def _track(
    state: UnifiedTrackState,
    *,
    track_id: str = "target-1",
    bbox: BoundingBox | None = None,
    last_seen_at_s: float = 10.0,
) -> UnifiedTrackSnapshot:
    bbox = bbox or BoundingBox(0.10, 0.20, 0.30, 0.50)
    return UnifiedTrackSnapshot(
        track_id=track_id,
        state=state,
        label="car",
        bbox=bbox,
        predicted_bbox=bbox,
        first_seen_at_s=1.0,
        last_seen_at_s=last_seen_at_s,
        state_changed_at_s=10.0,
        observation_count=20,
        missed_frame_count=1 if state is UnifiedTrackState.LOST else 0,
        confidence=0.9,
        tracking_quality=0.0 if state is UnifiedTrackState.LOST else 0.85,
        velocity_x_s=0.01,
        velocity_y_s=0.0,
        appearance_sample_count=4,
        last_appearance_distance=None,
        reid_confirmed=False,
        locked=True,
        primary=True,
        actionable=state not in {UnifiedTrackState.LOST, UnifiedTrackState.REACQUIRING},
    )


def _telemetry(
    *,
    ground_speed_mps: float = 20.0,
    position_healthy: bool | None = True,
    geofence_healthy: bool | None = True,
    link_healthy: bool | None = True,
) -> VehicleTelemetry:
    return VehicleTelemetry(
        altitude_agl_m=80.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=ground_speed_mps,
        in_allowed_zone=True,
        geofence_healthy=geofence_healthy,
        position_healthy=position_healthy,
        link_healthy=link_healthy,
        flight_mode_allows_deploy=False,
        release_zone_clear=None,
    )


def test_no_primary_target_remains_in_patrol_without_flight_output() -> None:
    assessment = PatrolAdvisoryEngine().assess(
        tracks=(),
        primary_target_id=None,
        telemetry=_telemetry(),
        now_s=12.0,
    )

    assert assessment.phase is PatrolPhase.PATROL
    assert assessment.return_to_observe is None
    assert assessment.advisory_only is True
    assert assessment.flight_control_enabled is False


@pytest.mark.parametrize(
    ("target_state", "expected_phase"),
    [
        (UnifiedTrackState.DETECTED, PatrolPhase.DETECTED),
        (UnifiedTrackState.LOCKED, PatrolPhase.LOCKED_MONITOR),
        (UnifiedTrackState.TRACKING, PatrolPhase.TRACKING),
        (UnifiedTrackState.RECOVERED, PatrolPhase.TRACKING),
        (UnifiedTrackState.OCCLUDED, PatrolPhase.OCCLUDED),
        (UnifiedTrackState.REACQUIRING, PatrolPhase.REACQUIRING),
    ],
)
def test_unified_track_states_map_to_read_only_patrol_phases(
    target_state: UnifiedTrackState,
    expected_phase: PatrolPhase,
) -> None:
    assessment = PatrolAdvisoryEngine().assess(
        tracks=(_track(target_state),),
        primary_target_id="target-1",
        telemetry=_telemetry(),
        now_s=10.2,
    )

    assert assessment.phase is expected_phase
    assert assessment.return_to_observe is None
    assert assessment.flight_control_enabled is False


def test_fresh_lost_target_generates_confirmed_sitl_only_left_revisit_advice() -> None:
    config = PatrolAdvisoryConfig(maximum_bank_angle_deg=25.0)
    assessment = PatrolAdvisoryEngine(config).assess(
        tracks=(_track(UnifiedTrackState.LOST, last_seen_at_s=10.0),),
        primary_target_id="target-1",
        telemetry=_telemetry(ground_speed_mps=20.0),
        now_s=10.4,
    )

    advisory = assessment.return_to_observe
    assert assessment.phase is PatrolPhase.LOST
    assert advisory is not None
    assert advisory.direction is ReturnObserveDirection.LEFT
    assert advisory.validity is AdvisoryValidity.VALID
    assert advisory.estimated_minimum_turn_radius_m == pytest.approx(
        20.0**2 / (9.80665 * math.tan(math.radians(25.0)))
    )
    assert advisory.operator_confirmation_required is True
    assert advisory.sitl_validation_required is True
    assert advisory.advisory_only is True
    assert advisory.flight_control_enabled is False


def test_stale_or_unhealthy_navigation_invalidates_revisit_advice() -> None:
    assessment = PatrolAdvisoryEngine().assess(
        tracks=(_track(UnifiedTrackState.LOST, last_seen_at_s=5.0),),
        primary_target_id="target-1",
        telemetry=_telemetry(position_healthy=False),
        now_s=10.0,
    )

    advisory = assessment.return_to_observe
    assert advisory is not None
    assert advisory.validity is AdvisoryValidity.INVALID
    assert "last target evidence is stale" in advisory.reasons
    assert "position health is false" in advisory.reasons
    assert advisory.flight_control_enabled is False


def test_unknown_health_low_speed_and_centered_target_produce_degraded_route_advice() -> None:
    assessment = PatrolAdvisoryEngine().assess(
        tracks=(
            _track(
                UnifiedTrackState.LOST,
                bbox=BoundingBox(0.40, 0.20, 0.60, 0.50),
                last_seen_at_s=10.0,
            ),
        ),
        primary_target_id="target-1",
        telemetry=_telemetry(
            ground_speed_mps=2.0,
            position_healthy=None,
            geofence_healthy=None,
            link_healthy=None,
        ),
        now_s=10.3,
    )

    advisory = assessment.return_to_observe
    assert advisory is not None
    assert advisory.validity is AdvisoryValidity.DEGRADED
    assert advisory.direction is ReturnObserveDirection.ROUTE_REQUIRED
    assert advisory.estimated_minimum_turn_radius_m is None
    assert advisory.flight_control_enabled is False


def test_missing_primary_record_never_infers_a_revisit_direction() -> None:
    assessment = PatrolAdvisoryEngine().assess(
        tracks=(_track(UnifiedTrackState.TRACKING, track_id="other"),),
        primary_target_id="missing",
        telemetry=_telemetry(),
        now_s=10.0,
    )

    assert assessment.phase is PatrolPhase.LOST
    assert assessment.primary_target_id == "missing"
    assert assessment.return_to_observe is None
    assert "unavailable" in assessment.reason


def test_qgc_patrol_status_builder_carries_read_only_pool_and_revisit_metadata() -> None:
    track = _track(UnifiedTrackState.LOST, last_seen_at_s=10.0)
    assessment = PatrolAdvisoryEngine().assess(
        tracks=(track,),
        primary_target_id=track.track_id,
        telemetry=_telemetry(),
        now_s=10.4,
    )

    status = build_patrol_status_message(
        mission_id="fire-patrol-demo",
        sequence=8,
        assessment=assessment,
        tracks=(track,),
        source_frame_id="frame-8",
        source_captured_at_s=10.35,
        produced_at_s=10.4,
    )

    assert status.phase is PatrolPhase.LOST
    assert status.primary_target_id == track.track_id
    assert status.target_state is UnifiedTrackState.LOST
    assert status.bbox == track.bbox
    assert status.total_track_count == 1
    assert status.locked_track_count == 1
    assert status.return_direction is ReturnObserveDirection.LEFT
    assert status.operator_confirmation_required is True
    assert status.sitl_validation_required is True
    assert status.advisory_only is True
    assert status.flight_control_enabled is False
