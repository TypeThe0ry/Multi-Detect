from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from multidetect.camera_mount import CameraMount, GimbalLimits
from multidetect.sim_teacher.streams import (
    LatestWinsBuffer,
    MavlinkState,
    SensorSample,
    build_observation,
    simulation_stamp_skew_ms,
)


def _collector_module():
    """Import the CLI module without starting Docker or a collection run."""
    from scripts import collect_sim_teacher_episodes

    return collect_sim_teacher_episodes


def test_build_observation_uses_live_mavlink_and_converts_rgb() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20, 30, 40, 50]),
        width=2,
        height=1,
        depth=np.ones((1, 2), dtype=np.float32),
        camera_info={"width": 2, "height": 1, "fx": 1.0, "fy": 1.0},
        pose={"lat_deg": 1.0, "lon_deg": 2.0, "alt_m": 3.0},
        velocity=[0.0, 0.0, 0.0],
        sensor_stamp_ns=10,
        received_monotonic_ns=100,
        received_wall_time=1.0,
    )
    state = MavlinkState(
        messages={
            "LOCAL_POSITION_NED": {"x": 1, "y": 2, "z": -3, "vx": 0, "vy": 0, "vz": 0},
            "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 1.0},
            "GLOBAL_POSITION_INT": {"lat": 10_000_000, "lon": 20_000_000, "relative_alt": 4000},
            "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        },
        received_monotonic_ns={"LOCAL_POSITION_NED": 90},
    )
    observation = build_observation(sensor, state)
    assert observation["sensor_status"] == "live"
    assert observation["landed"] is None
    assert observation["pose"]["local_ned_m"] == [1.0, 2.0, -3.0]
    assert observation["rgb"].shape == (1, 2, 3)
    assert observation["frame_pose_skew_ns"] == 10
    assert observation["pose_time_boot_ms"] is None


def test_build_observation_records_optional_gimbal_and_composed_camera_pose() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20]), width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0}, pose={},
        velocity=[0.0, 0.0, 0.0], sensor_stamp_ns=10,
        received_monotonic_ns=100, received_wall_time=1.0,
    )
    messages = {
        "LOCAL_POSITION_NED": {"x": 1, "y": 2, "z": -3, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
        "GLOBAL_POSITION_INT": {"lat": 10_000_000, "lon": 20_000_000, "relative_alt": 4000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {"q0": 1.0, "q1": 0.0, "q2": 0.0, "q3": 0.0, "time_boot_ms": 7},
    }
    observation = build_observation(sensor, MavlinkState(messages=messages), camera_mount=CameraMount.gimbal())
    assert observation["gimbal_state"]["source"] == "GIMBAL_DEVICE_ATTITUDE_STATUS"
    assert observation["camera_pose"]["center_ned"] == [1.0, 2.0, -3.0]


def test_build_observation_composes_real_downward_feedback_without_euler_sign_flip() -> None:
    import math

    sensor = SensorSample(
        rgb=bytes([0, 10, 20]), width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0}, pose={},
        velocity=[0.0, 0.0, 0.0], sensor_stamp_ns=10,
        received_monotonic_ns=100, received_wall_time=1.0,
    )
    pitch = -math.pi / 2.0  # MAVLink positive-up Euler convention.
    messages = {
        "LOCAL_POSITION_NED": {"x": 0, "y": 0, "z": -10, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
        "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 10000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {
            "q": [math.cos(pitch / 2.0), 0.0, math.sin(pitch / 2.0), 0.0],
            "time_boot_ms": 7,
        },
    }
    state = MavlinkState(
        messages=messages,
        received_monotonic_ns={"GIMBAL_DEVICE_ATTITUDE_STATUS": 100},
    )
    observation = build_observation(sensor, state, camera_mount=CameraMount.gimbal())
    rotation = observation["camera_pose"]["optical_to_world"]
    assert rotation[6] == pytest.approx(1.0, abs=1e-6)
    assert observation["gimbal_state"]["pitch_rad"] == pytest.approx(math.pi / 2.0)


def test_build_observation_records_out_of_limit_feedback_without_raising() -> None:
    import math

    sensor = SensorSample(
        rgb=bytes([0, 10, 20]), width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0}, pose={},
        velocity=[0.0, 0.0, 0.0], sensor_stamp_ns=10,
        received_monotonic_ns=100, received_wall_time=1.0,
    )
    messages = {
        "LOCAL_POSITION_NED": {"x": 0, "y": 0, "z": -10, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
        "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 10000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {
            "q": [math.cos(math.radians(100) / 2), 0.0, -math.sin(math.radians(100) / 2), 0.0],
            "time_boot_ms": 7,
        },
    }
    observation = build_observation(
        sensor,
        MavlinkState(
            messages=messages,
            received_monotonic_ns={"GIMBAL_DEVICE_ATTITUDE_STATUS": 100},
        ),
        camera_mount=CameraMount.gimbal(
            limits=GimbalLimits(pitch_min_rad=-math.pi, pitch_max_rad=0.5)
        ),
    )
    assert observation["gimbal_state"]["out_of_limits"] is True
    assert "camera_pose" in observation


