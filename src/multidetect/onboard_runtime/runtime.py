"""Keep PX4 setpoints independent of camera capture and slow policy calls."""

from __future__ import annotations

import inspect
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from multidetect.sim_teacher.primitives import ActionPrimitive, primitive_to_ned

from .camera import CameraFrame, CameraSource
from .vehicle import PositionSetpoint, VehicleInterface, VehicleState


class Policy(Protocol):
    def act(self, observation: dict[str, Any], *, pose_ned, yaw_deg: float): ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RuntimeConfig:
    stream_hz: float = 20.0
    camera_timeout_s: float = 0.75
    pose_timeout_s: float = 0.75
    heartbeat_timeout_s: float = 3.0
    hold_before_failsafe_s: float = 3.0
    autonomy_timeout_s: float = 15.0
    action_timeout_s: float = 20.0
    failsafe: str = "land"
    shutdown_action: str = "land"
    completion_action: str = "land"
    position_tolerance_m: float = 0.5
    yaw_tolerance_deg: float = 8.0
    speed_tolerance_mps: float = 0.5
    shutdown_timeout_s: float = 5.0
    terminal_retry_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        numeric = (
            self.stream_hz,
            self.camera_timeout_s,
            self.pose_timeout_s,
            self.heartbeat_timeout_s,
            self.hold_before_failsafe_s,
            self.autonomy_timeout_s,
            self.action_timeout_s,
            self.position_tolerance_m,
            self.yaw_tolerance_deg,
            self.speed_tolerance_mps,
            self.shutdown_timeout_s,
            self.terminal_retry_timeout_s,
        )
        if any(not math.isfinite(v) or v <= 0 for v in numeric):
            raise ValueError("runtime timing and tolerance values must be positive and finite")
        if self.stream_hz < 10:
            raise ValueError("stream_hz must be at least 10 Hz")
        if self.failsafe not in {"hold", "land", "rtl"}:
            raise ValueError("failsafe must be hold, land or rtl")
        if {self.shutdown_action, self.completion_action} - {"land", "rtl"}:
            raise ValueError("shutdown and completion actions must be land or rtl")


def hold_setpoint(state: VehicleState) -> PositionSetpoint:
    if state.position_ned is None or state.attitude_deg is None:
        raise ValueError("a hold setpoint requires position and attitude")
    return PositionSetpoint(*state.position_ned, state.attitude_deg[2])


def observation_for(frame: CameraFrame, state: VehicleState) -> dict[str, Any]:
    """Adapt sensor values to the same policy input used by the SIM collector."""
    if state.position_ned is None or state.attitude_deg is None:
        raise ValueError("policy observation requires position and attitude")
    roll, pitch, yaw = state.attitude_deg
    return {
        "rgb": frame.rgb,
        "depth": None,
        "camera_info": dict(frame.calibration.camera_info),
        "camera_extrinsics": dict(frame.calibration.extrinsics),
        "pose": {"local_ned_m": list(state.position_ned)},
        "attitude": {"roll_deg": roll, "pitch_deg": pitch, "yaw_deg": yaw},
        "velocity": state.velocity_ned,
        "home_ned_m": state.home_ned,
        "landed": state.landed,
        "battery_remaining": state.battery_remaining,
        "sensor_status": "live",
        "frame_received_monotonic_ns": int(frame.timestamp_monotonic * 1e9),
        "pose_received_monotonic_ns": int((state.pose_monotonic or 0) * 1e9),
    }


