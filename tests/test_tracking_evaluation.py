from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.domain import BoundingBox
from multidetect.tracking_evaluation import (
    GroundTruthVisibility,
    IdentityGroundTruthFrame,
    IdentityGroundTruthObject,
    IdentityPredictionFrame,
    JsonlIdentityPredictionWriter,
    PredictedTrack,
    evaluate_identity_tracking,
    load_identity_ground_truth_jsonl,
    load_identity_prediction_jsonl,
    tracking_evaluation_document,
)
from multidetect.unified_tracking import TargetObservation, UnifiedTargetPool

ROOT = Path(__file__).resolve().parents[1]
GROUND_TRUTH = ROOT / "examples/tracking_identity_ground_truth.demo.jsonl"
PREDICTIONS = ROOT / "examples/tracking_identity_predictions.demo.jsonl"
SESSION_ID = "12345678-1234-5678-9234-567812345678"


def test_identity_prediction_writer_emits_loader_compatible_target_pool_frames(
    tmp_path: Path,
) -> None:
    pool = UnifiedTargetPool()
    update = pool.update(
        frame_id="frame-1",
        captured_at_s=1.0,
        observations=(
            TargetObservation(
                label="fire",
                confidence=0.91,
                bbox=BoundingBox(0.1, 0.2, 0.3, 0.5),
            ),
        ),
    )
    path = tmp_path / "identity-tracks.jsonl"
    writer = JsonlIdentityPredictionWriter(path, session_id=SESSION_ID)
    writer.append(
        frame_id=update.frame_id,
        captured_at_s=update.captured_at_s,
        tracks=update.tracks,
    )
    writer.close()

    frames = load_identity_prediction_jsonl(path)
    assert len(frames) == 1
    assert frames[0].frame_id == "frame-1"
    assert frames[0].session_id == SESSION_ID
    assert len(frames[0].tracks) == 1
    assert frames[0].tracks[0].track_id == "target-000001"
    assert frames[0].tracks[0].label == "flame"
    assert frames[0].tracks[0].confidence == pytest.approx(0.91)
    with pytest.raises(RuntimeError, match="writer is closed"):
        writer.append(
            frame_id=update.frame_id,
            captured_at_s=update.captured_at_s,
            tracks=update.tracks,
        )


def test_identity_prediction_loader_rejects_mixed_evidence_sessions(tmp_path: Path) -> None:
    records = [json.loads(line) for line in PREDICTIONS.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["session_id"] = SESSION_ID
    records[-1]["session_id"] = "87654321-4321-6789-9234-567812345678"
    path = tmp_path / "mixed-sessions.jsonl"
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="one stable evidence session ID"):
        load_identity_prediction_jsonl(path)


def test_identity_tracking_evaluation_reports_perfect_demo_and_recovery() -> None:
    report = evaluate_identity_tracking(
        load_identity_ground_truth_jsonl(GROUND_TRUTH),
        load_identity_prediction_jsonl(PREDICTIONS),
    )

    assert report.frame_count == 6
    assert report.duration_s == pytest.approx(0.7)
    assert report.overall.ground_truth_detection_count == 10
    assert report.overall.predicted_detection_count == 10
    assert report.overall.frame_match_count == 10
    assert report.overall.idf1 == pytest.approx(1.0)
    assert report.overall.id_switch_count == 0
    assert report.overall.fragmentation_count == 0
    assert report.overall.mota == pytest.approx(1.0)
    assert report.overall.matched_iou_mean == pytest.approx(1.0)
    assert report.per_class[0].label == "vehicle"
    assert report.occlusion_recovery.eligible_event_count == 1
    assert report.occlusion_recovery.recovered_event_count == 1
    assert report.occlusion_recovery.recovery_rate == pytest.approx(1.0)
    assert report.occlusion_recovery.recovery_latency_p95_s == pytest.approx(0.0)
    assert report.out_of_frame_recovery.eligible_event_count == 1
    assert report.out_of_frame_recovery.recovered_event_count == 1
    assert report.out_of_frame_recovery.recovery_rate == pytest.approx(1.0)
    document = tracking_evaluation_document(report)
    assert document["input_is_identity_annotated"] is True
    assert document["flight_control_enabled"] is False
    assert document["physical_release_enabled"] is False


