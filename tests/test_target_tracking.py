"""CPU target tracking through independent bridge decode and physical C11 FK."""

from __future__ import annotations

import copy
import math
import random

import pytest

from multidetect.sim_teacher.horizon_stabilization import (
    HorizonStabilizer,
    joint_command_quaternion,
    rotation_deg,
)
from multidetect.sim_teacher.target_tracking import (
    WorldTarget,
    pixel_roi_ray,
    solve_track_target,
    track_horizon_error_deg,
    unit,
    world_offset_ned,
    world_target_direction,
    world_target_from_mavlink,
)
from scripts.realworld.horizon_report import LEGACY_LIMITS, percentile

AXES = ("roll", "pitch", "yaw")
ZERO = dict.fromkeys(AXES, 0.0)


def multiply(a, b):
    return tuple(
        sum(a[3 * i + k] * b[3 * k + j] for k in range(3)) for i in range(3) for j in range(3)
    )


def axis_rotation(axis, degrees):
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    if axis == "roll":
        return (1.0, 0.0, 0.0, 0.0, c, -s, 0.0, s, c)
    if axis == "pitch":
        return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)
    return (c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0)


def fk(body, joints):
    result = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    for angles, order in ((body, ("yaw", "pitch", "roll")), (joints, ("yaw", "roll", "pitch"))):
        for axis in order:
            result = multiply(result, axis_rotation(axis, angles[axis]))
    return result


def decode_carrier(joints):
    w, x, y, z = joint_command_quaternion(joints)
    return dict(
        zip(
            AXES,
            map(
                math.degrees,
                (
                    math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)),
                    math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))),
                    math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)),
                ),
            ),
            strict=True,
        )
    )


def independent_error(direction, rotation):
    a, b = unit(direction), rotation[0::3]
    return math.degrees(
        math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True)))))
    )


def grid_score(direction, limits, steps=24):
    best = -math.inf
    for ir in range(steps + 1):
        r = math.radians(limits["roll"][0] + ir / steps * (limits["roll"][1] - limits["roll"][0]))
        for ip in range(steps + 1):
            p = math.radians(
                limits["pitch"][0] + ip / steps * (limits["pitch"][1] - limits["pitch"][0])
            )
            x, y, z = math.cos(p), math.sin(r) * math.sin(p), -math.cos(r) * math.sin(p)
            for iy in range(steps + 1):
                yaw = math.radians(
                    limits["yaw"][0] + iy / steps * (limits["yaw"][1] - limits["yaw"][0])
                )
                q = (
                    math.cos(yaw) * x - math.sin(yaw) * y,
                    math.sin(yaw) * x + math.cos(yaw) * y,
                    z,
                )
                best = max(best, sum(a * b for a, b in zip(direction, q, strict=True)))
    return best


def test_world_target_uses_amsl_and_ned_with_body_transform():
    origin = WorldTarget(40.0, -93.0, 400.0)
    below = WorldTarget(40.0, -93.0, 300.0)
    assert world_offset_ned(origin, below) == pytest.approx((0.0, 0.0, 100.0), abs=1e-8)
    assert world_target_direction(origin, below) == pytest.approx((0.0, 0.0, 1.0), abs=1e-10)
    north = WorldTarget(40.001, -93.0, 300.0)
    offset = world_offset_ned(origin, north)
    assert offset[0] == pytest.approx(111.04, abs=0.05)
    assert offset[1] == pytest.approx(0.0, abs=1e-8)
    assert offset[2] == pytest.approx(100.0, abs=0.01)
    body = {"roll": 10.0, "pitch": -60.0, "yaw": 40.0}
    rotation = fk(body, ZERO)
    expected = tuple(sum(rotation[3 * j + i] * unit(offset)[j] for j in range(3)) for i in range(3))
    assert world_target_direction(origin, north, rotation) == pytest.approx(expected)
    assert (
        world_target_from_mavlink(
            {"lat": 400000000, "lon": -930000000, "alt": 400000, "relative_alt": 10000}
        )
        == origin
    )
    assert world_target_from_mavlink({"lat": None}) is None
    with pytest.raises(ValueError):
        world_target_direction(origin, origin)


@pytest.mark.parametrize("value", ["91,0,1", "0,181,1", "nan,0,1", "1,2", "1,2,3,4"])
def test_world_target_parse_rejects_invalid(value):
    with pytest.raises(ValueError):
        WorldTarget.parse(value)


