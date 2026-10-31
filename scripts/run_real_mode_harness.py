#!/usr/bin/env python3
"""Run the complete REAL entry against a loopback pymavlink PX4 and GCS."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from pymavlink import mavutil

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get("CX_REAL_HARNESS_SCRATCH", ROOT.parent / "_scratch" / "cx-real-mode-harness"))


class FakePx4:
    def __init__(self, port: int, values: dict[str, float]):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.02)
        self.peer = ("127.0.0.1", port)
        self.parser = mavutil.mavlink.MAVLink(None)
        self.sender = mavutil.mavlink.MAVLink(None, srcSystem=1, srcComponent=1)
        self.sender.file = self
        self.values, self.stop = values, threading.Event()
        self.mode, self.armed, self.landed = "HOLD", False, True
        self.position, self.target = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
        self.commands: list[str] = []
        self.last_telemetry = 0.0
        self.thread = threading.Thread(target=self._loop, name="fake-px4", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(2)
        self.sock.close()

    def write(self, data):
        if self.peer is not None:
            return self.sock.sendto(data, self.peer)
        return len(data)

    def _telemetry(self):
        base = 128 if self.armed else 0
        custom = 0x00060000 if self.mode == "OFFBOARD" else 0x00040003
        self.sender.heartbeat_send(2, 12, base, custom, 4, 3)
        self.sender.sys_status_send(1063, 1063, 1063, 0, 12000, 1000, 80, 0, 0, 0, 0, 0, 0)
        self.sender.battery_status_send(0, 0, 0, 0, [12000] + [0] * 9, 1000, 100, 0, 80)
        self.sender.local_position_ned_send(0, *self.position, 0, 0, 0)
        self.sender.attitude_send(0, 0, 0, 0, 0, 0, 0)
        self.sender.home_position_send(100000000, 200000000, 3000, 0, 0, 0, [1, 0, 0, 0], 0, 0, 0)
        self.sender.extended_sys_state_send(0, 1 if self.landed else 2)
        self.sender.estimator_status_send(0, 127, 0.1, 0.1, 0.1, 0, 0, 0, 0.5, 0.5)
        self.sender.gps_raw_int_send(0, 3, 100000000, 200000000, 3000, 120, 150, 0, 0, 10)

    def _handle(self, msg):
        kind = msg.get_type()
        if kind == "PARAM_REQUEST_READ":
            name = (msg.param_id.decode(errors="ignore") if isinstance(msg.param_id, bytes) else str(msg.param_id)).rstrip("\x00")
            value = float(self.values.get(name, 0.0))
            ptype = 6 if name in {"NAV_DLL_ACT", "COM_DL_LOSS_T", "NAV_RCL_ACT", "COM_RC_IN_MODE", "COM_OBL_RC_ACT", "GF_ACTION", "COM_LOW_BAT_ACT"} else 9
            wire_value = struct.unpack("<f", struct.pack("<i", int(round(value))))[0] if ptype == 6 else value
            threading.Timer(0.01, lambda: self.sender.param_value_send(name.encode(), wire_value, ptype, 1, 0)).start()
        elif kind == "PARAM_SET":
            name = (msg.param_id.decode(errors="ignore") if isinstance(msg.param_id, bytes) else str(msg.param_id)).rstrip("\x00")
            value = float(struct.unpack("<i", struct.pack("<f", msg.param_value))[0]) if msg.param_type == 6 else float(msg.param_value)
            self.values[name] = value
            threading.Timer(0.01, lambda: self.sender.param_value_send(name.encode(), msg.param_value, msg.param_type, 1, 0)).start()
        elif kind == "COMMAND_LONG":
            command = int(msg.command)
            self.commands.append(str(command))
            if command == 176:  # DO_SET_MODE
                self.mode = "OFFBOARD"
            elif command == 400 and msg.param1 >= 0.5:
                self.armed, self.landed = True, False
            elif command in (20, 21):
                self.mode, self.armed, self.landed = ("RTL" if command == 20 else "LAND"), False, True
            elif command == 512:
                self._telemetry()
            self.sender.command_ack_send(command, 0)
        elif kind == "SET_POSITION_TARGET_LOCAL_NED":
            self.target = [float(msg.x), float(msg.y), float(msg.z)]

    def _loop(self):
        while not self.stop.wait(0.05):
            for _ in range(8):
                try:
                    packet, self.peer = self.sock.recvfrom(4096)
                except TimeoutError:
                    break
                except ConnectionResetError:
                    continue
                for byte in packet:
                    msg = self.parser.parse_char(bytes((byte,)))
                    if msg is not None:
                        self._handle(msg)
            for i in range(3):
                self.position[i] += (self.target[i] - self.position[i]) * 0.45
            if time.monotonic() - self.last_telemetry >= 0.2:
                self._telemetry()
                self.last_telemetry = time.monotonic()


def _free_ports(count=2):
    """Pick loopback UDP ports the onboard process and fake GCS can bind.

    The links are UDP, so probe with UDP: Windows reserves separate, changing UDP
    ranges (Hyper-V/Docker take blocks of the dynamic range while SITL runs), and a
    free TCP port could still fail with WinError 10013.  Stay below the dynamic
    range (49152+) where those reservations land.
    """
    import random

    rng = random.Random()
    held = []
    try:
        while len(held) < count:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("127.0.0.1", rng.randint(20000, 45000)))
            except OSError:
                sock.close()
                continue
            held.append(sock)
        return [sock.getsockname()[1] for sock in held]
    finally:
        for sock in held:
            sock.close()


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    work = SCRATCH / f"real-process-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    key = b"R" * 32
    key_path = work / "operator.key"
    key_path.write_text("hmac:" + key.hex(), encoding="ascii")
    source = json.loads((ROOT / "configs/onboard/mission-package-count-cones-example.json").read_text())
    from multidetect.autonomy.mission_security import AuthenticationKey, sign_document
    package_path = work / "mission.json"
    package_path.write_text(json.dumps(sign_document(source, AuthenticationKey("hmac-sha256", key, "default"))), encoding="utf-8")
    from multidetect.autonomy.mission_package import load_mission_package
    from multidetect.onboard_runtime.preflight import build_parameter_matrix
    package = load_mission_package(source)
    matrix = build_parameter_matrix(package)
    values = {k: v for k, v in matrix.items() if isinstance(v, (int, float))}
    values.update(NAV_RCL_ACT=2, COM_RC_IN_MODE=0, COM_LOW_BAT_ACT=3, COM_OBL_RC_ACT=3)
    px4_port, gcs_port = _free_ports()
    px4 = FakePx4(px4_port, values)
    px4.start()
    calibration = work / "calibration.json"
    calibration.write_text(json.dumps({"width": 320, "height": 240, "fx": 200, "fy": 200, "cx": 160, "cy": 120, "extrinsics": {"mount_pitch_down_deg": 45, "mount_yaw_right_deg": 0, "mount_roll_clockwise_deg": 0, "camera_offset_forward_m": 0, "camera_offset_right_m": 0, "camera_offset_down_m": 0}}))
    env = os.environ.copy()
    env.update(PYTHONPATH=os.pathsep.join((str(ROOT / "src"), str(ROOT))), PYTHONUTF8="1", MULTIDETECT_ONBOARD_TEST_HOOK="scripts.onboard_real_test_hook")
    onboard = subprocess.Popen([sys.executable, "scripts/onboard_main.py", "real", "--endpoint", f"udpin:127.0.0.1:{px4_port}", "--camera", "0", "--calibration", str(calibration), "--mission-package", str(package_path), "--operator-key", str(key_path), "--failsafe", "land", "--duration", "4", "--startup-timeout", "5", "--takeoff-timeout", "8", "--camera-timeout", "15", "--operator-bind-host", "127.0.0.1", "--operator-port", str(gcs_port), "--operator-hmac-key-hex", key.hex(), "--operator-mavlink-signing-key-hex", (b"S" * 32).hex(), "--operator-requires-approval", "--operator-approval-timeout", "20", "--work-dir", str(work)], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    gcs = subprocess.Popen([sys.executable, "scripts/fake_gcs_console.py", str(package_path), "--host", "127.0.0.1", "--port", str(gcs_port), "--hmac-key", key.hex(), "--mavlink-key", (b"S" * 32).hex(), "--package-key", key_path, "--command-key", key_path, "--auto-approve", "--max-seconds", "40"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    try:
        # Fixed lifetimes made this flaky under load: a slow onboard start let the
        # fake GCS exit (7 s) before approving.  The GCS now outlives onboard
        # start-up and is stopped once the onboard process has finished.
        output, _ = onboard.communicate(timeout=75)
        try:
            gcs.wait(timeout=3)
        except subprocess.TimeoutExpired:
            gcs.terminate(); gcs.wait(5)
        gcs_output = gcs.stdout.read() if gcs.stdout else ""
    finally:
        if onboard.poll() is None:
            onboard.terminate(); onboard.wait(5)
        if gcs.poll() is None:
            gcs.terminate(); gcs.wait(5)
        px4.close()
    (SCRATCH / "real-process-onboard.log").write_text(output or "", encoding="utf-8")
    (SCRATCH / "real-process-gcs.log").write_text(gcs_output or "", encoding="utf-8")
    logs = sorted((work / "mission_log").glob("candidate-*/mission.jsonl"))
    records = []
    for log_path in logs:
        records.extend(json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines())
    final_summary = next((row for row in reversed(records) if row.get("kind") == "final_summary"), None)
    announced_plan = next((row for row in records if row.get("kind") == "plan"), None)
    if onboard.returncode != 0 or '"ok": true' not in output or final_summary is None:
        print(output)
        print("GCS:", gcs_output)
        return 1
    assert '"preflight": {' in output and "takeoff" in px4.commands or "400" in px4.commands
    print(json.dumps({"onboard_returncode": onboard.returncode, "gcs_returncode": gcs.returncode, "px4_commands": px4.commands, "announced_plan": announced_plan, "final_log": final_summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
