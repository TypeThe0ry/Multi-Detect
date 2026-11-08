from __future__ import annotations

import math

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


def test_target_fov_fixed_mount_keeps_legacy_and_gimbal_uses_world_camera_pose() -> None:
    from multidetect.camera_mount import CameraMount

    intrinsics = {"fx": 100.0, "fy": 100.0, "cx": 320.0, "cy": 240.0}
    args = ((10.0, 0.0, 0.0), (0.0, 0.0, 0.0), 90.0, intrinsics, (640, 480))
    legacy = target_in_fov(*args, camera_pitch_rad=0.0)
    fixed = target_in_fov(*args, camera_pitch_rad=0.0, camera_mount=CameraMount.fixed())
    assert fixed == legacy
    # Vehicle faces east, while the stabilized camera points north at the target.
    gimbal = CameraMount.gimbal(stabilization="lock-horizon")
    aimed = target_in_fov(*args, camera_mount=gimbal)
    assert aimed[0]


def test_gimbal_expert_observation_emits_target_camera_command() -> None:
    from multidetect.camera_mount import CameraMount

    policy = ExpertSearchPolicy(
        target_ned=(10.0, 0.0, 0.0),
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 320.0, "cy": 240.0},
        image_size=(640, 480),
        camera_mount=CameraMount.gimbal(stabilization="lock-horizon"),
        observation_frames=1,
        observation_waypoints=((0.0, 0.0, 0.0),),
    )
    result = policy.labels(pose_ned=(0.0, 0.0, 0.0), yaw_deg=90.0)
    # Without a fresh feedback pose the teacher must not claim that a commanded
    # gimbal position is already visible.
    assert result["target_in_fov"] is False
    assert result["gimbal_command"]["yaw_world_rad"] == pytest.approx(0.0)
    assert result["gimbal_command"]["mode"] == "lock-horizon"


def test_gimbal_teacher_can_command_x500_nadir_limit() -> None:
    from multidetect.autonomy.airframe_profile import load_airframe_profile

    profile = load_airframe_profile("configs/airframes/x500_gimbal.json")
    assert profile.camera_mount is not None
    policy = ExpertSearchPolicy(
        target_ned=(0.1, 0.0, 0.0),
        intrinsics={"fx": 100.0, "fy": 100.0, "cx": 320.0, "cy": 240.0},
        image_size=(640, 480),
        camera_mount=profile.camera_mount.to_camera_mount(),
    )
    command = policy.labels(pose_ned=(0.0, 0.0, -10.0))["gimbal_command"]
    assert command["pitch_down_rad"] == pytest.approx(math.atan2(10.0, 0.1), abs=1e-6)
    assert command["pitch_down_rad"] > math.radians(45.0)


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


