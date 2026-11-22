"""CPU-only horizon acceptance through bridge decode and independent C11 FK.

The delayed plant tracks the emitted joint setpoint exactly; it models 50 ms
body-telemetry latency and command rate limits, not Gazebo servo dynamics.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from multidetect.sim_teacher.horizon_stabilization import BridgeHorizonTarget, HorizonStabilizer
from scripts.realworld.horizon_report import LEGACY_LIMITS, percentile

AXES = ("roll", "pitch", "yaw")
TARGET = {"roll": 0.0, "pitch": -45.0, "yaw": 0.0}
LIMITS = {"yaw": (-360.0, 360.0), "roll": (-45.0, 45.0), "pitch": (-135.0, 45.0)}


def _multiply(left, right):
    return tuple(
        sum(left[3 * i + k] * right[3 * k + j] for k in range(3))
        for i in range(3)
        for j in range(3)
    )


def _axis_rotation(axis, angle_deg):
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    if axis == "roll":
        return (1, 0, 0, 0, c, -s, 0, s, c)
    if axis == "pitch":
        return (c, 0, s, 0, 1, 0, -s, 0, c)
    return (c, -s, 0, s, c, 0, 0, 0, 1)


def _rotation(angles, order):
    result = (1, 0, 0, 0, 1, 0, 0, 0, 1)
    for axis in order:
        result = _multiply(result, _axis_rotation(axis, angles[axis]))
    return result


def _bridge_decoded(joints):
    from multidetect.sim_teacher.horizon_stabilization import joint_command_quaternion

    return _angles(_quaternion_rotation(joint_command_quaternion(joints)))


def _quaternion_rotation(q):
    w, x, y, z = q
    return (
        1 - 2 * (y * y + z * z),
        2 * (x * y - w * z),
        2 * (x * z + w * y),
        2 * (x * y + w * z),
        1 - 2 * (x * x + z * z),
        2 * (y * z - w * x),
        2 * (x * z - w * y),
        2 * (y * z + w * x),
        1 - 2 * (x * x + y * y),
    )


def _earth(body, joints):
    # Physical chain, independently implemented rather than reusing the solver.
    return _multiply(
        _rotation(body, ("yaw", "pitch", "roll")), _rotation(joints, ("yaw", "roll", "pitch"))
    )


def _angles(matrix):
    return {
        "roll": math.degrees(math.atan2(matrix[7], matrix[8])),
        "pitch": math.degrees(math.asin(max(-1.0, min(1.0, -matrix[6])))),
        "yaw": math.degrees(math.atan2(matrix[3], matrix[0])),
    }


def _wrap(angle):
    return (angle + 180.0) % 360.0 - 180.0


def _horizon_error(actual, desired=TARGET):
    return max(abs(_wrap(actual[axis] - desired[axis])) for axis in ("roll", "pitch"))


def _body(case, seconds):
    if case == "tailsitter_hover":
        return {
            "roll": 0.0,
            "pitch": -85.0 - 5.0 * math.cos(seconds * math.pi / 5),
            "yaw": 25.0 + 4.0 * seconds,
        }
    if case == "fixed_wing_turn":
        return {
            "roll": 45.0 * math.sin(seconds * math.pi / 5),
            "pitch": 15.0 * math.sin(seconds * math.pi / 7),
            "yaw": 4.0 * seconds,
        }
    # Covers both 0 -> -90 and -90 -> 0, including both endpoints exactly.
    return {
        "roll": 0.0,
        "pitch": -45.0 * (1.0 - math.cos(seconds * math.pi / 10)),
        "yaw": 25.0 + 4.0 * seconds,
    }


@pytest.mark.parametrize("case", ["tailsitter_hover", "fixed_wing_turn", "vtol_fw_transition"])
@pytest.mark.parametrize("delay_s,max_error", [(0.0, 2.0), (0.05, 4.0)])
def test_lock_horizon_flight_sequences(case, delay_s, max_error, record_property):
    dt = 0.02  # 50 Hz deterministic host loop, independent of video cadence.
    rate = 27.0 if delay_s and case == "fixed_wing_turn" else 90.0
    stabilizer = HorizonStabilizer(
        target_deg=TARGET, mode="lock", yaw_mode="follow", max_rate_deg_s=rate
    )
    # Settle the initial joint pose before beginning the flight error window.
    previous = None
    for index in range(151):
        now = -3.0 + index * dt
        output = stabilizer.update(_body(case, -delay_s), now_s=now, attitude_time_s=now - delay_s)
        previous = output["joint_deg"]
    peak = 0.0
    largest_step = 0.0
    limited_steps = 0
    for index in range(1001):
        now = index * dt
        current = _body(case, now)
        output = stabilizer.update(
            _body(case, now - delay_s), now_s=now, attitude_time_s=now - delay_s
        )
        joints = output["joint_deg"]
        for axis in AXES:
            assert LIMITS[axis][0] - 1e-8 <= joints[axis] <= LIMITS[axis][1] + 1e-8
            step = abs(joints[axis] - previous[axis])
            largest_step = max(largest_step, step)
            assert step <= rate * dt + 1e-7
            limited_steps += int(abs(step - rate * dt) < 1e-7)
        previous = joints
        # Include the actual bridge carrier decode before physical FK.
        decoded_joints = _bridge_decoded(joints)
        assert decoded_joints == pytest.approx(joints, abs=1e-8)
        peak = max(peak, _horizon_error(_angles(_earth(current, decoded_joints))))
    record_property("peak_horizon_error_deg", peak)
    record_property("largest_joint_step_deg", largest_step)
    assert peak <= max_error, f"{case}: peak={peak:.6f} deg with {delay_s=}, limit={max_error}"
    if delay_s and case == "fixed_wing_turn":
        assert limited_steps > 50  # This acceptance case actually hits the rate limiter.
    if delay_s:
        assert peak > 0.05  # The latency is actually exercised, not ignored.


def _settle(stabilizer, body):
    for index in range(201):
        stabilizer.update(body, now_s=-4.0 + index * 0.02, attitude_time_s=-4.0 + index * 0.02)


def test_yaw_lock_holds_full_earth_rotation_while_vehicle_turns():
    target = {**TARGET, "yaw": 70.0}
    stabilizer = HorizonStabilizer(
        target_deg=target, mode="lock", yaw_mode="lock", max_rate_deg_s=10000.0
    )
    _settle(stabilizer, {"roll": 0.0, "pitch": -10.0, "yaw": 0.0})
    for index in range(101):
        body = {"roll": 20.0 * math.sin(index / 20), "pitch": -10.0, "yaw": index}
        output = stabilizer.update(body, now_s=index * 0.1, attitude_time_s=index * 0.1)
        actual = _angles(_earth(body, output["joint_deg"]))
        assert _horizon_error(actual) <= 2.0
        assert abs(_wrap(actual["yaw"] - target["yaw"])) <= 2.0


def test_follow_preserves_body_relative_pose_and_tracks_vehicle_tilt():
    stabilizer = HorizonStabilizer(target_deg=TARGET, mode="follow", max_rate_deg_s=10000.0)
    body = {"roll": 30.0, "pitch": -20.0, "yaw": 170.0}
    _settle(stabilizer, body)
    output = stabilizer.update(body, now_s=0.0, attitude_time_s=0.0)
    expected = _multiply(
        _rotation(body, ("yaw", "pitch", "roll")), _rotation(TARGET, ("yaw", "pitch", "roll"))
    )
    assert _earth(body, output["joint_deg"]) == pytest.approx(expected, abs=1e-9)
    assert _horizon_error(_angles(expected)) > 10.0


def test_yaw_wrap_uses_nearby_continuous_joint_branch():
    stabilizer = HorizonStabilizer(
        target_deg=TARGET, mode="lock", yaw_mode="lock", max_rate_deg_s=60.0
    )
    _settle(stabilizer, {"roll": 0.0, "pitch": 0.0, "yaw": 175.0})
    previous = None
    for index in range(201):
        unwrapped = 175.0 + index * 0.05
        body = {"roll": 0.0, "pitch": 0.0, "yaw": _wrap(unwrapped)}
        result = stabilizer.update(body, now_s=index * 0.02, attitude_time_s=index * 0.02)
        if previous is not None:
            assert abs(result["joint_deg"]["yaw"] - previous) <= 1.2 + 1e-8
        previous = result["joint_deg"]["yaw"]
        assert _horizon_error(_angles(_earth(body, result["joint_deg"]))) <= 2.0


def test_joint_limits_and_rate_limit_are_enforced_for_unreachable_target():
    stabilizer = HorizonStabilizer(
        target_deg=TARGET, mode="lock", yaw_mode="follow", max_rate_deg_s=30.0
    )
    first = stabilizer.update(
        {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}, now_s=0.0, attitude_time_s=0.0
    )
    result = stabilizer.update(
        {"roll": 85.0, "pitch": 30.0, "yaw": 0.0}, now_s=0.02, attitude_time_s=0.02
    )
    for axis in AXES:
        assert LIMITS[axis][0] <= result["joint_deg"][axis] <= LIMITS[axis][1]
        assert abs(result["joint_deg"][axis] - first["joint_deg"][axis]) <= 0.6 + 1e-8
    assert result["degraded"]


@pytest.mark.parametrize("pitch", [-90.0, 90.0])
def test_body_pitch_euler_singularity_produces_finite_bounded_commands(pitch):
    stabilizer = HorizonStabilizer(target_deg=TARGET, mode="lock", yaw_mode="follow")
    _settle(stabilizer, {"roll": 0.0, "pitch": pitch, "yaw": 25.0})
    result = stabilizer.update(
        {"roll": 0.0, "pitch": pitch, "yaw": 25.0}, now_s=0.0, attitude_time_s=0.0
    )
    assert all(math.isfinite(result["joint_deg"][axis]) for axis in AXES)
    for axis in AXES:
        assert LIMITS[axis][0] <= result["joint_deg"][axis] <= LIMITS[axis][1]
    assert (
        _horizon_error(
            _angles(_earth({"roll": 0.0, "pitch": pitch, "yaw": 25.0}, result["joint_deg"]))
        )
        <= 2.0
    )


def test_zxy_roll_singularity_is_reported_and_bounded():
    stabilizer = HorizonStabilizer(
        target_deg={"roll": 90.0, "pitch": 0.0, "yaw": 0.0}, mode="lock", yaw_mode="lock"
    )
    _settle(stabilizer, {"roll": 0.0, "pitch": 0.0, "yaw": 0.0})
    result = stabilizer.update(
        {"roll": 0.0, "pitch": 0.0, "yaw": 0.0}, now_s=0.0, attitude_time_s=0.0
    )
    assert result["degraded"]
    assert all(math.isfinite(value) for value in result["joint_deg"].values())
    assert abs(result["joint_deg"]["roll"]) <= 45.0
    assert "zxy_singular" in result["reasons"]


def test_expired_attitude_holds_last_joint_command_and_warns(caplog):
    stabilizer = HorizonStabilizer(target_deg=TARGET, mode="lock", max_age_s=0.2)
    _settle(stabilizer, {"roll": 0.0, "pitch": -30.0, "yaw": 0.0})
    first = stabilizer.update(
        {"roll": 0.0, "pitch": -30.0, "yaw": 0.0}, now_s=0.0, attitude_time_s=0.0
    )
    stale = stabilizer.update(
        {"roll": 40.0, "pitch": 20.0, "yaw": 80.0}, now_s=0.3, attitude_time_s=0.0
    )
    assert stale["joint_deg"] == first["joint_deg"]
    assert stale["degraded"]
    assert "stale" in caplog.text.lower() or "expired" in caplog.text.lower()
    recovered = stabilizer.update(
        {"roll": 0.0, "pitch": -30.0, "yaw": 0.0}, now_s=0.4, attitude_time_s=0.4
    )
    assert all(math.isfinite(value) for value in recovered["joint_deg"].values())


@pytest.mark.parametrize(
    "joints",
    [
        {"roll": 15.0, "pitch": -60.0, "yaw": 175.0},
        {"roll": -40.0, "pitch": 44.0, "yaw": -179.0},
    ],
)
def test_bridge_quaternion_carrier_recovers_all_three_joint_numbers(joints):
    from multidetect.sim_teacher.horizon_stabilization import joint_command_quaternion

    w, x, y, z = joint_command_quaternion(joints)
    # Pinned GZGimbal uses Eulerf(q), a ZYX decode, rather than ZXY inverse FK.
    matrix = (
        1 - 2 * (y * y + z * z),
        2 * (x * y - w * z),
        2 * (x * z + w * y),
        2 * (x * y + w * z),
        1 - 2 * (x * x + z * z),
        2 * (y * z - w * x),
        2 * (x * z - w * y),
        2 * (y * z + w * x),
        1 - 2 * (x * x + y * y),
    )
    assert _angles(matrix) == pytest.approx(joints, abs=1e-9)
    assert _earth({"roll": 0, "pitch": 0, "yaw": 0}, joints) != pytest.approx(matrix)


@pytest.mark.parametrize("pitch", [-135.0, -90.0, 90.0])
def test_bridge_carrier_rejects_ambiguous_or_unrepresentable_pitch(pitch):
    from multidetect.sim_teacher.horizon_stabilization import joint_command_quaternion

    with pytest.raises(ValueError, match="pitch"):
        joint_command_quaternion({"roll": 0.0, "pitch": pitch, "yaw": 0.0})


@pytest.mark.parametrize("controller", [HorizonStabilizer, BridgeHorizonTarget])
def test_frame_stabilization_uses_measured_c11_joints_and_camera_geometry(controller):
    from pathlib import Path

    from multidetect.sim_teacher.gimbal_recording import DeltaCameraGeometry

    geometry = DeltaCameraGeometry.from_sdf(
        Path(__file__).resolve().parents[1]
        / "simulation/gazebo/models/delta_tailsitter_gimbal_cam/model.sdf"
    )
    body = {"roll": 20.0, "pitch": -30.0, "yaw": 90.0}
    measured_joints = {"roll": -10.0, "pitch": -5.0, "yaw": 4.0}
    observation = {
        "attitude": {axis + "_deg": value for axis, value in body.items()},
        "pose": {"local_ned_m": [100.0, 20.0, -80.0]},
        "frame_sensor_stamp_ns": 1_000_000_000,
    }
    geometry.apply(
        observation,
        {
            "source": "gazebo_joint_state",
            "fresh": True,
            "source_timestamp_ns": 1_000_000_000,
            "position_rad": {axis: math.radians(value) for axis, value in measured_joints.items()},
        },
        max_age_ms=200.0,
    )
    kwargs = {"status_sender": (1, 1)} if controller is BridgeHorizonTarget else {}
    stabilizer = controller(target_deg=TARGET, mode="lock", yaw_mode="follow", **kwargs)
    result = stabilizer.update(body, now_s=1.0, attitude_time_s=1.0)
    stabilizer.record_command(result, 1.0)
    stabilizer.annotate(observation)
    measured_world = _earth(body, measured_joints)
    assert observation["camera_pose"]["optical_to_world"] == pytest.approx(measured_world)
    state = observation["gimbal_state"]["stabilization"]
    assert state["source"] == ("bridge" if controller is BridgeHorizonTarget else "host")
    assert state["mode"] == "lock"
    assert state["measured_earth_deg"] == pytest.approx(_angles(measured_world))
    assert state["desired_earth_deg"] == pytest.approx({**TARGET, "yaw": 90.0})
    assert state["horizon_error_deg"] == pytest.approx(_horizon_error(_angles(measured_world)))
    assert state["horizon_error_deg"] > 2.0
    if controller is HorizonStabilizer:
        assert observation["gimbal_state"]["joint_position_rad"] != {
            axis: math.radians(value) for axis, value in result["joint_deg"].items()
        }
    else:
        assert "joint_deg" not in result
        assert state["frame_control_age_ms"] == 0.0
        status = {"message_type": "NAMED_VALUE_FLOAT", "name": "c11_hdeg",
                  "_source_system": 1, "_source_component": 1, "time_boot_ms": 990, "value": 0.}
        assert stabilizer.frame_horizon_degraded(1_000_000_000, result) is None
        stabilizer.record_bridge_status({**status, "_source_component": 2})
        assert stabilizer.frame_horizon_degraded(1_000_000_000, result) is None
        stabilizer.record_bridge_status(status)
        stabilizer.record_bridge_status({**status, "time_boot_ms": 1010, "value": 1.})
        assert stabilizer.frame_horizon_degraded(1_000_000_000, result) is False
        assert stabilizer.frame_horizon_degraded(1_010_000_000, result) is True
        assert stabilizer.frame_horizon_degraded(1_211_000_000, result) is None
        stabilizer.annotate(observation)
        state = observation["gimbal_state"]["stabilization"]
        if state["horizon_feasible"]:
            assert state["horizon_degraded"] is False
        assert state["horizon_error_deg"] > 2.0
    observation["gimbal_state"]["fresh"] = False
    stabilizer.annotate(observation)
    state = observation["gimbal_state"]["stabilization"]
    assert state["measured_earth_deg"] is None
    assert state["horizon_error_deg"] is None


@pytest.mark.parametrize("mode,yaw_mode,flags", [
    ("lock", "lock", 92), ("lock", "follow", 44), ("follow", "lock", 32),
    ("track", "follow", 92),
])
def test_bridge_loop_sends_pose_and_flags_without_joint_inverse(mode, yaw_mode, flags):
    from multidetect.sim_teacher.horizon_stabilization import HostHorizonLoop
    from multidetect.sim_teacher.target_tracking import WorldTarget

    target = {"roll": 15., "pitch": -35., "yaw": 30.}
    body = {"roll": 20., "pitch": -30., "yaw": 90.}
    origin, destination = WorldTarget(40., -90., 100.), WorldTarget(40., -89.999, 100.)
    stabilizer = BridgeHorizonTarget(target, mode=mode, yaw_mode=yaw_mode, track_targets=[destination])
    sent = []
    loop = HostHorizonLoop(stabilizer, lambda: (body, 1., 1., origin),
                           lambda *args: sent.append(args), clock=lambda: 1.)
    loop.step()
    assert len(sent) == 1
    q, wire_flags = sent[0]
    assert wire_flags == flags
    assert "joint_deg" not in stabilizer.last_result
    if mode == "follow":
        expected = _rotation(target, ("yaw", "roll", "pitch"))
    elif mode == "track":
        from multidetect.sim_teacher.target_tracking import level_target_rotation, world_target_direction
        expected = level_target_rotation(world_target_direction(origin, destination))
        assert stabilizer.last_result["target_ref"] == destination.reference()
    else:
        expected = _rotation(target, ("yaw", "pitch", "roll"))
    assert _quaternion_rotation(q) == pytest.approx(expected)

    loop.sample = lambda: (body, 0., 1., origin)
    loop.step()
    assert len(sent) == 1


def test_legacy_joint_carrier_survives_mavlink2_serialization():
    import io

    from multidetect.sim_teacher.horizon_stabilization import joint_command_quaternion

    mavlink = pytest.importorskip("pymavlink.dialects.v20.common")
    joints = {"roll": -37.0, "pitch": 42.0, "yaw": 154.0}
    wire = io.BytesIO()
    sender = mavlink.MAVLink(wire, srcSystem=255, srcComponent=190)
    sender.gimbal_manager_set_attitude_send(
        1, 1, mavlink.GIMBAL_MANAGER_FLAGS_YAW_IN_VEHICLE_FRAME, 1,
        joint_command_quaternion(joints), math.nan, math.nan, math.nan,
    )
    packet = wire.getvalue()
    assert packet[0] == 0xFD
    message = mavlink.MAVLink(None).decode(bytearray(packet))
    assert message.get_type() == "GIMBAL_MANAGER_SET_ATTITUDE"
    assert (message.target_system, message.target_component, message.gimbal_device_id) == (1, 1, 1)
    assert message.flags == 32
    assert all(math.isnan(getattr(message, "angular_velocity_" + axis)) for axis in "xyz")
    assert sum(a * b for a, b in zip(message.q, joint_command_quaternion(joints), strict=True)) == (
        pytest.approx(1.0, abs=1e-7)
    )


def test_host_loop_does_not_accumulate_commands_behind_blocked_transport():
    import threading
    from types import SimpleNamespace

    from multidetect.sim_teacher.horizon_stabilization import HostHorizonLoop

    current = [1.0]
    sent = []
    ticket = SimpleNamespace(done=threading.Event(), error=None)

    def send(quaternion):
        sent.append(quaternion)
        return ticket

    loop = HostHorizonLoop(
        HorizonStabilizer(),
        lambda: ({"roll": 0, "pitch": 0, "yaw": 0}, current[0]),
        send,
        clock=lambda: current[0],
    )
    loop.step()
    for _ in range(10):
        current[0] += 0.02
        loop.step()
    assert len(sent) == loop.sent_count == 1
    ticket.done.set()
    loop.step()
    assert len(sent) == loop.sent_count == 2
    ticket.error = OSError("transport closed")
    with pytest.raises(RuntimeError, match="transport"):
        loop.step()


def test_host_loop_rejects_manager_ownership_loss_before_sending():
    from types import SimpleNamespace

    from multidetect.sim_teacher.horizon_stabilization import HostHorizonLoop

    loop = HostHorizonLoop(
        HorizonStabilizer(),
        lambda: pytest.fail("sampled after ownership loss"),
        lambda _q: pytest.fail("sent after ownership loss"),
        session=SimpleNamespace(poll=lambda: None, owned=False),
    )
    with pytest.raises(RuntimeError, match="ownership"):
        loop.step()


@pytest.mark.parametrize(
    "body,stamp",
    [
        (None, 0.5),
        ({"roll": float("nan"), "pitch": 0.0, "yaw": 0.0}, 0.5),
        ({"roll": 0.0, "pitch": 0.0, "yaw": 0.0}, 1.0),
    ],
)
def test_invalid_or_future_attitude_holds_previous_joint_output(body, stamp):
    stabilizer = HorizonStabilizer(target_deg=TARGET, mode="lock", max_age_s=0.2)
    _settle(stabilizer, {"roll": 0.0, "pitch": -30.0, "yaw": 0.0})
    previous = dict(stabilizer.last_joint)
    result = stabilizer.update(body, now_s=0.5, attitude_time_s=stamp)
    assert result["joint_deg"] == previous
    assert result["desired_earth_deg"] is None
    assert result["degraded"]
    assert "attitude_stale_or_invalid_hold_last" in result["reasons"]


def test_host_loop_background_thread_stops_before_transport_teardown():
    import threading

    from multidetect.sim_teacher.horizon_stabilization import HostHorizonLoop

    sent = threading.Event()
    loop = HostHorizonLoop(
        HorizonStabilizer(),
        lambda: ({"roll": 0, "pitch": 0, "yaw": 0}, 1.0),
        lambda _q: sent.set(),
        clock=lambda: 1.0,
    )
    loop.start()
    assert sent.wait(1.0)
    loop.close()
    assert not loop.thread.is_alive()
    assert loop.sent_count >= 1
    loop.check()


def test_effective_legacy_yaw_limit_avoids_carrier_wrap_jump():
    from multidetect.sim_teacher.horizon_stabilization import joint_command_quaternion

    limits = {**LIMITS, "yaw": (-179.9, 179.9)}
    stabilizer = HorizonStabilizer(TARGET, yaw_mode="lock", limits_deg=limits, max_rate_deg_s=60)
    _settle(stabilizer, {"roll": 0, "pitch": 0, "yaw": 175})
    last = stabilizer.last_joint["yaw"]
    saw_limit = False
    for index in range(201):
        body = {"roll": 0, "pitch": 0, "yaw": _wrap(175 + index * 0.05)}
        result = stabilizer.update(body, now_s=index * 0.02, attitude_time_s=index * 0.02)
        yaw = result["joint_deg"]["yaw"]
        joint_command_quaternion(result["joint_deg"])
        assert abs(yaw - last) <= 1.2 + 1e-8
        saw_limit |= "yaw_limit" in result["reasons"]
        last = yaw
    assert saw_limit


@pytest.mark.parametrize("sign", [-1, 1])
def test_unreachable_zxy_crossing_retains_outer_joint_branch(sign):
    from multidetect.sim_teacher.horizon_stabilization import joint_command_quaternion

    stabilizer = HorizonStabilizer(
        TARGET, limits_deg={**LIMITS, "pitch": (-89.9, 45), "yaw": (-179.9, 179.9)}
    )
    _settle(stabilizer, {"roll": sign*85, "pitch": -54, "yaw": 179})
    previous = dict(stabilizer.last_joint)
    for index in range(501):
        body = {"roll": sign*(85+6*math.sin(index/100)), "pitch": -54,
                "yaw": _wrap(179+index*.02)}
        result = stabilizer.update(body, now_s=index*.02, attitude_time_s=index*.02)
        joints = result["joint_deg"]
        joint_command_quaternion(joints)
        assert not result["target_pose_feasible"]
        if abs(body["roll"]) <= 90:
            assert result["horizon_feasible"] is True
            assert result["yaw_degraded"]
            assert result["yaw_error_deg"] > 1.0
        else:
            assert result["horizon_feasible"] is False
            assert result["horizon_feasibility_evidence"] == "no_joint_solution_on_heading_intervals"
        assert _horizon_error(_angles(_earth(body, joints))) < 1.0
        for axis in AXES:
            assert abs(joints[axis]-previous[axis]) <= 3.6+1e-7
        previous = joints


def test_horizon_fallback_stays_near_previous_feasible_joint_solution():
    # Both separated heading branches can be held when seeded on that branch.
    from multidetect.camera_mount import _mat_mul, _mat_transpose
    from multidetect.sim_teacher.horizon_stabilization import rotation_deg, solve_joint_target

    body = {"roll": 80, "pitch": -60, "yaw": 0}
    for yaw in (-65, 75):
        relative = _mat_mul(
            _mat_transpose(rotation_deg(body)), rotation_deg({**TARGET, "yaw": yaw})
        )
        stabilizer = HorizonStabilizer(TARGET)
        _, previous, feasible, _ = solve_joint_target(relative, stabilizer.last_joint, LIMITS)
        assert feasible
        stabilizer.last_joint.update(previous)
        stabilizer.update(body, now_s=0, attitude_time_s=0)
        output = stabilizer.update(body, now_s=.02, attitude_time_s=.02)
        assert output["yaw_degraded"]
        assert output["joint_deg"] == pytest.approx(previous, abs=.02)


@pytest.mark.parametrize("yaw", [-180, -179.9999, 179.9999, 180])
def test_yaw_follow_wrap_does_not_change_joint_solution(yaw):
    stabilizer = HorizonStabilizer(TARGET)
    body = {"roll": 20, "pitch": -70, "yaw": yaw}
    _settle(stabilizer, body)
    result = stabilizer.update(body, now_s=0, attitude_time_s=0)
    assert result["joint_deg"] == pytest.approx({"roll": -20, "pitch": 25, "yaw": 0})
    assert result["target_pose_feasible"]
    assert _horizon_error(_angles(_earth(body, result["joint_deg"]))) < 1e-7


def test_frame_feasibility_distinguishes_heading_requirement_and_measured_saturation():
    from multidetect.sim_teacher.horizon_stabilization import frame_feasibility, rotation_deg

    body = {"roll": -89.7979, "pitch": -54.327, "yaw": -85.204}
    desired = {**TARGET, "yaw": body["yaw"]}
    result = frame_feasibility(body, rotation_deg(desired),
                               {"roll": 45, "pitch": -75.796, "yaw": -66.026})
    assert result["target_pose_feasible"] is False
    assert result["mechanical_feasible"] is False
    assert result["horizon_feasible"] is True
    assert result["saturated"] and result["saturated_axes"] == ["roll"]
    assert _horizon_error(_angles(_earth(body, result["horizon_witness_joint_deg"]))) < 1e-6


def test_unsupported_joint_geometry_is_not_claimed_infeasible():
    from multidetect.sim_teacher.horizon_stabilization import frame_feasibility, rotation_deg

    result = frame_feasibility(dict.fromkeys(AXES, 0), rotation_deg(TARGET), limits={
        "roll": (-90, 90), "pitch": (-1, 1), "yaw": (-1, 1),
    })
    assert result["target_pose_feasible"] is False
    assert result["horizon_feasible"] is None
    assert result["horizon_feasibility_evidence"] is None


def test_pitch_saturation_is_distinct_from_static_target_feasibility():
    from multidetect.sim_teacher.horizon_stabilization import frame_feasibility, rotation_deg

    result = frame_feasibility({"roll": 0, "pitch": 0, "yaw": 0}, rotation_deg(TARGET),
                               {"roll": 0, "pitch": 45, "yaw": 0})
    assert result["target_pose_feasible"]
    assert result["saturated_axes"] == ["pitch"]


@pytest.mark.parametrize("roll", [-90, 90])
def test_exact_singular_coupling_reconstructs_with_nonzero_previous_yaw(roll):
    from multidetect.sim_teacher.horizon_stabilization import _zxy_branches, c11_rotation_deg

    desired = _rotation({"roll": roll, "pitch": 17, "yaw": -31}, ("yaw", "roll", "pitch"))
    branches, singular = _zxy_branches(desired, {"roll": 0, "pitch": 0, "yaw": 42})
    assert singular
    for branch in branches:
        assert c11_rotation_deg(branch) == pytest.approx(desired, abs=1e-8)


def test_saturated_frame_annotation_preserves_measured_error_and_control_degradation():
    stabilizer = HorizonStabilizer(TARGET)
    body = {"roll": -89.7979, "pitch": -54.327, "yaw": -85.204}
    joints = {"roll": 45, "pitch": -75.796, "yaw": -66.026}
    stabilizer.update(body, now_s=1, attitude_time_s=1)
    observation = {
        "attitude": {a+"_deg": body[a] for a in AXES},
        "gimbal_state": {"fresh": True, "source": "gazebo_joint_state",
                         "joint_position_rad": {a: math.radians(joints[a]) for a in AXES}},
        "camera_pose": {"age_ms": 0, "optical_to_world": _earth(body, joints)},
    }
    stabilizer.annotate(observation)
    state = observation["gimbal_state"]["stabilization"]
    assert state["horizon_error_deg"] > 150
    assert state["target_pose_feasible"] is False
    assert state["horizon_feasible"] is True
    assert state["yaw_degraded"] is True
    assert state["yaw_error_deg"] > 30
    assert state["saturated_axes"] == ["roll"]
    assert state["control"]["degraded"] is True
    assert state["frame_geometric_degraded"] is True
    assert "target_pose_unreachable" in state["frame_geometric_reasons"]
    assert "roll_saturated" in state["frame_geometric_reasons"]
    assert "not_frame_aligned" in state["control_alignment"]


def test_frame_horizon_degradation_uses_past_command_and_excludes_yaw_only():
    body = {"roll": 65, "pitch": -74, "yaw": 10}
    stabilizer = HorizonStabilizer(TARGET)
    _settle(stabilizer, body)
    level = stabilizer.last_result
    assert level["yaw_degraded"] and not level["horizon_degraded"]
    stabilizer.record_command(level, 1.0)
    later = stabilizer.update(None, now_s=.02, attitude_time_s=None)
    stabilizer.record_command(later, 1.1)
    joints = level["joint_deg"]
    observation = {
        "attitude": {a + "_deg": body[a] for a in AXES},
        "frame_sensor_stamp_ns": 1_050_000_000,
        "gimbal_state": {"fresh": True, "source": "gazebo_joint_state",
                         "joint_position_rad": {a: math.radians(joints[a]) for a in AXES}},
        "camera_pose": {"age_ms": 0, "optical_to_world": _earth(body, joints)},
    }
    stabilizer.annotate(observation)
    state = observation["gimbal_state"]["stabilization"]
    assert state["control"]["horizon_degraded"] is True
    assert state["frame_control_age_ms"] == pytest.approx(50)
    assert state["horizon_degraded"] is False
    assert state["yaw_degraded"] is True
    assert state["yaw_error_deg"] == pytest.approx(level["yaw_error_deg"])


def test_audited_iowa_frames_horizon_first_replay():
    # Projection of .tmp/episode-audit.csv. The samples are seconds apart;
    # hold each audited pose while the 50 Hz controller settles. The dense
    # telemetry replay separately tests dynamics between these samples.
    with (Path(__file__).parent / "fixtures/iowa_c11_attitudes.csv").open() as stream:
        frames = list(csv.DictReader(stream))
    assert len(frames) == 113
    stabilizer = HorizonStabilizer(TARGET, limits_deg=LEGACY_LIMITS)
    stabilizer.last_joint = {a: float(frames[0][f"joint_{a}_deg"]) for a in AXES}
    errors = []
    expected_yaw_degraded, yaw_degraded = set(), set()
    degraded_count = 0
    tick = 0
    for row in frames:
        frame = int(row["frame"])
        body = {a: float(row[f"body_{a}_deg"]) for a in AXES}
        if row["exact_pose_feasible"] == "False":
            expected_yaw_degraded.add(frame)
        for _ in range(125):
            before = dict(stabilizer.last_joint)
            stamp = float(frames[0]["sim_s"]) + tick * .02
            tick += 1
            output = stabilizer.update(body, now_s=stamp, attitude_time_s=stamp)
            assert max(abs(output["joint_deg"][a] - before[a]) for a in AXES) <= 3.6 + 1e-8
            if not output["rate_limited"]:
                break
        assert output["horizon_feasible"] is True
        actual = _angles(_earth(body, _bridge_decoded(output["joint_deg"])))
        errors.append(_horizon_error(actual))
        degraded_count += output["horizon_degraded"]
        if output["yaw_degraded"]:
            yaw_degraded.add(frame)
            assert output["yaw_error_deg"] == pytest.approx(abs(_wrap(actual["yaw"] - body["yaw"])))
    assert len(expected_yaw_degraded) == 32
    assert yaw_degraded == expected_yaw_degraded
    assert percentile(errors, .95) <= 4
    assert max(errors) <= 8
    assert degraded_count / len(frames) <= .05
