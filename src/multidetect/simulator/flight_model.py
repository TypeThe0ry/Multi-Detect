"""Small deterministic, truth-free flight model for simulator acceptance tests."""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from typing import Any

from ..mavlink_gps import (
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_GPS_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_SATELLITES_MAX,
    MAVLINK_SATELLITES_UNKNOWN,
)
from ..target_geolocation import (
    WGS84_SEMI_MINOR_AXIS_M,
    wgs84_forward_from_ned,
)
from .ai_executor import SimulatorTelemetrySnapshot as _BaseTelemetrySnapshot


class FlightModelError(ValueError):
    """Raised when a flight-model input would make the simulation invalid."""


class FlightMode(str, Enum):
    """Supported deterministic vehicle models.

    ``MULTIROTOR`` deliberately shares the same bounded, truth-free control
    contract as the VTOL hover phase.  It is a separate mode so manifests,
    telemetry, evidence and external PX4 plans can bind to the actual
    airframe instead of overloading ``vtol`` for every rotorcraft.
    """

    VTOL = "vtol"
    FIXED_WING = "fixed-wing"
    MULTIROTOR = "multirotor"

    @classmethod
    def parse(cls, value: FlightMode | str) -> FlightMode:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise FlightModelError("mode must be 'vtol', 'fixed-wing' or 'multirotor'")
        normalized = value.strip().lower().replace("_", "-")
        aliases = {
            "fixedwing": cls.FIXED_WING,
            "plane": cls.FIXED_WING,
            "multicopter": cls.MULTIROTOR,
            "multi-rotor": cls.MULTIROTOR,
            "quad": cls.MULTIROTOR,
            "quadcopter": cls.MULTIROTOR,
            "quad-copter": cls.MULTIROTOR,
            "copter": cls.MULTIROTOR,
            "x500": cls.MULTIROTOR,
        }
        try:
            # Do not pass ``cls(normalized)`` as ``dict.get``'s default: the
            # default expression is evaluated eagerly, which would reject a
            # perfectly valid alias such as ``quadcopter`` before the lookup
            # has a chance to return it.
            return aliases[normalized] if normalized in aliases else cls(normalized)
        except ValueError as exc:
            raise FlightModelError("mode must be 'vtol', 'fixed-wing' or 'multirotor'") from exc


# MAVLink command ids used by the in-process sink.  Keeping the values here
# avoids importing pymavlink in the deterministic fixture while retaining the
# wire-compatible command contract used by QGC/PX4.
MAV_CMD_NAV_RETURN_TO_LAUNCH = 20
MAV_CMD_NAV_LAND = 21
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_MISSION_START = 300
MAV_CMD_DO_VTOL_TRANSITION = 3_000


