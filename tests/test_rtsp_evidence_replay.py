from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

import multidetect
import multidetect.rtsp_evidence_replay as replay_module
from multidetect.backends import RawSensorReplayRecording
from multidetect.rtsp_evidence_recording import (
    RtspEvidenceRecordingConfig,
    StreamCopyResult,
    VerifiedRtspEvidenceRecording,
    record_rtsp_evidence,
)
from multidetect.rtsp_evidence_replay import (
    create_rtsp_evidence_replay_recording,
    verify_rtsp_evidence_replay_recording,
)
from multidetect.video_evidence import VideoEvidenceProbe

ROOT = Path(__file__).resolve().parents[1]


def test_rtsp_evidence_conversion_transfers_verified_identity_and_utc(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = tmp_path / "capture.manifest.json"
    video = tmp_path / "capture.mkv"
    output = tmp_path / "raw-replay"
    manifest.write_bytes(b"manifest")
    video.write_bytes(b"container")
    probe = VideoEvidenceProbe(
        path=video,
        decoded_frame_count=250,
        declared_frame_count=250,
        fps=25.0,
        width=1280,
        height=720,
        duration_s=10.0,
        full_frame_scan_completed=True,
        stable_dimensions=True,
        passed=True,
        failure_reasons=(),
    )
    verified = VerifiedRtspEvidenceRecording(
        manifest_path=manifest.resolve(),
        manifest_sha256="a" * 64,
        manifest_bytes=8,
        canonical_hash="b" * 64,
        session_id="12345678-1234-5678-9234-567812345678",
        output_video=video.resolve(),
        output_bytes=9,
        output_sha256="c" * 64,
        started_at_utc="2026-09-06T05:00:00Z",
        ended_at_utc="2026-09-06T05:00:10Z",
        actual_duration_s=10.0,
        video_probe=probe,
    )
    calls = []

    def fake_verify(path):
        assert Path(path) == manifest
        return verified

    def fake_create(path, destination, **kwargs):
        calls.append((path, destination, kwargs))
        destination.mkdir()
        summary = {
            "source_video_sha256": verified.output_sha256,
            "source_evidence_binding": kwargs["source_evidence_binding"],
            "summary_canonical_hash": "d" * 64,
        }
        (destination / "recording-summary.json").write_text(
            json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
        )
        return summary

    monkeypatch.setattr(replay_module, "verify_rtsp_evidence_recording_manifest", fake_verify)
    monkeypatch.setattr(replay_module, "create_video_replay_recording", fake_create)
    monkeypatch.setattr(
        replay_module,
        "verify_rtsp_evidence_replay_recording",
        lambda _output: {"summary_canonical_hash": "d" * 64},
    )

    summary = create_rtsp_evidence_replay_recording(manifest, output, frame_stride=2)

    kwargs = calls[0][2]
    assert calls[0][0] == video.resolve()
    assert kwargs["started_at_utc"] == "2026-09-06T05:00:00Z"
    assert kwargs["expected_source_video_sha256"] == "c" * 64
    assert kwargs["expected_source_video_bytes"] == 9
    assert kwargs["frame_stride"] == 2
    binding = summary["source_evidence_binding"]
    assert binding["manifest"]["sha256"] == "a" * 64
    assert binding["manifest"]["canonical_hash"] == "b" * 64
    assert binding["capture_window"] == {
        "started_at_utc": "2026-09-06T05:00:00Z",
        "ended_at_utc": "2026-09-06T05:00:10Z",
        "actual_duration_s": 10.0,
    }
    assert binding["claims"]["operator_field_provenance_attested"] is False
    assert binding["claims"]["release_ready"] is False


def test_rtsp_evidence_conversion_cli_help_and_lazy_export() -> None:
    assert multidetect.create_rtsp_evidence_replay_recording is (
        create_rtsp_evidence_replay_recording
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "create_rtsp_evidence_replay_recording.py"),
            "--help",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "evidence_manifest" in completed.stdout
    assert "non-overwriting camera-only raw replay" in completed.stdout


def test_rtsp_evidence_container_to_raw_replay_real_opencv_path(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    source_container = tmp_path / "source.avi"
    writer = cv2.VideoWriter(
        str(source_container),
        cv2.VideoWriter_fourcc(*"MJPG"),
        5.0,
        (24, 16),
    )
    if not writer.isOpened():
        writer.release()
        pytest.skip("OpenCV MJPG writer is unavailable in this environment")
    for index in range(3):
        frame = np.zeros((16, 24, 3), dtype=np.uint8)
        frame[:, :, 2] = 30 + index * 20
        writer.write(frame)
    writer.release()

    video = tmp_path / "capture.mkv"
    manifest = tmp_path / "capture.manifest.json"

    def runner(_source: str, path: Path, *_args) -> StreamCopyResult:
        path.write_bytes(source_container.read_bytes())
        return StreamCopyResult(
            actual_duration_s=0.4,
            eos_received=True,
            started_at_monotonic_s=20.0,
            ended_at_monotonic_s=20.4,
        )

    times = iter(
        (
            datetime.fromisoformat("2026-09-06T06:00:00+00:00"),
            datetime.fromisoformat("2026-09-06T06:00:00.400000+00:00"),
        )
    )
    report = record_rtsp_evidence(
        RtspEvidenceRecordingConfig(
            source_env="CAMERA_SOURCE",
            session_id="12345678-1234-5678-9234-567812345678",
            output_video=video,
            manifest_out=manifest,
            duration_s=1.0,
        ),
        environ={"CAMERA_SOURCE": "rtsp://camera/stream"},
        stream_copy_runner=runner,
        utc_now=lambda: next(times),
    )
    if not report.passed:
        pytest.skip("OpenCV cannot probe the local MJPG container through its .mkv evidence path")

    output = tmp_path / "raw-replay"
    summary = create_rtsp_evidence_replay_recording(manifest, output)

    assert summary["started_at_utc"] == "2026-09-06T06:00:00Z"
    assert summary["source_video_sha256"] == report.output_sha256
    assert summary["source_evidence_binding"]["verified"] is True
    assert summary["source_attestation_handoff"]["attestation_time_eligible"] is True
    recording = RawSensorReplayRecording.from_manifest(output / "recording.manifest.json")
    assert len(recording.frames) == 3
    assert recording.started_at_utc.isoformat() == "2026-09-06T06:00:00+00:00"
    verification = verify_rtsp_evidence_replay_recording(output)
    assert verification["recording_hash"] == recording.recording_hash
    assert verification["source_video_sha256"] == report.output_sha256
    assert verification["position_healthy"] is False
    assert verification["release_ready"] is False

    moved = tmp_path / "moved-bundle"
    moved.mkdir()
    shutil.move(str(video), moved / video.name)
    shutil.move(str(manifest), moved / manifest.name)
    shutil.move(str(output), moved / output.name)
    moved_output = moved / output.name
    moved_verification = verify_rtsp_evidence_replay_recording(moved_output)
    assert moved_verification["recording_hash"] == recording.recording_hash
    assert moved_verification["source_video_sha256"] == report.output_sha256

    summary_path = moved_output / "recording-summary.json"
    changed = json.loads(summary_path.read_text(encoding="utf-8"))
    changed["gps_bound"] = True
    summary_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="gps_bound must remain false"):
        verify_rtsp_evidence_replay_recording(moved_output)
