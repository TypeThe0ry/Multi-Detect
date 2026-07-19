from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from .domain import BoundingBox, VehicleTelemetry
from .multimodal_ranging import CameraCalibration
from .pixhawk import (
    PixhawkRcInputSnapshot,
    PixhawkReadOnlyConfig,
    PixhawkReadOnlyTelemetryProvider,
)
from .unified_tracking import UnifiedTrackState


class FixedWingAimState(str, Enum):
    INHIBITED = "inhibited"
    PRESTREAM = "prestream"
    ACTIVE = "active"
    REACQUIRING = "reacquiring"


@dataclass(frozen=True, slots=True)
class FixedWingAimConfig:
    maximum_target_age_s: float = 0.30
    maximum_attitude_age_s: float = 0.50
    minimum_airspeed_mps: float = 12.0
    minimum_altitude_agl_m: float = 8.0
    maximum_abs_roll_deg: float = 20.0
    maximum_abs_pitch_deg: float = 15.0
    maximum_roll_correction_deg: float = 10.0
    maximum_pitch_correction_deg: float = 6.0
    roll_gain: float = 0.70
    pitch_gain: float = 0.70
    maximum_roll_slew_deg_s: float = 35.0
    maximum_pitch_slew_deg_s: float = 25.0
    prestream_setpoints: int = 10
    control_mode: str = "OFFBOARD"
    return_mode: str = "AUTO"
    rc_input_maximum_age_s: float = 0.30
    rc_cancel_threshold_us: int = 50

    def __post_init__(self) -> None:
        positive = (
            self.maximum_target_age_s,
            self.maximum_attitude_age_s,
            self.minimum_airspeed_mps,
            self.minimum_altitude_agl_m,
            self.maximum_abs_roll_deg,
            self.maximum_abs_pitch_deg,
            self.maximum_roll_correction_deg,
            self.maximum_pitch_correction_deg,
            self.roll_gain,
            self.pitch_gain,
            self.maximum_roll_slew_deg_s,
            self.maximum_pitch_slew_deg_s,
            self.rc_input_maximum_age_s,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in positive):
            raise ValueError("fixed-wing aim numeric configuration must be finite and positive")
        if self.maximum_roll_correction_deg > self.maximum_abs_roll_deg:
            raise ValueError("roll correction cannot exceed the absolute roll limit")
        if self.maximum_pitch_correction_deg > self.maximum_abs_pitch_deg:
            raise ValueError("pitch correction cannot exceed the absolute pitch limit")
        if (
            isinstance(self.prestream_setpoints, bool)
            or not isinstance(self.prestream_setpoints, int)
            or self.prestream_setpoints < 1
        ):
            raise ValueError("prestream_setpoints must be a positive integer")
        if (
            isinstance(self.rc_cancel_threshold_us, bool)
            or not isinstance(self.rc_cancel_threshold_us, int)
            or not 10 <= self.rc_cancel_threshold_us <= 500
        ):
            raise ValueError("rc_cancel_threshold_us must be an integer in [10, 500]")
        if not self.control_mode.strip() or not self.return_mode.strip():
            raise ValueError("fixed-wing aim mode names cannot be empty")


@dataclass(frozen=True, slots=True)
class FixedWingAimTarget:
    target_id: str
    target_revision: int
    bbox: BoundingBox
    observed_at_s: float
    state: UnifiedTrackState
    locked: bool
    primary: bool

    def __post_init__(self) -> None:
        if not self.target_id.strip() or self.target_revision < 0:
            raise ValueError("fixed-wing aim target binding is invalid")
        if not math.isfinite(self.observed_at_s) or self.observed_at_s < 0.0:
            raise ValueError("fixed-wing aim target timestamp is invalid")


@dataclass(frozen=True, slots=True)
class FixedWingAttitudeTarget:
    target_id: str
    target_revision: int
    produced_at_s: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    yaw_error_deg: float
    pitch_error_deg: float
    quaternion_wxyz: tuple[float, float, float, float]
    reacquiring: bool


@dataclass(frozen=True, slots=True)
class FixedWingAimDecision:
    state: FixedWingAimState
    reason: str
    setpoint: FixedWingAttitudeTarget | None = None