@dataclass(frozen=True, slots=True)
class FlightModelConfig:
    """Bounds and initial state for :class:`DeterministicFlightModel`."""

    max_speed_mps: float = 45.0
    max_altitude_m: float = 2_000.0
    max_roll_deg: float = 55.0
    max_pitch_deg: float = 30.0
    max_yaw_rate_deg_s: float = 90.0
    max_vertical_speed_mps: float = 12.0
    max_dt_s: float = 10.0
    initial_latitude_deg: float = 0.0
    initial_longitude_deg: float = 0.0
    initial_altitude_m: float = 10.0
    initial_heading_deg: float = 0.0
    initial_speed_mps: float = 0.0
    # Lifecycle and sensor values are explicit fixture inputs rather than
    # constants hidden in ``_snapshot``.  A deterministic SIM starts
    # disarmed; callers must issue an ARM command before AI/actuator output is
    # accepted.  GPS/link values model the *observed* sensor contract and can
    # be set to ``None``/``nan`` to exercise unknown-quality gates.
    initial_armed: bool = False
    initial_link_healthy: bool | None = True
    initial_gps_fix_type: int | None = 3
    initial_gps_horizontal_accuracy_m: float = 1.5
    initial_gps_vertical_accuracy_m: float = 2.5
    initial_satellites_visible: int | None = 12
    initial_battery_remaining_pct: float = 100.0
    initial_flight_mode: str = "MANUAL"

    def __post_init__(self) -> None:
        positive = (
            "max_speed_mps",
            "max_altitude_m",
            "max_roll_deg",
            "max_pitch_deg",
            "max_yaw_rate_deg_s",
            "max_vertical_speed_mps",
            "max_dt_s",
        )
        for name in positive:
            if _finite(getattr(self, name), name) <= 0.0:
                raise FlightModelError(f"{name} must be greater than zero")
        if self.max_roll_deg > 89.0 or self.max_pitch_deg > 89.0:
            raise FlightModelError("attitude bounds must be below 90 degrees")
        for name in (
            "initial_latitude_deg",
            "initial_longitude_deg",
            "initial_altitude_m",
            "initial_heading_deg",
            "initial_speed_mps",
        ):
            _finite(getattr(self, name), name)
        if not -90.0 < self.initial_latitude_deg < 90.0:
            raise FlightModelError("initial_latitude_deg must be within (-90, 90)")
        if not -180.0 <= self.initial_longitude_deg <= 180.0:
            raise FlightModelError("initial_longitude_deg must be within [-180, 180]")
        if not 0.0 <= self.initial_altitude_m <= self.max_altitude_m:
            raise FlightModelError("initial_altitude_m is outside altitude bounds")
        if not 0.0 <= self.initial_speed_mps <= self.max_speed_mps:
            raise FlightModelError("initial_speed_mps is outside speed bounds")
        if not isinstance(self.initial_armed, bool):
            raise FlightModelError("initial_armed must be bool")
        if self.initial_link_healthy is not None and not isinstance(
            self.initial_link_healthy, bool
        ):
            raise FlightModelError("initial_link_healthy must be bool or None")
        if self.initial_gps_fix_type is not None:
            if (
                isinstance(self.initial_gps_fix_type, bool)
                or not isinstance(self.initial_gps_fix_type, int)
                or not MAVLINK_GPS_FIX_TYPE_MIN
                <= self.initial_gps_fix_type
                <= MAVLINK_GPS_FIX_TYPE_MAX
            ):
                raise FlightModelError(
                    "initial_gps_fix_type must be an integer within "
                    f"[{MAVLINK_GPS_FIX_TYPE_MIN}, {MAVLINK_GPS_FIX_TYPE_MAX}]"
                )
        for name in (
            "initial_gps_horizontal_accuracy_m",
            "initial_gps_vertical_accuracy_m",
        ):
            value = getattr(self, name)
            if not _finite_or_nan(value, name):
                raise FlightModelError(f"{name} must be finite or NaN")
            if math.isfinite(float(value)) and float(value) < 0.0:
                raise FlightModelError(f"{name} cannot be negative")
        if self.initial_satellites_visible is not None:
            if (
                isinstance(self.initial_satellites_visible, bool)
                or not isinstance(self.initial_satellites_visible, int)
                or not 0 <= self.initial_satellites_visible <= MAVLINK_SATELLITES_UNKNOWN
            ):
                raise FlightModelError(
                    "initial_satellites_visible must be an integer within "
                    f"[0, {MAVLINK_SATELLITES_UNKNOWN}]"
                )
        battery = _finite(self.initial_battery_remaining_pct, "initial_battery_remaining_pct")
        if not 0.0 <= battery <= 100.0:
            raise FlightModelError("initial_battery_remaining_pct must be within [0, 100]")
        if not isinstance(self.initial_flight_mode, str) or not self.initial_flight_mode.strip():
            raise FlightModelError("initial_flight_mode must be a non-empty string")


@dataclass(frozen=True, slots=True)
class FlightControlSetpoint:
    """Normalized actuator command accepted by ``step``."""

    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0
    throttle: float = 0.0
    timestamp_s: float | None = None
    source: str = "sim"

    def __post_init__(self) -> None:
        _validate_axes(self.roll, self.pitch, self.yaw, self.throttle)
        if self.timestamp_s is not None:
            _finite(self.timestamp_s, "timestamp_s")
        if not isinstance(self.source, str) or not self.source.strip():
            raise FlightModelError("source must be a non-empty string")


