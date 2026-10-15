"""Wall-clock replay of the collector's RGB and MAVLink episode artifacts."""

from __future__ import annotations

import json
import math
import threading
import time
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .camera import CameraCalibration, CameraFrame
from .vehicle import PositionSetpoint, PymavlinkVehicle, VehicleState


@dataclass(frozen=True, slots=True)
class RecordedFrame:
    offset_s: float
    path: Path
    calibration: CameraCalibration


@dataclass(frozen=True, slots=True)
class RecordedTelemetry:
    offset_s: float
    message: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EpisodeRecording:
    frames: tuple[RecordedFrame, ...]
    telemetry: tuple[RecordedTelemetry, ...]
    task_text: str = ""
    path: Path | None = None
    synthetic: bool | None = None

    @property
    def duration_s(self) -> float:
        return max(
            self.frames[-1].offset_s if self.frames else 0.0,
            self.telemetry[-1].offset_s if self.telemetry else 0.0,
        )


class ReplayTimeline:
    """Shared monotonic origin prevents camera/vehicle replay drift."""

    def __init__(
        self, *, speed: float = 1.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("replay speed must be positive")
        self.speed = speed
        self.clock = clock
        self._origin: float | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._origin is None:
                self._origin = self.clock()

    def elapsed(self) -> float:
        if self._origin is None:
            return 0.0
        return max(0.0, self.clock() - self._origin) * self.speed

    def timestamp(self, offset_s: float) -> float:
        if self._origin is None:
            raise RuntimeError("replay timeline has not started")
        return self._origin + offset_s / self.speed


def _rows(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream, 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{path}:{index}: expected a JSON object")
                yield value


def _stamp(row: Mapping[str, Any], *, camera: bool = False) -> float:
    field = "frame_received_monotonic_ns" if camera and (
        "frame_received_monotonic_ns" in row
    ) else "receive_monotonic_ns"
    stamp = float(row[field]) / 1e9
    if not math.isfinite(stamp):
        raise ValueError("replay receive_monotonic_ns must be finite")
    return stamp


def load_episode(
    path: str | Path, calibration: CameraCalibration | None = None,
) -> EpisodeRecording:
    """Load ``steps.jsonl`` observations and ``mavlink.jsonl`` from one episode.

    Replay begins at the first RGB capture receipt. Earlier MAVLink samples seed the
    initial state at time zero; all later source receipt intervals are retained.
    Camera exposure/simulation timestamps are not interchangeable with receipt
    timestamps and are deliberately not used for scheduling.
    """
    path = Path(path).resolve()
    metadata_path = path / "episode.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) \
        if metadata_path.is_file() else {}
    frames: list[tuple[float, Path, CameraCalibration]] = []
    seen: set[tuple[float, Path]] = set()
    for step in _rows(path / "steps.jsonl"):
        for key in ("obs_before", "obs_after"):
            observation = step.get(key)
            if not isinstance(observation, dict) or not observation.get("rgb_path"):
                continue
            image_path = (path / str(observation["rgb_path"]).replace("\\", "/")).resolve()
            if not image_path.is_relative_to(path):
                raise ValueError("replay RGB path must stay inside the episode")
            if not image_path.is_file():
                raise FileNotFoundError(image_path)
            frame_calibration = calibration or CameraCalibration.from_mapping(
                observation["camera_info"]
            )
            has_stamp = any(key in observation for key in (
                "frame_received_monotonic_ns", "receive_monotonic_ns",
            ))
            stamp = _stamp(observation if has_stamp else step, camera=True)
            if (stamp, image_path) not in seen:
                frames.append((stamp, image_path, frame_calibration))
                seen.add((stamp, image_path))
    if not frames:
        raise ValueError("episode contains no recorded RGB observations")
    frames.sort(key=lambda item: item[0])
    origin = frames[0][0]
    telemetry = sorted(
        ((_stamp(row), row) for row in _rows(path / "mavlink.jsonl")),
        key=lambda item: item[0],
    )
    if not telemetry:
        raise ValueError("episode contains no MAVLink telemetry")
    return EpisodeRecording(
        frames=tuple(RecordedFrame(stamp - origin, frame_path, frame_calibration)
                     for stamp, frame_path, frame_calibration in frames),
        telemetry=tuple(RecordedTelemetry(stamp - origin, row)
                        for stamp, row in telemetry),
        task_text=str(metadata.get("task_text", "")), path=path,
        synthetic=(metadata["synthetic"] if isinstance(metadata.get("synthetic"), bool)
                   else True if metadata.get("sensor_status") == "synthetic_bench" else None),
    )


class ReplayCameraSource:
    def __init__(self, recording: EpisodeRecording, timeline: ReplayTimeline) -> None:
        if not recording.frames:
            raise ValueError("replay camera needs recorded frames")
        self.recording = recording
        self.timeline = timeline
        self._offsets = [frame.offset_s for frame in recording.frames]
        self._index = -1
        self._latest: CameraFrame | None = None
        self._started = False
        self._closed = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        self.timeline.start()
        self._started = True
        self._publish()
        self._thread = threading.Thread(target=self._run, name="replay-camera", daemon=True)
        self._thread.start()

    def _publish(self) -> None:
        index = bisect_right(self._offsets, self.timeline.elapsed()) - 1
        if index < 0:
            return
        if index != self._index:
            self._index = index
            record = self.recording.frames[index]
            frame = CameraFrame(
                record.path.read_bytes(), self.timeline.timestamp(record.offset_s),
                record.calibration, record.offset_s,
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


class ReplayVehicle:
    """Decode recorded MAVLink; outbound commands are logged and never transmitted."""

    def __init__(self, recording: EpisodeRecording, timeline: ReplayTimeline) -> None:
        self.recording = recording
        self.timeline = timeline
        self._decoder = PymavlinkVehicle("replay", clock=timeline.clock)
        self._index = 0
        self._started = False
        self._closed = False
        self._lock = threading.Lock()
        self.setpoints: list[tuple[float, PositionSetpoint]] = []
        self.commands: list[tuple[float, str]] = []

    def start(self) -> None:
        self.timeline.start()
        self._started = True

    def snapshot(self) -> VehicleState:
        with self._lock:
            if self._started and not self._closed:
                elapsed = self.timeline.elapsed()
                while self._index < len(self.recording.telemetry):
                    sample = self.recording.telemetry[self._index]
                    if sample.offset_s > elapsed:
                        break
                    self._decoder.ingest(
                        sample.message, self.timeline.timestamp(sample.offset_s),
                    )
                    self._index += 1
            return self._decoder.snapshot()

    def send_setpoint(self, setpoint: PositionSetpoint) -> None:
        with self._lock:
            if self._closed or not self._started:
                raise RuntimeError("replay vehicle is not running")
            self.setpoints.append((self.timeline.clock(), setpoint))

    def _command(self, command: str) -> None:
        with self._lock:
            if self._closed or not self._started:
                raise RuntimeError("replay vehicle is not running")
            self.commands.append((self.timeline.clock(), command))

    def arm(self) -> None:
        self._command("ARM")

    def set_mode(self, mode: str) -> None:
        self._command(mode.upper())

    def land(self) -> None:
        self._command("LAND")

    def rtl(self) -> None:
        self._command("RTL")

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def timing_report(self) -> dict[str, Any]:
        with self._lock:
            stamps = [stamp for stamp, _ in self.setpoints]
        gaps = [right - left for left, right in zip(stamps, stamps[1:], strict=False)]
        elapsed = stamps[-1] - stamps[0] if len(stamps) > 1 else 0.0
        return {
            "setpoint_count": len(stamps),
            "setpoint_rate_hz": (len(stamps) - 1) / elapsed if elapsed > 0 else 0.0,
            "setpoint_max_gap_s": max(gaps, default=0.0),
            "elapsed_s": elapsed,
        }
