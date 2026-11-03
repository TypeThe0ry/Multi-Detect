from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from multidetect.onboard_runtime import runtime as runtime_module
from multidetect.onboard_runtime.camera import CameraCalibration, CameraFrame
from multidetect.onboard_runtime.runtime import OnboardRuntime, hold_setpoint
from multidetect.onboard_runtime.vehicle import VehicleState


@pytest.fixture
def sensor_reads(monkeypatch):
    """Advance a controlled clock during reads without starting any threads."""
    clock = SimpleNamespace(now=100.0)
    state = VehicleState(
        position_ned=(10.0, 20.0, -10.0),
        attitude_deg=(0.0, 0.0, 15.0),
        armed=True,
        mode="OFFBOARD",
        pose_monotonic=99.9,
        attitude_monotonic=99.9,
        heartbeat_monotonic=99.9,
    )
    frame = CameraFrame(b"rgb", 99.9, CameraCalibration(20, 10, 10, 10, 10, 5))
    reads = SimpleNamespace(state=state, frame=frame, camera_delay=0.01)

    def snapshot():
        clock.now += 0.01
        return reads.state

    def latest():
        clock.now += reads.camera_delay
        return reads.frame

    runtime = OnboardRuntime(
        SimpleNamespace(snapshot=snapshot), SimpleNamespace(latest=latest), policy=None
    )
    targets, launches = [], []
    runtime.pump = SimpleNamespace(error=None, set_target=targets.append)
    runtime._active = True
    runtime._last_good_state = replace(state, position_ned=(9.0, 19.0, -10.0))
    monkeypatch.setattr(runtime, "_launch_policy", lambda *args: launches.append(args))
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return runtime, reads, clock, targets, launches


@pytest.mark.parametrize("entrypoint", ["_guard", "poll"])
def test_freshness_clock_is_sampled_after_sensor_reads(sensor_reads, entrypoint):
    runtime, reads, clock, targets, launches = sensor_reads

    def snapshot():
        clock.now += 0.01
        reads.state = replace(
            reads.state,
            pose_monotonic=clock.now,
            attitude_monotonic=clock.now,
            heartbeat_monotonic=clock.now,
        )
        return reads.state

    def latest():
        clock.now += 0.01
        reads.frame = replace(reads.frame, timestamp_monotonic=clock.now)
        return reads.frame

    runtime.vehicle.snapshot = snapshot
    runtime.camera.latest = latest
    getattr(runtime, entrypoint)()

    assert runtime.hazard is None
    assert targets == []
    if entrypoint == "_guard":
        assert runtime._last_good_state == reads.state
        assert launches == []
    else:
        assert launches == [(reads.frame, reads.state)]


@pytest.mark.parametrize("entrypoint", ["_guard", "poll"])
@pytest.mark.parametrize("sensor", ["pose", "attitude", "heartbeat", "camera"])
@pytest.mark.parametrize("stamp", [90.0, 101.0], ids=["stale", "future"])
def test_freshness_still_rejects_stale_and_future_sensor_timestamps(
    sensor_reads, entrypoint, sensor, stamp
):
    runtime, reads, _clock, targets, launches = sensor_reads
    if sensor == "camera":
        reads.frame = replace(reads.frame, timestamp_monotonic=stamp)
    else:
        reads.state = replace(reads.state, **{f"{sensor}_monotonic": stamp})

    getattr(runtime, entrypoint)()

    assert runtime.hazard == f"stale_{sensor}"
    assert targets == [hold_setpoint(runtime._last_good_state)] * (
        2 if entrypoint == "_guard" else 1
    )
    assert launches == []


@pytest.mark.parametrize("entrypoint", ["_guard", "poll"])
def test_sensor_read_delay_counts_toward_freshness(sensor_reads, entrypoint):
    runtime, reads, _clock, targets, launches = sensor_reads
    reads.camera_delay = 1.0

    getattr(runtime, entrypoint)()

    assert runtime.hazard == "stale_pose"
    assert targets == [hold_setpoint(runtime._last_good_state)] * (
        2 if entrypoint == "_guard" else 1
    )
    assert launches == []