@dataclass(frozen=True, slots=True)
class FlightTelemetrySnapshot(_BaseTelemetrySnapshot):
    """Base AI telemetry plus measured attitude, with no simulator truth."""

    mode: str = FlightMode.VTOL.value
    roll_deg: float = 0.0
    pitch_deg: float = 0.0
    yaw_deg: float = 0.0
    airspeed_mps: float = 0.0
    wind_east_mps: float = 0.0
    wind_north_mps: float = 0.0
    step_count: int = 0
    flight_mode: str = "MANUAL"
    mission_started: bool = False

    def __post_init__(self) -> None:
        # Explicit dispatch avoids the zero-argument ``super()`` + slots
        # interaction on Python 3.10/3.11 dataclasses.
        _BaseTelemetrySnapshot.__post_init__(self)
        if self.mode not in {item.value for item in FlightMode}:
            raise FlightModelError("telemetry mode is invalid")
        for name in (
            "roll_deg",
            "pitch_deg",
            "yaw_deg",
            "airspeed_mps",
            "wind_east_mps",
            "wind_north_mps",
        ):
            _finite(getattr(self, name), name)
        if self.airspeed_mps < 0.0 or self.step_count < 0:
            raise FlightModelError("telemetry speed and step_count must be non-negative")
        if not isinstance(self.flight_mode, str) or not self.flight_mode.strip():
            raise FlightModelError("telemetry flight_mode must be a non-empty string")
        if not isinstance(self.mission_started, bool):
            raise FlightModelError("telemetry mission_started must be bool")

    def to_mapping(self) -> dict[str, object]:
        result = _BaseTelemetrySnapshot.to_mapping(self)
        result.update(
            {
                "mode": self.mode,
                "roll_deg": float(self.roll_deg),
                "pitch_deg": float(self.pitch_deg),
                "yaw_deg": float(self.yaw_deg),
                "airspeed_mps": float(self.airspeed_mps),
                "wind_east_mps": float(self.wind_east_mps),
                "wind_north_mps": float(self.wind_north_mps),
                "step_count": self.step_count,
                "flight_mode": self.flight_mode,
                "mission_started": self.mission_started,
                "truth_free": True,
            }
        )
        return result


@dataclass(slots=True)
class _FlightState:
    time_s: float
    east_m: float
    north_m: float
    altitude_m: float
    speed_mps: float
    roll_deg: float
    pitch_deg: float
    yaw_deg: float
    step_count: int
    armed: bool
    link_healthy: bool | None
    gps_fix_type: int | None
    gps_horizontal_accuracy_m: float
    gps_vertical_accuracy_m: float
    satellites_visible: int | None
    battery_remaining_pct: float
    flight_mode: str
    mission_started: bool