def test_pixel_roi_reserved_adapter_centres_and_frd_signs():
    intrinsics = dict(fx_px=400.0, fy_px=200.0, cx_px=320.0, cy_px=240.0)
    assert pixel_roi_ray(intrinsics, pixel=(320.0, 240.0)) == (1.0, 0.0, 0.0)
    expected = unit((1.0, 0.5, 0.25))
    assert pixel_roi_ray(intrinsics, pixel=(520.0, 290.0)) == pytest.approx(expected)
    assert pixel_roi_ray(intrinsics, roi_xyxy=(500.0, 280.0, 540.0, 300.0)) == pytest.approx(
        expected
    )
    assert pixel_roi_ray(intrinsics, pixel=(120.0, 190.0)) == pytest.approx(
        unit((1.0, -0.5, -0.25))
    )


@pytest.mark.parametrize(
    "intrinsics,kwargs",
    [
        ({}, {"pixel": (0.0, 0.0)}),
        ({"fx_px": 0, "fy_px": 1, "cx_px": 0, "cy_px": 0}, {"pixel": (0.0, 0.0)}),
        ({"fx_px": 1, "fy_px": 1, "cx_px": 0, "cy_px": 0}, {}),
        (
            {"fx_px": 1, "fy_px": 1, "cx_px": 0, "cy_px": 0},
            {"pixel": (0.0, 0.0), "roi_xyxy": (0, 0, 1, 1)},
        ),
        ({"fx_px": 1, "fy_px": 1, "cx_px": 0, "cy_px": 0}, {"roi_xyxy": (1, 1, 0, 2)}),
        ({"fx_px": 1, "fy_px": 1, "cx_px": 0, "cy_px": 0}, {"pixel": (math.nan, 0.0)}),
    ],
)
def test_pixel_roi_rejects_invalid(intrinsics, kwargs):
    with pytest.raises(ValueError):
        pixel_roi_ray(intrinsics, **kwargs)


def test_pointing_precedes_horizon_when_roll_stop_prevents_level_image():
    body = {"roll": 80.0, "pitch": 0.0, "yaw": 0.0}
    joints, _, geometry = solve_track_target(
        rotation_deg(body), (1.0, 0.0, 0.0), ZERO, LEGACY_LIMITS
    )
    measured = fk(body, joints)
    assert independent_error((1.0, 0.0, 0.0), measured) < 1e-6
    assert geometry["target_reachable"] is True
    assert geometry["horizon_feasible_given_target"] is False
    assert track_horizon_error_deg(measured) == pytest.approx(35.0, abs=1e-6)
    assert joints["roll"] == pytest.approx(-45.0)


def test_vertical_pointing_has_explicit_undefined_horizon():
    limits = {"roll": (-45.0, 45.0), "pitch": (-135.0, 45.0), "yaw": (-179.9, 179.9)}
    joints, _, geometry = solve_track_target(rotation_deg(ZERO), (0.0, 0.0, 1.0), ZERO, limits)
    assert independent_error((0.0, 0.0, 1.0), fk(ZERO, joints)) < 1e-6
    assert geometry["target_reachable"] is True
    assert geometry["horizon_feasible_given_target"] is None
    assert track_horizon_error_deg(fk(ZERO, joints)) is None


def test_unreachable_upward_ray_projects_to_pitch_stop():
    joints, _, geometry = solve_track_target(
        rotation_deg(ZERO), (0.0, 0.0, -1.0), ZERO, LEGACY_LIMITS
    )
    assert geometry["target_reachable"] is False
    assert joints["pitch"] == pytest.approx(45.0)
    assert joints["roll"] == pytest.approx(0.0, abs=1e-6)
    assert geometry["target_pointing_min_error_deg"] == pytest.approx(45.0)
    assert independent_error((0.0, 0.0, -1.0), fk(ZERO, joints)) == pytest.approx(45.0)


@pytest.mark.parametrize("seed", range(8))
def test_pointing_projection_beats_independent_dense_joint_grid(seed):
    randomizer = random.Random(seed)
    limits = {"roll": (-30.0, 35.0), "pitch": (-60.0, 25.0), "yaw": (-50.0, 40.0)}
    direction = unit(tuple(randomizer.uniform(-1.0, 1.0) for _ in range(3)))
    joints, _, geometry = solve_track_target(rotation_deg(ZERO), direction, ZERO, limits)
    score = sum(a * b for a, b in zip(direction, fk(ZERO, joints)[0::3], strict=True))
    assert score >= grid_score(direction, limits) - 1e-10
    assert geometry["target_reachable"] is (geometry["target_pointing_min_error_deg"] < 1e-6)
    for axis in AXES:
        assert limits[axis][0] - 1e-8 <= joints[axis] <= limits[axis][1] + 1e-8


