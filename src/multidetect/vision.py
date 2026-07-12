from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .adapters.fire_smoke_legacy import adapt_yolov5_detections
from .domain import Detection


class VisionDependencyError(RuntimeError):
    """Raised when a live-vision optional dependency is not installed."""


class CameraReadError(RuntimeError):
    """Raised when a local or RTSP source cannot deliver a frame."""


class OnnxOutputContractError(RuntimeError):
    """Raised when a model does not expose the required post-NMS Nx6 contract."""


def _require_cv2() -> Any:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise VisionDependencyError(
            "Install live vision dependencies: pip install -e '.[vision]'"
        ) from exc
    return cv2


def _require_numpy() -> Any:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise VisionDependencyError(
            "Install live vision dependencies: pip install -e '.[vision]'"
        ) from exc
    return np


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    source: int | str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    rtsp_transport: str = "tcp"
    reconnect_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if self.width is not None and self.width <= 0:
            raise ValueError("capture width must be positive")
        if self.height is not None and self.height <= 0:
            raise ValueError("capture height must be positive")
        if self.fps is not None and self.fps <= 0:
            raise ValueError("capture fps must be positive")
        if self.rtsp_transport not in {"tcp", "udp"}:
            raise ValueError("rtsp_transport must be tcp or udp")
        if self.reconnect_delay_seconds < 0:
            raise ValueError("reconnect delay cannot be negative")

    @property
    def is_rtsp(self) -> bool:
        return isinstance(self.source, str) and self.source.lower().startswith("rtsp://")


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    frame_id: str
    captured_at_s: float
    image_bgr: Any
    width: int
    height: int


class OpenCVFrameSource:
    """Low-latency local-device or RTSP reader with a one-frame capture buffer."""

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self._capture: Any | None = None
        self._frame_index = 0

    def open(self) -> None:
        if self._capture is not None and self._capture.isOpened():
            return
        cv2 = _require_cv2()
        if self.config.is_rtsp:
            # FFmpeg options are read while opening the stream. Keep only transport
            # policy here; credentials stay inside the supplied RTSP URI.
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                f"rtsp_transport;{self.config.rtsp_transport}"
            )
            capture = cv2.VideoCapture(self.config.source, cv2.CAP_FFMPEG)
        else:
            capture = cv2.VideoCapture(self.config.source)
        if self.config.width is not None:
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        if self.config.height is not None:
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        if self.config.fps is not None:
            capture.set(cv2.CAP_PROP_FPS, self.config.fps)
        # A bounded buffer drops stale frames rather than accumulating latency. Some
        # backends ignore this setting, so the live runner still processes one frame at a time.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            raise CameraReadError(f"unable to open video source: {self.config.source!r}")
        self._capture = capture

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()

    def read(self) -> CapturedFrame:
        self.open()
        assert self._capture is not None
        ok, image = self._capture.read()
        if not ok or image is None:
            self.close()
            if self.config.reconnect_delay_seconds:
                time.sleep(self.config.reconnect_delay_seconds)
            self.open()
            assert self._capture is not None
            ok, image = self._capture.read()
        if not ok or image is None:
            raise CameraReadError(f"video source returned no frame: {self.config.source!r}")
        height, width = image.shape[:2]
        self._frame_index += 1
        return CapturedFrame(
            frame_id=f"live-{self._frame_index:09d}",
            captured_at_s=time.monotonic(),
            image_bgr=image,
            width=width,
            height=height,
        )

    def __enter__(self) -> OpenCVFrameSource:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class LetterboxTransform:
    source_width: int
    source_height: int
    input_width: int
    input_height: int
    scale: float
    pad_x: float
    pad_y: float

    def map_input_xyxy_to_source(self, row: Sequence[float]) -> tuple[float, float, float, float]:
        if len(row) < 4:
            raise ValueError("Nx6 row must contain four coordinates")
        x1, y1, x2, y2 = (float(value) for value in row[:4])
        return (
            min(self.source_width, max(0.0, (x1 - self.pad_x) / self.scale)),
            min(self.source_height, max(0.0, (y1 - self.pad_y) / self.scale)),
            min(self.source_width, max(0.0, (x2 - self.pad_x) / self.scale)),
            min(self.source_height, max(0.0, (y2 - self.pad_y) / self.scale)),
        )


@dataclass(frozen=True, slots=True)
class OnnxNx6Config:
    model_path: Path
    class_names: tuple[str, ...]
    input_width: int | None = None
    input_height: int | None = None
    confidence_threshold: float = 0.25
    providers: tuple[str, ...] = ()
    model_version: str | None = None
    output_coordinates: str = "letterbox_xyxy_px"

    def __post_init__(self) -> None:
        if not self.class_names:
            raise ValueError("ONNX model needs at least one class name")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold must be in [0, 1]")
        if self.input_width is not None and self.input_width <= 0:
            raise ValueError("input_width must be positive")
        if self.input_height is not None and self.input_height <= 0:
            raise ValueError("input_height must be positive")
        if self.output_coordinates not in {"letterbox_xyxy_px", "normalized_xyxy"}:
            raise ValueError("output_coordinates must be letterbox_xyxy_px or normalized_xyxy")


