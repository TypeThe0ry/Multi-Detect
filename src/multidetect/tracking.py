from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .config import MissionConfig
from .domain import BoundingBox, Detection, FrameObservation, TrackSnapshot


class FrameOrderError(ValueError):
    """Raised when a frame identifier or timestamp is not strictly ordered."""


@dataclass(slots=True)
class _TrackState:
    track_id: str
    label: str
    bbox: BoundingBox
    first_seen_at_s: float
    last_seen_at_s: float
    first_area: float
    observation_count: int
    consecutive_observations: int
    confidence_floor: float
    confidence_total: float
    maximum_gap_s: float
    thermal_corroborated: bool
    revision: int

    @classmethod
    def from_detection(
        cls,
        track_id: str,
        detection: Detection,
        captured_at_s: float,
    ) -> _TrackState:
        return cls(
            track_id=track_id,
            label=detection.label,
            bbox=detection.bbox,
            first_seen_at_s=captured_at_s,
            last_seen_at_s=captured_at_s,
            first_area=detection.bbox.area,
            observation_count=1,
            consecutive_observations=1,
            confidence_floor=detection.confidence,
            confidence_total=detection.confidence,
            maximum_gap_s=0.0,
            thermal_corroborated=bool(detection.metadata.get("thermal_corroborated", False)),
            revision=1,
        )

    def observe(self, detection: Detection, captured_at_s: float) -> None:
        gap_s = captured_at_s - self.last_seen_at_s
        self.bbox = detection.bbox
        self.last_seen_at_s = captured_at_s
        self.observation_count += 1
        self.consecutive_observations += 1
        self.confidence_floor = min(self.confidence_floor, detection.confidence)
        self.confidence_total += detection.confidence
        self.maximum_gap_s = max(self.maximum_gap_s, gap_s)
        # Corroboration is evidence from the latest matched observation, not a
        # sticky historical capability. Losing current thermal agreement must
        # fail closed on the next safety evaluation.
        self.thermal_corroborated = bool(detection.metadata.get("thermal_corroborated", False))
        self.revision += 1

    def snapshot(self, config: MissionConfig) -> TrackSnapshot:
        duration_s = max(0.0, self.last_seen_at_s - self.first_seen_at_s)
        thermal_ok = self.thermal_corroborated or not config.require_thermal_corroboration
        confirmed = all(
            (
                self.label in config.target_classes,
                self.consecutive_observations >= config.minimum_track_observations,
                duration_s >= config.minimum_track_time_seconds,
                self.confidence_floor >= config.minimum_confidence,
                self.maximum_gap_s <= config.maximum_track_gap_seconds,
                thermal_ok,
            )
        )
        area_growth_rate = (
            (self.bbox.area - self.first_area) / duration_s if duration_s > 0 else 0.0
        )
        return TrackSnapshot(
            track_id=self.track_id,
            revision=self.revision,
            label=self.label,
            bbox=self.bbox,
            first_seen_at_s=self.first_seen_at_s,
            last_seen_at_s=self.last_seen_at_s,
            observation_count=self.observation_count,
            consecutive_observations=self.consecutive_observations,
            confidence_floor=self.confidence_floor,
            confidence_mean=self.confidence_total / self.observation_count,
            maximum_gap_s=self.maximum_gap_s,
            area_growth_rate=area_growth_rate,
            thermal_corroborated=self.thermal_corroborated,
            confirmed=confirmed,
        )


class IoUMultiObjectTracker:
    """Small deterministic IoU tracker with fail-closed confirmation rules."""

    def __init__(self, config: MissionConfig, *, iou_threshold: float = 0.3) -> None:
        if not 0.0 < iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be in (0, 1]")
        self._config = config
        self._iou_threshold = iou_threshold
        self._tracks: dict[str, _TrackState] = {}
        self._seen_frame_ids: set[str] = set()
        self._last_frame_time_s: float | None = None
        self._next_track_number = 1

    def update(self, observation: FrameObservation) -> tuple[TrackSnapshot, ...]:
        """Process one strictly ordered frame and return all active tracks."""

        return self.update_detections(
            frame_id=observation.frame_id,
            captured_at_s=observation.captured_at_s,
            detections=observation.detections,
        )

    def update_detections(
        self,
        *,
        frame_id: str,
        captured_at_s: float,
        detections: Sequence[Detection],
    ) -> tuple[TrackSnapshot, ...]:
        if not frame_id:
            raise ValueError("frame_id cannot be empty")
        if captured_at_s < 0:
            raise ValueError("captured_at_s cannot be negative")
        if frame_id in self._seen_frame_ids:
            raise FrameOrderError(f"duplicate frame_id: {frame_id}")
        if self._last_frame_time_s is not None and captured_at_s <= self._last_frame_time_s:
            raise FrameOrderError("frame timestamps must be strictly increasing")

        self._seen_frame_ids.add(frame_id)
        self._last_frame_time_s = captured_at_s

        # A gap beyond the configured limit ends identity continuity before any
        # matching occurs; a later detection therefore receives a new track ID.
        expired = [
            track_id
            for track_id, track in self._tracks.items()
            if captured_at_s - track.last_seen_at_s > self._config.maximum_track_gap_seconds
        ]
        for track_id in expired:
            del self._tracks[track_id]

        candidates: list[tuple[float, str, int]] = []
        for track_id, track in self._tracks.items():
            for detection_index, detection in enumerate(detections):
                if track.label != detection.label:
                    continue
                overlap = track.bbox.iou(detection.bbox)
                if overlap >= self._iou_threshold:
                    candidates.append((overlap, track_id, detection_index))

        matched_tracks: set[str] = set()
        matched_detections: set[int] = set()
        for _overlap, track_id, detection_index in sorted(
            candidates,
            key=lambda item: (-item[0], item[1], item[2]),
        ):
            if track_id in matched_tracks or detection_index in matched_detections:
                continue
            self._tracks[track_id].observe(detections[detection_index], captured_at_s)
            matched_tracks.add(track_id)
            matched_detections.add(detection_index)

        for track_id, track in self._tracks.items():
            if track_id not in matched_tracks:
                track.consecutive_observations = 0

        for detection_index, detection in enumerate(detections):
            if detection_index in matched_detections:
                continue
            track_id = f"track-{self._next_track_number:06d}"
            self._next_track_number += 1
            self._tracks[track_id] = _TrackState.from_detection(
                track_id,
                detection,
                captured_at_s,
            )

        return self.active_snapshots()

    def active_snapshots(self) -> tuple[TrackSnapshot, ...]:
        return tuple(
            self._tracks[track_id].snapshot(self._config) for track_id in sorted(self._tracks)
        )


# Short alias for callers that do not need the implementation detail in the name.
IoUTracker = IoUMultiObjectTracker
