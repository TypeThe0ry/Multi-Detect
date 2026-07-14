from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "px4_sitl_mission_uploader.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("px4_sitl_mission_uploader", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Message:
    def __init__(self, message_type: str, **fields: Any) -> None:
        self._message_type = message_type
        for name, value in fields.items():
            setattr(self, name, value)

    def get_type(self) -> str:
        return self._message_type


class _MavRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def mission_count_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_COUNT", args))

    def mission_item_int_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_ITEM_INT", args))

    def mission_item_send(self, *args: Any) -> None:
        self.calls.append(("MISSION_ITEM", args))


class _Connection:
    def __init__(self, messages: list[_Message | None]) -> None:
        self.mav = _MavRecorder()
        self._messages = iter(messages)

    def recv_match(self, **_: Any) -> _Message | None:
        return next(self._messages)


def test_hil_mission_is_contiguous_low_altitude_and_has_no_landing_claim() -> None:
    module = _load_script()
    mission = module.build_hil_patrol_mission(
        home_latitude_e7=473_977_430,
        home_longitude_e7=85_455_940,
    )

    assert [item.sequence for item in mission] == [0, 1, 2]
    assert [item.command_name for item in mission] == [
        "MAV_CMD_NAV_TAKEOFF",
        "MAV_CMD_NAV_WAYPOINT",
        "MAV_CMD_NAV_LOITER_TIME",
    ]
    assert max(item.relative_altitude_m for item in mission) == 5.0
    assert all(math.isnan(item.param4) for item in mission)
    assert all("LAND" not in item.command_name for item in mission)


def test_upload_heartbeat_gate_checks_armed_bit_not_only_system_status() -> None:
    module = _load_script()
    disarmed_standby = _Message(
        "HEARTBEAT",
        base_mode=0,
        system_status=module.mavutil.mavlink.MAV_STATE_STANDBY,
    )
    disarmed_active = _Message(
        "HEARTBEAT",
        base_mode=0,
        system_status=module.mavutil.mavlink.MAV_STATE_ACTIVE,
    )

    assert module.validate_disarmed_upload_heartbeat(disarmed_standby)["armed"] is False
    assert (
        module.validate_disarmed_upload_heartbeat(disarmed_active)["system_status"]
        == "MAV_STATE_ACTIVE"
    )

    armed = _Message(
        "HEARTBEAT",
        base_mode=module.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED,
        system_status=module.mavutil.mavlink.MAV_STATE_ACTIVE,
    )
    with pytest.raises(module.SitlMissionUploadError, match="refuses an armed vehicle"):
        module.validate_disarmed_upload_heartbeat(armed)

    disarmed_critical = _Message(
        "HEARTBEAT",
        base_mode=0,
        system_status=module.mavutil.mavlink.MAV_STATE_CRITICAL,
    )
    with pytest.raises(module.SitlMissionUploadError, match="disarmed Standby/Active"):
        module.validate_disarmed_upload_heartbeat(disarmed_critical)


def test_upload_implements_bounded_request_int_sequence_only() -> None:
    module = _load_script()
    mission = module.build_hil_patrol_mission(
        home_latitude_e7=473_977_430,
        home_longitude_e7=85_455_940,
    )
    connection = _Connection(
        [
            _Message("MISSION_REQUEST_INT", seq=0),
            _Message("MISSION_REQUEST_INT", seq=1),
            _Message("MISSION_REQUEST_INT", seq=2),
            _Message("MISSION_ACK", type=0),
        ]
    )

    result = module.upload_mission(
        connection,
        target_system=1,
        target_component=1,
        items=mission,
    )

    assert result.acknowledged is True
    assert result.request_sequences == (0, 1, 2)
    assert result.transmitted_message_count == 4
    assert [name for name, _ in connection.mav.calls] == [
        "MISSION_COUNT",
        "MISSION_ITEM_INT",
        "MISSION_ITEM_INT",
        "MISSION_ITEM_INT",
    ]


