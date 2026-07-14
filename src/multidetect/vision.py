from __future__ import annotations

import math
import os
import queue
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

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
    rtsp_codec: str = "h265"
    backend: str = "auto"
    gstreamer_hardware_decode: bool = False
    gstreamer_latency_ms: int = 100
    reconnect_delay_seconds: float = 0.25
    reconnect_attempts: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.source, bool) or not isinstance(self.source, (int, str)):
            raise ValueError("capture source must be a camera index or path/URL")
        if self.width is not None and (
            isinstance(self.width, bool) or not isinstance(self.width, int) or self.width <= 0
        ):
            raise ValueError("capture width must be positive")
        if self.height is not None and (
            isinstance(self.height, bool) or not isinstance(self.height, int) or self.height <= 0
        ):
            raise ValueError("capture height must be positive")
        if self.fps is not None and (
            isinstance(self.fps, bool) or not math.isfinite(self.fps) or self.fps <= 0
        ):
            raise ValueError("capture fps must be finite and positive")
        if self.rtsp_transport not in {"tcp", "udp"}:
            raise ValueError("rtsp_transport must be tcp or udp")
        if self.rtsp_codec not in {"h264", "h265"}:
            raise ValueError("rtsp_codec must be h264 or h265")
        if self.backend not in {"auto", "dshow", "msmf", "ffmpeg", "gstreamer"}:
            raise ValueError("capture backend must be auto, dshow, msmf, ffmpeg, or gstreamer")
        if self.backend == "gstreamer" and not self.is_rtsp:
            raise ValueError("gstreamer capture backend currently requires an RTSP source")
        if self.gstreamer_hardware_decode and self.backend != "gstreamer":
            raise ValueError("gstreamer hardware decode requires the gstreamer backend")
        if (
            isinstance(self.gstreamer_latency_ms, bool)
            or not isinstance(self.gstreamer_latency_ms, int)
            or self.gstreamer_latency_ms < 0
        ):
            raise ValueError("gstreamer latency must be a non-negative integer")
        if (
            isinstance(self.reconnect_delay_seconds, bool)
            or not math.isfinite(self.reconnect_delay_seconds)
            or self.reconnect_delay_seconds < 0
        ):
            raise ValueError("reconnect delay must be finite and non-negative")
        if (
            isinstance(self.reconnect_attempts, bool)
            or not isinstance(self.reconnect_attempts, int)
            or self.reconnect_attempts < 0
        ):
            raise ValueError("reconnect attempts must be a non-negative integer")

    @property
    def is_rtsp(self) -> bool:
        return isinstance(self.source, str) and self.source.lower().startswith("rtsp://")

    @property
    def redacted_source_description(self) -> str:
        if self.is_rtsp:
            return "RTSP source"
        if isinstance(self.source, int):
            return f"local camera index {self.source}"
        return "local video source"


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    frame_id: str
    captured_at_s: float
    image_bgr: Any
    width: int
    height: int


class FrameSource(Protocol):
    """Small camera-source boundary used by the live mission loop."""

    def open(self) -> None: ...

    def read(self) -> CapturedFrame: ...

    def close(self) -> None: ...


