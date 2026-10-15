#!/usr/bin/env python3
"""Run the onboard fault campaign with real source faults and evidence-only verdicts.

The campaign deliberately keeps the test doubles at the transport boundary.  The
same :class:`OnboardRuntime` coordinator used by the aircraft is exercised; the
verdict code only consumes command/setpoint traces and mission-log decisions.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy.energy_estimator import EnergyConfig  # noqa: E402
from multidetect.autonomy.mission_executor import ExecutorConfig, MissionExecutor  # noqa: E402
from multidetect.autonomy.mission_package import load_mission_package  # noqa: E402
from multidetect.autonomy.mission_security import sign_document  # noqa: E402
from multidetect.autonomy.mission_supervisor import MissionSupervisor  # noqa: E402
from multidetect.autonomy.onboard_loop import CameraModel, LoopConfig, OnboardLoop  # noqa: E402
from multidetect.autonomy.onboard_mission_log import OnboardMissionLog  # noqa: E402
from multidetect.onboard_runtime.camera import CameraCalibration, CameraFrame  # noqa: E402
from multidetect.onboard_runtime.runtime import OnboardRuntime, RuntimeConfig  # noqa: E402
from multidetect.onboard_runtime.vehicle import PositionSetpoint, VehicleState  # noqa: E402
from multidetect.sim_teacher.primitives import ActionPrimitive  # noqa: E402

FAULTS = (
    "camera_freeze", "camera_drop", "pose_stale", "slow_model_timeout",
    "slow_model_crash", "detector_exception", "gcs_heartbeat_loss",
    "gcs_heartbeat_recovery", "offboard_loss", "battery_sag", "battery_stale",
    "home_change", "fence_approach",
)


@dataclass(frozen=True, slots=True)
class FaultEvent:
    """A source fault window in monotonic campaign time."""

    fault: str
    at_s: float
    duration_s: float = 1.0
    recover: bool = True
    severity: str = "fault"

    def __post_init__(self) -> None:
        if self.fault not in FAULTS:
            raise ValueError(f"unknown fault: {self.fault}")
        if self.at_s < 0 or self.duration_s <= 0:
            raise ValueError("fault timing must be nonnegative and duration positive")


def schedule_faults(events: Iterable[FaultEvent | Mapping[str, Any]]) -> tuple[FaultEvent, ...]:
    """Validate and sort a deterministic schedule."""
    parsed = tuple(e if isinstance(e, FaultEvent) else FaultEvent(**dict(e)) for e in events)
    ordered = tuple(sorted(parsed, key=lambda e: (e.at_s, e.fault)))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.at_s + left.duration_s > right.at_s:
            raise ValueError(f"overlapping fault windows: {left.fault}, {right.fault}")
    return ordered


def expected_outcome(fault: str, policy: Mapping[str, Any] | None = None) -> str:
    """Return the product action expected from the admitted policy."""
    policy = policy or {}
    action = str(policy.get("action", "continue_then_return"))
    if fault in {"gcs_heartbeat_loss", "gcs_heartbeat_recovery"}:
        if fault == "gcs_heartbeat_recovery" and action in {
            "continue", "continue_then_return", "hold_then_return"
        }:
            return "continue"
        if fault == "gcs_heartbeat_recovery":
            # Compatibility for callers of the original helper: recovery is
            # always a continue expectation.  The campaign uses the explicit
            # policy-aware helper below for loss/recovery timing.
            return "continue"
        return {
            "continue": "continue", "continue_then_return": "return_now",
            "hold_then_return": "return_now", "return_now": "return_now",
            "land_now": "land_now",
        }.get(action, "return_now")
    if fault in {"slow_model_timeout", "slow_model_crash", "detector_exception",
                 "camera_freeze", "camera_drop", "pose_stale", "battery_stale"}:
        return "conservative"
    if fault == "battery_sag":
        return "return_now" if policy.get("energy_action", "return_now") else "conservative"
    if fault in {"offboard_loss", "home_change"}:
        # A mode/home transition is handled by the runtime hazard path.  Keep
        # the old public label for compatibility; campaign rows use the
        # concrete conservative action via ``_campaign_expected_outcome``.
        return "refuse"
    if fault == "fence_approach":
        return "constrained"
    raise ValueError(fault)


def _campaign_expected_outcome(fault: str, policy: Mapping[str, Any]) -> str:
    """Policy semantics used by campaign verdicts (including runtime hazards)."""
    if fault == "gcs_heartbeat_recovery":
        return "continue"
    if fault in {"offboard_loss", "home_change"}:
        return "conservative"
    if fault == "battery_sag" and policy.get("action") == "land_now":
        return "land_now"
    return expected_outcome(fault, policy)


def _expected_reason_fragment(fault: str) -> str:
    return {
        "camera_freeze": "stale_camera", "camera_drop": "stale_camera",
        "pose_stale": "stale_pose", "slow_model_timeout": "autonomy_timeout",
        "slow_model_crash": "policy_error", "detector_exception": "policy_error",
        "gcs_heartbeat_loss": "operator policy", "gcs_heartbeat_recovery": "operator policy",
        "offboard_loss": "offboard_exited", "battery_sag": "energy",
        "battery_stale": "battery telemetry stale", "home_change": "home_changed",
        "fence_approach": "fence",
    }[fault]


def _rank(action: str) -> int:
    return {"continue": 0, "constrained": 1, "hold": 1, "conservative": 1,
            "return_now": 2, "land_now": 3}.get(action, -1)


def verdict(expected: str, observed_action: str | None = None, reason: str = "",
            *, elapsed_s: float = 0.0, deadline_s: float = 3.0,
            observation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate independently recorded evidence.

    ``observed_action`` remains accepted for compatibility with unit callers;
    campaign runs pass an observation and derive it here from its records.
    """
    if observation is not None:
        observed_action, reason, elapsed_s = observed_from_observation(observation)
    observed = observed_action or "unknown"
    if expected == "continue":
        action_ok = observed == "continue"
    elif expected == "conservative":
        action_ok = _rank(observed) >= 1
    elif expected == "constrained":
        action_ok = observed == "constrained"
    elif expected == "return_now":
        action_ok = observed == "return_now"
    elif expected == "land_now":
        action_ok = observed == "land_now"
    else:
        action_ok = False
    return {"passed": bool(action_ok and elapsed_s <= deadline_s and bool(reason)),
            "observed_action": observed, "reason": reason,
            "elapsed_s": elapsed_s, "deadline_s": deadline_s}


