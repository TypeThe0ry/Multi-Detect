from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from multidetect.onboard_runtime.camera import CameraSource
from multidetect.onboard_runtime.replay import (
    ReplayCameraSource,
    ReplayTimeline,
    ReplayVehicle,
    load_episode,
)
from multidetect.onboard_runtime.runtime import observation_for
from multidetect.onboard_runtime.vehicle import PositionSetpoint, VehicleInterface


def write_episode(path: Path) -> Path:
    (path / "rgb").mkdir(parents=True)
    (path / "rgb/first.png").write_bytes(b"first")
    (path / "rgb/second.png").write_bytes(b"second")
    intrinsics = {"width": 4, "height": 3, "fx": 2, "fy": 2, "cx": 2, "cy": 1.5}
    observations = [
        {"rgb_path": "rgb/first.png", "camera_info": intrinsics,
         "receive_monotonic_ns": 10_000_000_000},
        {"rgb_path": "rgb/second.png", "camera_info": intrinsics,
         "receive_monotonic_ns": 11_000_000_000},
    ]
    (path / "steps.jsonl").write_text(json.dumps({
        "obs_before": observations[0], "obs_after": observations[1],
        "receive_monotonic_ns": 11_100_000_000,
    }) + "\n", encoding="utf-8")
    telemetry = [
        {"message_type": "LOCAL_POSITION_NED", "x": 1, "y": 2, "z": -3,
         "vx": 0, "vy": 0, "vz": 0, "receive_monotonic_ns": 9_900_000_000},
        {"mavpackettype": "ATTITUDE", "roll": 0, "pitch": 0, "yaw": 0,
         "receive_monotonic_ns": 10_000_000_000},
        {"message_type": "EXTENDED_SYS_STATE", "landed_state": 1,
         "receive_monotonic_ns": 10_000_000_000},
        {"message_type": "LOCAL_POSITION_NED", "x": 4, "y": 5, "z": -6,
         "vx": 1, "vy": 0, "vz": 0, "receive_monotonic_ns": 11_000_000_000},
    ]
    (path / "mavlink.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in telemetry), encoding="utf-8",
    )
    (path / "episode.json").write_text('{"task_text": "Inspect facade"}', encoding="utf-8")
    return path


def test_replay_camera_and_vehicle_share_recorded_intervals(tmp_path: Path) -> None:
    recording = load_episode(write_episode(tmp_path))
    assert recording.duration_s == 1.0
    assert recording.task_text == "Inspect facade"
    now = [50.0]
    timeline = ReplayTimeline(clock=lambda: now[0])
    vehicle = ReplayVehicle(recording, timeline)
    camera = ReplayCameraSource(recording, timeline)
    assert isinstance(vehicle, VehicleInterface)
    assert isinstance(camera, CameraSource)
    vehicle.start()
    camera.start()
    assert camera.latest().rgb == b"first"
    assert vehicle.snapshot().position_ned == (1, 2, -3)
    assert observation_for(camera.latest(), vehicle.snapshot())["landed"] is True
    assert vehicle.snapshot().pose_monotonic == pytest.approx(49.9)
    now[0] = 50.5
    assert camera.latest().rgb == b"first"
    assert vehicle.snapshot().position_ned == (1, 2, -3)
    now[0] = 51.1
    deadline = time.monotonic() + 1
    while camera.latest().rgb != b"second" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert camera.latest().rgb == b"second"
    assert camera.latest().timestamp_monotonic == 51.0
    assert vehicle.snapshot().position_ned == (4, 5, -6)
    assert vehicle.snapshot().pose_monotonic == 51.0
    now[0] = 60.0
    assert vehicle.snapshot().pose_monotonic == 51.0
    camera.close()
    vehicle.close()
    assert camera.latest() is None


def test_replay_commands_and_timing_report(tmp_path: Path) -> None:
    now = [100.0]
    vehicle = ReplayVehicle(
        load_episode(write_episode(tmp_path)), ReplayTimeline(clock=lambda: now[0]),
    )
    vehicle.start()
    setpoint = PositionSetpoint(1, 2, -3, 0)
    for index in range(21):
        now[0] = 100 + index / 20
        vehicle.send_setpoint(setpoint)
    vehicle.arm()
    vehicle.set_mode("OFFBOARD")
    vehicle.land()
    vehicle.rtl()
    assert [command for _, command in vehicle.commands] == ["ARM", "OFFBOARD", "LAND", "RTL"]
    report = vehicle.timing_report()
    assert report["setpoint_rate_hz"] == 20
    assert report["setpoint_max_gap_s"] == pytest.approx(0.05)
    vehicle.close()
    with pytest.raises(RuntimeError, match="running"):
        vehicle.send_setpoint(setpoint)


def test_replay_timeline_speed_and_idempotent_start() -> None:
    now = [10.0]
    timeline = ReplayTimeline(speed=2, clock=lambda: now[0])
    timeline.start()
    now[0] = 10.5
    timeline.start()
    assert timeline.elapsed() == 1.0
    assert timeline.timestamp(1.0) == 10.5
    with pytest.raises(ValueError, match="speed"):
        ReplayTimeline(speed=0)


def test_replay_rejects_rgb_path_escape(tmp_path: Path) -> None:
    episode = write_episode(tmp_path / "episode")
    rows = json.loads((episode / "steps.jsonl").read_text(encoding="utf-8"))
    rows["obs_before"]["rgb_path"] = "../../outside.png"
    (episode / "steps.jsonl").write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(ValueError, match="inside the episode"):
        load_episode(episode)


def test_replay_requires_recorded_rgb_and_telemetry(tmp_path: Path) -> None:
    write_episode(tmp_path)
    (tmp_path / "mavlink.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="MAVLink"):
        load_episode(tmp_path)


def test_loader_uses_frame_receipt_time_before_recording_time(tmp_path: Path) -> None:
    write_episode(tmp_path)
    rows = json.loads((tmp_path / "steps.jsonl").read_text(encoding="utf-8"))
    rows["obs_before"]["frame_received_monotonic_ns"] = 9_000_000_000
    rows["obs_after"]["frame_received_monotonic_ns"] = 9_500_000_000
    (tmp_path / "steps.jsonl").write_text(json.dumps(rows), encoding="utf-8")
    recording = load_episode(tmp_path)
    assert recording.frames[1].offset_s == 0.5
    assert recording.telemetry[0].offset_s == pytest.approx(0.9)


def test_replay_latest_does_not_wait_for_disk_read(tmp_path: Path, monkeypatch) -> None:
    recording = load_episode(write_episode(tmp_path))
    now = [50.0]
    camera = ReplayCameraSource(recording, ReplayTimeline(clock=lambda: now[0]))
    reading, release = threading.Event(), threading.Event()
    original_read = Path.read_bytes

    def slow_read(path: Path) -> bytes:
        if path.name == "second.png":
            reading.set()
            release.wait(timeout=2)
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", slow_read)
    camera.start()
    try:
        now[0] = 51.1
        assert reading.wait(timeout=1)
        started = time.monotonic()
        assert camera.latest().rgb == b"first"
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        camera.close()
