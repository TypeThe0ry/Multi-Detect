from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .alerts import AlertPublisher, RecordingAlertPublisher, SqliteAlertOutbox
from .domain import FireAlert, FrameObservation, MissionPhase, TrackSnapshot, VehicleTelemetry
from .evaluation import JsonlPredictionWriter
from .mission import MissionController, ObservationOutcome
from .operator_bridge import LiveOperatorBridge
from .telemetry import TelemetryProvider, with_person_detector_health
from .vision import CapturedFrame, DetectorEnsemble, OpenCVFrameSource, VisionDependencyError


@dataclass(frozen=True, slots=True)
class LiveRunConfig:
    operator_id: str = "local-operator"
    max_frames: int | None = None
    display: bool = True
    alert_banner_seconds: float = 5.0
    performance_window_frames: int = 600
    simulate_payload_cycle: bool = False
    observe_pixhawk_lifecycle: bool = False
    task_area_mission_sequence: int | None = None
    allowed_auto_modes: tuple[str, ...] = ("AUTO", "MISSION", "AUTO_MISSION")

    def __post_init__(self) -> None:
        if self.max_frames is not None and self.max_frames <= 0:
            raise ValueError("max_frames must be positive when supplied")
        if not self.operator_id.strip():
            raise ValueError("operator_id cannot be empty")
        if not math.isfinite(self.alert_banner_seconds) or self.alert_banner_seconds <= 0:
            raise ValueError("alert_banner_seconds must be a finite positive number")
        if self.performance_window_frames <= 0:
            raise ValueError("performance_window_frames must be positive")
        if self.observe_pixhawk_lifecycle and (
            self.task_area_mission_sequence is None or self.task_area_mission_sequence < 0
        ):
            raise ValueError(
                "observed Pixhawk lifecycle requires a non-negative task-area mission sequence"
            )
        if not self.allowed_auto_modes or any(not mode.strip() for mode in self.allowed_auto_modes):
            raise ValueError("allowed_auto_modes must contain non-empty values")


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
    remote_selection_count: int
    remote_tracking_status_count: int
    remote_transport_error_count: int


