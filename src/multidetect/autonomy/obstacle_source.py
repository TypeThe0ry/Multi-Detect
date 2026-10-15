"""Obstacle sources used by the onboard clearance gate.

The clearance gate deliberately exposes one small operation: determine whether
a local-NED segment is safe at the altitude represented by its start pose.
Implementations may be backed by a static world map, a rolling sensor grid, or
several sources composed conservatively.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

try:  # Keep the historical static-map path lightweight when vision extras are absent.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only in minimal deployments.
    np = None  # type: ignore[assignment]

from multidetect.autonomy.ground_model import GroundModel
from multidetect.sim_teacher.policies import SDFObstacle, segment_clear_of_obstacles

PoseNed = tuple[float, float, float]


@runtime_checkable
class ObstacleSource(Protocol):
    """Source of local-NED obstacle clearance decisions."""

    def segment_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        """Return ``True`` when the complete segment is safe to fly."""


class StaticMapSource:
    """Clearance against the SDF obstacle footprints used historically.

    The altitude and margin calculation intentionally mirrors
    :func:`segment_clear_of_obstacles`, preserving the old static-map behavior
    byte-for-byte for callers that previously supplied its lambda wrapper.
    """

    __slots__ = ("obstacles", "margin_m")

    def __init__(
        self,
        obstacles: Iterable[SDFObstacle] = (),
        *,
        margin_m: float = 3.0,
    ) -> None:
        self.obstacles = tuple(obstacles)
        self.margin_m = float(margin_m)

    def segment_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        altitude_m = max(0.0, -float(start_ned[2]))
        return segment_clear_of_obstacles(
            start_ned,
            end_ned,
            altitude_m,
            self.obstacles,
            margin_m=self.margin_m,
        )


class CompositeSource:
    """Conservative composition of obstacle sources.

    A segment is clear only when every configured source reports it clear. An
    empty composite is clear, which makes optional sensor sources safe to omit.
    """

    __slots__ = ("sources",)

    def __init__(self, sources: Iterable[ObstacleSource] = ()) -> None:
        self.sources = tuple(sources)

    def segment_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        return all(source.segment_clear(start_ned, end_ned) for source in self.sources)


class DepthGridSource:
    """Rolling local occupancy/height grid populated from metric depth frames.

    ``depth`` is an ``H x W`` array whose values are optical-axis Z in metres.
    ``intrinsics`` contains ``fx``, ``fy``, ``cx`` and ``cy`` in pixels.  The
    optional ``camera_rotation`` maps optical camera coordinates
    ``(right,down,forward)`` to local NED; callers should provide the calibrated
    rotation for a pitched-down camera.  If omitted, the identity is used,
    which is useful for tests and already-calibrated NED depth streams.

    The grid stores the minimum and maximum observed NED-down height in each
    horizontal cell.  Unknown cells remain unknown: when no fresh frame is
    available ``fallback`` is queried; without one, the source fails closed.
    This makes a depth source safe to combine with the historical static map
    using :class:`CompositeSource`.
    """

    __slots__ = (
        "cells", "resolution_m", "max_age_s", "min_depth_m", "max_depth_m",
        "height_tolerance_m", "ground_tolerance_m", "ground_model", "max_points",
        "fallback", "occupied", "terrain", "height_min", "height_max", "_points",
        "_anchor", "_last_timestamp", "_last_pose", "_received_monotonic",
        "_timestamp_unit", "_lock",
        "_last_sensor_seconds",
        "stale_policy", "degraded",
        "_generation",
    )

    def __init__(
        self,
        *,
        cells: int = 80,
        resolution_m: float = 0.5,
        max_age_s: float = 0.5,
        min_depth_m: float = 0.2,
        max_depth_m: float = 80.0,
        height_tolerance_m: float = 2.0,
        ground_tolerance_m: float = 0.25,
        ground_model: GroundModel | None = None,
        max_points: int = 20_000,
        fallback: ObstacleSource | None = None,
        timestamp_unit: str = "s",
        stale_policy: str = "fail_closed",
    ) -> None:
        if np is None:
            raise ImportError("DepthGridSource requires the optional numpy vision dependency")
        if int(cells) <= 0 or float(resolution_m) <= 0:
            raise ValueError("cells and resolution_m must be positive")
        if (
            float(max_age_s) <= 0
            or float(min_depth_m) <= 0
            or float(max_depth_m) <= float(min_depth_m)
        ):
            raise ValueError("invalid depth range or max_age_s; max_age_s must be > 0")
        self._timestamp_unit = self._normalise_timestamp_unit(timestamp_unit)
        self.cells = int(cells)
        self.resolution_m = float(resolution_m)
        self.max_age_s = float(max_age_s)
        self.min_depth_m = float(min_depth_m)
        self.max_depth_m = float(max_depth_m)
        self.height_tolerance_m = max(0.0, float(height_tolerance_m))
        if not math.isfinite(float(ground_tolerance_m)) or float(ground_tolerance_m) < 0:
            raise ValueError("ground_tolerance_m must be finite and non-negative")
        self.ground_tolerance_m = float(ground_tolerance_m)
        self.ground_model = ground_model
        self.max_points = max(1, int(max_points))
        self.fallback = fallback
        self.occupied = np.zeros((self.cells, self.cells), dtype=bool)
        # Terrain is retained separately from obstacle occupancy.  A return is
        # an obstacle only when it is sufficiently above the local ground
        # surface; callers may inspect this layer for diagnostics.
        self.terrain = np.zeros((self.cells, self.cells), dtype=bool)
        self.height_min = np.full((self.cells, self.cells), np.nan, dtype=np.float32)
        self.height_max = np.full((self.cells, self.cells), np.nan, dtype=np.float32)
        self._points: list[tuple[float, float, float, float | None, bool]] = []
        self._anchor: tuple[float, float] | None = None
        self._last_timestamp: float | None = None
        self._last_pose: PoseNed | None = None
        self._received_monotonic: float | None = None
        self._lock = threading.RLock()
        self._last_sensor_seconds: float | None = None
        if stale_policy not in {"fail_closed", "static_fallback"}:
            raise ValueError("stale_policy must be 'fail_closed' or 'static_fallback'")
        self.stale_policy = stale_policy
        self.degraded = False
        self._generation = 0

    @staticmethod
    def _normalise_timestamp_unit(unit: str) -> str:
        aliases = {
            "s": "s", "sec": "s", "second": "s", "seconds": "s",
            "ms": "ms", "millisecond": "ms", "milliseconds": "ms",
            "us": "us", "microsecond": "us", "microseconds": "us",
            "ns": "ns", "nanosecond": "ns", "nanoseconds": "ns",
        }
        try:
            return aliases[str(unit).strip().lower()]
        except (KeyError, TypeError):
            raise ValueError("timestamp_unit must be one of s, ms, us or ns") from None

    @staticmethod
    def _timestamp_seconds(value: float, unit: str) -> float:
        return value / {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}[unit]

    @property
    def grid(self) -> np.ndarray:
        """Boolean occupancy grid (north/east cells)."""
        with self._lock:
            return self.occupied.copy()

    @property
    def last_timestamp(self) -> float | None:
        with self._lock:
            return self._last_timestamp

    @property
    def last_pose(self) -> PoseNed | None:
        with self._lock:
            return self._last_pose

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def _fallback_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        # A missing or stale range observation is unsafe for flight.  Compose
        # with StaticMapSource when a map is available; otherwise fail closed.
        self.degraded = True
        if self.stale_policy != "static_fallback" or self.fallback is None:
            return False
        return self.fallback.segment_clear(start_ned, end_ned)

    def _fresh(self, now: float | None = None) -> bool:
        with self._lock:
            if self._last_timestamp is None or not self._points:
                return False
            receipt = self._received_monotonic
            if receipt is None:
                return False
            # Sensor timestamps are metadata only.  Freshness uses the local
            # monotonic receipt clock, so relative, epoch and nanosecond sensor
            # domains can never be confused with the host clock.
            clock = time.monotonic() if now is None else float(now)
            age = clock - receipt
            return 0.0 <= age <= self.max_age_s

    def _rebuild(self) -> None:
        self.occupied.fill(False)
        self.terrain.fill(False)
        self.height_min.fill(np.nan)
        self.height_max.fill(np.nan)
        if self._anchor is None:
            return
        half = self.cells * self.resolution_m / 2.0
        for north, east, down, _stamp, is_obstacle in self._points:
            col = int(math.floor((north - self._anchor[0] + half) / self.resolution_m))
            row = int(math.floor((east - self._anchor[1] + half) / self.resolution_m))
            if not (0 <= row < self.cells and 0 <= col < self.cells):
                continue
            if not is_obstacle:
                self.terrain[row, col] = True
                continue
            self.occupied[row, col] = True
            old_min = self.height_min[row, col]
            old_max = self.height_max[row, col]
            self.height_min[row, col] = down if np.isnan(old_min) else min(float(old_min), down)
            self.height_max[row, col] = down if np.isnan(old_max) else max(float(old_max), down)

    def update(
        self,
        depth: np.ndarray,
        pose_ned: PoseNed,
        intrinsics: Mapping[str, float],
        *,
        timestamp: float | None = None,
        timestamp_unit: str | None = None,
        encoding: str = "FLOAT32_M",
        camera_rotation: np.ndarray | None = None,
        camera_offset_ned: tuple[float, float, float] = (0.0, 0.0, 0.0),
        deadline_monotonic: float | None = None,
    ) -> int:
        """Project one frame into the rolling grid and return valid point count."""
        raw = np.asarray(depth)
        encoding_name = str(encoding).strip().upper()
        if encoding_name == "UINT16_MM":
            if raw.dtype.kind not in "ui":
                raise ValueError("UINT16_MM depth must use an integer array")
            image = raw.astype(np.float32) * 0.001
        elif encoding_name == "FLOAT32_M":
            if raw.dtype.kind != "f":
                raise ValueError("FLOAT32_M depth must use a floating-point array")
            image = raw.astype(np.float32, copy=False)
        else:
            raise ValueError("encoding must be FLOAT32_M or UINT16_MM")
        if image.ndim == 3 and image.shape[-1] == 1:
            image = image[..., 0]
        if image.ndim != 2:
            raise ValueError("depth must be a 2-D array")
        try:
            fx, fy = float(intrinsics["fx"]), float(intrinsics["fy"])
            cx, cy = float(intrinsics["cx"]), float(intrinsics["cy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("intrinsics require fx, fy, cx and cy") from exc
        if not all(math.isfinite(v) and v > 0 for v in (fx, fy)):
            raise ValueError("fx and fy must be finite and positive")
        rotation = (
            np.eye(3, dtype=np.float32)
            if camera_rotation is None
            else np.asarray(camera_rotation, dtype=np.float32)
        )
        if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
            raise ValueError("camera_rotation must be a finite 3x3 matrix")
        pose = tuple(float(v) for v in pose_ned)
        if len(pose) != 3 or not all(math.isfinite(v) for v in pose):
            raise ValueError("pose_ned must contain three finite values")
        valid = np.isfinite(image) & (image >= self.min_depth_m) & (image <= self.max_depth_m)
        ys, xs = np.nonzero(valid)
        if len(xs) > self.max_points:
            stride = int(math.ceil(len(xs) / self.max_points))
            ys, xs = ys[::stride], xs[::stride]
        values = image[ys, xs].astype(np.float32, copy=False)
        cam = np.stack(((xs - cx) * values / fx, (ys - cy) * values / fy, values), axis=1)
        world = cam @ rotation.T
        world[:, 0] += pose[0] + float(camera_offset_ned[0])
        world[:, 1] += pose[1] + float(camera_offset_ned[1])
        world[:, 2] += pose[2] + float(camera_offset_ned[2])
        # Retain the sensor stamp for diagnostics, but always use local receipt
        # age for freshness.  Units are explicit and never inferred from value
        # magnitude (in particular, nanosecond stamps are valid metadata).
        receipt = time.monotonic()
        if timestamp is None:
            stamp = receipt
            stamp_unit = "s"
        else:
            stamp = float(timestamp)
            if not math.isfinite(stamp):
                raise ValueError("timestamp must be finite")
            stamp_unit = self._normalise_timestamp_unit(
                self._timestamp_unit if timestamp_unit is None else timestamp_unit
            )
            sensor_seconds = self._timestamp_seconds(stamp, stamp_unit)
        # Classify returns against the supplied local ground model.  The
        # GroundModel API returns the horizontal hit only, so recover its NED
        # down coordinate from the ray parameter and the camera height.
        camera_origin_ned = (
            pose[0] + float(camera_offset_ned[0]),
            pose[1] + float(camera_offset_ned[1]),
            pose[2] + float(camera_offset_ned[2]),
        )
        camera_origin_neu = (camera_origin_ned[0], camera_origin_ned[1], -camera_origin_ned[2])
        classified: list[tuple[float, float, float, float | None, bool]] = []
        for north, east, down in world:
            is_obstacle = True
            if self.ground_model is not None:
                ray = (
                    float(north) - camera_origin_ned[0],
                    float(east) - camera_origin_ned[1],
                    float(down) - camera_origin_ned[2],
                )
                try:
                    hit = self.ground_model.intersect_ray(camera_origin_neu, ray)
                except (TypeError, ValueError, ZeroDivisionError):
                    hit = None
                if hit is not None:
                    hit_n, hit_e = float(hit[0]), float(hit[1])
                    horizontal = (ray[0], ray[1])
                    denom = horizontal[0] * horizontal[0] + horizontal[1] * horizontal[1]
                    if denom > 1e-12:
                        t = (
                            (hit_n - camera_origin_ned[0]) * horizontal[0]
                            + (hit_e - camera_origin_ned[1]) * horizontal[1]
                        ) / denom
                        ground_down = camera_origin_ned[2] + ray[2] * t
                        # NED down: points with smaller down than terrain are
                        # above the surface and may be obstacles.
                        is_obstacle = (ground_down - float(down)) >= self.ground_tolerance_m
                    else:
                        # A nadir ray has no horizontal baseline from which to
                        # recover the intersection parameter.  Probe the same
                        # ray with a tiny horizontal component; this preserves
                        # the local ground height while yielding a stable t for
                        # flat and DEM ground models alike.
                        epsilon = 1.0e-3
                        try:
                            probe = self.ground_model.intersect_ray(
                                camera_origin_neu, (epsilon, 0.0, ray[2])
                            )
                        except (TypeError, ValueError, ZeroDivisionError):
                            probe = None
                        if probe is not None:
                            t = (float(probe[0]) - camera_origin_ned[0]) / epsilon
                            ground_down = camera_origin_ned[2] + ray[2] * t
                            is_obstacle = (ground_down - float(down)) >= self.ground_tolerance_m
                        else:
                            is_obstacle = False
            classified.append((float(north), float(east), float(down), stamp, is_obstacle))
        # Build all arrays before publication.  The lock makes the assignment
        # of this complete generation atomic with respect to clearance queries.
        with self._lock:
            if deadline_monotonic is not None and time.monotonic() > float(deadline_monotonic):
                return 0
            if timestamp is not None and self._last_sensor_seconds is not None:
                if sensor_seconds < self._last_sensor_seconds:
                    # An out-of-order frame must never replace a newer map.
                    return 0
            self._anchor = (pose[0], pose[1])
            self._last_pose = pose
            self._last_timestamp = stamp
            self._received_monotonic = receipt
            if timestamp is not None:
                self._last_sensor_seconds = sensor_seconds
            self._points = classified
            self._rebuild()
            self._generation += 1
            return len(self._points)

    # Explicit aliases make integration with camera backends self-explanatory.
    add_frame = update
    ingest = update

    def segment_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        with self._lock:
            if not self._fresh() or self._anchor is None:
                return self._fallback_clear(start_ned, end_ned)
            self.degraded = False
            start = tuple(float(v) for v in start_ned)
            end = tuple(float(v) for v in end_ned)
            distance = math.dist(start[:2], end[:2])
            samples = max(1, int(math.ceil(distance / max(self.resolution_m * 0.5, 1e-3))))
            half = self.cells * self.resolution_m / 2.0
            for index in range(samples + 1):
                ratio = index / samples
                north = start[0] + ratio * (end[0] - start[0])
                east = start[1] + ratio * (end[1] - start[1])
                down = start[2] + ratio * (end[2] - start[2])
                col = int(math.floor((north - self._anchor[0] + half) / self.resolution_m))
                row = int(math.floor((east - self._anchor[1] + half) / self.resolution_m))
                if 0 <= row < self.cells and 0 <= col < self.cells and self.occupied[row, col]:
                    low, high = self.height_min[row, col], self.height_max[row, col]
                    if (
                        float(low) - self.height_tolerance_m
                        <= down
                        <= float(high) + self.height_tolerance_m
                    ):
                        return False
            return True


class AsyncDepthGridSource:
    """Single-worker, latest-frame-wins adapter for the setpoint thread.

    ``update`` only copies the frame reference into a one-slot queue; all
    projection work happens on the worker.  A busy worker therefore cannot
    block clearance queries or the 20 Hz control path.
    """

    def __init__(self, source: DepthGridSource, *, frame_budget_s: float = 0.1) -> None:
        if frame_budget_s <= 0 or not math.isfinite(frame_budget_s):
            raise ValueError("frame_budget_s must be finite and positive")
        self.source = source
        self.frame_budget_s = float(frame_budget_s)
        self._queue: queue.Queue[tuple[tuple, dict] | None] = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self.dropped_frames = 0
        self.deadline_misses = 0
        self.last_error: str | None = None
        self.error_count = 0
        self._failed = False
        self._closed = False
        self._mutex = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="depth-grid", daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                return
            args, kwargs = item
            started = time.monotonic()
            generation_before = self.source.generation
            try:
                kwargs = dict(kwargs)
                kwargs["deadline_monotonic"] = started + self.frame_budget_s
                self.source.update(*args, **kwargs)
                elapsed = time.monotonic() - started
                with self._mutex:
                    if elapsed > self.frame_budget_s:
                        self.deadline_misses += 1
                    elif self.source.generation > generation_before:
                        self.last_error = None
                        self._failed = False
            except Exception as exc:  # worker must survive a bad sensor frame
                with self._mutex:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self.error_count += 1
                    self._failed = True

    def update(self, *args, **kwargs) -> bool:
        item = (args, kwargs)
        with self._mutex:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._queue.get_nowait()
                self.dropped_frames += 1
                self._queue.put_nowait(item)
        return True

    def segment_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        with self._mutex:
            if self._failed:
                return False
        return self.source.segment_clear(start_ned, end_ned)

    def status(self) -> dict[str, object]:
        with self._mutex:
            return {
                "closed": self._closed,
                "failed": self._failed,
                "worker_alive": self._worker.is_alive(),
                "last_error": self.last_error,
                "error_count": self.error_count,
                "dropped_frames": self.dropped_frames,
                "deadline_misses": self.deadline_misses,
            }

    def close(self, *, join_timeout_s: float = 1.0) -> bool:
        with self._mutex:
            if self._closed:
                return not self._worker.is_alive()
            self._closed = True
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._worker.join(timeout=join_timeout_s)
        alive = self._worker.is_alive()
        if alive:
            with self._mutex:
                self._failed = True
                self.last_error = "worker did not stop before close timeout"
        return not alive


__all__ = [
    "AsyncDepthGridSource", "CompositeSource", "DepthGridSource", "ObstacleSource",
    "PoseNed", "StaticMapSource",
]