def test_upload_rejects_invalid_sequence_and_nonaccepted_ack() -> None:
    module = _load_script()
    mission = module.build_hil_patrol_mission(
        home_latitude_e7=473_977_430,
        home_longitude_e7=85_455_940,
    )

    with pytest.raises(module.SitlMissionUploadError, match="invalid mission sequence"):
        module.upload_mission(
            _Connection([_Message("MISSION_REQUEST_INT", seq=9)]),
            target_system=1,
            target_component=1,
            items=mission,
        )
    with pytest.raises(module.SitlMissionUploadError, match="MAV_MISSION_INVALID"):
        module.upload_mission(
            _Connection([_Message("MISSION_ACK", type=5)]),
            target_system=1,
            target_component=1,
            items=mission,
        )


def test_container_inspection_requires_exact_owned_isolated_runtime() -> None:
    module = _load_script()
    container_id = "a" * 64
    record = {
        "Id": container_id,
        "Config": {
            "Image": module.PINNED_IMAGE_REFERENCE,
            "Labels": {"multidetect.purpose": module.EXPECTED_PURPOSE_LABEL},
            "Env": ["PX4_SIM_MODEL=sihsim_airplane"],
            "Cmd": ["-c", "mavlink start -x -u 14550 -o 14652"],
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "Devices": [],
            "PortBindings": {},
        },
        "State": {"Running": True},
        "Mounts": [],
    }

    def run_command(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=json.dumps([record]), stderr="")

    evidence = module.inspect_owned_disposable_container(
        container_name=module.EXPECTED_CONTAINER_NAME,
        expected_container_id=container_id,
        run_command=run_command,
    )
    assert evidence["container_id"] == container_id
    assert evidence["ownership_profile"] == "auto_mission"
    assert all(evidence["checks"].values())

    record["HostConfig"]["Devices"] = [{"PathOnHost": "COM1"}]
    with pytest.raises(module.SitlMissionUploadError, match="no_device_mapping"):
        module.inspect_owned_disposable_container(
            container_name=module.EXPECTED_CONTAINER_NAME,
            expected_container_id=container_id,
            run_command=run_command,
        )


def test_datalink_profile_allows_only_the_exact_loopback_udp_mapping() -> None:
    module = _load_script()
    container_id = "b" * 64
    record = {
        "Id": container_id,
        "Config": {
            "Image": module.PINNED_IMAGE_REFERENCE,
            "Labels": {"multidetect.purpose": module.DATALINK_PURPOSE_LABEL},
            "Env": ["PX4_SIM_MODEL=sihsim_airplane"],
            "Cmd": ["-c", "mavlink start -x -u 18570 -o 14652"],
        },
        "HostConfig": {
            "NetworkMode": "bridge",
            "Privileged": False,
            "Devices": [],
            "PortBindings": {"18570/udp": [{"HostIp": "127.0.0.1", "HostPort": "18570"}]},
        },
        "State": {"Running": True},
        "Mounts": [],
    }

    def run_command(*_: Any, **__: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout=json.dumps([record]), stderr="")

    evidence = module.inspect_owned_disposable_container(
        container_name=module.DATALINK_CONTAINER_NAME,
        expected_container_id=container_id,
        ownership_profile="datalink_loss",
        run_command=run_command,
    )
    assert evidence["ownership_profile"] == "datalink_loss"
    assert all(evidence["checks"].values())

    record["HostConfig"]["PortBindings"]["18570/udp"][0]["HostIp"] = "0.0.0.0"
    with pytest.raises(module.SitlMissionUploadError, match="exact_host_port_boundary"):
        module.inspect_owned_disposable_container(
            container_name=module.DATALINK_CONTAINER_NAME,
            expected_container_id=container_id,
            ownership_profile="datalink_loss",
            run_command=run_command,
        )


def test_script_exposes_no_generic_or_real_hardware_command_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    upper = text.upper()

    assert "SITL_HOST_PORT = 14652" in text
    assert "PROTECTED_GROUND_STATION_PORT = 14550" in text
    assert '"datalink_loss"' in text
    assert '"HostIp": "127.0.0.1"' in text
    assert "--acknowledge-owned-disposable-sitl" in text
    assert "MISSION_COUNT_SEND" in upper
    assert "MISSION_ITEM_INT_SEND" in upper
    assert "COMMAND_LONG_SEND" not in upper
    assert "PARAM_SET_SEND" not in upper
    assert "MISSION_CLEAR_ALL_SEND" not in upper
    assert "SET_MODE" not in upper
    assert "UDPOUT:" not in upper
    assert "SERIAL:" not in upper
    assert 'payload_control_supported": False' in text