class OpenCVFrameSource:
    """Low-latency local-device or RTSP reader with a one-frame capture buffer."""

    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self._capture: Any | None = None
        self._frame_index = 0
        self._reconnect_count = 0

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def open(self) -> None:
        if self._capture is not None and self._capture.isOpened():
            return
        cv2 = _require_cv2()
        if self.config.is_rtsp and self.config.backend == "gstreamer":
            pipeline = _build_gstreamer_rtsp_pipeline(self.config)
            capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        elif self.config.is_rtsp:
            # FFmpeg options are read while opening the stream. Keep only transport
            # policy here; credentials stay inside the supplied RTSP URI.
            option_name = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
            previous_options = os.environ.get(option_name)
            os.environ[option_name] = f"rtsp_transport;{self.config.rtsp_transport}"
            try:
                capture = cv2.VideoCapture(self.config.source, cv2.CAP_FFMPEG)
            finally:
                if previous_options is None:
                    os.environ.pop(option_name, None)
                else:
                    os.environ[option_name] = previous_options
        else:
            auto_backend = (
                cv2.CAP_DSHOW
                if self.config.backend == "auto"
                and os.name == "nt"
                and isinstance(self.config.source, int)
                else cv2.CAP_ANY
            )
            backend = {
                "auto": auto_backend,
                "dshow": cv2.CAP_DSHOW,
                "msmf": cv2.CAP_MSMF,
                "ffmpeg": cv2.CAP_FFMPEG,
                "gstreamer": cv2.CAP_GSTREAMER,
            }[self.config.backend]
            capture = cv2.VideoCapture(self.config.source, backend)
        gstreamer_rtsp = self.config.is_rtsp and self.config.backend == "gstreamer"
        if not gstreamer_rtsp:
            if self.config.width is not None:
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
            if self.config.height is not None:
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
            if self.config.fps is not None:
                capture.set(cv2.CAP_PROP_FPS, self.config.fps)
            # The GStreamer RTSP pipeline already owns its caps and bounded appsink.
            # Calling set() after it starts can renegotiate and halt the live stream.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not capture.isOpened():
            capture.release()
            raise CameraReadError(f"unable to open {self.config.redacted_source_description}")
        self._capture = capture

    def close(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.release()

    def read(self) -> CapturedFrame:
        image = None
        for attempt in range(self.config.reconnect_attempts + 1):
            try:
                self.open()
            except CameraReadError:
                ok = False
            else:
                capture = self._capture
                if capture is None:  # Defensive guard for optimized Python and subclasses.
                    raise CameraReadError("camera source failed to initialize")
                ok, image = capture.read()
            if ok and image is not None:
                break
            self.close()
            if attempt >= self.config.reconnect_attempts:
                break
            self._reconnect_count += 1
            if self.config.reconnect_delay_seconds > 0:
                time.sleep(self.config.reconnect_delay_seconds)
        if not ok or image is None:
            raise CameraReadError(
                f"{self.config.redacted_source_description} returned no frame after "
                f"{self.config.reconnect_attempts} reconnect attempts"
            )
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


class BufferedFrameSource:
    """Capture frames on a dedicated thread into a bounded, ordered FIFO.

    The producer blocks when the FIFO is full instead of intentionally discarding
    an older frame. ``backpressure_count`` makes sustained overload observable;
    upstream RTSP components can still discard data when their own latency bounds
    are exceeded, so a non-zero value must be treated as a real-time capacity warning.
    """

    def __init__(
        self,
        source: FrameSource,
        *,
        capacity: int = 4,
        startup_timeout_seconds: float = 15.0,
        read_timeout_seconds: float = 5.0,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capture queue capacity must be a positive integer")
        if not math.isfinite(startup_timeout_seconds) or startup_timeout_seconds <= 0:
            raise ValueError("capture startup timeout must be finite and positive")
        if not math.isfinite(read_timeout_seconds) or read_timeout_seconds <= 0:
            raise ValueError("capture read timeout must be finite and positive")
        self._source = source
        self.capacity = capacity
        self.startup_timeout_seconds = startup_timeout_seconds
        self.read_timeout_seconds = read_timeout_seconds
        self._frames: queue.Queue[CapturedFrame] = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._worker: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._metrics_lock = threading.Lock()
        self._captured_frame_count = 0
        self._delivered_frame_count = 0
        self._queue_high_watermark = 0
        self._backpressure_count = 0

    @property
    def reconnect_count(self) -> int:
        return int(getattr(self._source, "reconnect_count", 0))

    @property
    def captured_frame_count(self) -> int:
        with self._metrics_lock:
            return self._captured_frame_count

    @property
    def delivered_frame_count(self) -> int:
        with self._metrics_lock:
            return self._delivered_frame_count

    @property
    def queue_high_watermark(self) -> int:
        with self._metrics_lock:
            return self._queue_high_watermark

    @property
    def backpressure_count(self) -> int:
        with self._metrics_lock:
            return self._backpressure_count

    def open(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._frames = queue.Queue(maxsize=self.capacity)
        self._stop.clear()
        self._ready.clear()
        self._failure = None
        with self._metrics_lock:
            self._captured_frame_count = 0
            self._delivered_frame_count = 0
            self._queue_high_watermark = 0
            self._backpressure_count = 0
        self._worker = threading.Thread(
            target=self._capture_loop,
            name="multi-detect-camera-capture",
            daemon=True,
        )
        self._worker.start()
        if not self._ready.wait(self.startup_timeout_seconds):
            self.close()
            raise CameraReadError("buffered camera source did not start before its timeout")
        if self._failure is not None:
            failure = self._failure
            self.close()
            raise CameraReadError(
                f"buffered camera source failed to start: {type(failure).__name__}"
            ) from failure

    def read(self) -> CapturedFrame:
        self.open()
        deadline = time.monotonic() + self.read_timeout_seconds
        while True:
            try:
                frame = self._frames.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                if self._failure is not None and self._frames.empty():
                    raise CameraReadError(
                        f"buffered camera worker stopped after {type(self._failure).__name__}"
                    ) from self._failure
                if time.monotonic() >= deadline:
                    raise CameraReadError(
                        "buffered camera source returned no frame before timeout"
                    ) from None
                continue
            with self._metrics_lock:
                self._delivered_frame_count += 1
            return frame

    def close(self) -> None:
        self._stop.set()
        worker = self._worker
        if worker is None:
            self._source.close()
            return
        worker.join(timeout=min(2.0, self.read_timeout_seconds))
        if worker.is_alive():
            # A backend can occasionally block inside capture.read(). Releasing it
            # is the only bounded shutdown path; OpenCVFrameSource.close is idempotent.
            self._source.close()
            worker.join(timeout=1.0)
        self._worker = None

    def _capture_loop(self) -> None:
        try:
            self._source.open()
        except BaseException as exc:  # The failure is re-raised on the caller thread.
            self._failure = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            while not self._stop.is_set():
                try:
                    frame = self._source.read()
                except BaseException as exc:  # The failure is re-raised on the caller thread.
                    self._failure = exc
                    return
                with self._metrics_lock:
                    self._captured_frame_count += 1
                encountered_backpressure = False
                while not self._stop.is_set():
                    try:
                        self._frames.put(frame, timeout=0.05)
                    except queue.Full:
                        if not encountered_backpressure:
                            with self._metrics_lock:
                                self._backpressure_count += 1
                            encountered_backpressure = True
                        continue
                    with self._metrics_lock:
                        self._queue_high_watermark = max(
                            self._queue_high_watermark,
                            self._frames.qsize(),
                        )
                    break
        finally:
            self._source.close()

    def __enter__(self) -> BufferedFrameSource:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _build_gstreamer_rtsp_pipeline(config: CaptureConfig) -> str:
    if not config.is_rtsp or not isinstance(config.source, str):
        raise ValueError("GStreamer RTSP pipeline requires an RTSP source")
    if any(ord(character) < 32 for character in config.source):
        raise ValueError("RTSP source cannot contain control characters")
    quoted_source = '"' + config.source.replace("\\", "\\\\").replace('"', '\\"') + '"'
    codec = config.rtsp_codec
    depayloader = f"rtp{codec}depay"
    parser = f"{codec}parse"
    if config.gstreamer_hardware_decode:
        if codec == "h265":
            parser = (
                "h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au"
            )
        decode_chain = (
            "nvv4l2decoder ! nvvidconv ! video/x-raw,format=BGRx ! "
            "videoconvert ! video/x-raw,format=BGR"
        )
    else:
        decode_chain = f"avdec_{codec} ! videoconvert ! video/x-raw,format=BGR"
    return (
        f"rtspsrc location={quoted_source} protocols={config.rtsp_transport} "
        f"latency={config.gstreamer_latency_ms} drop-on-latency=true ! "
        f"{depayloader} ! {parser} ! {decode_chain} ! "
        "appsink drop=true max-buffers=1 sync=false"
    )


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
    trt_engine_cache_path: Path | None = None
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
            if config.model_path.suffix.lower() in {".engine", ".plan"}:
                from .tensorrt_session import TensorRtNx6Session

                session = TensorRtNx6Session(config.model_path)
            else:
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
                configured_providers: list[str | tuple[str, dict[str, str | bool]]] = []
                for provider in providers:
                    if (
                        provider == "TensorrtExecutionProvider"
                        and self.config.trt_engine_cache_path is not None
                    ):
                        cache_path = self.config.trt_engine_cache_path
                        cache_path.mkdir(parents=True, exist_ok=True)
                        configured_providers.append(
                            (
                                provider,
                                {
                                    "trt_engine_cache_enable": True,
                                    "trt_engine_cache_path": str(cache_path),
                                },
                            )
                        )
                    else:
                        configured_providers.append(provider)
                session = ort.InferenceSession(
                    str(config.model_path),
                    providers=configured_providers,
                )
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

    def warmup(self, *, iterations: int = 1) -> None:
        """Initialize the execution provider before live capture starts."""

        if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
            raise ValueError("warmup iterations must be a non-negative integer")
        tensor = self._np.zeros(
            (1, 3, self._input_height, self._input_width),
            dtype=self._np.float32,
        )
        for _ in range(iterations):
            outputs = self._session.run(None, {self._input_name: tensor})
            if not outputs:
                raise OnnxOutputContractError("ONNX session returned no outputs during warmup")
            self._as_nx6_rows(outputs[0])

    def infer_nx6(
        self, image_bgr: Any
    ) -> tuple[tuple[float, float, float, float, float, float], ...]:
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
        resized = cv2.resize(
            image_bgr,
            (resized_width, resized_height),
            interpolation=cv2.INTER_LINEAR,
        )
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


class ClassConfidenceFilter:
    """Applies class-specific candidate thresholds after Nx6 adaptation."""

    def __init__(
        self,
        detector: Any,
        thresholds: dict[str, float],
        *,
        default_threshold: float | None = 0.0,
    ) -> None:
        normalized: dict[str, float] = {}
        for label, threshold in thresholds.items():
            key = label.strip().lower()
            if not key:
                raise ValueError("class confidence threshold label cannot be empty")
            if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
                raise ValueError("class confidence thresholds must be in [0, 1]")
            normalized[key] = threshold
        self.detector = detector
        self.thresholds = normalized
        if default_threshold is not None and (
            not math.isfinite(default_threshold) or not 0.0 <= default_threshold <= 1.0
        ):
            raise ValueError("default_threshold must be in [0, 1] or None")
        self.default_threshold = default_threshold

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        kept: list[Detection] = []
        for detection in self.detector.detect(image_bgr):
            threshold = self.thresholds.get(detection.label.strip().lower(), self.default_threshold)
            if threshold is not None and detection.confidence >= threshold:
                kept.append(detection)
        return tuple(kept)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)


class BrightNeutralLightVetoFilter:
    """Rejects compact white lamps/reflections that lack flame-like color texture."""

    def __init__(
        self,
        detector: Any,
        *,
        labels: frozenset[str] = frozenset({"fire", "flame"}),
        minimum_bright_neutral_fraction: float = 0.20,
        maximum_colorful_fraction: float = 0.02,
        bright_value_threshold: int = 235,
        neutral_saturation_threshold: int = 35,
        colorful_saturation_threshold: int = 80,
    ) -> None:
        for name, value in (
            ("minimum_bright_neutral_fraction", minimum_bright_neutral_fraction),
            ("maximum_colorful_fraction", maximum_colorful_fraction),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        self.detector = detector
        self.labels = frozenset(label.strip().lower() for label in labels)
        self.minimum_bright_neutral_fraction = minimum_bright_neutral_fraction
        self.maximum_colorful_fraction = maximum_colorful_fraction
        self.bright_value_threshold = bright_value_threshold
        self.neutral_saturation_threshold = neutral_saturation_threshold
        self.colorful_saturation_threshold = colorful_saturation_threshold

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        if not detections:
            return detections
        cv2 = _require_cv2()
        hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
        height, width = image_bgr.shape[:2]
        kept: list[Detection] = []
        for detection in detections:
            if detection.label not in self.labels:
                kept.append(detection)
                continue
            x1 = max(0, min(width - 1, round(detection.bbox.x1 * width)))
            y1 = max(0, min(height - 1, round(detection.bbox.y1 * height)))
            x2 = max(x1 + 1, min(width, round(detection.bbox.x2 * width)))
            y2 = max(y1 + 1, min(height, round(detection.bbox.y2 * height)))
            roi = hsv[y1:y2, x1:x2]
            saturation = roi[:, :, 1]
            value = roi[:, :, 2]
            bright_neutral_fraction = float(
                (
                    (value >= self.bright_value_threshold)
                    & (saturation <= self.neutral_saturation_threshold)
                ).mean()
            )
            colorful_fraction = float((saturation >= self.colorful_saturation_threshold).mean())
            is_neutral_light = (
                bright_neutral_fraction >= self.minimum_bright_neutral_fraction
                and colorful_fraction <= self.maximum_colorful_fraction
            )
            if not is_neutral_light:
                kept.append(detection)
        return tuple(kept)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)


class PersonOverlapVetoFilter:
    """Suppresses ambiguous fire candidates substantially covered by a detected person."""

    def __init__(
        self,
        detector: Any,
        *,
        fire_labels: frozenset[str] = frozenset({"fire", "flame", "smoke"}),
        person_labels: frozenset[str] = frozenset({"person", "firefighter"}),
        minimum_fire_coverage: float = 0.4,
        person_margin: float = 0.02,
    ) -> None:
        if not 0.0 <= minimum_fire_coverage <= 1.0:
            raise ValueError("minimum_fire_coverage must be in [0, 1]")
        if not math.isfinite(person_margin) or person_margin < 0.0:
            raise ValueError("person_margin must be finite and non-negative")
        self.detector = detector
        self.fire_labels = frozenset(label.strip().lower() for label in fire_labels)
        self.person_labels = frozenset(label.strip().lower() for label in person_labels)
        self.minimum_fire_coverage = minimum_fire_coverage
        self.person_margin = person_margin

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        people = tuple(
            detection.bbox.expanded(self.person_margin)
            for detection in detections
            if detection.label in self.person_labels
        )
        if not people:
            return detections
        return tuple(
            detection
            for detection in detections
            if detection.label not in self.fire_labels
            or not any(
                self._coverage(detection.bbox, person_bbox) >= self.minimum_fire_coverage
                for person_bbox in people
            )
        )

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)

    @staticmethod
    def _coverage(candidate: Any, person: Any) -> float:
        x1 = max(candidate.x1, person.x1)
        y1 = max(candidate.y1, person.y1)
        x2 = min(candidate.x2, person.x2)
        y2 = min(candidate.y2, person.y2)
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        return intersection / candidate.area if candidate.area > 0.0 else 0.0


@dataclass(slots=True)
class _TemporalCandidate:
    detection: Detection
    consecutive_frames: int
    missed_frames: int = 0


class TemporalDetectionFilter:
    """Requires selected classes to remain spatially stable for consecutive frames."""

    def __init__(
        self,
        detector: Any,
        *,
        labels: frozenset[str],
        minimum_consecutive_frames: int = 3,
        iou_threshold: float = 0.25,
        maximum_missed_frames: int = 1,
    ) -> None:
        if minimum_consecutive_frames <= 0:
            raise ValueError("minimum_consecutive_frames must be positive")
        if not 0.0 < iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be in (0, 1]")
        if maximum_missed_frames < 0:
            raise ValueError("maximum_missed_frames cannot be negative")
        self.detector = detector
        self.labels = frozenset(label.strip().lower() for label in labels)
        self.minimum_consecutive_frames = minimum_consecutive_frames
        self.iou_threshold = iou_threshold
        self.maximum_missed_frames = maximum_missed_frames
        self._candidates: list[_TemporalCandidate] = []

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        immediate = [detection for detection in detections if detection.label not in self.labels]
        filtered = sorted(
            (detection for detection in detections if detection.label in self.labels),
            key=lambda detection: detection.confidence,
            reverse=True,
        )
        unmatched = set(range(len(self._candidates)))
        next_candidates: list[_TemporalCandidate] = []
        stable: list[Detection] = []
        for detection in filtered:
            matches = [
                (self._candidates[index].detection.bbox.iou(detection.bbox), index)
                for index in unmatched
                if self._candidates[index].detection.label == detection.label
            ]
            overlap, index = max(matches, default=(0.0, -1))
            if index >= 0 and overlap >= self.iou_threshold:
                unmatched.remove(index)
                state = _TemporalCandidate(
                    detection,
                    self._candidates[index].consecutive_frames + 1,
                )
            else:
                state = _TemporalCandidate(detection, 1)
            next_candidates.append(state)
            if state.consecutive_frames >= self.minimum_consecutive_frames:
                stable.append(detection)
        for index in unmatched:
            state = self._candidates[index]
            state.missed_frames += 1
            if state.missed_frames <= self.maximum_missed_frames:
                next_candidates.append(state)
        self._candidates = next_candidates
        return tuple(immediate + stable)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)
