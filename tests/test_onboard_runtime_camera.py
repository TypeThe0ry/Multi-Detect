from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from multidetect.onboard_runtime.camera import (
    CameraCalibration,
    CameraSource,
    FileCameraSource,
    OpenCVCameraSource,
)


def calibration() -> CameraCalibration:
    return CameraCalibration(4, 3, 2.0, 2.0, 2.0, 1.5, {"mount_pitch_down_deg": 0.0})


def test_load_repository_calibration_and_camera_info() -> None:
    path = Path(__file__).parents[1] / "simulation/gazebo/calibration/" \
        "gazebo-multirotor-front-camera-v1.json"
    result = CameraCalibration.load(path)
    assert result.width == 1280
    assert result.camera_info["fx"] == pytest.approx(539.93636802453)
    assert result.extrinsics["camera_offset_forward_m"] == 0.18


def test_calibration_rejects_invalid_and_missing_mount(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="finite"):
        CameraCalibration(4, 3, float("nan"), 2, 2, 1)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"intrinsics": calibration().camera_info}), encoding="utf-8")
    with pytest.raises(ValueError, match="extrinsics"):
        CameraCalibration.load(path)


def test_file_camera_latest_wins_and_preserves_stale_timestamp(tmp_path: Path) -> None:
    (tmp_path / "001.png").write_bytes(b"first")
    (tmp_path / "002.png").write_bytes(b"second")
    now = [100.0]
    camera = FileCameraSource(tmp_path, calibration(), fps=10, clock=lambda: now[0])
    assert isinstance(camera, CameraSource)
    assert camera.latest() is None
    camera.start()
    assert camera.latest().rgb == b"first"
    now[0] = 100.15
    deadline = time.monotonic() + 1
    while camera.latest().rgb != b"second" and time.monotonic() < deadline:
        time.sleep(0.005)
    frame = camera.latest()
    assert frame.rgb == b"second"
    assert frame.timestamp_monotonic == 100.1
    now[0] = 105.0
    assert camera.latest() is frame
    camera.close()
    assert camera.latest() is None


def test_opencv_reader_encodes_on_producer_and_reports_disconnect() -> None:
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")

    class Capture:
        def __init__(self) -> None:
            self.calls = 0
            self.released = False

        def isOpened(self) -> bool:
            return True

        def set(self, *_args) -> bool:
            return True

        def read(self):
            self.calls += 1
            if self.calls == 1:
                return True, np.zeros((3, 4, 3), dtype=np.uint8)
            return False, None

        def release(self) -> None:
            self.released = True

    capture = Capture()
    camera = OpenCVCameraSource(
        "fake", calibration(), clock=lambda: 123.0,
        capture_factory=lambda *_args: capture,
    )
    camera.start()
    deadline = time.monotonic() + 2
    while camera.error is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert isinstance(camera.error, RuntimeError)
    frame = camera.latest()
    assert frame.timestamp_monotonic == 123.0
    image = cv2.imdecode(np.frombuffer(frame.rgb, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape == (3, 4, 3)
    camera.close()
    assert capture.released


def test_camera_backend_and_empty_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="backend"):
        OpenCVCameraSource(0, calibration(), backend="unknown")
    with pytest.raises(ValueError, match="image files"):
        FileCameraSource(tmp_path, calibration())
