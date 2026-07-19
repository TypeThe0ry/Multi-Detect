from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .evidence_session import normalize_evidence_session_id
from .tracking_evaluation import IdentityPredictionFrame, load_identity_prediction_jsonl
from .video_evidence import (
    VideoEvidenceProbe,
    probe_video_evidence,
    video_evidence_document,
)


@dataclass(frozen=True, slots=True)
class TrackingReviewBundle:
    output_directory: Path
    manifest_path: Path
    draft_path: Path
    frame_count: int
    candidate_observation_count: int
    unique_candidate_track_count: int
    labels: tuple[str, ...]
    duration_s: float
    predictions_sha256: str
    source_video_sha256: str
    source_video_manifest_sha256: str
    draft_sha256: str
    evidence_session_id: str
    recording_started_at_monotonic_s: float
    recording_ended_at_monotonic_s: float
    source_video_probe: VideoEvidenceProbe
    source_video_track_timeline_coverage_validated: bool
    review_status: str = "pending"
    annotations_reviewed: bool = False
    deployment_domain_evidence_complete: bool = False
    flight_control_enabled: bool = False
    physical_release_enabled: bool = False


def prepare_tracking_review_bundle(
    predictions_path: str | Path,
    source_video_path: str | Path,
    source_video_manifest_path: str | Path,
    output_directory: str | Path,
    *,
    overwrite: bool = False,
    video_probe: Callable[[Path], VideoEvidenceProbe] | None = None,
) -> TrackingReviewBundle:
    predictions_path = Path(predictions_path)
    source_video_path = Path(source_video_path)
    source_video_manifest_path = Path(source_video_manifest_path)
    output_directory = Path(output_directory)
    if not predictions_path.is_file():
        raise ValueError(f"identity predictions do not exist: {predictions_path}")
    if not source_video_path.is_file():
        raise ValueError(f"tracking source video does not exist: {source_video_path}")
    if not source_video_manifest_path.is_file():
        raise ValueError(
            f"tracking source video manifest does not exist: {source_video_manifest_path}"
        )
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be a boolean")

    frames = load_identity_prediction_jsonl(predictions_path)
    session_id = frames[0].session_id
    if session_id is None:
        raise ValueError("identity predictions must contain an evidence session ID")
    recording_manifest = _load_recording_manifest(source_video_manifest_path)
    if recording_manifest["session_id"] != session_id:
        raise ValueError("identity predictions and source video use different evidence sessions")
    source_video_sha256 = _sha256_file(source_video_path)
    if recording_manifest["output_sha256"] != source_video_sha256:
        raise ValueError("source video SHA256 does not match its recording manifest")
    if recording_manifest["output_bytes"] != source_video_path.stat().st_size:
        raise ValueError("source video size does not match its recording manifest")
    source_video_probe = (video_probe or probe_video_evidence)(source_video_path)
    if not source_video_probe.passed:
        raise ValueError(
            "source video validation failed: " + "; ".join(source_video_probe.failure_reasons)
        )
    prediction_duration_s = max(0.0, frames[-1].captured_at_s - frames[0].captured_at_s)
    recording_started_at_s = recording_manifest["started_at_monotonic_s"]
    recording_ended_at_s = recording_manifest["ended_at_monotonic_s"]
    timeline_tolerance_s = 0.25
    if frames[0].captured_at_s < recording_started_at_s - timeline_tolerance_s:
        raise ValueError("identity log begins before the bound source video recording window")
    if frames[-1].captured_at_s > recording_ended_at_s + timeline_tolerance_s:
        raise ValueError("identity log ends after the bound source video recording window")
    video_duration_s = source_video_probe.duration_s
    if video_duration_s is None or source_video_probe.fps is None:
        raise ValueError("source video validation did not produce duration and FPS")
    coverage_tolerance_s = max(0.1, 2.0 / source_video_probe.fps)
    coverage_failures: list[str] = []
    if source_video_probe.decoded_frame_count < len(frames):
        coverage_failures.append("source video has fewer decoded frames than the identity log")
    if video_duration_s + coverage_tolerance_s < prediction_duration_s:
        coverage_failures.append("source video duration does not cover the identity log timeline")
    if coverage_failures:
        raise ValueError("source video/identity timeline mismatch: " + "; ".join(coverage_failures))
    output_directory.mkdir(parents=True, exist_ok=True)
    draft_path = output_directory / "identity-ground-truth.review-draft.jsonl"
    manifest_path = output_directory / "review-manifest.json"
    existing = tuple(path for path in (draft_path, manifest_path) if path.exists())
    if existing and not overwrite:
        raise ValueError(
            "tracking review output already exists; use explicit overwrite only after preserving "
            "any human review work"
        )

    draft_text = _review_draft_text(frames)
    _atomic_write_text(draft_path, draft_text)
    labels = tuple(sorted({track.label for frame in frames for track in frame.tracks}))
    candidate_track_ids = {track.track_id for frame in frames for track in frame.tracks}
    candidate_observations = sum(len(frame.tracks) for frame in frames)
    duration_s = prediction_duration_s
    report = TrackingReviewBundle(
        output_directory=output_directory,
        manifest_path=manifest_path,
        draft_path=draft_path,
        frame_count=len(frames),
        candidate_observation_count=candidate_observations,
        unique_candidate_track_count=len(candidate_track_ids),
        labels=labels,
        duration_s=duration_s,
        predictions_sha256=_sha256_file(predictions_path),
        source_video_sha256=source_video_sha256,
        source_video_manifest_sha256=_sha256_file(source_video_manifest_path),
        draft_sha256=_sha256_file(draft_path),
        evidence_session_id=session_id,
        recording_started_at_monotonic_s=recording_started_at_s,
        recording_ended_at_monotonic_s=recording_ended_at_s,
        source_video_probe=source_video_probe,
        source_video_track_timeline_coverage_validated=True,
    )
    _atomic_write_text(
        manifest_path,
        json.dumps(
            tracking_review_bundle_document(
                report,
                predictions_path=predictions_path,
                source_video_path=source_video_path,
                source_video_manifest_path=source_video_manifest_path,
            ),
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n",
    )
    return report


def tracking_review_bundle_document(
    report: TrackingReviewBundle,
    *,
    predictions_path: str | Path,
    source_video_path: str | Path,
    source_video_manifest_path: str | Path,
) -> dict[str, object]:
    return {
        "event": "tracking_identity_review_bundle_prepared",
        "review_status": report.review_status,
        "annotations_reviewed": report.annotations_reviewed,
        "deployment_domain_evidence_complete": report.deployment_domain_evidence_complete,
        "frame_count": report.frame_count,
        "candidate_observation_count": report.candidate_observation_count,
        "unique_candidate_track_count": report.unique_candidate_track_count,
        "labels": list(report.labels),
        "duration_s": report.duration_s,
        "evidence_session_id": report.evidence_session_id,
        "predictions_path": str(Path(predictions_path)),
        "predictions_sha256": report.predictions_sha256,
        "source_video_path": str(Path(source_video_path)),
        "source_video_sha256": report.source_video_sha256,
        "source_video_manifest_path": str(Path(source_video_manifest_path)),
        "source_video_manifest_sha256": report.source_video_manifest_sha256,
        "recording_started_at_monotonic_s": report.recording_started_at_monotonic_s,
        "recording_ended_at_monotonic_s": report.recording_ended_at_monotonic_s,
        "session_id_binding_validated": True,
        "monotonic_recording_window_validated": True,
        "source_video_media_decoding_validated": report.source_video_probe.passed,
        "source_video_track_timeline_coverage_validated": (
            report.source_video_track_timeline_coverage_validated
        ),
        "source_video_probe": video_evidence_document(report.source_video_probe),
        "video_frame_alignment_reviewed": False,
        "review_draft_path": str(report.draft_path),
        "review_draft_sha256": report.draft_sha256,
        "draft_is_evaluation_input": False,
        "required_human_actions": [
            "confirm the source video is playable and complete",
            "align every record to the corresponding source-video frame",
            "replace suggested track IDs with independently reviewed identity IDs",
            "mark every identity visible, occluded, or out_of_frame on every timeline frame",
            "set non-visible ground-truth bbox to null",
            "obtain independent second-person review before evaluation",
        ],
        "flight_control_enabled": report.flight_control_enabled,
        "physical_release_enabled": report.physical_release_enabled,
    }


def _load_recording_manifest(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("source video recording manifest is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("source video recording manifest must be an object")
    if raw.get("event") != "rtsp_tracking_evidence_recording_completed":
        raise ValueError("source video recording manifest has the wrong event type")
    if raw.get("schema_version") != 2:
        raise ValueError("source video recording manifest schema version must be 2")
    if raw.get("passed") is not True:
        raise ValueError("source video recording manifest did not pass")
    if raw.get("source_uri_recorded") is not False:
        raise ValueError("source video recording manifest is not URI-redacted")
    if raw.get("stream_copy_no_decode_or_reencode") is not True:
        raise ValueError("source video recording manifest is not a stream copy")
    session_id = normalize_evidence_session_id(raw.get("session_id"))
    output_sha256 = raw.get("output_sha256")
    if (
        not isinstance(output_sha256, str)
        or len(output_sha256) != 64
        or any(character not in "0123456789abcdef" for character in output_sha256.lower())
    ):
        raise ValueError("source video recording manifest SHA256 is invalid")
    output_bytes = raw.get("output_bytes")
    if isinstance(output_bytes, bool) or not isinstance(output_bytes, int) or output_bytes <= 0:
        raise ValueError("source video recording manifest byte count is invalid")
    started_at_s = _finite_nonnegative_manifest_number(
        raw.get("started_at_monotonic_s"),
        "recording start",
    )
    ended_at_s = _finite_nonnegative_manifest_number(
        raw.get("ended_at_monotonic_s"),
        "recording end",
    )
    if ended_at_s <= started_at_s:
        raise ValueError("source video recording manifest time window is invalid")
    return {
        "session_id": session_id,
        "output_sha256": output_sha256.lower(),
        "output_bytes": output_bytes,
        "started_at_monotonic_s": started_at_s,
        "ended_at_monotonic_s": ended_at_s,
    }


def _finite_nonnegative_manifest_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"source video manifest {name} timestamp is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"source video manifest {name} timestamp is invalid")
    return number


def _review_draft_text(frames: tuple[IdentityPredictionFrame, ...]) -> str:
    records = []
    for frame in frames:
        records.append(
            json.dumps(
                {
                    "frame_id": frame.frame_id,
                    "captured_at_s": frame.captured_at_s,
                    "review_status": "pending",
                    "video_frame_index": None,
                    "candidates": [
                        {
                            "candidate_track_id": track.track_id,
                            "suggested_label": track.label,
                            "suggested_bbox": track.bbox.rounded(),
                            "suggested_state": track.state,
                            "suggested_confidence": track.confidence,
                            "identity_id": None,
                            "label": None,
                            "visibility": None,
                            "bbox": None,
                        }
                        for track in frame.tracks
                    ],
                },
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    return "\n".join(records) + "\n"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "TrackingReviewBundle",
    "prepare_tracking_review_bundle",
    "tracking_review_bundle_document",
]
