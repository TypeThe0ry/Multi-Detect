from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace

import pytest

from multidetect.onboard_runtime.bench import generate_fixture
from multidetect.onboard_runtime.camera import CameraCalibration, CameraFrame
from multidetect.onboard_runtime.runtime import RuntimeConfig
from multidetect.onboard_runtime.vehicle import VehicleState
from scripts import onboard_main


def test_replay_cli_uses_shared_policy_and_latency_backends_without_hardware(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("replay must never construct a live transport or model")

    monkeypatch.setattr(onboard_main, "PymavlinkVehicle", forbidden)
    monkeypatch.setattr(onboard_main, "OpenCVCameraSource", forbidden)
    monkeypatch.setattr("scripts.onboard_policy.YoloeProposer", forbidden)
    monkeypatch.setattr("scripts.eval_slow_system.LlamaServerRunner", forbidden)
    episode = generate_fixture(tmp_path / "episode", duration_s=1)
    output = tmp_path / "report.json"
    assert onboard_main.main([
        "replay", "--episode", str(episode), "--duration", "0.4",
        "--detector-latency", "0.01", "--slow-latency", "0.02",
        "--work-dir", str(tmp_path / "frames"), "--output", str(output),
    ]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert report["synthetic"] is True
    assert report["timing_passed"] is True
    assert report["slow_call_exercised"] is True
    assert report["slow_calls"] >= 1
    assert report["outbound_commands_transmitted"] is False
    assert report["policy_worker_stopped"] is True


def test_real_cli_requires_explicit_failsafe_and_finite_positive_timing():
    parser = onboard_main.build_parser()
    arguments = ["real", "--endpoint", "/dev/ttyTHS1", "--camera", "/dev/video0",
                 "--calibration", "camera.json", "--policy-config", "policy.json",
                 "--task", "Find a car"]
    with pytest.raises(SystemExit):
        parser.parse_args(arguments)
    for value in ("nan", "inf", "0", "-1"):
        with pytest.raises(SystemExit):
            parser.parse_args([*arguments, "--failsafe", "rtl", "--stream-hz", value])
    args = parser.parse_args([*arguments, "--failsafe", "land"])
    assert args.baud == 921600


def test_package_shorthand_detects_equals_options_and_preserves_explicit_failsafe(
    monkeypatch, capsys
):
    captured = []

    def fake_run(args):
        captured.append(args)
        return {"ok": True}

    monkeypatch.setattr(onboard_main, "run", fake_run)
    assert onboard_main.main([
        "--mission-package=mission.json", "--operator-key=operator.key", "--failsafe=rtl",
    ]) == 0
    assert captured[0].mode == "real" and captured[0].failsafe == "rtl"
    assert onboard_main.main([
        "--mission-package=mission.json", "--operator-key=operator.key",
    ]) == 0
    assert captured[1].failsafe == "land"
    capsys.readouterr()


def test_real_admission_requires_signed_package_and_key():
    missing_policy = type("Args", (), {"mode": "real", "mission_package": None,
                                       "policy_config": None, "task": "inspect"})()
    with pytest.raises(ValueError, match="signed --mission-package and --operator-key"):
        onboard_main._admit_package(missing_policy)
    missing_task = type("Args", (), {"mode": "real", "mission_package": None,
                                      "policy_config": "policy.json", "task": None})()
    with pytest.raises(ValueError, match="signed --mission-package and --operator-key"):
        onboard_main._admit_package(missing_task)


def test_real_mission_package_admission_requires_signature_key():
    from multidetect.autonomy.mission_package import load_mission_package

    with pytest.raises(ValueError, match="operator key path"):
        load_mission_package({}, require_signature=True)


def test_missing_model_assets_fail_before_camera_or_vehicle_is_opened(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid model assets must fail before hardware is opened")

    monkeypatch.setattr(onboard_main, "PymavlinkVehicle", forbidden)
    monkeypatch.setattr(onboard_main, "OpenCVCameraSource", forbidden)
    calibration = tmp_path / "camera.json"
    calibration.write_text(json.dumps({
        "width": 160, "height": 120, "fx": 120, "fy": 120, "cx": 80, "cy": 60,
        "extrinsics": {"mount_pitch_down_deg": 10},
    }), encoding="utf-8")
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"yoloe_weights": str(tmp_path / "absent.pt")}), encoding="utf-8")
    args = onboard_main.build_parser().parse_args([
        "real", "--endpoint", "/dev/ttyTHS1", "--camera", "/dev/video0",
        "--calibration", str(calibration), "--policy-config", str(policy),
        "--task", "Find a car", "--failsafe", "rtl",
    ])
    with pytest.raises(ValueError, match="signed --mission-package and --operator-key"):
        onboard_main.run(args)