class FixedWingAttitudeTransport(Protocol):
    def send_attitude_target(self, setpoint: FixedWingAttitudeTarget) -> None: ...

    def request_mode(self, mode: str) -> None: ...


class FixedWingAimController:
    """Converts one confirmed LCK target into bounded fixed-wing attitude targets."""

    _CONTROL_STATES = frozenset(
        {
            UnifiedTrackState.TRACKING,
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.RECOVERED,
        }
    )
    _REACQUIRE_STATES = frozenset(
        {
            UnifiedTrackState.OCCLUDED,
            UnifiedTrackState.REACQUIRING,
            UnifiedTrackState.RECOVERED,
        }
    )

    def __init__(
        self,
        calibration: CameraCalibration,
        config: FixedWingAimConfig | None = None,
    ) -> None:
        self.calibration = calibration
        self.config = config or FixedWingAimConfig()
        self._binding: tuple[str, int] | None = None
        self._reference_heading_deg: float | None = None
        self._last_setpoint: FixedWingAttitudeTarget | None = None

    def clear(self) -> None:
        self._binding = None
        self._reference_heading_deg = None
        self._last_setpoint = None

    def evaluate(
        self,
        *,
        target: FixedWingAimTarget | None,
        telemetry: VehicleTelemetry,
        mode3_active: bool,
        execution_confirmed: bool,
        now_s: float,
    ) -> FixedWingAimDecision:
        self._require_time(now_s)
        reason = self._inhibit_reason(
            target=target,
            telemetry=telemetry,
            mode3_active=mode3_active,
            execution_confirmed=execution_confirmed,
            now_s=now_s,
        )
        if reason is not None:
            self.clear()
            return FixedWingAimDecision(FixedWingAimState.INHIBITED, reason)
        if target is None:  # Defensive guard for optimized Python and future predicate changes.
            self.clear()
            return FixedWingAimDecision(
                FixedWingAimState.INHIBITED,
                "locked_target_unavailable",
            )

        binding = (target.target_id, target.target_revision)
        if binding != self._binding:
            self._binding = binding
            self._reference_heading_deg = telemetry.heading_deg % 360.0
            self._last_setpoint = None
        reference_heading_deg = self._reference_heading_deg
        if reference_heading_deg is None:
            self.clear()
            return FixedWingAimDecision(
                FixedWingAimState.INHIBITED,
                "reference_heading_unavailable",
            )

        yaw_error_deg, pitch_error_deg = self._optical_errors(target.bbox)
        roll_correction = self._clamp(
            yaw_error_deg * self.config.roll_gain,
            self.config.maximum_roll_correction_deg,
        )
        pitch_correction = self._clamp(
            -pitch_error_deg * self.config.pitch_gain,
            self.config.maximum_pitch_correction_deg,
        )
        desired_roll = self._clamp(
            telemetry.roll_deg + roll_correction,
            self.config.maximum_abs_roll_deg,
        )
        desired_pitch = self._clamp(
            telemetry.pitch_deg + pitch_correction,
            self.config.maximum_abs_pitch_deg,
        )
        if self._last_setpoint is not None:
            elapsed_s = max(0.0, now_s - self._last_setpoint.produced_at_s)
            desired_roll = self._slew(
                self._last_setpoint.roll_deg,
                desired_roll,
                self.config.maximum_roll_slew_deg_s * elapsed_s,
            )
            desired_pitch = self._slew(
                self._last_setpoint.pitch_deg,
                desired_pitch,
                self.config.maximum_pitch_slew_deg_s * elapsed_s,
            )
        reacquiring = target.state in self._REACQUIRE_STATES
        setpoint = FixedWingAttitudeTarget(
            target_id=target.target_id,
            target_revision=target.target_revision,
            produced_at_s=now_s,
            roll_deg=desired_roll,
            pitch_deg=desired_pitch,
            yaw_deg=reference_heading_deg,
            yaw_error_deg=yaw_error_deg,
            pitch_error_deg=pitch_error_deg,
            quaternion_wxyz=self._quaternion_from_euler(
                desired_roll,
                desired_pitch,
                reference_heading_deg,
            ),
            reacquiring=reacquiring,
        )
        self._last_setpoint = setpoint
        state = FixedWingAimState.REACQUIRING if reacquiring else FixedWingAimState.ACTIVE
        return FixedWingAimDecision(state, "bounded_attitude_target_ready", setpoint)

    def _inhibit_reason(
        self,
        *,
        target: FixedWingAimTarget | None,
        telemetry: VehicleTelemetry,
        mode3_active: bool,
        execution_confirmed: bool,
        now_s: float,
    ) -> str | None:
        if not mode3_active:
            return "mode3_inactive"
        if not execution_confirmed:
            return "execution_confirmation_required"
        if target is None:
            return "locked_target_unavailable"
        if not target.locked or not target.primary:
            return "target_not_locked_primary"
        if target.state not in self._CONTROL_STATES:
            return f"target_{target.state.value}"
        if (
            target.observed_at_s > now_s
            or now_s - target.observed_at_s > self.config.maximum_target_age_s
        ):
            return "target_stale"
        if telemetry.armed is not True:
            return "vehicle_not_armed"
        if telemetry.link_healthy is not True:
            return "flight_controller_link_unhealthy"
        required = (
            telemetry.roll_deg,
            telemetry.pitch_deg,
            telemetry.heading_deg,
            telemetry.attitude_observed_at_s,
            telemetry.airspeed_mps,
            telemetry.altitude_agl_m,
        )
        if not all(math.isfinite(value) for value in required):
            return "required_flight_telemetry_unavailable"
        attitude_age = now_s - telemetry.attitude_observed_at_s
        if attitude_age < 0.0 or attitude_age > self.config.maximum_attitude_age_s:
            return "attitude_telemetry_stale"
        if telemetry.airspeed_mps < self.config.minimum_airspeed_mps:
            return "airspeed_below_control_minimum"
        if telemetry.altitude_agl_m < self.config.minimum_altitude_agl_m:
            return "altitude_below_control_minimum"
        if abs(telemetry.roll_deg) > self.config.maximum_abs_roll_deg:
            return "roll_outside_control_domain"
        if abs(telemetry.pitch_deg) > self.config.maximum_abs_pitch_deg:
            return "pitch_outside_control_domain"
        return None

    def _optical_errors(self, bbox: BoundingBox) -> tuple[float, float]:
        x_px = (bbox.x1 + bbox.x2) * 0.5 * self.calibration.width_px
        y_px = (bbox.y1 + bbox.y2) * 0.5 * self.calibration.height_px
        yaw = math.degrees(math.atan2(x_px - self.calibration.cx_px, self.calibration.fx_px))
        pitch = math.degrees(
            math.atan2(y_px - self.calibration.cy_px, self.calibration.fy_px)
        )
        return yaw, pitch

    @staticmethod
    def _quaternion_from_euler(
        roll_deg: float,
        pitch_deg: float,
        yaw_deg: float,
    ) -> tuple[float, float, float, float]:
        roll = math.radians(roll_deg) * 0.5
        pitch = math.radians(pitch_deg) * 0.5
        yaw = math.radians(yaw_deg) * 0.5
        cr, sr = math.cos(roll), math.sin(roll)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        return (
            cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy,
        )

    @staticmethod
    def _clamp(value: float, limit: float) -> float:
        return max(-limit, min(limit, value))

    @staticmethod
    def _slew(previous: float, desired: float, maximum_delta: float) -> float:
        return max(previous - maximum_delta, min(previous + maximum_delta, desired))

    @staticmethod
    def _require_time(value: float) -> None:
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("fixed-wing aim time must be finite and non-negative")


