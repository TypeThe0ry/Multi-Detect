from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

import multidetect.tensorrt_session as tensorrt_session_module
import multidetect.vision as vision_module
from multidetect.domain import BoundingBox, Detection, SensorKind
from multidetect.vision import (
    BrightNeutralLightVetoFilter,
    CameraReadError,
    CaptureConfig,
    ClassConfidenceFilter,
    DetectorEnsemble,
    LetterboxTransform,
    OnnxNx6Config,
    OnnxNx6Detector,
    OnnxOutputContractError,
    OpenCVFrameSource,
    PersonOverlapVetoFilter,
    TemporalDetectionFilter,
)


def test_class_confidence_filter_uses_stricter_flame_threshold() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("flame", 0.24, BoundingBox(0.1, 0.1, 0.2, 0.2), SensorKind.RGB),
                Detection("flame", 0.42, BoundingBox(0.2, 0.2, 0.3, 0.3), SensorKind.RGB),
                Detection("smoke", 0.24, BoundingBox(0.3, 0.3, 0.4, 0.4), SensorKind.RGB),
            )

        def covers_labels(self, labels):
            return set(labels).issubset({"flame", "smoke"})

    filtered = ClassConfidenceFilter(_Detector(), {"flame": 0.35, "smoke": 0.20})

    results = filtered.detect(object())

    assert tuple((item.label, item.confidence) for item in results) == (
        ("flame", 0.42),
        ("smoke", 0.24),
    )
    assert filtered.covers_labels(("flame",)) is True


def test_person_overlap_veto_suppresses_ambiguous_flame_but_keeps_person() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("person", 0.9, BoundingBox(0.1, 0.1, 0.5, 0.9)),
                Detection("flame", 0.8, BoundingBox(0.2, 0.2, 0.4, 0.6)),
                Detection("smoke", 0.7, BoundingBox(0.7, 0.1, 0.9, 0.3)),
            )

        def covers_labels(self, _labels):
            return True

    filtered = PersonOverlapVetoFilter(_Detector())

    results = filtered.detect(object())

    assert tuple(item.label for item in results) == ("person", "smoke")


def test_bright_neutral_light_veto_rejects_white_lamp_but_keeps_colored_flame() -> None:
    class _Detector:
        def detect(self, _image):
            return (
                Detection("flame", 0.8, BoundingBox(0.0, 0.0, 0.5, 1.0)),
                Detection("flame", 0.8, BoundingBox(0.5, 0.0, 1.0, 1.0)),
            )

        def covers_labels(self, _labels):
            return True

    image = np.zeros((40, 80, 3), dtype=np.uint8)
    image[:, :40] = (255, 255, 255)
    image[:, 40:] = (0, 100, 255)
    filtered = BrightNeutralLightVetoFilter(_Detector())

    results = filtered.detect(image)

    assert len(results) == 1
    assert results[0].bbox == BoundingBox(0.5, 0.0, 1.0, 1.0)


def test_temporal_filter_requires_three_spatially_consistent_fire_frames() -> None:
    class _Detector:
        def __init__(self):
            self.frame = 0

        def detect(self, _image):
            self.frame += 1
            return (
                Detection("person", 0.9, BoundingBox(0.6, 0.1, 0.9, 0.9)),
                Detection(
                    "flame",
                    0.8,
                    BoundingBox(0.1 + self.frame * 0.001, 0.1, 0.3, 0.4),
                ),
            )

        def covers_labels(self, _labels):
            return True

    filtered = TemporalDetectionFilter(
        _Detector(),
        labels=frozenset({"flame", "smoke"}),
        minimum_consecutive_frames=3,
    )

    assert tuple(item.label for item in filtered.detect(object())) == ("person",)
    assert tuple(item.label for item in filtered.detect(object())) == ("person",)
    assert tuple(item.label for item in filtered.detect(object())) == ("person", "flame")


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


def test_detector_warmup_initializes_provider_with_static_input() -> None:
    model = detector(np.empty((1, 0, 6), dtype=np.float32))

    model.warmup(iterations=2)

    received = model._session.received
    assert received is not None
    assert received["images"].shape == (1, 3, 640, 640)
    assert received["images"].dtype == np.float32


def test_tensor_engine_path_uses_direct_tensorrt_session(monkeypatch) -> None:
    created: list[Path] = []

    class _TensorRtSession(_Session):
        def __init__(self, path: Path) -> None:
            created.append(path)
            super().__init__(np.empty((1, 0, 6), dtype=np.float32))

        def get_providers(self):
            return ["TensorrtExecutionProvider"]

    monkeypatch.setattr(tensorrt_session_module, "TensorRtNx6Session", _TensorRtSession)
    model = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=Path("fire.engine"),
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
        )
    )

    assert created == [Path("fire.engine")]
    assert model.provider_names == ("TensorrtExecutionProvider",)
    assert model.detect(np.zeros((640, 640, 3), dtype=np.uint8)) == ()


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
    assert CaptureConfig("rtsp://camera/live", backend="gstreamer").rtsp_codec == "h265"
    with pytest.raises(ValueError, match="rtsp_transport"):
        CaptureConfig(0, rtsp_transport="srt")
    with pytest.raises(ValueError, match="backend"):
        CaptureConfig(0, backend="v4l2")
    with pytest.raises(ValueError, match="requires an RTSP source"):
        CaptureConfig(0, backend="gstreamer")
    with pytest.raises(ValueError, match="hardware decode requires"):
        CaptureConfig("rtsp://camera/live", gstreamer_hardware_decode=True)
    with pytest.raises(ValueError, match="rtsp_codec"):
        CaptureConfig("rtsp://camera/live", rtsp_codec="vp9")
    with pytest.raises(ValueError, match="gstreamer latency"):
        CaptureConfig("rtsp://camera/live", gstreamer_latency_ms=-1)
    with pytest.raises(ValueError, match="reconnect attempts"):
        CaptureConfig(0, reconnect_attempts=-1)
    with pytest.raises(ValueError, match="fps must be finite"):
        CaptureConfig(0, fps=float("nan"))
    with pytest.raises(ValueError, match="reconnect delay must be finite"):
        CaptureConfig(0, reconnect_delay_seconds=float("inf"))
    with pytest.raises(ValueError, match="source must be a camera index"):
        CaptureConfig(True)


