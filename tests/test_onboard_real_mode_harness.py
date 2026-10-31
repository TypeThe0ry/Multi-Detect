"""Offline real-entry safety harness; no hardware, SITL, or model server."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multidetect.autonomy.mission_package import load_mission_package
from multidetect.autonomy.mission_security import AuthenticationKey, sign_document
from multidetect.onboard_runtime.preflight import PX4PreflightGate, build_parameter_matrix
from multidetect.onboard_runtime.vehicle import VehicleState
from scripts import onboard_main


def _signed_package(tmp_path: Path):
    key = AuthenticationKey("hmac-sha256", b"R" * 32, "default")
    raw = json.loads(Path("configs/onboard/mission-package-count-cones-example.json").read_text(encoding="utf-8"))
    signed = sign_document(raw, key)
    package_path, key_path = tmp_path / "mission.json", tmp_path / "operator.key"
    package_path.write_text(json.dumps(signed), encoding="utf-8")
    key_path.write_bytes(b"hmac:" + (b"R" * 32).hex().encode("ascii"))
    if os.name != "nt":
        key_path.chmod(0o600)
    return raw, signed, package_path, key_path, key


def test_real_admission_rejects_unsigned_and_tampered_packages(tmp_path):
    raw, signed, package_path, key_path, _key = _signed_package(tmp_path)
    args = SimpleNamespace(mode="real", mission_package=package_path, operator_key_path=key_path)
    assert onboard_main._admit_package(args)[0].mission_id == raw["mission_id"]
    package_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        onboard_main._admit_package(args)
    signed["task_text"] += " altered"
    package_path.write_text(json.dumps(signed), encoding="utf-8")
    with pytest.raises(ValueError, match="signature"):
        onboard_main._admit_package(args)


class _FakePx4:
    """Preflight-capable PX4 facade with realistic fresh health telemetry."""

    def __init__(self, values, *, battery=80):
        now = time.monotonic()
        self.values = dict(values)
        self.reads = []
        self.writes = []
        self.armed_commands = 0
        self.state = VehicleState(
            position_ned=(0, 0, 0), attitude_deg=(0, 0, 0), velocity_ned=(0, 0, 0),
            home_ned=(0, 0, 0), home_geodetic=(1.0, 2.0, 3.0), landed=True,
            armed=False, mode="HOLD",
            battery_remaining=battery, pose_monotonic=now, attitude_monotonic=now,
            heartbeat_monotonic=now, home_monotonic=now, landed_monotonic=now,
            battery_monotonic=now, sensor_present=1063, sensor_enabled=1063,
            sensor_health=1063, sys_status_monotonic=now, estimator_flags=127,
            estimator_monotonic=now, gps_fix_type=3, gps_satellites=10, gps_hdop=1.2,
            gps_monotonic=now,
        )

    def snapshot(self):
        return self.state

    def read_parameter(self, name, **_):
        self.reads.append(name)
        return self.values[name]

    def write_parameter(self, name, value, **_):
        self.writes.append((name, value))
        self.values[name] = value
        return value


def test_px4_preflight_failure_is_blocking_and_never_arms(tmp_path):
    raw, _signed, _path, _key_path, _key = _signed_package(tmp_path)
    package = load_mission_package(raw)
    matrix = build_parameter_matrix(package)
    values = {
        name: value for name, value in matrix.items()
        if name.startswith(("NAV_", "COM_", "GF_", "BAT_")) and value is not None
    }
    values.update(NAV_RCL_ACT=2, COM_RC_IN_MODE=0, COM_LOW_BAT_ACT=3,
                  COM_OBL_RC_ACT=3, BAT_LOW_THR=0.25, BAT_CRIT_THR=0.2)
    values["NAV_DLL_ACT"] = 2
    vehicle = _FakePx4(values, battery=None)
    result = PX4PreflightGate(vehicle, package).run()
    assert not result["passed"]
    assert "battery_telemetry" in result["blocking_failures"]
    assert "parameter:NAV_DLL_ACT" in result["blocking_failures"]
    assert vehicle.armed_commands == 0
    assert vehicle.writes == []


def test_signed_ground_approval_sequence_replay_is_rejected(tmp_path):
    from multidetect.autonomy.mission_executor import MissionExecutor
    from multidetect.autonomy.mission_supervisor import MissionSupervisor
    from multidetect.autonomy.onboard_mission_log import OnboardMissionLog

    raw, _signed, _package_path, _key_path, key = _signed_package(tmp_path)
    package = load_mission_package(raw)
    supervisor = MissionSupervisor(
        MissionExecutor(package.plan, loop_factory=lambda *a, **k: None, refs=package.refs),
        package, log=OnboardMissionLog(tmp_path / "log", package.mission_id),
        security_key=key, requires_approval=True,
    )
    event = sign_document({
        "mission_id": package.mission_id, "type": "approve_plan",
        "plan_sha256": supervisor.plan_sha256, "sequence": 10,
        "timestamp": time.time(), "timestamp_version": 1, "timestamp_domain": "unix",
    }, key)
    assert supervisor.ground_event(event)
    assert supervisor.plan_approved
    assert not supervisor.ground_event(event)