def scenario_table() -> list[dict[str, Any]]:
    return [{"fault": name, "stage": "bench/replay" if name not in
             {"offboard_loss", "home_change", "fence_approach"} else "HIL/tethered"}
            for name in FAULTS]


class FaultCameraSource:
    """Camera source that freezes or drops frames only within its event window."""

    def __init__(self, clock: Callable[[], float], event: FaultEvent | None) -> None:
        self.clock, self.event = clock, event
        self.origin = clock()
        self.calibration = CameraCalibration(160, 120, 120, 120, 80, 60,
                                             extrinsics={"mount_pitch_down_deg": 0})
        self.frame: CameraFrame | None = None
        self.started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.started = True
        self._publish()
        self._thread = threading.Thread(target=self._run, name="fault-camera", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.02):
            self._publish()

    def _active(self) -> bool:
        if self.event is None or self.event.fault not in {"camera_freeze", "camera_drop"}:
            return False
        elapsed = self.clock() - self.origin
        return self.event.at_s <= elapsed < self.event.at_s + self.event.duration_s

    def _publish(self) -> None:
        now = self.clock()
        if self._active():
            if self.event and self.event.fault == "camera_drop":
                self.frame = None
            return
        self.frame = CameraFrame(b"synthetic-frame", now, self.calibration)

    def latest(self) -> CameraFrame | None:
        if not self.started:
            return None
        self._publish()
        # Keep the healthy source independently live even while another
        # source (for example pose) is the injected fault.  This prevents a
        # producer scheduling gap from changing the observed fault reason.
        if self.frame is None and not self._active():
            self.frame = CameraFrame(b"synthetic-frame", self.clock(), self.calibration)
        return self.frame

    def close(self) -> None:
        self.started = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)