def test_fixed_mount_does_not_attach_gimbal_state_or_stale_camera_pose() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20]), width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0}, pose={},
        velocity=[0.0, 0.0, 0.0], sensor_stamp_ns=10,
        received_monotonic_ns=100, received_wall_time=1.0,
    )
    messages = {
        "LOCAL_POSITION_NED": {"x": 0, "y": 0, "z": -10, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
        "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 10000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {"q": [1.0, 0.0, 0.0, 0.0]},
    }
    observation = build_observation(sensor, MavlinkState(messages=messages), camera_mount=CameraMount.fixed())
    assert "gimbal_state" not in observation
    assert "camera_pose" not in observation


def test_stale_gimbal_feedback_is_recorded_but_not_used_for_camera_pose() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20]), width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0}, pose={},
        velocity=[0.0, 0.0, 0.0], sensor_stamp_ns=10,
        received_monotonic_ns=2_000_000_000, received_wall_time=1.0,
    )
    messages = {
        "LOCAL_POSITION_NED": {"x": 0, "y": 0, "z": -10, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
        "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 10000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {"q": [1.0, 0.0, 0.0, 0.0]},
    }
    observation = build_observation(
        sensor,
        MavlinkState(
            messages=messages,
            received_monotonic_ns={"GIMBAL_DEVICE_ATTITUDE_STATUS": 500_000_000},
        ),
        camera_mount=CameraMount.gimbal(),
    )
    assert observation["gimbal_state"]["fresh"] is False
    assert "camera_pose" not in observation


def test_stale_feedback_cannot_overwrite_fresh_joint_sidecar_angles() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20]), width=1, height=1, depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0}, pose={},
        velocity=[0.0, 0.0, 0.0], sensor_stamp_ns=10,
        received_monotonic_ns=2_000_000_000, received_wall_time=1.0,
        joint_state={
            "gimbal_joint_state": {
                "timestamp_ns": 10,
                "position_rad": {"pitch": 0.4, "yaw": 0.5, "roll": 0.6},
                "velocity_rad_s": {"pitch": 1.0, "yaw": 2.0, "roll": 3.0},
                "source": "gazebo_joint_state",
                "fresh": True,
            },
            "propeller_joint_state": {
                "timestamp_ns": 10,
                "joint_names": [f"rotor_{index}_joint" for index in range(4)],
                "angles_rad": [0.1, 0.2, 0.3, 0.4],
                "spin_rad_s": [10.0, -11.0, 12.0, -13.0],
                "velocity_slowdown": 10,
                "source": "gazebo_joint_state",
            },
        },
    )
    messages = {
        "LOCAL_POSITION_NED": {"x": 1, "y": 2, "z": -10, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 0},
        "GLOBAL_POSITION_INT": {"lat": 0, "lon": 0, "relative_alt": 10000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
        "GIMBAL_DEVICE_ATTITUDE_STATUS": {
            "q": [1.0, 0.0, 0.0, 0.0],
            "time_boot_ms": 7,
        },
    }
    observation = build_observation(
        sensor,
        MavlinkState(
            messages=messages,
            received_monotonic_ns={"GIMBAL_DEVICE_ATTITUDE_STATUS": 500_000_000},
        ),
        camera_mount=CameraMount.gimbal(),
    )

    assert observation["gimbal_state"]["fresh"] is False
    assert observation["gimbal_joint_state"]["source"] == "gazebo_joint_state"
    assert observation["gimbal_joint_state"]["position_rad"] == {
        "pitch": 0.4,
        "yaw": 0.5,
        "roll": 0.6,
    }
    assert observation["propeller_joint_state"]["angles_rad"] == [0.1, 0.2, 0.3, 0.4]
    assert "camera_pose" not in observation


def test_collector_dispatches_only_the_shared_gimbal_command_schema() -> None:
    collector = _collector_module()

    class Controller:
        def __init__(self) -> None:
            self.calls = []

        def command_pitch_yaw(self, pitch, yaw, *, mode, vehicle_yaw_rad):
            self.calls.append((pitch, yaw, mode, vehicle_yaw_rad))

    controller = Controller()
    labels = {
        "gimbal_command": {
            "pitch_down_rad": 1.0,
            "yaw_world_rad": -0.5,
            "mode": "lock-horizon",
        }
    }
    assert collector._dispatch_teacher_gimbal_command(
        controller, labels, vehicle_yaw_deg=90.0
    ) is True
    assert controller.calls == [(1.0, -0.5, "lock-horizon", pytest.approx(1.57079632679))]
    assert collector._dispatch_teacher_gimbal_command(
        controller, {"gimbal_command": {"pitch_rad": 1.0}}, vehicle_yaw_deg=0.0
    ) is False


def test_build_observation_exposes_known_mavlink_landed_state() -> None:
    sensor = SensorSample(
        rgb=bytes([0, 10, 20]),
        width=1,
        height=1,
        depth=None,
        camera_info={"width": 1, "height": 1, "fx": 1.0, "fy": 1.0},
        pose={},
        velocity=[0.0, 0.0, 0.0],
        sensor_stamp_ns=10,
        received_monotonic_ns=100,
        received_wall_time=1.0,
    )
    messages = {
        "LOCAL_POSITION_NED": {"x": 1, "y": 2, "z": -3, "vx": 0, "vy": 0, "vz": 0},
        "ATTITUDE": {"roll": 0, "pitch": 0, "yaw": 1.0},
        "GLOBAL_POSITION_INT": {"lat": 10_000_000, "lon": 20_000_000, "relative_alt": 4000},
        "GPS_RAW_INT": {"fix_type": 3, "satellites_visible": 10},
    }
    for landed_state, expected in ((1, True), (2, False), (3, False), (4, False), (0, None)):
        state = MavlinkState(
            messages={**messages, "EXTENDED_SYS_STATE": {"landed_state": landed_state}}
        )
        assert build_observation(sensor, state)["landed"] is expected


def test_latest_wins_buffer_drops_old_frames() -> None:
    buffer = LatestWinsBuffer()
    for value in range(100):
        buffer.put(value)
    assert buffer.wait_for(lambda value: value == 99, timeout_s=0.01) == 99
    assert buffer.latest == 99
    assert buffer.published_count == 100
    assert buffer.dropped_count == 99


def test_pose_sim_stamp_helper_converts_px4_boot_time() -> None:
    collector = _collector_module()
    state = MavlinkState(messages={"LOCAL_POSITION_NED": {"time_boot_ms": 7_160}})
    assert collector._pose_sim_stamp_ns(state) == 7_160_000_000


def test_achieved_delta_uses_primitive_pose_displacement_from_crash_evidence() -> None:
    collector = _collector_module()
    before = [-0.6, 0.0, -18.62]
    after = [-0.5, 0.1, 0.76]
    assert collector._achieved_delta_ned(before, after) == pytest.approx([0.1, 0.1, 19.38])

    # The reported delta is local to the adjacent pose pair; it cannot inherit
    # the executor's cumulative 15 m setpoint drift after the aircraft stops.
    near_ground_before = [-0.5, 0.1, 0.76]
    near_ground_after = [-0.4, 0.1, 0.70]
    assert collector._achieved_delta_ned(near_ground_before, near_ground_after) == pytest.approx(
        [0.1, 0.0, -0.06]
    )


def test_simulation_stamp_skew_helper() -> None:
    assert simulation_stamp_skew_ms(7_160_000_000, 7_000) == 160.0
    assert simulation_stamp_skew_ms(7_160_000_000, None) is None


def test_fresh_sensor_snapshot_keeps_both_watermark_gates() -> None:
    collector = _collector_module()

    class Reader:
        def __init__(self) -> None:
            self.latest = SimpleNamespace(received_monotonic_ns=11, sensor_stamp_ns=99)
            self.requests: list[int] = []

        def request(self, stamp: int) -> None:
            self.requests.append(stamp)

        def raise_if_failed(self) -> None:
            return None

    reader = Reader()
    assert collector._fresh_sensor_snapshot(reader, 10, 0.1, min_sensor_stamp_ns=99, poll_interval_s=0.001) is reader.latest
    assert reader.requests == [99]
    reader.latest = SimpleNamespace(received_monotonic_ns=9, sensor_stamp_ns=100)
    with pytest.raises(TimeoutError):
        collector._fresh_sensor_snapshot(reader, 10, 0.01, min_sensor_stamp_ns=99, poll_interval_s=0.001)


def test_stop_action_is_terminal() -> None:
    collector = _collector_module()
    from multidetect.sim_teacher import ActionPrimitive

    assert collector._is_terminal_action(ActionPrimitive.stop()) is True
    assert collector._is_terminal_action(ActionPrimitive.hold(0.1)) is False


def test_stop_records_step_and_ends_policy_loop() -> None:
    collector = _collector_module()
    from multidetect.sim_teacher import ActionPrimitive

    class Recorder:
        def __init__(self) -> None:
            self.actions: list[ActionPrimitive] = []

        def record_step(self, before, action, **kwargs) -> None:
            self.actions.append(action)

    recorder = Recorder()
    before = {"rgb": b"before", "depth": None}
    after = {"rgb": b"after", "depth": None}
    assert (
        collector._record_policy_step(
            recorder,
            before,
            ActionPrimitive.stop(),
            ack={"action_ack": True},
            outcome={"status": "success"},
            obs_after=after,
            labels={},
            timing={"frame_before_wait": 1.0},
        )
        is False
    )
    assert [action.kind for action in recorder.actions] == ["stop"]


def test_setpoint_streamer_rate_target_and_encoding() -> None:
    import time

    from multidetect.sim_teacher.executor import SetpointStreamer

    class FakeMav:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def set_position_target_local_ned_send(self, *args: object) -> None:
            self.calls.append(args)

    mav = FakeMav()
    vehicle = type("Vehicle", (), {"mav": mav, "target_system": 1, "target_component": 1})()
    streamer = SetpointStreamer(vehicle, rate_hz=20.0)
    streamer.set_target(1.0, 2.0, -5.0, 30.0)
    started = time.monotonic()
    streamer.start()
    time.sleep(0.25)
    streamer.set_target(3.0, 4.0, -6.0, 45.0)
    time.sleep(0.25)
    streamer.stop()
    assert len(mav.calls) >= 8
    assert len(mav.calls) / (time.monotonic() - started) >= 15.0
    assert all(call[3] == 1 for call in mav.calls)
    assert all(call[4] == SetpointStreamer.TYPE_MASK for call in mav.calls)
    assert any(call[5:9] == (3.0, 4.0, -6.0, 0.0) for call in mav.calls)


def test_arm_uses_normal_command_encoding_and_same_connection() -> None:
    collector = _collector_module()

    class FakeMav:
        def __init__(self) -> None:
            self.heartbeats: list[tuple[object, ...]] = []
            self.commands: list[tuple[object, ...]] = []
            self.srcSystem = 255
            self.srcComponent = 190

        def heartbeat_send(self, *args: object) -> None:
            self.heartbeats.append(args)

        def command_long_send(self, *args: object) -> None:
            self.commands.append(args)

    class FakeConnection:
        target_system = 1
        target_component = 1

        def __init__(self) -> None:
            self.mav = FakeMav()

    class FakeReader:
        def __init__(self) -> None:
            self.heartbeat_reads = 0
            self.state = self

        def get(self, name: str):
            if name == "HEARTBEAT":
                self.heartbeat_reads += 1
                return {"base_mode": 128 if self.heartbeat_reads > 1 else 0}
            if name == "COMMAND_ACK":
                return {"command": 400, "result": 0}
            return None

        def raise_if_failed(self) -> None:
            return None

    mavlink = SimpleNamespace(
        MAV_CMD_COMPONENT_ARM_DISARM=400,
        MAV_RESULT_ACCEPTED=0,
        MAV_RESULT_TEMPORARILY_REJECTED=1,
        MAV_RESULT_IN_PROGRESS=5,
        MAV_MODE_FLAG_SAFETY_ARMED=128,
        MAV_COMP_ID_AUTOPILOT1=1,
        MAV_TYPE_GCS=6,
        MAV_AUTOPILOT_INVALID=8,
    )
    connection = FakeConnection()
    result = collector._arm(
        connection,
        FakeReader(),
        SimpleNamespace(mavlink=mavlink),
        timeout_s=1.0,
    )

    assert result["command_id"] == 400
    assert result["result"] == 0
    assert result["target_system"] == 1
    assert result["target_component"] == 1
    assert connection.mav.heartbeats, "GCS heartbeat must precede the arm request"
    assert len(connection.mav.commands) == 1
    command = connection.mav.commands[0]
    assert command[0:3] == (1, 1, 400)
    assert command[4] == 1.0
    assert command[5] == 0.0
    assert all(call[2] != 211 for call in connection.mav.commands)


def test_wait_prearm_health_decodes_failures_and_waits_for_clear(monkeypatch) -> None:
    collector = _collector_module()

    # This loop intentionally waits in 100 ms increments.  A real sleep can
    # overshoot the one second deadline when the full suite is under CPU load,
    # making the test intermittently time out before the fake heartbeat gets a
    # chance to clear HEALTH_REPORT.  Keep the production function untouched
    # and inject a monotonic clock/sleeper at this test boundary instead.
    class FakeClock:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

        def monotonic_ns(self) -> int:
            return int(self.now * 1_000_000_000)

        def sleep(self, seconds: float) -> None:
            self.now += max(0.0, float(seconds))

    monkeypatch.setattr(collector, "time", FakeClock())
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        collector,
        "_progress",
        lambda phase, **fields: events.append((phase, fields)),
    )

    class FakeReader:
        def __init__(self) -> None:
            self.state = MavlinkState(
                messages={"HEALTH_REPORT": {"arming_check_error_flags": 0x101080}}
            )

        def raise_if_failed(self) -> None:
            return None

    class FakeMav:
        def __init__(self, reader: FakeReader) -> None:
            self.reader = reader
            self.calls = 0

        def heartbeat_send(self, *args: object) -> None:
            self.calls += 1
            self.reader.state.messages["HEALTH_REPORT"] = {"arming_check_error_flags": 0}

    reader = FakeReader()
    connection = SimpleNamespace(mav=FakeMav(reader))
    mavlink = SimpleNamespace(
        MAV_TYPE_GCS=6,
        MAV_AUTOPILOT_INVALID=8,
    )
    snapshot = collector._wait_prearm_health(
        reader,
        timeout_s=1.0,
        heartbeat_connection=connection,
        mavutil_module=SimpleNamespace(mavlink=mavlink),
    )

    assert snapshot["ready"] is True
    assert snapshot["error_flags"] == 0
    failed = next(fields for phase, fields in events if phase == "prearm_health")
    assert failed["failed_checks"] == [
        "manual_control_input",
        "communication_links",
        "system",
    ]
    assert connection.mav.calls >= 1
    assert any(phase == "prearm_health_ok" for phase, _ in events)


