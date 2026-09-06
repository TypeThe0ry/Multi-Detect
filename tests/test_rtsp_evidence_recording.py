from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect.rtsp_evidence_recording import (
    RtspEvidenceRecordingConfig,
    StreamCopyResult,
    record_rtsp_evidence,
    verify_rtsp_evidence_recording_manifest,
)
from multidetect.video_evidence import VideoEvidenceProbe

SESSION_ID = "12345678-1234-5678-9234-567812345678"
ROOT = Path(__file__).resolve().parents[1]


def _utc_clock(*values: str):
    timestamps = iter(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values)
    return lambda: next(timestamps)


def _valid_probe(path: Path) -> VideoEvidenceProbe:
    return VideoEvidenceProbe(
        path=path,
        decoded_frame_count=300,
        declared_frame_count=300,
        fps=25.0,
        width=1280,
        height=720,
        duration_s=12.0,
        full_frame_scan_completed=True,
        stable_dimensions=True,
        passed=True,
        failure_reasons=(),
    )


def test_rtsp_evidence_recording_is_hash_bound_redacted_stream_copy(tmp_path: Path) -> None:
    output = tmp_path / "recording.mkv"
    manifest = tmp_path / "recording.manifest.json"
    calls = []

    def runner(source: str, path: Path, duration: float, latency: int, finalize: float):
        calls.append((source, path, duration, latency, finalize))
        path.write_bytes(b"h265-matroska-stream-copy")
        return StreamCopyResult(
            actual_duration_s=12.1,
            eos_received=True,
            started_at_monotonic_s=100.0,
            ended_at_monotonic_s=112.1,
        )

    report = record_rtsp_evidence(
        RtspEvidenceRecordingConfig(
            source_env="CAMERA_SOURCE",
            session_id=SESSION_ID,
            output_video=output,
            manifest_out=manifest,
            duration_s=12.0,
            latency_ms=50,
        ),
        environ={"CAMERA_SOURCE": "rtsp://SECRET_USER:SECRET_PASSWORD@camera/stream"},
        stream_copy_runner=runner,
        video_probe=_valid_probe,
        utc_now=_utc_clock("2026-09-06T01:02:03Z", "2026-09-06T01:02:15.100000Z"),
    )

    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert report.passed is True
    assert report.output_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert calls[0][0].startswith("rtsp://SECRET_USER")
    assert document["source_description"] == "RTSP source"
    assert document["schema_version"] == 3
    assert document["session_id"] == SESSION_ID
    assert document["started_at_utc"] == "2026-09-06T01:02:03Z"
    assert document["ended_at_utc"] == "2026-09-06T01:02:15.100000Z"
    assert document["recorded_at_utc"] == document["ended_at_utc"]
    assert document["utc_clock"] == "system_utc_enclosing_stream_copy"
    assert document["started_at_monotonic_s"] == 100.0
    assert document["ended_at_monotonic_s"] == 112.1
    assert document["source_uri_recorded"] is False
    assert document["stream_copy_no_decode_or_reencode"] is True
    assert document["video_probe"]["passed"] is True
    assert document["flight_control_enabled"] is False
    assert document["physical_release_enabled"] is False
    assert "SECRET_USER" not in manifest.read_text(encoding="utf-8")
    assert "SECRET_PASSWORD" not in manifest.read_text(encoding="utf-8")
    schema = json.loads(
        (ROOT / "configs/schemas/rtsp-evidence-recording.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    verified = verify_rtsp_evidence_recording_manifest(
        manifest,
        expected_video=output,
        video_probe=_valid_probe,
    )
    assert verified.output_video == output.resolve()
    assert verified.output_sha256 == report.output_sha256
    assert verified.started_at_utc == "2026-09-06T01:02:03Z"
    assert verified.ended_at_utc == "2026-09-06T01:02:15.100000Z"


def test_rtsp_evidence_recording_rejects_missing_source_and_implicit_overwrite(
    tmp_path: Path,
) -> None:
    config = RtspEvidenceRecordingConfig(
        source_env="CAMERA_SOURCE",
        session_id=SESSION_ID,
        output_video=tmp_path / "recording.mkv",
        manifest_out=tmp_path / "manifest.json",
    )
    with pytest.raises(ValueError, match="environment variable is missing"):
        record_rtsp_evidence(config, environ={})
    with pytest.raises(ValueError, match="must be an rtsp") as error:
        record_rtsp_evidence(config, environ={"CAMERA_SOURCE": "SECRET_NOT_RTSP"})
    assert "SECRET_NOT_RTSP" not in str(error.value)

    config.output_video.write_bytes(b"preserve-me")
    with pytest.raises(ValueError, match="explicit overwrite"):
        record_rtsp_evidence(config, environ={"CAMERA_SOURCE": "rtsp://camera/stream"})


def test_rtsp_evidence_recording_preserves_failed_media_probe_in_manifest(
    tmp_path: Path,
) -> None:
    output = tmp_path / "recording.mkv"
    manifest = tmp_path / "manifest.json"

    def runner(_source: str, path: Path, *_args) -> StreamCopyResult:
        path.write_bytes(b"invalid-container")
        return StreamCopyResult(
            actual_duration_s=2.0,
            eos_received=True,
            started_at_monotonic_s=200.0,
            ended_at_monotonic_s=202.0,
        )

    def failed_probe(path: Path) -> VideoEvidenceProbe:
        return VideoEvidenceProbe(
            path=path,
            decoded_frame_count=0,
            declared_frame_count=None,
            fps=None,
            width=None,
            height=None,
            duration_s=None,
            full_frame_scan_completed=False,
            stable_dimensions=False,
            passed=False,
            failure_reasons=("video not decodable",),
        )

    report = record_rtsp_evidence(
        RtspEvidenceRecordingConfig(
            source_env="CAMERA_SOURCE",
            session_id=SESSION_ID,
            output_video=output,
            manifest_out=manifest,
            duration_s=2.0,
        ),
        environ={"CAMERA_SOURCE": "rtsp://camera/stream"},
        stream_copy_runner=runner,
        video_probe=failed_probe,
        utc_now=_utc_clock("2026-09-06T01:00:00Z", "2026-09-06T01:00:02Z"),
    )

    assert report.passed is False
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document["passed"] is False
    assert document["video_probe"]["failure_reasons"] == ["video not decodable"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"output_video": Path("recording.mp4")}, "must use .mkv"),
        ({"duration_s": 0.5}, "duration"),
        ({"latency_ms": -1}, "latency"),
        ({"finalize_timeout_s": 0.5}, "finalize timeout"),
    ],
)
def test_rtsp_evidence_recording_config_is_strict(changes: dict, message: str) -> None:
    values = {
        "source_env": "CAMERA_SOURCE",
        "session_id": SESSION_ID,
        "output_video": Path("recording.mkv"),
        "manifest_out": Path("manifest.json"),
        **changes,
    }
    with pytest.raises(ValueError, match=message):
        RtspEvidenceRecordingConfig(**values)


