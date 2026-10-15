"""Onboard, conservative multirotor energy estimates (Wh, W, metres, seconds).

Host receipt time is the only time domain. No PX4 time_remaining, simulated
clock, ground station or simulator power model is required.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


@dataclass(frozen=True)
class EnergyConfig:
    """Defaults are uncalibrated engineering floors, never SITL measurements."""

    capacity_wh: float
    reserve_energy_wh: float = 0
    reserve_time_s: float = 60
    nominal_voltage_v: float = 14.8
    cutoff_voltage_v: float = 13.2
    hover_power_w: float = 360
    translate_power_w: float = 400
    climb_power_w: float = 500
    cruise_speed_mps: float = 5
    climb_speed_mps: float = 2
    descent_speed_mps: float = 1
    landing_time_s: float = 15
    safety_factor: float = 1.3
    unknown_wind_mps: float = 2
    telemetry_timeout_s: float = 5
    power_window_s: float = 60
    battery_id: int = 0
    source_system: int = 1
    source_component: int = 1

    def __post_init__(self):
        for key, value in asdict(self).items():
            if _finite(value) is None or value < 0:
                raise ValueError(f"invalid energy configuration: {key}")
        for key in (
            "capacity_wh",
            "nominal_voltage_v",
            "hover_power_w",
            "translate_power_w",
            "climb_power_w",
            "cruise_speed_mps",
            "climb_speed_mps",
            "descent_speed_mps",
            "landing_time_s",
            "telemetry_timeout_s",
            "power_window_s",
        ):
            if getattr(self, key) <= 0:
                raise ValueError(f"energy configuration must be positive: {key}")
        if (
            self.cutoff_voltage_v >= self.nominal_voltage_v
            or self.safety_factor < 1
            or self.unknown_wind_mps >= self.cruise_speed_mps
            or self.reserve_energy_wh >= self.capacity_wh
        ):
            raise ValueError("invalid energy reserve, voltage, wind or safety factor")
        for key in ("battery_id", "source_system", "source_component"):
            if not isinstance(getattr(self, key), int):
                raise ValueError(f"{key} must be an integer")

    @classmethod
    def from_budget(cls, budget: Mapping, options: Mapping | None = None):
        values = {
            "capacity_wh": budget["energy_wh"],
            "reserve_energy_wh": budget["reserve_energy_wh"],
            "reserve_time_s": budget["reserve_time_s"],
            **(options or {}),
        }
        # Per-aircraft calibration cannot relax the uploaded mission reserves.
        values["reserve_energy_wh"] = max(values["reserve_energy_wh"], budget["reserve_energy_wh"])
        values["reserve_time_s"] = max(values["reserve_time_s"], budget["reserve_time_s"])
        return cls(**values)


class OnboardEnergyEstimator:
    def __init__(self, config: EnergyConfig):
        self.config = config
        self._last_time: float | None = None
        self._battery_at: float | None = None
        self._battery_message_at = -math.inf
        self._remaining: float | None = None
        self._voltage: float | None = None
        self._sag_factor = 1.0
        self._power: float | None = None
        self._consumed: float | None = None
        self._counter_energy: float | None = None
        self._wind: tuple[float, float, float] | None = None
        self._velocity: tuple[float, float, float, float] | None = None
        self._armed: tuple[bool, float] | None = None
        self._seen: dict[str, float] = {}
        self._powers = {phase: deque(maxlen=4096) for phase in ("hover", "translate", "climb")}

    def phase(self, now_s: float) -> str:
        if self._velocity and now_s - self._velocity[3] <= self.config.telemetry_timeout_s:
            n, e, down, _ = self._velocity
            if down < -0.5:
                return "climb"
            if math.hypot(n, e) > 1:
                return "translate"
        return "hover"

    def powers(self, now_s: float) -> dict[str, float]:
        result = {}
        for phase, values in self._powers.items():
            while values and now_s - values[0][0] > self.config.power_window_s:
                values.popleft()
            floor = getattr(self.config, f"{phase}_power_w")
            result[phase] = max(floor, sum(p for _, p in values) / len(values)) if values else floor
        return result

    def _advance(self, now_s: float):
        if _finite(now_s) is None or (self._last_time is not None and now_s < self._last_time):
            raise ValueError("invalid energy clock")
        if self._last_time is not None and self._remaining is not None:
            power = max(self.powers(now_s).values())
            if self._power is not None:
                power = max(power, self._power)
            self._remaining = max(0, self._remaining - power * (now_s - self._last_time) / 3600)
        self._last_time = now_s

    def ingest(self, message: Mapping[str, Any] | Any, *, received_at_s: float) -> bool:
        """Accept decoded MAVLink or mapping once; select the configured pack/source.

        Callers must preserve original local receipt timestamps, including for
        cached snapshots. Unknown/sentinel fields never become zero readings.
        """
        if not isinstance(message, Mapping):
            row = message.to_dict()
            row.update(
                source_system=message.get_srcSystem(), source_component=message.get_srcComponent()
            )
            message = row
        kind = message.get("mavpackettype", message.get("type"))
        if kind not in {
            "BATTERY_STATUS",
            "SYS_STATUS",
            "WIND_COV",
            "LOCAL_POSITION_NED",
            "HEARTBEAT",
        }:
            return False
        for field, expected in (
            ("source_system", self.config.source_system),
            ("source_component", self.config.source_component),
        ):
            if message.get(field, expected) != expected:
                return False
        if kind == "BATTERY_STATUS" and message.get("id", 0) != self.config.battery_id:
            return False
        stamp = _finite(received_at_s)
        if stamp is None or stamp <= self._seen.get(kind, -math.inf):
            return False
        if kind in {"BATTERY_STATUS", "SYS_STATUS"} and stamp < self._battery_message_at:
            return False
        # A cached snapshot may arrive after an evaluation. Preserve its actual
        # age and never rewind the integration clock or refresh a duplicate.
        self._advance(max(stamp, self._last_time or stamp))
        self._seen[kind] = stamp
        if kind == "LOCAL_POSITION_NED":
            velocity = tuple(_finite(message.get(k)) for k in ("vx", "vy", "vz"))
            if all(v is not None for v in velocity):
                self._velocity = (*velocity, stamp)
            return True
        if kind == "HEARTBEAT":
            mode = _finite(message.get("base_mode"))
            if mode is not None:
                self._armed = (bool(int(mode) & 128), stamp)
            return True
        if kind == "WIND_COV":
            n, e = (_finite(message.get(k)) for k in ("wind_x", "wind_y"))
            if n is not None and e is not None:
                self._wind = (n, e, stamp)
            return True
        self._battery_message_at = stamp
        voltage = None
        if kind == "BATTERY_STATUS":
            cells = message.get("voltages", [])
            if isinstance(cells, (list, tuple)):
                valid = [_finite(v) for v in cells]
                valid = [v for v in valid if v is not None and 0 < v < 65535]
                if valid:
                    voltage = sum(valid) / 1000
        else:
            raw = _finite(message.get("voltage_battery"))
            if raw is not None and 0 < raw < 65535:
                voltage = raw / 1000
        pct = _finite(message.get("battery_remaining"))
        consumed = _finite(message.get("current_consumed")) if kind == "BATTERY_STATUS" else None
        current = _finite(message.get("current_battery"))
        observed_power = (
            voltage * current / 100
            if voltage is not None and current is not None and current >= 0
            else 0
        )
        lag_drain = (
            max(*self.powers(self._last_time).values(), observed_power, self._power or 0)
            * (self._last_time - stamp)
            / 3600
        )
        if pct is not None and 0 <= pct <= 100:
            energy = max(0, self.config.capacity_wh * pct / 100 - lag_drain)
            self._remaining = energy if self._remaining is None else min(self._remaining, energy)
            self._battery_at = stamp
        if consumed is not None and consumed >= 0:
            counter_bound = max(
                0,
                self.config.capacity_wh
                - consumed * self.config.nominal_voltage_v / 1000
                - lag_drain,
            )
            # A counter starts at boot, which need not be a fully charged pack.
            # Require a SOC anchor before treating it as remaining energy.
            if self._remaining is not None:
                self._remaining = min(self._remaining, counter_bound)
                self._battery_at = stamp
            if (
                self._consumed is not None
                and consumed >= self._consumed
                and self._remaining is not None
            ):
                # Both current integration and counter constrain remaining energy;
                # the integration is already deducted, so don't deduct it twice.
                delta = (consumed - self._consumed) * self.config.nominal_voltage_v / 1000
                if self._counter_energy is not None:
                    self._counter_energy = max(0, self._counter_energy - delta)
                    self._remaining = min(self._remaining, self._counter_energy)
                self._battery_at = stamp
            self._consumed = consumed
            self._counter_energy = self._remaining
        if voltage is not None:
            self._voltage = voltage
            self._sag_factor = min(
                self._sag_factor,
                max(
                    0,
                    min(
                        1,
                        (voltage - self.config.cutoff_voltage_v)
                        / (self.config.nominal_voltage_v - self.config.cutoff_voltage_v),
                    ),
                ),
            )
        if voltage is not None and current is not None and current >= 0:
            self._power = voltage * current / 100
            self._battery_at = stamp if self._remaining is not None else self._battery_at
            if (
                self._armed
                and self._armed[0]
                and stamp - self._armed[1] <= self.config.telemetry_timeout_s
                and self._velocity
                and stamp - self._velocity[3] <= self.config.telemetry_timeout_s
            ):
                self._powers[self.phase(stamp)].append((stamp, self._power))
        return True

    def estimate(
        self,
        *,
        now_s: float,
        pose_ned,
        home_ne,
        return_altitude_m: float,
        mission_remaining_s: float,
    ) -> dict[str, Any]:
        self._advance(now_s)
        c = self.config
        powers = self.powers(now_s)
        result = {
            "source": "mission_time_budget_only",
            "reason": "no usable battery data; mission package time budget only",
            "remaining_energy_wh": None,
            "usable_energy_wh": None,
            "return_energy_wh": None,
            "reserve_energy_wh": None,
            "time_to_reserve_s": max(0, mission_remaining_s - c.reserve_time_s),
            "action_margin_wh": None,
            "action_margin_s": mission_remaining_s - c.reserve_time_s,
            "action": "continue",
            "phase_power_w": powers,
        }
        if self._remaining is None:
            return result
        stale = self._battery_at is None or now_s - self._battery_at > c.telemetry_timeout_s
        # Voltage sag can tighten, but never restore, the coulomb/SOC bound.
        sag = self._sag_factor
        remaining = self._remaining * sag
        reserve = max(c.reserve_energy_wh, max(powers.values()) * c.reserve_time_s / 3600)
        active_power = max(powers.values(), default=c.climb_power_w)
        result.update(
            source="onboard_battery_model",
            remaining_energy_wh=remaining,
            usable_energy_wh=max(0, remaining - reserve),
            reserve_energy_wh=reserve,
            time_to_reserve_s=max(0, (remaining - reserve) * 3600 / active_power),
            battery_age_s=None if self._battery_at is None else now_s - self._battery_at,
            voltage_v=self._voltage,
            voltage_sag_factor=sag,
            reason="battery SOC/counter/current bound with conservative phase power",
        )
        if pose_ned is None:
            result.update(
                action="return_now", reason="battery available but return geometry missing"
            )
            return result
        if len(pose_ned) != 3 or any(_finite(v) is None for v in pose_ned):
            raise ValueError("invalid energy pose")
        dn, de = home_ne[0] - pose_ned[0], home_ne[1] - pose_ned[1]
        distance = math.hypot(dn, de)
        altitude = max(0, -pose_ned[2])
        climb = max(0, return_altitude_m - altitude)
        descent = max(altitude, return_altitude_m)
        ground_speed = c.cruise_speed_mps - c.unknown_wind_mps
        wind_source = "configured_unknown_wind_allowance"
        if self._wind and now_s - self._wind[2] <= c.telemetry_timeout_s and distance:
            wn, we, _ = self._wind
            tail = (wn * dn + we * de) / distance
            cross = abs(wn * de - we * dn) / distance
            ground_speed = math.sqrt(max(0, c.cruise_speed_mps**2 - cross**2)) + min(0, tail)
            wind_source = "WIND_COV"
        result.update(
            distance_home_m=distance,
            climb_m=climb,
            descent_m=descent,
            return_ground_speed_mps=max(0, ground_speed),
            wind_source=wind_source,
        )
        if ground_speed <= 0 and distance > 0:
            result.update(action="land_now", reason="wind exceeds modeled return capability")
            return result
        horizontal_s = distance / max(0.01, ground_speed)
        return_s = (
            horizontal_s
            + climb / c.climb_speed_mps
            + descent / c.descent_speed_mps
            + c.landing_time_s
        )
        rtl = (
            c.safety_factor
            * (
                powers["translate"] * horizontal_s
                + powers["climb"] * climb / c.climb_speed_mps
                + powers["hover"] * (descent / c.descent_speed_mps + c.landing_time_s)
            )
            / 3600
        )
        margin = remaining - reserve - rtl
        result.update(
            return_energy_wh=rtl,
            return_time_s=return_s,
            action_margin_wh=margin,
            action_margin_s=margin * 3600 / active_power,
        )
        if remaining < rtl:
            result.update(action="land_now", reason="remaining energy insufficient for return_home")
        elif margin <= 0 or stale:
            result.update(
                action="return_now",
                reason="energy reserve reached"
                if not stale
                else "battery telemetry stale; return while feasible",
            )
        return result