class SetpointPump:
    """One bounded writer, with constant-memory timing statistics."""

    def __init__(
        self,
        vehicle: VehicleInterface,
        initial: PositionSetpoint,
        *,
        rate_hz: float = 20,
        before_send: Callable[[], None] | None = None,
    ) -> None:
        if not math.isfinite(rate_hz) or rate_hz < 10:
            raise ValueError("setpoint rate must be at least 10 Hz")
        self.vehicle, self.rate_hz, self.before_send = vehicle, rate_hz, before_send
        self._target = initial
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self.count = 0
        self.first_sent: float | None = None
        self.last_sent: float | None = None
        self.max_gap_s = 0.0

    def set_target(self, target: PositionSetpoint) -> None:
        with self._lock:
            self._target = target

    @property
    def target(self) -> PositionSetpoint:
        with self._lock:
            return self._target

    def _run(self) -> None:
        tick = time.monotonic()
        try:
            while not self._stop.is_set():
                if self.before_send:
                    self.before_send()
                self.vehicle.send_setpoint(self.target)
                sent = time.monotonic()
                with self._lock:
                    if self.last_sent is not None:
                        self.max_gap_s = max(self.max_gap_s, sent - self.last_sent)
                    if self.first_sent is None:
                        self.first_sent = sent
                    self.last_sent = sent
                    self.count += 1
                tick += 1.0 / self.rate_hz
                # Skip missed periods instead of flooding the flight controller.
                if tick <= sent:
                    tick = sent + 1.0 / self.rate_hz
                self._stop.wait(max(0, tick - time.monotonic()))
        except BaseException as exc:
            self.error = exc
            self._stop.set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("setpoint pump is single-use")
        self._thread = threading.Thread(target=self._run, name="onboard-setpoints", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        if self._thread:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                raise RuntimeError("setpoint transport did not stop within two seconds")

    def request_stop(self) -> None:
        """Request an immediate stop without joining from a coordinator lock."""
        self._stop.set()

    def report(self) -> dict[str, Any]:
        with self._lock:
            span = (self.last_sent or 0) - (self.first_sent or 0)
            return {
                "sent_count": self.count,
                "rate_hz": (self.count - 1) / span if span > 0 else 0.0,
                "max_gap_s": self.max_gap_s,
                "configured_rate_hz": self.rate_hz,
                "error": str(self.error) if self.error else None,
            }


class OnboardRuntime:
    """One policy job at a time; only the coordinator can accept its actions.

    Sensor faults are latched. Even a recovered stream cannot silently resume a
    mission whose policy may already have advanced on an obsolete observation.
    """

    def __init__(
        self,
        vehicle: VehicleInterface,
        camera: CameraSource,
        policy: Policy,
        *,
        config: RuntimeConfig | None = None,
        home_ned: tuple[float, float, float] | None = None,
        event_sink: Callable[[str, float, dict[str, Any]], None] | None = None,
        arbiter: Any | None = None,
        supervisor: Any | None = None,
    ) -> None:
        self.vehicle, self.camera, self.policy = vehicle, camera, policy
        self.config = config or RuntimeConfig()
        self.home_ned = home_ned
        self.event_sink = event_sink
        self.arbiter = arbiter
        self.supervisor = supervisor
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._worker: threading.Thread | None = None
        self._job_started: float | None = None
        self._result: tuple[ActionPrimitive, dict[str, Any], VehicleState] | None = None
        self._policy_error: str | None = None
        self._hazard_since: float | None = None
        self.hazard: str | None = None
        self.failsafe_sent: str | None = None
        self._command_last_sent = 0.0
        self._handoff_started = 0.0
        self._command_thread: threading.Thread | None = None
        self._command_error: str | None = None
        self._terminal_started = 0.0
        self._terminal_attempts = 0
        self._terminal_fallback_logged = False
        self._event_sink_failures = 0
        self._event_sink_failure_reported = False
        self._event_sink_dropped = 0
        # One worker drains a bounded queue: the sink can never stall or crash the
        # pump/watchdog/coordinator, events keep their order, and no thread piles up.
        self._event_queue: queue.Queue[tuple[str, float, dict[str, Any]] | None] = queue.Queue(
            maxsize=256
        )
        self._event_worker: threading.Thread | None = None
        if self.event_sink is not None:
            self._event_worker = threading.Thread(
                target=self._drain_events, name="onboard-event-sink", daemon=True
            )
            self._event_worker.start()
        self._motion: tuple[ActionPrimitive, float] | None = None
        self._last_frame_timestamp: float | None = None
        self._last_good_state: VehicleState | None = None
        self._hazard_target: PositionSetpoint | None = None
        self._flight_owned = False
        self._active = False
        self._closed = False
        self.steps = 0
        self.pending_ticks = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=128)
        self.pump: SetpointPump | None = None
        self.policy_worker_stopped = True
        self.mission_complete = False

    def _arbitrate_action(
        self,
        action: ActionPrimitive,
        *,
        pose_ned: tuple[float, float, float],
        yaw_deg: float,
        observation: dict[str, Any] | None = None,
    ) -> tuple[ActionPrimitive, dict[str, Any]]:
        """Run the optional final action arbiter without coupling its API.

        The onboard safety envelope has evolved independently of this runtime;
        accepting ``arbitrate``/``apply``/callable implementations keeps the
        pump integration small while allowing a decision record to be attached
        to runtime events.  A malformed arbiter result fails closed to a hold.
        """
        if self.arbiter is None:
            return action, {}
        fn = getattr(self.arbiter, "arbitrate", None)
        if fn is None:
            fn = getattr(self.arbiter, "apply", None)
        if fn is None and callable(self.arbiter):
            fn = self.arbiter
        if fn is None:
            raise TypeError("arbiter must be callable or expose arbitrate/apply")
        # Select the call shape up front; catching TypeError around invocation
        # could execute a stateful arbiter twice when its own body raises.
        try:
            parameters = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "observation" in parameters:
            result = fn(action, pose_ned=pose_ned, yaw_deg=yaw_deg, observation=observation)
        elif parameters and any(
            p.kind is inspect.Parameter.POSITIONAL_ONLY for p in parameters.values()
        ):
            result = fn(action, pose_ned, yaw_deg)
        else:
            result = fn(action, pose_ned=pose_ned, yaw_deg=yaw_deg)
        info: dict[str, Any] = {}
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[0], ActionPrimitive)
        ):
            action, metadata = result
            if isinstance(metadata, dict):
                info = metadata
        elif isinstance(result, ActionPrimitive):
            action = result
        elif hasattr(result, "action") and isinstance(result.action, ActionPrimitive):
            action = result.action
            metadata = result.info() if callable(getattr(result, "info", None)) else None
            if isinstance(metadata, dict):
                info = metadata
        else:
            raise TypeError("arbiter must return ActionPrimitive or (ActionPrimitive, info)")
        return action, info

    def _event(self, name: str, **details: Any) -> None:
        timestamp = time.monotonic()
        self.events.append({"event": name, "monotonic_s": timestamp, **details})
        if self.event_sink is not None:
            try:
                self._event_queue.put_nowait((name, timestamp, dict(details)))
            except queue.Full:
                self._event_sink_dropped += 1

    def _drain_events(self) -> None:
        while True:
            item = self._event_queue.get()
            if item is None:  # close(): every earlier event has been delivered
                return
            self._deliver_event(*item)

    def _stop_event_worker(self, errors: list[str]) -> None:
        if self._event_worker is None:
            return
        try:
            self._event_queue.put(None, timeout=1.0)
        except queue.Full:
            errors.append("event sink queue full at shutdown")
            return
        self._event_worker.join(timeout=2.0)
        if self._event_worker.is_alive():
            errors.append("event sink did not drain within two seconds")

    def _deliver_event(self, name: str, timestamp: float, details: dict[str, Any]) -> None:
        try:
            self.event_sink(name, timestamp, details)
        except Exception as exc:
            with self._lock:
                self._event_sink_failures += 1
                if not self._event_sink_failure_reported:
                    self._event_sink_failure_reported = True
                    self.events.append(
                        {
                            "event": "event_sink_failure",
                            "monotonic_s": time.monotonic(),
                            "reason": str(exc),
                        }
                    )

    def _set_target_checked(
        self,
        target: PositionSetpoint,
        purpose: str,
        *,
        state: VehicleState | None = None,
        already_enveloped: bool = False,
    ) -> PositionSetpoint:
        """Publish every target through one boundary check.

        Policy labels containing ``safety`` mean the primitive was already
        arbitrated; this helper only checks the resulting absolute setpoint and
        never clips it a second time. Holds are always accepted so a violated
        current position can still be held safely.
        """
        if self.pump is None:
            raise RuntimeError("runtime has not started")
        if purpose not in {
            "policy_action",
            "takeoff",
            "hazard_hold",
            "policy_hold",
            "close_hold",
            "terminal_hold",
        }:
            raise ValueError(f"unknown setpoint purpose: {purpose}")
        check_state = state
        if check_state is None:
            try:
                check_state = self.vehicle.snapshot()
            except Exception:
                check_state = None
        envelope = self.arbiter
        pending_reports: list[str] = []

        def report(reason: str) -> None:
            pending_reports.append(reason)

        def current_violation() -> None:
            if envelope is None or check_state is None or check_state.position_ned is None:
                return
            config = getattr(envelope, "config", None)
            home = getattr(envelope, "home_ned", None) or self.home_ned
            if config is None or home is None:
                return
            altitude = float(home[2]) - float(check_state.position_ned[2])
            if (
                getattr(config, "max_altitude_m", None) is not None
                and altitude > float(config.max_altitude_m) + 1e-9
            ):
                report("max_altitude")
            if altitude < float(getattr(config, "min_altitude_m", 0.0)) - 1e-9:
                report("min_altitude")

        if purpose in {"policy_hold", "hazard_hold", "close_hold", "terminal_hold"}:
            # Holds are never refused, including while already outside the
            # envelope.  The violation is reported for supervisor escalation.
            current_violation()
        elif envelope is not None and purpose == "takeoff":
            config = getattr(envelope, "config", None)
            home = self.home_ned or getattr(envelope, "home_ned", None)
            if home is None:
                raise RuntimeError("takeoff envelope requires known home")
            max_alt = getattr(config, "max_altitude_m", None)
            # NED: higher altitude = more negative down.  Clip only a target above
            # the ceiling; a lower requested takeoff altitude is kept as is.
            if max_alt is not None and target.down_m < float(home[2]) - float(max_alt):
                target = PositionSetpoint(
                    target.north_m, target.east_m, float(home[2]) - float(max_alt), target.yaw_deg
                )
        if envelope is not None and purpose == "policy_action" and not already_enveloped:
            config = getattr(envelope, "config", None)
            home = getattr(envelope, "home_ned", None) or self.home_ned
            if config is not None:
                if getattr(config, "max_altitude_m", None) is not None and home is not None:
                    max_down = float(home[2]) - float(config.max_altitude_m)
                    if target.down_m < max_down - 1e-9:
                        report("max_altitude")
                        target = hold_setpoint(state or self.vehicle.snapshot())
                if getattr(config, "min_altitude_m", 0.0) > 0 and home is not None:
                    min_down = float(home[2]) - float(config.min_altitude_m)
                    if target.down_m > min_down + 1e-9:
                        report("min_altitude")
                        target = hold_setpoint(state or self.vehicle.snapshot())
        self.pump.set_target(target)
        report_failures = 0
        for reason in dict.fromkeys(pending_reports):
            try:
                supervisor = self.supervisor or getattr(envelope, "supervisor", None)
                if supervisor is not None and hasattr(supervisor, "report_envelope_violation"):
                    supervisor.report_envelope_violation(reason)
                self._event("envelope_setpoint_violation", purpose=purpose, reason=reason)
            except Exception as exc:
                report_failures += 1
                if not getattr(self, "_envelope_report_failure_reported", False):
                    self._envelope_report_failure_reported = True
                    self._event("envelope_report_failure", reason=str(exc))
        if report_failures:
            self._envelope_report_failures = (
                getattr(self, "_envelope_report_failures", 0) + report_failures
            )
        return target

    def _freshness_fault(
        self,
        state: VehicleState,
        frame: CameraFrame | None,
        now: float,
    ) -> str | None:
        for name, stamp, limit in (
            ("pose", state.pose_monotonic, self.config.pose_timeout_s),
            ("attitude", state.attitude_monotonic, self.config.pose_timeout_s),
            ("heartbeat", state.heartbeat_monotonic, self.config.heartbeat_timeout_s),
            ("camera", frame.timestamp_monotonic if frame else None, self.config.camera_timeout_s),
        ):
            if stamp is None or not math.isfinite(stamp) or not 0 <= now - stamp <= limit:
                return f"stale_{name}"
        if state.position_ned is None or state.attitude_deg is None:
            return "missing_pose"
        if not all(math.isfinite(v) for v in (*state.position_ned, *state.attitude_deg)):
            return "invalid_pose"
        return None

    def _latch(self, reason: str, now: float) -> None:
        with self._lock:
            if self.hazard is not None:
                return
            self.hazard, self._hazard_since = reason, now
            self._result = None
            self._motion = None
            if self.pump and self._last_good_state:
                self._hazard_target = hold_setpoint(self._last_good_state)
                self._set_target_checked(
                    self._hazard_target, "hazard_hold", state=self._last_good_state
                )
            self._event("watchdog_hold", reason=reason)

    def _guard(self) -> None:
        """Runs in the pump, independent of coordinator or inference delays."""
        now = time.monotonic()
        try:
            state, frame = self.vehicle.snapshot(), self.camera.latest()
            fault = self._freshness_fault(state, frame, now)
            with self._lock:
                if (
                    state.pose_monotonic is not None
                    and state.attitude_monotonic is not None
                    and 0 <= now - state.pose_monotonic <= self.config.pose_timeout_s
                    and 0 <= now - state.attitude_monotonic <= self.config.pose_timeout_s
                    and state.position_ned is not None
                    and state.attitude_deg is not None
                    and all(math.isfinite(v) for v in (*state.position_ned, *state.attitude_deg))
                ):
                    self._last_good_state = state
                if self._closed:
                    return
                if fault:
                    self._latch(fault, now)
                elif self._policy_error:
                    self._latch(self._policy_error, now)
                elif (
                    self._job_started is not None
                    and now - self._job_started > self.config.autonomy_timeout_s
                ):
                    self._latch("autonomy_timeout", now)
                elif self._active and not self.failsafe_sent:
                    if not state.armed:
                        self._latch("unexpected_disarm", now)
                    elif state.mode != "OFFBOARD":
                        self._latch("offboard_exited", now)
                    elif self.home_ned is not None and (
                        state.home_ned is None or math.dist(state.home_ned, self.home_ned) > 0.5
                    ):
                        self._latch("home_changed", now)
                if self.hazard and self._hazard_target and self.pump:
                    self._set_target_checked(self._hazard_target, "hazard_hold", state=state)
        except Exception as exc:
            self._latch(f"sensor_error: {exc}", now)

    def start(self, *, active: bool = True, own_flight: bool = True) -> None:
        if self.pump is not None:
            raise RuntimeError("runtime is single-use")
        state, frame = self.vehicle.snapshot(), self.camera.latest()
        fault = self._freshness_fault(state, frame, time.monotonic())
        if fault:
            raise RuntimeError(f"runtime inputs are not ready: {fault}")
        if active and (state.armed is not True or state.mode != "OFFBOARD"):
            raise RuntimeError("active runtime requires confirmed armed OFFBOARD telemetry")
        self._last_good_state = state
        self._active = active
        self._flight_owned = active and own_flight
        self.pump = SetpointPump(
            self.vehicle,
            hold_setpoint(state),
            rate_hz=self.config.stream_hz,
            before_send=self._guard,
        )
        self.pump.start()

    def _check_running(self) -> None:
        if self.pump is None:
            raise RuntimeError("runtime has not started")
        if self.pump.error:
            raise RuntimeError(f"setpoint transport failed: {self.pump.error}")

    def arm_and_takeoff(self, altitude_m: float, *, timeout_s: float = 45.0) -> None:
        """Prime OFFBOARD, confirm mode/arm telemetry, then climb from home."""
        self._check_running()
        if not math.isfinite(altitude_m) or altitude_m <= 0:
            raise ValueError("takeoff altitude must be positive and finite")
        state = self.vehicle.snapshot()
        if state.home_ned is None or state.landed is not True or state.armed is not False:
            raise RuntimeError("takeoff requires known home and a disarmed, landed vehicle")
        bound_home = self.home_ned or state.home_ned
        if math.dist(state.home_ned, bound_home) > 0.5:
            raise RuntimeError("PX4 home changed before arming")
        self.home_ned = bound_home
        until = time.monotonic() + 1.2
        while time.monotonic() < until:
            self._check_running()
            if self.hazard or self._stopping.wait(0.02):
                raise RuntimeError(f"takeoff interrupted: {self.hazard or 'shutdown'}")
        self._flight_owned = True
        deadline = time.monotonic() + timeout_s
        last_request = 0.0
        while True:
            self._check_running()
            state = self.vehicle.snapshot()
            if self.hazard or self._stopping.is_set() or time.monotonic() >= deadline:
                raise RuntimeError(f"arm/OFFBOARD confirmation failed: {self.hazard}")
            if state.armed is True and state.mode == "OFFBOARD":
                break
            if time.monotonic() - last_request >= 1.0:
                self.vehicle.set_mode("OFFBOARD")
                self.vehicle.arm()
                last_request = time.monotonic()
            self._stopping.wait(0.02)
        with self._lock:
            if self.hazard or self._stopping.is_set():
                raise RuntimeError("takeoff interrupted before climb target publication")
            if state.home_ned is None or math.dist(state.home_ned, bound_home) > 0.5:
                raise RuntimeError("PX4 home changed during arming")
            self._active = True
            target = PositionSetpoint(
                *state.position_ned[:2], bound_home[2] - altitude_m, state.attitude_deg[2]
            )
            self._set_target_checked(target, "takeoff", state=state)
        while not self._arrived(self.vehicle.snapshot(), target):
            self.poll(allow_policy=False)
            if self.hazard or self._stopping.is_set() or time.monotonic() >= deadline:
                raise RuntimeError(f"takeoff did not complete: {self.hazard}")
            self._stopping.wait(0.02)
        self._event("takeoff_complete")

    def _arrived(self, state: VehicleState, target: PositionSetpoint) -> bool:
        if state.position_ned is None or state.attitude_deg is None or state.velocity_ned is None:
            return False
        error = math.dist(state.position_ned, (target.north_m, target.east_m, target.down_m))
        yaw_error = (state.attitude_deg[2] - target.yaw_deg + 180) % 360 - 180
        speed = math.sqrt(sum(v * v for v in state.velocity_ned))
        return (
            error <= self.config.position_tolerance_m
            and abs(yaw_error) <= self.config.yaw_tolerance_deg
            and speed <= self.config.speed_tolerance_mps
        )

    def _launch_policy(self, frame: CameraFrame, state: VehicleState) -> None:
        self._set_target_checked(hold_setpoint(state), "policy_hold", state=state)
        observation = observation_for(frame, state)
        self._job_started = time.monotonic()
        started = self._job_started
        self._last_frame_timestamp = frame.timestamp_monotonic

        def work() -> None:
            try:
                action, labels = self.policy.act(
                    observation,
                    pose_ned=state.position_ned,
                    yaw_deg=state.attitude_deg[2],
                )
                with self._lock:
                    if time.monotonic() - started > self.config.autonomy_timeout_s:
                        self._policy_error = "autonomy_timeout"
                    elif not self._stopping.is_set() and self.hazard is None:
                        self._result = action, labels, state
            except Exception as exc:
                with self._lock:
                    self._policy_error = f"policy_error: {exc}"
            finally:
                with self._lock:
                    self._job_started = None

        self._worker = threading.Thread(target=work, name="onboard-autonomy", daemon=True)
        self._worker.start()

    def _command(self, action: str) -> None:
        if action != self.failsafe_sent:
            self._handoff_started = time.monotonic()
            self._terminal_started = self._handoff_started
            self._terminal_attempts = 0
            self._terminal_fallback_logged = False
        self.failsafe_sent = action
        self._command_last_sent = time.monotonic()
        self._terminal_attempts += 1
        if self.pump is not None and self._last_good_state is not None:
            # Keep a fresh hold stream until telemetry confirms the mode change.
            self._set_target_checked(
                hold_setpoint(self._last_good_state), "terminal_hold", state=self._last_good_state
            )
        self._event("flight_handoff", action=action)
        if action == "hold":
            return

        if self._command_thread is not None and self._command_thread.is_alive():
            # The previous MAVLink command is still blocked: do not pile up threads.
            # The hold stream above keeps PX4 fed; the retry happens once it returns.
            self._event("handoff_command_blocked", action=action)
            return

        def send() -> None:
            try:
                if action == "land":
                    self.vehicle.land()
                else:
                    self.vehicle.rtl()
            except Exception as exc:
                self._command_error = str(exc)

        self._command_thread = threading.Thread(target=send, name="onboard-handoff", daemon=True)
        self._command_thread.start()

    def _handoff_confirmed(self, state: VehicleState) -> bool:
        if self.failsafe_sent not in {"land", "rtl"}:
            return False
        stamp = state.heartbeat_monotonic
        if (
            stamp is None
            or stamp < self._handoff_started
            or time.monotonic() - stamp > self.config.heartbeat_timeout_s
        ):
            return False
        return (
            state.armed is False
            or state.mode == self.failsafe_sent.upper()
            or state.mode == f"AUTO.{self.failsafe_sent.upper()}"
        )

    def poll(self, *, allow_policy: bool = True) -> None:
        self._check_running()
        with self._lock:
            now = time.monotonic()
            if self._stopping.is_set():
                return
            if self.hazard:
                self._result = None
                confirmed = False
                if now - self._hazard_since >= self.config.hold_before_failsafe_s:
                    try:
                        confirmed = self._handoff_confirmed(self.vehicle.snapshot())
                    except Exception:
                        confirmed = False
                if not self.failsafe_sent or (
                    not confirmed and now - self._command_last_sent >= 1.0
                ):
                    self._command(self.config.failsafe)
                return
            if self.failsafe_sent in {"land", "rtl"}:
                self._event(
                    "terminal_state",
                    action=self.failsafe_sent,
                    reason=self.hazard or "terminal handoff latched",
                )
                try:
                    confirmed = self._handoff_confirmed(self.vehicle.snapshot())
                except Exception:
                    confirmed = False
                if confirmed:
                    if self.pump:
                        self.pump.request_stop()
                    self._event("terminal_confirmed", action=self.failsafe_sent)
                elif now - self._command_last_sent >= 1:
                    if (
                        self.failsafe_sent == "rtl"
                        and now - self._terminal_started >= self.config.terminal_retry_timeout_s
                    ):
                        self._event(
                            "terminal_fallback",
                            from_action="rtl",
                            action="land",
                            attempts=self._terminal_attempts,
                        )
                        self._command("land")
                    elif (
                        self.failsafe_sent == "land"
                        and now - self._terminal_started >= self.config.terminal_retry_timeout_s
                    ):
                        if not self._terminal_fallback_logged:
                            self._terminal_fallback_logged = True
                            self._event(
                                "terminal_fallback_exhausted",
                                action="land",
                                attempts=self._terminal_attempts,
                            )
                    else:
                        self._command(self.failsafe_sent)
                return
            if self.failsafe_sent or not self._active or not allow_policy:
                return
            try:
                state, frame = self.vehicle.snapshot(), self.camera.latest()
            except Exception as exc:
                self._latch(f"sensor_error: {exc}", now)
                return
            fault = self._freshness_fault(state, frame, now)
            if fault:
                self._latch(fault, now)
                return
            if self._worker and self._worker.is_alive():
                self.pending_ticks += 1
                return
            if self._policy_error:
                self._latch(self._policy_error, now)
                return
            if self._result:
                action, _labels, source_state = self._result
                self._result = None
                if isinstance(_labels, dict) and "safety" in _labels:
                    envelope_info = _labels["safety"]
                else:
                    action, envelope_info = self._arbitrate_action(
                        action,
                        pose_ned=tuple(source_state.position_ned),
                        yaw_deg=source_state.attitude_deg[2],
                    )
                self.steps += 1
                self._event("autonomy_action", action=action.to_dict(), envelope=envelope_info)
                if action.kind == "stop":
                    self.mission_complete = True
                    self._command(self.config.completion_action)
                    return
                if action.kind == "hold":
                    target = hold_setpoint(state)
                else:
                    n, e, d, yaw = primitive_to_ned(action, source_state.attitude_deg[2])
                    origin = source_state.position_ned
                    target = PositionSetpoint(
                        origin[0] + n,
                        origin[1] + e,
                        origin[2] + d,
                        source_state.attitude_deg[2] + yaw,
                    )
                self._set_target_checked(
                    target,
                    "policy_action",
                    state=state,
                    already_enveloped=isinstance(_labels, dict) and "safety" in _labels,
                )
                self._motion = action, now
            if self._motion:
                action, started = self._motion
                complete = (
                    now - started >= action.seconds
                    if action.kind == "hold"
                    else self._arrived(state, self.pump.target)
                )
                if not complete:
                    if now - started >= self.config.action_timeout_s:
                        self._latch("action_timeout", now)
                    return
                self._motion = None
            if frame is not None and frame.timestamp_monotonic != self._last_frame_timestamp:
                self._launch_policy(frame, state)

    def run(self, duration_s: float | None = None) -> dict[str, Any]:
        if duration_s is not None and (not math.isfinite(duration_s) or duration_s <= 0):
            raise ValueError("duration_s must be positive and finite")
        started = time.monotonic()
        while not self._stopping.is_set():
            self.poll()
            try:
                state = self.vehicle.snapshot()
            except Exception:
                state = None
            if (
                self.failsafe_sent
                and self.failsafe_sent != "hold"
                and state is not None
                and state.landed is True
                and state.armed is False
            ):
                break
            if duration_s is not None and time.monotonic() - started >= duration_s:
                break
            self._stopping.wait(0.01)
        return self.report()

    def request_stop(self) -> None:
        self._stopping.set()

    @property
    def owns_flight(self) -> bool:
        return self._flight_owned

    @property
    def stop_requested(self) -> bool:
        return self._stopping.is_set()

    def fail(self, reason: str) -> None:
        """Enter the same hold/failsafe path for startup or external faults."""
        self._latch(reason, time.monotonic())

    def close(self) -> None:
        if self._closed:
            return
        self._stopping.set()
        with self._lock:
            self._closed = True
            self._result = None
            if self.pump and self._last_good_state:
                self._set_target_checked(
                    hold_setpoint(self._last_good_state), "close_hold", state=self._last_good_state
                )
        errors = []
        try:
            if self.pump and self._flight_owned:
                action = (
                    self.failsafe_sent
                    if self.failsafe_sent in {"land", "rtl"}
                    else self.config.shutdown_action
                )
                self._command(action)
                until = time.monotonic() + self.config.shutdown_timeout_s
                while not self._handoff_confirmed(self.vehicle.snapshot()):
                    if time.monotonic() >= until:
                        raise RuntimeError("flight controller did not confirm shutdown handoff")
                    if time.monotonic() - self._command_last_sent >= 1.0:
                        self._command(action)
                    time.sleep(0.02)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            # Some model clients only cancel blocking HTTP by closing their process.
            def close_policy() -> None:
                try:
                    self.policy.close()
                except Exception as exc:
                    errors.append(f"policy close: {exc}")

            closer = threading.Thread(target=close_policy, name="onboard-model-close", daemon=True)
            closer.start()
            closer.join(timeout=2)
            if closer.is_alive():
                errors.append("policy backend did not close within two seconds")
            if self._worker:
                self._worker.join(timeout=2)
                self.policy_worker_stopped = not self._worker.is_alive()
                if not self.policy_worker_stopped:
                    errors.append("autonomy worker did not stop within two seconds")
            for close in (
                self.pump.stop if self.pump else None,
                self.camera.close,
                self.vehicle.close,
            ):
                if close:
                    try:
                        close()
                    except Exception as exc:
                        errors.append(str(exc))
            if self._command_thread:
                self._command_thread.join(timeout=2)
                if self._command_thread.is_alive():
                    errors.append("flight command transport did not stop")
            self._stop_event_worker(errors)
        if errors:
            raise RuntimeError("; ".join(errors))

    def report(self) -> dict[str, Any]:
        return {
            "setpoints": self.pump.report() if self.pump else {},
            "autonomy_steps": self.steps,
            "pending_poll_count": self.pending_ticks,
            "watchdog_reason": self.hazard,
            "failsafe_sent": self.failsafe_sent,
            "command_error": self._command_error,
            "mission_complete": self.mission_complete,
            "policy_worker_stopped": self.policy_worker_stopped,
            "events": list(self.events),
            "event_sink_failures": self._event_sink_failures,
            "event_sink_dropped": self._event_sink_dropped,
        }
