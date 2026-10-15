from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from multidetect.onboard_runtime.camera import CameraCalibration, CameraFrame
from multidetect.onboard_runtime.runtime import (
    OnboardRuntime,
    RuntimeConfig,
    SetpointPump,
    hold_setpoint,
)
from multidetect.onboard_runtime.vehicle import PositionSetpoint, VehicleState
from multidetect.sim_teacher.primitives import ActionPrimitive


class FakeVehicle:
    def __init__(self):
        self.position = (10.0, 20.0, -10.0)
        self.mode = "OFFBOARD"
        self.armed = True
        self.landed = False
        self.pose_stamp = None
        self.snapshot_error = False
        self.setpoints = []
        self.commands = []
        self.closed = False
        self.confirm_commands = True
        self.command_delay = 0.0

    def start(self):
        pass

    def snapshot(self):
        if self.snapshot_error:
            raise RuntimeError("receiver stopped")
        now = time.monotonic()
        return VehicleState(
            position_ned=self.position, attitude_deg=(0, 0, 15), velocity_ned=(0, 0, 0),
            home_ned=(10, 20, 0), landed=self.landed, armed=self.armed, mode=self.mode,
            pose_monotonic=now if self.pose_stamp is None else self.pose_stamp,
            attitude_monotonic=now, heartbeat_monotonic=now,
        )

    def send_setpoint(self, setpoint):
        if self.closed:
            raise RuntimeError("closed")
        self.setpoints.append((time.monotonic(), setpoint))

    def arm(self):
        self.commands.append("arm")
        self.armed = True

    def set_mode(self, mode):
        self.commands.append(mode)
        self.mode = mode

    def _command(self, mode):
        self.commands.append(mode)
        time.sleep(self.command_delay)
        if self.confirm_commands:
            self.mode = mode

    def land(self):
        self._command("LAND")

    def rtl(self):
        self._command("RTL")

    def close(self):
        self.closed = True


class FakeCamera:
    calibration = CameraCalibration(20, 10, 10, 10, 10, 5)

    def __init__(self):
        self.stamp = None
        self.closed = False

    def start(self):
        pass

    def latest(self):
        return CameraFrame(b"unused", time.monotonic() if self.stamp is None else self.stamp,
                           self.calibration)

    def close(self):
        self.closed = True


class SlowPolicy:
    def __init__(self, latency=1.5, action=None):
        self.latency = latency
        self.action = action or ActionPrimitive.hold(0.05)
        self.entered = threading.Event()
        self.stopped = threading.Event()
        self.calls = 0

    def act(self, observation, *, pose_ned, yaw_deg):
        self.calls += 1
        self.entered.set()
        self.stopped.wait(self.latency)
        return self.action, {}

    def close(self):
        self.stopped.set()


def wait_for(predicate, timeout=1):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= until:
            pytest.fail("condition did not become true")
        time.sleep(0.005)


def test_stream_rate_and_hold_during_one_and_half_second_policy_call():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy()
    runtime = OnboardRuntime(vehicle, camera, policy)
    runtime.start()
    initial = runtime.pump.target
    try:
        runtime.poll()
        assert policy.entered.wait(0.5)
        runtime.run(2.5)  # the fake slow call takes 1.5 s; leave room on a loaded host
        during_call = [target for stamp, target in vehicle.setpoints
                       if stamp < vehicle.setpoints[0][0] + 1.4]
        assert len(during_call) >= 12
        assert all(target == initial for target in during_call)
        metrics = runtime.report()["setpoints"]
        # PX4 leaves OFFBOARD below ~2 Hz (0.5 s); margins allow a loaded test host.
        assert metrics["rate_hz"] >= 12
        assert metrics["max_gap_s"] < 0.4
        assert runtime.steps >= 1
        assert runtime.hazard is None
    finally:
        runtime.close()
    assert runtime.policy_worker_stopped
    assert vehicle.closed and camera.closed


@pytest.mark.parametrize("field,reason", [("camera", "stale_camera"), ("pose", "stale_pose")])
@pytest.mark.parametrize("failsafe", ["hold", "land", "rtl"])
def test_stale_sensor_holds_then_chosen_failsafe_and_discards_late_action(field, reason, failsafe):
    vehicle, camera = FakeVehicle(), FakeCamera()
    policy = SlowPolicy(0.2, ActionPrimitive.translate(100, 0))
    runtime = OnboardRuntime(vehicle, camera, policy, config=RuntimeConfig(
        camera_timeout_s=0.08, pose_timeout_s=0.08, hold_before_failsafe_s=0.05,
        failsafe=failsafe, shutdown_timeout_s=0.1,
    ))
    runtime.start()
    try:
        runtime.poll()
        assert policy.entered.wait(0.5)
        if field == "camera":
            vehicle.position = (11, 21, -10)
            camera.stamp = time.monotonic() - 1
        else:
            vehicle.pose_stamp = time.monotonic() - 1
        wait_for(lambda: runtime.hazard is not None)
        target = runtime.pump.target
        assert runtime.hazard == reason
        if field == "camera":
            assert (target.north_m, target.east_m) == (11, 21)
        assert vehicle.commands == []
        runtime.run(0.3)
        assert runtime.failsafe_sent == failsafe
        assert runtime.steps == 0
        assert runtime.pump.target == target
        if failsafe == "hold":
            assert vehicle.commands == []
        else:
            wait_for(lambda: failsafe.upper() in vehicle.commands)
    finally:
        runtime.close()


