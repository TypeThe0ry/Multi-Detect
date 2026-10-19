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


def _fly(pose: list[float], yaw: float, action) -> float:
    import math

    if action.kind == "turn":
        return (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
    if action.kind == "translate":
        heading = math.radians(yaw)
        pose[0] += action.forward_m * math.cos(heading) - action.right_m * math.sin(heading)
        pose[1] += action.forward_m * math.sin(heading) + action.right_m * math.cos(heading)
    return yaw


def test_expert_search_stays_in_its_zone_after_someone_else_turns_the_vehicle() -> None:
    import math

    policy = ExpertSearchPolicy(
        target_ned=(1.0e7, 1.0e7, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0},
        altitude_m=8.0,
        search_radius_m=60.0,
    )
    pose, yaw = [0.0, 0.0, -8.0], 0.0
    farthest = 0.0
    for step in range(200):
        if step in (20, 45, 90):
            yaw = (yaw + 140.0 + 180.0) % 360.0 - 180.0  # a host approach turned it
        yaw = _fly(pose, yaw, policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw))
        farthest = max(farthest, math.hypot(pose[0], pose[1]))
    assert farthest <= 60.0 + 1e-6