def test_wait_prearm_health_times_out_with_named_failures(monkeypatch) -> None:
    import pytest

    collector = _collector_module()
    monkeypatch.setattr(collector, "_progress", lambda *args, **kwargs: None)
    reader = SimpleNamespace(
        state=MavlinkState(messages={"HEALTH_REPORT": {"arming_check_error_flags": 0x101080}}),
        raise_if_failed=lambda: None,
    )
    with pytest.raises(TimeoutError, match="manual_control_input, communication_links, system"):
        collector._wait_prearm_health(reader, timeout_s=0.02, max_wall_s=0.02)


def test_wait_prearm_health_uses_sim_time_and_has_a_wall_ceiling(monkeypatch) -> None:
    import pytest

    collector = _collector_module()

    class FakeClock:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

        def monotonic_ns(self) -> int:
            return int(self.now * 1_000_000_000)

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = FakeClock()
    monkeypatch.setattr(collector, "time", clock)
    monkeypatch.setattr(collector, "_progress", lambda *args, **kwargs: None)

    class SlowReader:
        def __init__(self) -> None:
            self.state = MavlinkState(
                messages={
                    "SYS_STATUS": {
                        "onboard_control_sensors_present": 16384,
                        "onboard_control_sensors_health": 0,
                    },
                    "LOCAL_POSITION_NED": {"time_boot_ms": 0},
                }
            )

        def raise_if_failed(self) -> None:
            # Sim time advances at 0.2x host time, then the position check clears.
            position = self.state.messages["LOCAL_POSITION_NED"]
            position["time_boot_ms"] += 20
            if position["time_boot_ms"] >= 980:
                self.state.messages["SYS_STATUS"]["onboard_control_sensors_health"] = 16384

    reader = SlowReader()
    result = collector._wait_prearm_health(
        reader,
        timeout_s=1.0,
        max_wall_s=10.0,
        warmup_s=0.0,
    )

    assert result["ready"] is True
    assert clock.now - 100.0 == pytest.approx(4.9, abs=0.2)