class OpenCVAuthorizationUI:
    """Local display that records approval/denial only; it cannot request a release."""

    def __init__(self, *, title: str = "Multi-Detect live (A approve / D deny / Q quit)") -> None:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError(
                "Install live vision dependencies: pip install -e '.[vision]'"
            ) from exc
        self._cv2 = cv2
        self._title = title
        self._position_history: list[tuple[float, float]] = []

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
    ) -> str | None:
        panel_width = 360
        image = self._cv2.copyMakeBorder(
            captured.image_bgr.copy(),
            0,
            96,
            0,
            panel_width,
            self._cv2.BORDER_CONSTANT,
            value=(22, 24, 28),
        )
        if math.isfinite(telemetry.latitude_deg) and math.isfinite(telemetry.longitude_deg):
            position = (telemetry.latitude_deg, telemetry.longitude_deg)
            if not self._position_history or position != self._position_history[-1]:
                self._position_history.append(position)
                self._position_history = self._position_history[-200:]
        for detection in detections:
            x1 = round(detection.bbox.x1 * captured.width)
            y1 = round(detection.bbox.y1 * captured.height)
            x2 = round(detection.bbox.x2 * captured.width)
            y2 = round(detection.bbox.y2 * captured.height)
            color = (0, 220, 0) if detection.label in {"flame", "smoke"} else (0, 165, 255)
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
        for track in tracks:
            if not track.confirmed:
                continue
            x1 = round(track.bbox.x1 * captured.width)
            y1 = round(track.bbox.y1 * captured.height)
            x2 = round(track.bbox.x2 * captured.width)
            y2 = round(track.bbox.y2 * captured.height)
            self._cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 255), 3)
            self._cv2.putText(
                image,
                f"{track.track_id} confirmed {track.duration_s:.1f}s",
                (x1, min(captured.height - 8, y2 + 20)),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 0, 255),
                1,
                self._cv2.LINE_AA,
            )
        mode = "PATROL+PAYLOAD" if deployment_capable else "PATROL"
        line = f"{mode} | {phase.value} | payload {remaining_payload_count} | {fps:.1f} FPS"
        if deployment_capable:
            inventory_state = "VERIFIED" if payload_inventory_verified else "UNVERIFIED"
            line += f" | INVENTORY {inventory_state}"
        if deployment_ready:
            line += " | AUTHORIZED (simulation only)"
            if simulation_cycle_enabled:
                line += " | S SIMULATE"
        elif pending_authorization:
            line += " | AUTHORIZATION PENDING"
        self._cv2.rectangle(image, (0, 0), (captured.width, 39), (16, 18, 22), -1)
        self._cv2.putText(
            image,
            line,
            (12, 28),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (255, 255, 255),
            2,
            self._cv2.LINE_AA,
        )
        if alerts:
            alert = alerts[0]
            self._cv2.rectangle(image, (0, 42), (captured.width, 82), (0, 0, 180), -1)
            self._cv2.putText(
                image,
                (
                    f"FIRE | {alert.target_id} | {alert.confidence:.2f} "
                    f"| {alert_delivery_status or 'pending'} | C ACK"
                ),
                (12, 69),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
                self._cv2.LINE_AA,
            )
        panel_x = captured.width + 16
        self._cv2.putText(
            image,
            "TARGET QUEUE",
            (panel_x, 28),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (235, 235, 235),
            1,
            self._cv2.LINE_AA,
        )
        ordered_tracks = sorted(
            tracks,
            key=lambda item: (not item.confirmed, -item.confidence_mean, item.track_id),
        )
        for index, track in enumerate(ordered_tracks[:5]):
            state = "CONFIRMED" if track.confirmed else "TRACKING"
            self._cv2.putText(
                image,
                (
                    f"{track.track_id} {track.label} {track.confidence_mean:.2f} "
                    f"{track.duration_s:.1f}s {state}"
                ),
                (panel_x, 56 + index * 24),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (70, 90, 255) if track.confirmed else (200, 200, 200),
                1,
                self._cv2.LINE_AA,
            )
        position_y = 180
        self._cv2.putText(
            image,
            "POSITION / TELEMETRY",
            (panel_x, position_y),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (235, 235, 235),
            1,
            self._cv2.LINE_AA,
        )
        position_lines = (
            f"LAT {_display_coordinate(telemetry.latitude_deg)}",
            f"LON {_display_coordinate(telemetry.longitude_deg)}",
            f"HDG {_display_number(telemetry.heading_deg, 'deg')}",
            f"BAT {_display_number(telemetry.battery_remaining_pct, '%')}",
            f"SAT {_display_optional_integer(telemetry.satellites_visible)}",
            f"ARMED {_display_optional_boolean(telemetry.armed)}",
            f"MODE {telemetry.flight_mode or 'UNKNOWN'}",
            f"MISSION_SEQ {_display_optional_integer(telemetry.mission_sequence)}",
            f"PAYLOAD_INV {payload_inventory_source[:22]}",
        )
        for index, text in enumerate(position_lines):
            self._cv2.putText(
                image,
                text,
                (panel_x, position_y + 26 + index * 18),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (210, 210, 210),
                1,
                self._cv2.LINE_AA,
            )
        events_y = min(captured.height - 120, 350)
        self._cv2.putText(
            image,
            "RECENT EVENTS",
            (panel_x, events_y),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (235, 235, 235),
            1,
            self._cv2.LINE_AA,
        )
        for index, text in enumerate(recent_events[-5:]):
            self._cv2.putText(
                image,
                text[:44],
                (panel_x, events_y + 26 + index * 18),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.42,
                (190, 190, 190),
                1,
                self._cv2.LINE_AA,
            )
        self._draw_telemetry_strip(
            image,
            video_width=captured.width,
            video_height=captured.height,
            telemetry=telemetry,
            fps=fps,
            inference_latency_p95_ms=inference_latency_p95_ms,
            camera_reconnect_count=camera_reconnect_count,
        )
        self._draw_aircraft_track_map(
            image,
            left=captured.width + 12,
            top=captured.height + 8,
            right=captured.width + panel_width - 12,
            bottom=captured.height + 86,
            telemetry=telemetry,
        )
        self._cv2.imshow(self._title, image)
        key = self._cv2.waitKey(1) & 0xFF
        return {
            ord("a"): "approve",
            ord("c"): "ack_alert",
            ord("d"): "deny",
            ord("q"): "quit",
            ord("s"): "simulate_payload",
        }.get(key)

    def _draw_telemetry_strip(
        self,
        image: Any,
        *,
        video_width: int,
        video_height: int,
        telemetry: VehicleTelemetry,
        fps: float,
        inference_latency_p95_ms: float,
        camera_reconnect_count: int,
    ) -> None:
        self._cv2.line(image, (0, video_height), (video_width, video_height), (75, 84, 95), 1)
        lines = (
            (
                f"ALT {_display_number(telemetry.altitude_agl_m, 'm')}   "
                f"SPD {_display_number(telemetry.ground_speed_mps, 'm/s')}   "
                f"HDG {_display_number(telemetry.heading_deg, 'deg')}   "
                f"ROLL {_display_number(telemetry.roll_deg, 'deg')}   "
                f"PITCH {_display_number(telemetry.pitch_deg, 'deg')}"
            ),
            (
                f"LINK {_display_health(telemetry.link_healthy)}   "
                f"POSITION {_display_health(telemetry.position_healthy)}   "
                f"GPS SAT {_display_optional_integer(telemetry.satellites_visible)}   "
                f"BAT {_display_number(telemetry.battery_remaining_pct, '%')}   "
                f"CAMERA OK / RECONNECTS {camera_reconnect_count}   "
                f"MODEL OK / {fps:.1f} FPS / P95 {inference_latency_p95_ms:.1f}ms"
            ),
        )
        for index, text in enumerate(lines):
            self._cv2.putText(
                image,
                text,
                (12, video_height + 32 + index * 34),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.47,
                (225, 230, 235) if index == 0 else (180, 195, 205),
                1,
                self._cv2.LINE_AA,
            )

    def _draw_aircraft_track_map(
        self,
        image: Any,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        telemetry: VehicleTelemetry,
    ) -> None:
        self._cv2.rectangle(image, (left, top), (right, bottom), (70, 78, 88), 1)
        self._cv2.putText(
            image,
            "AIRCRAFT TRACK (relative)",
            (left + 8, top + 18),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (220, 225, 230),
            1,
            self._cv2.LINE_AA,
        )
        if not self._position_history:
            self._cv2.putText(
                image,
                "Waiting for GLOBAL_POSITION_INT",
                (left + 8, top + 47),
                self._cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                (145, 155, 165),
                1,
                self._cv2.LINE_AA,
            )
            return
        latitudes = [point[0] for point in self._position_history]
        longitudes = [point[1] for point in self._position_history]
        minimum_latitude = min(latitudes)
        minimum_longitude = min(longitudes)
        latitude_span = max(max(latitudes) - minimum_latitude, 1e-7)
        longitude_span = max(max(longitudes) - minimum_longitude, 1e-7)
        plot_left, plot_right = left + 190, right - 10
        plot_top, plot_bottom = top + 10, bottom - 10
        points = [
            (
                plot_left
                + round(
                    (longitude - minimum_longitude) / longitude_span * (plot_right - plot_left)
                ),
                plot_bottom
                - round((latitude - minimum_latitude) / latitude_span * (plot_bottom - plot_top)),
            )
            for latitude, longitude in self._position_history
        ]
        for start, end in zip(points, points[1:], strict=False):
            self._cv2.line(image, start, end, (220, 170, 60), 2)
        self._cv2.circle(image, points[-1], 4, (60, 220, 255), -1)
        self._cv2.putText(
            image,
            f"{telemetry.latitude_deg:.5f}, {telemetry.longitude_deg:.5f}",
            (left + 8, bottom - 13),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.34,
            (165, 180, 190),
            1,
            self._cv2.LINE_AA,
        )

    def close(self) -> None:
        self._cv2.destroyWindow(self._title)


