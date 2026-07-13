from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from typing import Any
from uuid import uuid4

from .domain import BoundingBox
from .operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)
from .vision import VisionDependencyError


class OpenCVManualTargetTracker:
    """CSRT/KCF tracking with bounded template reacquisition after a short loss."""

    def __init__(
        self,
        geometry: VideoGeometry,
        *,
        tracker_factory: Callable[[], Any] | None = None,
        reacquisition_timeout_s: float = 2.0,
        minimum_template_score: float = 0.55,
    ) -> None:
        if not isfinite(reacquisition_timeout_s) or reacquisition_timeout_s <= 0.0:
            raise ValueError("reacquisition_timeout_s must be finite and positive")
        if not isfinite(minimum_template_score) or not 0.0 <= minimum_template_score <= 1.0:
            raise ValueError("minimum_template_score must be in [0, 1]")
        self.geometry = geometry
        self._tracker_factory = tracker_factory or self._default_tracker_factory
        self.reacquisition_timeout_s = reacquisition_timeout_s
        self.minimum_template_score = minimum_template_score
        self._tracker: Any | None = None
        self._command_id: str | None = None
        self._target_id: str | None = None
        self._last_bbox: BoundingBox | None = None
        self._template_gray: Any | None = None
        self._lost_since_s: float | None = None
        self._sequence = 0

    @property
    def active(self) -> bool:
        return self._target_id is not None

    def apply_command(
        self,
        command: TargetSelectionCommand,
        *,
        image_bgr: Any,
        frame_id: str,
        now_s: float,
    ) -> TrackStatusMessage:
        self._validate_frame(frame_id=frame_id, now_s=now_s)
        if command.geometry != self.geometry:
            raise ValueError("manual tracker geometry does not match the target selection")
        self._command_id = command.command_id
        if command.action is SelectionAction.CANCEL:
            self._clear_target()
            return self._status(
                state=TrackingState.CANCELLED,
                frame_id=frame_id,
                captured_at_s=now_s,
                produced_at_s=now_s,
            )

        selection_bbox = command.bbox
        if selection_bbox is None:
            raise ValueError("select and switch commands require a bounding box")
        tracker = self._tracker_factory()
        initialized = tracker.init(image_bgr, self._normalized_to_pixel_xywh(selection_bbox))
        if initialized is False:
            self._clear_target()
            return self._status(
                state=TrackingState.REJECTED,
                frame_id=frame_id,
                captured_at_s=now_s,
                produced_at_s=now_s,
            )
        self._tracker = tracker
        self._target_id = f"manual-{command.command_id[:8]}"
        self._last_bbox = selection_bbox
        self._template_gray = self._extract_template(image_bgr, selection_bbox)
        self._lost_since_s = None
        return self._tracking_status(
            bbox=selection_bbox,
            frame_id=frame_id,
            captured_at_s=now_s,
            produced_at_s=now_s,
        )

    def update(
        self,
        *,
        image_bgr: Any,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
    ) -> TrackStatusMessage | None:
        self._validate_frame(frame_id=frame_id, now_s=produced_at_s)
        if captured_at_s > produced_at_s:
            raise ValueError("manual tracking status cannot predate its source frame")
        if self._command_id is None or self._target_id is None:
            return None

        bbox = None
        if self._tracker is not None:
            tracked, pixel_bbox = self._tracker.update(image_bgr)
            if tracked:
                bbox = self._pixel_xywh_to_normalized(pixel_bbox)
        if bbox is not None:
            self._last_bbox = bbox
            self._lost_since_s = None
            return self._tracking_status(
                bbox=bbox,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
            )

        self._tracker = None
        if self._lost_since_s is None:
            self._lost_since_s = produced_at_s
        reacquired = self._reacquire_from_template(image_bgr)
        if reacquired is not None:
            bbox, score = reacquired
            tracker = self._tracker_factory()
            initialized = tracker.init(image_bgr, self._normalized_to_pixel_xywh(bbox))
            if initialized is not False:
                self._tracker = tracker
                self._last_bbox = bbox
                self._lost_since_s = None
                return self._tracking_status(
                    bbox=bbox,
                    frame_id=frame_id,
                    captured_at_s=captured_at_s,
                    produced_at_s=produced_at_s,
                    tracking_quality=score,
                )

        lost_since_s = self._lost_since_s
        if lost_since_s is None:
            raise RuntimeError("manual tracker loss timestamp is not initialized")
        if produced_at_s - lost_since_s <= self.reacquisition_timeout_s:
            last_bbox = self._last_bbox
            if last_bbox is None:
                raise RuntimeError("manual tracker has no last bounding box")
            return self._status(
                state=TrackingState.INITIALIZING,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                target_id=self._target_id,
                bbox=last_bbox,
                label="manual",
                tracking_quality=0.0,
            )

        target_id = self._target_id
        self._clear_target()
        return self._status(
            state=TrackingState.LOST,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
            target_id=target_id,
        )

    def _tracking_status(
        self,
        *,
        bbox: BoundingBox,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
        tracking_quality: float | None = None,
    ) -> TrackStatusMessage:
        return self._status(
            state=TrackingState.TRACKING,
            frame_id=frame_id,
            captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
            target_id=self._target_id,
            bbox=bbox,
            label="manual",
            tracking_quality=tracking_quality,
        )

    def _extract_template(self, image_bgr: Any, bbox: BoundingBox) -> Any | None:
        if not hasattr(image_bgr, "shape"):
            return None
        cv2 = self._require_cv2()
        x, y, width, height = self._normalized_to_pixel_xywh(bbox)
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        patch = gray[y : y + height, x : x + width]
        if patch.size < 16 or float(patch.std()) < 3.0:
            return None
        return patch.copy()

    def _reacquire_from_template(self, image_bgr: Any) -> tuple[BoundingBox, float] | None:
        if self._template_gray is None or self._last_bbox is None:
            return None
        cv2 = self._require_cv2()
        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        x, y, width, height = self._normalized_to_pixel_xywh(self._last_bbox)
        margin_x = max(width, round(width * 1.5))
        margin_y = max(height, round(height * 1.5))
        left = max(0, x - margin_x)
        top = max(0, y - margin_y)
        right = min(self.geometry.width, x + width + margin_x)
        bottom = min(self.geometry.height, y + height + margin_y)
        search = gray[top:bottom, left:right]
        template_height, template_width = self._template_gray.shape[:2]
        if search.shape[0] < template_height or search.shape[1] < template_width:
            return None
        scores = cv2.matchTemplate(search, self._template_gray, cv2.TM_CCOEFF_NORMED)
        _, maximum_score, _, maximum_location = cv2.minMaxLoc(scores)
        if not isfinite(maximum_score) or maximum_score < self.minimum_template_score:
            return None
        match_x = left + maximum_location[0]
        match_y = top + maximum_location[1]
        bbox = self._pixel_xywh_to_normalized((match_x, match_y, template_width, template_height))
        return (bbox, float(maximum_score)) if bbox is not None else None

    def _normalized_to_pixel_xywh(self, bbox: BoundingBox) -> tuple[int, int, int, int]:
        x = round(bbox.x1 * self.geometry.width)
        y = round(bbox.y1 * self.geometry.height)
        width = max(1, round((bbox.x2 - bbox.x1) * self.geometry.width))
        height = max(1, round((bbox.y2 - bbox.y1) * self.geometry.height))
        return x, y, width, height

    def _pixel_xywh_to_normalized(self, values: Any) -> BoundingBox | None:
        try:
            x, y, width, height = (float(value) for value in values)
        except (TypeError, ValueError):
            return None
        if not all(isfinite(value) for value in (x, y, width, height)):
            return None
        x1 = min(1.0, max(0.0, x / self.geometry.width))
        y1 = min(1.0, max(0.0, y / self.geometry.height))
        x2 = min(1.0, max(0.0, (x + width) / self.geometry.width))
        y2 = min(1.0, max(0.0, (y + height) / self.geometry.height))
        if x2 <= x1 or y2 <= y1:
            return None
        return BoundingBox(x1, y1, x2, y2)

    def _status(
        self,
        *,
        state: TrackingState,
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
        target_id: str | None = None,
        bbox: BoundingBox | None = None,
        label: str | None = None,
        tracking_quality: float | None = None,
    ) -> TrackStatusMessage:
        command_id = self._command_id
        if command_id is None:
            raise RuntimeError("cannot emit manual tracking status without a selection command")
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return TrackStatusMessage(
            status_id=str(uuid4()),
            selection_command_id=command_id,
            sequence=self._sequence,
            geometry=self.geometry,
            state=state,
            target_id=target_id,
            bbox=bbox,
            label=label,
            confidence=None,
            tracking_quality=tracking_quality,
            source_frame_id=frame_id,
            source_captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
        )

    def _clear_target(self) -> None:
        self._tracker = None
        self._target_id = None
        self._last_bbox = None
        self._template_gray = None
        self._lost_since_s = None

    @classmethod
    def _default_tracker_factory(cls) -> Any:
        cv2 = cls._require_cv2()
        for creator_name in ("TrackerCSRT_create", "TrackerKCF_create", "TrackerMIL_create"):
            creator = getattr(cv2, creator_name, None)
            if callable(creator):
                return creator()
        legacy = getattr(cv2, "legacy", None)
        for creator_name in ("TrackerCSRT_create", "TrackerKCF_create", "TrackerMIL_create"):
            creator = getattr(legacy, creator_name, None)
            if callable(creator):
                return creator()
        raise VisionDependencyError("no supported OpenCV single-object tracker is available")

    @staticmethod
    def _require_cv2() -> Any:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - dependency-specific.
            raise VisionDependencyError(
                "Install live vision dependencies: pip install -e '.[vision]'"
            ) from exc
        return cv2

    @staticmethod
    def _validate_frame(*, frame_id: str, now_s: float) -> None:
        if not frame_id.strip():
            raise ValueError("manual tracker frame_id cannot be empty")
        if not isfinite(now_s) or now_s < 0.0:
            raise ValueError("manual tracker timestamp must be finite and non-negative")


__all__ = ["OpenCVManualTargetTracker"]
