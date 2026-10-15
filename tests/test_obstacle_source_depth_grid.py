"""CPU checks for the rolling depth obstacle source."""

import time

import numpy as np
import pytest

from multidetect.autonomy.ground_model import FlatGround
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


def test_ground_returns_are_kept_in_terrain_layer() -> None:
    source = DepthGridSource(
        cells=8,
        resolution_m=1.0,
        height_tolerance_m=0.1,
        ground_model=FlatGround(0.0),
    )
    # Camera is five metres above a flat ground plane; the centre ray returns
    # exactly on the plane (NED down = 0), so it must not become an obstacle.
    depth = np.full((4, 4), np.nan, dtype=np.float32)
    depth[2, 2] = 5.0
    source.update(depth, (0.0, 0.0, -5.0), INTRINSICS)
    assert not bool(source.grid.any())
    assert bool(source.terrain.any())
    assert source.segment_clear((-1.0, 1.25, -5.0), (2.0, 1.25, -5.0)) is True


def test_return_above_ground_is_obstacle() -> None:
    source = DepthGridSource(
        cells=8,
        resolution_m=1.0,
        height_tolerance_m=0.1,
        ground_tolerance_m=0.2,
        ground_model=FlatGround(0.0),
    )
    depth = np.full((4, 4), np.nan, dtype=np.float32)
    # Same camera pose, but return at NED down=-2: two metres above ground.
    depth[2, 2] = 3.0
    source.update(depth, (0.0, 0.0, -5.0), INTRINSICS)
    assert bool(source.grid.any())
    assert not bool(source.terrain.any())


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
    # Sensor timestamp domains do not determine freshness; age the local
    # receipt clock explicitly to model a stale frame.
    source._received_monotonic -= 1.0
    assert source.segment_clear((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) is False
    assert fallback.calls == 1
    empty = DepthGridSource(fallback=fallback)
    empty.update(np.full((2, 2), np.nan), (0.0, 0.0, 0.0), INTRINSICS)
    assert empty.segment_clear((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)) is False


def test_freshness_uses_receipt_age_for_explicit_nanosecond_stamps() -> None:
    source = DepthGridSource(max_age_s=0.05, timestamp_unit="ns")
    source.update(
        np.ones((2, 2), dtype=np.float32),
        (0.0, 0.0, 0.0),
        INTRINSICS,
        timestamp=1_700_000_000_000_000_000,
        timestamp_unit="ns",
    )
    assert source._fresh(now=time.monotonic())
    time.sleep(0.06)
    assert not source._fresh(now=time.monotonic())


def test_max_age_zero_and_unknown_timestamp_units_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_age_s"):
        DepthGridSource(max_age_s=0)
    with pytest.raises(ValueError, match="timestamp_unit"):
        DepthGridSource(timestamp_unit="guess")


def test_sensor_timestamp_unit_may_be_overridden_explicitly() -> None:
    source = DepthGridSource(max_age_s=0.5, timestamp_unit="s")
    source.update(
        np.ones((2, 2), dtype=np.float32),
        (0.0, 0.0, 0.0),
        INTRINSICS,
        timestamp=123_456,
        timestamp_unit="us",
    )
    assert source.last_timestamp == pytest.approx(123_456)


def test_depth_encoding_is_explicit_and_validated() -> None:
    source = DepthGridSource()
    integer_mm = np.full((2, 2), 1000, dtype=np.uint16)
    assert source.update(
        integer_mm, (0.0, 0.0, 0.0), INTRINSICS, encoding="UINT16_MM"
    ) == 4
    with pytest.raises(ValueError, match="UINT16_MM"):
        source.update(
            np.ones((2, 2), dtype=np.float32),
            (0.0, 0.0, 0.0),
            INTRINSICS,
            encoding="UINT16_MM",
        )
    with pytest.raises(ValueError, match="FLOAT32_M"):
        source.update(
            integer_mm, (0.0, 0.0, 0.0), INTRINSICS, encoding="FLOAT32_M"
        )


def test_out_of_order_sensor_frame_does_not_replace_newer_generation() -> None:
    source = DepthGridSource(max_age_s=0.5, timestamp_unit="ms")
    depth = np.ones((2, 2), dtype=np.float32)
    assert source.update(
        depth, (0.0, 0.0, 0.0), INTRINSICS, timestamp=2000, timestamp_unit="ms"
    ) == 4
    assert source.update(
        depth, (10.0, 0.0, 0.0), INTRINSICS, timestamp=1000, timestamp_unit="ms"
    ) == 0
    assert source.last_pose == (0.0, 0.0, 0.0)


def test_grid_property_is_copy_and_query_observes_complete_generation() -> None:
    source = DepthGridSource(cells=8, resolution_m=1.0)
    depth = np.full((4, 4), np.nan, dtype=np.float32)
    depth[2, 2] = 5.0
    source.update(depth, (0.0, 0.0, 0.0), INTRINSICS)
    grid = source.grid
    grid[:] = False
    assert source.grid.any()
    # The lock protects a generation while this query reads occupancy and
    # heights; the externally returned grid cannot mutate that generation.
    assert source.segment_clear((-1.0, 1.25, 5.0), (2.0, 1.25, 5.0)) is False


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

