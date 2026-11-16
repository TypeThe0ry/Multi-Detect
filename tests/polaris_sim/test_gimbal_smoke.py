import math
import sys
from types import ModuleType, SimpleNamespace

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
            pitch = math.radians(-25.0)
            return SimpleNamespace(
                q=(math.cos(pitch / 2.0), 0.0, math.sin(pitch / 2.0), 0.0),
                flags=64,
            )

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
