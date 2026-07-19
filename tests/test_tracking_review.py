from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.tracking_evaluation import load_identity_ground_truth_jsonl
from multidetect.tracking_review import prepare_tracking_review_bundle
from multidetect.video_evidence import VideoEvidenceProbe

ROOT = Path(__file__).resolve().parents[1]
PREDICTIONS = ROOT / "examples/tracking_identity_predictions.demo.jsonl"
SESSION_ID = "12345678-1234-5678-9234-567812345678"
OTHER_SESSION_ID = "87654321-4321-6789-9234-567812345678"


def _valid_probe(path: Path, *, frames: int = 12, duration_s: float = 1.0):
    return VideoEvidenceProbe(
        path=path,
        decoded_frame_count=frames,
        declared_frame_count=frames,
        fps=frames / duration_s,
        width=1280,
        height=720,
        duration_s=duration_s,
        full_frame_scan_completed=True,
        stable_dimensions=True,
        passed=True,
        failure_reasons=(),
    )


def _bound_inputs(
    tmp_path: Path,
    *,
    prediction_session_id: str = SESSION_ID,
    manifest_session_id: str = SESSION_ID,
    started_at_s: float = 0.0,
    ended_at_s: float = 1.0,
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    predictions = tmp_path / "identity-tracks.jsonl"
    records = [json.loads(line) for line in PREDICTIONS.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record["session_id"] = prediction_session_id
    predictions.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )
    video = tmp_path / "source.mkv"
    video.write_bytes(b"deterministic-video-placeholder")
    manifest_path = tmp_path / "source.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "event": "rtsp_tracking_evidence_recording_completed",
                "schema_version": 2,
                "session_id": manifest_session_id,
                "source_uri_recorded": False,
                "stream_copy_no_decode_or_reencode": True,
                "output_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                "output_bytes": video.stat().st_size,
                "started_at_monotonic_s": started_at_s,
                "ended_at_monotonic_s": ended_at_s,
                "passed": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return predictions, video, manifest_path


def test_tracking_review_bundle_binds_video_predictions_and_unreviewed_draft(
    tmp_path: Path,
) -> None:
    predictions, video, video_manifest = _bound_inputs(tmp_path)
    output = tmp_path / "review"

    report = prepare_tracking_review_bundle(
        predictions,
        video,
        video_manifest,
        output,
        video_probe=lambda path: _valid_probe(path),
    )

    manifest = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    records = [json.loads(line) for line in report.draft_path.read_text().splitlines()]
    assert report.frame_count == 6
    assert report.unique_candidate_track_count == 2
    assert report.candidate_observation_count == 12
    assert report.labels == ("vehicle",)
    assert manifest["evidence_session_id"] == SESSION_ID
    assert manifest["session_id_binding_validated"] is True
    assert manifest["monotonic_recording_window_validated"] is True
    assert manifest["source_video_sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert (
        manifest["source_video_manifest_sha256"]
        == hashlib.sha256(video_manifest.read_bytes()).hexdigest()
    )
    assert manifest["source_video_media_decoding_validated"] is True
    assert manifest["source_video_track_timeline_coverage_validated"] is True
    assert manifest["video_frame_alignment_reviewed"] is False
    assert manifest["source_video_probe"]["decoded_frame_count"] == 12
    assert manifest["predictions_sha256"] == hashlib.sha256(predictions.read_bytes()).hexdigest()
    assert manifest["review_status"] == "pending"
    assert manifest["annotations_reviewed"] is False
    assert manifest["deployment_domain_evidence_complete"] is False
    assert manifest["draft_is_evaluation_input"] is False
    assert manifest["flight_control_enabled"] is False
    assert manifest["physical_release_enabled"] is False
    assert len(records) == 6
    assert records[0]["review_status"] == "pending"
    assert records[0]["video_frame_index"] is None
    assert records[0]["candidates"][0]["identity_id"] is None
    assert records[0]["candidates"][0]["suggested_state"] == "tracking"

    with pytest.raises(ValueError, match="unknown ground-truth frame fields"):
        load_identity_ground_truth_jsonl(report.draft_path)


def test_tracking_review_bundle_refuses_to_overwrite_human_work_without_opt_in(
    tmp_path: Path,
) -> None:
    predictions, video, video_manifest = _bound_inputs(tmp_path)
    output = tmp_path / "review"
    first = prepare_tracking_review_bundle(
        predictions,
        video,
        video_manifest,
        output,
        video_probe=lambda path: _valid_probe(path),
    )
    first.draft_path.write_text("human work\n", encoding="utf-8")

    with pytest.raises(ValueError, match="explicit overwrite"):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            output,
            video_probe=lambda path: _valid_probe(path),
        )

    replaced = prepare_tracking_review_bundle(
        predictions,
        video,
        video_manifest,
        output,
        overwrite=True,
        video_probe=lambda path: _valid_probe(path),
    )
    assert replaced.draft_path.read_text(encoding="utf-8").startswith("{")


def test_tracking_review_bundle_rejects_video_that_cannot_cover_identity_timeline(
    tmp_path: Path,
) -> None:
    predictions, video, video_manifest = _bound_inputs(tmp_path)

    with pytest.raises(ValueError, match="fewer decoded frames"):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            tmp_path / "review-frames",
            video_probe=lambda path: _valid_probe(path, frames=3, duration_s=1.0),
        )

    with pytest.raises(ValueError, match="duration does not cover"):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            tmp_path / "review-duration",
            video_probe=lambda path: _valid_probe(path, frames=12, duration_s=0.2),
        )


def test_tracking_review_bundle_rejects_cross_session_or_tampered_video(
    tmp_path: Path,
) -> None:
    predictions, video, video_manifest = _bound_inputs(
        tmp_path / "cross-session",
        manifest_session_id=OTHER_SESSION_ID,
    )
    with pytest.raises(ValueError, match="different evidence sessions"):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            tmp_path / "cross-session-review",
            video_probe=lambda path: _valid_probe(path),
        )

    predictions, video, video_manifest = _bound_inputs(tmp_path / "tampered")
    video.write_bytes(video.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="SHA256"):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            tmp_path / "tampered-review",
            video_probe=lambda path: _valid_probe(path),
        )


@pytest.mark.parametrize(
    ("started_at_s", "ended_at_s", "message"),
    ((0.3, 1.0, "begins before"), (0.0, 0.3, "ends after")),
)
def test_tracking_review_bundle_rejects_identity_log_outside_recording_window(
    tmp_path: Path,
    started_at_s: float,
    ended_at_s: float,
    message: str,
) -> None:
    predictions, video, video_manifest = _bound_inputs(
        tmp_path,
        started_at_s=started_at_s,
        ended_at_s=ended_at_s,
    )
    with pytest.raises(ValueError, match=message):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            tmp_path / "review",
            video_probe=lambda path: _valid_probe(path),
        )


def test_tracking_review_bundle_rejects_legacy_manifest_without_session_binding(
    tmp_path: Path,
) -> None:
    predictions, video, video_manifest = _bound_inputs(tmp_path)
    manifest = json.loads(video_manifest.read_text(encoding="utf-8"))
    manifest["schema_version"] = 1
    video_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="schema version must be 2"):
        prepare_tracking_review_bundle(
            predictions,
            video,
            video_manifest,
            tmp_path / "review",
            video_probe=lambda path: _valid_probe(path),
        )