def test_rtsp_evidence_recording_requires_uuid_session_and_monotonic_window() -> None:
    with pytest.raises(ValueError, match="valid UUID"):
        RtspEvidenceRecordingConfig(
            source_env="CAMERA_SOURCE",
            session_id="not-a-session",
            output_video=Path("recording.mkv"),
            manifest_out=Path("manifest.json"),
        )
    with pytest.raises(ValueError, match="later than start"):
        StreamCopyResult(
            actual_duration_s=1.0,
            eos_received=True,
            started_at_monotonic_s=10.0,
            ended_at_monotonic_s=9.0,
        )


def test_rtsp_evidence_recorder_keeps_python_310_datetime_compatibility() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src/multidetect/rtsp_evidence_recording.py"
    ).read_text(encoding="utf-8")

    assert "datetime import UTC" not in source
    assert "timezone.utc" in source


def test_rtsp_evidence_verifier_rejects_video_and_manifest_mutation(tmp_path: Path) -> None:
    output = tmp_path / "recording.mkv"
    manifest = tmp_path / "recording.manifest.json"

    def runner(_source: str, path: Path, *_args) -> StreamCopyResult:
        path.write_bytes(b"hash-bound-video")
        return StreamCopyResult(
            actual_duration_s=2.0,
            eos_received=True,
            started_at_monotonic_s=10.0,
            ended_at_monotonic_s=12.0,
        )

    config = RtspEvidenceRecordingConfig(
        source_env="CAMERA_SOURCE",
        session_id=SESSION_ID,
        output_video=output,
        manifest_out=manifest,
        duration_s=2.0,
    )
    record_rtsp_evidence(
        config,
        environ={"CAMERA_SOURCE": "rtsp://camera/stream"},
        stream_copy_runner=runner,
        video_probe=_valid_probe,
        utc_now=_utc_clock("2026-09-06T02:00:00Z", "2026-09-06T02:00:02Z"),
    )
    output.write_bytes(b"hash-bound-vide0")
    with pytest.raises(ValueError, match="SHA-256 changed"):
        verify_rtsp_evidence_recording_manifest(manifest, video_probe=_valid_probe)

    output.write_bytes(b"hash-bound-video")
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["started_at_utc"] = "2026-09-06T01:59:59Z"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical_hash mismatch"):
        verify_rtsp_evidence_recording_manifest(manifest, video_probe=_valid_probe)


def test_rtsp_evidence_verifier_rejects_v2_and_non_utc_capture_window(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"schema_version":2}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field"):
        verify_rtsp_evidence_recording_manifest(manifest)

    with pytest.raises(RuntimeError, match="timezone-aware UTC"):
        record_rtsp_evidence(
            RtspEvidenceRecordingConfig(
                source_env="CAMERA_SOURCE",
                session_id=SESSION_ID,
                output_video=tmp_path / "naive.mkv",
                manifest_out=tmp_path / "naive.json",
            ),
            environ={"CAMERA_SOURCE": "rtsp://camera/stream"},
            utc_now=lambda: datetime(2026, 9, 6),
        )
