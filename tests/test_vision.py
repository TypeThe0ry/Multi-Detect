from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import multidetect.vision as vision_module
from multidetect.domain import BoundingBox
from multidetect.vision import (
    CameraReadError,
    CaptureConfig,
    DetectorEnsemble,
    LetterboxTransform,
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxOutputContractError,
    OpenCVFrameSource,
)


class _Input:
    name = "images"
    shape = [1, 3, 640, 640]


class _Session:
    def __init__(self, output: object) -> None:
        self.output = output
        self.received = None

    def get_inputs(self):
        return [_Input()]

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, _outputs, feeds):
        self.received = feeds
        return [self.output]


def detector(output: object, *, threshold: float = 0.25) -> OnnxNx6Detector:
    return OnnxNx6Detector(
        OnnxNx6Config(
            model_path=Path("fake.onnx"),
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
            confidence_threshold=threshold,
        ),
        session=_Session(output),
    )


def test_post_nms_nx6_is_adapted_to_canonical_detection() -> None:
    model = detector(np.array([[[64, 128, 320, 512, 0.9, 0]]], dtype=np.float32))
    image = np.zeros((640, 640, 3), dtype=np.uint8)

    (result,) = model.detect(image)

    assert result.label == "flame"
    assert result.confidence == pytest.approx(0.9)
    assert result.bbox == BoundingBox(0.1, 0.2, 0.5, 0.8)
    assert model.provider_names == ("CPUExecutionProvider",)


def test_confidence_filter_runs_before_legacy_adapter() -> None:
    model = detector(np.array([[64, 128, 320, 512, 0.2, 0]], dtype=np.float32), threshold=0.25)

    assert model.detect(np.zeros((640, 640, 3), dtype=np.uint8)) == ()


def test_non_nx6_onnx_output_is_rejected() -> None:
    model = detector(np.zeros((1, 3, 7), dtype=np.float32))

    with pytest.raises(OnnxOutputContractError, match="Nx6"):
        model.detect(np.zeros((640, 640, 3), dtype=np.uint8))


def test_letterbox_transform_restores_source_coordinates() -> None:
    transform = LetterboxTransform(
        source_width=1280,
        source_height=720,
        input_width=640,
        input_height=640,
        scale=0.5,
        pad_x=0,
        pad_y=140,
    )

    assert transform.map_input_xyxy_to_source((100, 190, 300, 390)) == (200, 100, 600, 500)


def test_ensemble_reports_person_safety_class_coverage() -> None:
    fire = detector(np.empty((0, 6), dtype=np.float32))
    safety = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=Path("safety.onnx"),
            class_names=("person", "firefighter"),
            input_width=640,
            input_height=640,
        ),
        session=_Session(np.empty((0, 6), dtype=np.float32)),
    )
    ensemble = DetectorEnsemble((fire, safety))

    assert ensemble.covers_labels(("person", "firefighter")) is True
    assert ensemble.covers_labels(("person", "vehicle")) is False


def test_capture_config_identifies_rtsp_and_validates_transport() -> None:
    assert CaptureConfig("rtsp://camera/live").is_rtsp is True
    assert CaptureConfig(0).is_rtsp is False
    with pytest.raises(ValueError, match="rtsp_transport"):
        CaptureConfig(0, rtsp_transport="srt")
    with pytest.raises(ValueError, match="backend"):
        CaptureConfig(0, backend="v4l2")
    with pytest.raises(ValueError, match="reconnect attempts"):
        CaptureConfig(0, reconnect_attempts=-1)


class _Capture:
    def __init__(self, image) -> None:
        self.image = image
        self.released = False

    def isOpened(self) -> bool:
        return not self.released

    def set(self, _key, _value) -> bool:
        return True

    def read(self):
        return (self.image is not None, self.image)

    def release(self) -> None:
        self.released = True


class _CV2:
    CAP_ANY = 0
    CAP_DSHOW = 1
    CAP_MSMF = 2
    CAP_FFMPEG = 3
    CAP_PROP_FRAME_WIDTH = 4
    CAP_PROP_FRAME_HEIGHT = 5
    CAP_PROP_FPS = 6
    CAP_PROP_BUFFERSIZE = 7

    def __init__(self, captures: list[_Capture]) -> None:
        self.captures = captures

    def VideoCapture(self, _source, _backend):
        return self.captures.pop(0)


def test_frame_source_reconnects_without_accumulating_stale_frames(monkeypatch) -> None:
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2 = _CV2([_Capture(None), _Capture(image)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(CaptureConfig(0, reconnect_attempts=1, reconnect_delay_seconds=0))

    frame = source.read()

    assert frame.width == 640
    assert frame.height == 480
    assert source.reconnect_count == 1
    source.close()


def test_rtsp_open_error_does_not_expose_credentials(monkeypatch) -> None:
    capture = _Capture(None)
    capture.released = True
    cv2 = _CV2([capture])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(
        CaptureConfig("rtsp://SECRET_USER:SECRET_PASSWORD@camera.invalid/stream")
    )

    with pytest.raises(CameraReadError) as captured_error:
        source.open()

    message = str(captured_error.value)
    assert "RTSP source" in message
    assert "SECRET_USER" not in message
    assert "SECRET_PASSWORD" not in message
