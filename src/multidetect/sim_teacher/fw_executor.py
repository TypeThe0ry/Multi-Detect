"""Fixed-wing and VTOL SIM-teacher primitive executor.

The existing :mod:`executor` remains the multirotor OFFBOARD implementation.
This adapter uses high-level PX4 guided commands for airframes that cannot
hover: translations and turns are forward reposition commands, and holds are
orbits. A fake vehicle can implement the same small methods and record a
deterministic command sequence in tests.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..onboard_runtime.fw_vtol_commands import (
    CommandSpec,
    do_land_start,
    do_orbit,
    do_reposition,
    do_vtol_transition,
    local_ned_to_global,
    nav_land,
    nav_takeoff,
    nav_vtol_takeoff,
)
from .executor import ExecutionResult, execute_gimbal_action
from .primitives import ActionPrimitive, CompletionCriterion, body_to_ned

if TYPE_CHECKING:  # motion_intent imports sim_teacher (via safety_envelope); avoid the cycle
    from multidetect.autonomy.motion_intent import MotionCommand


class FixedWingVtolExecutor:
    """Map teacher primitives to fixed-wing/VTOL PX4 command methods."""

    def __init__(
        self,
        vehicle: Any,
        *,
        airframe: str = "fixed_wing",
        takeoff_method: str = "runway",
        landing_method: str = "runway",
        altitude_m: float = 30.0,
        loiter_radius_m: float = 80.0,
        ground_speed_mps: float = 15.0,
        criterion: CompletionCriterion | None = None,
        command_timeout_s: float | None = None,
        position: Callable[[], tuple[float, float, float]] | None = None,
        yaw_deg: Callable[[], float] | None = None,
        wait_for_state: Callable[[str, float], Any] | None = None,
        land_mission: Callable[..., Any] | None = None,
        geo_reference: Callable[[], tuple[Any, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        transition_after_takeoff: bool = True,
        airframe_profile: Any = None,
        telemetry_reader: Any = None,
        gimbal_timeout_s: float = 15.0,
        gimbal_tolerance_deg: float = 4.0,
        gimbal_yaw_correction_deg: float = 0.0,
    ) -> None:
        normalized = airframe.strip().lower().replace("-", "_")
        # Profile IDs are intentionally accepted here so the collector can
        # pass an airframe profile without a second mode translation layer.
        if normalized not in {"fixed_wing", "fw", "vtol"} and not any(
            token in normalized for token in ("plane", "cessna", "vtol", "tiltrotor", "tailsitter")
        ):
            raise ValueError("airframe must identify a fixed-wing or VTOL aircraft")
        if altitude_m <= 0 or loiter_radius_m <= 0 or ground_speed_mps <= 0:
            raise ValueError("altitude, loiter radius and ground speed must be positive")
        self.vehicle = vehicle
        self.airframe = (
            "vtol"
            if (
                normalized == "vtol"
                or "vtol" in normalized
                or "tiltrotor" in normalized
                or "tailsitter" in normalized
            )
            else "fixed_wing"
        )
        self.mode = "FW"
        # A VTOL whose mission flies in hover (MC) stays there after the climb
        # instead of a forward transition the mission would immediately undo.
        self.transition_after_takeoff = bool(transition_after_takeoff)
        self.takeoff_method = takeoff_method
        self.landing_method = landing_method
        self.altitude_m = float(altitude_m)
        self.loiter_radius_m = float(loiter_radius_m)
        self.ground_speed_mps = float(ground_speed_mps)
        self.criterion = criterion or CompletionCriterion()
        if command_timeout_s is not None and (
            not isinstance(command_timeout_s, (int, float))
            or not math.isfinite(float(command_timeout_s))
            or float(command_timeout_s) <= 0
        ):
            raise ValueError("command_timeout_s must be positive and finite")
        self.command_timeout_s = (
            float(command_timeout_s)
            if command_timeout_s is not None
            else min(8.0, self.criterion.timeout_s)
        )
        self._position = position or (lambda: (0.0, 0.0, -self.altitude_m))
        self._yaw = yaw_deg or (lambda: 0.0)
        self._wait_for_state = wait_for_state
        self._land_mission = land_mission
        # () -> (home (lat, lon, amsl), home local NED); converts this executor's
        # local-NED targets into the WGS84/AMSL positions PX4 commands carry.
        self._geo_reference = geo_reference
        self._clock = clock
        self._commanded = tuple(float(v) for v in self._position())
        self._commanded_yaw = float(self._yaw())
        self._armed = False
        self._loiter_captured = False
        self.airframe_profile = airframe_profile
        self.telemetry_reader = telemetry_reader
        self.gimbal_timeout_s = float(gimbal_timeout_s)
        self.gimbal_tolerance_deg = float(gimbal_tolerance_deg)
        self.gimbal_yaw_correction_deg = float(gimbal_yaw_correction_deg)

    @staticmethod
    def _ok(result: Any) -> bool:
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        value = getattr(result, "ok", None)
        if value is not None:
            return bool(value)
        return getattr(result, "status", "") in {"accepted", "sent", "success"}

    def sync_commanded_state(self) -> tuple[float, float, float]:
        """Rebase local guided commands on the latest measured pose and heading."""
        position = tuple(float(value) for value in self._position())
        if len(position) != 3 or not all(math.isfinite(value) for value in position):
            raise ValueError("measured position must contain three finite NED coordinates")
        yaw = float(self._yaw())
        if not math.isfinite(yaw):
            raise ValueError("measured heading must be finite")
        self._commanded = position
        self._commanded_yaw = yaw
        return position

    def note_loiter_captured(self) -> None:
        """Keep a telemetry-confirmed loiter active across subsequent hold steps."""
        self._loiter_captured = True

    _GEO_COMMANDS = frozenset({"reposition", "orbit", "loiter_at", "fw_takeoff", "fw_land"})

    def _to_global(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Rewrite local-NED (north, east) + local-up altitude into WGS84 + AMSL."""
        if name not in self._GEO_COMMANDS:
            return args, kwargs
        if self._geo_reference is None:
            if callable(getattr(self.vehicle, name, None)):
                return args, kwargs  # deterministic local-frame fakes
            raise RuntimeError(f"{name} over COMMAND_LONG needs a geodetic home reference")
        home_geodetic, home_ned = self._geo_reference()

        def convert(point: Any, up: float) -> tuple[tuple[float, float], float]:
            lat, lon, amsl = local_ned_to_global(
                (float(point[0]), float(point[1]), -float(up)),
                home_geodetic=home_geodetic,
                home_ned=home_ned,
            )
            return (lat, lon), amsl

        kwargs = dict(kwargs)
        if name == "reposition":
            point, amsl = convert(args[0], args[1])
            return (point, amsl, *args[2:]), kwargs
        if name == "orbit":
            up = kwargs.get("alt")
            point, kwargs["alt"] = convert(args[0], -self._commanded[2] if up is None else up)
            return (point, *args[1:]), kwargs
        if name == "loiter_at":
            point, amsl = convert(args[0], args[2])
            return (point, args[1], amsl, *args[3:]), kwargs
        # Takeoff altitude is the climb height; landing is on the ground (up = 0).
        point = kwargs.get("point", self._commanded[:2])
        kwargs["point"], kwargs["alt"] = convert(point, kwargs.get("alt", 0.0))
        return args, kwargs

    def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = self._to_global(name, args, kwargs)
        method = getattr(self.vehicle, name, None)
        if callable(method):
            return method(*args, **kwargs)
        return self._invoke_command_long(name, *args, **kwargs)

    def _invoke_command_long(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Fallback for a raw pymavlink connection without runtime wrappers."""
        mav = getattr(self.vehicle, "mav", None)
        if callable(mav):
            mav = mav()
        sender = getattr(mav, "command_long_send", None)
        if not callable(sender):
            raise RuntimeError(f"vehicle lacks {name}() and COMMAND_LONG sender")
        timeout_s = float(kwargs.get("timeout_s", self._timeout()))
        if name == "fw_takeoff":
            method = str(args[0] if args else kwargs.get("method", "runway")).strip().lower()
            alt = float(kwargs["alt"])
            point = kwargs.get("point", (0.0, 0.0))
            pitch = float(kwargs.get("pitch_deg", 0.0))
            yaw = float(kwargs.get("yaw_deg", 0.0))
            spec = (
                nav_vtol_takeoff(alt=alt, pitch_deg=pitch, yaw_deg=yaw, point=point)
                if method == "vtol"
                else nav_takeoff(alt=alt, pitch_deg=pitch, yaw_deg=yaw, point=point)
            )
            return self._send_spec(mav, sender, spec, timeout_s, receiver_owner=self.vehicle)
        if name == "vtol_transition":
            target = args[0] if args else kwargs.get("to", "FW")
            return self._send_spec(
                mav, sender, do_vtol_transition(target), timeout_s, receiver_owner=self.vehicle
            )
        if name == "reposition":
            point = args[0]
            alt = float(args[1])
            speed = float(args[2]) if len(args) > 2 else self.ground_speed_mps
            return self._send_spec(
                mav,
                sender,
                do_reposition(
                    point, alt=alt, ground_speed=speed, yaw_deg=float(kwargs.get("yaw_deg", 0.0))
                ),
                timeout_s,
                receiver_owner=self.vehicle,
            )
        if name == "orbit":
            center = args[0]
            radius = float(args[1])
            speed = float(args[2])
            return self._send_spec(
                mav,
                sender,
                do_orbit(center, radius=radius, speed=speed, alt=kwargs.get("alt")),
                timeout_s,
                receiver_owner=self.vehicle,
            )
        if name == "fw_land":
            method = str(args[0] if args else kwargs.get("method", "runway")).strip().lower()
            point = kwargs.get("point", (0.0, 0.0))
            alt = float(kwargs.get("alt", 0.0))
            if method == "vtol":
                # PX4 answers NAV_VTOL_LAND as a COMMAND_LONG with MAV_RESULT_UNSUPPORTED
                # (it is a mission item); after the MC back-transition NAV_LAND switches
                # to AUTO.LAND and lands in place.
                return self._send_spec(
                    mav,
                    sender,
                    nav_land(point=point, alt=alt),
                    timeout_s,
                    receiver_owner=self.vehicle,
                )
            if self._land_mission is not None:
                return self._land_mission(method=method, point=point, alt=alt, timeout_s=timeout_s)
            start = self._send_spec(
                mav, sender, do_land_start(), timeout_s, receiver_owner=self.vehicle
            )
            if not self._ok(start):
                return start
            return self._send_spec(
                mav, sender, nav_land(point=point, alt=alt), timeout_s, receiver_owner=self.vehicle
            )
        if name == "rtl":
            return self._send_spec(
                mav,
                sender,
                CommandSpec("MAV_CMD_NAV_RETURN_TO_LAUNCH", 20, (0.0,) * 7),
                timeout_s,
                receiver_owner=self.vehicle,
            )
        raise RuntimeError(f"vehicle lacks {name}() and no COMMAND_LONG mapping")

    @staticmethod
    def _send_spec(
        mav: Any, sender: Any, spec: Any, timeout_s: float, *, receiver_owner: Any | None = None
    ) -> Any:
        target_system = int(
            getattr(mav, "target_system", None)
            or getattr(receiver_owner, "target_system", None)
            or 1
        )
        target_component = int(
            getattr(mav, "target_component", None)
            or getattr(receiver_owner, "target_component", None)
            or 1
        )
        sender(target_system, target_component, int(spec.command), 0, *spec.params)
        # A link that is already drained by a reader thread must not be polled with
        # recv_match (the reader would swallow the ACK); such owners expose
        # wait_command_ack(command, timeout_s) backed by the reader's state.
        waiter = getattr(receiver_owner, "wait_command_ack", None)
        if callable(waiter):
            ack = waiter(int(spec.command), max(0.0, timeout_s))
            if ack is None:
                return type(
                    "CommandResult",
                    (),
                    {
                        "status": "timeout",
                        "ok": False,
                        "detail": f"COMMAND_ACK timeout for {spec.name}",
                    },
                )()
            result = ack.get("result", 0) if isinstance(ack, dict) else getattr(ack, "result", 0)
            # MAV_RESULT_ACCEPTED or IN_PROGRESS; TEMPORARILY_REJECTED, DENIED,
            # UNSUPPORTED and FAILED all mean PX4 did not take the command.
            ok = int(result or 0) in {0, 5}
            return type(
                "CommandResult",
                (),
                {"status": "accepted" if ok else "failed", "ok": ok, "result": result},
            )()
        # A raw link may not expose ACK reception.  In that case the send is
        # still a valid command submission; wrappers with recv_match can
        # return a concrete COMMAND_ACK and failure status.
        receiver = getattr(receiver_owner, "recv_match", None)
        if not callable(receiver):
            receiver = getattr(mav, "recv_match", None)
        if not callable(receiver):
            receiver = getattr(getattr(mav, "_connection", None), "recv_match", None)
        if not callable(receiver):
            return type("CommandResult", (), {"status": "sent", "ok": True})()
        try:
            ack = receiver(type="COMMAND_ACK", blocking=True, timeout=max(0.0, timeout_s))
        except (TypeError, OSError):
            ack = None
        if ack is None:
            return type(
                "CommandResult",
                (),
                {
                    "status": "timeout",
                    "ok": False,
                    "detail": f"COMMAND_ACK timeout for {spec.name}",
                },
            )()
        result = getattr(ack, "result", 0)
        if isinstance(ack, dict):
            result = ack.get("result", result)
        ok = int(result or 0) in {0, 1, 2, 3, 4, 5}
        return type(
            "CommandResult",
            (),
            {"status": "accepted" if ok else "failed", "ok": ok, "result": result},
        )()

    def _timeout(self) -> float:
        """Command ACK timeout used for every fixed-wing operation."""
        return self.command_timeout_s

    # A runway roll and climb, a VTOL transition or a full approach take far longer
    # than a command ACK; physical state waits get their own bounded budgets.
    MC_HOLD_SPEED_MPS = 5.0

    STATE_TIMEOUT_S = {
        "takeoff": 180.0,
        "takeoff_state": 180.0,
        "transition_fw": 90.0,
        "transition_mc": 90.0,
        "orbit": 90.0,
        "reposition": 120.0,
        "turn": 120.0,
        "rtl": 300.0,
        "land": 420.0,
    }

    def _state_timeout(self, phase: str) -> float:
        """Return a budget in the executor clock's seconds.

        The collector injects PX4's lockstep simulation clock here.  A
        ``wait_for_state`` adapter must therefore compare this budget against
        that same clock and keep its own generous wall-clock ceiling for a
        stalled simulator.
        """
        return max(self._timeout(), self.STATE_TIMEOUT_S.get(phase, self._timeout()))

    def _wait_state(self, phase: str) -> tuple[bool, Any | None]:
        """Optionally wait for a telemetry state transition.

        Runtime vehicle adapters currently return an ACK and let the caller
        consume telemetry independently.  Tests and newer adapters may expose
        ``wait_for_state``; when absent we preserve the historical ACK-only
        behavior.  A false/timeout result is reported as a failed phase.
        """
        if self._wait_for_state is None:
            return True, None
        try:
            result = self._wait_for_state(phase, self._state_timeout(phase))
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return False, {"error": str(exc), "phase": phase}
        if result is None or result is True:
            return True, result
        if isinstance(result, bool):
            return result, result
        ok = getattr(result, "ok", None)
        if ok is None:
            ok = getattr(result, "status", "") in {"accepted", "sent", "success", "reached"}
        return bool(ok), result

    def _failure(
        self, started: float, *, phase: str, result: Any = None, reason: str | None = None
    ) -> ExecutionResult:
        detail = {"phase": phase, "result": result}
        if reason:
            detail["reason"] = reason
        status = getattr(result, "status", None)
        if status == "timeout" or reason == "timeout":
            detail["reason"] = "timeout"
        return ExecutionResult("failed", self._clock() - started, ack=detail)

    def arm_and_takeoff(self, altitude_m: float | None = None) -> ExecutionResult:
        started = self._clock()
        altitude = self.altitude_m if altitude_m is None else float(altitude_m)
        try:
            # The collector confirms arming through its bounded ARM/heartbeat
            # helper before entering this executor.  Do not send a second raw
            # ARM command in that case: on a slow lockstep run PX4 can still
            # report the first command as temporarily rejected even though
            # SYS_STATUS has just become healthy.
            if not self._armed:
                arm = getattr(self.vehicle, "arm", None) or getattr(
                    self.vehicle, "arducopter_arm", None
                )
                if callable(arm):
                    arm_result = arm()
                    if not self._ok(arm_result):
                        return self._failure(started, phase="arm", result=arm_result)
            method = "vtol" if self.airframe == "vtol" else self.takeoff_method
            if self.airframe == "vtol" and not self.transition_after_takeoff:
                # PX4's NAV_VTOL_TAKEOFF transitions to FW on its own after the
                # climb; NAV_TAKEOFF on a VTOL in MC is a multicopter takeoff.
                method = "multirotor"
            result = self._invoke(
                "fw_takeoff",
                method,
                alt=altitude,
                point=self._commanded[:2],
                pitch_deg=0.0,
                yaw_deg=self._commanded_yaw,
                timeout_s=self._timeout(),
            )
            if not self._ok(result):
                return self._failure(started, phase="takeoff", result=result)
            ok, state = self._wait_state("takeoff")
            if not ok:
                return self._failure(started, phase="takeoff_state", result=state, reason="timeout")
            if self.airframe == "vtol" and not self.transition_after_takeoff:
                self.mode = "MC"
            elif self.airframe == "vtol":
                transition = self._invoke("vtol_transition", "FW", timeout_s=self._timeout())
                if not self._ok(transition):
                    return self._failure(started, phase="transition_fw", result=transition)
                ok, state = self._wait_state("transition_fw")
                if not ok:
                    return self._failure(
                        started, phase="transition_fw_state", result=state, reason="timeout"
                    )
            self._armed = True
            # Takeoff and transition can carry a tailsitter far from the
            # preflight position before the first guided translation.  Start
            # subsequent local-NED legs from the measured FW position/heading,
            # rather than the stale position captured in __init__.
            current_position = tuple(float(value) for value in self._position())
            self._commanded = (current_position[0], current_position[1], -altitude)
            self._commanded_yaw = float(self._yaw())
            return ExecutionResult("success", self._clock() - started, ack={"takeoff": result})
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})

    def execute(self, action: ActionPrimitive) -> ExecutionResult:
        if action.kind == "gimbal":
            return execute_gimbal_action(
                self.vehicle,
                action,
                profile=self.airframe_profile,
                reader=self.telemetry_reader,
                clock=self._clock,
                timeout_s=self.gimbal_timeout_s,
                tolerance_deg=self.gimbal_tolerance_deg,
                yaw_correction_deg=self.gimbal_yaw_correction_deg,
            )
        started = self._clock()
        origin = self._commanded
        yaw = self._commanded_yaw
        try:
            from multidetect.autonomy.motion_intent import MotionCommand

            if isinstance(action, MotionCommand):
                return self.execute_command(action)
            if action.kind not in {"hold", "stop"}:
                self._loiter_captured = False
            if action.kind == "hold" and self.airframe == "vtol" and self.mode == "MC":
                # A hovering VTOL has no orbit state to wait for; the last position
                # setpoint keeps it in place, so a hold is just a pause.
                deadline = started + max(0.0, float(action.seconds))
                while self._clock() < deadline:
                    time.sleep(min(0.05, max(0.0, deadline - self._clock())))
                return ExecutionResult("success", self._clock() - started, ack={"hold": "mc_hover"})
            if action.kind == "hold":
                if self._loiter_captured:
                    result = {"status": "already_active", "ok": True}
                else:
                    result = self._invoke(
                        "orbit",
                        origin[:2],
                        self.loiter_radius_m,
                        self.ground_speed_mps,
                        alt=-origin[2],
                        timeout_s=self._timeout(),
                    )
                    if not self._ok(result):
                        return self._failure(started, phase="orbit", result=result)
                ok, state = self._wait_state("orbit")
                if not ok:
                    return self._failure(
                        started, phase="orbit_state", result=state, reason="timeout"
                    )
                return ExecutionResult("success", self._clock() - started, ack={"orbit": result})
            if action.kind == "continue":
                deadline = started + action.seconds
                while self._clock() < deadline:
                    time.sleep(min(0.05, max(0.0, deadline - self._clock())))
                return ExecutionResult("success", self._clock() - started, ack={"continue": True})
            if action.kind == "land":
                return self.land()
            if action.kind == "stop":
                return ExecutionResult("success", self._clock() - started)
            if action.kind == "turn":
                target_yaw = yaw + action.yaw_delta_deg
                if self.mode == "FW":
                    commanded_down = self._commanded[2]
                    measured = self.sync_commanded_state()
                    origin = (measured[0], measured[1], commanded_down)
                    self._commanded = origin
                    yaw = self._commanded_yaw
                    target_yaw = self._wrap_yaw(yaw + action.yaw_delta_deg)
                    heading = math.radians(target_yaw)
                    profile_limits = getattr(self.airframe_profile, "limits", None)
                    max_step = getattr(profile_limits, "max_step_distance_m", None)
                    leg = (
                        0.9 * float(max_step)
                        if max_step is not None
                        else 0.9 * self.loiter_radius_m
                    )
                    turn_radius = getattr(self.airframe_profile, "min_turn_radius_m", None)
                    target = (
                        origin[0] + leg * math.cos(heading),
                        origin[1] + leg * math.sin(heading),
                    )
                    result = self._invoke(
                        "reposition",
                        target,
                        -commanded_down,
                        self.ground_speed_mps,
                        yaw_deg=target_yaw,
                        radius=float(turn_radius or 0.0),
                        timeout_s=self._timeout(),
                    )
                    if not self._ok(result):
                        return self._failure(started, phase="turn", result=result)
                    return self._wait_turn(started, yaw, target_yaw, action.yaw_delta_deg, result)
                result = self._invoke(
                    "reposition",
                    origin[:2],
                    -origin[2],
                    self.ground_speed_mps,
                    yaw_deg=target_yaw,
                    timeout_s=self._timeout(),
                )
                if not self._ok(result):
                    return self._failure(started, phase="turn", result=result)
                self._commanded_yaw = target_yaw
                return ExecutionResult(
                    "success", self._clock() - started, ack={"reposition": result}
                )
            if self.airframe == "vtol" and self.mode == "FW":
                # A tailsitter can move substantially during the MC→FW
                # transition.  Rebase every relative translation on the
                # measured FW pose at dispatch time so a cached takeoff pose
                # cannot become the next guided waypoint.
                commanded_down = self._commanded[2]
                measured = self.sync_commanded_state()
                # Preserve the commanded cruise altitude.  Transition
                # dynamics can momentarily overshoot it; the guided waypoint
                # should rebase its horizontal origin and heading without
                # turning that transient into a new altitude command.
                self._commanded = (measured[0], measured[1], commanded_down)
                origin = self._commanded
                yaw = self._commanded_yaw
            north, east, down = body_to_ned(action.forward_m, action.right_m, action.up_m, yaw)
            target = (origin[0] + north, origin[1] + east, origin[2] + down)
            result = self._invoke(
                "reposition",
                target[:2],
                -target[2],
                self.ground_speed_mps,
                timeout_s=self._timeout(),
            )
            if not self._ok(result):
                return self._failure(started, phase="reposition", result=result)
            self._commanded = target
            return ExecutionResult(
                "success", self._clock() - started, (north, east, down), ack={"reposition": result}
            )
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})

    def _wait_turn(
        self,
        started: float,
        start_yaw: float,
        target_yaw: float,
        commanded_delta: float,
        command_result: Any,
    ) -> ExecutionResult:
        """Wait for telemetry to confirm an FW turn or report its timeout."""
        timeout_s = self._state_timeout("turn")
        sim_started = self._clock()
        wall_started = time.monotonic()
        wall_timeout_s = max(180.0, timeout_s * 10.0)
        measured_yaw = start_yaw
        error = self._wrap_yaw(target_yaw - measured_yaw)
        while (
            self._clock() - sim_started < timeout_s
            and time.monotonic() - wall_started < wall_timeout_s
        ):
            measured_yaw = float(self._yaw())
            error = self._wrap_yaw(target_yaw - measured_yaw)
            achieved = self._wrap_yaw(measured_yaw - start_yaw)
            if abs(error) <= self.criterion.yaw_tolerance_deg:
                self.sync_commanded_state()
                return ExecutionResult(
                    "success",
                    self._clock() - started,
                    achieved_yaw_delta_deg=achieved,
                    ack={
                        "phase": "turn",
                        "command": command_result,
                        "target_yaw_deg": target_yaw,
                        "measured_yaw_deg": measured_yaw,
                        "yaw_error_deg": error,
                    },
                )
            time.sleep(0.1)
        achieved = self._wrap_yaw(measured_yaw - start_yaw)
        try:
            self.sync_commanded_state()
        except (RuntimeError, OSError, ValueError, TypeError):
            pass
        return ExecutionResult(
            "timeout",
            self._clock() - started,
            achieved_yaw_delta_deg=achieved,
            ack={
                "phase": "turn",
                "reason": "timeout",
                "command": command_result,
                "commanded_yaw_delta_deg": commanded_delta,
                "achieved_yaw_delta_deg": achieved,
                "target_yaw_deg": target_yaw,
                "measured_yaw_deg": measured_yaw,
                "yaw_error_deg": error,
                "timeout_s": timeout_s,
            },
        )

    @staticmethod
    def _wrap_yaw(degrees: float) -> float:
        return (float(degrees) + 180.0) % 360.0 - 180.0

    def execute_command(self, command: MotionCommand) -> ExecutionResult:
        """Execute the shared intent command vocabulary.

        A vehicle may implement either ``orbit``/``reposition`` (the PX4
        protocol) or the equivalent ``loiter_at`` convenience method.  The
        duck-typed calls keep this executor usable by deterministic SIM fakes.
        """
        started = self._clock()
        try:
            if command.rejected:
                return ExecutionResult(
                    "failed", self._clock() - started, ack={"reason": command.reason}
                )
            if command.kind == "gimbal":
                action = command.action or ActionPrimitive.gimbal(
                    pitch_deg=float(command.gimbal_pitch_deg or 0.0),
                    yaw_deg=float(command.gimbal_yaw_deg or 0.0),
                    roll_deg=float(command.gimbal_roll_deg or 0.0),
                )
                return execute_gimbal_action(
                    self.vehicle,
                    action,
                    profile=self.airframe_profile,
                    reader=self.telemetry_reader,
                    clock=self._clock,
                    timeout_s=self.gimbal_timeout_s,
                    tolerance_deg=self.gimbal_tolerance_deg,
                    yaw_correction_deg=self.gimbal_yaw_correction_deg,
                )
            centre = command.target_ned or self._commanded
            altitude = -centre[2]
            speed = command.speed_mps or self.ground_speed_mps
            if command.kind == "mc_hold":
                # A hovering VTOL holds the point and heading; an orbit of the
                # fixed-wing loiter radius would fly it around a 50 m circle.
                result = self._invoke(
                    "reposition",
                    centre[:2],
                    altitude,
                    command.speed_mps or self.MC_HOLD_SPEED_MPS,
                    yaw_deg=(
                        command.yaw_deg if command.yaw_deg is not None else self._commanded_yaw
                    ),
                    timeout_s=self._timeout(),
                )
                self._commanded = centre
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"hold": result},
                )
            if command.kind in {"loiter", "orbit"}:
                radius = command.radius_m or self.loiter_radius_m
                orbit_method = getattr(self.vehicle, "orbit", None)
                loiter_method = getattr(self.vehicle, "loiter_at", None)
                if callable(loiter_method) and not callable(orbit_method):
                    result = self._invoke(
                        "loiter_at",
                        centre[:2],
                        radius,
                        altitude,
                        speed=speed,
                        timeout_s=self._timeout(),
                    )
                else:
                    result = self._invoke(
                        "orbit", centre[:2], radius, speed, alt=altitude, timeout_s=self._timeout()
                    )
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"orbit": result},
                )
            if command.kind in {"reposition", "mc_reposition"}:
                target = command.target_ned or self._commanded
                result = self._invoke(
                    "reposition",
                    target[:2],
                    -target[2],
                    speed,
                    radius=command.radius_m or 0.0,
                    yaw_deg=command.yaw_deg or 0.0,
                    timeout_s=self._timeout(),
                )
                self._commanded = target
                if command.yaw_deg is not None:
                    self._commanded_yaw = command.yaw_deg
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"reposition": result},
                )
            if command.kind == "transition":
                result = self._invoke(
                    "vtol_transition", command.mode or "FW", timeout_s=self._timeout()
                )
                if self._ok(result):
                    self.mode = command.mode or "FW"
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"transition": result},
                )
            if command.kind == "fw_land":
                result = self._invoke(
                    "fw_land",
                    (command.metadata or {}).get("method", self.landing_method),
                    point=centre[:2],
                    alt=0.0,
                    timeout_s=self._timeout(),
                )
                self._armed = False
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"land": result},
                )
            if command.kind == "mc_land":
                if self.airframe == "vtol":
                    self._invoke("vtol_transition", "MC", timeout_s=self._timeout())
                land = getattr(self.vehicle, "land", None)
                result = (
                    land()
                    if callable(land)
                    else self._invoke(
                        "fw_land", "vtol", point=centre[:2], alt=0.0, timeout_s=self._timeout()
                    )
                )
                self._armed = False
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"land": result},
                )
            if command.kind == "fly_by_deliver":
                # Delivery never stops a fixed-wing vehicle.  The command is a
                # safe fly-through window for a payload controller to consume.
                result = self._invoke(
                    "reposition",
                    centre[:2],
                    altitude,
                    speed,
                    radius=command.radius_m or self.loiter_radius_m,
                    yaw_deg=command.yaw_deg or 0.0,
                    timeout_s=self._timeout(),
                )
                return ExecutionResult(
                    "success" if self._ok(result) else "failed",
                    self._clock() - started,
                    ack={"fly_by": result},
                )
            if command.kind == "stop":
                return ExecutionResult("success", self._clock() - started)
            raise RuntimeError(f"unsupported motion command: {command.kind}")
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})

    def rtl(self) -> None:
        method = getattr(self.vehicle, "rtl", None)
        if callable(method):
            method()
        else:
            set_mode = getattr(self.vehicle, "set_mode", None)
            if callable(set_mode):
                set_mode("RTL")
            else:
                self._invoke_command_long("rtl", timeout_s=self._timeout())
        self._armed = False

    def land(self) -> ExecutionResult:
        started = self._clock()
        try:
            if self.airframe == "vtol":
                transition = self._invoke("vtol_transition", "MC", timeout_s=self._timeout())
                if not self._ok(transition):
                    return self._failure(started, phase="transition_mc", result=transition)
                ok, state = self._wait_state("transition_mc")
                if not ok:
                    return self._failure(
                        started, phase="transition_mc_state", result=state, reason="timeout"
                    )
            result = self._invoke(
                "fw_land",
                self.landing_method,
                point=self._commanded[:2],
                alt=0.0,
                timeout_s=self._timeout(),
            )
            if not self._ok(result):
                return self._failure(started, phase="land", result=result)
            ok, state = self._wait_state("land")
            if not ok:
                return self._failure(started, phase="land_state", result=state, reason="timeout")
            self._armed = False
            return ExecutionResult("success", self._clock() - started, ack={"land": result})
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            return ExecutionResult("failed", self._clock() - started, ack={"error": str(exc)})


__all__ = ["FixedWingVtolExecutor"]