def test_hover_turn_and_transition_world_target_sequences_through_bridge_fk():
    target = WorldTarget(40.00052, -93.0, 300.0)
    position = WorldTarget(40.0, -93.0, 385.0)
    controller = HorizonStabilizer(mode="track", track_targets=(target,), limits_deg=LEGACY_LIMITS)
    stages = [
        ("hover", {"roll": 0.0, "pitch": -85.0, "yaw": 0.0}),
        ("hover", {"roll": 12.0, "pitch": -78.0, "yaw": 5.0}),
        ("transition", {"roll": 15.0, "pitch": -35.0, "yaw": 10.0}),
        ("fixed_wing_turn", {"roll": 30.0, "pitch": -5.0, "yaw": 15.0}),
        ("fixed_wing_turn", {"roll": -25.0, "pitch": -5.0, "yaw": -10.0}),
        ("transition", {"roll": -10.0, "pitch": -65.0, "yaw": -5.0}),
        ("hover", {"roll": 0.0, "pitch": -85.0, "yaw": 0.0}),
    ]
    errors = {stage: [] for stage, _ in stages}
    tick = 0
    previous = dict(controller.last_joint)
    # Settle before recording; initial actuator slew is deliberately retained.
    for _ in range(100):
        now = tick * 0.02
        result = controller.update(stages[0][1], now_s=now, attitude_time_s=now, position=position)
        tick += 1
    previous = result["joint_deg"]
    for (_, start), (stage, end) in zip(stages[:-1], stages[1:], strict=True):
        for step in range(1, 301):
            body = {axis: start[axis] + step / 300 * (end[axis] - start[axis]) for axis in AXES}
            now = tick * 0.02
            result = controller.update(body, now_s=now, attitude_time_s=now, position=position)
            tick += 1
            joints = decode_carrier(result["joint_deg"])
            assert joints == pytest.approx(result["joint_deg"], abs=1e-7)
            assert all(abs(joints[a] - previous[a]) <= 3.6 + 1e-7 for a in AXES)
            previous = dict(joints)
            if result["target_reachable"]:
                error = independent_error(
                    world_target_direction(position, target), fk(body, joints)
                )
                assert result["pointing_error_deg"] == pytest.approx(error, abs=2e-6)
                errors[stage].append(error)
    for stage, values in errors.items():
        assert values, stage
        assert percentile(values, 0.95) <= 2.0, (stage, percentile(values, 0.95))
        assert max(values) <= 5.0, (stage, max(values))


def test_lock_track_switches_keep_joint_slew_and_converge():
    controller = HorizonStabilizer(limits_deg=LEGACY_LIMITS)
    body = {"roll": 20.0, "pitch": -70.0, "yaw": 20.0}
    previous = ZERO
    tick = 0
    for mode in ("lock", "track", "lock", "track"):
        controller.set_mode(mode)
        for _ in range(100):
            now = tick * 0.02
            result = controller.update(
                body, now_s=now, attitude_time_s=now, track_direction_ned=(0.8, 0.2, 0.6)
            )
            tick += 1
            assert all(abs(result["joint_deg"][a] - previous[a]) <= 3.6 + 1e-7 for a in AXES)
            previous = dict(result["joint_deg"])
        if mode == "track":
            assert result["target_reachable"]
            assert independent_error((0.8, 0.2, 0.6), fk(body, decode_carrier(previous))) <= 2.0


def test_nearest_world_target_selection_ties_use_input_order():
    origin = WorldTarget(40.0, -93.0, 385.0)
    first = WorldTarget(40.00052, -93.0, 300.0)
    second = WorldTarget(40.002, -93.0, 300.0)
    controller = HorizonStabilizer(
        mode="track", track_targets=(first, first, second), limits_deg=LEGACY_LIMITS
    )
    result = controller.update(ZERO, now_s=0.0, attitude_time_s=0.0, position=origin)
    assert result["target_ref"] == first.reference()


