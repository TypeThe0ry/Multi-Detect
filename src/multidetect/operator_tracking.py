from __future__ import annotations

from dataclasses import dataclass
from math import hypot, isfinite
from uuid import uuid4

from .domain import BoundingBox, TrackSnapshot
from .operator_link import (
    SelectionAction,
    TargetSelectionCommand,
    TrackingState,
    TrackStatusMessage,
    VideoGeometry,
)

FIRE_CANDIDATE_TRACK_LABELS = frozenset(
    {"fire", "flame", "smoke", "hotspot", "smoldering_area", "burned_area"}
)


@dataclass(frozen=True, slots=True)
class TargetLockConfig:
    allowed_labels: frozenset[str]
    acquisition_timeout_s: float = 1.0
    lost_after_s: float = 0.75
    reacquisition_timeout_s: float = 2.0

    def __post_init__(self) -> None:
        normalized = frozenset(
            label.strip().lower() for label in self.allowed_labels if label.strip()
        )
        if not normalized:
            raise ValueError("target-lock allowed_labels cannot be empty")
        if not isfinite(self.acquisition_timeout_s) or self.acquisition_timeout_s <= 0.0:
            raise ValueError("acquisition_timeout_s must be finite and positive")
        if not isfinite(self.lost_after_s) or self.lost_after_s <= 0.0:
            raise ValueError("lost_after_s must be finite and positive")
        if not isfinite(self.reacquisition_timeout_s) or self.reacquisition_timeout_s <= 0.0:
            raise ValueError("reacquisition_timeout_s must be finite and positive")
        object.__setattr__(self, "allowed_labels", normalized)


