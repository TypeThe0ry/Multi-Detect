from __future__ import annotations

import math

import pytest

from scripts.gz_mover_driver import parse_clock_text, pose_request, trajectory_position_yaw


def test_parse_clock_text_accepts_gz_text_proto() -> None:
    message = (
        "header {\n  stamp {\n    sec: 99\n  }\n}\n"
        "system {\n  sec: 1792250000\n  nsec: 1\n}\n"
        "real {\n  sec: 30\n  nsec: 7\n}\n"
        "sim {\n  sec: 12\n  nsec: 500000000\n}\n"
    )
    # Only the sim block is simulation time; header/system/real must be ignored.
    assert parse_clock_text(message) == pytest.approx(12.5)
    assert parse_clock_text("real {\n  sec: 3\n}\n") is None
    assert parse_clock_text("sim {\n  nsec: 4\n}\n") is None


def test_position_and_yaw_match_constant_speed_loop() -> None:
    trajectory = {"speed_mps": 2.0, "waypoints": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]}
    assert trajectory_position_yaw(trajectory, 1.0) == pytest.approx((2.0, 0.0, 0.0))
    assert trajectory_position_yaw(trajectory, 2.0) == pytest.approx((4.0, 0.0, 0.0))
    assert trajectory_position_yaw(trajectory, 3.0) == pytest.approx((4.0, 2.0, math.pi / 2))
    assert trajectory_position_yaw(trajectory, 8.0) == pytest.approx((0.0, 0.0, 0.0))


def test_pose_request_contains_enu_pose_and_yaw_quaternion() -> None:
    request = pose_request("target", 1.0, 2.0, 0.9, math.pi / 2)
    assert 'name: "target"' in request
    assert "x: 1.000000" in request and "y: 2.000000" in request
    assert "z: 0.707106781" in request and "w: 0.707106781" in request