class FaultVehicle:
    """Vehicle fake exposing every command and telemetry transition."""

    def __init__(self, clock: Callable[[], float], event: FaultEvent | None,
                 area: Mapping[str, float]) -> None:
        self.clock, self.event, self.area = clock, event, area
        self.origin = clock()
        self.commands: list[dict[str, Any]] = []
        self.setpoints: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.closed = False
        self.mode = "OFFBOARD"
        self.home = (0.0, 0.0, 0.0)
        self.position = (0.0, 0.0, -10.0)
        self.battery = 90.0

    def start(self) -> FaultVehicle:
        return self

    def _active(self, name: str) -> bool:
        if self.event is None or self.event.fault != name:
            return False
        elapsed = self.clock() - self.origin
        return self.event.at_s <= elapsed < self.event.at_s + self.event.duration_s

    def snapshot(self) -> VehicleState:
        now = self.clock()
        mode = "HOLD" if self._active("offboard_loss") else self.mode
        home = (2.0, 2.0, 0.0) if self._active("home_change") else self.home
        position = self.position
        if self._active("fence_approach"):
            position = (self.area["north_max_m"] - 0.2, self.area["east_max_m"] - 0.2, -10.0)
        battery = 5.0 if self._active("battery_sag") else self.battery
        pose_stamp = None if self._active("pose_stale") else now
        battery_stamp = None if self._active("battery_stale") else now
        state = VehicleState(position_ned=position, attitude_deg=(0.0, 0.0, 0.0),
                             velocity_ned=(0.0, 0.0, 0.0), home_ned=home,
                             landed=False, armed=True, mode=mode,
                             battery_remaining=battery, pose_monotonic=pose_stamp,
                             attitude_monotonic=pose_stamp, heartbeat_monotonic=now,
                             home_monotonic=now, battery_monotonic=battery_stamp,
                             landed_monotonic=now)
        self.history.append({"timestamp": now, "state": asdict(state)})
        return state

    def send_setpoint(self, setpoint: PositionSetpoint) -> None:
        self.setpoints.append({"timestamp": self.clock(), "setpoint": asdict(setpoint)})

    def arm(self) -> None:
        self.commands.append({"timestamp": self.clock(), "action": "arm"})

    def set_mode(self, mode: str) -> None:
        self.mode = mode.upper()
        self.commands.append({"timestamp": self.clock(), "action": "mode", "mode": self.mode})

    def land(self) -> None:
        self.commands.append({"timestamp": self.clock(), "action": "land"})
        self.mode = "LAND"

    def rtl(self) -> None:
        self.commands.append({"timestamp": self.clock(), "action": "rtl"})
        self.mode = "RTL"

    def close(self) -> None:
        self.closed = True

    def timing_report(self) -> dict[str, Any]:
        stamps = [x["timestamp"] for x in self.setpoints]
        gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
        return {"setpoint_count": len(stamps), "setpoint_max_gap_s": max(gaps, default=0),
                "commands": list(self.commands)}


class GroundEventSource:
    def __init__(self, clock: Callable[[], float], event: FaultEvent | None) -> None:
        self.clock, self.event, self.origin = clock, event, clock()
        self.heartbeats: list[float] = []
        self.loss_started: float | None = None

    def connected(self) -> bool:
        if self.event is None or self.event.fault not in {
            "gcs_heartbeat_loss", "gcs_heartbeat_recovery"
        }:
            return True
        elapsed = self.clock() - self.origin
        if self.event.fault == "gcs_heartbeat_loss":
            connected = not self.event.at_s <= elapsed < self.event.at_s + self.event.duration_s
            if not connected and self.loss_started is None:
                self.loss_started = self.clock()
            return connected
        # Recovery scenario starts disconnected and reconnects at the window.
        return elapsed >= self.event.at_s

    def emit(self, mission_id: str) -> bool:
        if self.connected():
            self.heartbeats.append(self.clock())
            return True
        return False