class FixedWingAimExecutor:
    """Streams real SET_ATTITUDE_TARGET messages and performs PX4 mode handoff."""

    def __init__(
        self,
        controller: FixedWingAimController,
        transport: FixedWingAttitudeTransport,
    ) -> None:
        self.controller = controller
        self.transport = transport
        self._prestream_count = 0
        self._control_mode_requested = False
        self._entry_mode: str | None = None
        self._execution_binding: tuple[str, int] | None = None
        self._rc_baseline: tuple[int | None, ...] | None = None
        self._last_rc_sample_at_s: float | None = None
        self._pilot_cancelled_binding: tuple[str, int] | None = None

    def step(
        self,
        *,
        target: FixedWingAimTarget | None,
        telemetry: VehicleTelemetry,
        mode3_active: bool,
        execution_confirmed: bool,
        now_s: float,
    ) -> FixedWingAimDecision:
        binding = (
            (target.target_id, target.target_revision)
            if target is not None and mode3_active and execution_confirmed
            else None
        )
        if binding != self._execution_binding:
            self._execution_binding = binding
            self._rc_baseline = None
            self._last_rc_sample_at_s = None
            self._pilot_cancelled_binding = None
        if binding is not None:
            rc_reason = self._rc_guard_reason(binding=binding, now_s=now_s)
            if rc_reason is not None:
                self.controller.clear()
                self._return_control_mode(observed_mode=telemetry.flight_mode)
                return FixedWingAimDecision(FixedWingAimState.INHIBITED, rc_reason)

        decision = self.controller.evaluate(
            target=target,
            telemetry=telemetry,
            mode3_active=mode3_active,
            execution_confirmed=execution_confirmed,
            now_s=now_s,
        )
        if decision.setpoint is None:
            self._return_control_mode(observed_mode=telemetry.flight_mode)
            return decision

        if self._entry_mode is None:
            observed_mode = telemetry.flight_mode
            if (
                isinstance(observed_mode, str)
                and observed_mode.strip()
                and observed_mode.strip().upper()
                != self.controller.config.control_mode.strip().upper()
            ):
                self._entry_mode = observed_mode.strip().upper()
        self.transport.send_attitude_target(decision.setpoint)
        self._prestream_count += 1
        if (
            not self._control_mode_requested
            and self._prestream_count >= self.controller.config.prestream_setpoints
        ):
            self.transport.request_mode(self.controller.config.control_mode)
            self._control_mode_requested = True
        if not self._control_mode_requested:
            return FixedWingAimDecision(
                FixedWingAimState.PRESTREAM,
                "offboard_setpoint_prestream",
                decision.setpoint,
            )
        return decision

    def _rc_guard_reason(
        self,
        *,
        binding: tuple[str, int],
        now_s: float,
    ) -> str | None:
        if self._pilot_cancelled_binding == binding:
            return "pilot_input_cancelled"
        sample = getattr(self.transport, "rc_input_snapshot", None)
        if not isinstance(sample, PixhawkRcInputSnapshot):
            return "rc_input_unavailable_or_stale"
        age_s = now_s - sample.observed_at_s
        if age_s < 0.0 or age_s > self.controller.config.rc_input_maximum_age_s:
            return "rc_input_unavailable_or_stale"
        if self._rc_baseline is None:
            self._rc_baseline = sample.channels_pwm
            self._last_rc_sample_at_s = sample.observed_at_s
            return None
        if (
            self._last_rc_sample_at_s is not None
            and sample.observed_at_s <= self._last_rc_sample_at_s
        ):
            return None
        self._last_rc_sample_at_s = sample.observed_at_s
        maximum_delta_us = max(
            (
                abs(current - baseline)
                for baseline, current in zip(self._rc_baseline, sample.channels_pwm, strict=True)
                if baseline is not None and current is not None
            ),
            default=0,
        )
        if maximum_delta_us >= self.controller.config.rc_cancel_threshold_us:
            self._pilot_cancelled_binding = binding
            return "pilot_input_cancelled"
        return None

    def _return_control_mode(self, *, observed_mode: str | None) -> None:
        if self._control_mode_requested:
            normalized_observed_mode = (
                observed_mode.strip().upper()
                if isinstance(observed_mode, str) and observed_mode.strip()
                else None
            )
            if normalized_observed_mode in {
                None,
                self.controller.config.control_mode.strip().upper(),
            }:
                self.transport.request_mode(
                    self._entry_mode or self.controller.config.return_mode
                )
        self._prestream_count = 0
        self._control_mode_requested = False
        self._entry_mode = None


