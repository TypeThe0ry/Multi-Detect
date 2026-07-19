from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any

from .assignment import rectangular_linear_assignment
from .domain import BoundingBox
from .evidence_session import normalize_evidence_session_id
from .unified_tracking import UnifiedTrackSnapshot


class GroundTruthVisibility(str, Enum):
    VISIBLE = "visible"
    OCCLUDED = "occluded"
    OUT_OF_FRAME = "out_of_frame"


_VISIBLE_TRACK_STATES = frozenset({"detected", "locked", "tracking", "recovered"})
_KNOWN_TRACK_STATES = _VISIBLE_TRACK_STATES | frozenset({"occluded", "reacquiring", "lost"})


@dataclass(frozen=True, slots=True)
class IdentityGroundTruthObject:
    identity_id: str
    label: str
    visibility: GroundTruthVisibility
    bbox: BoundingBox | None

    def __post_init__(self) -> None:
        if not self.identity_id.strip():
            raise ValueError("ground-truth identity_id cannot be empty")
        if not self.label.strip():
            raise ValueError("ground-truth label cannot be empty")
        if self.visibility is GroundTruthVisibility.VISIBLE and self.bbox is None:
            raise ValueError("visible ground-truth identity requires a bbox")
        if self.visibility is not GroundTruthVisibility.VISIBLE and self.bbox is not None:
            raise ValueError("non-visible ground-truth identity bbox must be null")


@dataclass(frozen=True, slots=True)
class IdentityGroundTruthFrame:
    frame_id: str
    captured_at_s: float
    objects: tuple[IdentityGroundTruthObject, ...]

    def __post_init__(self) -> None:
        _validate_frame_fields(self.frame_id, self.captured_at_s)
        _require_unique((item.identity_id for item in self.objects), "ground-truth identity_id")


@dataclass(frozen=True, slots=True)
class PredictedTrack:
    track_id: str
    label: str
    bbox: BoundingBox
    state: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.track_id.strip():
            raise ValueError("prediction track_id cannot be empty")
        if not self.label.strip():
            raise ValueError("prediction label cannot be empty")
        if self.state not in _KNOWN_TRACK_STATES:
            raise ValueError(f"invalid prediction track state: {self.state}")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("prediction confidence must be finite and in [0, 1]")

    @property
    def visible(self) -> bool:
        return self.state in _VISIBLE_TRACK_STATES


@dataclass(frozen=True, slots=True)
class IdentityPredictionFrame:
    frame_id: str
    captured_at_s: float
    tracks: tuple[PredictedTrack, ...]
    session_id: str | None = None

    def __post_init__(self) -> None:
        _validate_frame_fields(self.frame_id, self.captured_at_s)
        _require_unique((item.track_id for item in self.tracks), "prediction track_id")
        if self.session_id is not None:
            object.__setattr__(
                self,
                "session_id",
                normalize_evidence_session_id(self.session_id),
            )


