from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import multidetect
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.backends import RawSensorReplayRecording
from multidetect.product_version import load_product_version
from multidetect.video_replay_recording import create_video_replay_recording

ROOT = Path(__file__).resolve().parents[1]


class _Encoded:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def tobytes(self) -> bytes:
        return self._payload


class _Capture:
    def __init__(self, frames: list[object], *, fps: float, opened: bool = True) -> None:
        self._frames = list(frames)
        self._fps = fps
        self._opened = opened
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def get(self, property_id: int) -> float:
        assert property_id == _CV2.CAP_PROP_FPS
        return self._fps

    def read(self) -> tuple[bool, object | None]:
        if not self._frames:
            return False, None
        return True, self._frames.pop(0)

    def release(self) -> None:
        self.released = True


class _CV2:
    CAP_PROP_FPS = 5

    def __init__(self, capture: _Capture) -> None:
        self.capture = capture

    def VideoCapture(self, path: str) -> _Capture:
        assert path.endswith("input.video")
        return self.capture

    @staticmethod
    def imencode(extension: str, image: object) -> tuple[bool, _Encoded]:
        assert extension == ".png"
        return True, _Encoded(f"encoded-frame-{image.index}".encode("ascii"))


def _install_fake_cv2(monkeypatch: pytest.MonkeyPatch, capture: _Capture) -> None:
    monkeypatch.setitem(sys.modules, "cv2", _CV2(capture))


def _frame(index: int, *, width: int = 6, height: int = 4) -> SimpleNamespace:
    return SimpleNamespace(index=index, ndim=3, shape=(height, width, 3))


def test_video_conversion_writes_verified_raw_recording_and_unknown_gps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.video"
    source.write_bytes(b"camera-container")
    capture = _Capture([_frame(index) for index in range(5)], fps=10.0)
    _install_fake_cv2(monkeypatch, capture)
    output = tmp_path / "recording"

    summary = create_video_replay_recording(
        source,
        output,
        recording_id="field-video-001",
        airframe_profile_hash="a" * 64,
        started_at_utc="2026-09-02T03:00:00Z",
        frame_stride=2,
    )

    assert capture.released is True
    assert summary["release_label"] == load_product_version().release_label
    assert summary["frame_count"] == 3
    assert summary["decoded_frame_count"] == 5
    assert summary["fps"] == pytest.approx(10.0)
    assert summary["fps_source"] == "container"
    assert summary["telemetry_mode"] == "camera_only_unknown_navigation"
    assert summary["position_healthy"] is False
    assert summary["gps_bound"] is False
    assert summary["gate_passed"] is False
    assert output.is_dir()
    assert not tuple(tmp_path.glob(".recording.*"))

    manifest_path = output / "recording.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == summary[
        "recording_manifest_sha256"
    ]
    recording = RawSensorReplayRecording.from_manifest(manifest_path)
    assert manifest["recording_hash"] == recording.recording_hash
    assert [frame.frame_id for frame in recording.frames] == [
        "video-frame-000000",
        "video-frame-000001",
        "video-frame-000002",
    ]
    assert [frame.captured_at_s for frame in recording.frames] == pytest.approx([0.0, 0.2, 0.4])
    assert len(recording.telemetry_samples) == 3
    telemetry = recording.telemetry_samples[0].telemetry
    assert telemetry.position_healthy is False
    assert telemetry.link_healthy is True
    assert telemetry.latitude_deg != telemetry.latitude_deg  # NaN: GPS is absent, not fake.
    assert telemetry.longitude_deg != telemetry.longitude_deg
    assert all(path.is_file() for path in (output / "frames").glob("*.png"))

    # Image bytes are part of the manifest contract and a mutation fails closed.
    first_image = next((output / "frames").glob("*.png"))
    original_image = first_image.read_bytes()
    first_image.write_bytes(b"x" * len(original_image))
    with pytest.raises(AutonomyContractError, match="hash mismatch"):
        RawSensorReplayRecording.from_manifest(manifest_path)


def test_video_conversion_supports_fps_override_and_selected_frame_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.video"
    source.write_bytes(b"camera-container")
    capture = _Capture([_frame(index) for index in range(6)], fps=0.0)
    _install_fake_cv2(monkeypatch, capture)

    summary = create_video_replay_recording(
        source,
        tmp_path / "recording",
        fps=4.0,
        frame_stride=2,
        frame_limit=2,
    )

    assert summary["frame_count"] == 2
    assert summary["decoded_frame_count"] == 3
    assert summary["fps"] == pytest.approx(4.0)
    assert summary["fps_source"] == "override"


def test_video_conversion_reads_a_real_opencv_container(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    source = tmp_path / "camera.avi"
    writer = cv2.VideoWriter(
        str(source),
        cv2.VideoWriter_fourcc(*"MJPG"),
        5.0,
        (24, 16),
    )
    if not writer.isOpened():
        writer.release()
        pytest.skip("OpenCV MJPG writer is unavailable in this environment")
    for index in range(3):
        frame = np.zeros((16, 24, 3), dtype=np.uint8)
        frame[:, :, 1] = 40 + index * 20
        writer.write(frame)
    writer.release()

    summary = create_video_replay_recording(source, tmp_path / "recording")

    assert summary["frame_count"] == 3
    assert summary["decoded_frame_count"] == 3
    assert summary["width"] == 24
    assert summary["height"] == 16
    recording = RawSensorReplayRecording.from_manifest(
        tmp_path / "recording" / "recording.manifest.json"
    )
    assert len(recording.frames) == 3
    assert all(frame.image.encoding == "png" for frame in recording.frames)


def test_video_conversion_rejects_invalid_or_unopenable_inputs_without_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.video"
    source.write_bytes(b"camera-container")

    _install_fake_cv2(monkeypatch, _Capture([], fps=0.0, opened=False))
    output = tmp_path / "unopenable"
    with pytest.raises(ValueError, match="cannot open video"):
        create_video_replay_recording(source, output)
    assert not output.exists()
    assert not tuple(tmp_path.glob(".unopenable.*"))

    _install_fake_cv2(monkeypatch, _Capture([_frame(0)], fps=0.0))
    with pytest.raises(ValueError, match="container FPS"):
        create_video_replay_recording(source, tmp_path / "missing-fps")
    assert not (tmp_path / "missing-fps").exists()
    assert not tuple(tmp_path.glob(".missing-fps.*"))

    with pytest.raises(ValueError, match="frame_stride"):
        create_video_replay_recording(source, tmp_path / "bad-stride", frame_stride=0)

    with pytest.raises(ValueError, match="recording_id"):
        create_video_replay_recording(source, tmp_path / "bad-recording-id", recording_id=" ")


def test_video_replay_script_help_works_without_pythonpath() -> None:
    assert multidetect.create_video_replay_recording is create_video_replay_recording
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "create_video_replay_recording.py"), "--help"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--frame-stride" in completed.stdout
    assert "camera-only" in completed.stdout
