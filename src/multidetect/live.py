from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from .adaptive_ranging import AdaptiveRangingPolicy
from .aircraft_appearance import HandcraftedAircraftAppearanceEncoder
from .alerts import AlertPublisher, RecordingAlertPublisher, SqliteAlertOutbox
from .appearance_reid import OnnxPersonReIdEncoder
from .approach_hil import ApproachHilPhase
from .approach_live import LiveApproachHilCoordinator, LiveApproachHilFrame
from .attitude_camera_motion import AttitudeCameraMotionEstimator
from .deployment_planner import PrimaryRangeEvidence
from .depth_grid_udp import DepthGridUdpPublisher
from .domain import (
    BoundingBox,
    DeploymentWindowSolution,
    Detection,
    FireAlert,
    FrameObservation,
    MissionPhase,
    TrackSnapshot,
    VehicleTelemetry,
)
from .evaluation import JsonlPredictionWriter
from .fixed_camera_observation import FixedCameraObservationEngine
from .fixed_wing_aim_control import (
    FixedWingAimExecutor,
    FixedWingAimState,
    FixedWingAimTarget,
)
from .manual_tracking import OpenCVManualTargetTracker
from .metric_depth import AsyncMetricDepthRunner, MetricDepthResult
from .mission import MissionController, ObservationOutcome
from .monocular_avoidance import (
    CollisionRiskState,
    MonocularAvoidanceAssessment,
    MonocularCollisionRiskEvaluator,
    OpenCVSparseFlowAvoidance,
)
from .multimodal_ranging import (
    AircraftPose,
    CameraCalibration,
    MultiModalRangingEngine,
    RangeSolution,
    RangeValidity,
    TargetImageObservation,
    VerticalMeasurement,
    VerticalSource,
)
from .operator_bridge import LiveOperatorBridge
from .operator_link import (
    AuthorizationChallengeStatusMessage,
    AuthorizationDecision,
    AuthorizationDecisionCommand,
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from .operator_status import (
    build_authorization_challenge_status_message,
    build_mission_status_message,
    build_patrol_status_message,
    build_range_status_message,
    build_release_status_message,
    build_safety_status_message,
    build_scene_context_status_messages,
    build_target_pool_status_messages,
)
from .operator_tracking import (
    FIRE_CANDIDATE_TRACK_LABELS,
    OperatorTargetLock,
    TargetLockConfig,
)
from .patrol_advisory import PatrolAdvisoryEngine, PatrolModeAssessment
from .payload_hil_cycle import InertPayloadHilCycleCoordinator
from .payload_target_live import LivePayloadTargetCoordinator, LivePayloadTargetFrame
from .rgb_fire_corroboration import (
    IndependentRgbFireCorroborationConfig,
    IndependentRgbFireCorroborator,
)
from .rgb_slam_range import RgbSlamRangeConfig, RgbSlamRangeEstimator
from .selection_target_pool import UnifiedSelectionTargetPool
from .semantic_environment import (
    AsyncSemanticContextRunner,
    SemanticContextSnapshot,
    SemanticContextState,
)
from .short_term_tracking import (
    OpenCVShortTermTargetTracker,
    ShortTermTrackingResult,
    ShortTermTrackingStatus,
)
from .target_speed import TargetWorldSpeedEstimator
from .telemetry import (
    TelemetryProvider,
    with_observed_flight_mode_permission,
    with_person_detector_health,
)
from .tracking_evaluation import JsonlIdentityPredictionWriter
from .unified_tracking import (
    CameraMotionEstimate,
    TargetObservation,
    UnifiedTargetPool,
    UnifiedTrackSnapshot,
    UnifiedTrackState,
)
from .vehicle_reid import OnnxVehicleReIdEncoder
from .vision import CapturedFrame, DetectorEnsemble, FrameSource, VisionDependencyError
from .visual_inertial_range import VisualInertialRangeConfig, VisualInertialRangeEstimator

_PERSON_LOCK_MODEL_LABELS = frozenset(
    {"person", "pedestrian", "people", "person_sitting", "firefighter"}
)
_VEHICLE_LOCK_MODEL_LABELS = frozenset(
    {
        "vehicle",
        "car",
        "van",
        "truck",
        "bus",
        "train",
        "bicycle",
        "motorcycle",
        "motorbike",
        "motor",
        "tricycle",
        "awning-tricycle",
        "awning_tricycle",
        "boat",
    }
)
_AIRCRAFT_LOCK_MODEL_LABELS = frozenset(
    {"aircraft", "airplane", "aeroplane", "plane", "helicopter", "drone", "uav"}
)
_FIRE_LOCK_MODEL_LABELS = frozenset(
    {
        "fire",
        "flame",
        "smoke",
        "hotspot",
        "burned_area",
        "smoldering_area",
        "smolder_area",
    }
)


@dataclass(frozen=True, slots=True)
class LiveRunConfig:
    operator_id: str = "local-operator"
    max_frames: int | None = None
    display: bool = True
    alert_banner_seconds: float = 5.0
    performance_window_frames: int = 600
    simulate_payload_cycle: bool = False
    auto_simulate_payload_cycle: bool = False
    observe_pixhawk_lifecycle: bool = False
    task_area_mission_sequence: int | None = None
    allowed_auto_modes: tuple[str, ...] = ("AUTO", "MISSION", "AUTO_MISSION")
    person_safety_evidence_qualified: bool = False
    semantic_context_maximum_age_s: float = 2.0
    person_reid_frame_stride: int = 1
    vehicle_reid_frame_stride: int = 1
    reid_maximum_interval_s: float = 0.1

    def __post_init__(self) -> None:
        if self.max_frames is not None and (
            isinstance(self.max_frames, bool)
            or not isinstance(self.max_frames, int)
            or self.max_frames <= 0
        ):
            raise ValueError("max_frames must be positive when supplied")
        if not isinstance(self.operator_id, str) or not self.operator_id.strip():
            raise ValueError("operator_id cannot be empty")
        if not math.isfinite(self.alert_banner_seconds) or self.alert_banner_seconds <= 0:
            raise ValueError("alert_banner_seconds must be a finite positive number")
        if (
            not math.isfinite(self.semantic_context_maximum_age_s)
            or self.semantic_context_maximum_age_s <= 0.0
        ):
            raise ValueError("semantic context maximum age must be finite and positive")
        for name, value in (
            ("person ReID frame stride", self.person_reid_frame_stride),
            ("vehicle ReID frame stride", self.vehicle_reid_frame_stride),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 30:
                raise ValueError(f"{name} must be an integer between 1 and 30")
        if (
            not math.isfinite(self.reid_maximum_interval_s)
            or not 0.01 <= self.reid_maximum_interval_s <= 2.0
        ):
            raise ValueError("ReID maximum interval must be finite and between 0.01 and 2 seconds")
        if (
            isinstance(self.performance_window_frames, bool)
            or not isinstance(self.performance_window_frames, int)
            or self.performance_window_frames <= 0
        ):
            raise ValueError("performance_window_frames must be a positive integer")
        if self.task_area_mission_sequence is not None and (
            isinstance(self.task_area_mission_sequence, bool)
            or not isinstance(self.task_area_mission_sequence, int)
        ):
            raise ValueError("task-area mission sequence must be an integer")
        if self.observe_pixhawk_lifecycle and (
            self.task_area_mission_sequence is None or self.task_area_mission_sequence < 0
        ):
            raise ValueError(
                "observed Pixhawk lifecycle requires a non-negative task-area mission sequence"
            )
        if not self.allowed_auto_modes or any(
            not isinstance(mode, str) or not mode.strip() for mode in self.allowed_auto_modes
        ):
            raise ValueError("allowed_auto_modes must contain non-empty values")
        if self.auto_simulate_payload_cycle and not self.simulate_payload_cycle:
            raise ValueError("auto_simulate_payload_cycle requires simulate_payload_cycle=true")


@dataclass(frozen=True, slots=True)
class LiveRangingConfig:
    """Explicit uncertainty assumptions for read-only live range metadata."""

    calibration: CameraCalibration
    altitude_agl_sigma_m: float = 1.5
    roll_sigma_deg: float = 0.3
    pitch_sigma_deg: float = 0.3
    heading_sigma_deg: float = 1.0
    target_center_sigma_px: float = 2.0

    def __post_init__(self) -> None:
        for name, value in (
            ("AGL uncertainty", self.altitude_agl_sigma_m),
            ("roll uncertainty", self.roll_sigma_deg),
            ("pitch uncertainty", self.pitch_sigma_deg),
            ("heading uncertainty", self.heading_sigma_deg),
            ("target-center uncertainty", self.target_center_sigma_px),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"live ranging {name} must be finite and positive")


@dataclass(frozen=True, slots=True)
class LiveRunResult:
    processed_frames: int
    final_phase: MissionPhase
    authorization_count: int
    alert_delivery_count: int
    alert_delivery_failure_count: int
    average_fps: float
    steady_source_fps: float
    steady_processing_fps: float
    startup_to_first_frame_seconds: float
    capture_latency_p50_ms: float
    capture_latency_p95_ms: float
    frame_age_at_inference_p50_ms: float
    frame_age_at_inference_p95_ms: float
    inference_latency_p50_ms: float
    inference_latency_p95_ms: float
    rgb_fire_verifier_assessment_count: int
    rgb_fire_verifier_skipped_no_candidate_frame_count: int
    rgb_fire_verifier_inference_count: int
    rgb_fire_verifier_failure_count: int
    rgb_fire_verifier_unavailable_frame_count: int
    rgb_fire_verifier_unqualified_frame_count: int
    rgb_fire_verifier_corroborated_frame_count: int
    rgb_fire_verifier_corroborated_detection_count: int
    rgb_fire_verifier_latency_p50_ms: float
    rgb_fire_verifier_latency_p95_ms: float
    camera_reconnect_count: int
    capture_queue_high_watermark: int
    capture_queue_backpressure_count: int
    captured_frame_count: int
    retried_alert_count: int
    simulated_payload_cycle_count: int
    local_selection_count: int
    local_tracking_status_count: int
    remote_selection_count: int
    remote_tracking_status_count: int
    remote_mission_status_count: int
    remote_safety_status_count: int
    remote_patrol_status_count: int
    remote_range_status_count: int
    remote_release_status_count: int
    remote_approach_challenge_count: int
    remote_approach_status_count: int
    remote_approach_confirmation_count: int
    remote_payload_target_challenge_count: int
    remote_payload_target_status_count: int
    remote_payload_target_confirmation_count: int
    payload_target_error_count: int
    remote_target_pool_status_count: int
    remote_scene_context_status_count: int
    approach_hil_abort_count: int
    approach_hil_error_count: int
    remote_transport_error_count: int
    monocular_avoidance_assessment_count: int
    monocular_avoidance_invalid_count: int
    monocular_avoidance_caution_count: int
    monocular_avoidance_avoid_count: int
    monocular_avoidance_error_count: int
    monocular_avoidance_latency_p50_ms: float
    monocular_avoidance_latency_p95_ms: float
    unified_target_pool_update_count: int
    unified_target_pool_error_count: int
    unified_target_pool_maximum_track_count: int
    unified_target_pool_created_track_count: int
    unified_target_pool_recovered_track_count: int
    unified_target_pool_lost_track_count: int
    unified_target_pool_association_p50_ms: float
    unified_target_pool_association_p95_ms: float
    identity_tracking_log_frame_count: int
    identity_tracking_log_error_count: int
    identity_tracking_log_disabled_after_error: bool
    person_reid_failure_count: int
    person_reid_inference_count: int
    person_reid_skipped_frame_count: int
    person_reid_no_candidate_frame_count: int
    person_reid_forced_recovery_count: int
    person_reid_latency_p50_ms: float
    person_reid_latency_p95_ms: float
    vehicle_reid_failure_count: int
    vehicle_reid_inference_count: int
    vehicle_reid_skipped_frame_count: int
    vehicle_reid_no_candidate_frame_count: int
    vehicle_reid_forced_recovery_count: int
    vehicle_reid_latency_p50_ms: float
    vehicle_reid_latency_p95_ms: float
    patrol_advisory_assessment_count: int
    patrol_return_to_observe_count: int
    patrol_advisory_error_count: int
    short_term_tracking_update_count: int
    short_term_tracking_invalid_count: int
    short_term_tracking_error_count: int
    short_term_tracking_optical_flow_hint_count: int
    short_term_tracking_template_hint_count: int
    short_term_tracking_accepted_hint_count: int
    short_term_tracking_rejected_hint_count: int
    short_term_tracking_camera_motion_count: int
    short_term_tracking_latency_p50_ms: float
    short_term_tracking_latency_p95_ms: float
    selection_target_pool_sync_count: int
    selection_target_pool_binding_count: int
    selection_target_pool_pending_count: int
    selection_target_pool_cancel_count: int
    selection_target_pool_error_count: int
    ranging_assessment_count: int
    ranging_valid_count: int
    ranging_degraded_count: int
    ranging_invalid_count: int
    ranging_error_count: int
    ranging_latency_p50_ms: float
    ranging_latency_p95_ms: float
    semantic_context_submitted_frame_count: int
    semantic_context_interval_skipped_frame_count: int
    semantic_context_replaced_pending_frame_count: int
    semantic_context_valid_frame_count: int
    semantic_context_invalid_frame_count: int
    semantic_context_submit_error_count: int
    semantic_context_stale_count: int
    semantic_context_latency_p50_ms: float
    semantic_context_latency_p95_ms: float
    semantic_context_shutdown_clean: bool


class OpenCVAuthorizationUI:
    """Camera-only overlay with mouse target selection and operator approval keys."""

    def __init__(self, *, title: str = "Multi-Detect camera") -> None:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError(
                "Install live vision dependencies: pip install -e '.[vision]'"
            ) from exc
        self._cv2 = cv2
        self._title = title
        self._video_width = 1
        self._video_height = 1
        self._drag_start: tuple[int, int] | None = None
        self._drag_current: tuple[int, int] | None = None
        self._pending_selection: tuple[int, int, int, int] | None = None
        self._last_selection: tuple[int, int, int, int] | None = None
        self._pending_cancel = False
        self._has_selection = False
        self._session_id = str(uuid4())
        self._selection_sequence = 0
        self._cv2.namedWindow(self._title, self._cv2.WINDOW_AUTOSIZE)
        self._cv2.setMouseCallback(self._title, self._on_mouse)

    def _on_mouse(self, event: int, x: int, y: int, _flags: int, _param: Any) -> None:
        x = min(max(0, x), self._video_width - 1)
        y = min(max(0, y), self._video_height - 1)
        if event == self._cv2.EVENT_LBUTTONDOWN:
            self._drag_start = (x, y)
            self._drag_current = (x, y)
        elif event == self._cv2.EVENT_MOUSEMOVE and self._drag_start is not None:
            self._drag_current = (x, y)
        elif event == self._cv2.EVENT_LBUTTONUP and self._drag_start is not None:
            start_x, start_y = self._drag_start
            x1, x2 = sorted((start_x, x))
            y1, y2 = sorted((start_y, y))
            if x2 - x1 >= 2 and y2 - y1 >= 2:
                self._pending_selection = self._expand_selection(
                    x1,
                    y1,
                    x2,
                    y2,
                    minimum_size=24,
                )
                self._last_selection = self._pending_selection
            self._drag_start = None
            self._drag_current = None
        elif event == self._cv2.EVENT_RBUTTONUP:
            self._pending_cancel = True
            self._last_selection = None
            self._drag_start = None
            self._drag_current = None

    def _expand_selection(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        minimum_size: int,
    ) -> tuple[int, int, int, int]:
        center_x = (x1 + x2) / 2.0
        center_y = (y1 + y2) / 2.0
        half_width = max((x2 - x1) / 2.0, minimum_size / 2.0)
        half_height = max((y2 - y1) / 2.0, minimum_size / 2.0)
        left = max(0, round(center_x - half_width))
        top = max(0, round(center_y - half_height))
        right = min(self._video_width - 1, round(center_x + half_width))
        bottom = min(self._video_height - 1, round(center_y + half_height))
        if right - left < minimum_size and left == 0:
            right = min(self._video_width - 1, minimum_size)
        if bottom - top < minimum_size and top == 0:
            bottom = min(self._video_height - 1, minimum_size)
        return left, top, right, bottom

    def consume_target_command(
        self,
        captured: CapturedFrame,
        *,
        now_s: float,
    ) -> TargetSelectionCommand | None:
        """Turn the latest local mouse gesture into the same command used by G20."""
        geometry = VideoGeometry("local-camera", captured.width, captured.height)
        if self._pending_cancel:
            self._pending_cancel = False
            self._has_selection = False
            self._last_selection = None
            action = SelectionAction.CANCEL
            bbox = None
        elif self._pending_selection is not None:
            x1, y1, x2, y2 = self._pending_selection
            self._pending_selection = None
            action = SelectionAction.SWITCH if self._has_selection else SelectionAction.SELECT
            bbox = BoundingBox(
                x1 / captured.width,
                y1 / captured.height,
                x2 / captured.width,
                y2 / captured.height,
            )
            self._has_selection = True
        else:
            return None
        self._selection_sequence = (self._selection_sequence + 1) & 0xFFFFFFFF
        return TargetSelectionCommand(
            command_id=str(uuid4()),
            session_id=self._session_id,
            sequence=self._selection_sequence,
            action=action,
            geometry=geometry,
            issued_at_s=now_s,
            expires_at_s=now_s + 3.0,
            bbox=bbox,
            displayed_frame_id=captured.frame_id,
        )

    def render(
        self,
        captured: CapturedFrame,
        *,
        detections: tuple[Any, ...],
        tracks: tuple[TrackSnapshot, ...],
        alerts: tuple[FireAlert, ...],
        alert_delivery_status: str | None,
        phase: MissionPhase,
        deployment_capable: bool,
        remaining_payload_count: int,
        payload_inventory_verified: bool,
        payload_inventory_source: str,
        telemetry: VehicleTelemetry,
        fps: float,
        inference_latency_p95_ms: float,
        camera_reconnect_count: int,
        recent_events: tuple[str, ...],
        pending_authorization: bool,
        deployment_ready: bool,
        simulation_cycle_enabled: bool,
        deployment_window: DeploymentWindowSolution | None = None,
        local_track_status: TrackStatusMessage | None = None,
        monocular_avoidance: MonocularAvoidanceAssessment | None = None,
        ranging_solution: RangeSolution | None = None,
    ) -> str | None:
        del (
            remaining_payload_count,
            payload_inventory_verified,
            payload_inventory_source,
            telemetry,
            inference_latency_p95_ms,
            camera_reconnect_count,
            recent_events,
        )
        self._video_width = captured.width
        self._video_height = captured.height
        image = captured.image_bgr.copy()
        for detection in detections:
            x1 = round(detection.bbox.x1 * captured.width)
            y1 = round(detection.bbox.y1 * captured.height)
            x2 = round(detection.bbox.x2 * captured.width)
            y2 = round(detection.bbox.y2 * captured.height)
            color = (
                (0, 165, 255)
                if detection.label.strip().lower() in {"fire", "flame", "smoke"}
                else (220, 180, 40)
            )
            self._cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
            self._cv2.putText(
                image,
                f"{detection.label} {detection.confidence:.2f}",
                (x1, max(18, y1 - 6)),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                self._cv2.LINE_AA,
            )
        locked_track_id = (
            local_track_status.target_id
            if local_track_status is not None and local_track_status.state is TrackingState.TRACKING
            else None
        )
        locked_track_drawn = False
        for track in tracks:
            x1 = round(track.bbox.x1 * captured.width)
            y1 = round(track.bbox.y1 * captured.height)
            x2 = round(track.bbox.x2 * captured.width)
            y2 = round(track.bbox.y2 * captured.height)
            is_locked = track.track_id == locked_track_id
            locked_track_drawn = locked_track_drawn or is_locked
            color = (0, 255, 0) if is_locked else (0, 0, 255)
            thickness = 4 if is_locked else (2 if track.confirmed else 1)
            self._cv2.rectangle(image, (x1, y1), (x2, y2), color, thickness)
            self._cv2.putText(
                image,
                (
                    f"LOCK {track.track_id} {track.label}"
                    if is_locked
                    else f"{track.track_id} {track.duration_s:.1f}s"
                ),
                (x1, min(captured.height - 8, y2 + 20)),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                self._cv2.LINE_AA,
            )
        if (
            not locked_track_drawn
            and local_track_status is not None
            and local_track_status.state in {TrackingState.TRACKING, TrackingState.INITIALIZING}
            and local_track_status.bbox is not None
        ):
            bbox = local_track_status.bbox
            x1 = round(bbox.x1 * captured.width)
            y1 = round(bbox.y1 * captured.height)
            x2 = round(bbox.x2 * captured.width)
            y2 = round(bbox.y2 * captured.height)
            tracking = local_track_status.state is TrackingState.TRACKING
            color = (0, 255, 0) if tracking else (0, 165, 255)
            self._cv2.rectangle(image, (x1, y1), (x2, y2), color, 4 if tracking else 2)
            self._cv2.putText(
                image,
                (
                    f"LOCK {local_track_status.target_id} {local_track_status.label or ''}"
                    if tracking
                    else "REACQUIRING"
                ),
                (x1, max(18, y1 - 6)),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                self._cv2.LINE_AA,
            )
        if self._drag_start is not None and self._drag_current is not None:
            self._cv2.rectangle(image, self._drag_start, self._drag_current, (0, 255, 255), 2)
        elif self._last_selection is not None and (
            local_track_status is None or local_track_status.state is not TrackingState.TRACKING
        ):
            x1, y1, x2, y2 = self._last_selection
            self._cv2.rectangle(image, (x1, y1), (x2, y2), (0, 255, 255), 2)

        mode = "PATROL+PAYLOAD" if deployment_capable else "PATROL"
        target_state = local_track_status.state.value.upper() if local_track_status else "NONE"
        window_state = deployment_window.status.value.upper() if deployment_window else "N/A"
        obstacle_state = (
            monocular_avoidance.state.value.upper() if monocular_avoidance is not None else "OFF"
        )
        range_state = ranging_solution.validity.value.upper() if ranging_solution else "OFF"
        line = (
            f"{mode} | {phase.value} | {fps:.1f} FPS | "
            f"TARGET {target_state} | RANGE {range_state} | WINDOW {window_state} | "
            f"OBS {obstacle_state} ADVISORY"
        )
        self._cv2.rectangle(image, (0, 0), (captured.width, 36), (16, 18, 22), -1)
        self._cv2.putText(
            image,
            line,
            (10, 25),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            2,
            self._cv2.LINE_AA,
        )
        if alerts:
            alert = alerts[0]
            self._cv2.rectangle(image, (0, 38), (captured.width, 72), (0, 0, 180), -1)
            self._cv2.putText(
                image,
                (
                    f"FIRE {alert.target_id} {alert.confidence:.2f} "
                    f"{alert_delivery_status or 'pending'}"
                ),
                (10, 62),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                self._cv2.LINE_AA,
            )
        hint = "Drag: select/switch | Right-click or X: cancel | Q: quit"
        if pending_authorization:
            hint += " | A approve / D deny"
        elif deployment_ready and simulation_cycle_enabled:
            hint += " | S simulate only"
        if deployment_window is not None and deployment_window.along_track_error_m is not None:
            hint += (
                f" | lead {deployment_window.release_lead_distance_m:.1f}m"
                f" error {deployment_window.along_track_error_m:+.1f}m"
            )
        if ranging_solution is not None and ranging_solution.slant_range_m is not None:
            ci95 = ranging_solution.slant_range_ci95_m
            hint += f" | range {ranging_solution.slant_range_m:.1f}m"
            if ci95 is not None:
                hint += f" [{ci95[0]:.1f},{ci95[1]:.1f}]"
        self._cv2.putText(
            image,
            hint,
            (10, captured.height - 12),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
            self._cv2.LINE_AA,
        )
        self._cv2.imshow(self._title, image)
        key = self._cv2.waitKey(1) & 0xFF
        if key == ord("x"):
            self._pending_cancel = True
            return None
        return {
            ord("a"): "approve",
            ord("c"): "ack_alert",
            ord("d"): "deny",
            ord("q"): "quit",
            ord("s"): "simulate_payload",
        }.get(key)

    def close(self) -> None:
        self._cv2.destroyWindow(self._title)


class LiveMissionRunner:
    """Connect live pixels, operator state, target tracking and optional flight control."""

    def __init__(
        self,
        *,
        mission: MissionController,
        frame_source: FrameSource,
        detector: DetectorEnsemble,
        telemetry_provider: TelemetryProvider,
        config: LiveRunConfig,
        alert_publisher: AlertPublisher | None = None,
        alert_outbox: SqliteAlertOutbox | None = None,
        prediction_writer: JsonlPredictionWriter | None = None,
        identity_prediction_writer: JsonlIdentityPredictionWriter | None = None,
        operator_bridge: LiveOperatorBridge | None = None,
        payload_hil_cycle: InertPayloadHilCycleCoordinator | None = None,
        monocular_avoidance: OpenCVSparseFlowAvoidance | None = None,
        unified_target_pool: UnifiedTargetPool | None = None,
        person_reid_encoder: OnnxPersonReIdEncoder | None = None,
        vehicle_reid_encoder: OnnxVehicleReIdEncoder | None = None,
        aircraft_appearance_encoder: HandcraftedAircraftAppearanceEncoder | None = None,
        patrol_advisory_engine: PatrolAdvisoryEngine | None = None,
        short_term_tracker: OpenCVShortTermTargetTracker | None = None,
        attitude_camera_motion: AttitudeCameraMotionEstimator | None = None,
        selection_target_pool: UnifiedSelectionTargetPool | None = None,
        ranging_engine: MultiModalRangingEngine | None = None,
        ranging_config: LiveRangingConfig | None = None,
        adaptive_ranging_policy: AdaptiveRangingPolicy | None = None,
        rgb_slam_ranging: RgbSlamRangeEstimator | None = None,
        metric_depth_runner: AsyncMetricDepthRunner | None = None,
        depth_grid_publisher: DepthGridUdpPublisher | None = None,
        approach_hil_coordinator: LiveApproachHilCoordinator | None = None,
        fixed_wing_aim_executor: FixedWingAimExecutor | None = None,
        payload_target_coordinator: LivePayloadTargetCoordinator | None = None,
        semantic_context_runner: AsyncSemanticContextRunner | None = None,
        rgb_fire_verifier: Any | None = None,
        rgb_fire_corroborator: IndependentRgbFireCorroborator | None = None,
        fixed_camera_observation_engine: FixedCameraObservationEngine | None = None,
    ) -> None:
        if payload_hil_cycle is not None and not config.simulate_payload_cycle:
            raise ValueError("payload HIL cycle requires simulate_payload_cycle=true")
        if payload_hil_cycle is not None and payload_hil_cycle.mission is not mission:
            raise ValueError("payload HIL cycle must use the live mission controller")
        if person_reid_encoder is not None and unified_target_pool is None:
            raise ValueError("person ReID requires the unified target pool")
        if vehicle_reid_encoder is not None and unified_target_pool is None:
            raise ValueError("vehicle ReID requires the unified target pool")
        if aircraft_appearance_encoder is not None and unified_target_pool is None:
            raise ValueError("aircraft appearance recovery requires the unified target pool")
        if (rgb_fire_verifier is None) != (rgb_fire_corroborator is None):
            raise ValueError(
                "independent RGB fire verifier and corroborator must be supplied together"
            )
        if identity_prediction_writer is not None and unified_target_pool is None:
            raise ValueError("identity prediction logging requires the unified target pool")
        if patrol_advisory_engine is not None and unified_target_pool is None:
            raise ValueError("patrol advisory requires the unified target pool")
        if short_term_tracker is not None and unified_target_pool is None:
            raise ValueError("short-term tracking requires the unified target pool")
        if attitude_camera_motion is not None and (
            short_term_tracker is None or ranging_config is None
        ):
            raise ValueError("attitude camera motion requires short-term tracking and calibration")
        if selection_target_pool is not None and unified_target_pool is None:
            raise ValueError("selection target-pool integration requires the unified target pool")
        if selection_target_pool is not None and operator_bridge is None:
            raise ValueError("selection target-pool integration requires the operator bridge")
        if (
            selection_target_pool is not None
            and selection_target_pool.target_pool is not unified_target_pool
        ):
            raise ValueError("selection integration must use the live unified target pool")
        if (ranging_engine is None) != (ranging_config is None):
            raise ValueError("live ranging engine and configuration must be supplied together")
        if ranging_engine is not None and unified_target_pool is None:
            raise ValueError("live ranging requires the unified target pool")
        if adaptive_ranging_policy is not None and ranging_engine is None:
            raise ValueError("adaptive ranging policy requires live ranging")
        if rgb_slam_ranging is not None and ranging_engine is None:
            raise ValueError("RGB-SLAM ranging requires live ranging")
        if metric_depth_runner is not None and any(
            value is None for value in (ranging_engine, ranging_config, selection_target_pool)
        ):
            raise ValueError("metric depth requires ranging and the operator selection pool")
        if depth_grid_publisher is not None and metric_depth_runner is None:
            raise ValueError("depth-grid publishing requires metric depth")
        if approach_hil_coordinator is not None and any(
            dependency is None
            for dependency in (
                operator_bridge,
                unified_target_pool,
                selection_target_pool,
                monocular_avoidance,
                ranging_engine,
                ranging_config,
            )
        ):
            raise ValueError(
                "Mode-3 HIL requires operator link, unified selection pool, avoidance and ranging"
            )
        if fixed_wing_aim_executor is not None and approach_hil_coordinator is None:
            raise ValueError("fixed-wing aim control requires the Mode-3 coordinator")
        if payload_target_coordinator is not None and any(
            dependency is None
            for dependency in (
                operator_bridge,
                unified_target_pool,
                selection_target_pool,
            )
        ):
            raise ValueError(
                "Mode-2 target confirmation requires operator link and unified selection pool"
            )
        if payload_target_coordinator is not None and not mission.config.deployment_capable:
            raise ValueError("Mode-2 target confirmation requires a deployment-capable mission")
        self.mission = mission
        self.frame_source = frame_source
        self.detector = detector
        self.telemetry_provider = telemetry_provider
        self.config = config
        self.alert_publisher = alert_publisher or RecordingAlertPublisher()
        self.alert_outbox = alert_outbox
        self.prediction_writer = prediction_writer
        self.identity_prediction_writer = identity_prediction_writer
        self.operator_bridge = operator_bridge
        self.payload_hil_cycle = payload_hil_cycle
        self.monocular_avoidance = monocular_avoidance
        self.unified_target_pool = unified_target_pool
        self.person_reid_encoder = person_reid_encoder
        self.vehicle_reid_encoder = vehicle_reid_encoder
        self.aircraft_appearance_encoder = aircraft_appearance_encoder
        self.patrol_advisory_engine = patrol_advisory_engine
        self.short_term_tracker = short_term_tracker
        self.attitude_camera_motion = attitude_camera_motion
        self.selection_target_pool = selection_target_pool
        self.ranging_engine = ranging_engine
        self.ranging_config = ranging_config
        self.adaptive_ranging_policy = adaptive_ranging_policy or (
            AdaptiveRangingPolicy() if ranging_engine is not None else None
        )
        self.metric_depth_runner = metric_depth_runner
        self.depth_grid_publisher = depth_grid_publisher
        self.visual_inertial_ranging = (
            VisualInertialRangeEstimator(
                VisualInertialRangeConfig(
                    minimum_range_m=ranging_engine.config.minimum_slant_range_m,
                    maximum_range_m=ranging_engine.config.maximum_slant_range_m,
                )
            )
            if ranging_engine is not None
            else None
        )
        self.rgb_slam_ranging = rgb_slam_ranging or (
            RgbSlamRangeEstimator(
                RgbSlamRangeConfig(
                    minimum_range_m=ranging_engine.config.minimum_slant_range_m,
                    maximum_range_m=ranging_engine.config.maximum_slant_range_m,
                )
            )
            if ranging_engine is not None
            else None
        )
        self.approach_hil_coordinator = approach_hil_coordinator
        self.fixed_wing_aim_executor = fixed_wing_aim_executor
        self.payload_target_coordinator = payload_target_coordinator
        self.semantic_context_runner = semantic_context_runner
        self.rgb_fire_verifier = rgb_fire_verifier
        self.rgb_fire_corroborator = rgb_fire_corroborator
        self.fixed_camera_observation_engine = (
            fixed_camera_observation_engine
            if fixed_camera_observation_engine is not None
            else FixedCameraObservationEngine()
            if selection_target_pool is not None
            else None
        )
        self._rgb_fire_fail_closed = rgb_fire_corroborator or IndependentRgbFireCorroborator(
            IndependentRgbFireCorroborationConfig()
        )
        self._lifecycle_waiting_fingerprint: tuple[object, ...] | None = None
        self._ranging_solution_fingerprints: dict[str, tuple[object, ...]] = {}
        # Detector-stride and short occlusion frames must not make QGC alternate
        # between a metric value and ``--``.  The QGC overlay holds the matching
        # value for 1.5 s, so retain the same bounded interval here. A new valid
        # estimate always replaces this cache immediately.
        self._target_metric_cache: dict[str, tuple[float, float | None, float]] = {}
        self._metric_depth_last_result_frame_id: str | None = None
        self._metric_depth_last_failure_count = 0
        self._latest_metric_depth_result: MetricDepthResult | None = None
        self._target_world_speed = (
            TargetWorldSpeedEstimator() if ranging_engine is not None else None
        )

    def run(self) -> LiveRunResult:
        ui = OpenCVAuthorizationUI() if self.config.display else None
        processed = 0
        authorizations = 0
        alert_deliveries = 0
        alert_delivery_failures = 0
        retried_alert_count = 0
        simulated_payload_cycles = 0
        local_selections = 0
        local_tracking_statuses = 0
        remote_selections = 0
        remote_tracking_statuses = 0
        remote_mission_statuses = 0
        remote_safety_statuses = 0
        remote_patrol_statuses = 0
        remote_range_statuses = 0
        remote_release_statuses = 0
        remote_approach_challenges = 0
        remote_approach_statuses = 0
        remote_approach_confirmations = 0
        remote_payload_target_challenges = 0
        remote_payload_target_statuses = 0
        remote_payload_target_confirmations = 0
        payload_target_errors = 0
        remote_target_pool_statuses = 0
        remote_scene_context_statuses = 0
        operator_peer_state_initialized = False
        last_operator_peer: tuple[str, int] | None = None
        approach_hil_aborts = 0
        approach_hil_errors = 0
        remote_mission_status_sequence = 0
        remote_safety_status_sequence = 0
        remote_patrol_status_sequence = 0
        remote_range_status_sequence = 0
        remote_release_status_sequence = 0
        remote_authorization_challenge_sequence = 0
        remote_target_pool_status_sequence = 0
        remote_target_pool_revision = 0
        remote_scene_context_status_sequence = 0
        remote_scene_context_revision = 0
        remote_transport_errors = 0
        monocular_avoidance_assessments = 0
        monocular_avoidance_invalid = 0
        monocular_avoidance_caution = 0
        monocular_avoidance_avoid = 0
        monocular_avoidance_errors = 0
        rgb_fire_verifier_assessments = 0
        rgb_fire_verifier_skipped_no_candidate_frames = 0
        rgb_fire_verifier_inferences = 0
        rgb_fire_verifier_failures = 0
        rgb_fire_verifier_unavailable_frames = 0
        rgb_fire_verifier_unqualified_frames = 0
        rgb_fire_verifier_corroborated_frames = 0
        rgb_fire_verifier_corroborated_detections = 0
        unified_target_pool_updates = 0
        unified_target_pool_errors = 0
        unified_target_pool_maximum_tracks = 0
        unified_target_pool_created_tracks = 0
        unified_target_pool_recovered_tracks = 0
        unified_target_pool_lost_tracks = 0
        identity_tracking_log_frames = 0
        identity_tracking_log_errors = 0
        identity_tracking_log_disabled_after_error = False
        person_reid_failures = 0
        person_reid_inferences = 0
        person_reid_skipped_frames = 0
        person_reid_no_candidate_frames = 0
        person_reid_forced_recoveries = 0
        vehicle_reid_failures = 0
        vehicle_reid_inferences = 0
        vehicle_reid_skipped_frames = 0
        vehicle_reid_no_candidate_frames = 0
        vehicle_reid_forced_recoveries = 0
        last_telemetry_diagnostics_at_s = float("-inf")
        patrol_advisory_assessments = 0
        patrol_return_to_observe = 0
        patrol_advisory_errors = 0
        short_term_tracking_updates = 0
        short_term_tracking_invalid = 0
        short_term_tracking_errors = 0
        short_term_tracking_optical_flow_hints = 0
        short_term_tracking_template_hints = 0
        short_term_tracking_accepted_hints = 0
        short_term_tracking_rejected_hints = 0
        short_term_tracking_camera_motion_estimates = 0
        short_term_tracking_camera_motion_reported = False
        attitude_camera_motion_reported = False
        selection_target_pool_syncs = 0
        selection_target_pool_bindings = 0
        selection_target_pool_pending = 0
        selection_target_pool_cancels = 0
        selection_target_pool_errors = 0
        ranging_assessments = 0
        ranging_valid = 0
        ranging_degraded = 0
        ranging_invalid = 0
        ranging_errors = 0
        semantic_context_submit_errors = 0
        semantic_context_stale = 0
        semantic_context_shutdown_clean = True
        latest_alert: FireAlert | None = None
        latest_alert_delivery_status: str | None = None
        latest_monocular_avoidance: MonocularAvoidanceAssessment | None = None
        latest_patrol_assessment: PatrolModeAssessment | None = None
        latest_ranging_solution: RangeSolution | None = None
        latest_approach_frame: LiveApproachHilFrame | None = None
        latest_payload_target_frame: LivePayloadTargetFrame | None = None
        latest_semantic_context: SemanticContextSnapshot | None = None
        last_approach_phase: ApproachHilPhase | None = None
        last_fixed_wing_aim_state: FixedWingAimState | None = None
        last_target_pool_fingerprint: tuple[object, ...] | None = None
        last_target_pool_built_at_s: float | None = None
        last_scene_context_wire_fingerprint: tuple[object, ...] | None = None
        last_scene_context_built_at_s: float | None = None
        last_patrol_fingerprint: tuple[object, ...] | None = None
        last_monocular_avoidance_fingerprint: tuple[str, str | None] | None = None
        last_semantic_context_frame_id: str | None = None
        semantic_context_stale_reported_frame_id: str | None = None
        rgb_fire_verifier_unavailable_reported = False
        recent_events: deque[str] = deque(maxlen=8)
        capture_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        frame_age_at_inference_ms: deque[float] = deque(
            maxlen=self.config.performance_window_frames
        )
        inference_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        rgb_fire_verifier_latency_ms: deque[float] = deque(
            maxlen=self.config.performance_window_frames
        )
        monocular_avoidance_latency_ms: deque[float] = deque(
            maxlen=self.config.performance_window_frames
        )
        unified_target_pool_association_ms: deque[float] = deque(
            maxlen=self.config.performance_window_frames
        )
        short_term_tracking_latency_ms: deque[float] = deque(
            maxlen=self.config.performance_window_frames
        )
        ranging_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        semantic_context_latency_ms: deque[float] = deque(
            maxlen=self.config.performance_window_frames
        )
        person_reid_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        vehicle_reid_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        run_started_s = time.monotonic()
        last_person_reid_at_s: float | None = run_started_s
        last_vehicle_reid_at_s: float | None = run_started_s
        first_captured_at_s: float | None = None
        last_captured_at_s: float | None = None
        first_processed_at_s: float | None = None
        last_processed_at_s: float | None = None
        first_frame_received_at_s: float | None = None
        local_target_lock: OperatorTargetLock | None = None
        local_manual_tracker: OpenCVManualTargetTracker | None = None
        local_manual_tracker_unavailable = False
        last_fixed_camera_observation_fingerprint: tuple[object, ...] | None = None
        local_active_selection_command: TargetSelectionCommand | None = None
        local_track_status: TrackStatusMessage | None = None
        # The target pool drives the video overlay directly. Publish on a
        # cadence shorter than a 15 Hz frame period so every fresh tracker frame
        # can reach QGC once the runtime is meeting its minimum source rate.
        # The per-state caps leave detector and flight-control workload unchanged;
        # this is metadata only.
        normal_target_pool_status_interval_s = min(
            self.operator_bridge.target_pool_status_heartbeat_s
            if self.operator_bridge is not None
            else 0.2,
            1.0 / 25.0,
        )
        operator_trk_target_pool_status_interval_s = min(
            normal_target_pool_status_interval_s,
            1.0 / 25.0,
        )
        exclusive_target_pool_status_interval_s = min(
            normal_target_pool_status_interval_s,
            1.0 / 30.0,
        )
        last_exclusive_lock_track_id: str | None = None
        last_lock_model_profile_fingerprint: tuple[object, ...] | None = None

        def deliver_alert(alert: FireAlert, *, delivered_at_s: float, retry: bool) -> None:
            nonlocal alert_deliveries, alert_delivery_failures, retried_alert_count
            nonlocal latest_alert, latest_alert_delivery_status
            latest_alert = alert
            if retry:
                retried_alert_count += 1
            try:
                self.alert_publisher.publish(alert)
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                alert_delivery_failures += 1
                latest_alert_delivery_status = "failed"
                recent_events.append(f"ALERT {alert.target_id} DELIVERY FAILED")
                if self.alert_outbox is not None:
                    self.alert_outbox.mark_failed(
                        alert.alert_id,
                        error_type=type(exc).__name__,
                    )
                self.mission.audit.append(
                    "alert.delivery_failed",
                    delivered_at_s,
                    {
                        "alert_id": alert.alert_id,
                        "error_type": type(exc).__name__,
                        "retry": retry,
                    },
                )
            else:
                alert_deliveries += 1
                latest_alert_delivery_status = "sent"
                recent_events.append(f"ALERT {alert.target_id} SENT")
                if self.alert_outbox is not None:
                    self.alert_outbox.mark_delivered(
                        alert.alert_id,
                        delivered_at_s=delivered_at_s,
                    )
                self.mission.audit.append(
                    "alert.delivery_succeeded",
                    delivered_at_s,
                    {"alert_id": alert.alert_id, "retry": retry},
                )

        try:
            now_s = time.monotonic()
            if self.config.observe_pixhawk_lifecycle:
                self.mission.audit.append(
                    "mission.pixhawk_lifecycle_observation_started",
                    now_s,
                    {
                        "task_area_mission_sequence": self.config.task_area_mission_sequence,
                        "allowed_auto_modes": self.config.allowed_auto_modes,
                        "flight_commands_enabled": False,
                    },
                )
            else:
                self.mission.launch(now_s=now_s)
                self.mission.arrive_task_area(now_s=now_s)
            if self.alert_outbox is not None:
                for pending_alert in self.alert_outbox.pending_alerts():
                    deliver_alert(
                        pending_alert,
                        delivered_at_s=time.monotonic(),
                        retry=True,
                    )
            if self.operator_bridge is not None:
                self.operator_bridge.start()
            if self.semantic_context_runner is not None:
                self.semantic_context_runner.start()
            self.frame_source.open()
            while self.config.max_frames is None or processed < self.config.max_frames:
                capture_started_s = time.perf_counter()
                try:
                    captured = self.frame_source.read()
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self.mission.audit.append(
                        "camera.read_failed",
                        time.monotonic(),
                        {"error_type": type(exc).__name__},
                    )
                    raise
                capture_latency_ms.append((time.perf_counter() - capture_started_s) * 1_000.0)
                frame_received_at_s = time.monotonic()
                if first_frame_received_at_s is None:
                    first_frame_received_at_s = frame_received_at_s
                if first_captured_at_s is None:
                    first_captured_at_s = captured.captured_at_s
                last_captured_at_s = captured.captured_at_s
                inference_started_s = time.perf_counter()
                frame_age_at_inference_ms.append(
                    max(0.0, time.monotonic() - captured.captured_at_s) * 1_000.0
                )
                exclusive_lock_track_id = (
                    self.selection_target_pool.exclusive_lock_track_id
                    if self.selection_target_pool is not None
                    else None
                )
                exclusive_lock_snapshot = _target_pool_snapshot(
                    self.unified_target_pool,
                    exclusive_lock_track_id,
                )
                exclusive_lock_label = (
                    exclusive_lock_snapshot.label if exclusive_lock_snapshot is not None else None
                )
                exclusive_lock_family = _lock_model_family(exclusive_lock_label)
                detector_route_labels = (
                    None
                    if exclusive_lock_track_id is None
                    else _lock_model_labels(exclusive_lock_family)
                )
                detector_route_applied, active_detector_count = _configure_detector_active_labels(
                    self.detector,
                    detector_route_labels,
                )
                specialized_reid_enabled = (
                    (exclusive_lock_family == "person" and self.person_reid_encoder is not None)
                    or (
                        exclusive_lock_family == "vehicle" and self.vehicle_reid_encoder is not None
                    )
                    or (
                        exclusive_lock_family == "aircraft"
                        and self.aircraft_appearance_encoder is not None
                    )
                )
                specialized_detector_enabled = (
                    exclusive_lock_track_id is not None
                    and bool(detector_route_labels)
                    and (
                        active_detector_count > 0
                        if active_detector_count is not None
                        else _detector_covers_any_label(
                            self.detector,
                            detector_route_labels,
                        )
                    )
                )
                if exclusive_lock_track_id is None:
                    lock_model_profile = "general_multiclass"
                elif specialized_detector_enabled or specialized_reid_enabled:
                    lock_model_profile = f"{exclusive_lock_family}_specialist"
                else:
                    lock_model_profile = "arbitrary_object_fallback"
                lock_model_profile_fingerprint = (
                    exclusive_lock_track_id,
                    exclusive_lock_label,
                    exclusive_lock_family,
                    lock_model_profile,
                    detector_route_applied,
                    active_detector_count,
                    specialized_reid_enabled,
                )
                if lock_model_profile_fingerprint != last_lock_model_profile_fingerprint:
                    self.mission.audit.append(
                        "tracking.lock_model_profile_changed",
                        max(captured.captured_at_s, time.monotonic()),
                        {
                            "enabled": exclusive_lock_track_id is not None,
                            "target_id": exclusive_lock_track_id,
                            "label": exclusive_lock_label,
                            "family": exclusive_lock_family,
                            "profile": lock_model_profile,
                            "detector_route_applied": detector_route_applied,
                            "active_detector_count": active_detector_count,
                            "specialized_detector_enabled": specialized_detector_enabled,
                            "specialized_detector_frame_stride": (
                                1 if specialized_detector_enabled else None
                            ),
                            "specialized_reid_enabled": specialized_reid_enabled,
                            "generic_tracker_enabled": self.short_term_tracker is not None,
                            "fallback": "arbitrary_object_tracker",
                        },
                    )
                    last_lock_model_profile_fingerprint = lock_model_profile_fingerprint
                if self.semantic_context_runner is not None:
                    try:
                        self.semantic_context_runner.submit(
                            captured.image_bgr,
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            submitted_at_s=max(captured.captured_at_s, time.monotonic()),
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        semantic_context_submit_errors += 1
                        self.mission.audit.append(
                            "perception.semantic_context_submit_failed",
                            max(captured.captured_at_s, time.monotonic()),
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "fallback": "primary_perception_continues",
                                "advisory_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                try:
                    detections = self.detector.detect(captured.image_bgr)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self.mission.audit.append(
                        "perception.inference_failed",
                        time.monotonic(),
                        {"error_type": type(exc).__name__, "frame_id": captured.frame_id},
                    )
                    raise
                if self.rgb_fire_verifier is None or self.rgb_fire_corroborator is None:
                    detections = self._rgb_fire_fail_closed.fail_closed(detections).detections
                    if self.mission.config.require_independent_rgb_corroboration:
                        rgb_fire_verifier_unavailable_frames += 1
                        if not rgb_fire_verifier_unavailable_reported:
                            self.mission.audit.append(
                                "perception.rgb_fire_verifier_unavailable",
                                max(captured.captured_at_s, time.monotonic()),
                                {
                                    "frame_id": captured.frame_id,
                                    "fallback": "patrol_continues_payload_path_denied",
                                    "independent_rgb_corroborated": False,
                                    "flight_control_enabled": False,
                                    "physical_release_enabled": False,
                                },
                            )
                            rgb_fire_verifier_unavailable_reported = True
                else:
                    # The verifier only corroborates primary fire candidates; it
                    # never creates targets on its own.  Skipping it on an empty
                    # fire frame removes a second full-frame inference from the
                    # common person/vehicle-only patrol path without weakening the
                    # evidence contract for a real primary candidate.
                    if not _has_fire_candidates(detections):
                        rgb_fire_verifier_skipped_no_candidate_frames += 1
                        detections = self.rgb_fire_corroborator.corroborate(
                            detections,
                            (),
                        ).detections
                    else:
                        rgb_fire_verifier_assessments += 1
                        verifier_started_s = time.perf_counter()
                        try:
                            verifier_detections = self.rgb_fire_verifier.detect(captured.image_bgr)
                        except (OSError, RuntimeError, TypeError, ValueError) as exc:
                            rgb_fire_verifier_failures += 1
                            corroboration = self.rgb_fire_corroborator.fail_closed(detections)
                            self.mission.audit.append(
                                "perception.rgb_fire_verifier_failed",
                                max(captured.captured_at_s, time.monotonic()),
                                {
                                    "frame_id": captured.frame_id,
                                    "error_type": type(exc).__name__,
                                    "fallback": "patrol_continues_payload_path_denied",
                                    "independent_rgb_corroborated": False,
                                    "flight_control_enabled": False,
                                    "physical_release_enabled": False,
                                },
                            )
                        else:
                            rgb_fire_verifier_inferences += 1
                            corroboration = self.rgb_fire_corroborator.corroborate(
                                detections,
                                verifier_detections,
                            )
                            if not corroboration.evidence_qualified:
                                rgb_fire_verifier_unqualified_frames += 1
                            if corroboration.corroborated_detection_count:
                                rgb_fire_verifier_corroborated_frames += 1
                                rgb_fire_verifier_corroborated_detections += (
                                    corroboration.corroborated_detection_count
                                )
                        detections = corroboration.detections
                        rgb_fire_verifier_latency_ms.append(
                            (time.perf_counter() - verifier_started_s) * 1_000.0
                        )
                inference_elapsed_ms = (time.perf_counter() - inference_started_s) * 1_000.0
                inference_latency_ms.append(inference_elapsed_ms)
                if self.semantic_context_runner is not None:
                    latest_semantic_context = self.semantic_context_runner.latest_snapshot()
                    if (
                        latest_semantic_context is not None
                        and latest_semantic_context.frame_id != last_semantic_context_frame_id
                    ):
                        last_semantic_context_frame_id = latest_semantic_context.frame_id
                        semantic_context_latency_ms.append(
                            latest_semantic_context.processing_time_ms
                        )
                        self.mission.audit.append(
                            "perception.semantic_context_updated",
                            max(captured.captured_at_s, time.monotonic()),
                            {
                                "source_frame_id": latest_semantic_context.frame_id,
                                "state": latest_semantic_context.state.value,
                                "region_count": len(latest_semantic_context.regions),
                                "regions": tuple(
                                    {
                                        "label": region.label,
                                        "class_id": region.class_id,
                                        "bbox": (
                                            region.bbox.x1,
                                            region.bbox.y1,
                                            region.bbox.x2,
                                            region.bbox.y2,
                                        ),
                                        "frame_area_fraction": region.frame_area_fraction,
                                        "bbox_fill_fraction": region.bbox_fill_fraction,
                                        "categorical_mask_only": True,
                                    }
                                    for region in latest_semantic_context.regions
                                ),
                                "error_type": latest_semantic_context.error_type,
                                "processing_time_ms": (latest_semantic_context.processing_time_ms),
                                "confidence_available": False,
                                "target_pool_identity_authority": False,
                                "advisory_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    if (
                        latest_semantic_context is not None
                        and latest_semantic_context.state is SemanticContextState.VALID
                        and time.monotonic() - latest_semantic_context.produced_at_s
                        > self.config.semantic_context_maximum_age_s
                        and semantic_context_stale_reported_frame_id
                        != latest_semantic_context.frame_id
                    ):
                        semantic_context_stale += 1
                        semantic_context_stale_reported_frame_id = latest_semantic_context.frame_id
                        self.mission.audit.append(
                            "perception.semantic_context_stale",
                            max(captured.captured_at_s, time.monotonic()),
                            {
                                "source_frame_id": latest_semantic_context.frame_id,
                                "maximum_age_s": self.config.semantic_context_maximum_age_s,
                                "region_count": 0,
                                "metadata_valid": False,
                                "advisory_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                frame_motion_at_s = time.monotonic()
                telemetry = self.telemetry_provider.snapshot(now_s=frame_motion_at_s)
                if frame_motion_at_s - last_telemetry_diagnostics_at_s >= 5.0:
                    diagnostics = getattr(self.telemetry_provider, "diagnostics", None)
                    if callable(diagnostics):
                        self.mission.audit.append(
                            "telemetry.pixhawk_diagnostics",
                            frame_motion_at_s,
                            diagnostics(now_s=frame_motion_at_s),
                        )
                    last_telemetry_diagnostics_at_s = frame_motion_at_s
                unified_camera_motion = None
                if self.attitude_camera_motion is not None:
                    try:
                        unified_camera_motion = self.attitude_camera_motion.update(
                            telemetry,
                            captured_at_s=captured.captured_at_s,
                        )
                    except ValueError as exc:
                        self.mission.audit.append(
                            "tracking.attitude_camera_motion_rejected",
                            max(captured.captured_at_s, frame_motion_at_s),
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "metadata_only": True,
                                "flight_control_enabled": False,
                            },
                        )
                    if unified_camera_motion is not None and not attitude_camera_motion_reported:
                        self.mission.audit.append(
                            "tracking.attitude_camera_motion_ready",
                            max(captured.captured_at_s, frame_motion_at_s),
                            {
                                "frame_id": captured.frame_id,
                                "dx": unified_camera_motion.dx,
                                "dy": unified_camera_motion.dy,
                                "scale": unified_camera_motion.scale,
                                "rotation_deg": unified_camera_motion.rotation_deg,
                                "confidence": unified_camera_motion.confidence,
                                "source": "pixhawk_attitude_calibrated_camera",
                                "metadata_only": True,
                                "flight_control_enabled": False,
                            },
                        )
                        attitude_camera_motion_reported = True
                if self.monocular_avoidance is not None:
                    avoidance_started_s = time.perf_counter()
                    avoidance_error_type = None
                    try:
                        latest_monocular_avoidance = self.monocular_avoidance.update(
                            captured.image_bgr,
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            produced_at_s=time.monotonic(),
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        monocular_avoidance_errors += 1
                        avoidance_error_type = type(exc).__name__
                        failed_at_s = max(captured.captured_at_s, time.monotonic())
                        latest_monocular_avoidance = MonocularCollisionRiskEvaluator().invalid(
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            produced_at_s=failed_at_s,
                            reason=f"PROCESSING_{avoidance_error_type.upper()}",
                            processing_time_ms=(time.perf_counter() - avoidance_started_s)
                            * 1_000.0,
                        )
                        self.mission.audit.append(
                            "avoidance.processing_failed",
                            failed_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "error_type": avoidance_error_type,
                                "advisory_only": True,
                                "flight_control_enabled": False,
                            },
                        )
                    monocular_avoidance_assessments += 1
                    avoidance_elapsed_ms = max(
                        latest_monocular_avoidance.processing_time_ms,
                        (time.perf_counter() - avoidance_started_s) * 1_000.0,
                    )
                    monocular_avoidance_latency_ms.append(avoidance_elapsed_ms)
                    if latest_monocular_avoidance.state is CollisionRiskState.INVALID:
                        monocular_avoidance_invalid += 1
                    elif latest_monocular_avoidance.state is CollisionRiskState.CAUTION:
                        monocular_avoidance_caution += 1
                    elif latest_monocular_avoidance.state is CollisionRiskState.AVOID:
                        monocular_avoidance_avoid += 1
                    avoidance_fingerprint = (
                        latest_monocular_avoidance.state.value,
                        latest_monocular_avoidance.reason,
                    )
                    if avoidance_fingerprint != last_monocular_avoidance_fingerprint:
                        self.mission.audit.append(
                            "avoidance.state_changed",
                            max(captured.captured_at_s, time.monotonic()),
                            _monocular_avoidance_details(latest_monocular_avoidance),
                        )
                        recent_events.append(
                            "OBS " + latest_monocular_avoidance.state.value.upper() + " ADVISORY"
                        )
                        last_monocular_avoidance_fingerprint = avoidance_fingerprint
                    camera_motion_values = (
                        latest_monocular_avoidance.camera_motion_dx,
                        latest_monocular_avoidance.camera_motion_dy,
                        latest_monocular_avoidance.camera_motion_scale,
                        latest_monocular_avoidance.camera_motion_confidence,
                    )
                    if unified_camera_motion is None and all(
                        value is not None for value in camera_motion_values
                    ):
                        unified_camera_motion = CameraMotionEstimate(
                            dx=float(camera_motion_values[0]),
                            dy=float(camera_motion_values[1]),
                            scale=float(camera_motion_values[2]),
                            confidence=float(camera_motion_values[3]),
                            rotation_deg=float(
                                latest_monocular_avoidance.camera_motion_rotation_deg or 0.0
                            ),
                            aspect_ratio=float(
                                latest_monocular_avoidance.camera_motion_aspect_ratio or 1.0
                            ),
                            affine=latest_monocular_avoidance.camera_motion_affine,
                        )
                operator_tracked_target_count = (
                    len(self.selection_target_pool.tracked_track_ids)
                    if self.selection_target_pool is not None
                    else 0
                )
                target_pool_wire_interval_s = _target_pool_status_interval_s(
                    normal_interval_s=normal_target_pool_status_interval_s,
                    operator_trk_interval_s=operator_trk_target_pool_status_interval_s,
                    exclusive_lock_interval_s=exclusive_target_pool_status_interval_s,
                    operator_tracked_target_count=operator_tracked_target_count,
                    exclusive_lock_track_id=exclusive_lock_track_id,
                )
                if self.operator_bridge is not None:
                    self.operator_bridge.target_pool_status_heartbeat_s = (
                        target_pool_wire_interval_s
                    )
                if exclusive_lock_track_id != last_exclusive_lock_track_id:
                    self.mission.audit.append(
                        "tracking.exclusive_lock_rate_changed",
                        max(captured.captured_at_s, time.monotonic()),
                        {
                            "enabled": exclusive_lock_track_id is not None,
                            "target_id": exclusive_lock_track_id,
                            "short_term_frame_stride": (
                                1 if exclusive_lock_track_id is not None else None
                            ),
                            "target_pool_refresh_hz": round(
                                1.0 / target_pool_wire_interval_s,
                                1,
                            ),
                            "metadata_only": True,
                            "flight_control_enabled": False,
                        },
                    )
                    last_exclusive_lock_track_id = exclusive_lock_track_id
                short_term_result: ShortTermTrackingResult | None = None
                unified_update = None
                if self.short_term_tracker is not None:
                    short_term_started_s = time.perf_counter()
                    try:
                        short_term_arguments: dict[str, object] = {
                            "captured_at_s": captured.captured_at_s,
                            "camera_motion": unified_camera_motion,
                            # Target-excluded background flow is the authoritative
                            # camera transform when it is available.  Monocular
                            # motion remains a bounded fallback for blank scenes.
                            "prefer_background_motion": True,
                            "background_exclusion_boxes": tuple(
                                detection.bbox for detection in detections
                            ),
                        }
                        if exclusive_lock_track_id is not None:
                            short_term_arguments["exclusive_track_id"] = exclusive_lock_track_id
                        short_term_result = self.short_term_tracker.update_frame(
                            captured.image_bgr,
                            **short_term_arguments,
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        short_term_tracking_errors += 1
                        self.mission.audit.append(
                            "tracking.short_term_failed",
                            max(captured.captured_at_s, time.monotonic()),
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "fallback": "kalman_prediction_only",
                                "identity_observation_created": False,
                                "flight_control_enabled": False,
                            },
                        )
                    else:
                        short_term_tracking_updates += 1
                        if short_term_result.camera_motion is not None and (
                            unified_camera_motion is None
                            or (short_term_result.camera_motion_source or "").startswith(
                                "background_"
                            )
                        ):
                            unified_camera_motion = short_term_result.camera_motion
                        if short_term_result.camera_motion is not None and (
                            short_term_result.camera_motion_source or ""
                        ).startswith("background_"):
                            short_term_tracking_camera_motion_estimates += 1
                            if not short_term_tracking_camera_motion_reported:
                                self.mission.audit.append(
                                    "tracking.background_camera_motion_ready",
                                    max(captured.captured_at_s, time.monotonic()),
                                    {
                                        "frame_id": captured.frame_id,
                                        "dx": short_term_result.camera_motion.dx,
                                        "dy": short_term_result.camera_motion.dy,
                                        "scale": short_term_result.camera_motion.scale,
                                        "rotation_deg": (
                                            short_term_result.camera_motion.rotation_deg
                                        ),
                                        "aspect_ratio": (
                                            short_term_result.camera_motion.aspect_ratio
                                        ),
                                        "affine": short_term_result.camera_motion.affine,
                                        "homography": short_term_result.camera_motion.homography,
                                        "confidence": (short_term_result.camera_motion.confidence),
                                        "feature_count": (
                                            short_term_result.camera_motion_feature_count
                                        ),
                                        "target_regions_excluded": True,
                                        "source": short_term_result.camera_motion_source,
                                    },
                                )
                                short_term_tracking_camera_motion_reported = True
                        if short_term_result.status is ShortTermTrackingStatus.INVALID:
                            short_term_tracking_invalid += 1
                        short_term_tracking_optical_flow_hints += (
                            short_term_result.optical_flow_hint_count
                        )
                        short_term_tracking_template_hints += short_term_result.template_hint_count
                        short_term_tracking_latency_ms.append(
                            max(
                                short_term_result.processing_time_ms,
                                (time.perf_counter() - short_term_started_s) * 1_000.0,
                            )
                        )
                if self.unified_target_pool is not None:
                    unified_observations = tuple(
                        TargetObservation.from_detection(
                            detection,
                            appearance_reliable=False,
                        )
                        for detection in detections
                    )
                    person_reid_domain_enabled = (
                        exclusive_lock_track_id is None or exclusive_lock_family == "person"
                    )
                    if self.person_reid_encoder is not None and person_reid_domain_enabled:
                        person_reid_labels = _reid_allowed_labels(
                            self.person_reid_encoder,
                            fallback=frozenset({"person", "firefighter"}),
                        )
                        if not _has_reid_candidates(detections, person_reid_labels):
                            person_reid_no_candidate_frames += 1
                        else:
                            person_reid_now_s = time.monotonic()
                            if exclusive_lock_track_id is not None:
                                person_reid_due, person_reid_forced = True, False
                            else:
                                person_recovery_required = _reid_recovery_required(
                                    self.unified_target_pool,
                                    person_reid_labels,
                                )
                                person_reid_due, person_reid_forced = _reid_inference_due(
                                    frame_index=processed,
                                    now_s=person_reid_now_s,
                                    last_inference_at_s=last_person_reid_at_s,
                                    frame_stride=self.config.person_reid_frame_stride,
                                    frame_phase=0,
                                    maximum_interval_s=self.config.reid_maximum_interval_s,
                                    recovery_required=person_recovery_required,
                                )
                            if person_reid_due:
                                person_reid_started_s = time.perf_counter()
                                person_reid_inferences += 1
                                person_reid_forced_recoveries += int(person_reid_forced)
                                last_person_reid_at_s = person_reid_now_s
                                try:
                                    unified_observations = (
                                        self.person_reid_encoder.encode_detections(
                                            captured.image_bgr,
                                            detections,
                                        )
                                    )
                                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                    person_reid_failures += 1
                                    self.mission.audit.append(
                                        "tracking.person_reid_failed",
                                        max(captured.captured_at_s, time.monotonic()),
                                        {
                                            "frame_id": captured.frame_id,
                                            "error_type": type(exc).__name__,
                                            "fallback": "motion_only",
                                            "identity_recovery_enabled": False,
                                            "flight_control_enabled": False,
                                        },
                                    )
                                finally:
                                    person_reid_latency_ms.append(
                                        (time.perf_counter() - person_reid_started_s) * 1_000.0
                                    )
                            else:
                                person_reid_skipped_frames += 1
                    elif self.person_reid_encoder is not None:
                        person_reid_skipped_frames += 1
                    vehicle_reid_domain_enabled = (
                        exclusive_lock_track_id is None or exclusive_lock_family == "vehicle"
                    )
                    if self.vehicle_reid_encoder is not None and vehicle_reid_domain_enabled:
                        vehicle_reid_labels = _reid_allowed_labels(
                            self.vehicle_reid_encoder,
                            fallback=frozenset({"vehicle", "car", "van", "bus", "truck"}),
                        )
                        if not _has_reid_candidates(detections, vehicle_reid_labels):
                            vehicle_reid_no_candidate_frames += 1
                        else:
                            vehicle_reid_now_s = time.monotonic()
                            if exclusive_lock_track_id is not None:
                                vehicle_reid_due, vehicle_reid_forced = True, False
                            else:
                                vehicle_recovery_required = _reid_recovery_required(
                                    self.unified_target_pool,
                                    vehicle_reid_labels,
                                )
                                vehicle_reid_due, vehicle_reid_forced = _reid_inference_due(
                                    frame_index=processed,
                                    now_s=vehicle_reid_now_s,
                                    last_inference_at_s=last_vehicle_reid_at_s,
                                    frame_stride=self.config.vehicle_reid_frame_stride,
                                    frame_phase=(
                                        1 if self.config.vehicle_reid_frame_stride > 1 else 0
                                    ),
                                    maximum_interval_s=self.config.reid_maximum_interval_s,
                                    recovery_required=vehicle_recovery_required,
                                )
                            if vehicle_reid_due:
                                vehicle_reid_started_s = time.perf_counter()
                                vehicle_reid_inferences += 1
                                vehicle_reid_forced_recoveries += int(vehicle_reid_forced)
                                last_vehicle_reid_at_s = vehicle_reid_now_s
                                try:
                                    vehicle_observations = (
                                        self.vehicle_reid_encoder.encode_detections(
                                            captured.image_bgr,
                                            detections,
                                        )
                                    )
                                    unified_observations = _merge_reid_observations(
                                        unified_observations,
                                        vehicle_observations,
                                    )
                                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                    vehicle_reid_failures += 1
                                    self.mission.audit.append(
                                        "tracking.vehicle_reid_failed",
                                        max(captured.captured_at_s, time.monotonic()),
                                        {
                                            "frame_id": captured.frame_id,
                                            "error_type": type(exc).__name__,
                                            "fallback": "motion_and_other_reid_domains",
                                            "vehicle_identity_recovery_enabled": False,
                                            "flight_control_enabled": False,
                                        },
                                    )
                                finally:
                                    vehicle_reid_latency_ms.append(
                                        (time.perf_counter() - vehicle_reid_started_s) * 1_000.0
                                    )
                            else:
                                vehicle_reid_skipped_frames += 1
                    elif self.vehicle_reid_encoder is not None:
                        vehicle_reid_skipped_frames += 1
                    aircraft_appearance_domain_enabled = (
                        exclusive_lock_track_id is None or exclusive_lock_family == "aircraft"
                    )
                    if (
                        self.aircraft_appearance_encoder is not None
                        and aircraft_appearance_domain_enabled
                    ):
                        aircraft_labels = _reid_allowed_labels(
                            self.aircraft_appearance_encoder,
                            fallback=_AIRCRAFT_LOCK_MODEL_LABELS,
                        )
                        if _has_reid_candidates(detections, aircraft_labels):
                            try:
                                aircraft_observations = (
                                    self.aircraft_appearance_encoder.encode_detections(
                                        captured.image_bgr,
                                        detections,
                                    )
                                )
                                unified_observations = _merge_reid_observations(
                                    unified_observations,
                                    aircraft_observations,
                                )
                            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                self.mission.audit.append(
                                    "tracking.aircraft_appearance_failed",
                                    max(captured.captured_at_s, time.monotonic()),
                                    {
                                        "frame_id": captured.frame_id,
                                        "error_type": type(exc).__name__,
                                        "fallback": "class_aware_motion_and_short_term_tracker",
                                        "aircraft_identity_recovery_enabled": False,
                                        "flight_control_enabled": False,
                                    },
                                )
                    if self.selection_target_pool is not None:
                        try:
                            unified_observations = (
                                *unified_observations,
                                *self.selection_target_pool.observations_for_next_pool_update(),
                            )
                        except (RuntimeError, TypeError, ValueError) as exc:
                            selection_target_pool_errors += 1
                            self.mission.audit.append(
                                "tracking.selection_pool_observation_failed",
                                max(captured.captured_at_s, time.monotonic()),
                                {
                                    "frame_id": captured.frame_id,
                                    "error_type": type(exc).__name__,
                                    "manual_identity_fabricated": False,
                                    "flight_control_enabled": False,
                                },
                            )
                    try:
                        unified_update = self.unified_target_pool.update(
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            observations=unified_observations,
                            camera_motion=unified_camera_motion,
                            motion_hints=(
                                short_term_result.hints if short_term_result is not None else ()
                            ),
                            visual_confirmation_track_ids=(
                                self.selection_target_pool.visual_confirmation_track_ids
                                if self.selection_target_pool is not None
                                else ()
                            ),
                        )
                    except (RuntimeError, TypeError, ValueError) as exc:
                        unified_target_pool_errors += 1
                        if self.short_term_tracker is not None:
                            try:
                                self.short_term_tracker.synchronize_tracks(())
                            except (RuntimeError, TypeError, ValueError):
                                short_term_tracking_errors += 1
                        self.mission.audit.append(
                            "tracking.unified_target_pool_failed",
                            max(captured.captured_at_s, time.monotonic()),
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "metadata_valid": False,
                                "flight_control_enabled": False,
                            },
                        )
                    else:
                        if self.selection_target_pool is not None:
                            try:
                                selection_sync = self.selection_target_pool.after_pool_update(
                                    now_s=max(captured.captured_at_s, time.monotonic())
                                )
                            except (RuntimeError, TypeError, ValueError) as exc:
                                selection_target_pool_errors += 1
                                self.mission.audit.append(
                                    "tracking.selection_pool_sync_failed",
                                    max(captured.captured_at_s, time.monotonic()),
                                    {
                                        "frame_id": captured.frame_id,
                                        "error_type": type(exc).__name__,
                                        "flight_control_enabled": False,
                                    },
                                )
                            else:
                                selection_target_pool_syncs += 1
                                selection_target_pool_bindings += int(
                                    selection_sync.bound_track_id is not None
                                )
                                selection_target_pool_pending += int(
                                    selection_sync.pending_manual_observation
                                )
                                if selection_sync.bound_track_id is not None:
                                    self.mission.audit.append(
                                        "tracking.selection_pool_bound",
                                        max(captured.captured_at_s, time.monotonic()),
                                        {
                                            "frame_id": captured.frame_id,
                                            "target_id": selection_sync.bound_track_id,
                                            "primary_switch_latency_ms": (
                                                selection_sync.primary_switch_latency_ms
                                            ),
                                            "background_locked_track_ids": (
                                                selection_sync.background_locked_track_ids
                                            ),
                                            "metadata_only": True,
                                            "flight_control_enabled": False,
                                        },
                                    )
                        # Selection synchronization may lock, unlock, or switch
                        # the primary track after UnifiedTargetPool.update()
                        # created its immutable snapshot. Refresh it here so the
                        # same frame's ranging, approach state and wire metadata
                        # all observe one coherent target state.
                        refreshed_tracks = self.unified_target_pool.snapshots()
                        refreshed_primary_id = next(
                            (track.track_id for track in refreshed_tracks if track.primary),
                            None,
                        )
                        unified_update = replace(
                            unified_update,
                            tracks=refreshed_tracks,
                            primary_track_id=refreshed_primary_id,
                        )
                        unified_target_pool_updates += 1
                        unified_target_pool_maximum_tracks = max(
                            unified_target_pool_maximum_tracks,
                            len(unified_update.tracks),
                        )
                        unified_target_pool_created_tracks += len(unified_update.created_track_ids)
                        unified_target_pool_recovered_tracks += len(
                            unified_update.recovered_track_ids
                        )
                        unified_target_pool_lost_tracks += len(unified_update.lost_track_ids)
                        short_term_tracking_accepted_hints += (
                            unified_update.accepted_motion_hint_count
                        )
                        short_term_tracking_rejected_hints += (
                            unified_update.rejected_motion_hint_count
                        )
                        unified_target_pool_association_ms.append(
                            unified_update.association_latency_ms
                        )
                        if self.short_term_tracker is not None:
                            try:
                                synchronized_tracks = self.unified_target_pool.snapshots()
                                if exclusive_lock_track_id is None:
                                    self.short_term_tracker.synchronize_tracks(synchronized_tracks)
                                else:
                                    self.short_term_tracker.synchronize_tracks(
                                        synchronized_tracks,
                                        exclusive_track_id=exclusive_lock_track_id,
                                    )
                            except (RuntimeError, TypeError, ValueError) as exc:
                                short_term_tracking_errors += 1
                                self.mission.audit.append(
                                    "tracking.short_term_synchronization_failed",
                                    max(captured.captured_at_s, time.monotonic()),
                                    {
                                        "frame_id": captured.frame_id,
                                        "error_type": type(exc).__name__,
                                        "identity_observation_created": False,
                                        "flight_control_enabled": False,
                                    },
                                )
                        if any(
                            (
                                unified_update.created_track_ids,
                                unified_update.recovered_track_ids,
                                unified_update.lost_track_ids,
                                unified_update.removed_track_ids,
                            )
                        ):
                            self.mission.audit.append(
                                "tracking.unified_target_pool_changed",
                                max(captured.captured_at_s, time.monotonic()),
                                {
                                    "frame_id": captured.frame_id,
                                    "track_count": len(unified_update.tracks),
                                    "created_track_ids": unified_update.created_track_ids,
                                    "recovered_track_ids": unified_update.recovered_track_ids,
                                    "lost_track_ids": unified_update.lost_track_ids,
                                    "removed_track_ids": unified_update.removed_track_ids,
                                    "dropped_observation_count": (
                                        unified_update.dropped_observation_count
                                    ),
                                    "primary_track_id": unified_update.primary_track_id,
                                    "visual_confirmed_track_ids": (
                                        unified_update.visual_confirmed_track_ids
                                    ),
                                    "metadata_only": True,
                                    "flight_control_enabled": False,
                                },
                            )
                if self.prediction_writer is not None:
                    self.prediction_writer.append(
                        frame_id=captured.frame_id,
                        captured_at_s=captured.captured_at_s,
                        detections=detections,
                        inference_latency_ms=inference_elapsed_ms,
                    )
                if (
                    self.identity_prediction_writer is not None
                    and not identity_tracking_log_disabled_after_error
                ):
                    try:
                        self.identity_prediction_writer.append(
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            tracks=(unified_update.tracks if unified_update is not None else ()),
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        identity_tracking_log_errors += 1
                        identity_tracking_log_disabled_after_error = True
                        self.mission.audit.append(
                            "tracking.identity_prediction_log_failed",
                            max(captured.captured_at_s, time.monotonic()),
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "logging_disabled_for_remainder_of_run": True,
                                "perception_continues": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    else:
                        identity_tracking_log_frames += 1
                frame_event_at_s = time.monotonic()
                telemetry = with_person_detector_health(
                    telemetry,
                    healthy=(
                        self.config.person_safety_evidence_qualified
                        and self.detector.covers_labels(self.mission.config.person_labels)
                    ),
                )
                if self.config.observe_pixhawk_lifecycle:
                    telemetry = with_observed_flight_mode_permission(
                        telemetry,
                        allowed_modes=self.config.allowed_auto_modes,
                    )
                    self._advance_observed_pixhawk_lifecycle(
                        telemetry=telemetry,
                        now_s=frame_event_at_s,
                    )
                latest_ranging_solution = None
                target_relative_bearings: dict[str, float] = {}
                target_estimated_ranges: dict[str, float] = {}
                target_estimated_speeds: dict[str, float] = {}
                if self.ranging_engine is not None and unified_update is not None:
                    ranging_started_s = time.perf_counter()
                    try:
                        if (
                            self.metric_depth_runner is not None
                            and exclusive_lock_track_id is not None
                        ):
                            metric_track = _target_pool_snapshot(
                                self.unified_target_pool,
                                exclusive_lock_track_id,
                            )
                            if metric_track is not None:
                                self.metric_depth_runner.submit(
                                    image_bgr=captured.image_bgr,
                                    target_id=metric_track.track_id,
                                    bbox=metric_track.bbox,
                                    target_label=metric_track.label,
                                    frame_id=captured.frame_id,
                                    captured_at_s=captured.captured_at_s,
                                    now_s=frame_event_at_s,
                                )
                                metric_result = self.metric_depth_runner.latest_result()
                                if (
                                    metric_result is not None
                                    and metric_result.frame_id
                                    != self._metric_depth_last_result_frame_id
                                ):
                                    self._metric_depth_last_result_frame_id = metric_result.frame_id
                                    self._latest_metric_depth_result = metric_result
                                    if self.depth_grid_publisher is not None:
                                        try:
                                            fragment_count = self.depth_grid_publisher.publish(
                                                metric_result.depth_grid
                                            )
                                            self.mission.audit.append(
                                                "ranging.depth_grid_published",
                                                frame_event_at_s,
                                                {
                                                    "frame_id": metric_result.frame_id,
                                                    "width": metric_result.depth_grid.width,
                                                    "height": metric_result.depth_grid.height,
                                                    "fragment_count": fragment_count,
                                                },
                                            )
                                        except (
                                            OSError,
                                            RuntimeError,
                                            TypeError,
                                            ValueError,
                                        ) as exc:
                                            self.mission.audit.append(
                                                "ranging.depth_grid_publish_failed",
                                                frame_event_at_s,
                                                {
                                                    "error_type": type(exc).__name__,
                                                    "frame_loop_continues": True,
                                                },
                                            )
                                    self.mission.audit.append(
                                        "ranging.metric_depth_updated",
                                        frame_event_at_s,
                                        {
                                            "frame_id": metric_result.frame_id,
                                            "target_id": metric_result.target_id,
                                            "slant_range_m": metric_result.slant_range_m,
                                            "raw_slant_range_m": (
                                                metric_result.raw_slant_range_m
                                            ),
                                            "sigma_m": metric_result.sigma_m,
                                            "valid_pixel_count": metric_result.valid_pixel_count,
                                            "processing_time_ms": (
                                                metric_result.processing_time_ms
                                            ),
                                            "providers": metric_result.provider_names,
                                            "calibration_scale": (
                                                metric_result.calibration_scale
                                            ),
                                            "calibration_offset_m": (
                                                metric_result.calibration_offset_m
                                            ),
                                            "calibration_profile": (
                                                metric_result.calibration_profile
                                            ),
                                            "grid_width": metric_result.depth_grid.width,
                                            "grid_height": metric_result.depth_grid.height,
                                            "exclusive_lck_only": True,
                                        },
                                    )
                                if (
                                    self.metric_depth_runner.failure_count
                                    != self._metric_depth_last_failure_count
                                ):
                                    self._metric_depth_last_failure_count = (
                                        self.metric_depth_runner.failure_count
                                    )
                                    self.mission.audit.append(
                                        "ranging.metric_depth_failed",
                                        frame_event_at_s,
                                        {
                                            "failure_count": (
                                                self.metric_depth_runner.failure_count
                                            ),
                                            "error": self.metric_depth_runner.last_error,
                                            "frame_loop_continues": True,
                                        },
                                    )
                        for track in unified_update.tracks:
                            # Range/bearing metadata is per target. A malformed
                            # observation, one stale estimate, or a transient
                            # model error must not blank the metrics for the
                            # other DET/TRK/LCK boxes in this pool update.
                            try:
                                center_x, center_y = track.bbox.center
                                target_observation = TargetImageObservation(
                                    target_id=track.track_id,
                                    frame_id=captured.frame_id,
                                    captured_at_s=captured.captured_at_s,
                                    center_x=center_x,
                                    center_y=center_y,
                                    center_sigma_px=self.ranging_config.target_center_sigma_px,
                                )
                                target_relative_bearings[track.track_id] = (
                                    self.ranging_engine.relative_bearing_deg(
                                        calibration=self.ranging_config.calibration,
                                        target=target_observation,
                                    )
                                )
                                track_solution = self._evaluate_track_ranging(
                                    track=track,
                                    captured=captured,
                                    telemetry=telemetry,
                                    now_s=frame_event_at_s,
                                    camera_motion=unified_camera_motion,
                                    exclusive_lock=(
                                        track.track_id == exclusive_lock_track_id
                                    ),
                                )
                                ranging_fingerprint = (
                                    track_solution.validity.value,
                                    track_solution.reasons,
                                    track_solution.sources,
                                    track_solution.rejected_sources,
                                )
                                if (
                                    self._ranging_solution_fingerprints.get(track.track_id)
                                    != ranging_fingerprint
                                ):
                                    self._ranging_solution_fingerprints[track.track_id] = (
                                        ranging_fingerprint
                                    )
                                    self.mission.audit.append(
                                        "ranging.target_solution_changed",
                                        frame_event_at_s,
                                        {
                                            "frame_id": captured.frame_id,
                                            "target_id": track.track_id,
                                            "label": track.label,
                                            "validity": track_solution.validity.value,
                                            "reasons": track_solution.reasons,
                                            "sources": track_solution.sources,
                                            "rejected_sources": track_solution.rejected_sources,
                                            "slant_range_m": track_solution.slant_range_m,
                                            "ground_range_m": track_solution.ground_range_m,
                                            "source_contributions": [
                                                {
                                                    "source": contribution.source,
                                                    "range_m": contribution.range_m,
                                                    "sigma_m": contribution.sigma_m,
                                                    "weight": contribution.weight,
                                                    "freshness_s": contribution.freshness_s,
                                                }
                                                for contribution in (
                                                    track_solution.source_contributions
                                                )
                                            ],
                                            "fusion_profile": track_solution.fusion_profile,
                                            "vehicle_profile": track_solution.vehicle_profile,
                                            "navigation_state": track_solution.navigation_state,
                                            "motion_regime": track_solution.motion_regime,
                                            "advisory_only": True,
                                        },
                                    )
                                if track_solution.slant_range_m is not None:
                                    target_estimated_ranges[track.track_id] = (
                                        track_solution.slant_range_m
                                    )
                                    target_speed_mps = self._estimate_target_speed_mps(
                                        track=track,
                                        solution=track_solution,
                                        telemetry=telemetry,
                                        captured_at_s=captured.captured_at_s,
                                        calibration=self.ranging_config.calibration,
                                    )
                                    if target_speed_mps is not None:
                                        target_estimated_speeds[track.track_id] = target_speed_mps
                                    self._target_metric_cache[track.track_id] = (
                                        track_solution.slant_range_m,
                                        target_speed_mps,
                                        frame_event_at_s,
                                    )
                                if track.track_id == unified_update.primary_track_id:
                                    latest_ranging_solution = track_solution
                            except (RuntimeError, TypeError, ValueError) as exc:
                                ranging_errors += 1
                                self.mission.audit.append(
                                    "ranging.processing_failed",
                                    frame_event_at_s,
                                    {
                                        "frame_id": captured.frame_id,
                                        "target_id": track.track_id,
                                        "error_type": type(exc).__name__,
                                        "error": str(exc)[:240],
                                        "isolated_target_failure": True,
                                        "advisory_only": True,
                                        "flight_control_enabled": False,
                                        "physical_release_enabled": False,
                                    },
                                )
                        active_track_ids = {track.track_id for track in unified_update.tracks}
                        if self._target_world_speed is not None:
                            self._target_world_speed.retain(active_track_ids)
                        for track_id in active_track_ids:
                            cached = self._target_metric_cache.get(track_id)
                            if cached is None:
                                continue
                            cached_range_m, cached_speed_mps, cached_at_s = cached
                            if frame_event_at_s - cached_at_s > 1.5:
                                continue
                            target_estimated_ranges.setdefault(track_id, cached_range_m)
                            if cached_speed_mps is not None:
                                target_estimated_speeds.setdefault(track_id, cached_speed_mps)
                        self._target_metric_cache = {
                            track_id: cached
                            for track_id, cached in self._target_metric_cache.items()
                            if track_id in active_track_ids and frame_event_at_s - cached[2] <= 1.5
                        }
                        if latest_ranging_solution is not None:
                            ranging_assessments += 1
                            if latest_ranging_solution.validity is RangeValidity.VALID:
                                ranging_valid += 1
                            elif latest_ranging_solution.validity is RangeValidity.DEGRADED:
                                ranging_degraded += 1
                            else:
                                ranging_invalid += 1
                            self.mission.audit.append(
                                "ranging.primary_target_solution",
                                frame_event_at_s,
                                _range_solution_details(
                                    latest_ranging_solution,
                                    telemetry=telemetry,
                                    now_s=frame_event_at_s,
                                ),
                            )
                    except Exception as exc:
                        # Ranging and optional dense-depth metadata share the
                        # live video loop.  Preserve the stream and operator
                        # heartbeat when a provider or integration defect
                        # rejects one frame; the audit retains the exact error
                        # for diagnosis and the next frame retries normally.
                        ranging_errors += 1
                        self.mission.audit.append(
                            "ranging.frame_processing_failed",
                            frame_event_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:240],
                                "frame_loop_continues": True,
                                "advisory_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    finally:
                        ranging_latency_ms.append(
                            (time.perf_counter() - ranging_started_s) * 1_000.0
                        )
                if self.patrol_advisory_engine is not None and unified_update is not None:
                    latest_patrol_assessment = None
                    try:
                        latest_patrol_assessment = self.patrol_advisory_engine.assess(
                            tracks=unified_update.tracks,
                            primary_target_id=unified_update.primary_track_id,
                            telemetry=telemetry,
                            now_s=frame_event_at_s,
                        )
                    except (RuntimeError, TypeError, ValueError) as exc:
                        patrol_advisory_errors += 1
                        self.mission.audit.append(
                            "patrol.advisory_failed",
                            frame_event_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "flight_control_enabled": False,
                            },
                        )
                    else:
                        patrol_advisory_assessments += 1
                        advisory = latest_patrol_assessment.return_to_observe
                        fingerprint = (
                            latest_patrol_assessment.phase.value,
                            latest_patrol_assessment.primary_target_id,
                            advisory.validity.value if advisory is not None else None,
                            advisory.direction.value if advisory is not None else None,
                        )
                        if fingerprint != last_patrol_fingerprint:
                            details: dict[str, object] = {
                                "frame_id": captured.frame_id,
                                "phase": latest_patrol_assessment.phase.value,
                                "primary_target_id": (latest_patrol_assessment.primary_target_id),
                                "reason": latest_patrol_assessment.reason,
                                "advisory_only": True,
                                "flight_control_enabled": False,
                            }
                            if advisory is not None:
                                patrol_return_to_observe += 1
                                details.update(
                                    {
                                        "return_direction": advisory.direction.value,
                                        "return_validity": advisory.validity.value,
                                        "evidence_age_s": advisory.evidence_age_s,
                                        "estimated_minimum_turn_radius_m": (
                                            advisory.estimated_minimum_turn_radius_m
                                        ),
                                        "operator_confirmation_required": True,
                                        "sitl_validation_required": True,
                                    }
                                )
                            self.mission.audit.append(
                                "patrol.state_changed",
                                frame_event_at_s,
                                details,
                            )
                            last_patrol_fingerprint = fingerprint
                observation = FrameObservation(
                    frame_id=captured.frame_id,
                    captured_at_s=captured.captured_at_s,
                    detections=detections,
                    telemetry=telemetry,
                )
                active_selection_id = (
                    self.selection_target_pool.active_command_id
                    if self.selection_target_pool is not None
                    else None
                )
                active_track_id = (
                    self.selection_target_pool.active_track_id
                    if self.selection_target_pool is not None
                    else None
                )
                active_unified_track = None
                if unified_update is not None and active_track_id is not None:
                    active_unified_track = next(
                        (
                            track
                            for track in unified_update.tracks
                            if track.track_id == active_track_id
                        ),
                        None,
                    )
                if self.fixed_camera_observation_engine is not None:
                    fixed_camera_status = self.fixed_camera_observation_engine.evaluate(
                        track=active_unified_track,
                        telemetry=telemetry,
                        now_s=frame_event_at_s,
                    )
                    fixed_camera_fingerprint = (
                        fixed_camera_status.state,
                        fixed_camera_status.reason,
                        fixed_camera_status.target_id,
                        fixed_camera_status.aligned,
                    )
                    if fixed_camera_fingerprint != last_fixed_camera_observation_fingerprint:
                        self.mission.audit.append(
                            "fixed_camera_observation.state_changed",
                            frame_event_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "state": fixed_camera_status.state.value,
                                "reason": fixed_camera_status.reason,
                                "target_id": fixed_camera_status.target_id,
                                "error_x_fraction": fixed_camera_status.error_x_fraction,
                                "error_y_fraction": fixed_camera_status.error_y_fraction,
                                "aligned": fixed_camera_status.aligned,
                                "roll_deg": fixed_camera_status.roll_deg,
                                "pitch_deg": fixed_camera_status.pitch_deg,
                                "heading_deg": fixed_camera_status.heading_deg,
                                "fixed_camera": True,
                            },
                        )
                        last_fixed_camera_observation_fingerprint = fixed_camera_fingerprint
                payload_target_intent = None
                if self.payload_target_coordinator is not None:
                    payload_target_intent = self.payload_target_coordinator.active_intent(
                        selection_command_id=active_selection_id,
                        track=active_unified_track,
                        now_s=frame_event_at_s,
                    )
                primary_range_evidence = None
                if latest_ranging_solution is not None and unified_update is not None:
                    primary_snapshot = next(
                        (
                            track
                            for track in unified_update.tracks
                            if track.track_id == unified_update.primary_track_id
                        ),
                        None,
                    )
                    if primary_snapshot is not None:
                        try:
                            primary_range_evidence = PrimaryRangeEvidence(
                                source_target_id=primary_snapshot.track_id,
                                source_frame_id=captured.frame_id,
                                source_captured_at_s=captured.captured_at_s,
                                source_label=primary_snapshot.label,
                                source_bbox=primary_snapshot.bbox,
                                solution=latest_ranging_solution,
                            )
                        except ValueError as exc:
                            self.mission.audit.append(
                                "ranging.mission_binding_rejected",
                                frame_event_at_s,
                                {
                                    "frame_id": captured.frame_id,
                                    "target_id": primary_snapshot.track_id,
                                    "reason": str(exc),
                                    "advisory_only": True,
                                    "flight_control_enabled": False,
                                    "physical_release_enabled": False,
                                },
                            )
                if self.mission.state.phase in {
                    MissionPhase.SEARCHING,
                    MissionPhase.AWAITING_AUTHORIZATION,
                    MissionPhase.DEPLOYMENT_READY,
                }:
                    outcome = self.mission.process_observation(
                        observation,
                        now_s=frame_event_at_s,
                        primary_range_evidence=primary_range_evidence,
                        payload_target_intent=payload_target_intent,
                        require_payload_target_intent=(self.payload_target_coordinator is not None),
                    )
                else:
                    outcome = ObservationOutcome(
                        phase=self.mission.state.phase,
                        tracks=(),
                        decisions=(),
                        challenge=None,
                    )
                for alert in outcome.alerts:
                    if self.alert_outbox is not None:
                        self.alert_outbox.enqueue(alert)
                    deliver_alert(
                        alert,
                        delivered_at_s=time.monotonic(),
                        retry=False,
                    )
                latest_payload_target_frame = None
                if self.payload_target_coordinator is not None:
                    try:
                        latest_payload_target_frame = self.payload_target_coordinator.prepare_frame(
                            selection_command_id=active_selection_id,
                            selected=active_unified_track,
                            fire_tracks=outcome.tracks,
                            now_s=frame_event_at_s,
                            wire_now_s=time.time(),
                        )
                    except (RuntimeError, TypeError, ValueError) as exc:
                        payload_target_errors += 1
                        self.payload_target_coordinator.clear()
                        self.mission.audit.append(
                            "payload_target.processing_failed",
                            frame_event_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "fallback": "no_slide_challenge_and_no_intent",
                                "hil_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                latest_approach_frame = None
                if self.approach_hil_coordinator is not None:
                    try:
                        latest_approach_frame = self.approach_hil_coordinator.prepare_frame(
                            selection_command_id=active_selection_id,
                            track=active_unified_track,
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            ranging=latest_ranging_solution,
                            avoidance=latest_monocular_avoidance,
                            telemetry=telemetry,
                            now_s=frame_event_at_s,
                            wire_now_s=time.time(),
                        )
                    except (RuntimeError, TypeError, ValueError) as exc:
                        approach_hil_errors += 1
                        self.approach_hil_coordinator.clear()
                        self.mission.audit.append(
                            "approach_hil.processing_failed",
                            frame_event_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "error": str(exc)[:240],
                                "fallback": "search_and_no_advice",
                                "sitl_hil_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    else:
                        approach_phase = latest_approach_frame.assessment.phase
                        if approach_phase is not last_approach_phase:
                            if approach_phase is ApproachHilPhase.ABORT_CLIMB_SIM:
                                approach_hil_aborts += 1
                            self.mission.audit.append(
                                "approach_hil.phase_changed",
                                frame_event_at_s,
                                {
                                    "phase": approach_phase.value,
                                    "target_id": latest_approach_frame.assessment.target_id,
                                    "reasons": latest_approach_frame.assessment.reasons,
                                    "advisory_only": True,
                                    "sitl_hil_only": True,
                                    "flight_control_enabled": False,
                                    "physical_release_enabled": False,
                                },
                            )
                            last_approach_phase = approach_phase
                if self.fixed_wing_aim_executor is not None:
                    aim_target = None
                    if (
                        active_unified_track is not None
                        and latest_approach_frame is not None
                        and latest_approach_frame.assessment.target_revision is not None
                    ):
                        aim_target = FixedWingAimTarget(
                            target_id=active_unified_track.track_id,
                            target_revision=latest_approach_frame.assessment.target_revision,
                            bbox=(
                                active_unified_track.predicted_bbox
                                if active_unified_track.state
                                in {
                                    UnifiedTrackState.OCCLUDED,
                                    UnifiedTrackState.REACQUIRING,
                                    UnifiedTrackState.RECOVERED,
                                }
                                else active_unified_track.bbox
                            ),
                            observed_at_s=active_unified_track.last_seen_at_s,
                            state=active_unified_track.state,
                            locked=active_unified_track.locked,
                            primary=active_unified_track.primary,
                        )
                    try:
                        aim_decision = self.fixed_wing_aim_executor.step(
                            target=aim_target,
                            telemetry=telemetry,
                            mode3_active=True,
                            execution_confirmed=(
                                self.approach_hil_coordinator is not None
                                and self.approach_hil_coordinator.controller.confirmation_accepted
                            ),
                            now_s=frame_event_at_s,
                        )
                    except (RuntimeError, TypeError, ValueError) as exc:
                        self.fixed_wing_aim_executor.controller.clear()
                        self.mission.audit.append(
                            "fixed_wing_aim.control_failed",
                            frame_event_at_s,
                            {
                                "frame_id": captured.frame_id,
                                "error_type": type(exc).__name__,
                                "flight_control_enabled": True,
                            },
                        )
                    else:
                        pilot_input_cancelled = aim_decision.reason == "pilot_input_cancelled" or (
                            latest_approach_frame is not None
                            and latest_approach_frame.status.pilot_input_cancelled
                        )
                        aim_control_active = aim_decision.state in {
                            FixedWingAimState.PRESTREAM,
                            FixedWingAimState.ACTIVE,
                            FixedWingAimState.REACQUIRING,
                        }
                        if pilot_input_cancelled and self.approach_hil_coordinator is not None:
                            self.approach_hil_coordinator.cancel_execution(
                                now_s=frame_event_at_s,
                                pilot_input_cancelled=True,
                            )
                        if latest_approach_frame is not None:
                            latest_approach_frame = replace(
                                latest_approach_frame,
                                status=replace(
                                    latest_approach_frame.status,
                                    aim_control_active=aim_control_active,
                                    pilot_input_cancelled=pilot_input_cancelled,
                                ),
                            )
                        if aim_decision.state is not last_fixed_wing_aim_state:
                            self.mission.audit.append(
                                "fixed_wing_aim.state_changed",
                                frame_event_at_s,
                                {
                                    "state": aim_decision.state.value,
                                    "reason": aim_decision.reason,
                                    "target_id": (
                                        aim_decision.setpoint.target_id
                                        if aim_decision.setpoint is not None
                                        else None
                                    ),
                                    "flight_control_enabled": True,
                                },
                            )
                            last_fixed_wing_aim_state = aim_decision.state
                remote_authorization_handled = False
                if self.operator_bridge is not None:
                    remote_mission_status_sequence = (
                        remote_mission_status_sequence + 1
                    ) & 0xFFFFFFFF
                    remote_safety_status_sequence = (remote_safety_status_sequence + 1) & 0xFFFFFFFF
                    produced_at_s = time.monotonic()
                    authorization_challenge_message = None
                    if outcome.challenge is not None:
                        remote_authorization_challenge_sequence = (
                            remote_authorization_challenge_sequence + 1
                        ) & 0xFFFFFFFF
                        authorization_challenge_message = (
                            build_authorization_challenge_status_message(
                                challenge=outcome.challenge,
                                sequence=remote_authorization_challenge_sequence,
                                produced_at_s=time.time(),
                                challenge_clock_now_s=time.monotonic(),
                            )
                        )
                    current_mission_status = self.mission.status()
                    mission_status_message = build_mission_status_message(
                        mission_id=self.mission.config.mission_id,
                        sequence=remote_mission_status_sequence,
                        status=current_mission_status,
                        outcome=outcome,
                        produced_at_s=produced_at_s,
                    )
                    safety_status_message = build_safety_status_message(
                        mission_id=self.mission.config.mission_id,
                        sequence=remote_safety_status_sequence,
                        status=current_mission_status,
                        outcome=outcome,
                        produced_at_s=produced_at_s,
                    )
                    patrol_status_message = None
                    if latest_patrol_assessment is not None and unified_update is not None:
                        remote_patrol_status_sequence = (
                            remote_patrol_status_sequence + 1
                        ) & 0xFFFFFFFF
                        patrol_status_message = build_patrol_status_message(
                            mission_id=self.mission.config.mission_id,
                            sequence=remote_patrol_status_sequence,
                            assessment=latest_patrol_assessment,
                            tracks=unified_update.tracks,
                            source_frame_id=captured.frame_id,
                            source_captured_at_s=captured.captured_at_s,
                            produced_at_s=produced_at_s,
                        )
                    range_status_message = None
                    if latest_ranging_solution is not None:
                        remote_range_status_sequence = (
                            remote_range_status_sequence + 1
                        ) & 0xFFFFFFFF
                        try:
                            range_status_message = build_range_status_message(
                                sequence=remote_range_status_sequence,
                                solution=replace(
                                    latest_ranging_solution,
                                    evaluated_at_s=produced_at_s,
                                ),
                                source_captured_at_s=captured.captured_at_s,
                            )
                        except (TypeError, ValueError) as exc:
                            # Range metadata is optional transport output. A
                            # schema/registry mismatch must never terminate the
                            # camera, detector, tracker, or LCK state machine.
                            remote_transport_errors += 1
                            self.mission.audit.append(
                                "operator.range_status_build_failed",
                                produced_at_s,
                                {
                                    "frame_id": captured.frame_id,
                                    "target_id": latest_ranging_solution.target_id,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc)[:240],
                                    "metadata_packet_dropped": True,
                                    "perception_continues": True,
                                },
                            )
                    release_status_message = None
                    release_decision = next(
                        (
                            decision
                            for decision in outcome.decisions
                            if decision.target_id == mission_status_message.target_id
                            and decision.deployment_window is not None
                        ),
                        None,
                    )
                    if release_decision is None:
                        release_decision = next(
                            (
                                decision
                                for decision in sorted(
                                    outcome.decisions,
                                    key=lambda item: item.priority_score,
                                    reverse=True,
                                )
                                if decision.deployment_window is not None
                            ),
                            None,
                        )
                    if release_decision is not None:
                        remote_release_status_sequence = (
                            remote_release_status_sequence + 1
                        ) & 0xFFFFFFFF
                        release_status_message = build_release_status_message(
                            sequence=remote_release_status_sequence,
                            solution=replace(
                                release_decision.deployment_window,
                                evaluated_at_s=produced_at_s,
                            ),
                        )
                    target_pool_status_messages = ()
                    if unified_update is not None:
                        operator_tracked_ids = (
                            self.selection_target_pool.tracked_track_ids
                            if self.selection_target_pool is not None
                            else ()
                        )
                        target_pool_fingerprint = (
                            operator_tracked_ids,
                            tuple(
                                (
                                    track.track_id,
                                    track.state.value,
                                    track.label,
                                    round(track.confidence, 2),
                                    round(track.tracking_quality, 2),
                                    track.locked,
                                    track.primary,
                                    track.actionable,
                                    track.reid_confirmed,
                                    (
                                        round(target_relative_bearings[track.track_id], 1)
                                        if track.track_id in target_relative_bearings
                                        else None
                                    ),
                                    (
                                        round(target_estimated_ranges[track.track_id], 1)
                                        if track.track_id in target_estimated_ranges
                                        else None
                                    ),
                                    (
                                        round(target_estimated_speeds[track.track_id], 1)
                                        if track.track_id in target_estimated_speeds
                                        else None
                                    ),
                                )
                                for track in unified_update.tracks
                            ),
                        )
                        target_pool_due = (
                            target_pool_fingerprint != last_target_pool_fingerprint
                            or last_target_pool_built_at_s is None
                            or produced_at_s - last_target_pool_built_at_s
                            >= target_pool_wire_interval_s
                        )
                        if target_pool_due:
                            remote_target_pool_revision = (
                                remote_target_pool_revision + 1
                            ) & 0xFFFFFFFF
                            sequence_start = (remote_target_pool_status_sequence + 1) & 0xFFFFFFFF
                            target_pool_status_messages = build_target_pool_status_messages(
                                sequence_start=sequence_start,
                                pool_revision=remote_target_pool_revision,
                                tracks=unified_update.tracks,
                                produced_at_s=produced_at_s,
                                include_tentative=False,
                                operator_tracked_ids=operator_tracked_ids,
                                relative_bearing_by_target_id=target_relative_bearings,
                                estimated_range_by_target_id=target_estimated_ranges,
                                target_speed_by_target_id=target_estimated_speeds,
                            )
                            page_count = len(target_pool_status_messages)
                            remote_target_pool_status_sequence = (
                                remote_target_pool_status_sequence + page_count
                            ) & 0xFFFFFFFF
                            last_target_pool_fingerprint = target_pool_fingerprint
                            last_target_pool_built_at_s = produced_at_s
                    scene_context_status_messages = ()
                    if latest_semantic_context is not None:
                        semantic_age_s = produced_at_s - latest_semantic_context.produced_at_s
                        semantic_wire_state = (
                            "STALE"
                            if semantic_age_s < 0.0
                            or semantic_age_s > self.config.semantic_context_maximum_age_s
                            else latest_semantic_context.state.value
                        )
                        scene_context_fingerprint = (
                            latest_semantic_context.frame_id,
                            semantic_wire_state,
                            tuple(
                                (
                                    region.label,
                                    region.bbox,
                                    round(region.frame_area_fraction, 4),
                                    round(region.bbox_fill_fraction, 4),
                                )
                                for region in latest_semantic_context.regions
                            ),
                        )
                        scene_context_due = (
                            scene_context_fingerprint != last_scene_context_wire_fingerprint
                            or last_scene_context_built_at_s is None
                            or produced_at_s - last_scene_context_built_at_s >= 0.5
                        )
                        if scene_context_due:
                            if scene_context_fingerprint != last_scene_context_wire_fingerprint:
                                remote_scene_context_revision = (
                                    remote_scene_context_revision + 1
                                ) & 0xFFFFFFFF
                            sequence_start = (remote_scene_context_status_sequence + 1) & 0xFFFFFFFF
                            scene_context_status_messages = build_scene_context_status_messages(
                                sequence_start=sequence_start,
                                context_revision=remote_scene_context_revision,
                                snapshot=latest_semantic_context,
                                produced_at_s=produced_at_s,
                                maximum_age_s=self.config.semantic_context_maximum_age_s,
                            )
                            remote_scene_context_status_sequence = (
                                remote_scene_context_status_sequence
                                + len(scene_context_status_messages)
                            ) & 0xFFFFFFFF
                            last_scene_context_wire_fingerprint = scene_context_fingerprint
                            last_scene_context_built_at_s = produced_at_s
                    operator_tracks = (
                        _operator_track_snapshots(self.unified_target_pool.snapshots())
                        if self.unified_target_pool is not None
                        else outcome.tracks
                    )
                    bridge_result = self.operator_bridge.process_frame(
                        tracks=operator_tracks,
                        frame_id=captured.frame_id,
                        captured_at_s=captured.captured_at_s,
                        produced_at_s=produced_at_s,
                        image_bgr=captured.image_bgr,
                        mission_status=mission_status_message,
                        safety_status=safety_status_message,
                        patrol_status=patrol_status_message,
                        range_status=range_status_message,
                        release_status=release_status_message,
                        approach_challenge=(
                            latest_approach_frame.challenge
                            if latest_approach_frame is not None
                            else None
                        ),
                        approach_status=(
                            latest_approach_frame.status
                            if latest_approach_frame is not None
                            else None
                        ),
                        payload_target_challenge=(
                            latest_payload_target_frame.challenge
                            if latest_payload_target_frame is not None
                            else None
                        ),
                        payload_target_status=(
                            latest_payload_target_frame.status
                            if latest_payload_target_frame is not None
                            else None
                        ),
                        target_pool_statuses=target_pool_status_messages,
                        scene_context_statuses=scene_context_status_messages,
                        authorization_challenge=authorization_challenge_message,
                    )
                    current_operator_peer = self.operator_bridge.active_peer
                    if (
                        not operator_peer_state_initialized
                        or current_operator_peer != last_operator_peer
                    ):
                        self.mission.audit.append(
                            "operator.metadata_peer_state",
                            time.monotonic(),
                            {
                                "connected": current_operator_peer is not None,
                                "peer": (
                                    current_operator_peer[0]
                                    if current_operator_peer is not None
                                    else None
                                ),
                                "port": (
                                    current_operator_peer[1]
                                    if current_operator_peer is not None
                                    else None
                                ),
                                "target_pool_page_count": len(target_pool_status_messages),
                                "transport_error_count": len(bridge_result.transport_errors),
                                "metadata_only": True,
                            },
                        )
                        operator_peer_state_initialized = True
                        last_operator_peer = current_operator_peer
                    remote_selections += bridge_result.accepted_command_count
                    remote_tracking_statuses += len(bridge_result.published_statuses)
                    remote_mission_statuses += len(bridge_result.published_mission_statuses)
                    remote_safety_statuses += len(bridge_result.published_safety_statuses)
                    remote_patrol_statuses += len(bridge_result.published_patrol_statuses)
                    remote_range_statuses += len(bridge_result.published_range_statuses)
                    remote_release_statuses += len(bridge_result.published_release_statuses)
                    remote_approach_challenges += len(bridge_result.published_approach_challenges)
                    remote_approach_statuses += len(bridge_result.published_approach_statuses)
                    remote_payload_target_challenges += len(
                        bridge_result.published_payload_target_challenges
                    )
                    remote_payload_target_statuses += len(
                        bridge_result.published_payload_target_statuses
                    )
                    remote_target_pool_statuses += len(bridge_result.published_target_pool_statuses)
                    remote_scene_context_statuses += len(
                        bridge_result.published_scene_context_statuses
                    )
                    remote_transport_errors += len(bridge_result.transport_errors)
                    if self.selection_target_pool is not None:
                        try:
                            selection_sync = self.selection_target_pool.consume_bridge_result(
                                bridge_result,
                                now_s=max(captured.captured_at_s, time.monotonic()),
                            )
                        except (RuntimeError, TypeError, ValueError) as exc:
                            selection_target_pool_errors += 1
                            self.mission.audit.append(
                                "tracking.selection_pool_bridge_failed",
                                max(captured.captured_at_s, time.monotonic()),
                                {
                                    "frame_id": captured.frame_id,
                                    "error_type": type(exc).__name__,
                                    "flight_control_enabled": False,
                                },
                            )
                        else:
                            selection_target_pool_syncs += 1
                            selection_target_pool_bindings += int(
                                selection_sync.bound_track_id is not None
                            )
                            selection_target_pool_pending += int(
                                selection_sync.pending_manual_observation
                            )
                            selection_target_pool_cancels += sum(
                                command.action
                                in {SelectionAction.CANCEL, SelectionAction.CANCEL_TRK}
                                for command, _peer in bridge_result.accepted_selection_commands
                            )
                            if (
                                selection_sync.bound_track_id is not None
                                or selection_sync.unlocked_track_id is not None
                                or bridge_result.accepted_selection_commands
                            ):
                                self.mission.audit.append(
                                    "tracking.selection_pool_operator_sync",
                                    max(captured.captured_at_s, time.monotonic()),
                                    {
                                        "frame_id": captured.frame_id,
                                        "active_selection_command_id": (
                                            selection_sync.active_selection_command_id
                                        ),
                                        "active_track_id": selection_sync.active_track_id,
                                        "tracked_track_ids": selection_sync.tracked_track_ids,
                                        "bound_track_id": selection_sync.bound_track_id,
                                        "unlocked_track_id": selection_sync.unlocked_track_id,
                                        "pending_manual_observation": (
                                            selection_sync.pending_manual_observation
                                        ),
                                        "background_locked_track_ids": (
                                            selection_sync.background_locked_track_ids
                                        ),
                                        "reason": selection_sync.reason,
                                        "metadata_only": True,
                                        "flight_control_enabled": False,
                                    },
                                )
                            if self.short_term_tracker is not None:
                                try:
                                    synchronized_tracks = self.unified_target_pool.snapshots()
                                    post_command_exclusive_track_id = (
                                        self.selection_target_pool.exclusive_lock_track_id
                                    )
                                    if post_command_exclusive_track_id is None:
                                        self.short_term_tracker.synchronize_tracks(
                                            synchronized_tracks
                                        )
                                    else:
                                        self.short_term_tracker.synchronize_tracks(
                                            synchronized_tracks,
                                            exclusive_track_id=post_command_exclusive_track_id,
                                        )
                                except (RuntimeError, TypeError, ValueError):
                                    short_term_tracking_errors += 1
                    for (
                        remote_command,
                        remote_peer,
                    ) in bridge_result.accepted_approach_confirmations:
                        accepted = False
                        if self.approach_hil_coordinator is not None:
                            try:
                                accepted = self.approach_hil_coordinator.consume_confirmation(
                                    remote_command,
                                    now_s=time.monotonic(),
                                )
                            except (RuntimeError, TypeError, ValueError) as exc:
                                approach_hil_errors += 1
                                self.mission.audit.append(
                                    "approach_hil.confirmation_failed",
                                    time.monotonic(),
                                    {
                                        "command_token": remote_command.command_token,
                                        "peer": remote_peer[0],
                                        "error_type": type(exc).__name__,
                                        "flight_control_enabled": False,
                                    },
                                )
                        remote_approach_confirmations += int(accepted)
                        self.mission.audit.append(
                            "approach_hil.confirmation_consumed",
                            time.monotonic(),
                            {
                                "command_token": remote_command.command_token,
                                "peer": remote_peer[0],
                                "accepted": accepted,
                                "continuous": remote_command.continuous,
                                "slide_duration_s": remote_command.slide_duration_s,
                                "completion_fraction": remote_command.completion_fraction,
                                "sitl_hil_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    for (
                        remote_command,
                        remote_peer,
                    ) in bridge_result.accepted_payload_target_confirmations:
                        accepted = False
                        if self.payload_target_coordinator is not None:
                            try:
                                accepted = self.payload_target_coordinator.consume_confirmation(
                                    remote_command,
                                    now_s=time.monotonic(),
                                )
                            except (RuntimeError, TypeError, ValueError) as exc:
                                payload_target_errors += 1
                                self.mission.audit.append(
                                    "payload_target.confirmation_failed",
                                    time.monotonic(),
                                    {
                                        "command_token": remote_command.command_token,
                                        "peer": remote_peer[0],
                                        "error_type": type(exc).__name__,
                                        "flight_control_enabled": False,
                                        "physical_release_enabled": False,
                                    },
                                )
                        remote_payload_target_confirmations += int(accepted)
                        self.mission.audit.append(
                            "payload_target.confirmation_consumed",
                            time.monotonic(),
                            {
                                "command_token": remote_command.command_token,
                                "peer": remote_peer[0],
                                "accepted": accepted,
                                "continuous": remote_command.continuous,
                                "slide_duration_s": remote_command.slide_duration_s,
                                "completion_fraction": remote_command.completion_fraction,
                                "hil_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    for (
                        remote_command,
                        remote_peer,
                    ) in bridge_result.accepted_authorization_decisions:
                        current_challenge = outcome.challenge
                        current_status = authorization_challenge_message
                        if (
                            remote_authorization_handled
                            or current_challenge is None
                            or current_status is None
                            or not _authorization_decision_matches_status(
                                remote_command,
                                current_status,
                            )
                        ):
                            self.mission.audit.append(
                                "operator.remote_authorization_rejected",
                                time.monotonic(),
                                {
                                    "command_token": remote_command.command_token,
                                    "operator_token": remote_command.operator_token,
                                    "peer": remote_peer[0],
                                    "reason": "current challenge binding changed",
                                    "hardware_control_enabled": False,
                                },
                            )
                            recent_events.append("REMOTE AUTHORIZATION REJECTED")
                            continue
                        operator_id = f"g20:{remote_command.operator_token:016x}"
                        try:
                            if remote_command.decision is AuthorizationDecision.APPROVE:
                                self.mission.approve_authorization(
                                    challenge_id=current_challenge.challenge_id,
                                    nonce=current_challenge.nonce,
                                    operator_id=operator_id,
                                    now_s=time.monotonic(),
                                )
                                authorizations += 1
                                recent_events.append(
                                    f"TARGET {current_challenge.target_id} REMOTELY AUTHORIZED"
                                )
                            else:
                                self.mission.deny_authorization(
                                    challenge_id=current_challenge.challenge_id,
                                    nonce=current_challenge.nonce,
                                    operator_id=operator_id,
                                    now_s=time.monotonic(),
                                )
                                recent_events.append(
                                    f"TARGET {current_challenge.target_id} REMOTELY DENIED"
                                )
                        except (RuntimeError, ValueError) as exc:
                            self.mission.audit.append(
                                "operator.remote_authorization_rejected",
                                time.monotonic(),
                                {
                                    "command_token": remote_command.command_token,
                                    "operator_token": remote_command.operator_token,
                                    "peer": remote_peer[0],
                                    "reason": type(exc).__name__,
                                    "hardware_control_enabled": False,
                                },
                            )
                            recent_events.append(
                                f"REMOTE AUTHORIZATION REJECTED {type(exc).__name__}"
                            )
                        else:
                            remote_authorization_handled = True
                            self.mission.audit.append(
                                "operator.remote_authorization_applied",
                                time.monotonic(),
                                {
                                    "command_token": remote_command.command_token,
                                    "operator_token": remote_command.operator_token,
                                    "decision": remote_command.decision.value,
                                    "peer": remote_peer[0],
                                    "payload_release_requested": False,
                                    "hardware_control_enabled": False,
                                },
                            )
                    for remote_status in bridge_result.published_statuses:
                        self.mission.audit.append(
                            "operator.remote_tracking_status",
                            time.monotonic(),
                            {
                                "selection_command_id": remote_status.selection_command_id,
                                "state": remote_status.state.value,
                                "target_id": remote_status.target_id,
                                "hardware_control_enabled": False,
                            },
                        )
                    for remote_status in bridge_result.published_mission_statuses:
                        self.mission.audit.append(
                            "operator.remote_mission_status",
                            time.monotonic(),
                            {
                                "phase": remote_status.phase.value,
                                "authorization_state": (remote_status.authorization_state.value),
                                "release_window": (
                                    remote_status.release_window.value
                                    if remote_status.release_window is not None
                                    else None
                                ),
                                "safety_allowed": remote_status.safety_allowed,
                                "advisory_only": True,
                                "hardware_control_enabled": False,
                            },
                        )
                    for remote_status in bridge_result.published_safety_statuses:
                        self.mission.audit.append(
                            "operator.remote_safety_status",
                            time.monotonic(),
                            {
                                "target_id": remote_status.target_id,
                                "ruleset_version": remote_status.ruleset_version,
                                "pass_count": remote_status.pass_count,
                                "deny_count": remote_status.deny_count,
                                "unknown_count": remote_status.unknown_count,
                                "advisory_only": True,
                                "hardware_control_enabled": False,
                            },
                        )
                    for remote_status in bridge_result.published_patrol_statuses:
                        self.mission.audit.append(
                            "operator.remote_patrol_status",
                            time.monotonic(),
                            {
                                "phase": remote_status.phase.value,
                                "primary_target_id": remote_status.primary_target_id,
                                "total_track_count": remote_status.total_track_count,
                                "locked_track_count": remote_status.locked_track_count,
                                "return_direction": (
                                    remote_status.return_direction.value
                                    if remote_status.return_direction is not None
                                    else None
                                ),
                                "return_validity": (
                                    remote_status.return_validity.value
                                    if remote_status.return_validity is not None
                                    else None
                                ),
                                "advisory_only": True,
                                "hardware_control_enabled": False,
                            },
                        )
                    for remote_status in bridge_result.published_range_statuses:
                        self.mission.audit.append(
                            "operator.remote_range_status",
                            time.monotonic(),
                            {
                                "target_id": remote_status.target_id,
                                "validity": remote_status.validity.value,
                                "slant_range_m": remote_status.slant_range_m,
                                "data_freshness_s": remote_status.data_freshness_s,
                                "sensor_consistency": remote_status.sensor_consistency,
                                "advisory_only": True,
                                "hardware_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    for remote_status in bridge_result.published_release_statuses:
                        self.mission.audit.append(
                            "operator.remote_release_status",
                            time.monotonic(),
                            {
                                "target_id": remote_status.target_id,
                                "timing_status": remote_status.timing_status.value,
                                "impact_north_offset_m": remote_status.impact_north_offset_m,
                                "impact_east_offset_m": remote_status.impact_east_offset_m,
                                "error_ellipse_major_m": remote_status.error_ellipse_major_m,
                                "error_ellipse_minor_m": remote_status.error_ellipse_minor_m,
                                "range_bound": remote_status.range_target_id is not None,
                                "advisory_only": True,
                                "hardware_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    for remote_status in bridge_result.published_approach_statuses:
                        self.mission.audit.append(
                            "operator.remote_approach_status",
                            time.monotonic(),
                            {
                                "target_id": remote_status.target_id,
                                "target_revision": remote_status.target_revision,
                                "phase": remote_status.phase.value,
                                "reasons": remote_status.reasons,
                                "yaw_advice_deg": remote_status.yaw_advice_deg,
                                "pitch_advice_deg": remote_status.pitch_advice_deg,
                                "climb_pitch_advice_deg": remote_status.climb_pitch_advice_deg,
                                "advisory_only": True,
                                "sitl_hil_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    if bridge_result.published_target_pool_statuses:
                        first_page = bridge_result.published_target_pool_statuses[0]
                        published_target_entries = tuple(
                            entry
                            for page in bridge_result.published_target_pool_statuses
                            for entry in page.entries
                        )
                        self.mission.audit.append(
                            "operator.remote_target_pool_status",
                            time.monotonic(),
                            {
                                "pool_revision": first_page.pool_revision,
                                "page_count": first_page.page_count,
                                "total_track_count": first_page.total_track_count,
                                "published_page_count": len(
                                    bridge_result.published_target_pool_statuses
                                ),
                                "published_entry_count": len(published_target_entries),
                                "range_track_ids": tuple(
                                    entry.target_id
                                    for entry in published_target_entries
                                    if entry.estimated_range_m is not None
                                ),
                                "speed_track_ids": tuple(
                                    entry.target_id
                                    for entry in published_target_entries
                                    if entry.target_speed_mps is not None
                                ),
                                "advisory_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    if bridge_result.published_scene_context_statuses:
                        first_page = bridge_result.published_scene_context_statuses[0]
                        self.mission.audit.append(
                            "operator.remote_scene_context_status",
                            time.monotonic(),
                            {
                                "context_revision": first_page.context_revision,
                                "source_frame_id": first_page.source_frame_id,
                                "state": first_page.state.value,
                                "page_count": first_page.page_count,
                                "total_region_count": first_page.total_region_count,
                                "published_page_count": len(
                                    bridge_result.published_scene_context_statuses
                                ),
                                "confidence_available": False,
                                "target_identity_authority": False,
                                "advisory_only": True,
                                "flight_control_enabled": False,
                                "physical_release_enabled": False,
                            },
                        )
                    if bridge_result.accepted_command_count:
                        recent_events.append("REMOTE TARGET SELECTION ACCEPTED")
                    for error_type in bridge_result.transport_errors:
                        self.mission.audit.append(
                            "operator.remote_transport_error",
                            time.monotonic(),
                            {
                                "error_type": error_type,
                                "hardware_control_enabled": False,
                            },
                        )
                        recent_events.append(f"REMOTE LINK ERROR {error_type}")
                if ui is not None:
                    consume_target_command = getattr(ui, "consume_target_command", None)
                    local_operator_now_s = time.monotonic()
                    local_command = (
                        consume_target_command(captured, now_s=local_operator_now_s)
                        if callable(consume_target_command)
                        else None
                    )
                    if local_command is not None:
                        if (
                            local_target_lock is None
                            or local_target_lock.geometry != local_command.geometry
                        ):
                            local_target_lock = OperatorTargetLock(
                                local_command.geometry,
                                TargetLockConfig(
                                    frozenset(self.mission.config.target_classes)
                                    | FIRE_CANDIDATE_TRACK_LABELS,
                                ),
                            )
                            local_manual_tracker = None
                            local_manual_tracker_unavailable = False
                            local_active_selection_command = None
                        detector_lock_status = local_target_lock.apply_command(
                            local_command,
                            tracks=outcome.tracks,
                            frame_id=captured.frame_id,
                            now_s=local_operator_now_s,
                        )
                        if local_command.action is SelectionAction.CANCEL:
                            local_active_selection_command = None
                            local_track_status = detector_lock_status
                            if local_manual_tracker is not None:
                                local_manual_tracker.apply_command(
                                    local_command,
                                    image_bgr=captured.image_bgr,
                                    frame_id=captured.frame_id,
                                    now_s=local_operator_now_s,
                                )
                            local_manual_tracker = None
                            local_manual_tracker_unavailable = False
                        else:
                            local_active_selection_command = local_command
                            local_manual_tracker_unavailable = False
                            manual_command = local_command
                            if (
                                detector_lock_status.state is TrackingState.TRACKING
                                and detector_lock_status.bbox is not None
                            ):
                                manual_command = replace(
                                    local_command,
                                    bbox=detector_lock_status.bbox,
                                )
                            try:
                                local_manual_tracker = OpenCVManualTargetTracker(
                                    manual_command.geometry
                                )
                                manual_status = local_manual_tracker.apply_command(
                                    manual_command,
                                    image_bgr=captured.image_bgr,
                                    frame_id=captured.frame_id,
                                    now_s=local_operator_now_s,
                                )
                            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                local_manual_tracker = None
                                local_manual_tracker_unavailable = True
                                self.mission.audit.append(
                                    "operator.local_manual_tracker_failed",
                                    time.monotonic(),
                                    {"error_type": type(exc).__name__},
                                )
                                recent_events.append(f"LOCAL TRACKER ERROR {type(exc).__name__}")
                                manual_status = detector_lock_status
                            if local_manual_tracker is not None and not local_manual_tracker.active:
                                local_manual_tracker = None
                                local_manual_tracker_unavailable = True
                            local_track_status = (
                                detector_lock_status
                                if detector_lock_status.state is TrackingState.TRACKING
                                else manual_status
                            )
                        local_selections += 1
                        local_tracking_statuses += 1
                        self.mission.audit.append(
                            "operator.local_target_selection",
                            time.monotonic(),
                            {
                                "action": local_command.action.value,
                                "state": local_track_status.state.value,
                                "target_id": local_track_status.target_id,
                                "hardware_control_enabled": False,
                            },
                        )
                    elif local_target_lock is not None:
                        detector_lock_status = local_target_lock.update(
                            tracks=outcome.tracks,
                            frame_id=captured.frame_id,
                            captured_at_s=captured.captured_at_s,
                            produced_at_s=max(captured.captured_at_s, time.monotonic()),
                        )
                        manual_status = None
                        if local_manual_tracker is not None and local_manual_tracker.active:
                            try:
                                manual_status = local_manual_tracker.update(
                                    image_bgr=captured.image_bgr,
                                    frame_id=captured.frame_id,
                                    captured_at_s=captured.captured_at_s,
                                    produced_at_s=max(
                                        captured.captured_at_s,
                                        time.monotonic(),
                                    ),
                                )
                            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                local_manual_tracker = None
                                local_manual_tracker_unavailable = True
                                self.mission.audit.append(
                                    "operator.local_manual_tracker_failed",
                                    time.monotonic(),
                                    {"error_type": type(exc).__name__},
                                )
                                recent_events.append(f"LOCAL TRACKER ERROR {type(exc).__name__}")
                        if (
                            detector_lock_status is not None
                            and detector_lock_status.state is TrackingState.TRACKING
                        ):
                            local_track_status = detector_lock_status
                            if (
                                (local_manual_tracker is None or not local_manual_tracker.active)
                                and not local_manual_tracker_unavailable
                                and local_active_selection_command is not None
                                and detector_lock_status.bbox is not None
                            ):
                                try:
                                    shadow_command = replace(
                                        local_active_selection_command,
                                        bbox=detector_lock_status.bbox,
                                    )
                                    local_manual_tracker = OpenCVManualTargetTracker(
                                        shadow_command.geometry
                                    )
                                    shadow_status = local_manual_tracker.apply_command(
                                        shadow_command,
                                        image_bgr=captured.image_bgr,
                                        frame_id=captured.frame_id,
                                        now_s=time.monotonic(),
                                    )
                                    if shadow_status.state is not TrackingState.TRACKING:
                                        local_manual_tracker = None
                                        local_manual_tracker_unavailable = True
                                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                                    local_manual_tracker = None
                                    local_manual_tracker_unavailable = True
                                    self.mission.audit.append(
                                        "operator.local_manual_tracker_failed",
                                        time.monotonic(),
                                        {"error_type": type(exc).__name__},
                                    )
                                    recent_events.append(
                                        f"LOCAL TRACKER ERROR {type(exc).__name__}"
                                    )
                        elif manual_status is not None:
                            local_track_status = manual_status
                            if (
                                local_track_status is not None
                                and local_track_status.bbox is not None
                                and local_track_status.state
                                in {TrackingState.TRACKING, TrackingState.INITIALIZING}
                            ):
                                local_target_lock.hint_bbox(
                                    local_track_status.bbox,
                                    now_s=max(captured.captured_at_s, time.monotonic()),
                                )
                        else:
                            local_track_status = detector_lock_status
                        if local_track_status is not None:
                            local_tracking_statuses += 1
                processed += 1
                processed_at_s = time.monotonic()
                if first_processed_at_s is None:
                    first_processed_at_s = processed_at_s
                last_processed_at_s = processed_at_s
                status = self.mission.status()
                elapsed_s = max(time.monotonic() - run_started_s, 1e-9)
                fps = processed / elapsed_s
                visible_alerts = (
                    (latest_alert,)
                    if latest_alert is not None
                    and time.monotonic() - latest_alert.observed_at_s
                    <= self.config.alert_banner_seconds
                    else ()
                )
                action = (
                    ui.render(
                        captured,
                        detections=detections,
                        tracks=outcome.tracks,
                        alerts=visible_alerts,
                        alert_delivery_status=latest_alert_delivery_status,
                        phase=status.phase,
                        deployment_capable=self.mission.config.deployment_capable,
                        remaining_payload_count=status.remaining_payload_count,
                        payload_inventory_verified=status.payload_inventory_verified,
                        payload_inventory_source=status.payload_inventory_source,
                        telemetry=telemetry,
                        fps=fps,
                        inference_latency_p95_ms=_percentile(inference_latency_ms, 0.95),
                        camera_reconnect_count=int(
                            getattr(self.frame_source, "reconnect_count", 0)
                        ),
                        recent_events=tuple(recent_events),
                        pending_authorization=outcome.challenge is not None,
                        deployment_ready=status.phase is MissionPhase.DEPLOYMENT_READY,
                        simulation_cycle_enabled=self.config.simulate_payload_cycle,
                        deployment_window=next(
                            (
                                decision.deployment_window
                                for decision in outcome.decisions
                                if decision.deployment_window is not None
                            ),
                            None,
                        ),
                        local_track_status=local_track_status,
                        monocular_avoidance=latest_monocular_avoidance,
                        ranging_solution=latest_ranging_solution,
                    )
                    if ui is not None
                    else None
                )
                if action == "quit":
                    break
                if action == "ack_alert" and latest_alert is not None:
                    self.mission.audit.append(
                        "alert.operator_acknowledged",
                        time.monotonic(),
                        {"alert_id": latest_alert.alert_id},
                    )
                    recent_events.append(f"ALERT {latest_alert.target_id} ACKNOWLEDGED")
                    latest_alert = None
                    latest_alert_delivery_status = None
                if (
                    action == "approve"
                    and outcome.challenge is not None
                    and not remote_authorization_handled
                ):
                    self.mission.approve_authorization(
                        challenge_id=outcome.challenge.challenge_id,
                        nonce=outcome.challenge.nonce,
                        operator_id=self.config.operator_id,
                        now_s=time.monotonic(),
                    )
                    authorizations += 1
                    recent_events.append(f"TARGET {outcome.challenge.target_id} AUTHORIZED")
                elif (
                    action == "deny"
                    and outcome.challenge is not None
                    and not remote_authorization_handled
                ):
                    self.mission.deny_authorization(
                        challenge_id=outcome.challenge.challenge_id,
                        nonce=outcome.challenge.nonce,
                        operator_id=self.config.operator_id,
                        now_s=time.monotonic(),
                    )
                    recent_events.append(f"TARGET {outcome.challenge.target_id} DENIED")
                elif (
                    self.config.simulate_payload_cycle
                    and (
                        action == "simulate_payload"
                        or (
                            self.config.auto_simulate_payload_cycle
                            and self.mission.status().phase is MissionPhase.DEPLOYMENT_READY
                        )
                    )
                    and self.mission.status().phase is MissionPhase.DEPLOYMENT_READY
                ):
                    automatically_triggered = action != "simulate_payload"
                    self.mission.audit.append(
                        (
                            "hil.auto_simulated_payload_cycle_requested"
                            if automatically_triggered
                            else "operator.simulated_payload_cycle_requested"
                        ),
                        time.monotonic(),
                        {
                            "operator_id": (
                                None if automatically_triggered else self.config.operator_id
                            ),
                            "trigger": (
                                "authorization_ready" if automatically_triggered else "operator_key"
                            ),
                            "authenticated_controller_hil": self.payload_hil_cycle is not None,
                            "simulation_only": True,
                            "physical_release_enabled": False,
                        },
                    )
                    if self.payload_hil_cycle is not None:
                        self.payload_hil_cycle.execute(now_s=time.monotonic())
                    else:
                        release_id = self.mission.request_simulated_deployment(
                            now_s=time.monotonic()
                        )
                        self.mission.report_simulated_execution(
                            release_id=release_id,
                            now_s=time.monotonic(),
                        )
                        self.mission.report_independent_confirmation(
                            release_id=release_id,
                            source_id="live-hil-independent-bay-sensor",
                            now_s=time.monotonic(),
                        )
                    simulated_payload_cycles += 1
                    recent_events.append("SIMULATED PAYLOAD CYCLE CONFIRMED")
                self.mission.tick(now_s=time.monotonic())
        finally:
            self.frame_source.close()
            if self.semantic_context_runner is not None:
                semantic_context_shutdown_clean = self.semantic_context_runner.close()
            if self.metric_depth_runner is not None:
                self.metric_depth_runner.close()
            if self.depth_grid_publisher is not None:
                self.depth_grid_publisher.close()
            if self.operator_bridge is not None:
                self.operator_bridge.close()
            if self.payload_hil_cycle is not None:
                self.payload_hil_cycle.close()
            if ui is not None:
                ui.close()
            close = getattr(self.telemetry_provider, "close", None)
            if callable(close):
                close()
        semantic_context_statistics = (
            self.semantic_context_runner.statistics()
            if self.semantic_context_runner is not None
            else None
        )
        result = LiveRunResult(
            processed_frames=processed,
            final_phase=self.mission.status().phase,
            authorization_count=authorizations,
            alert_delivery_count=alert_deliveries,
            alert_delivery_failure_count=alert_delivery_failures,
            average_fps=processed / max(time.monotonic() - run_started_s, 1e-9),
            steady_source_fps=_interval_rate(
                processed,
                first_captured_at_s,
                last_captured_at_s,
            ),
            steady_processing_fps=_interval_rate(
                processed,
                first_processed_at_s,
                last_processed_at_s,
            ),
            startup_to_first_frame_seconds=(
                max(0.0, first_frame_received_at_s - run_started_s)
                if first_frame_received_at_s is not None
                else 0.0
            ),
            capture_latency_p50_ms=_percentile(capture_latency_ms, 0.50),
            capture_latency_p95_ms=_percentile(capture_latency_ms, 0.95),
            frame_age_at_inference_p50_ms=_percentile(frame_age_at_inference_ms, 0.50),
            frame_age_at_inference_p95_ms=_percentile(frame_age_at_inference_ms, 0.95),
            inference_latency_p50_ms=_percentile(inference_latency_ms, 0.50),
            inference_latency_p95_ms=_percentile(inference_latency_ms, 0.95),
            rgb_fire_verifier_assessment_count=rgb_fire_verifier_assessments,
            rgb_fire_verifier_skipped_no_candidate_frame_count=(
                rgb_fire_verifier_skipped_no_candidate_frames
            ),
            rgb_fire_verifier_inference_count=rgb_fire_verifier_inferences,
            rgb_fire_verifier_failure_count=rgb_fire_verifier_failures,
            rgb_fire_verifier_unavailable_frame_count=rgb_fire_verifier_unavailable_frames,
            rgb_fire_verifier_unqualified_frame_count=rgb_fire_verifier_unqualified_frames,
            rgb_fire_verifier_corroborated_frame_count=(rgb_fire_verifier_corroborated_frames),
            rgb_fire_verifier_corroborated_detection_count=(
                rgb_fire_verifier_corroborated_detections
            ),
            rgb_fire_verifier_latency_p50_ms=_percentile(
                rgb_fire_verifier_latency_ms,
                0.50,
            ),
            rgb_fire_verifier_latency_p95_ms=_percentile(
                rgb_fire_verifier_latency_ms,
                0.95,
            ),
            camera_reconnect_count=int(getattr(self.frame_source, "reconnect_count", 0)),
            capture_queue_high_watermark=int(getattr(self.frame_source, "queue_high_watermark", 0)),
            capture_queue_backpressure_count=int(
                getattr(self.frame_source, "backpressure_count", 0)
            ),
            captured_frame_count=int(getattr(self.frame_source, "captured_frame_count", processed)),
            retried_alert_count=retried_alert_count,
            simulated_payload_cycle_count=simulated_payload_cycles,
            local_selection_count=local_selections,
            local_tracking_status_count=local_tracking_statuses,
            remote_selection_count=remote_selections,
            remote_tracking_status_count=remote_tracking_statuses,
            remote_mission_status_count=remote_mission_statuses,
            remote_safety_status_count=remote_safety_statuses,
            remote_patrol_status_count=remote_patrol_statuses,
            remote_range_status_count=remote_range_statuses,
            remote_release_status_count=remote_release_statuses,
            remote_approach_challenge_count=remote_approach_challenges,
            remote_approach_status_count=remote_approach_statuses,
            remote_target_pool_status_count=remote_target_pool_statuses,
            remote_scene_context_status_count=remote_scene_context_statuses,
            remote_approach_confirmation_count=remote_approach_confirmations,
            remote_payload_target_challenge_count=remote_payload_target_challenges,
            remote_payload_target_status_count=remote_payload_target_statuses,
            remote_payload_target_confirmation_count=remote_payload_target_confirmations,
            payload_target_error_count=payload_target_errors,
            approach_hil_abort_count=approach_hil_aborts,
            approach_hil_error_count=approach_hil_errors,
            remote_transport_error_count=remote_transport_errors,
            monocular_avoidance_assessment_count=monocular_avoidance_assessments,
            monocular_avoidance_invalid_count=monocular_avoidance_invalid,
            monocular_avoidance_caution_count=monocular_avoidance_caution,
            monocular_avoidance_avoid_count=monocular_avoidance_avoid,
            monocular_avoidance_error_count=monocular_avoidance_errors,
            monocular_avoidance_latency_p50_ms=_percentile(monocular_avoidance_latency_ms, 0.50),
            monocular_avoidance_latency_p95_ms=_percentile(monocular_avoidance_latency_ms, 0.95),
            unified_target_pool_update_count=unified_target_pool_updates,
            unified_target_pool_error_count=unified_target_pool_errors,
            unified_target_pool_maximum_track_count=unified_target_pool_maximum_tracks,
            unified_target_pool_created_track_count=unified_target_pool_created_tracks,
            unified_target_pool_recovered_track_count=unified_target_pool_recovered_tracks,
            unified_target_pool_lost_track_count=unified_target_pool_lost_tracks,
            unified_target_pool_association_p50_ms=_percentile(
                unified_target_pool_association_ms,
                0.50,
            ),
            unified_target_pool_association_p95_ms=_percentile(
                unified_target_pool_association_ms,
                0.95,
            ),
            identity_tracking_log_frame_count=identity_tracking_log_frames,
            identity_tracking_log_error_count=identity_tracking_log_errors,
            identity_tracking_log_disabled_after_error=(identity_tracking_log_disabled_after_error),
            person_reid_failure_count=person_reid_failures,
            person_reid_inference_count=person_reid_inferences,
            person_reid_skipped_frame_count=person_reid_skipped_frames,
            person_reid_no_candidate_frame_count=person_reid_no_candidate_frames,
            person_reid_forced_recovery_count=person_reid_forced_recoveries,
            person_reid_latency_p50_ms=_percentile(person_reid_latency_ms, 0.50),
            person_reid_latency_p95_ms=_percentile(person_reid_latency_ms, 0.95),
            vehicle_reid_failure_count=vehicle_reid_failures,
            vehicle_reid_inference_count=vehicle_reid_inferences,
            vehicle_reid_skipped_frame_count=vehicle_reid_skipped_frames,
            vehicle_reid_no_candidate_frame_count=vehicle_reid_no_candidate_frames,
            vehicle_reid_forced_recovery_count=vehicle_reid_forced_recoveries,
            vehicle_reid_latency_p50_ms=_percentile(vehicle_reid_latency_ms, 0.50),
            vehicle_reid_latency_p95_ms=_percentile(vehicle_reid_latency_ms, 0.95),
            patrol_advisory_assessment_count=patrol_advisory_assessments,
            patrol_return_to_observe_count=patrol_return_to_observe,
            patrol_advisory_error_count=patrol_advisory_errors,
            short_term_tracking_update_count=short_term_tracking_updates,
            short_term_tracking_invalid_count=short_term_tracking_invalid,
            short_term_tracking_error_count=short_term_tracking_errors,
            short_term_tracking_optical_flow_hint_count=(short_term_tracking_optical_flow_hints),
            short_term_tracking_template_hint_count=short_term_tracking_template_hints,
            short_term_tracking_accepted_hint_count=short_term_tracking_accepted_hints,
            short_term_tracking_rejected_hint_count=short_term_tracking_rejected_hints,
            short_term_tracking_camera_motion_count=(short_term_tracking_camera_motion_estimates),
            short_term_tracking_latency_p50_ms=_percentile(
                short_term_tracking_latency_ms,
                0.50,
            ),
            short_term_tracking_latency_p95_ms=_percentile(
                short_term_tracking_latency_ms,
                0.95,
            ),
            selection_target_pool_sync_count=selection_target_pool_syncs,
            selection_target_pool_binding_count=selection_target_pool_bindings,
            selection_target_pool_pending_count=selection_target_pool_pending,
            selection_target_pool_cancel_count=selection_target_pool_cancels,
            selection_target_pool_error_count=selection_target_pool_errors,
            ranging_assessment_count=ranging_assessments,
            ranging_valid_count=ranging_valid,
            ranging_degraded_count=ranging_degraded,
            ranging_invalid_count=ranging_invalid,
            ranging_error_count=ranging_errors,
            ranging_latency_p50_ms=_percentile(ranging_latency_ms, 0.50),
            ranging_latency_p95_ms=_percentile(ranging_latency_ms, 0.95),
            semantic_context_submitted_frame_count=(
                semantic_context_statistics.submitted_frame_count
                if semantic_context_statistics is not None
                else 0
            ),
            semantic_context_interval_skipped_frame_count=(
                semantic_context_statistics.interval_skipped_frame_count
                if semantic_context_statistics is not None
                else 0
            ),
            semantic_context_replaced_pending_frame_count=(
                semantic_context_statistics.replaced_pending_frame_count
                if semantic_context_statistics is not None
                else 0
            ),
            semantic_context_valid_frame_count=(
                semantic_context_statistics.completed_frame_count
                - semantic_context_statistics.failed_frame_count
                if semantic_context_statistics is not None
                else 0
            ),
            semantic_context_invalid_frame_count=(
                semantic_context_statistics.failed_frame_count
                if semantic_context_statistics is not None
                else 0
            ),
            semantic_context_submit_error_count=semantic_context_submit_errors,
            semantic_context_stale_count=semantic_context_stale,
            semantic_context_latency_p50_ms=_percentile(semantic_context_latency_ms, 0.50),
            semantic_context_latency_p95_ms=_percentile(semantic_context_latency_ms, 0.95),
            semantic_context_shutdown_clean=semantic_context_shutdown_clean,
        )
        self.mission.audit.append(
            "live.performance_summary",
            time.monotonic(),
            {
                "processed_frames": result.processed_frames,
                "average_fps": result.average_fps,
                "steady_source_fps": result.steady_source_fps,
                "steady_processing_fps": result.steady_processing_fps,
                "startup_to_first_frame_seconds": result.startup_to_first_frame_seconds,
                "capture_latency_p50_ms": result.capture_latency_p50_ms,
                "capture_latency_p95_ms": result.capture_latency_p95_ms,
                "frame_age_at_inference_p50_ms": result.frame_age_at_inference_p50_ms,
                "frame_age_at_inference_p95_ms": result.frame_age_at_inference_p95_ms,
                "inference_latency_p50_ms": result.inference_latency_p50_ms,
                "inference_latency_p95_ms": result.inference_latency_p95_ms,
                "rgb_fire_verifier_assessment_count": (result.rgb_fire_verifier_assessment_count),
                "rgb_fire_verifier_skipped_no_candidate_frame_count": (
                    result.rgb_fire_verifier_skipped_no_candidate_frame_count
                ),
                "rgb_fire_verifier_inference_count": (result.rgb_fire_verifier_inference_count),
                "rgb_fire_verifier_failure_count": result.rgb_fire_verifier_failure_count,
                "rgb_fire_verifier_unavailable_frame_count": (
                    result.rgb_fire_verifier_unavailable_frame_count
                ),
                "rgb_fire_verifier_unqualified_frame_count": (
                    result.rgb_fire_verifier_unqualified_frame_count
                ),
                "rgb_fire_verifier_corroborated_frame_count": (
                    result.rgb_fire_verifier_corroborated_frame_count
                ),
                "rgb_fire_verifier_corroborated_detection_count": (
                    result.rgb_fire_verifier_corroborated_detection_count
                ),
                "rgb_fire_verifier_latency_p50_ms": (result.rgb_fire_verifier_latency_p50_ms),
                "rgb_fire_verifier_latency_p95_ms": (result.rgb_fire_verifier_latency_p95_ms),
                "rgb_fire_verifier_flight_control_enabled": False,
                "rgb_fire_verifier_physical_release_enabled": False,
                "camera_reconnect_count": result.camera_reconnect_count,
                "capture_queue_high_watermark": result.capture_queue_high_watermark,
                "capture_queue_backpressure_count": result.capture_queue_backpressure_count,
                "captured_frame_count": result.captured_frame_count,
                "retried_alert_count": result.retried_alert_count,
                "simulated_payload_cycle_count": result.simulated_payload_cycle_count,
                "local_selection_count": result.local_selection_count,
                "local_tracking_status_count": result.local_tracking_status_count,
                "remote_selection_count": result.remote_selection_count,
                "remote_tracking_status_count": result.remote_tracking_status_count,
                "remote_mission_status_count": result.remote_mission_status_count,
                "remote_safety_status_count": result.remote_safety_status_count,
                "remote_patrol_status_count": result.remote_patrol_status_count,
                "remote_range_status_count": result.remote_range_status_count,
                "remote_release_status_count": result.remote_release_status_count,
                "remote_approach_challenge_count": result.remote_approach_challenge_count,
                "remote_approach_status_count": result.remote_approach_status_count,
                "remote_target_pool_status_count": result.remote_target_pool_status_count,
                "remote_scene_context_status_count": result.remote_scene_context_status_count,
                "remote_approach_confirmation_count": (result.remote_approach_confirmation_count),
                "approach_hil_abort_count": result.approach_hil_abort_count,
                "approach_hil_error_count": result.approach_hil_error_count,
                "approach_hil_advisory_only": True,
                "approach_hil_flight_control_enabled": False,
                "approach_hil_physical_release_enabled": False,
                "remote_transport_error_count": result.remote_transport_error_count,
                "monocular_avoidance_assessment_count": (
                    result.monocular_avoidance_assessment_count
                ),
                "monocular_avoidance_invalid_count": result.monocular_avoidance_invalid_count,
                "monocular_avoidance_caution_count": result.monocular_avoidance_caution_count,
                "monocular_avoidance_avoid_count": result.monocular_avoidance_avoid_count,
                "monocular_avoidance_error_count": result.monocular_avoidance_error_count,
                "monocular_avoidance_latency_p50_ms": (result.monocular_avoidance_latency_p50_ms),
                "monocular_avoidance_latency_p95_ms": (result.monocular_avoidance_latency_p95_ms),
                "monocular_avoidance_advisory_only": True,
                "monocular_avoidance_flight_control_enabled": False,
                "unified_target_pool_update_count": result.unified_target_pool_update_count,
                "unified_target_pool_error_count": result.unified_target_pool_error_count,
                "unified_target_pool_maximum_track_count": (
                    result.unified_target_pool_maximum_track_count
                ),
                "unified_target_pool_created_track_count": (
                    result.unified_target_pool_created_track_count
                ),
                "unified_target_pool_recovered_track_count": (
                    result.unified_target_pool_recovered_track_count
                ),
                "unified_target_pool_lost_track_count": (
                    result.unified_target_pool_lost_track_count
                ),
                "unified_target_pool_association_p50_ms": (
                    result.unified_target_pool_association_p50_ms
                ),
                "unified_target_pool_association_p95_ms": (
                    result.unified_target_pool_association_p95_ms
                ),
                "identity_tracking_log_frame_count": result.identity_tracking_log_frame_count,
                "identity_tracking_log_error_count": result.identity_tracking_log_error_count,
                "identity_tracking_log_disabled_after_error": (
                    result.identity_tracking_log_disabled_after_error
                ),
                "identity_tracking_log_contains_pixels": False,
                "identity_tracking_log_flight_control_enabled": False,
                "person_reid_failure_count": result.person_reid_failure_count,
                "vehicle_reid_failure_count": result.vehicle_reid_failure_count,
                "patrol_advisory_assessment_count": (result.patrol_advisory_assessment_count),
                "patrol_return_to_observe_count": result.patrol_return_to_observe_count,
                "patrol_advisory_error_count": result.patrol_advisory_error_count,
                "patrol_advisory_flight_control_enabled": False,
                "unified_target_pool_metadata_only": True,
                "unified_target_pool_flight_control_enabled": False,
                "short_term_tracking_update_count": result.short_term_tracking_update_count,
                "short_term_tracking_invalid_count": result.short_term_tracking_invalid_count,
                "short_term_tracking_error_count": result.short_term_tracking_error_count,
                "short_term_tracking_optical_flow_hint_count": (
                    result.short_term_tracking_optical_flow_hint_count
                ),
                "short_term_tracking_template_hint_count": (
                    result.short_term_tracking_template_hint_count
                ),
                "short_term_tracking_accepted_hint_count": (
                    result.short_term_tracking_accepted_hint_count
                ),
                "short_term_tracking_rejected_hint_count": (
                    result.short_term_tracking_rejected_hint_count
                ),
                "short_term_tracking_camera_motion_count": (
                    result.short_term_tracking_camera_motion_count
                ),
                "short_term_tracking_latency_p50_ms": (result.short_term_tracking_latency_p50_ms),
                "short_term_tracking_latency_p95_ms": (result.short_term_tracking_latency_p95_ms),
                "short_term_tracking_metadata_only": True,
                "short_term_tracking_identity_authority": False,
                "short_term_tracking_flight_control_enabled": False,
                "selection_target_pool_sync_count": result.selection_target_pool_sync_count,
                "selection_target_pool_binding_count": (result.selection_target_pool_binding_count),
                "selection_target_pool_pending_count": (result.selection_target_pool_pending_count),
                "selection_target_pool_cancel_count": (result.selection_target_pool_cancel_count),
                "selection_target_pool_error_count": (result.selection_target_pool_error_count),
                "selection_target_pool_metadata_only": True,
                "selection_target_pool_flight_control_enabled": False,
                "ranging_assessment_count": result.ranging_assessment_count,
                "ranging_valid_count": result.ranging_valid_count,
                "ranging_degraded_count": result.ranging_degraded_count,
                "ranging_invalid_count": result.ranging_invalid_count,
                "ranging_error_count": result.ranging_error_count,
                "ranging_latency_p50_ms": result.ranging_latency_p50_ms,
                "ranging_latency_p95_ms": result.ranging_latency_p95_ms,
                "ranging_advisory_only": True,
                "ranging_flight_control_enabled": False,
                "ranging_physical_release_enabled": False,
                "semantic_context_submitted_frame_count": (
                    result.semantic_context_submitted_frame_count
                ),
                "semantic_context_interval_skipped_frame_count": (
                    result.semantic_context_interval_skipped_frame_count
                ),
                "semantic_context_replaced_pending_frame_count": (
                    result.semantic_context_replaced_pending_frame_count
                ),
                "semantic_context_valid_frame_count": result.semantic_context_valid_frame_count,
                "semantic_context_invalid_frame_count": (
                    result.semantic_context_invalid_frame_count
                ),
                "semantic_context_submit_error_count": result.semantic_context_submit_error_count,
                "semantic_context_stale_count": result.semantic_context_stale_count,
                "semantic_context_latency_p50_ms": result.semantic_context_latency_p50_ms,
                "semantic_context_latency_p95_ms": result.semantic_context_latency_p95_ms,
                "semantic_context_shutdown_clean": result.semantic_context_shutdown_clean,
                "semantic_context_queue_capacity": 1,
                "semantic_context_confidence_available": False,
                "semantic_context_advisory_only": True,
                "semantic_context_target_pool_identity_authority": False,
                "semantic_context_flight_control_enabled": False,
                "semantic_context_physical_release_enabled": False,
                "sample_window_frames": self.config.performance_window_frames,
            },
        )
        return result

    def _evaluate_track_ranging(
        self,
        *,
        captured: CapturedFrame,
        track: UnifiedTrackSnapshot,
        telemetry: VehicleTelemetry,
        now_s: float,
        camera_motion: CameraMotionEstimate | None = None,
        exclusive_lock: bool = False,
    ) -> RangeSolution:
        engine = self.ranging_engine
        config = self.ranging_config
        if engine is None or config is None:
            raise RuntimeError("ranging engine is not configured")
        predicted_track_usable = (
            track.state in {UnifiedTrackState.OCCLUDED, UnifiedTrackState.REACQUIRING}
            and track.missed_frame_count <= 3
            and track.tracking_quality >= 0.20
        )
        # An exclusive LCK keeps a dedicated tracker and dense-depth worker on
        # this object. Permit its bounded prediction window to bridge detector
        # stride, temporary blur and the EKF transition that follows ARM. The
        # async depth result itself still has its own one-second freshness gate.
        exclusive_metric_hold_usable = (
            exclusive_lock
            and track.state
            in {
                UnifiedTrackState.TRACKING,
                UnifiedTrackState.OCCLUDED,
                UnifiedTrackState.REACQUIRING,
            }
            and track.missed_frame_count <= 24
            and track.tracking_quality >= 0.10
        )
        if (
            not track.actionable
            and not predicted_track_usable
            and not exclusive_metric_hold_usable
        ):
            return _invalid_live_range_solution(
                target_id=track.track_id,
                frame_id=captured.frame_id,
                calibration_id=config.calibration.calibration_id,
                now_s=now_s,
                reason="target_not_freshly_observed",
            )
        # Older read-only telemetry providers exposed only a position timestamp.
        # Keep that compatibility path while new Pixhawk telemetry reports a
        # dedicated local/relative-altitude timestamp for GPS-denied operation.
        altitude_timestamp_s = telemetry.altitude_observed_at_s
        if not math.isfinite(altitude_timestamp_s):
            altitude_timestamp_s = telemetry.position_observed_at_s
        pose_values = (
            telemetry.roll_deg,
            telemetry.pitch_deg,
            telemetry.heading_deg,
            telemetry.attitude_observed_at_s,
        )
        if not all(math.isfinite(value) for value in pose_values):
            return _invalid_live_range_solution(
                target_id=track.track_id,
                frame_id=captured.frame_id,
                calibration_id=config.calibration.calibration_id,
                now_s=now_s,
                reason="pixhawk_pose_or_timestamp_unavailable",
            )
        center_x, center_y = track.bbox.center
        target = TargetImageObservation(
            target_id=track.track_id,
            frame_id=captured.frame_id,
            captured_at_s=captured.captured_at_s,
            center_x=center_x,
            center_y=center_y,
            center_sigma_px=config.target_center_sigma_px,
        )
        adaptive_decision = (
            self.adaptive_ranging_policy.decide(telemetry, now_s=now_s)
            if self.adaptive_ranging_policy is not None
            else None
        )
        source_weight_multipliers = (
            adaptive_decision.source_weight_priors if adaptive_decision is not None else None
        )
        fusion_metadata = (
            {
                "fusion_profile": "outdoor-multimodal-v1",
                "vehicle_profile": adaptive_decision.vehicle_profile.value,
                "navigation_state": adaptive_decision.navigation_state.value,
                "motion_regime": adaptive_decision.motion_regime.value,
            }
            if adaptive_decision is not None
            else {}
        )
        visual_direct = (
            self.visual_inertial_ranging.observe(
                track=track,
                telemetry=telemetry,
                calibration=config.calibration,
                frame_id=captured.frame_id,
                captured_at_s=captured.captured_at_s,
            )
            if self.visual_inertial_ranging is not None
            else None
        )
        rgb_slam_direct = (
            self.rgb_slam_ranging.observe(
                track=track,
                telemetry=telemetry,
                calibration=config.calibration,
                frame_id=captured.frame_id,
                captured_at_s=captured.captured_at_s,
                camera_motion=camera_motion,
            )
            if self.rgb_slam_ranging is not None
            else None
        )
        metric_direct = (
            self.metric_depth_runner.measurement_for(
                target_id=track.track_id,
                now_s=now_s,
            )
            if self.metric_depth_runner is not None
            else None
        )
        direct_measurements = tuple(
            measurement
            for measurement in (visual_direct, rgb_slam_direct, metric_direct)
            if measurement is not None
        )
        pose = AircraftPose(
            captured_at_s=telemetry.attitude_observed_at_s,
            roll_deg=telemetry.roll_deg,
            pitch_deg=telemetry.pitch_deg,
            heading_deg=telemetry.heading_deg % 360.0,
            roll_sigma_deg=config.roll_sigma_deg,
            pitch_sigma_deg=config.pitch_sigma_deg,
            heading_sigma_deg=config.heading_sigma_deg,
        )
        # Sub-decimetre local-NED noise while parked is a learned zero, not a
        # geometric camera height.  Keep it out of the ground-ray solver and let
        # the temporal motion estimator wait for real platform excitation.
        if (
            telemetry.armed is False
            or not math.isfinite(telemetry.altitude_agl_m)
            or telemetry.altitude_agl_m <= 0.15
        ):
            if direct_measurements:
                return engine.solve_direct(
                    calibration=config.calibration,
                    pose=pose,
                    target=target,
                    direct_measurements=direct_measurements,
                    source_weight_multipliers=source_weight_multipliers,
                    now_s=now_s,
                    **fusion_metadata,
                )
            return _invalid_live_range_solution(
                target_id=track.track_id,
                frame_id=captured.frame_id,
                calibration_id=config.calibration.calibration_id,
                now_s=now_s,
                reason="vertical_reference_unavailable",
            )
        if not math.isfinite(altitude_timestamp_s):
            return _invalid_live_range_solution(
                target_id=track.track_id,
                frame_id=captured.frame_id,
                calibration_id=config.calibration.calibration_id,
                now_s=now_s,
                reason="vertical_reference_unavailable",
            )
        if (
            abs(telemetry.attitude_observed_at_s - altitude_timestamp_s)
            > engine.config.maximum_pose_image_skew_s
        ):
            # A metric-depth observation is tied to the image itself and does
            # not require the vertical reference to share the attitude sample's
            # exact timestamp.  Keep the direct measurement available while the
            # ground-ray branch waits for synchronized aircraft telemetry.
            if direct_measurements:
                return engine.solve_direct(
                    calibration=config.calibration,
                    pose=pose,
                    target=target,
                    direct_measurements=direct_measurements,
                    source_weight_multipliers=source_weight_multipliers,
                    now_s=now_s,
                    **fusion_metadata,
                )
            return _invalid_live_range_solution(
                target_id=track.track_id,
                frame_id=captured.frame_id,
                calibration_id=config.calibration.calibration_id,
                now_s=now_s,
                reason="attitude_position_time_skew_exceeded",
            )
        pose_timestamp_s = min(
            telemetry.attitude_observed_at_s,
            altitude_timestamp_s,
        )
        # The RTSP decoder can deliver a frame 0.4-0.7 s behind the newest
        # Pixhawk sample. The metric model measures that captured image directly,
        # while camera-ground projection needs tightly synchronized pose/height.
        # Preserve the calibrated direct result during this bounded skew instead
        # of alternating the QGC field between a value and an invalid marker.
        if direct_measurements and abs(pose_timestamp_s - captured.captured_at_s) > (
            engine.config.maximum_pose_image_skew_s
        ):
            return engine.solve_direct(
                calibration=config.calibration,
                pose=pose,
                target=target,
                direct_measurements=direct_measurements,
                source_weight_multipliers=source_weight_multipliers,
                now_s=now_s,
                **fusion_metadata,
            )
        return engine.solve(
            calibration=config.calibration,
            pose=AircraftPose(
                captured_at_s=pose_timestamp_s,
                roll_deg=telemetry.roll_deg,
                pitch_deg=telemetry.pitch_deg,
                heading_deg=telemetry.heading_deg % 360.0,
                roll_sigma_deg=config.roll_sigma_deg,
                pitch_sigma_deg=config.pitch_sigma_deg,
                heading_sigma_deg=config.heading_sigma_deg,
            ),
            target=target,
            vertical_measurements=(
                VerticalMeasurement(
                    source=VerticalSource.PIXHAWK_AGL,
                    height_m=telemetry.altitude_agl_m,
                    sigma_m=config.altitude_agl_sigma_m,
                    captured_at_s=altitude_timestamp_s,
                ),
            ),
            direct_measurements=direct_measurements,
            source_weight_multipliers=source_weight_multipliers,
            now_s=now_s,
            **fusion_metadata,
        )

    def _estimate_target_speed_mps(
        self,
        *,
        track: UnifiedTrackSnapshot,
        solution: RangeSolution,
        telemetry: VehicleTelemetry,
        captured_at_s: float,
        calibration: CameraCalibration,
    ) -> float | None:
        """Estimate world speed, with a conservative image-motion startup gate.

        Once enough metric positions exist, Local-NED plus target offsets remove
        aircraft translation and the regression estimator classifies stationary
        jitter. During its first 0.6 s, only a near-zero image-motion result is
        published; larger ambiguous startup motion stays absent until measured.
        """

        predicted_track_usable = (
            track.state in {UnifiedTrackState.OCCLUDED, UnifiedTrackState.REACQUIRING}
            and track.missed_frame_count <= 3
            and track.tracking_quality >= 0.20
        )
        if not track.actionable and not predicted_track_usable:
            return None
        if solution.slant_range_m is None:
            return None
        if self._target_world_speed is not None:
            world_speed = self._target_world_speed.update(
                target_id=track.track_id,
                solution=solution,
                telemetry=telemetry,
                captured_at_s=captured_at_s,
            )
            if world_speed is not None:
                return world_speed
        angular_x_s = track.velocity_x_s * calibration.width_px / calibration.fx_px
        angular_y_s = track.velocity_y_s * calibration.height_px / calibration.fy_px
        speed_mps = solution.slant_range_m * math.hypot(angular_x_s, angular_y_s)
        if not math.isfinite(speed_mps) or not 0.0 <= speed_mps <= 6_553.4:
            return None
        return 0.0 if speed_mps <= 1.0 else None

    def _advance_observed_pixhawk_lifecycle(
        self,
        *,
        telemetry: VehicleTelemetry,
        now_s: float,
    ) -> None:
        allowed_modes = {mode.strip().upper() for mode in self.config.allowed_auto_modes}
        current_mode = (telemetry.flight_mode or "").strip().upper()
        if self.mission.state.phase is MissionPhase.STANDBY:
            reasons = tuple(
                reason
                for condition, reason in (
                    (telemetry.link_healthy is True, "link is not healthy"),
                    (telemetry.position_healthy is True, "position is not healthy"),
                    (telemetry.armed is True, "vehicle is not armed"),
                    (current_mode in allowed_modes, "flight mode is not an allowed auto mode"),
                )
                if not condition
            )
            if reasons:
                self._audit_lifecycle_waiting(now_s, MissionPhase.STANDBY, reasons)
                return
            self.mission.launch(now_s=now_s)
            self._lifecycle_waiting_fingerprint = None
        if self.mission.state.phase is MissionPhase.NAVIGATING:
            required_sequence = self.config.task_area_mission_sequence
            if required_sequence is None:
                raise RuntimeError("task-area mission sequence is not configured")
            if telemetry.mission_sequence is None or telemetry.mission_sequence < required_sequence:
                self._audit_lifecycle_waiting(
                    now_s,
                    MissionPhase.NAVIGATING,
                    (
                        "task-area mission sequence has not been reached: "
                        f"current={telemetry.mission_sequence}, required={required_sequence}",
                    ),
                )
                return
            self.mission.arrive_task_area(now_s=now_s)
            self._lifecycle_waiting_fingerprint = None

    def _audit_lifecycle_waiting(
        self,
        now_s: float,
        phase: MissionPhase,
        reasons: tuple[str, ...],
    ) -> None:
        fingerprint: tuple[object, ...] = (phase, reasons)
        if fingerprint == self._lifecycle_waiting_fingerprint:
            return
        self._lifecycle_waiting_fingerprint = fingerprint
        self.mission.audit.append(
            "mission.pixhawk_lifecycle_waiting",
            now_s,
            {"phase": phase.value, "reasons": reasons},
        )


def _authorization_decision_matches_status(
    command: AuthorizationDecisionCommand,
    status: AuthorizationChallengeStatusMessage,
) -> bool:
    return (
        command.challenge_token,
        command.mission_token,
        command.target_token,
        command.ruleset_token,
        command.payload_slot_token,
    ) == (
        status.challenge_token,
        status.mission_token,
        status.target_token,
        status.ruleset_token,
        status.payload_slot_token,
    )


def _monocular_avoidance_details(
    assessment: MonocularAvoidanceAssessment,
) -> dict[str, object]:
    return {
        "frame_id": assessment.frame_id,
        "state": assessment.state.value,
        "reason": assessment.reason,
        "data_age_s": assessment.data_age_s,
        "frame_interval_s": assessment.frame_interval_s,
        "valid_feature_count": assessment.valid_feature_count,
        "rotation_compensated": assessment.rotation_compensated,
        "camera_motion_dx": assessment.camera_motion_dx,
        "camera_motion_dy": assessment.camera_motion_dy,
        "camera_motion_scale": assessment.camera_motion_scale,
        "camera_motion_rotation_deg": assessment.camera_motion_rotation_deg,
        "camera_motion_aspect_ratio": assessment.camera_motion_aspect_ratio,
        "camera_motion_affine": assessment.camera_motion_affine,
        "camera_motion_confidence": assessment.camera_motion_confidence,
        "processing_time_ms": assessment.processing_time_ms,
        "zones": [
            {
                "zone": zone.zone.value,
                "state": zone.state.value,
                "feature_count": zone.feature_count,
                "outward_feature_count": zone.outward_feature_count,
                "ttc_s": zone.ttc_s,
                "confidence": zone.confidence,
            }
            for zone in assessment.zones
        ],
        "advisory_only": assessment.advisory_only,
        "flight_control_enabled": False,
        "metric_depth_available": False,
    }


def _invalid_live_range_solution(
    *,
    target_id: str,
    frame_id: str,
    calibration_id: str,
    now_s: float,
    reason: str,
) -> RangeSolution:
    return RangeSolution(
        target_id=target_id,
        frame_id=frame_id,
        calibration_id=calibration_id,
        evaluated_at_s=now_s,
        validity=RangeValidity.INVALID,
        reasons=(reason,),
        sources=(),
        rejected_sources=(),
    )


def _range_solution_details(
    solution: RangeSolution,
    *,
    telemetry: VehicleTelemetry | None = None,
    now_s: float | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "target_id": solution.target_id,
        "frame_id": solution.frame_id,
        "calibration_id": solution.calibration_id,
        "validity": solution.validity.value,
        "reasons": solution.reasons,
        "sources": solution.sources,
        "rejected_sources": solution.rejected_sources,
        "slant_range_m": solution.slant_range_m,
        "ground_range_m": solution.ground_range_m,
        "slant_range_ci95_m": solution.slant_range_ci95_m,
        "ground_range_ci95_m": solution.ground_range_ci95_m,
        "relative_bearing_deg": solution.relative_bearing_deg,
        "absolute_bearing_deg": solution.absolute_bearing_deg,
        "bearing_sigma_deg": solution.bearing_sigma_deg,
        "north_offset_m": solution.north_offset_m,
        "east_offset_m": solution.east_offset_m,
        "data_freshness_s": solution.data_freshness_s,
        "sensor_consistency": solution.sensor_consistency,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
    }
    if telemetry is not None:
        details["ranging_input"] = _ranging_input_details(telemetry, now_s=now_s)
    return details


def _ranging_input_details(
    telemetry: VehicleTelemetry,
    *,
    now_s: float | None,
) -> dict[str, object]:
    """Compact evidence for a ``--`` range, without inventing metric output."""

    def _sample(value: float, observed_at_s: float) -> dict[str, object]:
        finite_value = math.isfinite(value)
        finite_timestamp = math.isfinite(observed_at_s)
        return {
            "sample_available": finite_value and finite_timestamp,
            "age_s": (
                max(0.0, now_s - observed_at_s) if now_s is not None and finite_timestamp else None
            ),
        }

    return {
        "attitude": {
            "roll_pitch_heading_available": all(
                math.isfinite(value)
                for value in (telemetry.roll_deg, telemetry.pitch_deg, telemetry.heading_deg)
            ),
            **_sample(telemetry.roll_deg, telemetry.attitude_observed_at_s),
        },
        "vertical": {
            "altitude_agl_available": math.isfinite(telemetry.altitude_agl_m)
            and telemetry.altitude_agl_m > 0.0,
            "reference": telemetry.altitude_reference,
            **_sample(telemetry.altitude_agl_m, telemetry.altitude_observed_at_s),
        },
        "position": _sample(telemetry.latitude_deg, telemetry.position_observed_at_s),
        "local_position": _sample(
            telemetry.local_down_m,
            telemetry.local_position_observed_at_s,
        ),
    }


def _lock_model_family(label: str | None) -> str:
    normalized = "" if label is None else label.strip().lower()
    if normalized in _PERSON_LOCK_MODEL_LABELS:
        return "person"
    if normalized in _VEHICLE_LOCK_MODEL_LABELS:
        return "vehicle"
    if normalized in _AIRCRAFT_LOCK_MODEL_LABELS:
        return "aircraft"
    if normalized in _FIRE_LOCK_MODEL_LABELS:
        return "fire"
    return "arbitrary_object"


def _lock_model_labels(family: str) -> frozenset[str]:
    if family == "person":
        return _PERSON_LOCK_MODEL_LABELS
    if family == "vehicle":
        return _VEHICLE_LOCK_MODEL_LABELS
    if family == "aircraft":
        return _AIRCRAFT_LOCK_MODEL_LABELS
    if family == "fire":
        return _FIRE_LOCK_MODEL_LABELS
    return frozenset()


def _target_pool_snapshot(
    target_pool: UnifiedTargetPool | None,
    target_id: str | None,
) -> UnifiedTrackSnapshot | None:
    if target_pool is None or target_id is None:
        return None
    return next(
        (snapshot for snapshot in target_pool.snapshots() if snapshot.track_id == target_id),
        None,
    )


def _configure_detector_active_labels(
    detector: Any,
    labels: frozenset[str] | None,
) -> tuple[bool, int | None]:
    """Reach the ensemble through filter wrappers and apply the LCK route."""

    visited: set[int] = set()

    def configure(node: Any) -> tuple[bool, int | None]:
        node_id = id(node)
        if node_id in visited:
            return False, None
        visited.add(node_id)
        setter = getattr(node, "set_active_labels", None)
        if callable(setter):
            active_count = setter(labels)
            return True, active_count if isinstance(active_count, int) else None
        child = getattr(node, "detector", None)
        if child is not None:
            applied, active_count = configure(child)
            if applied:
                return applied, active_count
        for nested in getattr(node, "detectors", ()):
            applied, active_count = configure(nested)
            if applied:
                return applied, active_count
        return False, None

    return configure(detector)


def _detector_covers_any_label(detector: Any, labels: frozenset[str]) -> bool:
    if not labels:
        return False
    covers_labels = getattr(detector, "covers_labels", None)
    if callable(covers_labels):
        for label in labels:
            try:
                if covers_labels((label,)):
                    return True
            except (AttributeError, TypeError, ValueError):
                break
    class_names = getattr(detector, "class_names", ())
    available = frozenset(str(label).strip().lower() for label in class_names)
    if available.intersection(labels):
        return True
    child = getattr(detector, "detector", None)
    if child is not None and _detector_covers_any_label(child, labels):
        return True
    return any(
        _detector_covers_any_label(nested, labels) for nested in getattr(detector, "detectors", ())
    )


def _reid_allowed_labels(encoder: Any, *, fallback: frozenset[str]) -> frozenset[str]:
    config = getattr(encoder, "config", None)
    configured = getattr(config, "allowed_labels", None)
    if configured is None:
        return fallback
    normalized = frozenset(str(label).strip().lower() for label in configured if str(label).strip())
    return normalized or fallback


def _operator_track_snapshots(
    tracks: tuple[UnifiedTrackSnapshot, ...],
) -> tuple[TrackSnapshot, ...]:
    """Adapt the shared target bank to the operator lock's read-only track view.

    QGC candidates are produced from the unified bank, so selection association
    must use that same bank. Feeding only the fire-mission tracker made person and
    vehicle "+" candidates fall through to the generic manual tracker.
    """

    return tuple(
        TrackSnapshot(
            track_id=track.track_id,
            revision=max(1, track.observation_count),
            label=track.label,
            bbox=track.bbox,
            first_seen_at_s=track.first_seen_at_s,
            last_seen_at_s=track.last_seen_at_s,
            observation_count=track.observation_count,
            consecutive_observations=max(
                1,
                track.observation_count - track.missed_frame_count,
            ),
            confidence_floor=track.confidence,
            confidence_mean=track.confidence,
            maximum_gap_s=max(0.0, track.state_changed_at_s - track.last_seen_at_s),
            area_growth_rate=0.0,
            thermal_corroborated=False,
            confirmed=track.state not in {UnifiedTrackState.DETECTED, UnifiedTrackState.LOST},
            independent_rgb_corroborated=False,
        )
        for track in tracks
        if track.state is not UnifiedTrackState.LOST
    )


def _has_reid_candidates(
    detections: tuple[Detection, ...],
    allowed_labels: frozenset[str],
) -> bool:
    return any(detection.label.strip().lower() in allowed_labels for detection in detections)


def _has_fire_candidates(detections: tuple[Detection, ...]) -> bool:
    return any(
        detection.label.strip().lower() in _FIRE_LOCK_MODEL_LABELS for detection in detections
    )


def _reid_recovery_required(
    target_pool: UnifiedTargetPool,
    allowed_labels: frozenset[str],
) -> bool:
    recovery_states = {
        UnifiedTrackState.OCCLUDED,
        UnifiedTrackState.REACQUIRING,
        UnifiedTrackState.LOST,
    }
    return any(
        track.label in allowed_labels and track.state in recovery_states
        for track in target_pool.snapshots()
    )


def _reid_inference_due(
    *,
    frame_index: int,
    now_s: float,
    last_inference_at_s: float | None,
    frame_stride: int,
    frame_phase: int,
    maximum_interval_s: float,
    recovery_required: bool,
) -> tuple[bool, bool]:
    """Return whether ReID is due and whether recovery overrode normal cadence."""

    if not 0 <= frame_phase < frame_stride:
        raise ValueError("ReID frame phase must be smaller than the frame stride")
    if last_inference_at_s is None:
        return True, False
    cadence_due = frame_index % frame_stride == frame_phase
    interval_due = max(0.0, now_s - last_inference_at_s) >= maximum_interval_s
    forced_recovery = recovery_required and not cadence_due and not interval_due
    return cadence_due or interval_due or recovery_required, forced_recovery


def _target_pool_status_interval_s(
    *,
    normal_interval_s: float,
    operator_trk_interval_s: float,
    exclusive_lock_interval_s: float,
    operator_tracked_target_count: int,
    exclusive_lock_track_id: str | None,
) -> float:
    """Choose a bounded metadata cadence without changing detector workload.

    DET and explicit multi-TRK refresh at up to 25 Hz, so a source running at
    the 15 Hz ground-overlay minimum emits every fresh tracker update rather
    than falling into an every-other-frame heartbeat. Exclusive LCK remains
    capped at 30 Hz. This only changes target-pool metadata publication; it
    does not alter model, mission, or flight-control workload.
    """

    if exclusive_lock_track_id is not None:
        return exclusive_lock_interval_s
    if operator_tracked_target_count > 0:
        return operator_trk_interval_s
    return normal_interval_s


def _merge_reid_observations(
    base: tuple[TargetObservation, ...],
    additional: tuple[TargetObservation, ...],
) -> tuple[TargetObservation, ...]:
    """Merge disjoint learned ReID domains without comparing their feature spaces."""

    if len(base) != len(additional):
        raise ValueError("ReID encoders must preserve the detector observation count")
    merged: list[TargetObservation] = []
    for existing, candidate in zip(base, additional, strict=True):
        if (
            existing.label != candidate.label
            or existing.confidence != candidate.confidence
            or existing.bbox != candidate.bbox
            or existing.source != candidate.source
        ):
            raise ValueError("ReID encoders must preserve detector observation ordering")
        if candidate.appearance is None:
            merged.append(existing)
            continue
        if existing.appearance is not None:
            raise ValueError("ReID label domains must be disjoint")
        merged.append(
            replace(
                existing,
                appearance=candidate.appearance,
                appearance_reliable=candidate.appearance_reliable,
            )
        )
    return tuple(merged)


def _percentile(values: deque[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _interval_rate(
    count: int,
    first_at_s: float | None,
    last_at_s: float | None,
) -> float:
    if count < 2 or first_at_s is None or last_at_s is None:
        return 0.0
    return (count - 1) / max(last_at_s - first_at_s, 1e-9)