def test_invalid_or_stale_track_input_holds_last_and_does_not_claim_reachable():
    controller = HorizonStabilizer(mode="track", limits_deg=LEGACY_LIMITS)
    controller.update(ZERO, now_s=0.0, attitude_time_s=0.0, track_direction_ned=(1, 0, 1))
    good = controller.update(ZERO, now_s=0.02, attitude_time_s=0.02, track_direction_ned=(1, 0, 1))
    for now, stamp, kwargs in (
        (0.04, 0.04, {}),
        (0.06, 0.0, {"track_direction_ned": (0, 0, 0)}),
        (0.3, 0.0, {"track_direction_ned": (1, 0, 1)}),
    ):
        result = controller.update(ZERO, now_s=now, attitude_time_s=stamp, **kwargs)
        assert result["joint_deg"] == good["joint_deg"]
        assert result["target_reachable"] is None
        assert result["pointing_error_deg"] is None
        assert result["degraded"]


def test_frame_annotation_uses_past_world_target_measured_fk_and_camera_centre():
    origin = WorldTarget(40.0, -93.0, 385.0)
    target = WorldTarget(40.00052, -93.0, 300.0)
    later_target = WorldTarget(40.0, -92.999, 300.0)
    controller = HorizonStabilizer(mode="track", track_targets=(target,), limits_deg=LEGACY_LIMITS)
    body = {"roll": 0.0, "pitch": -70.0, "yaw": 0.0}
    command = controller.update(body, now_s=1.0, attitude_time_s=1.0, position=origin)
    controller.record_command(command, 1.0)
    controller.track_targets = (later_target,)
    later = controller.update(body, now_s=1.1, attitude_time_s=1.1, position=origin)
    controller.record_command(later, 1.1)
    # Simulate transport/image delay across a mode switch.
    controller.set_mode("lock")
    joints = {"roll": 5.0, "pitch": 10.0, "yaw": 15.0}
    measured = fk(body, joints)
    observation = {
        "attitude": {a + "_deg": body[a] for a in AXES},
        "pose": {
            "lat_deg": origin.lat_deg,
            "lon_deg": origin.lon_deg,
            "alt_amsl_m": origin.alt_m,
            "alt_m": 85.0,
            "local_ned_m": [0.0, 0.0, -85.0],
        },
        "frame_sensor_stamp_ns": 1_050_000_000,
        "gimbal_state": {
            "fresh": True,
            "source": "gazebo_joint_state",
            "joint_position_rad": {a: math.radians(joints[a]) for a in AXES},
        },
        "camera_pose": {
            "age_ms": 0.0,
            "optical_to_world": measured,
            "center_ned": [10.0, 0.0, -85.0],
        },
    }
    controller.annotate(observation)
    state = observation["gimbal_state"]["stabilization"]
    expected_ray = world_target_direction(origin, target, camera_offset_ned=(10.0, 0.0, 0.0))
    assert state["mode"] == "track"
    assert state["target_ref"] == target.reference()
    assert state["frame_control_age_ms"] == pytest.approx(50.0)
    assert state["pointing_error_deg"] == pytest.approx(independent_error(expected_ray, measured))
    assert state["pointing_error_deg"] != pytest.approx(command["pointing_error_deg"])
    assert state["measurement_fresh"]
    stale = copy.deepcopy(observation)
    stale["gimbal_state"]["fresh"] = False
    controller.annotate(stale)
    assert stale["gimbal_state"]["stabilization"]["pointing_error_deg"] is None
    no_ref = copy.deepcopy(observation)
    no_ref["frame_sensor_stamp_ns"] = 500_000_000
    controller.set_mode("track")
    controller.annotate(no_ref)
    assert no_ref["gimbal_state"]["stabilization"]["pointing_error_deg"] is None
    assert no_ref["gimbal_state"]["stabilization"]["target_reachable"] is None


@pytest.mark.parametrize("seed", range(12))
def test_constructed_reachable_targets_keep_exact_pointing_with_asymmetric_stops(seed):
    randomizer = random.Random(1000 + seed)
    limits = {"roll": (-23.0, 17.0), "pitch": (-58.0, 31.0), "yaw": (-71.0, 42.0)}
    body = {a: randomizer.uniform(-85.0, 85.0) for a in AXES}
    witness = {a: randomizer.uniform(*limits[a]) for a in AXES}
    # The target direction comes from an independent FK witness. This also
    # exercises the twist family when horizon-level orientation is unreachable.
    direction = fk(body, witness)[0::3]
    joints, _, geometry = solve_track_target(rotation_deg(body), direction, ZERO, limits)
    assert geometry["target_reachable"] is True
    assert independent_error(direction, fk(body, joints)) < 2e-6
    for axis in AXES:
        assert limits[axis][0] - 1e-8 <= joints[axis] <= limits[axis][1] + 1e-8
    expected_horizon = track_horizon_error_deg(fk(body, witness))
    solved_horizon = track_horizon_error_deg(fk(body, joints))
    assert solved_horizon <= expected_horizon + 1e-6


