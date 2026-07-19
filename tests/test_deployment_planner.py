from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.deployment_planner import (
    FixedWingReleaseWindowPlanner,
    PrimaryRangeEvidence,
)
from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    Detection,
    FrameObservation,
    MissionPhase,
    ReleaseTimingStatus,
    SensorKind,
    TrackSnapshot,
    VehicleTelemetry,
    Verdict,
)
from multidetect.mission import MissionController
from multidetect.multimodal_ranging import RangeSolution, RangeValidity
from multidetect.safety import SafetyRuleEngine

ROOT = Path(__file__).resolve().parents[1]


def _config() -> MissionConfig:
    return MissionConfig.from_json(ROOT / "configs/missions/fire_suppression_fixed_wing.demo.json")


def _track(*, bbox: BoundingBox | None = None, label: str = "flame") -> TrackSnapshot:
    return TrackSnapshot(
        track_id="fire-track-1",
        revision=4,
        label=label,
        bbox=bbox or BoundingBox(0.45, 0.40, 0.55, 0.50),
        first_seen_at_s=96.0,
        last_seen_at_s=100.0,
        observation_count=5,
        consecutive_observations=5,
        confidence_floor=0.90,
        confidence_mean=0.93,
        maximum_gap_s=1.0,
        area_growth_rate=0.0,
        thermal_corroborated=True,
        confirmed=True,
        independent_rgb_corroborated=True,
    )


def _telemetry(**changes: object) -> VehicleTelemetry:
    telemetry = VehicleTelemetry(
        altitude_agl_m=40.0,
        roll_deg=0.0,
        pitch_deg=0.0,
        ground_speed_mps=18.0,
        in_allowed_zone=True,
        geofence_healthy=True,
        position_healthy=True,
        link_healthy=True,
        flight_mode_allows_deploy=True,
        release_zone_clear=True,
        person_detector_healthy=True,
        heading_deg=0.0,
        velocity_north_mps=18.0,
        velocity_east_mps=0.0,
        airspeed_mps=16.0,
        wind_north_mps=2.0,
        wind_east_mps=0.0,
        velocity_observed_at_s=100.0,
        airspeed_observed_at_s=100.0,
        wind_observed_at_s=100.0,
    )
    return replace(telemetry, **changes)


def _frame(
    track: TrackSnapshot,
    *,
    telemetry: VehicleTelemetry | None = None,
    captured_at_s: float = 100.0,
) -> FrameObservation:
    resolved_telemetry = telemetry or _telemetry(
        velocity_observed_at_s=captured_at_s,
        airspeed_observed_at_s=captured_at_s,
        wind_observed_at_s=captured_at_s,
    )
    return FrameObservation(
        frame_id="fixed-wing-frame",
        captured_at_s=captured_at_s,
        detections=(
            Detection(
                "flame",
                0.94,
                track.bbox,
                SensorKind.RGB,
                "rgb-v1",
                metadata={
                    "independent_rgb_corroborated": True,
                    "independent_rgb_evidence_contract_version": 1,
                    "independent_rgb_iou": 0.9,
                    "independent_rgb_confidence": 0.9,
                    "independent_rgb_label": "flame",
                    "independent_rgb_verifier_model_version": "rgb-verifier-v1",
                    "independent_rgb_primary_artifact_sha256": "1" * 64,
                    "independent_rgb_verifier_artifact_sha256": "2" * 64,
                },
            ),
        ),
        telemetry=resolved_telemetry,
    )


def _planner(config: MissionConfig) -> FixedWingReleaseWindowPlanner:
    assert config.fixed_wing_release_window is not None
    return FixedWingReleaseWindowPlanner(
        config.fixed_wing_release_window,
        allowed_target_labels=config.target_classes,
    )


