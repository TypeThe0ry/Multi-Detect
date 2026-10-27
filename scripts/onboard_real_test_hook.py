"""Explicitly enabled substitutions for the local REAL-mode process harness."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.mission_security import AuthenticationKey
from multidetect.autonomy.mission_supervisor import MissionSupervisor
from multidetect.autonomy.onboard_mission_log import DownlinkQueue, OnboardMissionLog
from multidetect.onboard_runtime.camera import CameraCalibration, CameraFrame
from multidetect.sim_teacher.primitives import ActionPrimitive

TEST_HOOK_METADATA = {
    "test_hook_active": True,
    "test_hook_name": "scripts.onboard_real_test_hook",
    "test_hook_message_zh": "离线真实模式测试 hook 已启用，仅允许回环 endpoint",
}


class FakeCamera:
    def __init__(self, _source, calibration, **_kwargs):
        self.calibration = calibration
        self.error = None
        self.closed = False
        self._frame = None

    def start(self):
        self._frame = CameraFrame(
            b"synthetic-rgb-frame", time.monotonic(), self.calibration
        )

    def latest(self):
        self._frame = CameraFrame(
            b"synthetic-rgb-frame", time.monotonic(), self.calibration
        )
        return self._frame

    def close(self):
        self.closed = True


class FakePolicy:
    def __init__(self, *, task_text, work_dir, mission_package, runtime_mode, **_kwargs):
        self.task_text = task_text
        self.work_dir = Path(work_dir)
        self.package = mission_package
        self.runtime_mode = runtime_mode
        self._generation = 0
        self.supervisor = self._supervisor(mission_package)
        self.executor = self.supervisor.executor
        self.downlink = self.supervisor.downlink
        self.closed = False
        self.steps = 0

    def _supervisor(self, package):
        self._generation += 1
        mission_log_dir = self.work_dir / "mission_log" / f"candidate-{self._generation}"
        executor = MissionExecutor(
            package.plan, loop_factory=lambda *a, **k: None, refs=package.refs
        )
        log = OnboardMissionLog(mission_log_dir, package.mission_id)
        downlink = DownlinkQueue(log, sender=None)
        supervisor = MissionSupervisor(
            executor, package, log=log, downlink=downlink,
            security_key=AuthenticationKey("hmac-sha256", b"R" * 32, "default"),
            runtime_mode="real", requires_approval=True,
            replay_state_path=mission_log_dir / "ground-replay-state.json",
            checkpoint_path=self.work_dir / "crash_resume" / "checkpoint.json",
        )
        supervisor.test_hook_metadata = dict(TEST_HOOK_METADATA)
        return supervisor

    def prepare_operator_package(self, package):
        return self._supervisor(package)

    def install_operator_package(self, supervisor):
        self.package, self.supervisor, self.executor = supervisor.package, supervisor, supervisor.executor

    def act(self, observation, *, pose_ned, yaw_deg):
        self.steps += 1
        if os.environ.get("MULTIDETECT_CRASH_RESUME_TEST") == "1":
            boot_time_ms = observation.get("time_boot_ms")
            if isinstance(boot_time_ms, (int, float)) and not isinstance(boot_time_ms, bool):
                self.supervisor.boot_time_ms = boot_time_ms
            # SITL exercises checkpoint creation; the ordinary loopback harness
            # retains its fixed hold action and approval behavior.
            return self.supervisor.next_action(
                pose_ned=pose_ned, yaw_deg=yaw_deg, landed=observation.get("landed")
            )
        return ActionPrimitive.hold(0.05), {}

    def close(self):
        self.closed = True
        if self.supervisor is not None:
            self.supervisor.finish("stopped")
        if self.downlink is not None:
            self.downlink.close()


def _calibration(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return CameraCalibration.load(path) if value else None


def install(namespace):
    namespace["OpenCVCameraSource"] = FakeCamera
    namespace["OnboardPolicy"] = FakePolicy
    namespace["_validate_local_models"] = lambda _options: None
    namespace["CameraCalibration"] = SimpleNamespace(load=_calibration)
    gate_type = namespace["PX4PreflightGate"]

    class HarnessPreflightGate(gate_type):
        def run(self):
            report = super().run()
            # Only this explicit offline hook tolerates Windows scheduler delay
            # between the already-received HOME/BATTERY frames and the gate.
            if set(report["blocking_failures"]) <= {"home", "battery_telemetry"}:
                report["passed"], report["blocking_failures"] = True, []
            return report

    namespace["PX4PreflightGate"] = HarnessPreflightGate
    command_log = os.environ.get("MULTIDETECT_ONBOARD_MAVLINK_COMMAND_LOG")
    if command_log:
        vehicle_type = namespace["PymavlinkVehicle"]
        for method_name in ("_command", "_command_with_ack"):
            original = getattr(vehicle_type, method_name)

            def traced(self, command, *params, _original=original, _name=method_name):
                result = _original(self, command, *params)
                record = {"pid": os.getpid(), "method": _name,
                          "command": int(command if isinstance(command, int) else command.command),
                          "sent_at": time.time()}
                path = Path(command_log)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
                return result

            setattr(vehicle_type, method_name, traced)
    return dict(TEST_HOOK_METADATA)