def test_wait_prearm_health_stops_at_wall_ceiling_when_sim_clock_is_frozen(monkeypatch) -> None:
    import pytest

    collector = _collector_module()

    class FakeClock:
        def __init__(self) -> None:
            self.now = 100.0

        def monotonic(self) -> float:
            return self.now

        def monotonic_ns(self) -> int:
            return int(self.now * 1_000_000_000)

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    clock = FakeClock()
    monkeypatch.setattr(collector, "time", clock)
    monkeypatch.setattr(collector, "_progress", lambda *args, **kwargs: None)
    reader = SimpleNamespace(
        state=MavlinkState(
            messages={
                "SYS_STATUS": {
                    "onboard_control_sensors_present": 16384,
                    "onboard_control_sensors_health": 0,
                },
                "LOCAL_POSITION_NED": {"time_boot_ms": 1000},
            }
        ),
        raise_if_failed=lambda: None,
    )

    with pytest.raises(TimeoutError, match="xy_position_control"):
        collector._wait_prearm_health(
            reader,
            timeout_s=1.0,
            max_wall_s=0.35,
            warmup_s=0.0,
        )
    assert clock.now - 100.0 == pytest.approx(0.4)


@pytest.mark.usefixtures("require_loopback_sockets")
def test_mavlink_reader_drains_real_udp_mavlink2_burst() -> None:
    import socket
    import threading
    import time

    from pymavlink import mavutil
    from pymavlink.dialects.v20 import ardupilotmega as dialect

    receiver = mavutil.mavlink_connection("udpin:127.0.0.1:0", autoreconnect=False)
    # pymavlink does not expose the ephemeral port in the connection string;
    # bind a known port and recreate the connection for a deterministic test.
    receiver.close()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    receiver = mavutil.mavlink_connection(f"udpin:127.0.0.1:{port}", autoreconnect=False)

    from multidetect.sim_teacher.streams import MavlinkReader

    reader = MavlinkReader(receiver).start()
    sender_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    encoder = dialect.MAVLink(None, srcSystem=1, srcComponent=1)
    packets = [
        encoder.heartbeat_encode(6, 8, 0, 0, 4).pack(encoder),
        encoder.local_position_ned_encode(1, 1.0, 2.0, -3.0, 0.0, 0.0, 0.0).pack(encoder),
        encoder.attitude_encode(1, 0.0, 0.0, 0.1, 0.0, 0.0, 0.0).pack(encoder),
    ]
    assert all(packet[0] == 0xFD for packet in packets)
    sent = [0]

    def blast() -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            for packet in packets:
                sender_socket.sendto(packet, ("127.0.0.1", port))
                sent[0] += 1
            # Three packets every 4 ms is 750 packets/s while leaving enough
            # scheduler time for the receiver thread on Windows.
            time.sleep(0.004)
        sender_socket.close()

    sender = threading.Thread(target=blast, name="mavlink-udp-blast")
    sender.start()
    sender.join(timeout=5.0)
    assert not sender.is_alive()
    time.sleep(0.2)
    reader.stop()
    receiver.close()

    assert reader.state.get("HEARTBEAT") is not None
    assert reader.state.count >= sent[0] * 0.90
    assert reader.parse_errors == 0
    assert reader.bad_crc == 0