@dataclass(frozen=True, slots=True)
class PixhawkFlightControlConfig:
    telemetry: PixhawkReadOnlyConfig
    target_component_id: int = 1
    rc_input_rate_hz: float = 20.0

    def __post_init__(self) -> None:
        if self.telemetry.expected_system_id is None:
            raise ValueError("flight control requires an explicit Pixhawk system ID")
        if (
            isinstance(self.target_component_id, bool)
            or not isinstance(self.target_component_id, int)
            or not 1 <= self.target_component_id <= 255
        ):
            raise ValueError("target component ID must be an integer in [1, 255]")
        if not math.isfinite(self.rc_input_rate_hz) or not 2.0 <= self.rc_input_rate_hz <= 50.0:
            raise ValueError("RC input rate must be finite and in [2, 50] Hz")


class PixhawkFlightControlProvider(PixhawkReadOnlyTelemetryProvider):
    """Qualified telemetry provider with explicit PX4 attitude-target transmission."""

    _ATTITUDE_TARGET_TYPE_MASK = 1 | 2 | 4 | 64
    _CUSTOM_MODE_ENABLED = 1
    _MAV_CMD_SET_MESSAGE_INTERVAL = 511
    _RC_CHANNELS_MESSAGE_ID = 65
    _RC_STREAM_REQUEST_RETRY_S = 2.0

    def __init__(self, config: PixhawkFlightControlConfig) -> None:
        super().__init__(config.telemetry)
        self.flight_control_config = config
        self._messages_transmitted = 0
        self._last_rc_stream_request_s: float | None = None

    @property
    def is_read_only(self) -> bool:
        return False

    @property
    def messages_transmitted(self) -> int:
        return self._messages_transmitted

    def diagnostics(self, *, now_s: float) -> dict[str, object]:
        document = super().diagnostics(now_s=now_s)
        document["read_only"] = False
        document["hardware_control_enabled"] = True
        document["messages_transmitted"] = self.messages_transmitted
        return document

    def snapshot(self, *, now_s: float) -> VehicleTelemetry:
        telemetry = super().snapshot(now_s=now_s)
        self._ensure_rc_input_stream(now_s=now_s)
        return telemetry

    def send_attitude_target(self, setpoint: FixedWingAttitudeTarget) -> None:
        connection = self._qualified_connection()
        connection.mav.set_attitude_target_send(
            int(setpoint.produced_at_s * 1000.0) & 0xFFFFFFFF,
            int(self.config.expected_system_id),
            self.flight_control_config.target_component_id,
            self._ATTITUDE_TARGET_TYPE_MASK,
            setpoint.quaternion_wxyz,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        self._messages_transmitted += 1

    def request_mode(self, mode: str) -> None:
        connection = self._qualified_connection()
        mapping = connection.mode_mapping()
        normalized = mode.strip().upper()
        if not isinstance(mapping, dict) or normalized not in mapping:
            raise ValueError(f"PX4 mode is unavailable on this link: {normalized}")
        connection.mav.set_mode_send(
            int(self.config.expected_system_id),
            self._CUSTOM_MODE_ENABLED,
            int(mapping[normalized]),
        )
        self._messages_transmitted += 1

    def _ensure_rc_input_stream(self, *, now_s: float) -> None:
        sample = self.rc_input_snapshot
        if sample is not None and now_s - sample.observed_at_s <= 1.0:
            return
        if (
            self._last_rc_stream_request_s is not None
            and now_s - self._last_rc_stream_request_s < self._RC_STREAM_REQUEST_RETRY_S
        ):
            return
        if self.qualification.passed is not True or self._connection is None:
            return
        interval_us = int(round(1_000_000.0 / self.flight_control_config.rc_input_rate_hz))
        self._connection.mav.command_long_send(
            int(self.config.expected_system_id),
            self.flight_control_config.target_component_id,
            self._MAV_CMD_SET_MESSAGE_INTERVAL,
            0,
            float(self._RC_CHANNELS_MESSAGE_ID),
            float(interval_us),
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        self._last_rc_stream_request_s = now_s
        self._messages_transmitted += 1

    def _qualified_connection(self) -> Any:
        self.connect()
        connection = self._connection
        if connection is None:
            raise RuntimeError("Pixhawk connection failed to initialize")
        qualification = self.qualification
        if qualification.passed is not True:
            raise RuntimeError("Pixhawk identity qualification is not satisfied")
        return connection


__all__ = [
    "FixedWingAimConfig",
    "FixedWingAimController",
    "FixedWingAimDecision",
    "FixedWingAimExecutor",
    "FixedWingAimState",
    "FixedWingAimTarget",
    "FixedWingAttitudeTarget",
    "FixedWingAttitudeTransport",
    "PixhawkFlightControlConfig",
    "PixhawkFlightControlProvider",
]
