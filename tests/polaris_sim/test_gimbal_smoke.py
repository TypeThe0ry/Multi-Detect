import math
import sys
from types import ModuleType, SimpleNamespace

import pytest

from scripts.polaris_sim import gimbal_smoke


def test_gimbal_smoke_sends_device_one_commands_and_checks_attitude(monkeypatch, capsys):
    command_configure = 1001
    command_pitch_yaw = 1000
    sent = []

    class FakeConnection:
        target_system = 1
        target_component = 1

        class Mav:
            @staticmethod
            def command_long_send(*args):
                sent.append(args)

        mav = Mav()

        def wait_heartbeat(self, timeout):
            return object()

        def recv_match(self, *, type, blocking, timeout):
            if type == ["COMMAND_ACK"]:
                if not hasattr(self, "acks"):
                    self.acks = 0
                command = sent[self.acks][2]
                self.acks += 1
                return SimpleNamespace(command=command, result=0)
            if type == ["GIMBAL_DEVICE_ATTITUDE_STATUS"]:
                pitch = math.radians(-25.0)
                yaw = math.pi / 2.0
                return SimpleNamespace(
                    q=(
                        math.cos(yaw / 2.0) * math.cos(pitch / 2.0),
                        -math.sin(yaw / 2.0) * math.sin(pitch / 2.0),
                        math.cos(yaw / 2.0) * math.sin(pitch / 2.0),
                        math.sin(yaw / 2.0) * math.cos(pitch / 2.0),
                    ),
                    flags=64,
                    get_srcSystem=lambda: 1,
                )
            if type == ["ATTITUDE"]:
                return SimpleNamespace(yaw=math.pi / 2.0, get_srcSystem=lambda: 1)
            raise AssertionError(f"unexpected message filter: {type}")

        def close(self):
            return None

    fake_mavutil = SimpleNamespace(
        mavlink=SimpleNamespace(
            MAV_CMD_DO_GIMBAL_MANAGER_CONFIGURE=command_configure,
            MAV_CMD_DO_GIMBAL_MANAGER_PITCHYAW=command_pitch_yaw,
            MAV_RESULT_ACCEPTED=0,
        ),
        mavlink_connection=lambda *args, **kwargs: FakeConnection(),
    )
    pymavlink = ModuleType("pymavlink")
    pymavlink.mavutil = fake_mavutil
    monkeypatch.setitem(sys.modules, "pymavlink", pymavlink)

    assert gimbal_smoke.main(["--timeout", "1"]) == 0
    assert sent[0][2] == command_configure
    assert sent[0][4:] == (255.0, 190.0, -1.0, -1.0, 0.0, 0.0, 1.0)
    assert sent[1][2] == command_pitch_yaw
    assert sent[1][10] == 1.0
    assert "PASS" in capsys.readouterr().out


def test_expected_yaw_readback_converts_vehicle_frame_command_to_earth_frame():
    assert gimbal_smoke.expected_yaw_readback_deg(
        0.0,
        command_flags=44,
        status_flags=64,
        vehicle_yaw_deg=89.1,
    ) == pytest.approx(89.1)
    assert gimbal_smoke.expected_yaw_readback_deg(
        -20.0,
        command_flags=0,
        status_flags=64,
        vehicle_yaw_deg=89.1,
    ) == -20.0
