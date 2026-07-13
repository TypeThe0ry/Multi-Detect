from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.config import MissionConfig
from multidetect.deployment_planner import FixedWingReleaseWindowPlanner
from multidetect.domain import (
    BoundingBox,
    DeploymentWindowStatus,
    Detection,
    FrameObservation,
    MissionPhase,
    SensorKind,
    TrackSnapshot,
    VehicleTelemetry,
    Verdict,
)
from multidetect.mission import MissionController
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
    )
    return replace(telemetry, **changes)


def _frame(
    track: TrackSnapshot,
    *,
    telemetry: VehicleTelemetry | None = None,
    captured_at_s: float = 100.0,
) -> FrameObservation:
    return FrameObservation(
        frame_id="fixed-wing-frame",
        captured_at_s=captured_at_s,
        detections=(
            Detection("flame", 0.94, track.bbox, SensorKind.RGB, "rgb-v1"),
            Detection("hotspot", 0.92, track.bbox, SensorKind.THERMAL, "thermal-v1"),
        ),
        telemetry=telemetry or _telemetry(),
    )


def _planner(config: MissionConfig) -> FixedWingReleaseWindowPlanner:
    assert config.fixed_wing_release_window is not None
    return FixedWingReleaseWindowPlanner(
        config.fixed_wing_release_window,
        allowed_target_labels=config.target_classes,
    )


def test_fixed_wing_planner_computes_advisory_ready_window() -> None:
    config = _config()
    track = _track()

    solution = _planner(config).plan(track=track, frame=_frame(track), now_s=100.1)

    assert solution.status is DeploymentWindowStatus.READY
    assert solution.reasons == ("release_window_ready",)
    assert solution.release_lead_distance_m == pytest.approx(62.72, abs=0.1)
    assert solution.along_track_error_m == pytest.approx(0.15, abs=0.2)
    assert solution.cross_track_error_m == pytest.approx(0.0)
    assert solution.advisory_only is True
    assert solution.flight_control_enabled is False
    assert solution.physical_release_enabled is False


@pytest.mark.parametrize(
    ("bbox", "reason"),
    [
        (BoundingBox(0.45, 0.15, 0.55, 0.25), "before_release_window"),
        (BoundingBox(0.45, 0.70, 0.55, 0.80), "release_window_passed"),
        (BoundingBox(0.65, 0.40, 0.75, 0.50), "target_outside_cross_track_corridor"),
    ],
)
def test_fixed_wing_planner_waits_outside_release_corridor(
    bbox: BoundingBox,
    reason: str,
) -> None:
    config = _config()
    track = _track(bbox=bbox)

    solution = _planner(config).plan(track=track, frame=_frame(track), now_s=100.1)

    assert solution.status is DeploymentWindowStatus.WAIT
    assert reason in solution.reasons


def test_fixed_wing_planner_fails_closed_without_required_telemetry() -> None:
    config = _config()
    track = _track()
    frame = _frame(track, telemetry=_telemetry(ground_speed_mps=float("nan")))

    solution = _planner(config).plan(track=track, frame=frame, now_s=100.1)

    assert solution.status is DeploymentWindowStatus.UNAVAILABLE
    assert solution.reasons == ("required_telemetry_unavailable",)


def test_release_window_is_a_safety_gate_before_authorization() -> None:
    config = _config()
    ready_track = _track()
    ready = SafetyRuleEngine(config).evaluate(
        track=ready_track,
        frame=_frame(ready_track),
        now_s=100.1,
    )
    assert ready.allowed is True
    assert ready.deployment_window is not None
    assert ready.deployment_window.status is DeploymentWindowStatus.READY

    early_track = _track(bbox=BoundingBox(0.45, 0.15, 0.55, 0.25))
    early = SafetyRuleEngine(config).evaluate(
        track=early_track,
        frame=_frame(early_track),
        now_s=100.1,
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

    early_bbox = BoundingBox(0.45, 0.15, 0.55, 0.25)
    for index in range(4):
        captured_at_s = 96.0 + index
        frame = _frame(
            _track(bbox=early_bbox),
            captured_at_s=captured_at_s,
        )
        frame = replace(frame, frame_id=f"early-{index}")
        outcome = controller.process_observation(frame, now_s=captured_at_s)
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
        outcome = controller.process_observation(frame, now_s=captured_at_s)

    assert outcome.challenge is not None
    assert outcome.decisions[0].deployment_window is not None
    assert outcome.decisions[0].deployment_window.status is DeploymentWindowStatus.READY
    assert controller.fake_payload_port.request_count == 0