class LiveMissionRunner:
    """Connects live pixels to perception and authorization, never to a real release port."""

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
    ) -> None:
        self.mission = mission
        self.frame_source = frame_source
        self.detector = detector
        self.telemetry_provider = telemetry_provider
        self.config = config
        self.alert_publisher = alert_publisher or RecordingAlertPublisher()
        self.alert_outbox = alert_outbox
        self.prediction_writer = prediction_writer
        self.operator_bridge = operator_bridge
        self._lifecycle_waiting_fingerprint: tuple[object, ...] | None = None

    def run(self) -> LiveRunResult:
        ui = OpenCVAuthorizationUI() if self.config.display else None
        processed = 0
        authorizations = 0
        alert_deliveries = 0
        alert_delivery_failures = 0
        retried_alert_count = 0
        simulated_payload_cycles = 0
        remote_selections = 0
        remote_tracking_statuses = 0
        remote_transport_errors = 0
        latest_alert: FireAlert | None = None
        latest_alert_delivery_status: str | None = None
        recent_events: deque[str] = deque(maxlen=8)
        capture_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        inference_latency_ms: deque[float] = deque(maxlen=self.config.performance_window_frames)
        run_started_s = time.monotonic()

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
                    healthy=self.detector.covers_labels(self.mission.config.person_labels),
                )
                if self.config.observe_pixhawk_lifecycle:
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
                if self.operator_bridge is not None:
                    bridge_result = self.operator_bridge.process_frame(
                        tracks=outcome.tracks,
                        frame_id=captured.frame_id,
                        captured_at_s=captured.captured_at_s,
                        produced_at_s=max(captured.captured_at_s, time.monotonic()),
                    )
                    remote_selections += bridge_result.accepted_command_count
                    remote_tracking_statuses += len(bridge_result.published_statuses)
                    remote_transport_errors += len(bridge_result.transport_errors)
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
                if action == "approve" and outcome.challenge is not None:
                    self.mission.approve_authorization(
                        challenge_id=outcome.challenge.challenge_id,
                        nonce=outcome.challenge.nonce,
                        operator_id=self.config.operator_id,
                        now_s=time.monotonic(),
                    )
                    authorizations += 1
                    recent_events.append(f"TARGET {outcome.challenge.target_id} AUTHORIZED")
                elif action == "deny" and outcome.challenge is not None:
                    self.mission.deny_authorization(
                        challenge_id=outcome.challenge.challenge_id,
                        nonce=outcome.challenge.nonce,
                        operator_id=self.config.operator_id,
                        now_s=time.monotonic(),
                    )
                    recent_events.append(f"TARGET {outcome.challenge.target_id} DENIED")
                elif (
                    action == "simulate_payload"
                    and self.config.simulate_payload_cycle
                    and self.mission.status().phase is MissionPhase.DEPLOYMENT_READY
                ):
                    self.mission.audit.append(
                        "operator.simulated_payload_cycle_requested",
                        time.monotonic(),
                        {"operator_id": self.config.operator_id},
                    )
                    release_id = self.mission.request_simulated_deployment(now_s=time.monotonic())
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
            remote_selection_count=remote_selections,
            remote_tracking_status_count=remote_tracking_statuses,
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
                "remote_selection_count": result.remote_selection_count,
                "remote_tracking_status_count": result.remote_tracking_status_count,
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
            assert required_sequence is not None
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


def _display_number(value: float, unit: str) -> str:
    return f"{value:.1f}{unit}" if math.isfinite(value) else "UNKNOWN"


def _display_coordinate(value: float) -> str:
    return f"{value:.6f}" if math.isfinite(value) else "UNKNOWN"


def _display_health(value: bool | None) -> str:
    if value is True:
        return "OK"
    if value is False:
        return "STALE"
    return "UNKNOWN"


def _display_optional_integer(value: int | None) -> str:
    return str(value) if value is not None else "UNKNOWN"


def _display_optional_boolean(value: bool | None) -> str:
    if value is True:
        return "YES"
    if value is False:
        return "NO"
    return "UNKNOWN"


def _percentile(values: deque[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]