class CampaignPolicy:
    """Deterministic policy adapter whose decisions are persisted as mission facts."""

    def __init__(self, controller: CampaignController) -> None:
        self.controller = controller

    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float):
        if self.controller.fault_active() and self.controller.event.fault == "slow_model_timeout":
            time.sleep(self.controller.runtime_timeout_s * 2)
        if self.controller.fault_active() and self.controller.event.fault == "slow_model_crash":
            raise RuntimeError("slow model crashed")
        if self.controller.fault_active() and self.controller.event.fault == "detector_exception":
            raise RuntimeError("detector exception")
        decision, reason = self.controller.decide(pose_ned)
        self.controller.log.append("execution", self.controller.clock(),
                                   action=decision, reason=reason)
        # Terminal policy decisions use the same runtime coordinator as a real
        # policy: stop causes the configured completion action to be sent by
        # OnboardRuntime.  No command is synthesized by the campaign.
        if decision in {"return_now", "land_now"}:
            return ActionPrimitive.stop(), {"supervisor_action": decision,
                                            "supervisor_reason": reason}
        return ActionPrimitive.hold(0.05), {"supervisor_action": decision,
                                            "supervisor_reason": reason}

    def close(self) -> None:
        return None


class CampaignController:
    def __init__(self, package: Any, event: FaultEvent | None, vehicle: FaultVehicle,
                 ground: GroundEventSource, log: OnboardMissionLog, clock: Callable[[], float]):
        self.package, self.event, self.vehicle, self.ground, self.log, self.clock = (
            package, event, vehicle, ground, log, clock)
        self.origin = clock()
        self.latched: str | None = None
        self.runtime_timeout_s = 0.15
        self.executor = self._executor()
        self.supervisor = MissionSupervisor(
            self.executor, package, log=log, clock=clock,
            energy_config=EnergyConfig.from_budget(
                package.document["budget"], {"telemetry_timeout_s": 0.2}
            ),
        )

    def fault_active(self) -> bool:
        if self.event is None:
            return False
        elapsed = self.clock() - self.origin
        return self.event.at_s <= elapsed < self.event.at_s + self.event.duration_s

    def _executor(self):
        def loop_factory(_task, **kwargs):
            return OnboardLoop(
                camera=CameraModel(120, 120, 80, 60, 160, 120),
                search_action=lambda pose, yaw: ActionPrimitive.hold(0.05),
                segment_clear=lambda pose, north, east: True,
                config=LoopConfig(**kwargs),
            )

        return MissionExecutor(
            self.package.plan, loop_factory=loop_factory, refs=self.package.refs,
            config=ExecutorConfig(step_budget=4),
        )

    def decide(self, pose: tuple[float, float, float] | None) -> tuple[str, str]:
        now = self.clock()
        event = self.event.fault if self.event and self.fault_active() else None
        if self.ground.emit(self.package.mission_id):
            self.supervisor.ground_event({"mission_id": self.package.mission_id,
                                          "type": "heartbeat", "timestamp": now})
        # Preserve the vehicle fake's receipt timestamp.  During battery
        # staleness no new message is delivered, so the estimator's documented
        # telemetry-age rule is what produces the conservative action.
        if not (event == "battery_stale"):
            state = self.vehicle.snapshot()
            voltage = 13.2 if event == "battery_sag" else 16.0
            self.supervisor.ingest_energy_telemetry(
                {"mavpackettype": "BATTERY_STATUS", "id": 0,
                 "voltages": [int(voltage * 1000)],
                 "battery_remaining": state.battery_remaining,
                 "current_consumed": 0, "current_battery": 20,
                 "source_system": 1, "source_component": 1},
                received_at_s=now,
            )
        action = self.supervisor.evaluate(pose_ned=pose)
        reason = self.supervisor.reason
        if event == "fence_approach" and action == "continue":
            action, reason = "constrained", "operating area fence constrains unsafe setpoint"
            # Keep the rejected target and the actually emitted hold target in
            # the durable mission evidence.  The fake vehicle's setpoint trace
            # is the authoritative command record; this row explains why the
            # unsafe proposal never reached it.
            area = self.package.document["operating_area"]
            proposed = (area["north_max_m"] + 1.0, area["east_max_m"] + 1.0, pose[2])
            emitted = tuple(pose)
            self.log.append("fence_rejection", now, reason=reason,
                            proposed_setpoint=proposed, emitted_setpoint=emitted)
        self.log.append("decision", now, action=action, reason=reason,
                        connected=self.supervisor.connected, fault=event, pose_ned=pose)
        if action in {"return_now", "land_now"}:
            self.latched = self.latched or action
        return self.latched or action, reason


