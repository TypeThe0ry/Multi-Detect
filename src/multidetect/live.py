from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from .alerts import AlertPublisher, RecordingAlertPublisher, SqliteAlertOutbox
from .domain import (
    BoundingBox,
    DeploymentWindowSolution,
    FireAlert,
    FrameObservation,
    MissionPhase,
    TrackSnapshot,
    VehicleTelemetry,
)
from .evaluation import JsonlPredictionWriter
from .manual_tracking import OpenCVManualTargetTracker
from .mission import MissionController, ObservationOutcome
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
    build_safety_status_message,
)
from .operator_tracking import (
    FIRE_CANDIDATE_TRACK_LABELS,
    OperatorTargetLock,
    TargetLockConfig,
)
from .payload_hil_cycle import InertPayloadHilCycleCoordinator
from .telemetry import (
    TelemetryProvider,
    with_observed_flight_mode_permission,
    with_person_detector_health,
)
from .vision import CapturedFrame, DetectorEnsemble, OpenCVFrameSource, VisionDependencyError


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
class LiveRunResult:
    processed_frames: int
    final_phase: MissionPhase
    authorization_count: int
    alert_delivery_count: int
    alert_delivery_failure_count: int
    average_fps: float
    capture_latency_p50_ms: float
    capture_latency_p95_ms: float
    inference_latency_p50_ms: float
    inference_latency_p95_ms: float
    camera_reconnect_count: int
    retried_alert_count: int
    simulated_payload_cycle_count: int
    local_selection_count: int
    local_tracking_status_count: int
    remote_selection_count: int
    remote_tracking_status_count: int
    remote_mission_status_count: int
    remote_safety_status_count: int
    remote_transport_error_count: int


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
        line = (
            f"{mode} | {phase.value} | {fps:.1f} FPS | "
            f"TARGET {target_state} | WINDOW {window_state}"
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
    """Connect live pixels to safety and optional explicit inert HIL, never an actuator."""

    def __init__(
        self,
        *,
        mission: MissionController,
        frame_source: OpenCVFrameSource,
        detector: DetectorEnsemble,
        telemetry_provider: TelemetryProvider,
        config: LiveRunConfig,
        alert_publisher: AlertPublisher | None = None,
        alert_outbox: SqliteAlertOutbox | None = None,
        prediction_writer: JsonlPredictionWriter | None = None,
        operator_bridge: LiveOperatorBridge | None = None,
        payload_hil_cycle: InertPayloadHilCycleCoordinator | None = None,
    ) -> None:
        if payload_hil_cycle is not None and not config.simulate_payload_cycle:
            raise ValueError("payload HIL cycle requires simulate_payload_cycle=true")
        if payload_hil_cycle is not None and payload_hil_cycle.mission is not mission:
            raise ValueError("payload HIL cycle must use the live mission controller")
        self.mission = mission
        self.frame_source = frame_source
        self.detector = detector
        self.telemetry_provider = telemetry_provider
        self.config = config
        self.alert_publisher = alert_publisher or RecordingAlertPublisher()
        self.alert_outbox = alert_outbox
        self.prediction_writer = prediction_writer
        self.operator_bridge = operator_bridge
        self.payload_hil_cycle = payload_hil_cycle
        self._lifecycle_waiting_fingerprint: tuple[object, ...] | None = None

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
        remote_mission_status_sequence = 0
        remote_safety_status_sequence = 0
        remote_authorization_challenge_sequence = 0
        remote_transport_errors = 0
        latest_alert: FireAlert | None = None
        latest_alert_delivery_status: str | None = None
        recent_events: deque[str] = deque(maxlen=8)
        capture_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        inference_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        run_started_s = time.monotonic()
        local_target_lock: OperatorTargetLock | None = None
        local_manual_tracker: OpenCVManualTargetTracker | None = None
        local_track_status: TrackStatusMessage | None = None

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
                inference_started_s = time.perf_counter()
                try:
                    detections = self.detector.detect(captured.image_bgr)
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    self.mission.audit.append(
                        "perception.inference_failed",
                        captured.captured_at_s,
                        {"error_type": type(exc).__name__, "frame_id": captured.frame_id},
                    )
                    raise
                inference_elapsed_ms = (time.perf_counter() - inference_started_s) * 1_000.0
                inference_latency_ms.append(inference_elapsed_ms)
                if self.prediction_writer is not None:
                    self.prediction_writer.append(
                        frame_id=captured.frame_id,
                        captured_at_s=captured.captured_at_s,
                        detections=detections,
                        inference_latency_ms=inference_elapsed_ms,
                    )
                telemetry = self.telemetry_provider.snapshot(now_s=captured.captured_at_s)
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
                        now_s=captured.captured_at_s,
                    )
                observation = FrameObservation(
                    frame_id=captured.frame_id,
                    captured_at_s=captured.captured_at_s,
                    detections=detections,
                    telemetry=telemetry,
                )
                if self.mission.state.phase in {
                    MissionPhase.SEARCHING,
                    MissionPhase.AWAITING_AUTHORIZATION,
                    MissionPhase.DEPLOYMENT_READY,
                }:
                    outcome = self.mission.process_observation(
                        observation,
                        now_s=captured.captured_at_s,
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
                        delivered_at_s=captured.captured_at_s,
                        retry=False,
                    )
                remote_authorization_handled = False
                if self.operator_bridge is not None:
                    remote_mission_status_sequence = (
                        remote_mission_status_sequence + 1
                    ) & 0xFFFFFFFF
                    remote_safety_status_sequence = (remote_safety_status_sequence + 1) & 0xFFFFFFFF
                    produced_at_s = max(captured.captured_at_s, time.monotonic())
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
                                challenge_clock_now_s=captured.captured_at_s,
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
                    bridge_result = self.operator_bridge.process_frame(
                        tracks=outcome.tracks,
                        frame_id=captured.frame_id,
                        captured_at_s=captured.captured_at_s,
                        produced_at_s=produced_at_s,
                        mission_status=mission_status_message,
                        safety_status=safety_status_message,
                        authorization_challenge=authorization_challenge_message,
                    )
                    remote_selections += bridge_result.accepted_command_count
                    remote_tracking_statuses += len(bridge_result.published_statuses)
                    remote_mission_statuses += len(bridge_result.published_mission_statuses)
                    remote_safety_statuses += len(bridge_result.published_safety_statuses)
                    remote_transport_errors += len(bridge_result.transport_errors)
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
                                captured.captured_at_s,
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
                                captured.captured_at_s,
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
                                captured.captured_at_s,
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
                            captured.captured_at_s,
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
                            captured.captured_at_s,
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
                            captured.captured_at_s,
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
                    if bridge_result.accepted_command_count:
                        recent_events.append("REMOTE TARGET SELECTION ACCEPTED")
                    for error_type in bridge_result.transport_errors:
                        self.mission.audit.append(
                            "operator.remote_transport_error",
                            captured.captured_at_s,
                            {
                                "error_type": error_type,
                                "hardware_control_enabled": False,
                            },
                        )
                        recent_events.append(f"REMOTE LINK ERROR {error_type}")
                if ui is not None:
                    consume_target_command = getattr(ui, "consume_target_command", None)
                    local_command = (
                        consume_target_command(captured, now_s=captured.captured_at_s)
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
                            local_manual_tracker = OpenCVManualTargetTracker(local_command.geometry)
                        detector_lock_status = local_target_lock.apply_command(
                            local_command,
                            tracks=outcome.tracks,
                            frame_id=captured.frame_id,
                            now_s=captured.captured_at_s,
                        )
                        if local_manual_tracker is None:
                            raise RuntimeError("manual target tracker failed to initialize")
                        if (
                            local_command.action is SelectionAction.CANCEL
                            or detector_lock_status.state is TrackingState.TRACKING
                        ):
                            local_track_status = detector_lock_status
                            if local_command.action is SelectionAction.CANCEL:
                                local_manual_tracker.apply_command(
                                    local_command,
                                    image_bgr=captured.image_bgr,
                                    frame_id=captured.frame_id,
                                    now_s=captured.captured_at_s,
                                )
                            else:
                                local_manual_tracker = OpenCVManualTargetTracker(
                                    local_command.geometry
                                )
                        else:
                            local_track_status = local_manual_tracker.apply_command(
                                local_command,
                                image_bgr=captured.image_bgr,
                                frame_id=captured.frame_id,
                                now_s=captured.captured_at_s,
                            )
                        local_selections += 1
                        local_tracking_statuses += 1
                        self.mission.audit.append(
                            "operator.local_target_selection",
                            captured.captured_at_s,
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
                        if (
                            detector_lock_status is not None
                            and detector_lock_status.state is TrackingState.TRACKING
                        ):
                            local_track_status = detector_lock_status
                            local_manual_tracker = OpenCVManualTargetTracker(
                                local_target_lock.geometry
                            )
                        elif local_manual_tracker is not None and local_manual_tracker.active:
                            local_track_status = local_manual_tracker.update(
                                image_bgr=captured.image_bgr,
                                frame_id=captured.frame_id,
                                captured_at_s=captured.captured_at_s,
                                produced_at_s=max(captured.captured_at_s, time.monotonic()),
                            )
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
                status = self.mission.status()
                elapsed_s = max(time.monotonic() - run_started_s, 1e-9)
                fps = processed / elapsed_s
                visible_alerts = (
                    (latest_alert,)
                    if latest_alert is not None
                    and captured.captured_at_s - latest_alert.observed_at_s
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
                    )
                    if ui is not None
                    else None
                )
                if action == "quit":
                    break
                if action == "ack_alert" and latest_alert is not None:
                    self.mission.audit.append(
                        "alert.operator_acknowledged",
                        captured.captured_at_s,
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
            if self.operator_bridge is not None:
                self.operator_bridge.close()
            if self.payload_hil_cycle is not None:
                self.payload_hil_cycle.close()
            if ui is not None:
                ui.close()
            close = getattr(self.telemetry_provider, "close", None)
            if callable(close):
                close()
        result = LiveRunResult(
            processed_frames=processed,
            final_phase=self.mission.status().phase,
            authorization_count=authorizations,
            alert_delivery_count=alert_deliveries,
            alert_delivery_failure_count=alert_delivery_failures,
            average_fps=processed / max(time.monotonic() - run_started_s, 1e-9),
            capture_latency_p50_ms=_percentile(capture_latency_ms, 0.50),
            capture_latency_p95_ms=_percentile(capture_latency_ms, 0.95),
            inference_latency_p50_ms=_percentile(inference_latency_ms, 0.50),
            inference_latency_p95_ms=_percentile(inference_latency_ms, 0.95),
            camera_reconnect_count=int(getattr(self.frame_source, "reconnect_count", 0)),
            retried_alert_count=retried_alert_count,
            simulated_payload_cycle_count=simulated_payload_cycles,
            local_selection_count=local_selections,
            local_tracking_status_count=local_tracking_statuses,
            remote_selection_count=remote_selections,
            remote_tracking_status_count=remote_tracking_statuses,
            remote_mission_status_count=remote_mission_statuses,
            remote_safety_status_count=remote_safety_statuses,
            remote_transport_error_count=remote_transport_errors,
        )
        self.mission.audit.append(
            "live.performance_summary",
            time.monotonic(),
            {
                "processed_frames": result.processed_frames,
                "average_fps": result.average_fps,
                "capture_latency_p50_ms": result.capture_latency_p50_ms,
                "capture_latency_p95_ms": result.capture_latency_p95_ms,
                "inference_latency_p50_ms": result.inference_latency_p50_ms,
                "inference_latency_p95_ms": result.inference_latency_p95_ms,
                "camera_reconnect_count": result.camera_reconnect_count,
                "retried_alert_count": result.retried_alert_count,
                "simulated_payload_cycle_count": result.simulated_payload_cycle_count,
                "local_selection_count": result.local_selection_count,
                "local_tracking_status_count": result.local_tracking_status_count,
                "remote_selection_count": result.remote_selection_count,
                "remote_tracking_status_count": result.remote_tracking_status_count,
                "remote_mission_status_count": result.remote_mission_status_count,
                "remote_safety_status_count": result.remote_safety_status_count,
                "remote_transport_error_count": result.remote_transport_error_count,
                "sample_window_frames": self.config.performance_window_frames,
            },
        )
        return result

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


def _percentile(values: deque[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]
