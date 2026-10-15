"""Obstacle sources used by the onboard clearance gate.

The clearance gate deliberately exposes one small operation: determine whether
a local-NED segment is safe at the altitude represented by its start pose.
Implementations may be backed by a static world map, a rolling sensor grid, or
several sources composed conservatively.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from typing import Protocol, runtime_checkable

try:  # Keep the historical static-map path lightweight when vision extras are absent.
    import numpy as np
except ImportError:  # pragma: no cover - exercised only in minimal deployments.
    np = None  # type: ignore[assignment]

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
        "height_tolerance_m", "max_points", "fallback", "occupied", "height_min",
        "height_max", "_points", "_anchor", "_last_timestamp", "_last_pose",
        "_received_monotonic",
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
        max_points: int = 20_000,
        fallback: ObstacleSource | None = None,
    ) -> None:
        if np is None:
            raise ImportError("DepthGridSource requires the optional numpy vision dependency")
        if int(cells) <= 0 or float(resolution_m) <= 0:
            raise ValueError("cells and resolution_m must be positive")
        if (
            float(max_age_s) < 0
            or float(min_depth_m) <= 0
            or float(max_depth_m) <= float(min_depth_m)
        ):
            raise ValueError("invalid depth range or max_age_s")
        self.cells = int(cells)
        self.resolution_m = float(resolution_m)
        self.max_age_s = float(max_age_s)
        self.min_depth_m = float(min_depth_m)
        self.max_depth_m = float(max_depth_m)
        self.height_tolerance_m = max(0.0, float(height_tolerance_m))
        self.max_points = max(1, int(max_points))
        self.fallback = fallback
        self.occupied = np.zeros((self.cells, self.cells), dtype=bool)
        self.height_min = np.full((self.cells, self.cells), np.nan, dtype=np.float32)
        self.height_max = np.full((self.cells, self.cells), np.nan, dtype=np.float32)
        self._points: list[tuple[float, float, float, float | None]] = []
        self._anchor: tuple[float, float] | None = None
        self._last_timestamp: float | None = None
        self._last_pose: PoseNed | None = None
        self._received_monotonic: float | None = None

    @property
    def grid(self) -> np.ndarray:
        """Boolean occupancy grid (north/east cells)."""
        return self.occupied

    @property
    def last_timestamp(self) -> float | None:
        return self._last_timestamp

    @property
    def last_pose(self) -> PoseNed | None:
        return self._last_pose

    def _fallback_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        # A missing or stale range observation is unsafe for flight.  Compose
        # with StaticMapSource when a map is available; otherwise fail closed.
        if self.fallback is None:
            return False
        return self.fallback.segment_clear(start_ned, end_ned)

    def _fresh(self, now: float | None = None) -> bool:
        if self._last_timestamp is None or not self._points:
            return False
        if self.max_age_s == 0:
            return True
        # Recorded SIM stamps are usually relative seconds, whereas live
        # callers may provide monotonic stamps.  Relative stamps cannot be
        # compared with this process clock, so use receipt age for them.
        clock = time.monotonic() if now is None else float(now)
        if self._last_timestamp > 100_000.0:
            delta = clock - self._last_timestamp
        else:
            delta = clock - (self._received_monotonic or clock)
        return delta <= self.max_age_s

    def _rebuild(self) -> None:
        self.occupied.fill(False)
        self.height_min.fill(np.nan)
        self.height_max.fill(np.nan)
        if self._anchor is None:
            return
        half = self.cells * self.resolution_m / 2.0
        for north, east, down, _stamp in self._points:
            col = int(math.floor((north - self._anchor[0] + half) / self.resolution_m))
            row = int(math.floor((east - self._anchor[1] + half) / self.resolution_m))
            if not (0 <= row < self.cells and 0 <= col < self.cells):
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
        camera_rotation: np.ndarray | None = None,
        camera_offset_ned: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> int:
        """Project one frame into the rolling grid and return valid point count."""
        image = np.asarray(depth, dtype=np.float32)
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
        # A monotonic local stamp keeps frames fresh for live callers that do
        # not carry sensor timestamps; recorded timestamps are retained as-is.
        stamp = time.monotonic() if timestamp is None else float(timestamp)
        self._anchor = (pose[0], pose[1])
        self._last_pose = pose
        self._last_timestamp = stamp
        self._received_monotonic = time.monotonic()
        self._points = [(float(n), float(e), float(d), stamp) for n, e, d in world]
        self._rebuild()
        return len(self._points)

    # Explicit aliases make integration with camera backends self-explanatory.
    add_frame = update
    ingest = update

    def segment_clear(self, start_ned: PoseNed, end_ned: PoseNed) -> bool:
        if not self._fresh() or self._anchor is None:
            return self._fallback_clear(start_ned, end_ned)
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


__all__ = ["CompositeSource", "DepthGridSource", "ObstacleSource", "PoseNed", "StaticMapSource"]