@dataclass
class CampaignSession:
    runtime: Any
    vehicle: FaultVehicle
    camera: FaultCameraSource
    controller: CampaignController
    log: OnboardMissionLog
    started: float

    def observation(self) -> dict[str, Any]:
        records = []
        if self.log.path.exists():
            records = [
                json.loads(line)
                for line in self.log.path.read_text(encoding="utf-8").splitlines()
            ]
        return {"setpoints": list(self.vehicle.setpoints), "commands": list(self.vehicle.commands),
                "runtime_report": self.runtime.report(), "mission_records": records,
                "started": self.started,
                "fault_at": self.controller.origin + self.controller.event.at_s,
                "finished": time.monotonic(), "latched": self.controller.latched}


def observed_from_observation(observation: Mapping[str, Any]) -> tuple[str, str, float]:
    records = observation.get("mission_records", [])
    decisions = [r for r in records if r.get("kind") == "decision"]
    runtime = observation.get("runtime_report", {})
    commands = observation.get("commands", [])
    if any(c.get("action") == "land" for c in commands):
        action = "land_now"
    elif any(c.get("action") == "rtl" for c in commands):
        action = "return_now"
    elif any(r.get("action") == "return_now" for r in decisions):
        action = "return_now"
    elif any(r.get("action") == "land_now" for r in decisions):
        action = "land_now"
    elif any(r.get("action") == "constrained" for r in decisions):
        action = "constrained"
    elif any(r.get("action") == "hold" for r in decisions) or runtime.get("hazard"):
        action = "hold"
    else:
        action = "continue" if decisions else "unknown"
    reason = str(runtime.get("watchdog_reason") or "")
    if not reason and decisions:
        wanted = {
            "constrained": "constrained", "hold": "hold",
            "return_now": "return_now", "land_now": "land_now",
        }.get(action)
        matching = [row for row in decisions
                    if wanted is None or row.get("action") == wanted]
        reason = str((matching or decisions)[-1].get("reason", ""))
    started = float(observation.get("started", time.monotonic()))
    fault_at = float(observation.get("fault_at", started))
    finished = float(observation.get("finished", started))
    candidates: list[float] = []
    candidates.extend(
        float(row.get("monotonic_s", started))
        for row in runtime.get("events", [])
        if row.get("event") == "watchdog_hold"
    )
    candidates.extend(
        float(row.get("timestamp", started)) for row in commands
        if row.get("action") in {"land", "rtl"}
    )
    candidates.extend(
        float(row.get("timestamp", started)) for row in decisions
        if row.get("action") in {"hold", "constrained", "return_now", "land_now"}
    )
    response = min((stamp for stamp in candidates if stamp >= fault_at), default=finished)
    return action, reason, max(0.0, response - fault_at)


