"""Real-runtime wiring of the operator link in scripts/onboard_main.py (no hardware)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.onboard_main as onboard_main
from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_operator import MissionOperatorAdapter
from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import OnboardMissionLog
from multidetect.operator_link import VideoGeometry
from multidetect.operator_protocol import OperatorTunnelCodec

EXAMPLE = Path("configs/onboard/mission-package-count-cones-example.json")


def _supervisor(tmp_path: Path, *, approved: bool = True) -> MissionSupervisor:
    package = load_mission_package(json.loads(EXAMPLE.read_text(encoding="utf-8")))
    supervisor = MissionSupervisor(
        MissionExecutor(package.plan, loop_factory=lambda *a, **k: None, refs=package.refs),
        package,
        log=OnboardMissionLog(tmp_path, package.mission_id),
        requires_approval=not approved,
    )
    return supervisor


def _adapter(supervisor=None) -> MissionOperatorAdapter:
    codec = OperatorTunnelCodec(
        hmac_key=b"k" * 32, geometries=(VideoGeometry("camera-main", 1280, 720),)
    )
    return MissionOperatorAdapter(codec, supervisor=supervisor)


def _link_args(**extra):
    return SimpleNamespace(
        operator_bind_host="127.0.0.1",
        operator_port=0,
        operator_hmac_key_hex="11" * 32,
        operator_mavlink_signing_key_hex="22" * 32,
        operator_requires_approval=True,
        **extra,
    )


def test_operator_link_refuses_to_start_without_a_package_key() -> None:
    pytest.importorskip("pymavlink")
    policy = SimpleNamespace(supervisor=None, admit_operator_package=lambda package: None)
    with pytest.raises(ValueError, match="verification key"):
        onboard_main._start_operator_link(_link_args(), policy, SimpleNamespace())


def test_start_up_mission_is_bound_and_held_for_operator_approval(tmp_path) -> None:
    pytest.importorskip("pymavlink")
    current = _supervisor(tmp_path)
    assert current.plan_approved  # a CLI package is approved by default
    policy = SimpleNamespace(
        supervisor=current,
        runtime_mode="sim",
        prepare_operator_package=lambda package: None,
        install_operator_package=lambda supervisor: None,
    )
    server = onboard_main._start_operator_link(
        _link_args(operator_package_key_hex="33" * 32), policy, SimpleNamespace()
    )
    try:
        assert server.mission_adapter.supervisor is current
        assert current.requires_approval and not current.plan_approved
        records = [
            json.loads(line) for line in current.log.path.read_text(encoding="utf-8").splitlines()
        ]
        (plan,) = [r for r in records if r["kind"] == "plan"]
        assert plan["plan_sha256"] == current.plan_sha256 and plan["requires_approval"]
    finally:
        server.close()


def test_await_approval_seals_the_approved_mission(tmp_path) -> None:
    supervisor = _supervisor(tmp_path, approved=False)
    adapter = _adapter(supervisor)
    policy = SimpleNamespace(supervisor=supervisor)
    server = SimpleNamespace(mission_adapter=adapter)
    threading.Timer(0.1, lambda: supervisor.approve_plan(supervisor.plan_sha256)).start()
    assert onboard_main._await_operator_approval(policy, server, 5, poll_s=0.01) is supervisor
    assert adapter._sealed


def test_await_approval_fails_on_rejection_timeout_and_replacement(tmp_path) -> None:
    rejected = _supervisor(tmp_path / "rejected", approved=False)
    rejected.reject_plan(rejected.plan_sha256)
    server = SimpleNamespace(mission_adapter=_adapter(rejected))
    with pytest.raises(RuntimeError, match="rejected"):
        onboard_main._await_operator_approval(
            SimpleNamespace(supervisor=rejected), server, 1, poll_s=0.01
        )
    waiting = _supervisor(tmp_path / "waiting", approved=False)
    with pytest.raises(TimeoutError):
        onboard_main._await_operator_approval(
            SimpleNamespace(supervisor=waiting),
            SimpleNamespace(mission_adapter=_adapter(waiting)),
            0.05,
            poll_s=0.01,
        )
    # A replacement admitted between the approval check and the seal never flies.
    approved = _supervisor(tmp_path / "approved")
    replacement = _supervisor(tmp_path / "replacement", approved=False)
    with pytest.raises(RuntimeError, match="changed"):
        onboard_main._await_operator_approval(
            SimpleNamespace(supervisor=approved),
            SimpleNamespace(mission_adapter=_adapter(replacement)),
            1,
            poll_s=0.01,
        )


class _Closable:
    def __init__(self, name, calls, *, fail=False):
        self.name, self.calls, self.fail = name, calls, fail

    def close(self):
        self.calls.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


def test_shutdown_closes_each_resource_once_on_every_path() -> None:
    calls: list[str] = []
    runtime = _Closable("runtime", calls)
    errors = onboard_main._shutdown(
        runtime,
        _Closable("server", calls),
        _Closable("policy", calls),
        _Closable("camera", calls),
        _Closable("vehicle", calls),
    )
    # The runtime closes policy, camera and vehicle itself: no second close.
    assert errors == [] and calls == ["runtime", "server"]
    calls.clear()
    errors = onboard_main._shutdown(
        None,
        _Closable("server", calls),
        _Closable("policy", calls, fail=True),
        _Closable("camera", calls),
        _Closable("vehicle", calls),
    )
    # Without a runtime nothing leaked, even with an operator link open.
    assert calls == ["policy", "camera", "vehicle", "server"]
    assert errors == ["policy failed"]


def test_real_runtime_waits_for_approval_on_the_policy_that_owns_the_supervisor(
    tmp_path, monkeypatch
) -> None:
    """HomeRelativePolicy has no ``supervisor``: the wait raised AttributeError
    and the real runtime never took off with an operator link configured."""
    supervisor = _supervisor(tmp_path / "log", approved=False)
    package = supervisor.package
    events: list[str] = []
    state = SimpleNamespace(
        armed=False, landed=True, home_ned=(0.0, 0.0, 0.0), position_ned=(0.0, 0.0, 0.0)
    )
    calibration = SimpleNamespace(
        camera_info={}, width=1280, height=720, extrinsics={"mount_pitch_down_deg": 45.0}
    )
    frame = SimpleNamespace(calibration=calibration)

    class Device:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def close(self):
            events.append("device_close")

    class Policy:
        def __init__(self, **kwargs):
            self.supervisor = supervisor

        def close(self):
            events.append("policy_close")

    class Gate:
        def __init__(self, vehicle, package):
            pass

        def run(self):
            return {"passed": True, "blocking_failures": []}

    adapter = _adapter(supervisor)

    class Server:
        mission_adapter = adapter

        def close(self):
            events.append("server_close")

    class Runtime:
        owns_flight = True
        stop_requested = False

        def __init__(self, *args, **kwargs):
            pass

        def start(self, **kwargs):
            pass

        def arm_and_takeoff(self, altitude, timeout_s):
            events.append("takeoff" if supervisor.plan_approved and adapter._sealed else "early")

        def fail(self, reason):
            events.append(f"fail:{reason}")

        def run(self, duration):
            pass

        def close(self):
            events.append("runtime_close")

        def report(self):
            return {}

        hazard = None

    monkeypatch.setattr(
        onboard_main, "_admit_package", lambda args: (package, {"task_text": "count cones"})
    )
    monkeypatch.setattr(onboard_main, "_ready", lambda *a, **k: (state, frame))
    monkeypatch.setattr(onboard_main, "_validate_local_models", lambda options: None)
    monkeypatch.setattr(
        onboard_main, "CameraCalibration", SimpleNamespace(load=lambda path: calibration)
    )
    monkeypatch.setattr(onboard_main, "OpenCVCameraSource", Device)
    monkeypatch.setattr(onboard_main, "PymavlinkVehicle", Device)
    monkeypatch.setattr(onboard_main, "PX4PreflightGate", Gate)
    monkeypatch.setattr(onboard_main, "GroundProjector", lambda calibration: None)
    monkeypatch.setattr(onboard_main, "OnboardPolicy", Policy)
    monkeypatch.setattr(onboard_main, "OnboardRuntime", Runtime)
    monkeypatch.setattr(onboard_main, "_start_operator_link", lambda *a, **k: Server())
    args = onboard_main.build_parser().parse_args(
        [
            "real",
            "--endpoint",
            "udpin:0.0.0.0:14540",
            "--camera",
            "0",
            "--calibration",
            str(tmp_path / "calibration.json"),
            "--failsafe",
            "land",
            "--duration",
            "1",
            "--work-dir",
            str(tmp_path / "work"),
            "--operator-bind-host",
            "127.0.0.1",
            "--operator-requires-approval",
        ]
    )

    def approve_later():
        time.sleep(0.2)
        supervisor.approve_plan(supervisor.plan_sha256)

    threading.Thread(target=approve_later, daemon=True).start()
    report = onboard_main.run(args)
    assert "takeoff" in events and not any(e.startswith("fail:") for e in events), events
    assert events.count("runtime_close") == 1 and events.count("server_close") == 1
    assert "error" not in report