def test_identity_switch_reduces_idf1_and_fails_same_id_occlusion_recovery() -> None:
    truth = load_identity_ground_truth_jsonl(GROUND_TRUTH)
    predictions = list(load_identity_prediction_jsonl(PREDICTIONS))
    for frame_index in (3, 5):
        frame = predictions[frame_index]
        tracks = tuple(
            replace(track, track_id="target-000099") if track.track_id == "target-000001" else track
            for track in frame.tracks
        )
        predictions[frame_index] = replace(frame, tracks=tracks)

    report = evaluate_identity_tracking(truth, tuple(predictions))

    assert report.overall.id_true_positive_count == 8
    assert report.overall.id_false_positive_count == 2
    assert report.overall.id_false_negative_count == 2
    assert report.overall.idf1 == pytest.approx(0.8)
    assert report.overall.id_switch_count == 1
    assert report.occlusion_recovery.recovered_event_count == 0
    assert report.occlusion_recovery.failed_event_count == 1
    assert report.out_of_frame_recovery.recovered_event_count == 1


def test_identity_annotation_requires_explicit_timeline_and_null_hidden_bbox(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.jsonl"
    missing.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "frame_id": "f0",
                        "captured_at_s": 0.0,
                        "objects": [
                            {
                                "identity_id": "a",
                                "label": "person",
                                "visibility": "visible",
                                "bbox": [0.1, 0.1, 0.2, 0.3],
                            }
                        ],
                    }
                ),
                json.dumps({"frame_id": "f1", "captured_at_s": 0.1, "objects": []}),
                json.dumps(
                    {
                        "frame_id": "f2",
                        "captured_at_s": 0.2,
                        "objects": [
                            {
                                "identity_id": "a",
                                "label": "person",
                                "visibility": "visible",
                                "bbox": [0.2, 0.1, 0.3, 0.3],
                            }
                        ],
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicitly annotate every frame"):
        load_identity_ground_truth_jsonl(missing)

    invalid_bbox = tmp_path / "invalid-bbox.jsonl"
    invalid_bbox.write_text(
        json.dumps(
            {
                "frame_id": "f0",
                "captured_at_s": 0.0,
                "objects": [
                    {
                        "identity_id": "a",
                        "label": "person",
                        "visibility": "occluded",
                        "bbox": [0.1, 0.1, 0.2, 0.3],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="bbox must be null"):
        load_identity_ground_truth_jsonl(invalid_bbox)


def test_identity_evaluation_rejects_timestamp_misalignment() -> None:
    truth = load_identity_ground_truth_jsonl(GROUND_TRUTH)
    predictions = list(load_identity_prediction_jsonl(PREDICTIONS))
    predictions[0] = replace(predictions[0], captured_at_s=0.06)

    with pytest.raises(ValueError, match="timestamp misalignment"):
        evaluate_identity_tracking(truth, tuple(predictions))


def test_delayed_same_id_recovery_reports_latency_and_fragmentation() -> None:
    box = BoundingBox(0.1, 0.1, 0.2, 0.3)

    def truth(frame_id: str, captured_at_s: float, visibility: GroundTruthVisibility):
        return IdentityGroundTruthFrame(
            frame_id,
            captured_at_s,
            (
                IdentityGroundTruthObject(
                    "person-a",
                    "person",
                    visibility,
                    box if visibility is GroundTruthVisibility.VISIBLE else None,
                ),
            ),
        )

    def prediction(frame_id: str, captured_at_s: float, visible: bool):
        return IdentityPredictionFrame(
            frame_id,
            captured_at_s,
            ((PredictedTrack("target-a", "person", box, "tracking", 0.9),) if visible else ()),
        )

    report = evaluate_identity_tracking(
        (
            truth("f0", 0.0, GroundTruthVisibility.VISIBLE),
            truth("f1", 0.1, GroundTruthVisibility.OCCLUDED),
            truth("f2", 0.2, GroundTruthVisibility.VISIBLE),
            truth("f3", 0.4, GroundTruthVisibility.VISIBLE),
        ),
        (
            prediction("f0", 0.0, True),
            prediction("f1", 0.1, False),
            prediction("f2", 0.2, False),
            prediction("f3", 0.4, True),
        ),
    )

    assert report.overall.idf1 == pytest.approx(0.8)
    assert report.overall.fragmentation_count == 1
    assert report.occlusion_recovery.recovered_event_count == 1
    assert report.occlusion_recovery.recovery_latency_p50_s == pytest.approx(0.2)
    assert report.occlusion_recovery.recovery_latency_p95_s == pytest.approx(0.2)
