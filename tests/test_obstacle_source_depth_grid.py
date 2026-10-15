"""CPU checks for the rolling depth obstacle source."""

import time

import numpy as np

from multidetect.autonomy.obstacle_source import CompositeSource, DepthGridSource, StaticMapSource
from multidetect.sim_teacher.policies import SDFObstacle, segment_clear_of_obstacles


class _Fallback:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls = 0

    def segment_clear(self, _start, _end) -> bool:
        self.calls += 1
        return self.result


INTRINSICS = {"fx": 2.0, "fy": 2.0, "cx": 1.5, "cy": 1.5}


def test_depth_frame_builds_occupancy_and_blocks_segment() -> None:
    source = DepthGridSource(cells=8, resolution_m=1.0, height_tolerance_m=0.4)
    depth = np.full((4, 4), np.nan, dtype=np.float32)
    depth[2, 2] = 5.0
    assert source.update(depth, (0.0, 0.0, 0.0), INTRINSICS) == 1
    assert source.grid.any()
    assert source.segment_clear((-1.0, 1.25, 5.0), (2.0, 1.25, 5.0)) is False
    assert source.segment_clear((-1.0, 0.0, 1.0), (1.0, 0.0, 1.0)) is True


def test_camera_rotation_and_pose_are_applied() -> None:
    source = DepthGridSource(cells=10, resolution_m=0.5, height_tolerance_m=0.2)
    # Optical forward (+Z) points north; optical right maps east.
    rotation = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    depth = np.full((2, 2), np.nan, dtype=np.float32)
    depth[0, 0] = 2.0
    source.update(
        depth,
        (3.0, 4.0, -1.0),
        {"fx": 1.0, "fy": 1.0, "cx": 0.0, "cy": 0.0},
        camera_rotation=rotation,
    )
    # The forward ray maps to N=5, E=4, down=-1.
    assert source.segment_clear((4.0, 4.0, -1.0), (6.0, 4.0, -1.0)) is False


def test_stale_or_empty_depth_uses_fallback() -> None:
    fallback = _Fallback(False)
    source = DepthGridSource(fallback=fallback, max_age_s=0.01)
    depth = np.ones((2, 2), dtype=np.float32)
    source.update(depth, (0.0, 0.0, 0.0), INTRINSICS, timestamp=time.monotonic() - 1.0)
    assert source.segment_clear((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) is False
    assert fallback.calls == 1
    empty = DepthGridSource(fallback=fallback)
    empty.update(np.full((2, 2), np.nan), (0.0, 0.0, 0.0), INTRINSICS)
    assert empty.segment_clear((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) is False


def test_composite_is_conservative_and_empty_is_clear() -> None:
    clear = _Fallback(True)
    block = _Fallback(False)
    assert CompositeSource().segment_clear((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) is True
    assert CompositeSource((clear, block)).segment_clear((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) is False


def test_static_source_matches_historical_clearance() -> None:
    obstacles = (SDFObstacle(3.0, 0.0, 10.0, 2.0, 2.0),)
    start, end = (0.0, 0.0, -10.0), (6.0, 0.0, -10.0)
    expected = segment_clear_of_obstacles(start, end, 10.0, obstacles)
    assert StaticMapSource(obstacles).segment_clear(start, end) is expected