def test_tangent_only_pointing_at_pitch_stop_is_reachable():
    direction = unit((1.0, 0.0, -1.0))
    joints, _, geometry = solve_track_target(rotation_deg(ZERO), direction, ZERO, LEGACY_LIMITS)
    assert geometry["target_reachable"] is True
    assert joints["pitch"] == pytest.approx(45.0)
    assert joints["roll"] == pytest.approx(0.0, abs=1e-6)
    assert independent_error(direction, fk(ZERO, joints)) < 2e-6


def test_delayed_lock_frame_keeps_frame_heading_after_switch_to_track():
    controller = HorizonStabilizer(limits_deg=LEGACY_LIMITS)
    old = controller.update(ZERO, now_s=1.0, attitude_time_s=1.0)
    controller.record_command(old, 1.0)
    controller.set_mode("track")
    body = {"roll": 0.0, "pitch": -70.0, "yaw": 20.0}
    observation = {
        "attitude": {a + "_deg": body[a] for a in AXES},
        "frame_sensor_stamp_ns": 1_050_000_000,
        "gimbal_state": {
            "fresh": True,
            "source": "gazebo_joint_state",
            "joint_position_rad": {a: math.radians(old["joint_deg"][a]) for a in AXES},
        },
        "camera_pose": {"age_ms": 0.0, "optical_to_world": fk(body, old["joint_deg"])},
    }
    controller.annotate(observation)
    state = observation["gimbal_state"]["stabilization"]
    assert state["mode"] == "lock"
    assert state["desired_earth_deg"]["yaw"] == pytest.approx(20.0)


def test_track_global_position_clock_skew_cannot_look_fresh():
    origin, target = WorldTarget(40.0, -93.0, 385.0), WorldTarget(40.001, -93.0, 300.0)
    controller = HorizonStabilizer(mode="track", track_targets=(target,), limits_deg=LEGACY_LIMITS)
    command = controller.update(ZERO, now_s=1.0, attitude_time_s=1.0, position=origin)
    controller.record_command(command, 1.0)
    observation = {
        "attitude": {a + "_deg": 0.0 for a in AXES},
        "frame_sensor_stamp_ns": 1_050_000_000,
        "pose": {
            "lat_deg": 40.0,
            "lon_deg": -93.0,
            "alt_amsl_m": 385.0,
            "local_ned_m": [0.0, 0.0, 0.0],
            "global_position_skew_ms": 250.0,
        },
        "gimbal_state": {
            "fresh": True,
            "source": "gazebo_joint_state",
            "joint_position_rad": dict.fromkeys(AXES, 0.0),
        },
        "camera_pose": {
            "age_ms": 0.0,
            "optical_to_world": fk(ZERO, ZERO),
            "center_ned": [0.0, 0.0, 0.0],
        },
    }
    controller.annotate(observation)
    state = observation["gimbal_state"]["stabilization"]
    assert state["target_ref"] == target.reference()
    assert state["pointing_error_deg"] is None
    assert state["target_reachable"] is None


def test_host_loop_passes_fresh_world_position_and_records_track_command():
    from multidetect.sim_teacher.horizon_stabilization import HostHorizonLoop

    position = WorldTarget(40.0, -93.0, 385.0)
    target = WorldTarget(40.001, -93.0, 300.0)
    controller = HorizonStabilizer(mode="track", track_targets=(target,), limits_deg=LEGACY_LIMITS)
    sent = []
    loop = HostHorizonLoop(
        controller, lambda: (ZERO, 1.0, 10.0, position), sent.append, clock=lambda: 1.0
    )
    loop.step()
    assert len(sent) == 1
    assert controller.last_result["mode"] == "track"
    assert controller.last_result["target_ref"] == target.reference()
    assert type(controller.last_result["target_reachable"]) is bool
    assert controller._command_at(10_050_000_000)[0] is controller.last_result
