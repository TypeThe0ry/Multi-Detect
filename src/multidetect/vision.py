from __future__ import annotations

import math
import os
import queue
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from .adapters.fire_smoke_legacy import adapt_yolov5_detections
from .assignment import rectangular_linear_assignment
from .domain import BoundingBox, Detection


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
        if self.is_synthetic and self.backend != "auto":
            raise ValueError("synthetic capture source requires the auto backend")
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
    def is_synthetic(self) -> bool:
        return isinstance(self.source, str) and self.source == "synthetic://patrol"

    @property
    def redacted_source_description(self) -> str:
        if self.is_rtsp:
            return "RTSP source"
        if self.is_synthetic:
            return "deterministic synthetic patrol source"
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


class SyntheticFrameSource:
    """Clock-paced deterministic scene for software HIL without camera access.

    The source is intentionally infinite and contains moving fire/smoke-like regions,
    a person-shaped region, a vehicle-shaped region, and a periodic occluder.  It is
    not an accuracy dataset: its purpose is to exercise capture timing, queueing,
    tracking and SITL mission plumbing without opening a local device or network URL.
    """

    def __init__(self, config: CaptureConfig) -> None:
        if not config.is_synthetic:
            raise ValueError("SyntheticFrameSource requires synthetic://patrol")
        self.config = config
        self._width = config.width or 640
        self._height = config.height or 480
        self._fps = config.fps or 30.0
        self._period_s = 1.0 / self._fps
        self._frame_index = 0
        self._next_frame_at_s: float | None = None

    @property
    def reconnect_count(self) -> int:
        return 0

    def open(self) -> None:
        if self._next_frame_at_s is None:
            self._next_frame_at_s = time.monotonic()

    def close(self) -> None:
        self._next_frame_at_s = None

    def read(self) -> CapturedFrame:
        self.open()
        deadline = self._next_frame_at_s
        if deadline is None:  # Defensive guard for optimized Python.
            raise CameraReadError("synthetic source failed to initialize")
        now_s = time.monotonic()
        if deadline > now_s:
            time.sleep(deadline - now_s)
        captured_at_s = time.monotonic()
        self._next_frame_at_s = max(deadline + self._period_s, captured_at_s + self._period_s)
        self._frame_index += 1
        image = self._render(self._frame_index)
        return CapturedFrame(
            frame_id=f"synthetic-{self._frame_index:09d}",
            captured_at_s=captured_at_s,
            image_bgr=image,
            width=self._width,
            height=self._height,
        )

    def _render(self, frame_index: int) -> Any:
        np = _require_numpy()
        height, width = self._height, self._width
        image = np.empty((height, width, 3), dtype=np.uint8)
        vertical = np.linspace(24, 70, height, dtype=np.uint8)[:, None]
        image[:, :, 0] = vertical
        image[:, :, 1] = np.minimum(vertical + 24, 255)
        image[:, :, 2] = np.minimum(vertical + 10, 255)

        # A deterministic feature field represents distant terrain texture.  It
        # gives sparse optical flow enough well-distributed corners to validate
        # its CLEAR/CAUTION/AVOID state machine without reading any camera.
        marker_size = max(4, min(width, height) // 60)
        step_x = max(24, width // 16)
        step_y = max(24, height // 12)
        for row, marker_y in enumerate(range(step_y // 2, height, step_y)):
            for column, marker_x in enumerate(range(step_x // 2, width, step_x)):
                marker_value = 178 if (row + column) % 2 == 0 else 92
                image[
                    marker_y : min(height, marker_y + marker_size),
                    marker_x : min(width, marker_x + marker_size),
                ] = (marker_value, marker_value, marker_value)

        phase = frame_index % 240
        vehicle_x = int((phase / 239.0) * max(1, width - width // 5))
        vehicle_y = int(height * 0.68)
        image[vehicle_y : vehicle_y + max(4, height // 12), vehicle_x : vehicle_x + width // 5] = (
            185,
            92,
            35,
        )

        person_x = int(width * 0.72)
        person_y = int(height * 0.38)
        image[person_y : person_y + height // 4, person_x : person_x + max(3, width // 28)] = (
            210,
            150,
            65,
        )

        fire_x = int(width * 0.28 + math.sin(frame_index * 0.17) * width * 0.02)
        fire_y = int(height * 0.58)
        fire_w = max(6, width // 10)
        fire_h = max(6, height // 7)
        image[fire_y : fire_y + fire_h, fire_x : fire_x + fire_w] = (18, 105, 245)
        image[
            max(0, fire_y - fire_h // 2) : fire_y,
            fire_x + fire_w // 4 : fire_x + 3 * fire_w // 4,
        ] = (128, 134, 140)

        # A moving opaque band creates a repeatable short occlusion window.
        if 80 <= phase < 115:
            occluder_x = int(width * 0.18 + (phase - 80) / 35.0 * width * 0.28)
            image[:, occluder_x : occluder_x + max(8, width // 18)] = (32, 32, 32)
        return image

    def __enter__(self) -> SyntheticFrameSource:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def frame_source_from_config(config: CaptureConfig) -> FrameSource:
    """Select the only source implementation authorized by the explicit URI."""

    if config.is_synthetic:
        return SyntheticFrameSource(config)
    return OpenCVFrameSource(config)


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


@dataclass(frozen=True, slots=True)
class OnnxRawYoloConfig:
    """Traditional Ultralytics detect head with host-side class-aware NMS.

    This contract deliberately avoids end-to-end ``TopK``/NMS nodes so an ONNX
    exported with ``end2end=False, nms=False`` remains buildable by older target
    TensorRT runtimes such as JetPack 5 / TensorRT 8.6.
    """

    model_path: Path
    class_names: tuple[str, ...]
    input_width: int | None = None
    input_height: int | None = None
    confidence_threshold: float = 0.25
    iou_threshold: float = 0.45
    maximum_detections: int = 300
    providers: tuple[str, ...] = ()
    trt_engine_cache_path: Path | None = None
    model_version: str | None = None

    def __post_init__(self) -> None:
        if not self.class_names:
            raise ValueError("raw YOLO model needs at least one class name")
        for name, value in (
            ("confidence_threshold", self.confidence_threshold),
            ("iou_threshold", self.iou_threshold),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.input_width is not None and self.input_width <= 0:
            raise ValueError("input_width must be positive")
        if self.input_height is not None and self.input_height <= 0:
            raise ValueError("input_height must be positive")
        if (
            isinstance(self.maximum_detections, bool)
            or not isinstance(self.maximum_detections, int)
            or self.maximum_detections <= 0
        ):
            raise ValueError("maximum_detections must be a positive integer")


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


class OnnxRawYoloDetector(OnnxNx6Detector):
    """Ultralytics ``1 x (4 + classes) x anchors`` detector with local NMS."""

    def __init__(self, config: OnnxRawYoloConfig, *, session: Any | None = None) -> None:
        self.raw_config = config
        if session is None and config.model_path.suffix.lower() in {".engine", ".plan"}:
            from .tensorrt_session import TensorRtRawYoloSession

            session = TensorRtRawYoloSession(
                config.model_path,
                class_count=len(config.class_names),
            )
        super().__init__(
            OnnxNx6Config(
                model_path=config.model_path,
                class_names=config.class_names,
                input_width=config.input_width,
                input_height=config.input_height,
                confidence_threshold=config.confidence_threshold,
                providers=config.providers,
                trt_engine_cache_path=config.trt_engine_cache_path,
                model_version=config.model_version,
            ),
            session=session,
        )

    def warmup(self, *, iterations: int = 1) -> None:
        if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
            raise ValueError("warmup iterations must be a non-negative integer")
        tensor = self._np.zeros(
            (1, 3, self._input_height, self._input_width),
            dtype=self._np.float32,
        )
        for _ in range(iterations):
            outputs = self._session.run(None, {self._input_name: tensor})
            if not outputs:
                raise OnnxOutputContractError("raw YOLO session returned no outputs during warmup")
            self._as_raw_predictions(outputs[0])

    def infer_nx6(
        self, image_bgr: Any
    ) -> tuple[tuple[float, float, float, float, float, float], ...]:
        tensor, transform = self._preprocess(image_bgr)
        outputs = self._session.run(None, {self._input_name: tensor})
        if not outputs:
            raise OnnxOutputContractError("raw YOLO session returned no outputs")
        predictions = self._as_raw_predictions(outputs[0])
        if predictions.shape[0] == 0:
            return ()

        boxes_xywh = predictions[:, :4]
        class_scores = predictions[:, 4:]
        class_ids = self._np.argmax(class_scores, axis=1)
        confidences = class_scores[self._np.arange(class_scores.shape[0]), class_ids]
        keep = confidences >= self.raw_config.confidence_threshold
        if not self._np.any(keep):
            return ()
        boxes_xywh = boxes_xywh[keep]
        confidences = confidences[keep]
        class_ids = class_ids[keep]

        boxes = self._np.empty_like(boxes_xywh)
        boxes[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2.0
        boxes[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2.0
        boxes[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2.0
        boxes[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2.0
        selected = self._class_aware_nms(boxes, confidences, class_ids)

        rows: list[tuple[float, float, float, float, float, float]] = []
        for index in selected:
            x1, y1, x2, y2 = transform.map_input_xyxy_to_source(boxes[index])
            if x2 <= x1 or y2 <= y1:
                continue
            rows.append(
                (
                    x1,
                    y1,
                    x2,
                    y2,
                    float(confidences[index]),
                    float(class_ids[index]),
                )
            )
        return tuple(rows)

    def _as_raw_predictions(self, output: Any) -> Any:
        array = self._np.asarray(output)
        if array.ndim != 3 or array.shape[0] != 1:
            raise OnnxOutputContractError(
                f"expected raw YOLO batch-1 rank-3 output, received shape {tuple(array.shape)}"
            )
        feature_count = 4 + len(self.class_names)
        array = array[0]
        if array.shape[0] == feature_count:
            array = array.T
        elif array.shape[1] != feature_count:
            raise OnnxOutputContractError(
                "raw YOLO output must contain exactly 4 box values plus one score per class"
            )
        return array

    def _class_aware_nms(self, boxes: Any, scores: Any, class_ids: Any) -> tuple[int, ...]:
        selected: list[int] = []
        for class_id in sorted(int(value) for value in self._np.unique(class_ids)):
            indices = self._np.flatnonzero(class_ids == class_id)
            order = indices[self._np.argsort(-scores[indices], kind="stable")]
            while order.size and len(selected) < self.raw_config.maximum_detections:
                current = int(order[0])
                selected.append(current)
                if order.size == 1:
                    break
                rest = order[1:]
                left = self._np.maximum(boxes[current, 0], boxes[rest, 0])
                top = self._np.maximum(boxes[current, 1], boxes[rest, 1])
                right = self._np.minimum(boxes[current, 2], boxes[rest, 2])
                bottom = self._np.minimum(boxes[current, 3], boxes[rest, 3])
                intersection = self._np.maximum(0.0, right - left) * self._np.maximum(
                    0.0, bottom - top
                )
                current_area = max(
                    0.0,
                    float(
                        (boxes[current, 2] - boxes[current, 0])
                        * (boxes[current, 3] - boxes[current, 1])
                    ),
                )
                rest_area = self._np.maximum(
                    0.0, boxes[rest, 2] - boxes[rest, 0]
                ) * self._np.maximum(0.0, boxes[rest, 3] - boxes[rest, 1])
                union = current_area + rest_area - intersection
                iou = self._np.divide(
                    intersection,
                    union,
                    out=self._np.zeros_like(intersection),
                    where=union > 0.0,
                )
                order = rest[iou <= self.raw_config.iou_threshold]
        selected.sort(key=lambda index: (-float(scores[index]), int(class_ids[index]), index))
        return tuple(selected[: self.raw_config.maximum_detections])


@dataclass(frozen=True, slots=True)
class TiledDetectionConfig:
    """Periodic overlapping tile scan fused with the normal full-frame detector pass."""

    columns: int = 2
    rows: int = 1
    overlap_fraction: float = 0.15
    scan_interval_frames: int = 3
    fusion_iou_threshold: float = 0.30
    tile_confidence_threshold: float = 0.40
    tile_confidence_by_label: Mapping[str, float] = field(default_factory=dict)
    tile_labels: frozenset[str] = frozenset()
    maximum_tile_box_area: float = 0.04
    maximum_detections: int = 300

    def __post_init__(self) -> None:
        for name, value, minimum in (
            ("columns", self.columns, 1),
            ("rows", self.rows, 1),
            ("scan_interval_frames", self.scan_interval_frames, 1),
            ("maximum_detections", self.maximum_detections, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if self.columns * self.rows > 16:
            raise ValueError("tiled detection supports at most 16 tiles")
        if not math.isfinite(self.overlap_fraction) or not 0.0 <= self.overlap_fraction < 0.5:
            raise ValueError("overlap_fraction must be in [0, 0.5)")
        if (
            not math.isfinite(self.fusion_iou_threshold)
            or not 0.0 < self.fusion_iou_threshold <= 1.0
        ):
            raise ValueError("fusion_iou_threshold must be in (0, 1]")
        if (
            not math.isfinite(self.tile_confidence_threshold)
            or not 0.0 <= self.tile_confidence_threshold <= 1.0
        ):
            raise ValueError("tile_confidence_threshold must be in [0, 1]")
        normalized_confidence_by_label: dict[str, float] = {}
        for raw_label, raw_threshold in self.tile_confidence_by_label.items():
            label = str(raw_label).strip().lower()
            threshold = float(raw_threshold)
            if not label:
                raise ValueError("tile confidence label overrides cannot be empty")
            if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
                raise ValueError("tile confidence label overrides must be in [0, 1]")
            normalized_confidence_by_label[label] = threshold
        if (
            not math.isfinite(self.maximum_tile_box_area)
            or not 0.0 < self.maximum_tile_box_area <= 1.0
        ):
            raise ValueError("maximum_tile_box_area must be in (0, 1]")
        normalized_labels = frozenset(
            label.strip().lower() for label in self.tile_labels if label.strip()
        )
        object.__setattr__(self, "tile_labels", normalized_labels)
        object.__setattr__(
            self,
            "tile_confidence_by_label",
            MappingProxyType(normalized_confidence_by_label),
        )


class TiledDetectionFusion:
    """Recover small distant objects without replacing the low-latency full-frame pass.

    The wrapped detector still runs once on every frame.  On a bounded schedule it also
    scans overlapping crops, maps their normalized boxes back to the source image, and
    performs confidence-ranked same-class suppression.  The first call always includes
    a tile scan so startup does not wait for the interval.
    """

    def __init__(self, detector: Any, config: TiledDetectionConfig | None = None) -> None:
        self.detector = detector
        self.config = config or TiledDetectionConfig()
        self._frame_number = 0

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(self.detector.class_names)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(getattr(self.detector, "provider_names", ()))

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        available = {
            "flame" if label.strip().lower() == "fire" else label.strip().lower()
            for label in self.class_names
        }
        return {
            "flame" if label.strip().lower() == "fire" else label.strip().lower()
            for label in required_labels
        }.issubset(available)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        if not hasattr(image_bgr, "shape") or len(image_bgr.shape) < 2:
            raise ValueError("tiled detection requires an image array")
        image_height, image_width = image_bgr.shape[:2]
        if image_width <= 0 or image_height <= 0:
            raise ValueError("tiled detection image cannot be empty")
        self._frame_number += 1
        detections = list(self.detector.detect(image_bgr))
        tile_count = self.config.columns * self.config.rows
        scan_tiles = tile_count > 1 and (
            (self._frame_number - 1) % self.config.scan_interval_frames == 0
        )
        if scan_tiles:
            for tile_index, (x0, y0, x1, y1) in enumerate(
                self._tile_windows(image_width, image_height)
            ):
                crop = image_bgr[y0:y1, x0:x1]
                for detection in self.detector.detect(crop):
                    label = detection.label.strip().lower()
                    required_confidence = self.config.tile_confidence_by_label.get(
                        label,
                        self.config.tile_confidence_threshold,
                    )
                    if detection.confidence < required_confidence:
                        continue
                    if self.config.tile_labels and label not in self.config.tile_labels:
                        continue
                    mapped = self._map_tile_detection(
                        detection,
                        tile_index=tile_index,
                        x0=x0,
                        y0=y0,
                        x1=x1,
                        y1=y1,
                        image_width=image_width,
                        image_height=image_height,
                    )
                    if (
                        mapped.bbox.area <= self.config.maximum_tile_box_area + 1e-12
                        and self._owns_tile_center(mapped.bbox, tile_index)
                    ):
                        detections.append(mapped)
        return self._fuse(detections)

    def _owns_tile_center(self, bbox: BoundingBox, tile_index: int) -> bool:
        """Assign overlap-area detections to one nominal tile to avoid duplicates."""

        row, column = divmod(tile_index, self.config.columns)
        center_x, center_y = bbox.center
        x0 = column / self.config.columns
        x1 = (column + 1) / self.config.columns
        y0 = row / self.config.rows
        y1 = (row + 1) / self.config.rows
        owns_x = x0 <= center_x and (center_x < x1 or column == self.config.columns - 1)
        owns_y = y0 <= center_y and (center_y < y1 or row == self.config.rows - 1)
        return owns_x and owns_y

    def _tile_windows(
        self, image_width: int, image_height: int
    ) -> tuple[tuple[int, int, int, int], ...]:
        windows: list[tuple[int, int, int, int]] = []
        overlap = self.config.overlap_fraction
        for row in range(self.config.rows):
            nominal_y0 = row / self.config.rows
            nominal_y1 = (row + 1) / self.config.rows
            margin_y = (nominal_y1 - nominal_y0) * overlap / 2.0
            normalized_y0 = max(0.0, nominal_y0 - margin_y)
            normalized_y1 = min(1.0, nominal_y1 + margin_y)
            y0 = max(0, min(image_height - 1, math.floor(normalized_y0 * image_height)))
            y1 = max(y0 + 1, min(image_height, math.ceil(normalized_y1 * image_height)))
            for column in range(self.config.columns):
                nominal_x0 = column / self.config.columns
                nominal_x1 = (column + 1) / self.config.columns
                margin_x = (nominal_x1 - nominal_x0) * overlap / 2.0
                normalized_x0 = max(0.0, nominal_x0 - margin_x)
                normalized_x1 = min(1.0, nominal_x1 + margin_x)
                x0 = max(0, min(image_width - 1, math.floor(normalized_x0 * image_width)))
                x1 = max(x0 + 1, min(image_width, math.ceil(normalized_x1 * image_width)))
                windows.append((x0, y0, x1, y1))
        return tuple(windows)

    @staticmethod
    def _map_tile_detection(
        detection: Detection,
        *,
        tile_index: int,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        image_width: int,
        image_height: int,
    ) -> Detection:
        tile_width = x1 - x0
        tile_height = y1 - y0
        bbox = detection.bbox
        mapped = BoundingBox(
            max(0.0, min(1.0, (x0 + bbox.x1 * tile_width) / image_width)),
            max(0.0, min(1.0, (y0 + bbox.y1 * tile_height) / image_height)),
            max(0.0, min(1.0, (x0 + bbox.x2 * tile_width) / image_width)),
            max(0.0, min(1.0, (y0 + bbox.y2 * tile_height) / image_height)),
        )
        metadata = dict(detection.metadata)
        metadata.update(
            {
                "tiled_detection": True,
                "tile_index": tile_index,
                "tile_xyxy_px": (x0, y0, x1, y1),
            }
        )
        return Detection(
            label=detection.label,
            confidence=detection.confidence,
            bbox=mapped,
            sensor=detection.sensor,
            model_version=detection.model_version,
            metadata=metadata,
        )

    def _fuse(self, detections: Sequence[Detection]) -> tuple[Detection, ...]:
        remaining = sorted(
            detections,
            key=lambda item: (-item.confidence, item.label, item.bbox.x1, item.bbox.y1),
        )
        fused: list[Detection] = []
        while remaining and len(fused) < self.config.maximum_detections:
            seed = remaining.pop(0)
            cluster = [seed]
            unclustered: list[Detection] = []
            for candidate in remaining:
                if (
                    candidate.label == seed.label
                    and seed.bbox.iou(candidate.bbox) >= self.config.fusion_iou_threshold
                ):
                    cluster.append(candidate)
                else:
                    unclustered.append(candidate)
            remaining = unclustered
            fused.append(self._fuse_cluster(cluster))
        fused.sort(key=lambda item: (-item.confidence, item.label, item.bbox.x1, item.bbox.y1))
        return tuple(fused)

    @staticmethod
    def _fuse_cluster(cluster: Sequence[Detection]) -> Detection:
        best = max(cluster, key=lambda item: item.confidence)
        if len(cluster) == 1:
            return best
        metadata = dict(best.metadata)
        metadata.update(
            {
                "tiled_fusion_count": len(cluster),
                "tiled_detection": any(
                    bool(item.metadata.get("tiled_detection")) for item in cluster
                ),
            }
        )
        return Detection(
            label=best.label,
            confidence=best.confidence,
            bbox=best.bbox,
            sensor=best.sensor,
            model_version=best.model_version,
            metadata=metadata,
        )


class FrameCadencedDetector:
    """Run a detector on one phase of a fixed frame cadence.

    Skipped frames intentionally emit no repeated boxes. The unified target pool and
    short-term tracker own prediction between fresh detector observations.
    """

    def __init__(self, detector: Any, *, frame_stride: int = 1, frame_phase: int = 0) -> None:
        if isinstance(frame_stride, bool) or not isinstance(frame_stride, int):
            raise ValueError("frame_stride must be an integer")
        if frame_stride <= 0:
            raise ValueError("frame_stride must be positive")
        if isinstance(frame_phase, bool) or not isinstance(frame_phase, int):
            raise ValueError("frame_phase must be an integer")
        if not 0 <= frame_phase < frame_stride:
            raise ValueError("frame_phase must be in [0, frame_stride)")
        self.detector = detector
        self.frame_stride = frame_stride
        self.frame_phase = frame_phase
        self.frame_index = 0
        self.inference_count = 0
        self.skipped_count = 0
        self._force_every_frame = False

    @property
    def force_every_frame(self) -> bool:
        return self._force_every_frame

    def set_force_every_frame(self, enabled: bool) -> None:
        self._force_every_frame = bool(enabled)

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(self.detector.class_names)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(getattr(self.detector, "provider_names", ()))

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        scheduled = (
            self._force_every_frame or self.frame_index % self.frame_stride == self.frame_phase
        )
        self.frame_index += 1
        if not scheduled:
            self.skipped_count += 1
            return ()
        self.inference_count += 1
        return tuple(self.detector.detect(image_bgr))


def _set_detector_force_every_frame(detector: Any, enabled: bool) -> bool:
    setter = getattr(detector, "set_force_every_frame", None)
    if callable(setter):
        setter(enabled)
        return True
    child = getattr(detector, "detector", None)
    if child is not None and _set_detector_force_every_frame(child, enabled):
        return True
    return any(
        _set_detector_force_every_frame(nested, enabled)
        for nested in getattr(detector, "detectors", ())
    )


class DetectorEnsemble:
    """Runs independent post-NMS ONNX models and concatenates candidate detections."""

    def __init__(
        self,
        detectors: Sequence[Any],
        *,
        force_locked_cadence: bool = True,
    ) -> None:
        if not detectors:
            raise ValueError("DetectorEnsemble needs at least one detector")
        if not isinstance(force_locked_cadence, bool):
            raise ValueError("force_locked_cadence must be boolean")
        self.detectors = tuple(detectors)
        # A selected target can remain visually continuous through scheduled
        # detector gaps via the short-term tracker. Keeping the old opt-in
        # forced-inference policy available is useful for diagnostic runs, but
        # the live Jetson profile can retain each detector's staggered cadence
        # so the ground overlay receives more fresh tracker frames.
        self.force_locked_cadence = force_locked_cadence
        self._active_labels: frozenset[str] | None = None

    @property
    def active_labels(self) -> frozenset[str] | None:
        """Labels requested by the current LCK route, or ``None`` in DET/TRK."""

        return self._active_labels

    @property
    def active_detector_count(self) -> int:
        return len(self._routed_detectors())

    def set_active_labels(self, labels: Sequence[str] | None) -> int:
        """Select the most class-specialized detectors for a locked family.

        ``None`` restores the normal multi-class DET/TRK ensemble.  An empty
        sequence deliberately pauses learned detectors so the arbitrary-object
        optical-flow/template tracker owns an unclassified LCK target.
        """

        if labels is None:
            self._active_labels = None
        else:
            self._active_labels = frozenset(
                str(label).strip().lower() for label in labels if str(label).strip()
            )
        routed_detector_ids = {id(detector) for detector in self._routed_detectors()}
        force_locked_cadence = self.force_locked_cadence and bool(self._active_labels)
        for detector in self.detectors:
            _set_detector_force_every_frame(
                detector,
                force_locked_cadence and id(detector) in routed_detector_ids,
            )
        return self.active_detector_count

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        active_labels = self._active_labels
        return tuple(
            detection
            for detector in self._routed_detectors()
            for detection in detector.detect(image_bgr)
            if active_labels is None or detection.label.strip().lower() in active_labels
        )

    def _routed_detectors(self) -> tuple[Any, ...]:
        active_labels = self._active_labels
        if active_labels is None:
            return self.detectors
        if not active_labels:
            return ()
        matching = tuple(
            detector
            for detector in self.detectors
            if active_labels.intersection(
                str(label).strip().lower() for label in detector.class_names
            )
        )
        # LCK is an exclusive target mode, not an exclusive-model gamble.  Keep
        # every model that covers the locked semantic family and force all of them
        # to fresh-frame cadence.  Selecting only the smallest class domain dropped
        # the COCO detector whenever the aerial VisDrone model was present; close
        # indoor people then vanished immediately after entering LCK even though
        # COCO had been detecting them reliably in DET/TRK.
        return matching

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        available = {
            "flame" if label.strip().lower() == "fire" else label.strip().lower()
            for detector in self.detectors
            for label in detector.class_names
        }
        return set(label.strip().lower() for label in required_labels).issubset(available)


class LabelRemapDetector:
    """Map source-model labels into runtime semantic families and suppress duplicates."""

    def __init__(
        self,
        detector: Any,
        label_map: dict[str, str],
        *,
        fusion_iou_threshold: float = 0.45,
    ) -> None:
        normalized: dict[str, str] = {}
        for source, destination in label_map.items():
            source_label = source.strip().lower()
            destination_label = destination.strip().lower()
            if not source_label or not destination_label:
                raise ValueError("label remap entries cannot be empty")
            normalized[source_label] = destination_label
        if not math.isfinite(fusion_iou_threshold) or not 0.0 < fusion_iou_threshold <= 1.0:
            raise ValueError("fusion_iou_threshold must be in (0, 1]")
        self.detector = detector
        self.label_map = normalized
        self.fusion_iou_threshold = fusion_iou_threshold

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.label_map.get(label.strip().lower(), label.strip().lower())
                for label in self.detector.class_names
            )
        )

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(getattr(self.detector, "provider_names", ()))

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        available = set(self.class_names)
        return {label.strip().lower() for label in required_labels}.issubset(available)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        remapped: list[Detection] = []
        for detection in self.detector.detect(image_bgr):
            source_label = detection.label.strip().lower()
            destination_label = self.label_map.get(source_label, source_label)
            metadata = dict(detection.metadata)
            metadata["source_label"] = source_label
            remapped.append(
                Detection(
                    label=destination_label,
                    confidence=detection.confidence,
                    bbox=detection.bbox,
                    sensor=detection.sensor,
                    model_version=detection.model_version,
                    metadata=metadata,
                )
            )
        return _same_label_nms(
            remapped,
            iou_threshold=self.fusion_iou_threshold,
            maximum_detections=len(remapped),
        )


class SameLabelDetectionFusion:
    """Suppress overlapping same-label detections emitted by detector ensembles."""

    def __init__(
        self,
        detector: Any,
        *,
        iou_threshold: float = 0.45,
        maximum_detections: int = 300,
    ) -> None:
        if not math.isfinite(iou_threshold) or not 0.0 < iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be in (0, 1]")
        if maximum_detections <= 0:
            raise ValueError("maximum_detections must be positive")
        self.detector = detector
        self.iou_threshold = iou_threshold
        self.maximum_detections = maximum_detections

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        return _same_label_nms(
            self.detector.detect(image_bgr),
            iou_threshold=self.iou_threshold,
            maximum_detections=self.maximum_detections,
        )

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)


def _same_label_nms(
    detections: Sequence[Detection],
    *,
    iou_threshold: float,
    maximum_detections: int,
) -> tuple[Detection, ...]:
    remaining = sorted(
        detections,
        key=lambda item: (-item.confidence, item.label, item.bbox.x1, item.bbox.y1),
    )
    selected: list[Detection] = []
    while remaining and len(selected) < maximum_detections:
        best = remaining.pop(0)
        selected.append(best)
        remaining = [
            candidate
            for candidate in remaining
            if candidate.label != best.label or best.bbox.iou(candidate.bbox) < iou_threshold
        ]
    return tuple(selected)


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

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(self.detector.class_names)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(getattr(self.detector, "provider_names", ()))

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        kept: list[Detection] = []
        for detection in self.detector.detect(image_bgr):
            threshold = self.thresholds.get(detection.label.strip().lower(), self.default_threshold)
            if threshold is not None and detection.confidence >= threshold:
                kept.append(detection)
        return tuple(kept)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)


class LabelAllowListFilter:
    """Keeps only labels eligible for automatic target presentation.

    This runs after filters that need broad common-model context (for example,
    furniture evidence used to reject a false ``car``), but before detections
    can enter the automatic target pool.  Manual operator selections are
    injected separately and are intentionally unaffected by this filter.
    """

    def __init__(self, detector: Any, *, labels: frozenset[str]) -> None:
        normalized = frozenset(label.strip().lower() for label in labels if label.strip())
        if not normalized:
            raise ValueError("automatic candidate labels must be non-empty")
        self.detector = detector
        self.labels = normalized

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(
            label
            for label in getattr(self.detector, "class_names", ())
            if label.strip().lower() in self.labels
        )

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(getattr(self.detector, "provider_names", ()))

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        return tuple(
            detection
            for detection in self.detector.detect(image_bgr)
            if detection.label.strip().lower() in self.labels
        )

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        normalized = frozenset(label.strip().lower() for label in required_labels if label.strip())
        return normalized.issubset(self.labels) and self.detector.covers_labels(required_labels)


class BrightNeutralLightVetoFilter:
    """Reject neutral lamps and optional color-only warm graphics from flame candidates."""

    def __init__(
        self,
        detector: Any,
        *,
        labels: frozenset[str] = frozenset({"fire", "flame"}),
        minimum_bright_neutral_fraction: float = 0.20,
        maximum_colorful_fraction: float = 0.02,
        minimum_bright_warm_fraction: float = 0.0,
        bright_value_threshold: int = 235,
        neutral_saturation_threshold: int = 35,
        colorful_saturation_threshold: int = 80,
    ) -> None:
        for name, value in (
            ("minimum_bright_neutral_fraction", minimum_bright_neutral_fraction),
            ("maximum_colorful_fraction", maximum_colorful_fraction),
            ("minimum_bright_warm_fraction", minimum_bright_warm_fraction),
        ):
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        self.detector = detector
        self.labels = frozenset(label.strip().lower() for label in labels)
        self.minimum_bright_neutral_fraction = minimum_bright_neutral_fraction
        self.maximum_colorful_fraction = maximum_colorful_fraction
        self.minimum_bright_warm_fraction = minimum_bright_warm_fraction
        self.bright_value_threshold = bright_value_threshold
        self.neutral_saturation_threshold = neutral_saturation_threshold
        self.colorful_saturation_threshold = colorful_saturation_threshold

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        if not detections:
            return detections
        # Most live frames contain only people, furniture, or vehicles.  Avoid a
        # full-frame BGR→HSV conversion unless the detector actually proposed a
        # fire candidate, then convert only that compact candidate ROI.
        if not any(detection.label in self.labels for detection in detections):
            return detections
        cv2 = _require_cv2()
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
            roi_bgr = image_bgr[y1:y2, x1:x2]
            if roi_bgr.size == 0:
                kept.append(detection)
                continue
            roi = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
            hue = roi[:, :, 0]
            saturation = roi[:, :, 1]
            value = roi[:, :, 2]
            bright_neutral_fraction = float(
                (
                    (value >= self.bright_value_threshold)
                    & (saturation <= self.neutral_saturation_threshold)
                ).mean()
            )
            colorful_fraction = float((saturation >= self.colorful_saturation_threshold).mean())
            warm_fraction = float(
                (
                    (saturation >= self.colorful_saturation_threshold)
                    & ((hue <= 35) | (hue >= 170))
                ).mean()
            )
            bright_warm_fraction = float(
                (
                    (value >= self.bright_value_threshold)
                    & (saturation >= self.colorful_saturation_threshold)
                    & ((hue <= 35) | (hue >= 170))
                ).mean()
            )
            is_neutral_light = (
                bright_neutral_fraction >= self.minimum_bright_neutral_fraction
                and colorful_fraction <= self.maximum_colorful_fraction
            )
            has_bright_warm_evidence = (
                bright_warm_fraction >= self.minimum_bright_warm_fraction
            )
            if not is_neutral_light and has_bright_warm_evidence:
                # These scalar diagnostics are intentionally attached only to
                # candidates that survive the neutral-light veto.  Prediction
                # JSONL can retain them without recording pixels, which lets a
                # later offline review distinguish warm illumination, graphics,
                # and genuinely flame-like color texture before thresholds are
                # changed on an active camera feed.
                width_fraction = detection.bbox.x2 - detection.bbox.x1
                height_fraction = detection.bbox.y2 - detection.bbox.y1
                metadata = dict(detection.metadata)
                metadata.update(
                    {
                        "fire_rgb_bright_neutral_fraction": bright_neutral_fraction,
                        "fire_rgb_colorful_fraction": colorful_fraction,
                        "fire_rgb_warm_fraction": warm_fraction,
                        "fire_rgb_bright_warm_fraction": bright_warm_fraction,
                        "fire_rgb_bbox_aspect_ratio": (
                            height_fraction / width_fraction if width_fraction > 0.0 else 0.0
                        ),
                    }
                )
                kept.append(
                    Detection(
                        label=detection.label,
                        confidence=detection.confidence,
                        bbox=detection.bbox,
                        sensor=detection.sensor,
                        model_version=detection.model_version,
                        metadata=metadata,
                    )
                )
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


class VehicleFurnitureOverlapVetoFilter:
    """Suppresses vehicle false positives contained by confident furniture boxes.

    The common COCO model still emits furniture classes even though they are not
    shown as selectable UI candidates.  Aerial-priority models can mistake a
    chair base or table edge for a car when used on an indoor commissioning
    feed.  If a vehicle candidate is substantially *inside* a concurrently
    detected furniture object, preserve the furniture observation but discard
    the conflicting vehicle label.  A nearby vehicle is unaffected because the
    threshold is measured against the vehicle candidate's own area.
    """

    def __init__(
        self,
        detector: Any,
        *,
        vehicle_labels: frozenset[str] = frozenset(
            {
                "vehicle",
                "car",
                "van",
                "truck",
                "bus",
                "train",
                "motor",
                "motorcycle",
                "tricycle",
                "awning-tricycle",
            }
        ),
        furniture_labels: frozenset[str] = frozenset(
            {"chair", "couch", "bed", "dining table", "toilet"}
        ),
        minimum_vehicle_coverage: float = 0.45,
        minimum_furniture_confidence: float = 0.35,
        furniture_margin: float = 0.01,
    ) -> None:
        if not 0.0 <= minimum_vehicle_coverage <= 1.0:
            raise ValueError("minimum_vehicle_coverage must be in [0, 1]")
        if not 0.0 <= minimum_furniture_confidence <= 1.0:
            raise ValueError("minimum_furniture_confidence must be in [0, 1]")
        if not math.isfinite(furniture_margin) or furniture_margin < 0.0:
            raise ValueError("furniture_margin must be finite and non-negative")
        self.detector = detector
        self.vehicle_labels = frozenset(label.strip().lower() for label in vehicle_labels)
        self.furniture_labels = frozenset(label.strip().lower() for label in furniture_labels)
        if not self.vehicle_labels or not self.furniture_labels:
            raise ValueError("vehicle and furniture labels must be non-empty")
        self.minimum_vehicle_coverage = minimum_vehicle_coverage
        self.minimum_furniture_confidence = minimum_furniture_confidence
        self.furniture_margin = furniture_margin

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        furniture = tuple(
            detection.bbox.expanded(self.furniture_margin)
            for detection in detections
            if detection.label in self.furniture_labels
            and detection.confidence >= self.minimum_furniture_confidence
        )
        if not furniture:
            return detections
        return tuple(
            detection
            for detection in detections
            if detection.label not in self.vehicle_labels
            or not any(
                self._coverage(detection.bbox, furniture_bbox) >= self.minimum_vehicle_coverage
                for furniture_bbox in furniture
            )
        )

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)

    @staticmethod
    def _coverage(candidate: Any, furniture: Any) -> float:
        x1 = max(candidate.x1, furniture.x1)
        y1 = max(candidate.y1, furniture.y1)
        x2 = min(candidate.x2, furniture.x2)
        y2 = min(candidate.y2, furniture.y2)
        intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
        return intersection / candidate.area if candidate.area > 0.0 else 0.0


class MultiSourceConfidenceFilter:
    """Require corroboration or higher evidence for selected cross-domain labels.

    A single aerial-priority detector can produce persistent indoor ``car``
    false positives from chair wheels and floor geometry.  Before same-label
    fusion removes source provenance, keep a candidate when either independent
    model versions agree on an overlapping box or its individual confidence
    clears a stricter single-source threshold.  This preserves high-confidence
    vehicles and normal two-model agreement while rejecting weak, isolated
    priority-model hallucinations.
    """

    def __init__(
        self,
        detector: Any,
        *,
        labels: frozenset[str],
        minimum_sources: int = 2,
        iou_threshold: float = 0.25,
        single_source_confidence: float = 0.80,
    ) -> None:
        if not labels:
            raise ValueError("labels must be non-empty")
        if minimum_sources < 2:
            raise ValueError("minimum_sources must be at least 2")
        if not math.isfinite(iou_threshold) or not 0.0 < iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be in (0, 1]")
        if (
            not math.isfinite(single_source_confidence)
            or not 0.0 <= single_source_confidence <= 1.0
        ):
            raise ValueError("single_source_confidence must be in [0, 1]")
        self.detector = detector
        self.labels = frozenset(label.strip().lower() for label in labels)
        self.minimum_sources = minimum_sources
        self.iou_threshold = iou_threshold
        self.single_source_confidence = single_source_confidence

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        kept: list[Detection] = []
        for detection in detections:
            if detection.label not in self.labels:
                kept.append(detection)
                continue
            sources = {self._source_key(detection)}
            for candidate in detections:
                if candidate.label != detection.label:
                    continue
                if detection.bbox.iou(candidate.bbox) >= self.iou_threshold:
                    sources.add(self._source_key(candidate))
            if (
                len(sources) >= self.minimum_sources
                or detection.confidence >= self.single_source_confidence
            ):
                kept.append(detection)
        return tuple(kept)

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)

    @staticmethod
    def _source_key(detection: Detection) -> str:
        source = str(detection.model_version or "").strip().lower()
        return source if source else "<unknown>"


@dataclass(slots=True)
class _TemporalCandidate:
    detection: Detection
    consecutive_frames: int
    missed_frames: int = 0


class TemporalDetectionFilter:
    """Requires selected classes to persist across consecutive camera frames.

    IoU alone is brittle for flame: its visible contour changes rapidly while the
    ignition source remains in the same local image region.  Optional canonical
    labels and a bounded centre/area fallback preserve temporal evidence for that
    case without allowing detections from unrelated regions to accumulate.
    """

    def __init__(
        self,
        detector: Any,
        *,
        labels: frozenset[str],
        minimum_consecutive_frames: int = 3,
        iou_threshold: float = 0.25,
        maximum_missed_frames: int = 1,
        label_aliases: Mapping[str, str] | None = None,
        maximum_center_distance: float | None = None,
        minimum_area_ratio: float = 0.0,
    ) -> None:
        if minimum_consecutive_frames <= 0:
            raise ValueError("minimum_consecutive_frames must be positive")
        if not 0.0 < iou_threshold <= 1.0:
            raise ValueError("iou_threshold must be in (0, 1]")
        if maximum_missed_frames < 0:
            raise ValueError("maximum_missed_frames cannot be negative")
        if maximum_center_distance is not None and (
            not math.isfinite(maximum_center_distance) or maximum_center_distance <= 0.0
        ):
            raise ValueError("maximum_center_distance must be finite and positive when set")
        if not math.isfinite(minimum_area_ratio) or not 0.0 <= minimum_area_ratio <= 1.0:
            raise ValueError("minimum_area_ratio must be in [0, 1]")
        self.detector = detector
        raw_aliases = label_aliases or {}
        self.label_aliases = {
            source.strip().lower(): target.strip().lower()
            for source, target in raw_aliases.items()
        }
        if any(not source or not target for source, target in self.label_aliases.items()):
            raise ValueError("temporal label aliases must contain non-empty labels")
        self.labels = frozenset(self._canonical_label(label) for label in labels)
        self.minimum_consecutive_frames = minimum_consecutive_frames
        self.iou_threshold = iou_threshold
        self.maximum_missed_frames = maximum_missed_frames
        self.maximum_center_distance = maximum_center_distance
        self.minimum_area_ratio = minimum_area_ratio
        self._candidates: list[_TemporalCandidate] = []

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(self.detector.class_names)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(getattr(self.detector, "provider_names", ()))

    def warmup(self, *, iterations: int = 1) -> None:
        self.detector.warmup(iterations=iterations)

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        detections = self.detector.detect(image_bgr)
        immediate = [
            detection
            for detection in detections
            if self._canonical_label(detection.label) not in self.labels
        ]
        filtered = sorted(
            (
                detection
                for detection in detections
                if self._canonical_label(detection.label) in self.labels
            ),
            key=lambda detection: detection.confidence,
            reverse=True,
        )
        # A confidence-ordered greedy match can make two nearby flame contours
        # steal each other's history: the first contour takes the best previous
        # candidate while the second then has no compatible predecessor.  That
        # produces a one-frame confirmation flicker exactly when two flame fronts
        # overlap in projection.  Solve the small per-frame bipartite assignment
        # globally so temporal evidence survives adjacent fire regions.
        matched_candidate_by_detection = self._assign_temporal_candidates(filtered)
        unmatched = set(range(len(self._candidates)))
        next_candidates: list[_TemporalCandidate] = []
        stable: list[Detection] = []
        for detection_index, detection in enumerate(filtered):
            index = matched_candidate_by_detection.get(detection_index)
            if index is not None:
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

    def _assign_temporal_candidates(
        self,
        detections: Sequence[Detection],
    ) -> dict[int, int]:
        """Return a maximum-cardinality, maximum-quality temporal assignment.

        The temporal candidate count is bounded by a single detector frame, so a
        compact Hungarian solve is cheaper than a false confirmation reset.  Each
        prior candidate gets a private dummy column; valid temporal matches always
        cost less than that dummy, while incompatible pairs remain prohibitively
        expensive.  Equal cases retain detector/candidate index order.
        """

        if not detections or not self._candidates:
            return {}
        candidate_count = len(self._candidates)
        detection_count = len(detections)
        invalid_cost = 1_000_000.0
        dummy_cost = 2.25
        quality_by_pair: dict[tuple[int, int], float] = {}
        costs: list[list[float]] = []
        for candidate_index, candidate in enumerate(self._candidates):
            row: list[float] = []
            candidate_label = self._canonical_label(candidate.detection.label)
            for detection_index, detection in enumerate(detections):
                if candidate_label != self._canonical_label(detection.label):
                    row.append(invalid_cost)
                    continue
                quality = self._match_quality(candidate.detection, detection)
                if quality is None:
                    row.append(invalid_cost)
                    continue
                quality_by_pair[(candidate_index, detection_index)] = quality
                # _match_quality is in (1, 3]; maximize it after retaining all
                # compatible candidate/detection pairs.
                row.append(3.0 - quality)
            row.extend(
                dummy_cost + abs(candidate_index - dummy_index) * 1e-9
                for dummy_index in range(candidate_count)
            )
            costs.append(row)

        matched: dict[int, int] = {}
        for candidate_index, column_index in enumerate(rectangular_linear_assignment(costs)):
            if (
                column_index < detection_count
                and (candidate_index, column_index) in quality_by_pair
            ):
                matched[column_index] = candidate_index
        return matched

    def _canonical_label(self, label: str) -> str:
        normalized = label.strip().lower()
        return self.label_aliases.get(normalized, normalized)

    def _match_quality(self, previous: Detection, current: Detection) -> float | None:
        """Return a match score, preferring IoU before the bounded flame fallback."""

        overlap = previous.bbox.iou(current.bbox)
        if overlap >= self.iou_threshold:
            # Keep IoU matches above all centre-only matches during greedy matching.
            return 2.0 + overlap
        if self.maximum_center_distance is None:
            return None
        center_distance = previous.bbox.center_distance(current.bbox)
        if center_distance > self.maximum_center_distance:
            return None
        previous_area = previous.bbox.area
        current_area = current.bbox.area
        if previous_area <= 0.0 or current_area <= 0.0:
            return None
        area_ratio = min(previous_area, current_area) / max(previous_area, current_area)
        if area_ratio < self.minimum_area_ratio:
            return None
        return 1.0 + (1.0 - center_distance / self.maximum_center_distance) + area_ratio * 0.01

    def covers_labels(self, required_labels: Sequence[str]) -> bool:
        return self.detector.covers_labels(required_labels)