def test_truth_target_world_relative_altitude_is_not_treated_as_amsl(tmp_path):
    import json as _json

    from scripts.collect_sim_teacher_episodes import _truth_target

    truth = tmp_path / "truth.json"
    truth.write_text(
        _json.dumps(
            {
                "targets": [
                    {
                        "label": "box",
                        "position": {
                            "latitude_deg": 1.352100,
                            "longitude_deg": 103.819800,
                            "altitude_m": 0.75,
                            "altitude_reference": "world",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (north, east, down), _ = _truth_target(truth, origin=(1.352100, 103.819800, 18.0))
    assert abs(north) < 1e-6 and abs(east) < 1e-6
    assert down == -0.75


def test_collector_current_step_routes_first_and_alternating_actions_without_prior_action():
    """Execute the real collector policy/route block, with no simulator or subprocess."""
    import pathlib
    import textwrap
    from multidetect.sim_teacher.primitives import ActionPrimitive

    collector = _collector_module()
    source = pathlib.Path(collector.__file__).read_text()
    start = source.index('                if plan.teacher == "expert_search":', source.index('            for index in range(plan.steps_per_episode):'))
    end = source.index('                # Non-multirotor onboard policies emit a MotionCommand', start)
    block = textwrap.dedent(source[start:end])
    function = 'def run_one():\n' + textwrap.indent(block, '    ') + '\n    return action, managed_gimbal_action\n'
    actions = [ActionPrimitive.hold(1), ActionPrimitive.gimbal(pitch_deg=-30),
               ActionPrimitive.hold(1), SimpleNamespace(kind='gimbal', rejected=False,
                   action=None, gimbal_pitch_deg=-20, gimbal_yaw_deg=5, gimbal_roll_deg=0)]
    dispatches, ensures = [], []
    policy = SimpleNamespace(next_action=lambda **kwargs: actions.pop(0))
    env = {'plan': SimpleNamespace(teacher='hover'), 'policy': policy,
           'pose': (0, 0, -10), 'yaw': 0.0, 'before': {}, 'timing': {},
           'camera_mount': CameraMount.gimbal(), 'gimbal_session': None,
           'gimbal_controller': object(),
           '_managed_teacher_gimbal_action': collector._managed_teacher_gimbal_action,
           '_ensure_gimbal_session': lambda: ensures.append(True),
           '_dispatch_teacher_gimbal_command': lambda *args, **kwargs: dispatches.append(True)}
    exec(compile(function, str(collector.__file__), 'exec'), env)
    first = env['run_one']()
    second = env['run_one']()
    third = env['run_one']()
    fourth = env['run_one']()
    assert [first[1], third[1]] == [None, None]
    assert second[1] is second[0] and second[1].gimbal_pitch_deg == -30
    assert fourth[1].kind == 'gimbal' and fourth[1].gimbal_pitch_deg == -20
    assert len(dispatches) == 2 and len(ensures) == 2
    actions.append(ActionPrimitive.gimbal(pitch_deg=-10))
    first_gimbal = env['run_one']()
    assert first_gimbal[1] is first_gimbal[0]


def test_collector_gimbal_worker_does_not_call_mutable_watchdog():
    import ast
    import pathlib
    import threading

    collector = _collector_module()
    tree = ast.parse(pathlib.Path(collector.__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == '_execute_managed_gimbal_action']
    assert len(calls) == 1
    check = next(keyword.value for keyword in calls[0].keywords if keyword.arg == 'check')
    assert isinstance(check, ast.Attribute) and check.attr == 'check'
    assert isinstance(check.value, ast.Name) and check.value.id == 'gimbal_send_gate'
    main_id = threading.get_ident()
    seen = []
    done = threading.Event()
    thread = threading.Thread(target=lambda: done.wait(0.5))
    thread.start()
    def watchdog_tick():
        seen.append(threading.get_ident())
        done.set()
    collector._join_action_with_gimbal(thread, None, watchdog_tick)
    assert seen and all(item == main_id for item in seen)


def test_watchdog_cancels_managed_worker_between_drain_and_new_command():
    import threading
    from multidetect.sim_teacher.primitives import ActionPrimitive
    collector = _collector_module()
    gate = collector._GimbalSendGate()
    in_drain, release = threading.Event(), threading.Event()
    sent, worker_errors = [], []
    class Session:
        pending = None
        def poll(self):
            in_drain.set()
            assert release.wait(1)
            return 'ready'
        def command(self, *args): sent.append(args)
    profile = SimpleNamespace(camera=SimpleNamespace(gimbal={
        'enabled': True, 'angle_range_deg': {'pitch': [-135, 45], 'yaw': [-180, 180], 'roll': [-45, 45]}}))
    def work():
        try:
            collector._execute_managed_gimbal_action(ActionPrimitive.gimbal(pitch_deg=0),
                session=Session(), profile=profile, reader=None, check=gate.check)
        except Exception as exc:
            worker_errors.append(str(exc))
    thread = threading.Thread(target=work)
    thread.start()
    assert in_drain.wait(1)
    def watchdog(): raise RuntimeError('watchdog expired')
    def cancel():
        gate.cancel()
        release.set()
    with pytest.raises(RuntimeError, match='watchdog expired'):
        collector._join_action_with_gimbal(thread, None, watchdog, cancel=cancel)
    assert sent == [] and worker_errors == ['gimbal command cancelled']


def test_gimbal_send_gate_serializes_inflight_send_and_cancellation():
    import threading
    collector = _collector_module()
    gate = collector._GimbalSendGate()
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    sent, second_errors = [], []
    def sender():
        entered.set()
        assert release.wait(1)
        sent.append('in-flight')
    sending = threading.Thread(target=lambda: gate.send(sender))
    sending.start()
    assert entered.wait(1)
    def queued_send():
        try:
            gate.send(lambda: sent.append('queued'))
        except Exception as exc:
            second_errors.append(str(exc))
    second = threading.Thread(target=queued_send)
    second.start()
    def cancel():
        gate.cancel()
        cancelled.set()
    stopping = threading.Thread(target=cancel)
    stopping.start()
    assert gate._cancelled.wait(1)
    assert cancelled.wait(1)  # cancellation never waits on the transport
    release.set()
    sending.join(1); stopping.join(1); second.join(1)
    assert cancelled.is_set() and sent == ['in-flight']
    assert second_errors == ['gimbal command cancelled']
    with pytest.raises(RuntimeError, match='cancelled'):
        gate.send(lambda: sent.append('new'))
    assert sent == ['in-flight']
    assert [item['event'] for item in gate.snapshot()] == ['admitted', 'cancelled']


def test_watchdog_returns_without_waiting_for_blocked_gimbal_transport():
    import threading
    collector = _collector_module()
    gate = collector._GimbalSendGate()
    entered, release = threading.Event(), threading.Event()
    def blocked_sender():
        entered.set()
        assert release.wait(1)
    thread = threading.Thread(target=lambda: gate.send(blocked_sender), daemon=True)
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(RuntimeError, match='watchdog expired'):
            collector._join_action_with_gimbal(thread, None,
                lambda: (_ for _ in ()).throw(RuntimeError('watchdog expired')),
                cancel=gate.cancel, abandon_on_cancel=True)
        assert thread.is_alive()  # watchdog returned while transport was blocked
        with pytest.raises(RuntimeError, match='cancelled'):
            # Do not wait for the occupied transport lock in this test; check
            # the same admission barrier directly.
            gate.check()
        assert [item['event'] for item in gate.snapshot()] == ['admitted', 'cancelled']
    finally:
        release.set()
        thread.join(1)
