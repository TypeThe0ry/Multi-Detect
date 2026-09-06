"""Promote one verified RTSP evidence container into a camera-only raw replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_bool,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)
from .backends import RawSensorReplayRecording
from .rtsp_evidence_recording import (
    VerifiedRtspEvidenceRecording,
    verify_rtsp_evidence_recording_manifest,
)
from .video_replay_recording import (
    DEFAULT_AIRFRAME_PROFILE_HASH,
    create_video_replay_recording,
)


def create_rtsp_evidence_replay_recording(
    evidence_manifest: str | Path,
    output: str | Path,
    *,
    recording_id: str | None = None,
    airframe_profile_hash: str = DEFAULT_AIRFRAME_PROFILE_HASH,
    fps: float | None = None,
    frame_stride: int = 1,
    frame_limit: int | None = None,
) -> dict[str, Any]:
    """Verify an RTSP v3 manifest and atomically create its raw replay.

    Container identity and the capture UTC anchor come only from the strict
    evidence manifest.  Operator-entered field provenance is deliberately not
    inferred; the resulting camera-only recording still needs a separate
    deployment source attestation and independent navigation data.
    """

    verified = verify_rtsp_evidence_recording_manifest(evidence_manifest)
    output_root = Path(output).expanduser().resolve()
    binding = _source_binding(verified, output_root)
    summary = create_video_replay_recording(
        verified.output_video,
        output,
        recording_id=recording_id or f"rtsp-{verified.session_id}",
        airframe_profile_hash=airframe_profile_hash,
        started_at_utc=verified.started_at_utc,
        fps=fps,
        frame_stride=frame_stride,
        frame_limit=frame_limit,
        expected_source_video_sha256=verified.output_sha256,
        expected_source_video_bytes=verified.output_bytes,
        source_evidence_binding=binding,
    )
    verification = verify_rtsp_evidence_replay_recording(output)
    if verification["summary_canonical_hash"] != summary["summary_canonical_hash"]:
        raise RuntimeError("RTSP replay verification returned a different summary identity")
    return summary


def verify_rtsp_evidence_replay_recording(output: str | Path) -> dict[str, Any]:
    """Reopen the raw replay, source container and full RTSP binding."""

    requested_root = Path(output).expanduser()
    if requested_root.is_symlink() or not requested_root.is_dir():
        raise AutonomyContractError("RTSP replay root must be a regular non-symlink directory")
    root = requested_root.resolve(strict=True)
    summary_path = root / "recording-summary.json"
    if summary_path.is_symlink() or not summary_path.is_file():
        raise AutonomyContractError("RTSP replay summary must be a regular non-symlink file")
    raw = strict_json_loads(summary_path.read_bytes(), name="RTSP replay recording summary")
    required = frozenset(
        {
            "event",
            "schema_version",
            "product",
            "release_label",
            "python_version",
            "source_video",
            "source_video_sha256",
            "source_video_bytes",
            "recording_id",
            "airframe_profile_hash",
            "started_at_utc",
            "recording_manifest",
            "recording_manifest_sha256",
            "recording_hash",
            "events_sha256",
            "events_bytes",
            "frame_count",
            "decoded_frame_count",
            "frame_stride",
            "frame_limit",
            "fps",
            "fps_source",
            "width",
            "height",
            "telemetry_mode",
            "position_healthy",
            "gps_bound",
            "ground_truth_present",
            "predictions_present",
            "software_only",
            "deployment_domain_evidence_complete",
            "accuracy_validated",
            "gate_passed",
            "source_attestation_handoff",
            "notes",
            "source_evidence_binding",
            "summary_canonical_hash",
        }
    )
    summary = strict_object(raw, name="RtspEvidenceReplaySummary", required=required)
    require_schema_version(summary["schema_version"])
    if summary["event"] != "video_replay_recording_created":
        raise AutonomyContractError("RTSP replay summary event is invalid")
    expected_false = (
        "position_healthy",
        "gps_bound",
        "ground_truth_present",
        "predictions_present",
        "deployment_domain_evidence_complete",
        "accuracy_validated",
        "gate_passed",
    )
    for field in expected_false:
        if require_bool(summary[field], field):
            raise AutonomyContractError(f"RTSP replay summary {field} must remain false")
    if require_bool(summary["software_only"], "software_only") is not True:
        raise AutonomyContractError("RTSP replay summary must remain software_only")
    if summary["telemetry_mode"] != "camera_only_unknown_navigation":
        raise AutonomyContractError("RTSP replay telemetry mode is invalid")

    claimed_summary_hash = require_sha256(
        summary["summary_canonical_hash"], "summary_canonical_hash"
    )
    unhashed = dict(summary)
    unhashed.pop("summary_canonical_hash")
    if canonical_sha256(unhashed) != claimed_summary_hash:
        raise AutonomyContractError("RTSP replay summary canonical hash mismatch")

    binding = strict_object(
        summary["source_evidence_binding"],
        name="source_evidence_binding",
        required=frozenset(
            {
                "type",
                "verified",
                "manifest",
                "session_id",
                "source_video",
                "capture_window",
                "claims",
            }
        ),
    )
    manifest_binding = strict_object(
        binding["manifest"],
        name="source_evidence_binding.manifest",
        required=frozenset({"path", "path_base", "sha256", "bytes", "canonical_hash"}),
    )
    source_manifest_path = _resolve_bound_path(
        root,
        require_text(manifest_binding["path"], "manifest.path"),
        require_text(manifest_binding["path_base"], "manifest.path_base"),
    )
    verified_source = verify_rtsp_evidence_recording_manifest(source_manifest_path)
    if dict(binding) != _source_binding(verified_source, root):
        raise AutonomyContractError("RTSP replay source evidence binding changed")

    recorded_manifest_reference = Path(
        require_text(summary["recording_manifest"], "recording_manifest")
    )
    if recorded_manifest_reference.name != "recording.manifest.json":
        raise AutonomyContractError("RTSP replay summary raw manifest reference is invalid")
    recording_manifest_path = root / "recording.manifest.json"
    if recording_manifest_path.is_symlink() or not recording_manifest_path.is_file():
        raise AutonomyContractError("RTSP replay raw manifest must be a regular file")
    manifest_payload = recording_manifest_path.read_bytes()
    if hashlib.sha256(manifest_payload).hexdigest() != require_sha256(
        summary["recording_manifest_sha256"], "recording_manifest_sha256"
    ):
        raise AutonomyContractError("RTSP replay raw manifest SHA-256 changed")
    recording = RawSensorReplayRecording.from_manifest(recording_manifest_path)
    if recording.recording_hash != require_sha256(summary["recording_hash"], "recording_hash"):
        raise AutonomyContractError("RTSP replay recording hash changed")
    if recording.recording_id != require_text(summary["recording_id"], "recording_id"):
        raise AutonomyContractError("RTSP replay recording ID changed")
    if summary["started_at_utc"] != verified_source.started_at_utc:
        raise AutonomyContractError("RTSP replay UTC anchor changed")
    if require_sha256(summary["source_video_sha256"], "source_video_sha256") != (
        verified_source.output_sha256
    ):
        raise AutonomyContractError("RTSP replay source video SHA-256 changed")
    if require_int(summary["source_video_bytes"], "source_video_bytes", minimum=1) != (
        verified_source.output_bytes
    ):
        raise AutonomyContractError("RTSP replay source video byte count changed")
    if require_int(summary["frame_count"], "frame_count", minimum=1) != len(recording.frames):
        raise AutonomyContractError("RTSP replay frame count changed")
    return {
        "event": "rtsp_evidence_replay_recording_verified",
        "summary_canonical_hash": claimed_summary_hash,
        "source_manifest_sha256": verified_source.manifest_sha256,
        "source_video_sha256": verified_source.output_sha256,
        "recording_manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "recording_hash": recording.recording_hash,
        "frame_count": len(recording.frames),
        "position_healthy": False,
        "gps_bound": False,
        "release_ready": False,
    }


def _source_binding(
    verified: VerifiedRtspEvidenceRecording,
    recording_root: Path,
) -> dict[str, Any]:
    manifest_path, manifest_base = _portable_path_binding(
        verified.manifest_path, recording_root
    )
    video_path, video_base = _portable_path_binding(verified.output_video, recording_root)
    return {
        "type": "rtsp_evidence_recording_v3",
        "verified": True,
        "manifest": {
            "path": manifest_path,
            "path_base": manifest_base,
            "sha256": verified.manifest_sha256,
            "bytes": verified.manifest_bytes,
            "canonical_hash": verified.canonical_hash,
        },
        "session_id": verified.session_id,
        "source_video": {
            "path": video_path,
            "path_base": video_base,
            "sha256": verified.output_sha256,
            "bytes": verified.output_bytes,
        },
        "capture_window": {
            "started_at_utc": verified.started_at_utc,
            "ended_at_utc": verified.ended_at_utc,
            "actual_duration_s": verified.actual_duration_s,
        },
        "claims": {
            "container_integrity_verified": True,
            "full_frame_media_probe_verified": True,
            "source_uri_recorded": False,
            "stream_copy_no_decode_or_reencode": True,
            "operator_field_provenance_attested": False,
            "navigation_bound": False,
            "release_ready": False,
        },
    }


def _portable_path_binding(path: Path, recording_root: Path) -> tuple[str, str]:
    resolved = path.resolve(strict=True)
    try:
        relative = Path(os.path.relpath(resolved, start=recording_root.resolve()))
    except ValueError:
        return str(resolved), "absolute"
    return relative.as_posix(), "recording_root"


def _resolve_bound_path(root: Path, reference: str, path_base: str) -> Path:
    path = Path(reference)
    if path_base == "recording_root":
        if path.is_absolute():
            raise AutonomyContractError("recording-root source path must be relative")
        return (root / path).resolve()
    if path_base == "absolute":
        if not path.is_absolute():
            raise AutonomyContractError("absolute source path binding must be absolute")
        return path.resolve()
    raise AutonomyContractError("source path_base is invalid")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one RTSP evidence v3 manifest and convert its exact H.265/Matroska "
            "container into a non-overwriting camera-only raw replay."
        )
    )
    parser.add_argument("evidence_manifest", type=Path)
    parser.add_argument("output", type=Path, help="new raw replay directory")
    parser.add_argument("--recording-id")
    parser.add_argument(
        "--airframe-profile-hash",
        default=DEFAULT_AIRFRAME_PROFILE_HASH,
    )
    parser.add_argument("--fps", type=float)
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--frame-limit", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = create_rtsp_evidence_replay_recording(
            args.evidence_manifest,
            args.output,
            recording_id=args.recording_id,
            airframe_profile_hash=args.airframe_profile_hash,
            fps=args.fps,
            frame_stride=args.frame_stride,
            frame_limit=args.frame_limit,
        )
    except (AutonomyContractError, FileExistsError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"RTSP evidence replay conversion failed: {exc}") from exc
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "create_rtsp_evidence_replay_recording",
    "main",
    "verify_rtsp_evidence_replay_recording",
]
