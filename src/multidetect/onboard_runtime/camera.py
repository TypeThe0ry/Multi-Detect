"""Latest-frame camera adapters with explicit calibration and monotonic timing."""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class CameraCalibration:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    extrinsics: Mapping[str, Any] = field(default_factory=dict)
    distortion: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("camera dimensions must be positive")
        if not all(math.isfinite(value) for value in (self.fx, self.fy, self.cx, self.cy)):
            raise ValueError("camera intrinsics must be finite")
        if self.fx <= 0 or self.fy <= 0:
            raise ValueError("camera focal lengths must be positive")
        if not all(math.isfinite(value) for value in self.distortion):
            raise ValueError("camera distortion must be finite")

    @property
    def camera_info(self) -> dict[str, Any]:
        return {
            "width": self.width, "height": self.height,
            "fx": self.fx, "fy": self.fy, "cx": self.cx, "cy": self.cy,
            "distortion": list(self.distortion), "extrinsics": dict(self.extrinsics),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CameraCalibration:
        values = payload.get("calibration", payload)
        intrinsics = values.get("intrinsics", values)
        mount_names = (
            "mount_pitch_down_deg", "mount_yaw_right_deg", "mount_roll_clockwise_deg",
            "camera_offset_forward_m", "camera_offset_right_m", "camera_offset_down_m",
        )
        extrinsics = values.get("extrinsics", {
            key: values[key] for key in mount_names if key in values
        })
        if not isinstance(extrinsics, Mapping):
            raise ValueError("camera extrinsics must be a mapping")
        for value in extrinsics.values():
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise ValueError("camera extrinsics must be finite")
        distortion = intrinsics.get("distortion", [
            intrinsics.get(key, 0.0) for key in ("k1", "k2", "p1", "p2", "k3")
        ])
        return cls(
            width=int(intrinsics.get("width", intrinsics.get("width_px", 0))),
            height=int(intrinsics.get("height", intrinsics.get("height_px", 0))),
            fx=float(intrinsics.get("fx", intrinsics.get("fx_px", 0))),
            fy=float(intrinsics.get("fy", intrinsics.get("fy_px", 0))),
            cx=float(intrinsics.get("cx", intrinsics.get("cx_px", 0))),
            cy=float(intrinsics.get("cy", intrinsics.get("cy_px", 0))),
            extrinsics=dict(extrinsics), distortion=tuple(float(item) for item in distortion),
        )

    @classmethod
    def load(cls, path: str | Path) -> CameraCalibration:
        calibration = cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))
        if not calibration.extrinsics:
            raise ValueError("real camera calibration requires explicit extrinsics")
        return calibration


@dataclass(frozen=True, slots=True)
class CameraFrame:
    rgb: bytes
    timestamp_monotonic: float
    calibration: CameraCalibration
    source_timestamp: float | None = None


@runtime_checkable
class CameraSource(Protocol):
    def start(self) -> None: ...

    def latest(self) -> CameraFrame | None: ...

    def close(self) -> None: ...


class OpenCVCameraSource:
    """Decode V4L2 or GStreamer on a producer thread; never queue old frames.

    Timestamp is the local capture receipt time, not a hardware exposure timestamp.
    Use a bounded appsink (max-buffers=1 drop=true sync=false) for CSI pipelines.
    """

    def __init__(
        self, source: int | str, calibration: CameraCalibration, *,
        backend: str = "v4l2", clock: Callable[[], float] = time.monotonic,
        capture_factory: Callable[[Any, int], Any] | None = None,
    ) -> None:
        if backend not in {"v4l2", "gstreamer"}:
            raise ValueError("camera backend must be v4l2 or gstreamer")
        self.source = source
        self.calibration = calibration
        self.backend = backend
        self.clock = clock
        self._capture_factory = capture_factory
        self._capture: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: CameraFrame | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("camera already started")
        import cv2

        backend = cv2.CAP_V4L2 if self.backend == "v4l2" else cv2.CAP_GSTREAMER
        self._capture = (self._capture_factory or cv2.VideoCapture)(self.source, backend)
        if not self._capture.isOpened():
            self._capture.release()
            raise RuntimeError(f"cannot open {self.backend} camera: {self.source}")
        if self.backend == "v4l2":
            self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.calibration.width)
            self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.calibration.height)
            self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._thread = threading.Thread(target=self._run, name="onboard-camera", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import cv2

        try:
            while not self._stop.is_set():
                success, image = self._capture.read()
                captured = self.clock()
                if not success:
                    raise RuntimeError("camera read failed")
                if image.shape[:2] != (self.calibration.height, self.calibration.width):
                    raise ValueError("captured camera dimensions differ from calibration")
                success, encoded = cv2.imencode(".jpg", image)
                if not success:
                    raise RuntimeError("camera frame encoding failed")
                frame = CameraFrame(encoded.tobytes(), captured, self.calibration)
                with self._lock:
                    self._latest = frame
        except BaseException as error:
            self.error = error

    def latest(self) -> CameraFrame | None:
        with self._lock:
            return self._latest

    def close(self) -> None:
        self._stop.set()
        if self._capture is not None:
            self._capture.release()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                self.error = RuntimeError("camera reader did not stop within 2 seconds")


class FileCameraSource:
    """Replay an image file or sorted image directory on a monotonic timeline."""

    def __init__(
        self, path: str | Path, calibration: CameraCalibration, *, fps: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("file camera fps must be positive")
        path = Path(path)
        self.paths = sorted(
            entry for entry in path.iterdir()
            if entry.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp"}
        ) if path.is_dir() else [path]
        if not self.paths or any(not entry.is_file() for entry in self.paths):
            raise ValueError("file camera requires image files")
        self.calibration = calibration
        self.fps = fps
        self.clock = clock
        self._origin: float | None = None
        self._index = -1
        self._latest: CameraFrame | None = None
        self._closed = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        if self._origin is not None:
            raise RuntimeError("file camera already started")
        self._origin = self.clock()
        self._publish()
        self._thread = threading.Thread(target=self._run, name="file-camera", daemon=True)
        self._thread.start()

    def _publish(self) -> None:
        assert self._origin is not None
        index = min(int(max(0.0, self.clock() - self._origin) * self.fps), len(self.paths) - 1)
        if index != self._index:
            self._index = index
            frame = CameraFrame(
                self.paths[index].read_bytes(), self._origin + index / self.fps,
                self.calibration, index / self.fps,
            )
            with self._lock:
                self._latest = frame

    def _run(self) -> None:
        try:
            while not self._stop.wait(0.01):
                self._publish()
        except BaseException as error:
            self.error = error

    def latest(self) -> CameraFrame | None:
        with self._lock:
            return None if self._closed else self._latest

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