class OperatorTargetLock:
    """Associates an operator rectangle with perception tracks; it has no mission-control API."""

    def __init__(self, geometry: VideoGeometry, config: TargetLockConfig) -> None:
        self.geometry = geometry
        self.config = config
        self._command_id: str | None = None
        self._selection_bbox: BoundingBox | None = None
        self._acquisition_deadline_s: float | None = None
        self._active_track_id: str | None = None
        self._last_bbox: BoundingBox | None = None
        self._sequence = 0

    @property
    def active_track_id(self) -> str | None:
        return self._active_track_id

    @property
    def selection_command_id(self) -> str | None:
        return self._command_id

    def apply_command(
        self,
        command: TargetSelectionCommand,
        *,
        tracks: tuple[TrackSnapshot, ...],
        frame_id: str,
        now_s: float,
    ) -> TrackStatusMessage:
        self._validate_update(frame_id=frame_id, now_s=now_s)
        if command.geometry != self.geometry:
            raise ValueError("selection geometry does not match the target-lock geometry")
        self._command_id = command.command_id
        self._active_track_id = None
        self._last_bbox = None
        if command.action is SelectionAction.CANCEL:
            self._selection_bbox = None
            self._acquisition_deadline_s = None
            self._last_bbox = None
            return self._status(
                state=TrackingState.CANCELLED,
                frame_id=frame_id,
                captured_at_s=now_s,
                produced_at_s=now_s,
            )
        selection_bbox = command.bbox
        if selection_bbox is None:
            raise ValueError("select and switch commands require a bounding box")
        self._selection_bbox = selection_bbox
        self._acquisition_deadline_s = now_s + self.config.acquisition_timeout_s
        candidate = self._associate(selection_bbox, tracks)
        if candidate is None:
            return self._status(
                state=TrackingState.INITIALIZING,
                frame_id=frame_id,
                captured_at_s=now_s,
                produced_at_s=now_s,
                target_id=f"pending:{command.command_id}",
                bbox=selection_bbox,
                tracking_quality=0.0,
            )
        self._active_track_id = candidate.track_id
        self._last_bbox = candidate.bbox
        return self._tracking_status(candidate, frame_id=frame_id, now_s=now_s)

    def hint_bbox(self, bbox: BoundingBox, *, now_s: float) -> None:
        """Move the detector reacquisition window using a visual-tracker estimate."""
        self._validate_update(frame_id="bbox-hint", now_s=now_s)
        if self._command_id is None or self._active_track_id is not None:
            return
        self._selection_bbox = bbox
        self._last_bbox = bbox
        self._acquisition_deadline_s = now_s + self.config.reacquisition_timeout_s

    def update(
        self,
        *,
        tracks: tuple[TrackSnapshot, ...],
        frame_id: str,
        captured_at_s: float,
        produced_at_s: float,
    ) -> TrackStatusMessage | None:
        self._validate_update(frame_id=frame_id, now_s=produced_at_s)
        if captured_at_s > produced_at_s:
            raise ValueError("target-lock source frame cannot postdate its status")
        if self._command_id is None:
            return None
        if self._active_track_id is None:
            if self._selection_bbox is None:
                return None
            candidate = self._associate(self._selection_bbox, tracks)
            if candidate is not None:
                self._active_track_id = candidate.track_id
                self._last_bbox = candidate.bbox
                return self._tracking_status(
                    candidate,
                    frame_id=frame_id,
                    now_s=produced_at_s,
                    captured_at_s=captured_at_s,
                )
            acquisition_deadline_s = self._acquisition_deadline_s
            if acquisition_deadline_s is None:
                raise RuntimeError("target-lock acquisition deadline is not initialized")
            if produced_at_s <= acquisition_deadline_s:
                return self._status(
                    state=TrackingState.INITIALIZING,
                    frame_id=frame_id,
                    captured_at_s=captured_at_s,
                    produced_at_s=produced_at_s,
                    target_id=f"pending:{self._command_id}",
                    bbox=self._selection_bbox,
                    tracking_quality=0.0,
                )
            status = self._status(
                state=TrackingState.REJECTED,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
            )
            self._selection_bbox = None
            self._acquisition_deadline_s = None
            self._last_bbox = None
            return status

        active = next((track for track in tracks if track.track_id == self._active_track_id), None)
        if active is None or produced_at_s - active.last_seen_at_s > self.config.lost_after_s:
            reacquisition_bbox = self._last_bbox
            status = self._status(
                state=TrackingState.LOST,
                frame_id=frame_id,
                captured_at_s=captured_at_s,
                produced_at_s=produced_at_s,
                target_id=self._active_track_id,
            )
            self._active_track_id = None
            self._selection_bbox = reacquisition_bbox
            self._acquisition_deadline_s = produced_at_s + self.config.reacquisition_timeout_s
            return status
        self._last_bbox = active.bbox
        return self._tracking_status(
            active,
            frame_id=frame_id,
            now_s=produced_at_s,
            captured_at_s=captured_at_s,
        )

    def _associate(
        self,
        selection: BoundingBox,
        tracks: tuple[TrackSnapshot, ...],
    ) -> TrackSnapshot | None:
        candidates = []
        sx, sy = selection.center
        for track in tracks:
            if track.label.strip().lower() not in self.config.allowed_labels:
                continue
            tx, ty = track.bbox.center
            overlap = selection.iou(track.bbox)
            center_inside = (
                selection.x1 <= tx <= selection.x2 and selection.y1 <= ty <= selection.y2
            )
            if overlap <= 0.0 and not center_inside:
                continue
            candidates.append(
                (
                    -overlap,
                    -int(center_inside),
                    hypot(tx - sx, ty - sy),
                    -int(track.confirmed),
                    -track.confidence_mean,
                    track.track_id,
                    track,
                )
            )
        return min(candidates)[-1] if candidates else None

    def _tracking_status(
        self,
        track: TrackSnapshot,
        *,
        frame_id: str,
        now_s: float,
        captured_at_s: float | None = None,
    ) -> TrackStatusMessage:
        age_s = max(0.0, now_s - track.last_seen_at_s)
        freshness = max(0.0, 1.0 - age_s / self.config.lost_after_s)
        continuity = min(1.0, track.consecutive_observations / 5.0)
        quality = max(
            0.0,
            min(1.0, track.confidence_mean * freshness * (0.5 + 0.5 * continuity)),
        )
        return self._status(
            state=TrackingState.TRACKING,
            frame_id=frame_id,
            captured_at_s=track.last_seen_at_s if captured_at_s is None else captured_at_s,
            produced_at_s=now_s,
            target_id=track.track_id,
            bbox=track.bbox,
            label=track.label,
            confidence=track.confidence_mean,
            tracking_quality=quality,
        )

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
        confidence: float | None = None,
        tracking_quality: float | None = None,
    ) -> TrackStatusMessage:
        command_id = self._command_id
        if command_id is None:
            raise RuntimeError("cannot emit target-lock status without a selection command")
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
            confidence=confidence,
            tracking_quality=tracking_quality,
            source_frame_id=frame_id,
            source_captured_at_s=captured_at_s,
            produced_at_s=produced_at_s,
        )

    @staticmethod
    def _validate_update(*, frame_id: str, now_s: float) -> None:
        if not frame_id.strip():
            raise ValueError("target-lock frame_id cannot be empty")
        if not isfinite(now_s) or now_s < 0.0:
            raise ValueError("target-lock timestamp must be finite and non-negative")


__all__ = ["FIRE_CANDIDATE_TRACK_LABELS", "OperatorTargetLock", "TargetLockConfig"]