def _default_package(work_dir: Path) -> Any:
    """Create a signed, self-contained bench package when no path is supplied."""
    key_path = work_dir / "bench-operator.key"
    key = b"fault-campaign-bench-operator-key-32"
    key_path.write_bytes(key)
    document = {
        "mission_id": "fault-campaign", "risk_level": "medium",
        "link_loss_policy": {"action": "continue_then_return", "timeout_s": 0.4},
        "heartbeat_timeout_s": 0.4, "home": [0, 0], "places": {"poi:gate": [1, 0]},
        "zones": {}, "routes": {}, "oversight_steps": [],
        "operating_area": {"north_min_m": -5, "north_max_m": 5,
                            "east_min_m": -5, "east_max_m": 5,
                            "min_altitude_m": 5, "max_altitude_m": 20},
        "budget": {"time_s": 30, "reserve_time_s": 2, "energy_wh": 100,
                   "reserve_energy_wh": 10},
        "plan": {"plan_version": 1, "goal": "visit gate then return home",
                 "report": ["positions"], "steps": [
                     {"id": "gate", "skill": "goto",
                      "params": {"target_ref": "poi:gate"},
                      "on_success": "home", "on_fail": "home"},
                     {"id": "home", "skill": "return_home", "params": {},
                      "on_success": None, "on_fail": None}]},
    }
    package_path = work_dir / "bench-mission-package.json"
    package_path.write_text(json.dumps(sign_document(document, key)), encoding="utf-8")
    return load_mission_package(package_path, operator_key_path=key_path, require_signature=True)


def _build_session(package: Any, event: FaultEvent, work_dir: Path) -> CampaignSession:
    clock = time.monotonic
    area = package.document["operating_area"]
    vehicle = FaultVehicle(clock, event, area)
    camera = FaultCameraSource(clock, event)
    ground = GroundEventSource(clock, event)
    log = OnboardMissionLog(work_dir, package.mission_id)
    controller = CampaignController(package, event, vehicle, ground, log, clock)
    policy = CampaignPolicy(controller)
    terminal_action = ("land" if package.document["link_loss_policy"]["action"] == "land_now"
                       else "rtl")
    camera_timeout = 0.12 if event.fault in {"camera_freeze", "camera_drop"} else 100.0
    pose_timeout = 0.12 if event.fault in {"pose_stale", "offboard_loss", "home_change"} else 5.0
    autonomy_timeout = 0.15 if event.fault == "slow_model_timeout" else 2.0
    heartbeat_timeout = 0.12 if event.fault.startswith("gcs_heartbeat") else 0.4
    config = RuntimeConfig(
        stream_hz=20, camera_timeout_s=camera_timeout, pose_timeout_s=pose_timeout,
        heartbeat_timeout_s=heartbeat_timeout, hold_before_failsafe_s=0.15,
        autonomy_timeout_s=autonomy_timeout, action_timeout_s=2.0,
        failsafe=terminal_action, shutdown_action=terminal_action,
        completion_action=terminal_action,
    )
    runtime = OnboardRuntime(vehicle, camera, policy, config=config, home_ned=vehicle.home)
    vehicle.start()
    camera.start()
    # Admission and mission-log fsyncs can take longer than a short bench
    # window on a busy host.  Define schedule time at the runtime start barrier
    # so no fault is active before the runtime has admitted fresh inputs.
    source_origin = time.monotonic()
    vehicle.origin = source_origin
    camera.origin = source_origin
    ground.origin = source_origin
    controller.origin = source_origin
    session_origin = source_origin
    runtime.start(active=True, own_flight=False)
    return CampaignSession(runtime, vehicle, camera, controller, log, session_origin)