class DeterministicFlightModel:
    """Bounded deterministic VTOL/multirotor/fixed-wing kinematic model."""

    def __init__(
        self,
        mode: FlightMode | str = FlightMode.VTOL,
        *,
        seed: int = 0,
        wind_mps: Sequence[Real] = (0.0, 0.0),
        config: FlightModelConfig | None = None,
        vehicle_type: FlightMode | str | None = None,
    ) -> None:
        if vehicle_type is not None:
            if mode != FlightMode.VTOL and FlightMode.parse(mode) != FlightMode.parse(vehicle_type):
                raise FlightModelError("mode and vehicle_type disagree")
            mode = vehicle_type
        self.mode = FlightMode.parse(mode)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise FlightModelError("seed must be an integer")
        self.seed = seed
        self.config = config or FlightModelConfig()
        self.wind_east_mps, self.wind_north_mps = _wind(wind_mps)
        self._rng = random.Random(seed)
        self._last_timestamp_s: float | None = None
        cfg = self.config
        self._state = _FlightState(
            time_s=0.0,
            east_m=0.0,
            north_m=0.0,
            altitude_m=cfg.initial_altitude_m,
            speed_mps=cfg.initial_speed_mps,
            roll_deg=0.0,
            pitch_deg=0.0,
            yaw_deg=_wrap_heading(cfg.initial_heading_deg),
            step_count=0,
            armed=cfg.initial_armed,
            link_healthy=cfg.initial_link_healthy,
            gps_fix_type=cfg.initial_gps_fix_type,
            gps_horizontal_accuracy_m=cfg.initial_gps_horizontal_accuracy_m,
            gps_vertical_accuracy_m=cfg.initial_gps_vertical_accuracy_m,
            satellites_visible=cfg.initial_satellites_visible,
            battery_remaining_pct=cfg.initial_battery_remaining_pct,
            flight_mode=cfg.initial_flight_mode.strip().upper(),
            mission_started=False,
        )
        self._initial_state = _copy_state(self._state)
        self._command_history: list[dict[str, object]] = []
        self._last_snapshot = self._snapshot()

    @property
    def time_s(self) -> float:
        return self._state.time_s

    @property
    def altitude_m(self) -> float:
        return self._state.altitude_m

    @property
    def speed_mps(self) -> float:
        return self._state.speed_mps

    @property
    def airspeed_mps(self) -> float:
        return self._state.speed_mps

    @property
    def ground_speed_mps(self) -> float:
        return math.hypot(*self._velocity_components())

    @property
    def roll_deg(self) -> float:
        return self._state.roll_deg

    @property
    def pitch_deg(self) -> float:
        return self._state.pitch_deg

    @property
    def yaw_deg(self) -> float:
        return self._state.yaw_deg

    @property
    def armed(self) -> bool:
        """Whether the simulated vehicle has received an ARM command."""

        return self._state.armed

    @property
    def link_healthy(self) -> bool | None:
        return self._state.link_healthy

    @property
    def gps_fix_type(self) -> int | None:
        return self._state.gps_fix_type

    @property
    def battery_remaining_pct(self) -> float:
        return self._state.battery_remaining_pct

    @property
    def flight_mode(self) -> str:
        return self._state.flight_mode

    @property
    def mission_started(self) -> bool:
        return self._state.mission_started

    @property
    def command_history(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._command_history)

    @property
    def latitude_deg(self) -> float:
        return self._geodetic_position()[0]

    @property
    def longitude_deg(self) -> float:
        return self._geodetic_position()[1]

    def _geodetic_position(self) -> tuple[float, float]:
        """Return the current endpoint using the shared WGS-84 direct solve.

        The kinematic state remains a local N/E displacement because that is
        the stable control/evidence representation.  Converting the endpoint
        with the same ellipsoid solver as target geolocation removes the old
        equirectangular 111,320 m/degree approximation from SIM telemetry,
        especially at higher outdoor latitudes and across the dateline.
        """

        try:
            return wgs84_forward_from_ned(
                latitude_deg=self.config.initial_latitude_deg,
                longitude_deg=self.config.initial_longitude_deg,
                north_offset_m=self._state.north_m,
                east_offset_m=self._state.east_m,
            )
        except ValueError as exc:
            raise FlightModelError(f"state geodetic position is invalid: {exc}") from exc

    @property
    def telemetry(self) -> FlightTelemetrySnapshot:
        return self._last_snapshot

    def reset(self) -> FlightTelemetrySnapshot:
        """Reset state and the deterministic random stream to initial values."""

        self._state = _copy_state(self._initial_state)
        self._rng = random.Random(self.seed)
        self._last_timestamp_s = None
        self._command_history.clear()
        self._last_snapshot = self._snapshot()
        return self._last_snapshot

    def set_armed(self, armed: bool, *, reason: str = "external") -> bool:
        """Set lifecycle arming state and record the transition.

        This is intentionally explicit: constructing a SIM model never arms
        it implicitly, and unknown values are rejected rather than coerced.
        """

        if not isinstance(armed, bool):
            raise FlightModelError("armed must be bool")
        if not isinstance(reason, str) or not reason.strip():
            raise FlightModelError("arm reason must be a non-empty string")
        self._state.armed = armed
        self._record_command("ARM" if armed else "DISARM", accepted=True, reason=reason.strip())
        self._last_snapshot = self._snapshot()
        return True

    def arm(self, *, reason: str = "command") -> bool:
        return self.set_armed(True, reason=reason)

    def disarm(self, *, reason: str = "command") -> bool:
        return self.set_armed(False, reason=reason)

    def set_link_healthy(self, value: bool | None) -> None:
        if value is not None and not isinstance(value, bool):
            raise FlightModelError("link_healthy must be bool or None")
        self._state.link_healthy = value
        self._last_snapshot = self._snapshot()

    def set_gps_status(
        self,
        *,
        fix_type: int | None,
        horizontal_accuracy_m: Real,
        vertical_accuracy_m: Real,
        satellites_visible: int | None,
    ) -> None:
        if fix_type is not None and (
            isinstance(fix_type, bool)
            or not isinstance(fix_type, int)
            or not MAVLINK_GPS_FIX_TYPE_MIN <= fix_type <= MAVLINK_GPS_FIX_TYPE_MAX
        ):
            raise FlightModelError(
                "gps fix_type must be an integer within "
                f"[{MAVLINK_GPS_FIX_TYPE_MIN}, {MAVLINK_GPS_FIX_TYPE_MAX}] or None"
            )
        hacc = _finite_or_nan(horizontal_accuracy_m, "horizontal_accuracy_m")
        vacc = _finite_or_nan(vertical_accuracy_m, "vertical_accuracy_m")
        if math.isfinite(hacc) and hacc < 0.0:
            raise FlightModelError("horizontal_accuracy_m cannot be negative")
        if math.isfinite(vacc) and vacc < 0.0:
            raise FlightModelError("vertical_accuracy_m cannot be negative")
        if satellites_visible is not None and (
            isinstance(satellites_visible, bool)
            or not isinstance(satellites_visible, int)
            or not 0 <= satellites_visible <= MAVLINK_SATELLITES_UNKNOWN
        ):
            raise FlightModelError(
                "satellites_visible must be an integer within "
                f"[0, {MAVLINK_SATELLITES_UNKNOWN}] or None"
            )
        self._state.gps_fix_type = fix_type
        self._state.gps_horizontal_accuracy_m = hacc
        self._state.gps_vertical_accuracy_m = vacc
        self._state.satellites_visible = satellites_visible
        self._last_snapshot = self._snapshot()

    def set_battery_remaining(self, value: Real) -> None:
        remaining = _finite(value, "battery_remaining_pct")
        if not 0.0 <= remaining <= 100.0:
            raise FlightModelError("battery_remaining_pct must be within [0, 100]")
        self._state.battery_remaining_pct = remaining
        self._last_snapshot = self._snapshot()

    def set_mode(self, mode: str | int, *, reason: str = "command") -> bool:
        """Update autopilot mode from a SET_MODE-like command."""

        if isinstance(mode, bool) or not isinstance(mode, (str, int)):
            self._record_command("SET_MODE", accepted=False, reason="invalid_mode")
            return False
        if isinstance(mode, int):
            if mode < 0:
                self._record_command("SET_MODE", accepted=False, reason="invalid_mode")
                return False
            normalized = f"CUSTOM_{mode}"
        else:
            normalized = mode.strip().upper()
            if not normalized:
                self._record_command("SET_MODE", accepted=False, reason="invalid_mode")
                return False
        self._state.flight_mode = normalized
        self._record_command("SET_MODE", accepted=True, reason=reason, mode=normalized)
        self._last_snapshot = self._snapshot()
        return True

    def command_long(
        self,
        command: int,
        param1: Real = 0.0,
        param2: Real = 0.0,
        param3: Real = 0.0,
        param4: Real = 0.0,
        param5: Real = 0.0,
        param6: Real = 0.0,
        param7: Real = 0.0,
    ) -> bool:
        """Apply the small lifecycle command subset used by QGC/PX4.

        Unknown commands are recorded and rejected (fail-closed), never
        treated as an implicit arm or movement command.
        """

        del param2, param3, param4, param5, param6, param7
        if isinstance(command, bool) or not isinstance(command, int):
            self._record_command("UNKNOWN", accepted=False, reason="invalid_command")
            return False
        value = _finite(param1, "param1")
        if command == MAV_CMD_COMPONENT_ARM_DISARM:
            if math.isclose(value, 1.0, abs_tol=1e-6):
                return self.set_armed(True, reason="MAV_CMD_COMPONENT_ARM_DISARM")
            if math.isclose(value, 0.0, abs_tol=1e-6):
                return self.set_armed(False, reason="MAV_CMD_COMPONENT_ARM_DISARM")
            self._record_command("ARM", accepted=False, reason="invalid_param1")
            return False
        if command == MAV_CMD_DO_VTOL_TRANSITION:
            if self.mode is not FlightMode.VTOL:
                # A plain multirotor or fixed-wing airframe has no VTOL
                # transition state.  Keep the lifecycle command visible in
                # evidence, but fail closed without mutating flight mode.
                self._record_command(
                    "VTOL_TRANSITION",
                    accepted=False,
                    reason="unsupported_for_mode",
                    mode=self.mode.value,
                )
                return False
            # MAV_VTOL_STATE_MC=3 and MAV_VTOL_STATE_FW=4.
            if math.isclose(value, 3.0, abs_tol=1e-6):
                self._state.flight_mode = "MC"
            elif math.isclose(value, 4.0, abs_tol=1e-6):
                self._state.flight_mode = "FW"
            else:
                self._record_command("VTOL_TRANSITION", accepted=False, reason="invalid_state")
                return False
            self._record_command(
                "VTOL_TRANSITION", accepted=True, target_state=self._state.flight_mode
            )
            self._last_snapshot = self._snapshot()
            return True
        if command == MAV_CMD_NAV_RETURN_TO_LAUNCH:
            return self.set_mode("RTL", reason="MAV_CMD_NAV_RETURN_TO_LAUNCH")
        if command == MAV_CMD_NAV_LAND:
            return self.set_mode("LAND", reason="MAV_CMD_NAV_LAND")
        if command == MAV_CMD_MISSION_START:
            self._state.mission_started = True
            self._record_command("MISSION_START", accepted=True)
            self._last_snapshot = self._snapshot()
            return True
        self._record_command("UNKNOWN", accepted=False, command=command)
        return False

    def step(
        self,
        control_setpoint: FlightControlSetpoint | Mapping[str, object] | Any,
        dt: Real,
    ) -> FlightTelemetrySnapshot:
        """Advance the model and return one truth-free telemetry snapshot."""

        delta = _finite(dt, "dt")
        if delta <= 0.0 or delta > self.config.max_dt_s:
            raise FlightModelError(f"dt must be within (0, {self.config.max_dt_s:g}]")
        control = _coerce_control(control_setpoint)
        if control.timestamp_s is not None:
            if self._last_timestamp_s is not None and control.timestamp_s < self._last_timestamp_s:
                raise FlightModelError("control timestamp moved backwards")
            self._last_timestamp_s = control.timestamp_s
        self._integrate(control, delta)
        self._state.time_s += delta
        self._state.step_count += 1
        self._last_snapshot = self._snapshot()
        return self._last_snapshot

    def evidence(self) -> dict[str, object]:
        """Return canonical, truth-free state evidence (without its hash)."""

        return {
            "schema_version": 1,
            "model": "deterministic-flight-model-v1",
            "mode": self.mode.value,
            "seed": self.seed,
            "wind_mps": [self.wind_east_mps, self.wind_north_mps],
            "time_s": self._state.time_s,
            "step_count": self._state.step_count,
            "state": {
                "altitude_m": self.altitude_m,
                "airspeed_mps": self.airspeed_mps,
                "roll_deg": self.roll_deg,
                "pitch_deg": self.pitch_deg,
                "yaw_deg": self.yaw_deg,
                "latitude_deg": self.latitude_deg,
                "longitude_deg": self.longitude_deg,
                "armed": self.armed,
                "link_healthy": self.link_healthy,
                "flight_mode": self.flight_mode,
                "mission_started": self.mission_started,
            },
            "commands": list(self.command_history),
            "telemetry": self.telemetry.to_mapping(),
            "truth_free": True,
        }

    @property
    def evidence_hash(self) -> str:
        """SHA-256 of canonical JSON evidence."""

        encoded = json.dumps(
            self.evidence(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def evidence_with_hash(self) -> dict[str, object]:
        result = self.evidence()
        result["sha256"] = self.evidence_hash
        return result

    def _integrate(self, control: FlightControlSetpoint, dt: float) -> None:
        cfg = self.config
        # A disarmed vehicle must not drift or accept actuator output.  Keep
        # the deterministic fixture stationary until an explicit ARM command;
        # this also makes accidental AI startup immediately observable in
        # telemetry without simulating a hidden launch sequence.
        if not self._state.armed:
            self._state.speed_mps = _approach(self._state.speed_mps, 0.0, 16.0 * dt)
            self._state.roll_deg = _approach(self._state.roll_deg, 0.0, 100.0 * dt)
            self._state.pitch_deg = _approach(self._state.pitch_deg, 0.0, 80.0 * dt)
            return
        if self.mode is FlightMode.FIXED_WING:
            minimum = min(8.0, cfg.max_speed_mps)
            target_speed = minimum + control.throttle * (cfg.max_speed_mps - minimum)
            vertical_speed = self._state.speed_mps * math.sin(math.radians(self._state.pitch_deg))
        else:
            target_speed = control.throttle * cfg.max_speed_mps
            vertical_speed = (control.throttle - 0.5) * cfg.max_vertical_speed_mps
            vertical_speed += control.pitch * cfg.max_vertical_speed_mps * 0.5
        acceleration = 10.0 if self.mode is FlightMode.FIXED_WING else 16.0
        self._state.speed_mps = _approach(self._state.speed_mps, target_speed, acceleration * dt)
        self._state.roll_deg = _approach(
            self._state.roll_deg, control.roll * cfg.max_roll_deg, 100.0 * dt
        )
        self._state.pitch_deg = _approach(
            self._state.pitch_deg, control.pitch * cfg.max_pitch_deg, 80.0 * dt
        )
        self._state.yaw_deg = _wrap_heading(
            self._state.yaw_deg + control.yaw * cfg.max_yaw_rate_deg_s * dt
        )
        if self.mode is FlightMode.FIXED_WING:
            vertical_speed = self._state.speed_mps * math.sin(math.radians(self._state.pitch_deg))
        self._state.altitude_m = _clamp(
            self._state.altitude_m
            + _clamp(
                vertical_speed,
                -cfg.max_vertical_speed_mps,
                cfg.max_vertical_speed_mps,
            )
            * dt,
            0.0,
            cfg.max_altitude_m,
        )
        east, north = self._velocity_components()
        self._state.east_m += east * dt
        self._state.north_m += north * dt
        self._bound_coordinates()

    def _velocity_components(self) -> tuple[float, float]:
        if not self._state.armed:
            return 0.0, 0.0
        direction = math.radians(self._state.yaw_deg)
        # Heading is compass-referenced (0=N, 90=E), matching MAVLink/QGC.
        return (
            self._state.speed_mps * math.sin(direction) + self.wind_east_mps,
            self._state.speed_mps * math.cos(direction) + self.wind_north_mps,
        )

    def _bound_coordinates(self) -> None:
        # Keep malformed or unbounded synthetic runs inside the direct-solve
        # domain.  The endpoint is still checked below so a path that would
        # cross a geographic pole fails closed instead of producing an
        # apparently valid latitude with an undefined local longitude axis.
        max_direct_distance_m = math.pi * WGS84_SEMI_MINOR_AXIS_M
        distance_m = math.hypot(self._state.north_m, self._state.east_m)
        if distance_m > max_direct_distance_m:
            scale = max_direct_distance_m / distance_m
            self._state.north_m *= scale
            self._state.east_m *= scale
        try:
            wgs84_forward_from_ned(
                latitude_deg=self.config.initial_latitude_deg,
                longitude_deg=self.config.initial_longitude_deg,
                north_offset_m=self._state.north_m,
                east_offset_m=self._state.east_m,
            )
        except ValueError as exc:
            raise FlightModelError(f"state geodetic position is invalid: {exc}") from exc

    def _snapshot(self) -> FlightTelemetrySnapshot:
        # The deterministic model emits a sensor observation on every
        # snapshot.  Keep the GPS sample timestamp independent from the
        # estimator/position timestamp exposed by the base contract: only a
        # complete, qualified raw-GPS tuple is stamped with the model clock;
        # a degraded/missing tuple is explicitly unavailable.  This mirrors
        # the REAL outdoor GPS gate without consulting simulator truth.
        latitude_deg, longitude_deg = self._geodetic_position()
        gps_observed_at_s: float | None = None
        if (
            self._state.gps_fix_type is not None
            # Fix types 0..2 are no-fix/2-D/degraded observations.  Keep the
            # timestamp only for a complete 3-D-or-better GPS sample, which
            # matches the REAL Pixhawk provider's raw-GPS cursor semantics.
            and self._state.gps_fix_type >= MAVLINK_QUALIFIED_FIX_TYPE_MIN
            and self._state.satellites_visible is not None
            and 0 <= self._state.satellites_visible <= MAVLINK_SATELLITES_MAX
            and math.isfinite(self._state.gps_horizontal_accuracy_m)
            and self._state.gps_horizontal_accuracy_m >= 0.0
            and math.isfinite(self._state.gps_vertical_accuracy_m)
            and self._state.gps_vertical_accuracy_m >= 0.0
            and math.isfinite(latitude_deg)
            and math.isfinite(longitude_deg)
            and -90.0 < latitude_deg < 90.0
            and -180.0 <= longitude_deg <= 180.0
        ):
            gps_observed_at_s = self.time_s
        return FlightTelemetrySnapshot(
            observed_at_s=self.time_s,
            altitude_agl_m=self.altitude_m,
            heading_deg=self.yaw_deg,
            ground_speed_mps=self.ground_speed_mps,
            latitude_deg=latitude_deg,
            longitude_deg=longitude_deg,
            gps_fix_type=self._state.gps_fix_type,
            gps_horizontal_accuracy_m=self._state.gps_horizontal_accuracy_m,
            gps_vertical_accuracy_m=self._state.gps_vertical_accuracy_m,
            satellites_visible=self._state.satellites_visible,
            link_healthy=self._state.link_healthy,
            armed=self._state.armed,
            battery_remaining_pct=self._state.battery_remaining_pct,
            mode=self.mode.value,
            roll_deg=self.roll_deg,
            pitch_deg=self.pitch_deg,
            yaw_deg=self.yaw_deg,
            airspeed_mps=self.airspeed_mps,
            wind_east_mps=self.wind_east_mps,
            wind_north_mps=self.wind_north_mps,
            step_count=self._state.step_count,
            flight_mode=self._state.flight_mode,
            mission_started=self._state.mission_started,
            gps_observed_at_s=gps_observed_at_s,
        )

    def _record_command(self, name: str, *, accepted: bool, **details: object) -> None:
        self._command_history.append({"name": name, "accepted": bool(accepted), **details})


def _coerce_control(value: object) -> FlightControlSetpoint:
    if isinstance(value, FlightControlSetpoint):
        return value
    if isinstance(value, Mapping):
        return FlightControlSetpoint(
            roll=value.get("roll", 0.0),
            pitch=value.get("pitch", 0.0),
            yaw=value.get("yaw", 0.0),
            throttle=value.get("throttle", 0.0),
            timestamp_s=value.get("timestamp_s", value.get("timestamp")),
            source=value.get("source", "sim"),
        )
    try:
        return FlightControlSetpoint(
            roll=value.roll,
            pitch=value.pitch,
            yaw=value.yaw,
            throttle=value.throttle,
            timestamp_s=getattr(value, "timestamp_s", None),
            source=getattr(value, "source", "sim"),
        )
    except AttributeError as exc:
        raise FlightModelError(
            "control setpoint must expose roll, pitch, yaw and throttle"
        ) from exc


def _validate_axes(roll: object, pitch: object, yaw: object, throttle: object) -> None:
    for name, value in (("roll", roll), ("pitch", pitch), ("yaw", yaw)):
        if not -1.0 <= _finite(value, name) <= 1.0:
            raise FlightModelError(f"{name} must be within [-1, 1]")
    if not 0.0 <= _finite(throttle, "throttle") <= 1.0:
        raise FlightModelError("throttle must be within [0, 1]")


def _wind(value: Sequence[Real]) -> tuple[float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise FlightModelError("wind_mps must contain east and north components")
    return _finite(value[0], "wind_east_mps"), _finite(value[1], "wind_north_mps")


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)):
        raise FlightModelError(f"{name} must be finite numeric")
    return float(value)


def _finite_or_nan(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise FlightModelError(f"{name} must be finite numeric or NaN")
    converted = float(value)
    if not math.isfinite(converted) and not math.isnan(converted):
        raise FlightModelError(f"{name} must be finite numeric or NaN")
    return converted


def _approach(current: float, target: float, delta: float) -> float:
    if current < target:
        return min(target, current + delta)
    return max(target, current - delta)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _wrap_heading(value: float) -> float:
    return value % 360.0


def _copy_state(state: _FlightState) -> _FlightState:
    return _FlightState(
        state.time_s,
        state.east_m,
        state.north_m,
        state.altitude_m,
        state.speed_mps,
        state.roll_deg,
        state.pitch_deg,
        state.yaw_deg,
        state.step_count,
        state.armed,
        state.link_healthy,
        state.gps_fix_type,
        state.gps_horizontal_accuracy_m,
        state.gps_vertical_accuracy_m,
        state.satellites_visible,
        state.battery_remaining_pct,
        state.flight_mode,
        state.mission_started,
    )


FlightModel = DeterministicFlightModel
SimulatorFlightModel = DeterministicFlightModel
SimulatorTelemetrySnapshot = FlightTelemetrySnapshot

__all__ = [
    "DeterministicFlightModel",
    "FlightControlSetpoint",
    "FlightMode",
    "FlightModel",
    "FlightModelConfig",
    "FlightModelError",
    "FlightTelemetrySnapshot",
    "MAV_CMD_COMPONENT_ARM_DISARM",
    "MAV_CMD_DO_VTOL_TRANSITION",
    "MAV_CMD_MISSION_START",
    "MAV_CMD_NAV_LAND",
    "MAV_CMD_NAV_RETURN_TO_LAUNCH",
    "SimulatorFlightModel",
    "SimulatorTelemetrySnapshot",
]