class JsonlIdentityPredictionWriter:
    """Stream frame-aligned target-pool metadata for offline identity evaluation."""

    def __init__(self, path: str | Path, *, session_id: str | None = None) -> None:
        self.path = Path(path)
        self.session_id = (
            normalize_evidence_session_id(session_id) if session_id is not None else None
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8", newline="\n")
        self._lock = RLock()
        self._closed = False

    def append(
        self,
        *,
        frame_id: str,
        captured_at_s: float,
        tracks: tuple[UnifiedTrackSnapshot, ...],
    ) -> None:
        _validate_frame_fields(frame_id, captured_at_s)
        _require_unique((track.track_id for track in tracks), "prediction track_id")
        document = {
            "frame_id": frame_id,
            "captured_at_s": captured_at_s,
            "tracks": [
                {
                    "track_id": track.track_id,
                    "label": _canonical_label(track.label),
                    "bbox": (
                        track.bbox.rounded()
                        if track.state.value in _VISIBLE_TRACK_STATES
                        else track.predicted_bbox.rounded()
                    ),
                    "state": track.state.value,
                    "confidence": track.confidence,
                }
                for track in tracks
            ],
        }
        if self.session_id is not None:
            document["session_id"] = self.session_id
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("identity prediction writer is closed")
            self._handle.write(encoded)
            self._handle.write("\n")
            self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()


@dataclass(frozen=True, slots=True)
class IdentityMetrics:
    label: str
    ground_truth_detection_count: int
    predicted_detection_count: int
    frame_match_count: int
    id_true_positive_count: int
    id_false_positive_count: int
    id_false_negative_count: int
    id_precision: float | None
    id_recall: float | None
    idf1: float | None
    id_switch_count: int
    fragmentation_count: int
    mota: float | None
    matched_iou_mean: float | None
    matched_iou_p50: float | None
    matched_iou_p95: float | None


@dataclass(frozen=True, slots=True)
class RecoveryMetrics:
    kind: str
    maximum_recovery_s: float
    annotated_event_count: int
    eligible_event_count: int
    recovered_event_count: int
    failed_event_count: int
    recovery_rate: float | None
    recovery_latency_p50_s: float | None
    recovery_latency_p95_s: float | None


@dataclass(frozen=True, slots=True)
class IdentityTrackingEvaluationReport:
    frame_count: int
    duration_s: float
    iou_threshold: float
    confidence_threshold: float
    maximum_timestamp_delta_s: float
    overall: IdentityMetrics
    per_class: tuple[IdentityMetrics, ...]
    occlusion_recovery: RecoveryMetrics
    out_of_frame_recovery: RecoveryMetrics
    input_is_identity_annotated: bool = True
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False


@dataclass(frozen=True, slots=True)
class _FrameMatch:
    frame_id: str
    ground_truth: IdentityGroundTruthObject
    prediction: PredictedTrack
    iou: float


def load_identity_ground_truth_jsonl(
    path: str | Path,
) -> tuple[IdentityGroundTruthFrame, ...]:
    frames: list[IdentityGroundTruthFrame] = []
    for record in _load_jsonl(path):
        _reject_unknown_keys(record, {"frame_id", "captured_at_s", "objects"}, "ground-truth frame")
        raw_objects = record.get("objects")
        if not isinstance(raw_objects, list):
            raise ValueError("ground-truth objects must be an array")
        objects: list[IdentityGroundTruthObject] = []
        for raw in raw_objects:
            if not isinstance(raw, dict):
                raise ValueError("ground-truth object entries must be objects")
            _reject_unknown_keys(
                raw,
                {"identity_id", "label", "visibility", "bbox"},
                "ground-truth object",
            )
            try:
                visibility = GroundTruthVisibility(_required_text(raw, "visibility").lower())
            except ValueError as exc:
                raise ValueError("invalid ground-truth visibility") from exc
            objects.append(
                IdentityGroundTruthObject(
                    identity_id=_required_text(raw, "identity_id"),
                    label=_canonical_label(_required_text(raw, "label")),
                    visibility=visibility,
                    bbox=_optional_bbox(raw.get("bbox")),
                )
            )
        _require_unique((item.identity_id for item in objects), "ground-truth identity_id")
        frames.append(
            IdentityGroundTruthFrame(
                frame_id=_required_text(record, "frame_id"),
                captured_at_s=_finite_nonnegative(record.get("captured_at_s"), "captured_at_s"),
                objects=tuple(objects),
            )
        )
    _validate_frame_sequence(frames)
    _validate_identity_annotations(tuple(frames))
    return tuple(frames)


def load_identity_prediction_jsonl(path: str | Path) -> tuple[IdentityPredictionFrame, ...]:
    frames: list[IdentityPredictionFrame] = []
    labels_by_track: dict[str, str] = {}
    for record in _load_jsonl(path):
        _reject_unknown_keys(
            record,
            {"frame_id", "captured_at_s", "tracks", "session_id"},
            "prediction frame",
        )
        raw_tracks = record.get("tracks")
        if not isinstance(raw_tracks, list):
            raise ValueError("prediction tracks must be an array")
        tracks: list[PredictedTrack] = []
        for raw in raw_tracks:
            if not isinstance(raw, dict):
                raise ValueError("prediction track entries must be objects")
            _reject_unknown_keys(
                raw,
                {"track_id", "label", "bbox", "state", "confidence"},
                "prediction track",
            )
            track_id = _required_text(raw, "track_id")
            label = _canonical_label(_required_text(raw, "label"))
            previous_label = labels_by_track.setdefault(track_id, label)
            if previous_label != label:
                raise ValueError(f"prediction track label changed for {track_id}")
            state = _required_text(raw, "state").lower()
            if state not in _KNOWN_TRACK_STATES:
                raise ValueError(f"invalid prediction track state: {state}")
            bbox = _optional_bbox(raw.get("bbox"))
            if bbox is None:
                raise ValueError("prediction track bbox must be a four-number array")
            tracks.append(
                PredictedTrack(
                    track_id=track_id,
                    label=label,
                    bbox=bbox,
                    state=state,
                    confidence=_finite_probability(raw.get("confidence"), "confidence"),
                )
            )
        _require_unique((item.track_id for item in tracks), "prediction track_id")
        frames.append(
            IdentityPredictionFrame(
                frame_id=_required_text(record, "frame_id"),
                captured_at_s=_finite_nonnegative(record.get("captured_at_s"), "captured_at_s"),
                tracks=tuple(tracks),
                session_id=(
                    normalize_evidence_session_id(record["session_id"])
                    if "session_id" in record
                    else None
                ),
            )
        )
    _validate_frame_sequence(frames)
    _validate_prediction_session(frames)
    return tuple(frames)


def evaluate_identity_tracking(
    ground_truth: tuple[IdentityGroundTruthFrame, ...],
    predictions: tuple[IdentityPredictionFrame, ...],
    *,
    iou_threshold: float = 0.5,
    confidence_threshold: float = 0.1,
    maximum_timestamp_delta_s: float = 0.05,
    maximum_occlusion_recovery_s: float = 0.5,
    maximum_out_of_frame_recovery_s: float = 2.0,
) -> IdentityTrackingEvaluationReport:
    if not ground_truth or not predictions:
        raise ValueError("tracking evaluation inputs cannot be empty")
    _validate_frame_sequence(list(ground_truth))
    _validate_identity_annotations(ground_truth)
    _validate_frame_sequence(list(predictions))
    _validate_prediction_annotations(predictions)
    for name, value in (
        ("iou_threshold", iou_threshold),
        ("confidence_threshold", confidence_threshold),
    ):
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be finite and in [0, 1]")
    if iou_threshold <= 0.0:
        raise ValueError("iou_threshold must be positive")
    for name, value in (
        ("maximum_timestamp_delta_s", maximum_timestamp_delta_s),
        ("maximum_occlusion_recovery_s", maximum_occlusion_recovery_s),
        ("maximum_out_of_frame_recovery_s", maximum_out_of_frame_recovery_s),
    ):
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and positive")

    truth_by_id = {frame.frame_id: frame for frame in ground_truth}
    prediction_by_id = {frame.frame_id: frame for frame in predictions}
    if set(truth_by_id) != set(prediction_by_id):
        missing = sorted(set(truth_by_id) - set(prediction_by_id))
        extra = sorted(set(prediction_by_id) - set(truth_by_id))
        raise ValueError(f"tracking frame IDs differ; missing={missing}, extra={extra}")
    ordered_truth = tuple(sorted(ground_truth, key=lambda frame: frame.captured_at_s))
    ordered_predictions: list[IdentityPredictionFrame] = []
    for frame in ordered_truth:
        prediction = prediction_by_id[frame.frame_id]
        if abs(frame.captured_at_s - prediction.captured_at_s) > maximum_timestamp_delta_s:
            raise ValueError(f"tracking timestamp misalignment for frame {frame.frame_id}")
        ordered_predictions.append(prediction)

    matches: list[_FrameMatch] = []
    for truth_frame, prediction_frame in zip(ordered_truth, ordered_predictions, strict=True):
        matches.extend(
            _match_frame(
                truth_frame,
                prediction_frame,
                iou_threshold=iou_threshold,
                confidence_threshold=confidence_threshold,
            )
        )
    labels = sorted(
        {item.label for frame in ordered_truth for item in frame.objects}
        | {item.label for frame in ordered_predictions for item in frame.tracks}
    )
    overall = _identity_metrics(
        "__overall__",
        ordered_truth,
        tuple(ordered_predictions),
        tuple(matches),
        confidence_threshold=confidence_threshold,
        label=None,
    )
    per_class = tuple(
        _identity_metrics(
            label,
            ordered_truth,
            tuple(ordered_predictions),
            tuple(matches),
            confidence_threshold=confidence_threshold,
            label=label,
        )
        for label in labels
    )
    return IdentityTrackingEvaluationReport(
        frame_count=len(ordered_truth),
        duration_s=(
            ordered_truth[-1].captured_at_s - ordered_truth[0].captured_at_s
            if ordered_truth
            else 0.0
        ),
        iou_threshold=iou_threshold,
        confidence_threshold=confidence_threshold,
        maximum_timestamp_delta_s=maximum_timestamp_delta_s,
        overall=overall,
        per_class=per_class,
        occlusion_recovery=_recovery_metrics(
            GroundTruthVisibility.OCCLUDED,
            ordered_truth,
            tuple(matches),
            maximum_recovery_s=maximum_occlusion_recovery_s,
        ),
        out_of_frame_recovery=_recovery_metrics(
            GroundTruthVisibility.OUT_OF_FRAME,
            ordered_truth,
            tuple(matches),
            maximum_recovery_s=maximum_out_of_frame_recovery_s,
        ),
    )


def tracking_evaluation_document(report: IdentityTrackingEvaluationReport) -> dict[str, Any]:
    return {
        "frame_count": report.frame_count,
        "duration_s": report.duration_s,
        "iou_threshold": report.iou_threshold,
        "confidence_threshold": report.confidence_threshold,
        "maximum_timestamp_delta_s": report.maximum_timestamp_delta_s,
        "overall": _identity_metrics_document(report.overall),
        "per_class": [_identity_metrics_document(item) for item in report.per_class],
        "occlusion_recovery": _recovery_metrics_document(report.occlusion_recovery),
        "out_of_frame_recovery": _recovery_metrics_document(report.out_of_frame_recovery),
        "input_is_identity_annotated": report.input_is_identity_annotated,
        "flight_control_enabled": report.flight_control_enabled,
        "physical_release_enabled": report.physical_release_enabled,
    }


def _match_frame(
    ground_truth: IdentityGroundTruthFrame,
    prediction: IdentityPredictionFrame,
    *,
    iou_threshold: float,
    confidence_threshold: float,
) -> tuple[_FrameMatch, ...]:
    matches: list[_FrameMatch] = []
    labels = {item.label for item in ground_truth.objects} | {
        item.label for item in prediction.tracks
    }
    for label in sorted(labels):
        truth = tuple(
            item
            for item in ground_truth.objects
            if item.label == label and item.visibility is GroundTruthVisibility.VISIBLE
        )
        predicted = tuple(
            item
            for item in prediction.tracks
            if item.label == label and item.visible and item.confidence >= confidence_threshold
        )
        if not truth or not predicted:
            continue
        invalid_penalty = 1_000_000.0
        dummy_penalty = 2.0
        costs: list[list[float]] = []
        for truth_item in truth:
            row = []
            for predicted_item in predicted:
                overlap = truth_item.bbox.iou(predicted_item.bbox)  # type: ignore[union-attr]
                row.append(1.0 - overlap if overlap >= iou_threshold else invalid_penalty)
            row.extend(dummy_penalty for _ in truth)
            costs.append(row)
        for truth_index, column_index in enumerate(rectangular_linear_assignment(costs)):
            if column_index < 0 or column_index >= len(predicted):
                continue
            overlap = truth[truth_index].bbox.iou(predicted[column_index].bbox)  # type: ignore[union-attr]
            if overlap < iou_threshold:
                continue
            matches.append(
                _FrameMatch(
                    frame_id=ground_truth.frame_id,
                    ground_truth=truth[truth_index],
                    prediction=predicted[column_index],
                    iou=overlap,
                )
            )
    return tuple(matches)


def _identity_metrics(
    metric_label: str,
    ground_truth: tuple[IdentityGroundTruthFrame, ...],
    predictions: tuple[IdentityPredictionFrame, ...],
    matches: tuple[_FrameMatch, ...],
    *,
    confidence_threshold: float,
    label: str | None,
) -> IdentityMetrics:
    filtered_matches = tuple(
        item for item in matches if label is None or item.ground_truth.label == label
    )
    truth_count = sum(
        item.visibility is GroundTruthVisibility.VISIBLE and (label is None or item.label == label)
        for frame in ground_truth
        for item in frame.objects
    )
    prediction_count = sum(
        item.visible
        and item.confidence >= confidence_threshold
        and (label is None or item.label == label)
        for frame in predictions
        for item in frame.tracks
    )
    pair_counts = Counter(
        (item.ground_truth.identity_id, item.prediction.track_id) for item in filtered_matches
    )
    id_true_positive = _maximum_identity_true_positives(pair_counts)
    id_false_positive = prediction_count - id_true_positive
    id_false_negative = truth_count - id_true_positive
    id_precision = _ratio(id_true_positive, id_true_positive + id_false_positive)
    id_recall = _ratio(id_true_positive, id_true_positive + id_false_negative)
    idf1 = _ratio(
        2 * id_true_positive,
        2 * id_true_positive + id_false_positive + id_false_negative,
    )
    id_switches, fragmentations = _identity_continuity_counts(
        ground_truth,
        filtered_matches,
        label=label,
    )
    false_positives = prediction_count - len(filtered_matches)
    false_negatives = truth_count - len(filtered_matches)
    mota = (
        1.0 - (false_positives + false_negatives + id_switches) / truth_count
        if truth_count
        else None
    )
    overlaps = [item.iou for item in filtered_matches]
    return IdentityMetrics(
        label=metric_label,
        ground_truth_detection_count=truth_count,
        predicted_detection_count=prediction_count,
        frame_match_count=len(filtered_matches),
        id_true_positive_count=id_true_positive,
        id_false_positive_count=id_false_positive,
        id_false_negative_count=id_false_negative,
        id_precision=id_precision,
        id_recall=id_recall,
        idf1=idf1,
        id_switch_count=id_switches,
        fragmentation_count=fragmentations,
        mota=mota,
        matched_iou_mean=(sum(overlaps) / len(overlaps) if overlaps else None),
        matched_iou_p50=_percentile(overlaps, 0.50),
        matched_iou_p95=_percentile(overlaps, 0.95),
    )


def _maximum_identity_true_positives(pair_counts: Counter[tuple[str, str]]) -> int:
    if not pair_counts:
        return 0
    truth_ids = sorted({pair[0] for pair in pair_counts})
    track_ids = sorted({pair[1] for pair in pair_counts})
    costs = [
        [-float(pair_counts[(truth_id, track_id)]) for track_id in track_ids]
        + [0.0] * len(truth_ids)
        for truth_id in truth_ids
    ]
    assignment = rectangular_linear_assignment(costs)
    return sum(
        pair_counts[(truth_ids[row], track_ids[column])]
        for row, column in enumerate(assignment)
        if 0 <= column < len(track_ids)
    )


def _identity_continuity_counts(
    ground_truth: tuple[IdentityGroundTruthFrame, ...],
    matches: tuple[_FrameMatch, ...],
    *,
    label: str | None,
) -> tuple[int, int]:
    match_by_frame_identity = {
        (item.frame_id, item.ground_truth.identity_id): item.prediction.track_id for item in matches
    }
    last_track: dict[str, str] = {}
    unmatched_since_match: set[str] = set()
    switches = 0
    fragmentations = 0
    for frame in ground_truth:
        for item in frame.objects:
            if item.visibility is not GroundTruthVisibility.VISIBLE:
                continue
            if label is not None and item.label != label:
                continue
            track_id = match_by_frame_identity.get((frame.frame_id, item.identity_id))
            if track_id is None:
                if item.identity_id in last_track:
                    unmatched_since_match.add(item.identity_id)
                continue
            previous = last_track.get(item.identity_id)
            if previous is not None and previous != track_id:
                switches += 1
            if item.identity_id in unmatched_since_match:
                fragmentations += 1
                unmatched_since_match.remove(item.identity_id)
            last_track[item.identity_id] = track_id
    return switches, fragmentations


def _recovery_metrics(
    kind: GroundTruthVisibility,
    frames: tuple[IdentityGroundTruthFrame, ...],
    matches: tuple[_FrameMatch, ...],
    *,
    maximum_recovery_s: float,
) -> RecoveryMetrics:
    match_by_frame_identity = {
        (item.frame_id, item.ground_truth.identity_id): item.prediction.track_id for item in matches
    }
    objects_by_identity: dict[str, list[tuple[int, IdentityGroundTruthObject]]] = {}
    for frame_index, frame in enumerate(frames):
        for item in frame.objects:
            objects_by_identity.setdefault(item.identity_id, []).append((frame_index, item))

    annotated = 0
    eligible = 0
    recovered = 0
    latencies: list[float] = []
    for identity_id, timeline in objects_by_identity.items():
        index = 1
        while index < len(timeline):
            if (
                timeline[index - 1][1].visibility is not GroundTruthVisibility.VISIBLE
                or timeline[index][1].visibility is GroundTruthVisibility.VISIBLE
            ):
                index += 1
                continue
            gap_start = index
            gap_states: set[GroundTruthVisibility] = set()
            while (
                index < len(timeline)
                and timeline[index][1].visibility is not GroundTruthVisibility.VISIBLE
            ):
                gap_states.add(timeline[index][1].visibility)
                index += 1
            if index >= len(timeline):
                continue
            event_kind = (
                GroundTruthVisibility.OCCLUDED
                if GroundTruthVisibility.OCCLUDED in gap_states
                else GroundTruthVisibility.OUT_OF_FRAME
            )
            if event_kind is not kind:
                continue
            annotated += 1
            previous_frame = frames[timeline[gap_start - 1][0]]
            previous_track = match_by_frame_identity.get((previous_frame.frame_id, identity_id))
            if previous_track is None:
                continue
            eligible += 1
            reappearance_index = index
            reappearance_frame_index = timeline[reappearance_index][0]
            reappearance_time = frames[reappearance_frame_index].captured_at_s
            recovered_same_identity = False
            scan = reappearance_index
            while (
                scan < len(timeline)
                and timeline[scan][1].visibility is GroundTruthVisibility.VISIBLE
            ):
                scan_frame = frames[timeline[scan][0]]
                latency = scan_frame.captured_at_s - reappearance_time
                if latency > maximum_recovery_s:
                    break
                current_track = match_by_frame_identity.get((scan_frame.frame_id, identity_id))
                if current_track is not None:
                    if current_track == previous_track:
                        recovered_same_identity = True
                        recovered += 1
                        latencies.append(latency)
                    break
                scan += 1
            if not recovered_same_identity:
                pass
    failed = eligible - recovered
    return RecoveryMetrics(
        kind=kind.value,
        maximum_recovery_s=maximum_recovery_s,
        annotated_event_count=annotated,
        eligible_event_count=eligible,
        recovered_event_count=recovered,
        failed_event_count=failed,
        recovery_rate=_ratio(recovered, eligible),
        recovery_latency_p50_s=_percentile(latencies, 0.50),
        recovery_latency_p95_s=_percentile(latencies, 0.95),
    )


def _validate_identity_annotations(frames: tuple[IdentityGroundTruthFrame, ...]) -> None:
    positions: dict[str, list[int]] = {}
    labels: dict[str, str] = {}
    for frame_index, frame in enumerate(frames):
        for item in frame.objects:
            positions.setdefault(item.identity_id, []).append(frame_index)
            previous = labels.setdefault(item.identity_id, item.label)
            if previous != item.label:
                raise ValueError(f"ground-truth identity label changed for {item.identity_id}")
    for identity_id, indices in positions.items():
        expected = list(range(indices[0], indices[-1] + 1))
        if indices != expected:
            raise ValueError(
                "ground-truth identity timeline must explicitly annotate every frame: "
                f"{identity_id}"
            )


def _validate_prediction_annotations(frames: tuple[IdentityPredictionFrame, ...]) -> None:
    _validate_prediction_session(list(frames))
    labels: dict[str, str] = {}
    for frame in frames:
        for item in frame.tracks:
            previous = labels.setdefault(item.track_id, item.label)
            if previous != item.label:
                raise ValueError(f"prediction track label changed for {item.track_id}")


def _validate_prediction_session(frames: list[IdentityPredictionFrame]) -> None:
    session_ids = {frame.session_id for frame in frames}
    if len(session_ids) > 1:
        raise ValueError("identity prediction frames must use one stable evidence session ID")


def _validate_frame_sequence(frames: list[Any]) -> None:
    _require_unique((frame.frame_id for frame in frames), "tracking frame_id")
    previous = None
    for frame in frames:
        if previous is not None and frame.captured_at_s <= previous:
            raise ValueError("tracking frame timestamps must be strictly increasing")
        previous = frame.captured_at_s


def _validate_frame_fields(frame_id: str, captured_at_s: float) -> None:
    if not frame_id.strip():
        raise ValueError("tracking frame_id cannot be empty")
    if not math.isfinite(captured_at_s) or captured_at_s < 0.0:
        raise ValueError("tracking captured_at_s must be finite and non-negative")


def _identity_metrics_document(metrics: IdentityMetrics) -> dict[str, Any]:
    return {field: getattr(metrics, field) for field in metrics.__dataclass_fields__}


def _recovery_metrics_document(metrics: RecoveryMetrics) -> dict[str, Any]:
    return {field: getattr(metrics, field) for field in metrics.__dataclass_fields__}


def _load_jsonl(path: str | Path) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid tracking JSONL line {line_number}") from exc
            if not isinstance(raw, dict):
                raise ValueError(f"tracking JSONL line {line_number} must be an object")
            records.append(raw)
    if not records:
        raise ValueError("tracking evaluation JSONL cannot be empty")
    return tuple(records)


def _optional_bbox(value: object) -> BoundingBox | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("bbox must be null or a four-number array")
    return BoundingBox(*(float(component) for component in value))


def _required_text(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _finite_nonnegative(value: object, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _finite_probability(value: object, name: str) -> float:
    number = _finite_nonnegative(value, name)
    if number > 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return number


def _canonical_label(label: str) -> str:
    normalized = label.strip().lower()
    return "flame" if normalized == "fire" else normalized


def _reject_unknown_keys(mapping: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(f"unknown {context} fields: {unknown}")


def _require_unique(values: Any, name: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {name}: {value}")
        seen.add(value)


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


__all__ = [
    "GroundTruthVisibility",
    "IdentityGroundTruthFrame",
    "IdentityGroundTruthObject",
    "IdentityMetrics",
    "IdentityPredictionFrame",
    "IdentityTrackingEvaluationReport",
    "JsonlIdentityPredictionWriter",
    "PredictedTrack",
    "RecoveryMetrics",
    "evaluate_identity_tracking",
    "load_identity_ground_truth_jsonl",
    "load_identity_prediction_jsonl",
    "tracking_evaluation_document",
]
