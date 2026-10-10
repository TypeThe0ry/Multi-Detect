from __future__ import annotations

import math
import threading
import time

import pytest

from multidetect.sim_teacher.streams import (
    MavlinkReader,
    SensorSample,
    build_observation,
)


def _reader_with_pose(stamp_ms: int = 1000) -> MavlinkReader:
    reader = MavlinkReader(object())
    rows = {
        "LOCAL_POSITION_NED": {
            "time_boot_ms": stamp_ms,
            "x": 1.0,
            "y": 2.0,
            "z": -3.0,
            "vx": 0.1,
            "vy": 0.2,
            "vz": 0.3,
        },
        "ATTITUDE": {"time_boot_ms": stamp_ms, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "GLOBAL_POSITION_INT": {
            "time_boot_ms": stamp_ms,
            "lat": 10_000_000,
            "lon": 20_000_000,
            "relative_alt": 4000,
        },
    }
    for name, row in rows.items():
        reader._record_pose_message(name, row, stamp_ms * 1_000_000, 1.0)
        reader.state.messages[name] = row
    reader.state.messages["GPS_RAW_INT"] = {"fix_type": 3, "satellites_visible": 10}
    return reader


def test_pose_history_interpolates_position_and_wraps_yaw() -> None:
    reader = MavlinkReader(object())
    local_a = {
        "time_boot_ms": 1000,
        "x": 0.0,
        "y": 10.0,
        "z": 0.0,
        "vx": 0.0,
        "vy": 0.0,
        "vz": 0.0,
    }
    local_b = {
        "time_boot_ms": 1100,
        "x": 10.0,
        "y": 20.0,
        "z": 2.0,
        "vx": 1.0,
        "vy": 2.0,
        "vz": 3.0,
    }
    attitude_a = {"time_boot_ms": 1000, "roll": 0.0, "pitch": 0.0, "yaw": math.radians(359)}
    attitude_b = {"time_boot_ms": 1100, "roll": 0.0, "pitch": 0.0, "yaw": math.radians(1)}
    global_a = {"time_boot_ms": 1000, "lat": 1, "lon": 2, "relative_alt": 3}
    global_b = {"time_boot_ms": 1100, "lat": 101, "lon": 202, "relative_alt": 103}
    for name, first, second in (
        ("LOCAL_POSITION_NED", local_a, local_b),
        ("ATTITUDE", attitude_a, attitude_b),
        ("GLOBAL_POSITION_INT", global_a, global_b),
    ):
        reader._record_pose_message(name, first, 0, 0.0)
        reader._record_pose_message(name, second, 0, 0.0)

    state = reader.aligned_state(1_050_000_000, timeout_s=0.01)
    assert state.pose_alignment == {
        "method": "interpolated",
        "source_time_boot_ms": 1050,
        "residual_skew_ms": 0.0,
    }
    assert state.messages["LOCAL_POSITION_NED"]["x"] == 5.0
    assert state.messages["LOCAL_POSITION_NED"]["y"] == 15.0
    assert math.degrees(state.messages["ATTITUDE"]["yaw"]) == pytest.approx(360.0)


def test_pose_alignment_wait_for_newer_pose_is_bounded() -> None:
    reader = _reader_with_pose(1000)
    started = time.monotonic()
    state = reader.aligned_state(1_100_000_000, timeout_s=0.03)
    elapsed = time.monotonic() - started
    assert elapsed < 0.25
    assert state.pose_alignment is not None
    assert state.pose_alignment["method"] == "nearest_before"
    assert state.pose_alignment["source_time_boot_ms"] == 1000
    assert state.pose_alignment["residual_skew_ms"] == 100.0


def test_pose_history_thread_safety_smoke() -> None:
    reader = MavlinkReader(object())
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for stamp in range(1000, 1200):
                for name, row in (
                    ("LOCAL_POSITION_NED", {"time_boot_ms": stamp, "x": float(stamp)}),
                    ("ATTITUDE", {"time_boot_ms": stamp, "yaw": 0.0}),
                    ("GLOBAL_POSITION_INT", {"time_boot_ms": stamp, "lat": stamp}),
                ):
                    reader._record_pose_message(name, row, 0, 0.0)
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for stamp in range(1000, 1200):
            reader.aligned_state(stamp * 1_000_000, timeout_s=0.0)
    except BaseException as exc:
        errors.append(exc)
    thread.join(timeout=1.0)
    assert not errors
    with reader._pose_condition:
        for history in reader._pose_history.values():
            assert [sample[0] for sample in history] == sorted(sample[0] for sample in history)


def test_build_observation_exposes_interpolated_alignment_with_zero_residual() -> None:
    reader = _reader_with_pose(1000)
    reader.state.pose_alignment = {
        "method": "interpolated",
        "source_time_boot_ms": 1000,
        "residual_skew_ms": 0.0,
    }
    sensor = SensorSample(
        rgb=bytes([0, 1, 2]),
        width=1,
        height=1,
        depth=None,
        camera_info={},
        pose={},
        velocity=[0.0, 0.0, 0.0],
        sensor_stamp_ns=1_000_000_000,
        received_monotonic_ns=0,
        received_wall_time=0.0,
    )
    observation = build_observation(sensor, reader.state)
    assert observation["pose_alignment"]["method"] == "interpolated"
    assert observation["pose_alignment"]["residual_skew_ms"] == 0.0
    assert observation["sim_skew_ms"] == 0.0