def _range_evidence(
    track: TrackSnapshot,
    *,
    frame_id: str = "fixed-wing-frame",
    captured_at_s: float = 100.0,
    north_m: float = 45.1,
    east_m: float = 0.0,
    validity: RangeValidity = RangeValidity.VALID,
    sensor_consistency: float = 0.9,
) -> PrimaryRangeEvidence:
    ground_range_m = (north_m * north_m + east_m * east_m) ** 0.5
    bearing_deg = 0.0 if ground_range_m == 0.0 else math.degrees(math.atan2(east_m, north_m))
    distance_values = (
        {
            "slant_range_m": ground_range_m,
            "ground_range_m": ground_range_m,
            "slant_range_ci95_m": (max(0.0, ground_range_m - 0.6), ground_range_m + 0.6),
            "ground_range_ci95_m": (
                max(0.0, ground_range_m - 0.6),
                ground_range_m + 0.6,
            ),
        }
        if validity is not RangeValidity.INVALID
        else {}
    )
    solution = RangeSolution(
        target_id="unified-fire-1",
        frame_id=frame_id,
        calibration_id="camera-bench-v1",
        evaluated_at_s=captured_at_s,
        validity=validity,
        reasons=(
            (
                "multimodal_range_consistent"
                if validity is RangeValidity.VALID
                else "single_absolute_range_method"
            ),
        ),
        sources=("camera_ground", "laser"),
        rejected_sources=(),
        relative_bearing_deg=bearing_deg,
        absolute_bearing_deg=bearing_deg % 360.0,
        bearing_sigma_deg=0.5,
        north_offset_m=north_m,
        east_offset_m=east_m,
        data_freshness_s=0.05,
        sensor_consistency=sensor_consistency,
        **distance_values,
    )
    return PrimaryRangeEvidence(
        source_target_id="unified-fire-1",
        source_frame_id=frame_id,
        source_captured_at_s=captured_at_s,
        source_label=track.label,
        source_bbox=track.bbox,
        solution=solution,
    )


def test_fixed_wing_planner_computes_advisory_ready_window() -> None:
    config = _config()
    track = _track()

    solution = _planner(config).plan(
        track=track,
        frame=_frame(track),
        now_s=100.1,
        ranging_evidence=_range_evidence(track),
    )

    assert solution.status is DeploymentWindowStatus.READY
    assert solution.timing_status is ReleaseTimingStatus.WINDOW
    assert solution.reasons == ("multimodal_release_window_ready",)
    assert solution.release_lead_distance_m == pytest.approx(45.11, abs=0.1)
    assert solution.along_track_error_m == pytest.approx(-0.015, abs=0.1)
    assert solution.cross_track_error_m == pytest.approx(0.0)
    assert solution.error_ellipse_major_m == pytest.approx(4.82, abs=0.1)
    assert solution.error_ellipse_minor_m == pytest.approx(3.35, abs=0.1)
    assert solution.advisory_only is True
    assert solution.flight_control_enabled is False
    assert solution.physical_release_enabled is False


@pytest.mark.parametrize(
    ("north_m", "east_m", "timing_status", "reason"),
    [
        (55.0, 0.0, ReleaseTimingStatus.TOO_EARLY, "before_release_window"),
        (35.0, 0.0, ReleaseTimingStatus.TOO_LATE, "release_window_passed"),
        (45.1, 7.0, ReleaseTimingStatus.INVALID, "target_outside_cross_track_corridor"),
    ],
)
def test_fixed_wing_planner_waits_outside_release_corridor(
    north_m: float,
    east_m: float,
    timing_status: ReleaseTimingStatus,
    reason: str,
) -> None:
    config = _config()
    track = _track()

    solution = _planner(config).plan(
        track=track,
        frame=_frame(track),
        now_s=100.1,
        ranging_evidence=_range_evidence(track, north_m=north_m, east_m=east_m),
    )

    assert solution.status is DeploymentWindowStatus.WAIT
    assert solution.timing_status is timing_status
    assert reason in solution.reasons