def test_invalid_latency_is_rejected_and_fixture_never_overwrites_input(tmp_path):
    from multidetect.onboard_runtime.bench import LatencyProposer, LatencySlowRunner

    for value in (-1, math.nan, math.inf):
        with pytest.raises(ValueError):
            LatencyProposer(value)
        with pytest.raises(ValueError):
            LatencySlowRunner(value)
    with pytest.raises(FileExistsError):
        generate_fixture(tmp_path)


def test_startup_camera_failure_does_not_wait_for_timeout():
    with pytest.raises(RuntimeError, match="camera startup failed"):
        onboard_main._ready(
            SimpleNamespace(snapshot=lambda: SimpleNamespace()),
            SimpleNamespace(latest=lambda: None, error=RuntimeError("camera failed")),
            SimpleNamespace(), 1,
        )


def test_real_readiness_waits_for_home_and_landed_messages():
    count = [0]

    def snapshot():
        count[0] += 1
        now = time.monotonic()
        return VehicleState(
            position_ned=(0, 0, 0), attitude_deg=(0, 0, 0), armed=False,
            home_ned=(0, 0, 0) if count[0] >= 3 else None,
            landed=True if count[0] >= 3 else None, landed_monotonic=now,
            pose_monotonic=now, attitude_monotonic=now, heartbeat_monotonic=now,
        )

    camera = SimpleNamespace(latest=lambda: CameraFrame(
        b"unused", time.monotonic(), CameraCalibration(10, 10, 10, 10, 5, 5),
    ))
    state, _frame = onboard_main._ready(
        SimpleNamespace(snapshot=snapshot), camera, RuntimeConfig(), 1, require_ground=True,
    )
    assert count[0] >= 3
    assert state.home_ned == (0, 0, 0) and state.landed is True


def test_startup_failure_closes_every_resource_when_policy_close_raises(tmp_path, monkeypatch):
    episode = generate_fixture(tmp_path / "episode", duration_s=1)
    closed = []
    original_ready = onboard_main._ready
    ready_calls = [0]

    def ready(*args, **kwargs):
        ready_calls[0] += 1
        if ready_calls[0] == 2:
            raise TimeoutError("startup lost inputs")
        return original_ready(*args, **kwargs)

    def policy_close():
        closed.append("policy")
        raise RuntimeError("backend close failed")

    monkeypatch.setattr(onboard_main, "_ready", ready)
    monkeypatch.setattr(onboard_main, "OnboardPolicy",
                        lambda **kwargs: SimpleNamespace(close=policy_close))
    original_camera_close = onboard_main.ReplayCameraSource.close
    original_vehicle_close = onboard_main.ReplayVehicle.close

    def camera_close(camera):
        closed.append("camera")
        original_camera_close(camera)

    def vehicle_close(vehicle):
        closed.append("vehicle")
        original_vehicle_close(vehicle)

    monkeypatch.setattr(onboard_main.ReplayCameraSource, "close", camera_close)
    monkeypatch.setattr(onboard_main.ReplayVehicle, "close", vehicle_close)
    args = onboard_main.build_parser().parse_args([
        "replay", "--episode", str(episode), "--duration", "0.2",
        "--work-dir", str(tmp_path / "frames"),
    ])
    report = onboard_main.run(args)
    assert closed == ["policy", "camera", "vehicle"]
    assert report["ok"] is False
    assert "startup lost inputs" in report["error"]
    assert "backend close failed" in report["error"]