def test_fixed_wing_expert_search_skips_in_place_scan_and_enters_route() -> None:
    policy = ExpertSearchPolicy(
        target_ned=(1.0e7, 1.0e7, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
        fixed_wing_mode=True,
    )

    first = policy.next_action(pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
    assert policy._initial_scan_remaining == 0
    assert first.kind == "translate"
    assert math.hypot(first.forward_m, first.right_m) > 0.0


def test_hover_expert_search_keeps_initial_turn_scan() -> None:
    policy = ExpertSearchPolicy(
        target_ned=(1.0e7, 1.0e7, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
    )

    action = policy.next_action(pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)

    assert action.kind == "turn"
    assert policy._initial_scan_remaining == 3


def test_intent_observation_climbs_routes_then_captures_each_view() -> None:
    policy = ExpertSearchPolicy(
        target_ned=(20.0, 40.0, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0},
        altitude_m=22.0,
        home_ned=(20.0, 0.0, 0.0),
        observation_frames=3,
        observation_waypoints=((20.0, 0.0, -22.0), (20.0, 10.0, -22.0)),
    )
    pose = [0.0, 0.0, 0.0]
    yaw = 0.0
    first = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
    assert first.kind == "translate" and first.up_m > 0
    pose[2] = -22.0
    action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
    assert action.kind == "translate" and abs(action.forward_m) + abs(action.right_m) > 0
    pose[0] = 20.0
    while policy.state != "observe":
        action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
        if action.kind == "turn":
            yaw = (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
    assert policy._observation_count == 1
    for expected in (2, 3):
        action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
        if expected == 2:
            assert policy._observation_count == expected and action.kind == "hold"
        else:
            assert policy._observation_count == 0 and policy._observation_index == 1
            assert action.kind == "hold"
    # The next view is routed before its frames are counted; it cannot silently
    # reuse the first view's orbit position.
    action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
    assert policy._observation_index == 1
    assert policy._observation_count == 0
    assert action.kind == "translate"
    pose[1] = 10.0
    seen = 0
    while seen == 0:
        action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
        if policy._observation_sample_index == 1:
            seen += 1
        elif action.kind == "turn":
            yaw = (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
    for expected in (2, 3):
        action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
        assert policy._observation_index == 1
        assert action.kind == ("stop" if expected == 3 else "hold")


def test_fixed_wing_observation_orbits_center_and_counts_only_fov_frames() -> None:
    from multidetect.autonomy.motion_intent import MotionCommand

    policy = ExpertSearchPolicy(
        target_ned=(0.0, 55.0, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
        image_size=(1280, 960),
        altitude_m=30.0,
        camera_pitch_rad=0.18,
        observation_frames=3,
        observation_waypoints=((0.0, 0.0, -30.0),),
        fixed_wing_orbit_radius_m=80.0,
    )
    pose = (-20.0, -20.0, -5.0)
    outside = policy.next_action(pose_ned=pose, yaw_deg=0.0)
    assert isinstance(outside, MotionCommand)
    assert outside.kind == "orbit"
    assert outside.target_ned == (0.0, 0.0, -30.0)
    assert policy._observation_count == 0
    # The first command itself establishes the orbit and its 30 m altitude;
    # there is no point-to-point or vertical translate phase for a fixed wing.
    pose = (0.0, 0.0, -30.0)
    for expected in (1, 2, 3):
        action = policy.next_action(pose_ned=pose, yaw_deg=90.0)
        if expected < 3:
            assert isinstance(action, MotionCommand) and action.kind == "orbit"
        else:
            assert action.kind == "stop"
        assert policy._observation_count == expected


def test_intent_observation_records_each_view_before_advancing() -> None:
    policy = ExpertSearchPolicy(
        target_ned=(20.0, 40.0, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0},
        altitude_m=20.0,
        home_ned=(20.0, 0.0, 0.0),
        observation_frames=2,
        observation_waypoints=((20.0, 0.0, -20.0), (20.0, 0.0, -20.0)),
    )
    pose = [20.0, 0.0, -20.0]
    yaw = 90.0
    first = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
    assert policy.labels(pose_ned=tuple(pose), yaw_deg=yaw)["observation_view_index"] == 0
    second = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
    assert second.kind == "hold"
    assert policy.labels(pose_ned=tuple(pose), yaw_deg=yaw)["observation_frame_index"] == 2
    assert policy._observation_index == 1
    for _ in range(20):
        action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
        labels = policy.labels(pose_ned=tuple(pose), yaw_deg=yaw)
        if "observation_view_index" in labels:
            assert labels["observation_view_index"] == 1
            assert labels["observation_frame_index"] == 1
            break
        if action.kind == "turn":
            yaw = (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
        elif action.kind == "translate":
            pose[0] += action.forward_m
            pose[1] += action.right_m
    else:
        raise AssertionError("second observation view was not reached")


def test_expert_search_advances_when_target_not_visible() -> None:
    policy = ExpertSearchPolicy(target_ned=(0.0, 30.0, 0.0), intrinsics={"fx": 100.0, "fy": 100.0})
    action = policy.next_action(pose_ned=(0.0, 0.0, 0.0), yaw_deg=0.0)
    assert policy.state in {"scan", "search"}
    assert action.kind in {"turn", "translate"}


def test_expert_search_can_finish_one_route_without_changing_default_restart() -> None:
    import math

    def run(policy: ExpertSearchPolicy) -> tuple[int, str]:
        pose = [0.0, 0.0, -10.0]
        yaw = 0.0
        for step in range(200):
            action = policy.next_action(pose_ned=tuple(pose), yaw_deg=yaw)
            if action.kind == "stop":
                return step, policy.search_outcome
            if action.kind == "turn":
                yaw = (yaw + action.yaw_delta_deg + 180.0) % 360.0 - 180.0
            elif action.kind == "translate":
                heading = math.radians(yaw)
                pose[0] += action.forward_m * math.cos(heading) - action.right_m * math.sin(heading)
                pose[1] += action.forward_m * math.sin(heading) + action.right_m * math.cos(heading)
        return 200, policy.search_outcome

    kwargs = {
        "target_ned": (1.0e7, 1.0e7, 0.0),
        "intrinsics": {"fx": 540.0, "fy": 540.0},
        "image_size": (1280, 960),
        "altitude_m": 10.0,
        "search_radius_m": 8.0,
    }
    single_pass = ExpertSearchPolicy(**kwargs, restart_route=False)
    steps, outcome = run(single_pass)
    assert steps < 200 and outcome == "coverage_complete"

    looping = ExpertSearchPolicy(**kwargs)
    steps, outcome = run(looping)
    assert steps == 200 and outcome == "running"
    assert looping.restart_route is True


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


def test_a_vehicle_inside_a_clearance_ring_may_climb_or_leave_but_not_approach() -> None:
    from multidetect.sim_teacher.policies import SDFObstacle, segment_clear_of_obstacles

    house = SDFObstacle(
        north_m=28.8, east_m=-2.3, height_m=7.2, size_north_m=9.8, size_east_m=12.7, yaw_rad=0.0
    )
    start = (20.0, 4.0, -8.0)  # 10.8 m from the house centre: inside its 11 m ring
    assert segment_clear_of_obstacles(start, (20.0, 4.0, -12.0), 8.0, (house,))  # climb
    assert segment_clear_of_obstacles(start, (10.0, 10.0, -8.0), 8.0, (house,))  # leave
    assert not segment_clear_of_obstacles(start, (27.0, 0.0, -8.0), 8.0, (house,))  # approach
    assert not segment_clear_of_obstacles(start, (20.0, 4.0, -4.0), 8.0, (house,))  # descend
    outside = (5.0, 4.0, -8.0)
    assert not segment_clear_of_obstacles(outside, (20.0, 4.0, -8.0), 8.0, (house,))  # enter


def test_a_descent_below_a_roof_under_the_start_altitude_is_not_clear() -> None:
    from multidetect.sim_teacher.policies import SDFObstacle, segment_clear_of_obstacles

    # gen_cnt2_w0001 obstacle_01: a 14.8 m apartment block.  A count run hovered
    # over its roof at 17.2 m, saw a hydrant 17 m away and descended 4 m toward
    # it: the roof was 2.4 m below the start altitude, so the map skipped it and
    # the vehicle flew into the roof (SITL loopfix/relook3, 3 of 4 runs).
    apartment = SDFObstacle(
        north_m=-24.641,
        east_m=-10.416,
        height_m=14.8046,
        size_north_m=22.2701,
        size_east_m=20.2572,
        yaw_rad=1.019681,
    )
    start = (-27.2, -17.0, -17.2)
    toward = (-30.4, -18.8)
    # Level above the roof (more than 2 m clear) stays allowed.
    assert segment_clear_of_obstacles(start, (*toward, -17.2), 17.2, (apartment,))
    assert not segment_clear_of_obstacles(start, (*toward, -13.2), 17.2, (apartment,))
    assert not segment_clear_of_obstacles(start, (-27.2, -17.0, -13.2), 17.2, (apartment,))
    # Away from the block the same descent is clear.
    far = (-40.0, -40.0, -17.2)
    assert segment_clear_of_obstacles(far, (-38.0, -38.0, -13.2), 17.2, (apartment,))


def _apartment():
    from multidetect.sim_teacher.policies import SDFObstacle

    return SDFObstacle(
        north_m=-24.641,
        east_m=-10.416,
        height_m=14.8046,
        size_north_m=22.2701,
        size_east_m=20.2572,
        yaw_rad=1.019681,
    )


def test_a_lane_blocked_only_below_the_search_altitude_climbs_instead_of_skipping() -> None:
    from multidetect.sim_teacher.primitives import ActionPrimitive

    # SITL count-fire-hydrants 54ef68e5 (gen_cnt2_w0001): the search altitude is
    # the 14.8 m apartment + 2 m (16.8 m), but a closest-view climb back settled at
    # 16.4 m, inside the loop's 1 m tolerance.  Below 16.8 m the roof is an
    # obstacle with an 18 m ring, so the west lane over it was sidestepped three
    # times, its waypoints skipped and the coverage "completed" 45 m short of the
    # hydrant next to the block (2/3).  At 17.2 m (every other run) it is clear.
    apartment = _apartment()
    policy = ExpertSearchPolicy(
        target_ned=(1.0e7, 1.0e7, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0},
        altitude_m=apartment.height_m + 2.0,
        obstacles=(apartment,),
        search_radius_m=55.0,
    )
    leg = ActionPrimitive.translate(15.0, 0.0, 0.0)
    low = (-28.52, 12.96, -16.41)
    action = policy._safe_translation(low, leg, -93.5)
    assert action.kind == "translate"
    assert action.forward_m == 0.0 and action.right_m == 0.0
    # Back above the planned altitude with a margin, not merely onto it.
    assert -low[2] + action.up_m >= policy.altitude_m + 0.3
    assert policy.skipped_waypoints == []
    # From the climbed altitude the same lane is flown as planned.
    high = (low[0], low[1], low[2] - action.up_m)
    assert policy._safe_translation(high, leg, -93.5) == leg


def test_a_lane_still_blocked_above_the_search_altitude_keeps_its_sidestep() -> None:
    from multidetect.sim_teacher.primitives import ActionPrimitive

    # A roof above the search altitude is not climbed over: the lane is
    # sidestepped as before, and a vehicle that does not gain height after
    # climbing goes back to sidesteps instead of climbing forever.
    apartment = _apartment()
    policy = ExpertSearchPolicy(
        target_ned=(1.0e7, 1.0e7, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0},
        altitude_m=12.0,
        obstacles=(apartment,),
        search_radius_m=55.0,
    )
    leg = ActionPrimitive.translate(15.0, 0.0, 0.0)
    action = policy._safe_translation((-28.52, 12.96, -12.0), leg, -93.5)
    assert not (action.kind == "translate" and action.up_m > 0.0)

    stuck = ExpertSearchPolicy(
        target_ned=(1.0e7, 1.0e7, 0.0),
        intrinsics={"fx": 540.0, "fy": 540.0},
        altitude_m=apartment.height_m + 2.0,
        obstacles=(apartment,),
        search_radius_m=55.0,
    )
    climbs = 0
    for _ in range(6):
        action = stuck._safe_translation((-28.52, 12.96, -16.41), leg, -93.5)
        climbs += action.kind == "translate" and action.up_m > 0.0
    assert 1 <= climbs <= 2
