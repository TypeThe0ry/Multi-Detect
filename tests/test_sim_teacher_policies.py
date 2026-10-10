from __future__ import annotations

import pytest

from multidetect.sim_teacher.policies import (
    ExpertSearchPolicy,
    ExploreBounds,
    ExplorePolicy,
    target_in_fov,
)


def test_explore_seeded_determinism_and_bounds() -> None:
    bounds = ExploreBounds(
        min_altitude_m=3.0,
        max_altitude_m=10.0,
        box_min_north_m=-20.0,
        box_max_north_m=20.0,
        box_min_east_m=-20.0,
        box_max_east_m=20.0,
    )
    p1 = ExplorePolicy(seed=123, bounds=bounds)
    p2 = ExplorePolicy(seed=123, bounds=bounds)
    p1.reset(altitude_m=5.0)
    p2.reset(altitude_m=5.0)
    for _ in range(30):
        assert p1.next_action() == p2.next_action()
        assert bounds.min_altitude_m <= p1.altitude <= bounds.max_altitude_m
        assert bounds.box_min_north_m <= p1.north <= bounds.box_max_north_m
        assert bounds.box_min_east_m <= p1.east <= bounds.box_max_east_m


def test_target_fov_projection_and_range() -> None:
    intrinsics = {"fx": 100.0, "fy": 100.0, "cx": 320.0, "cy": 240.0}
    inside, bearing, distance = target_in_fov(
        (10.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, intrinsics, (640, 480)
    )
    assert inside and abs(bearing) < 1e-6 and distance == pytest.approx(10.0)
    outside, _, _ = target_in_fov((10.0, 100.0, 0.0), (0.0, 0.0, 0.0), 0.0, intrinsics, (640, 480))
    assert not outside
    behind, _, _ = target_in_fov((-1.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, intrinsics, (640, 480))
    assert not behind


def test_expert_search_state_machine() -> None:
    policy = ExpertSearchPolicy(
        target_ned=(20.0, 0.0, 0.0),
        intrinsics={"fx": 100.0, "fy": 100.0},
        image_size=(640, 480),
        standoff_m=8.0,
        hold_seconds=0.1,
    )
    # In FOV but too far: transition search -> approach and move toward standoff.
    action = policy.next_action(pose_ned=(0.0, 0.0, 0.0), yaw_deg=0.0)
    assert policy.state == "approach"
    assert action.kind == "translate" and action.forward_m <= 15.0
    # At standoff: hold, then stop.
    action = policy.next_action(pose_ned=(12.0, 0.0, 0.0), yaw_deg=0.0)
    assert policy.state == "hold" and action.kind == "hold"
    action = policy.next_action(pose_ned=(12.0, 0.0, 0.0), yaw_deg=0.0)
    assert policy.state == "done" and action.kind == "stop"
    assert policy.target_in_fov
    labels = policy.labels(pose_ned=(12.0, 0.0, 0.0), yaw_deg=0.0)
    assert {"phase", "target_in_fov", "target_bearing_deg", "target_range_m"} <= set(labels)


def test_expert_search_advances_when_target_not_visible() -> None:
    policy = ExpertSearchPolicy(target_ned=(0.0, 30.0, 0.0), intrinsics={"fx": 100.0, "fy": 100.0})
    action = policy.next_action(pose_ned=(0.0, 0.0, 0.0), yaw_deg=0.0)
    assert policy.state in {"scan", "search"}
    assert action.kind in {"turn", "translate"}