class OnnxNx6Detector:
    """ONNX detector with a deliberately strict, post-NMS ``N x 6`` output boundary."""

    def __init__(self, config: OnnxNx6Config, *, session: Any | None = None) -> None:
        self.config = config
        self._np = _require_numpy()
        if session is None:
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - dependency-specific.
                raise VisionDependencyError(
                    "Install ONNX Runtime: pip install -e '.[vision]'"
                ) from exc
            available = set(ort.get_available_providers())
            requested = self.config.providers or (
                "TensorrtExecutionProvider",
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            )
            providers = [provider for provider in requested if provider in available]
            if not providers:
                raise VisionDependencyError(
                    "No requested ONNX Runtime provider is available; "
                    f"available={sorted(available)}"
                )
            session = ort.InferenceSession(str(config.model_path), providers=providers)
        self._session = session
        input_meta = self._session.get_inputs()[0]
        self._input_name = input_meta.name
        self._input_width, self._input_height = self._resolve_input_dimensions(input_meta.shape)

    @property
    def class_names(self) -> tuple[str, ...]:
        return self.config.class_names

    @property
    def provider_names(self) -> tuple[str, ...]:
        if hasattr(self._session, "get_providers"):
            return tuple(self._session.get_providers())
        return ()

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        rows = self.infer_nx6(image_bgr)
        height, width = image_bgr.shape[:2]
        return adapt_yolov5_detections(
            rows,
            image_width=width,
            image_height=height,
            class_names=self.config.class_names,
            model_version=self.config.model_version or self.config.model_path.name,
        )

    def infer_nx6(self, image_bgr: Any) -> tuple[tuple[float, float, float, float, float, float], ...]:
        tensor, transform = self._preprocess(image_bgr)
        outputs = self._session.run(None, {self._input_name: tensor})
        if not outputs:
            raise OnnxOutputContractError("ONNX session returned no outputs")
        rows = self._as_nx6_rows(outputs[0])
        normalized_rows: list[tuple[float, float, float, float, float, float]] = []
        for raw_row in rows:
            x1, y1, x2, y2, confidence, class_id = (float(value) for value in raw_row)
            if not all(math.isfinite(value) for value in (x1, y1, x2, y2, confidence, class_id)):
                continue
            if confidence < self.config.confidence_threshold:
                continue
            if self.config.output_coordinates == "normalized_xyxy":
                x1 *= transform.input_width
                x2 *= transform.input_width
                y1 *= transform.input_height
                y2 *= transform.input_height
            x1, y1, x2, y2 = transform.map_input_xyxy_to_source((x1, y1, x2, y2))
            if x2 <= x1 or y2 <= y1:
                continue
            normalized_rows.append((x1, y1, x2, y2, confidence, class_id))
        return tuple(normalized_rows)

    def _resolve_input_dimensions(self, shape: Sequence[Any]) -> tuple[int, int]:
        if len(shape) != 4:
            raise OnnxOutputContractError("only NCHW image inputs are supported")
        channels = shape[1]
        if isinstance(channels, int) and channels != 3:
            raise OnnxOutputContractError("ONNX input must have three image channels")
        width = self.config.input_width or (shape[3] if isinstance(shape[3], int) else None)
        height = self.config.input_height or (shape[2] if isinstance(shape[2], int) else None)
        if not isinstance(width, int) or not isinstance(height, int):
            raise OnnxOutputContractError(
                "dynamic ONNX image dimensions require --input-width and --input-height"
            )
        return width, height

    def _preprocess(self, image_bgr: Any) -> tuple[Any, LetterboxTransform]:
        cv2 = _require_cv2()
        np = self._np
        source_height, source_width = image_bgr.shape[:2]
        scale = min(self._input_width / source_width, self._input_height / source_height)
        resized_width = max(1, round(source_width * scale))
        resized_height = max(1, round(source_height * scale))
        resized = cv2.resize(image_bgr, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
        pad_x = (self._input_width - resized_width) / 2.0
        pad_y = (self._input_height - resized_height) / 2.0
        canvas = np.full((self._input_height, self._input_width, 3), 114, dtype=np.uint8)
        left, top = int(math.floor(pad_x)), int(math.floor(pad_y))
        canvas[top : top + resized_height, left : left + resized_width] = resized
        rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0)
        return tensor, LetterboxTransform(
            source_width=source_width,
            source_height=source_height,
            input_width=self._input_width,
            input_height=self._input_height,
            scale=scale,
            pad_x=left,
            pad_y=top,
        )

    def _as_nx6_rows(self, output: Any) -> Any:
        array = self._np.asarray(output)
        if array.ndim == 3 and array.shape[0] == 1:
            array = array[0]
        if array.ndim != 2 or array.shape[1] != 6:
            raise OnnxOutputContractError(
                f"expected post-NMS Nx6 output, received shape {tuple(array.shape)}"
            )
        return array


class DetectorEnsemble:
    """Runs independent post-NMS ONNX models and concatenates candidate detections."""

    def __init__(self, detectors: Sequence[OnnxNx6Detector]) -> None:
        if not detectors:
            raise ValueError("DetectorEnsemble needs at least one detector")
        self.detectors = tuple(detectors)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        return tuple(
            detection for detector in self.detectors for detection in detector.detect(image_bgr)
        )

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        available = {
            "flame" if label.strip().lower() == "fire" else label.strip().lower()
            for detector in self.detectors
            for label in detector.class_names
        }
        return set(label.strip().lower() for label in required_labels).issubset(available)