def test_fixed_wing_planner_fails_closed_without_required_telemetry() -> None:
    config = _config()
    track = _track()
    frame = _frame(track, telemetry=_telemetry(airspeed_mps=float("nan")))

    solution = _planner(config).plan(
        track=track,
        frame=frame,
        now_s=100.1,
        ranging_evidence=_range_evidence(track),
    )

    assert solution.status is DeploymentWindowStatus.UNAVAILABLE
    assert solution.timing_status is ReleaseTimingStatus.INVALID
    assert solution.reasons == ("ballistic_telemetry_unavailable",)


def test_fixed_wing_planner_fails_closed_without_valid_bound_range() -> None:
    config = _config()
    track = _track()
    planner = _planner(config)

    missing = planner.plan(track=track, frame=_frame(track), now_s=100.1)
    degraded = planner.plan(
        track=track,
        frame=_frame(track),
        now_s=100.1,
        ranging_evidence=_range_evidence(track, validity=RangeValidity.DEGRADED),
    )
    mismatched = planner.plan(
        track=track,
        frame=_frame(track),
        now_s=100.1,
        ranging_evidence=replace(
            _range_evidence(track),
            source_bbox=BoundingBox(0.0, 0.0, 0.1, 0.1),
        ),
    )

    assert missing.reasons == ("multimodal_range_evidence_unavailable",)
    assert degraded.reasons == ("multimodal_range_not_valid",)
    assert mismatched.reasons == ("range_target_spatial_binding_failed",)


def test_release_window_is_a_safety_gate_before_authorization() -> None:
    config = _config()
    ready_track = _track()
    ready = SafetyRuleEngine(config).evaluate(
        track=ready_track,
        frame=_frame(ready_track),
        now_s=100.1,
        ranging_evidence=_range_evidence(ready_track),
    )
    assert ready.allowed is True
    assert ready.deployment_window is not None
    assert ready.deployment_window.status is DeploymentWindowStatus.READY

    early_track = _track()
    early = SafetyRuleEngine(config).evaluate(
        track=early_track,
        frame=_frame(early_track),
        now_s=100.1,
        ranging_evidence=_range_evidence(early_track, north_m=55.0),
    )
    window_check = next(
        check for check in early.checks if check.rule_id == "deployment.fixed_wing_release_window"
    )
    assert early.allowed is False
    assert window_check.verdict is Verdict.DENY


def test_fixed_wing_mission_creates_challenge_only_inside_release_window() -> None:
    config = _config()
    controller = MissionController(config)
    controller.launch(now_s=95.0)
    controller.arrive_task_area(now_s=95.1)

    early_bbox = BoundingBox(0.45, 0.40, 0.55, 0.50)
    for index in range(4):
        captured_at_s = 96.0 + index
        frame = _frame(
            _track(bbox=early_bbox),
            captured_at_s=captured_at_s,
        )
        frame = replace(frame, frame_id=f"early-{index}")
        outcome = controller.process_observation(
            frame,
            now_s=captured_at_s,
            primary_range_evidence=_range_evidence(
                _track(bbox=early_bbox),
                frame_id=frame.frame_id,
                captured_at_s=captured_at_s,
                north_m=55.0,
            ),
        )
    assert outcome.challenge is None
    assert controller.state.phase is MissionPhase.SEARCHING

    ready_bbox = BoundingBox(0.45, 0.40, 0.55, 0.50)
    for index in range(5):
        captured_at_s = 101.0 + index
        frame = _frame(
            _track(bbox=ready_bbox),
            captured_at_s=captured_at_s,
        )
        frame = replace(frame, frame_id=f"ready-{index}")
        outcome = controller.process_observation(
            frame,
            now_s=captured_at_s,
            primary_range_evidence=_range_evidence(
                _track(bbox=ready_bbox),
                frame_id=frame.frame_id,
                captured_at_s=captured_at_s,
            ),
        )

    assert outcome.challenge is not None
    assert outcome.decisions[0].deployment_window is not None
    assert outcome.decisions[0].deployment_window.status is DeploymentWindowStatus.READY
    assert outcome.decisions[0].deployment_window.timing_status is ReleaseTimingStatus.WINDOW
    assert controller.fake_payload_port.request_count == 0