class _Capture:
    def __init__(self, image) -> None:
        self.image = image
        self.released = False
        self.set_calls: list[tuple[object, object]] = []

    def isOpened(self) -> bool:
        return not self.released

    def set(self, key, value) -> bool:
        self.set_calls.append((key, value))
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
    CAP_GSTREAMER = 8
    CAP_PROP_FRAME_WIDTH = 4
    CAP_PROP_FRAME_HEIGHT = 5
    CAP_PROP_FPS = 6
    CAP_PROP_BUFFERSIZE = 7

    def __init__(self, captures: list[_Capture]) -> None:
        self.captures = captures
        self.calls: list[tuple[object, int]] = []

    def VideoCapture(self, source, backend):
        self.calls.append((source, backend))
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


def test_rtsp_open_restores_process_ffmpeg_options(monkeypatch) -> None:
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2 = _CV2([_Capture(image)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "existing;value")
    source = OpenCVFrameSource(CaptureConfig("rtsp://camera.invalid/stream"))

    source.open()

    assert vision_module.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "existing;value"
    source.close()


def test_rtsp_gstreamer_h265_hardware_pipeline_is_bounded(monkeypatch) -> None:
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2 = _CV2([_Capture(image)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(
        CaptureConfig(
            "rtsp://camera.invalid/stream=0",
            backend="gstreamer",
            rtsp_codec="h265",
            gstreamer_hardware_decode=True,
            gstreamer_latency_ms=80,
        )
    )

    frame = source.read()

    pipeline, backend = cv2.calls[0]
    assert backend == cv2.CAP_GSTREAMER
    assert "protocols=tcp" in pipeline
    assert "latency=80" in pipeline
    assert (
        "rtph265depay ! h265parse config-interval=-1 ! "
        "video/x-h265,stream-format=byte-stream,alignment=au ! nvv4l2decoder" in pipeline
    )
    assert "appsink drop=true max-buffers=1 sync=false" in pipeline
    assert source._capture is not None
    assert source._capture.set_calls == []
    assert frame.width == 1280
    assert frame.height == 720
    source.close()


def test_rtsp_gstreamer_software_pipeline_rejects_control_characters(monkeypatch) -> None:
    cv2 = _CV2([_Capture(None)])
    monkeypatch.setattr(vision_module, "_require_cv2", lambda: cv2)
    source = OpenCVFrameSource(
        CaptureConfig("rtsp://camera.invalid/stream\nattack", backend="gstreamer")
    )

    with pytest.raises(ValueError, match="control characters"):
        source.open()


def test_buffered_frame_source_preserves_fifo_order_and_reports_backpressure() -> None:
    class _FastSource:
        reconnect_count = 2

        def __init__(self) -> None:
            self.index = 0
            self.closed = False

        def open(self) -> None:
            pass

        def read(self):
            self.index += 1
            return vision_module.CapturedFrame(
                frame_id=f"frame-{self.index}",
                captured_at_s=time.monotonic(),
                image_bgr=None,
                width=640,
                height=480,
            )

        def close(self) -> None:
            self.closed = True

    inner = _FastSource()
    source = vision_module.BufferedFrameSource(inner, capacity=2)
    source.open()
    deadline = time.monotonic() + 1.0
    while source.backpressure_count == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    frames = [source.read(), source.read(), source.read()]
    source.close()

    assert [frame.frame_id for frame in frames] == ["frame-1", "frame-2", "frame-3"]
    assert source.reconnect_count == 2
    assert source.queue_high_watermark == 2
    assert source.backpressure_count >= 1
    assert source.captured_frame_count >= 3
    assert source.delivered_frame_count == 3
    assert inner.closed is True


def test_buffered_frame_source_propagates_worker_failure_without_secret_text() -> None:
    class _FailingSource:
        def open(self) -> None:
            pass

        def read(self):
            raise RuntimeError("SECRET camera address")

        def close(self) -> None:
            pass

    source = vision_module.BufferedFrameSource(_FailingSource(), capacity=2)

    with pytest.raises(CameraReadError) as captured_error:
        source.read()

    assert "RuntimeError" in str(captured_error.value)
    assert "SECRET" not in str(captured_error.value)
    source.close()