def test_hung_autonomy_times_out_without_stopping_stream():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(10)
    runtime = OnboardRuntime(vehicle, camera, policy, config=RuntimeConfig(
        autonomy_timeout_s=0.08, hold_before_failsafe_s=0.04, failsafe="rtl",
    ))
    runtime.start()
    try:
        runtime.run(0.3)
        assert runtime.hazard == "autonomy_timeout"
        assert runtime.failsafe_sent == "rtl"
        assert len(vehicle.setpoints) >= 4
        assert runtime.pump.report()["max_gap_s"] < 0.2
    finally:
        runtime.close()


def test_receiver_error_still_completes_hold_grace_and_configured_rtl():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(0.01)
    runtime = OnboardRuntime(vehicle, camera, policy, config=RuntimeConfig(
        hold_before_failsafe_s=0.06, failsafe="rtl", shutdown_timeout_s=0.03,
    ))
    runtime.start(own_flight=False)
    try:
        vehicle.snapshot_error = True
        runtime.run(0.25)
        assert "receiver stopped" in runtime.hazard
        assert runtime.failsafe_sent == "rtl"
        assert "RTL" in vehicle.commands
        assert runtime.pump.report()["sent_count"] >= 4
    finally:
        runtime.close()


def test_blocked_failsafe_command_does_not_block_setpoint_guard():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(10)
    vehicle.command_delay = 0.25
    runtime = OnboardRuntime(vehicle, camera, policy, config=RuntimeConfig(
        hold_before_failsafe_s=0.01, camera_timeout_s=0.05, failsafe="land",
    ))
    runtime.start()
    try:
        camera.stamp = time.monotonic() - 1
        runtime.run(0.4)
        assert runtime.pump.report()["sent_count"] >= 6
        assert runtime.pump.report()["max_gap_s"] < 0.2
    finally:
        runtime.close()


def test_shutdown_waits_for_post_command_confirmation_and_cancels_worker():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(10)
    vehicle.confirm_commands = False
    runtime = OnboardRuntime(vehicle, camera, policy, config=RuntimeConfig(shutdown_timeout_s=0.1))
    runtime.start()
    runtime.poll()
    with pytest.raises(RuntimeError, match="did not confirm"):
        runtime.close()
    assert runtime.policy_worker_stopped
    assert vehicle.closed and camera.closed
    assert "LAND" in vehicle.commands


def test_active_start_requires_confirmed_armed_offboard_and_valid_stream_rate():
    with pytest.raises(ValueError, match="at least 10"):
        RuntimeConfig(stream_hz=9)
    with pytest.raises(ValueError, match="at least 10"):
        SetpointPump(FakeVehicle(), PositionSetpoint(0, 0, 0, 0), rate_hz=2)
    vehicle = FakeVehicle()
    vehicle.mode = "HOLD"
    runtime = OnboardRuntime(vehicle, FakeCamera(), SlowPolicy(0))
    with pytest.raises(RuntimeError, match="confirmed"):
        runtime.start()
    assert vehicle.setpoints == []


def test_stale_cached_mode_cannot_confirm_handoff():
    runtime = OnboardRuntime(FakeVehicle(), FakeCamera(), SlowPolicy(0))
    runtime.failsafe_sent = "land"
    runtime._handoff_started = time.monotonic()
    state = replace(runtime.vehicle.snapshot(), mode="LAND", armed=False,
                    heartbeat_monotonic=runtime._handoff_started - 0.01)
    assert not runtime._handoff_confirmed(state)
    assert not runtime._handoff_confirmed(replace(
        state, mode="OFFBOARD", armed=None, heartbeat_monotonic=time.monotonic(),
    ))


def test_policy_timeout_is_enforced_even_if_worker_finishes_between_guard_ticks():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(0.035)
    runtime = OnboardRuntime(vehicle, camera, policy, config=RuntimeConfig(
        stream_hz=10, autonomy_timeout_s=0.02,
    ))
    runtime.start()
    try:
        runtime.poll()
        wait_for(lambda: runtime._worker is not None and not runtime._worker.is_alive())
        runtime.poll()
        assert runtime.hazard == "autonomy_timeout"
        assert runtime.steps == 0
        assert runtime.pump.target == hold_setpoint(vehicle.snapshot())
    finally:
        runtime.close()


def test_takeoff_primes_stream_and_confirms_mode_arm_before_home_relative_climb():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(0)
    vehicle.position = (10, 20, 7)
    vehicle.mode, vehicle.armed, vehicle.landed = "HOLD", False, True
    snapshot = vehicle.snapshot
    send = vehicle.send_setpoint
    vehicle.snapshot = lambda: replace(snapshot(), home_ned=(10, 20, 7))

    def follow_setpoint(target):
        send(target)
        vehicle.position = (target.north_m, target.east_m, target.down_m)

    vehicle.send_setpoint = follow_setpoint
    runtime = OnboardRuntime(vehicle, camera, policy, home_ned=(10, 20, 7))
    runtime.start(active=False)
    started = time.monotonic()
    try:
        runtime.arm_and_takeoff(10, timeout_s=1)
        assert time.monotonic() - started >= 1
        assert vehicle.commands[:2] == ["OFFBOARD", "arm"]
        assert runtime.pump.target.down_m == -3
        assert runtime.owns_flight
        assert policy.calls == 0
        assert len(vehicle.setpoints) >= 15
    finally:
        runtime.close()


def test_changed_home_cannot_silently_shift_policy_coordinate_frame():
    vehicle, camera, policy = FakeVehicle(), FakeCamera(), SlowPolicy(10)
    runtime = OnboardRuntime(vehicle, camera, policy, home_ned=(10, 20, 0))
    runtime.start(own_flight=False)
    original = vehicle.snapshot
    vehicle.snapshot = lambda: replace(original(), home_ned=(15, 20, 1))
    try:
        wait_for(lambda: runtime.hazard == "home_changed")
        assert runtime.pump.target == hold_setpoint(original())
    finally:
        runtime.close()
