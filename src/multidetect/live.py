from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from .domain import FrameObservation, MissionPhase
from .mission import MissionController
from .telemetry import TelemetryProvider, with_person_detector_health
from .vision import CapturedFrame, DetectorEnsemble, OpenCVFrameSource, VisionDependencyError


@dataclass(frozen=True, slots=True)
class LiveRunConfig:
    operator_id: str = "local-operator"
    max_frames: int | None = None
    display: bool = True

    def __post_init__(self) -> None:
        if self.max_frames is not None and self.max_frames <= 0:
            raise ValueError("max_frames must be positive when supplied")
        if not self.operator_id.strip():
            raise ValueError("operator_id cannot be empty")


@dataclass(frozen=True, slots=True)
class LiveRunResult:
    processed_frames: int
    final_phase: MissionPhase
    authorization_count: int


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

    def render(
        self,
        captured: CapturedFrame,
        *,
        detections: tuple[Any, ...],
        phase: MissionPhase,
        pending_authorization: bool,
        deployment_ready: bool,
    ) -> str | None:
        image = captured.image_bgr.copy()
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
        line = f"phase={phase.value} | A=approve D=deny Q=quit"
        if deployment_ready:
            line += " | AUTHORIZED: no physical release driver"
        elif pending_authorization:
            line += " | authorization pending"
        self._cv2.putText(
            image,
            line,
            (12, 28),
            self._cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            self._cv2.LINE_AA,
        )
        self._cv2.imshow(self._title, image)
        key = self._cv2.waitKey(1) & 0xFF
        return {ord("a"): "approve", ord("d"): "deny", ord("q"): "quit"}.get(key)

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
    ) -> None:
        self.mission = mission
        self.frame_source = frame_source
        self.detector = detector
        self.telemetry_provider = telemetry_provider
        self.config = config

    def run(self) -> LiveRunResult:
        ui = OpenCVAuthorizationUI() if self.config.display else None
        processed = 0
        authorizations = 0
        try:
            now_s = time.monotonic()
            self.mission.launch(now_s=now_s)
            self.mission.arrive_task_area(now_s=now_s)
            self.frame_source.open()
            while self.config.max_frames is None or processed < self.config.max_frames:
                captured = self.frame_source.read()
                detections = self.detector.detect(captured.image_bgr)
                telemetry = self.telemetry_provider.snapshot(now_s=captured.captured_at_s)
                telemetry = with_person_detector_health(
                    telemetry,
                    healthy=self.detector.covers_labels(self.mission.config.person_labels),
                )
                observation = FrameObservation(
                    frame_id=captured.frame_id,
                    captured_at_s=captured.captured_at_s,
                    detections=detections,
                    telemetry=telemetry,
                )
                outcome = self.mission.process_observation(
                    observation,
                    now_s=captured.captured_at_s,
                )
                processed += 1
                status = self.mission.status()
                action = (
                    ui.render(
                        captured,
                        detections=detections,
                        phase=status.phase,
                        pending_authorization=outcome.challenge is not None,
                        deployment_ready=status.phase is MissionPhase.DEPLOYMENT_READY,
                    )
                    if ui is not None
                    else None
                )
                if action == "quit":
                    break
                if action == "approve" and outcome.challenge is not None:
                    self.mission.approve_authorization(
                        challenge_id=outcome.challenge.challenge_id,
                        nonce=outcome.challenge.nonce,
                        operator_id=self.config.operator_id,
                        now_s=time.monotonic(),
                    )
                    authorizations += 1
                elif action == "deny" and outcome.challenge is not None:
                    self.mission.deny_authorization(
                        challenge_id=outcome.challenge.challenge_id,
                        nonce=outcome.challenge.nonce,
                        operator_id=self.config.operator_id,
                        now_s=time.monotonic(),
                    )
                self.mission.tick(now_s=time.monotonic())
        finally:
            self.frame_source.close()
            if ui is not None:
                ui.close()
            close = getattr(self.telemetry_provider, "close", None)
            if callable(close):
                close()
        return LiveRunResult(
            processed_frames=processed,
            final_phase=self.mission.status().phase,
            authorization_count=authorizations,
        )