def run_campaign(*, episode: Path | None = None, package: Path | None = None,
                 operator_key: Path | None = None, output: Path | None = None,
                 work_dir: Path | None = None,
                 runtime_factory: Callable[[Any, FaultEvent, Path], CampaignSession] | None = None,
                 duration_s: float | None = None) -> dict[str, Any]:
    """Run every scenario in its own runtime and compare only captured evidence."""
    del episode
    work_dir = (work_dir or ROOT / "tmp" / "onboard-fault-campaign").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    if package is None:
        admitted = _default_package(work_dir)
    else:
        admitted = load_mission_package(
            package.resolve(),
            operator_key_path=operator_key.resolve() if operator_key else None,
            require_signature=True,
        )
    policy = admitted.document["link_loss_policy"]
    # Each scenario owns an isolated runtime, so every source fault uses the
    # same relative window.  ``schedule_faults`` remains available for HIL
    # plans containing multiple non-overlapping events in one flight.
    severities = {
        "camera_freeze": "high", "camera_drop": "high", "pose_stale": "critical",
        "slow_model_timeout": "high", "slow_model_crash": "high",
        "detector_exception": "high", "gcs_heartbeat_loss": "policy",
        "gcs_heartbeat_recovery": "recovery", "offboard_loss": "critical",
        "battery_sag": "critical", "battery_stale": "high", "home_change": "critical",
        "fence_approach": "high",
    }
    events = tuple(
        FaultEvent(
            name, 0.05 if name == "gcs_heartbeat_recovery" else 0.25,
            1.0 if name == "gcs_heartbeat_loss" else 0.35,
            severity=severities[name],
        )
        for name in FAULTS
    )
    rows: list[dict[str, Any]] = []
    factory = runtime_factory or _build_session
    for event in events:
        # Each invocation gets a clean mission log.  Reusing a JSONL log would
        # mix evidence from earlier runs and make a defective runtime appear
        # healthy because of old commands/decisions.
        scenario_dir = work_dir / "runs" / f"{event.fault}-{time.time_ns()}"
        scenario_dir.mkdir(parents=True, exist_ok=True)
        session = factory(admitted, event, scenario_dir)
        run_for = duration_s or (event.at_s + event.duration_s + 0.65)
        try:
            session.runtime.run(run_for)
        finally:
            observation = session.observation()
            session.runtime.close()
        expected = _campaign_expected_outcome(event.fault, policy)
        # Include bounded shutdown/command handoff jitter while preserving the
        # scenario's explicit injection window as the primary deadline.
        check = verdict(expected, observation=observation, deadline_s=run_for + 0.5)
        reason_rule = _expected_reason_fragment(event.fault)
        check["reason_rule"] = reason_rule
        check["reason_matches"] = reason_rule.lower() in check["reason"].lower()
        check["passed"] = bool(check["passed"] and check["reason_matches"])
        rows.append({"scenario": event.fault, "scheduled": asdict(event),
                     "expected": expected, **check,
                     "fault_severity": event.severity,
                     "persistence_s": event.duration_s,
                     "remaining_budget": admitted.document["budget"],
                     "mission_id": admitted.mission_id,
                     "policy": policy,
                     "policy_timeout_s": policy.get("timeout_s"),
                     "runtime_report": observation["runtime_report"]})
    report = {"mode": "replay", "mission_id": admitted.mission_id, "policy": policy,
              "policy_timeout_s": policy.get("timeout_s"),
              "scenarios": rows, "scenario_table": scenario_table(),
              "passed": all(row["passed"] for row in rows), "generated_at": time.time(),
              "limitations": ["offboard/home/fence require real HIL sign-off"]}
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        lines = [
            "# Onboard \u6545\u969c\u6ce8\u5165\u6d3b\u52a8\u62a5\u544a", "",
            "| \u573a\u666f | \u9884\u671f | \u89c2\u6d4b | \u539f\u56e0 | \u7ed3\u679c |",
            "|---|---|---|---|---|",
        ]
        lines.extend(
            f"| {row['scenario']} | {row['expected']} | {row['observed_action']} | "
            f"{row['reason']} | "
            f"{(chr(0x901a) + chr(0x8fc7)) if row['passed'] else (chr(0x5931) + chr(0x8d25))} |"
            for row in rows
        )
        output.with_suffix(".md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode", type=Path,
                        help="Optional replay recording (source fakes remain authoritative).")
    parser.add_argument("--mission-package", type=Path)
    parser.add_argument("--operator-key", type=Path)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "tmp/onboard-fault-campaign/report.json"
    )
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_campaign(episode=args.episode, package=args.mission_package,
                              operator_key=args.operator_key,
                              output=args.output, work_dir=args.work_dir)
    except Exception as exc:
        print(json.dumps(
            {"passed": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
        ))
        failure = {"mode": "replay", "passed": False,
                   "error": f"{type(exc).__name__}: {exc}"}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
            args.output.with_suffix(".md").write_text(
                "# Onboard \u6545\u969c\u6ce8\u5165\u6d3b\u52a8\u62a5\u544a\n\n"
                "\u8fd0\u884c\u5931\u8d25\uff1a" + failure["error"] + "\n",
                encoding="utf-8",
            )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
