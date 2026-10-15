"""Fail-closed PX4 admission checks for the real-aircraft runtime.

The gate is deliberately transport agnostic.  A fake vehicle implementing
``read_parameter``/``write_parameter`` and ``snapshot`` is enough for tests.
"""
# ruff: noqa: E501

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from multidetect.autonomy.mission_package import MissionPackage

_ACTION_RANK = {"continue": 0, "hold": 1, "return": 2, "land": 3, "terminate": 4}
_PARAMS = ("NAV_DLL_ACT", "COM_DL_LOSS_T", "NAV_RCL_ACT", "COM_RC_IN_MODE",
           "COM_OF_LOSS_T", "COM_OBL_RC_ACT", "GF_ACTION", "GF_MAX_HOR_DIST",
           "GF_MAX_VER_DIST", "BAT_LOW_THR", "BAT_CRIT_THR", "COM_LOW_BAT_ACT")
_PARAM_ACTIONS = {
    "NAV_DLL_ACT": {0: 0, 1: 1, 2: 2, 3: 3, 5: 4, 6: 4},
    "NAV_RCL_ACT": {1: 1, 2: 2, 3: 3, 5: 4, 6: 4},
    "COM_OBL_RC_ACT": {0: 0, 1: 0, 2: 0, 3: 2, 4: 3, 5: 1, 6: 4, 7: 4},
    "GF_ACTION": {0: 0, 1: 0, 2: 1, 3: 2, 4: 4, 5: 3},
}


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    passed: bool
    blocking: bool
    values: Mapping[str, Any] = field(default_factory=dict)
    expected: Mapping[str, Any] = field(default_factory=dict)
    explanation_en: str = ""
    explanation_zh: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "blocking": self.blocking,
                "values": dict(self.values), "expected": dict(self.expected),
                "explanation_en": self.explanation_en, "explanation_zh": self.explanation_zh}


def _check(name, passed, values, expected, en, zh, *, blocking=True):
    return PreflightCheck(name, bool(passed), blocking, values, expected, en, zh)


def _policy(package: MissionPackage | Mapping[str, Any]) -> tuple[str, float, Mapping[str, Any]]:
    document = package.document if isinstance(package, MissionPackage) else package
    raw = document["link_loss_policy"]
    return str(raw["action"]), float(raw.get("timeout_s", 0)), document


def build_parameter_matrix(package: MissionPackage | Mapping[str, Any]) -> dict[str, Any]:
    """Return expected PX4 values for the package, without touching the vehicle."""
    action, timeout, document = _policy(package)
    area, budget = document["operating_area"], document["budget"]
    corners = [(area["north_min_m"], area["east_min_m"]),
               (area["north_min_m"], area["east_max_m"]),
               (area["north_max_m"], area["east_min_m"]),
               (area["north_max_m"], area["east_max_m"])]
    hor = max(math.hypot(n - document["home"][0], e - document["home"][1]) for n, e in corners)
    expected_dll = {"continue": 0, "continue_then_return": 0, "hold_then_return": 0,
                    "return_now": 2, "land_now": 3}[action]
    # COM_OF_LOSS_T is bounded by the package timer when one exists; zero is
    # valid for immediate onboard handling and remains a readback requirement.
    return {
        "NAV_DLL_ACT": expected_dll, "COM_DL_LOSS_T": max(5.0, timeout) if timeout else 5.0,
        "NAV_RCL_ACT": (3 if action == "land_now" else 2
                        if action in {"return_now", "continue_then_return"} else 1),
        "COM_RC_IN_MODE": None, "COM_OF_LOSS_T": timeout if timeout else 1.0,
        "COM_OBL_RC_ACT": 3 if action == "return_now" else 5 if action in {"continue", "continue_then_return", "hold_then_return"} else 4,
        "GF_ACTION": 5 if action == "land_now" else 3,
        "GF_MAX_HOR_DIST": hor, "GF_MAX_VER_DIST": area["max_altitude_m"],
        "BAT_LOW_THR": max(0.12, min(0.5, budget["reserve_energy_wh"] / budget["energy_wh"] + 0.05)),
        "BAT_CRIT_THR": max(0.05, min(0.5, budget["reserve_energy_wh"] / budget["energy_wh"])),
        "COM_LOW_BAT_ACT": 3 if action != "land_now" else 2,
        "link_loss_policy": action, "link_loss_timeout_s": timeout,
        "accepted_px4_overrides": document.get("accepted_px4_overrides", {}),
    }


class PX4PreflightGate:
    def __init__(self, vehicle: Any, package: MissionPackage | Mapping[str, Any], *,
                 timeout_s: float = 1.0, retries: int = 3, clock=time.monotonic):
        self.vehicle, self.package, self.timeout_s, self.retries, self.clock = vehicle, package, timeout_s, retries, clock
        self.checks: list[PreflightCheck] = []
        self.parameters: dict[str, float] = {}

    def _params(self) -> dict[str, float]:
        for name in _PARAMS:
            try:
                self.parameters[name] = float(self.vehicle.read_parameter(name, timeout_s=self.timeout_s, retries=self.retries))
            except Exception as exc:
                self.checks.append(_check("parameter_readback", False, {"parameter": name, "error": str(exc)},
                                          {"readable": True}, "PX4 parameter readback is unavailable.", "PX4 参数回读不可用。"))
                break
        return self.parameters

    def _health(self) -> None:
        state = self.vehicle.snapshot()
        now = self.clock()
        def get(name, default=None):
            return getattr(state, name, default)

        def fresh(stamp):
            return stamp is not None and 0 <= now - stamp <= self.timeout_s * 3

        # Gyro, accelerometer, magnetometer and GPS are the minimum sensor
        # health evidence needed by this gate; controller health is checked by
        # the runtime's arm/OFFBOARD confirmation.
        sensor_bits = (1 | 2 | 4 | 32)
        present, enabled, health = get("sensor_present"), get("sensor_enabled"), get("sensor_health")
        sensor_ok = (present is not None and enabled is not None and health is not None
                     and (int(present) & sensor_bits) == sensor_bits
                     and (int(enabled) & sensor_bits) == sensor_bits
                     and (int(health) & sensor_bits) == sensor_bits
                     and fresh(get("sys_status_monotonic")))
        self.checks.append(_check("sensor_health", sensor_ok,
            {"present": present, "enabled": enabled, "health": health},
            {"required_bits": sensor_bits}, "Required PX4 sensors are present, enabled and healthy.", "PX4 必需传感器已存在、启用且健康。"))
        estimator_flags = get("estimator_flags")
        flags = int(estimator_flags) if estimator_flags is not None else 0
        estimator_ok = (estimator_flags is not None and (flags & 0x07) == 0x07
                        and flags & 0x18 and flags & 0x60
                        and fresh(get("estimator_monotonic")))
        self.checks.append(_check("estimator", estimator_ok, {"flags": estimator_flags}, {"required_flags": "attitude+velocity+position"},
            "Estimator attitude, velocity and position flags are ready.", "估计器姿态、速度和位置标志已就绪。"))
        fix, satellites, eph = get("gps_fix_type"), get("gps_satellites"), get("gps_eph_m")
        gps_ok = (fix is not None and fix >= 3 and satellites is not None and satellites >= 6
                  and eph is not None and eph >= 0 and fresh(get("gps_monotonic")))
        self.checks.append(_check("gnss", gps_ok, {"fix_type": fix, "satellites": satellites, "eph_m": eph},
            {"fix_type": ">=3", "satellites": ">=6"}, "GNSS fix, satellites and eph are fresh and sufficient.", "GNSS 定位、卫星数和 eph 新鲜且满足要求。"))
        home_ned, home_geo = get("home_ned"), get("home_geodetic")
        home_ok = home_ned is not None and home_geo is not None and fresh(get("home_monotonic"))
        self.checks.append(_check("home", home_ok, {"home_ned": home_ned, "home_geodetic": home_geo}, {"known": True},
            "PX4 HOME_POSITION is known and fresh.", "PX4 HOME_POSITION 已知且新鲜。"))
        battery, battery_stamp = get("battery_remaining"), get("battery_monotonic")
        battery_ok = battery is not None and fresh(battery_stamp)
        self.checks.append(_check("battery_telemetry", battery_ok, {"remaining_pct": battery, "age_s": None if battery_stamp is None else now - battery_stamp}, {"fresh": True},
            "BATTERY_STATUS/SYS_STATUS telemetry is fresh.", "BATTERY_STATUS/SYS_STATUS 电池遥测新鲜。"))

    def _matrix(self, values: Mapping[str, float]) -> None:
        expected = build_parameter_matrix(self.package)
        action = expected["link_loss_policy"]
        overrides = expected.get("accepted_px4_overrides") or {}
        budget = (self.package.document if isinstance(self.package, MissionPackage)
                  else self.package)["budget"]
        reserve_fraction = budget["reserve_energy_wh"] / budget["energy_wh"]
        if reserve_fraction > 0.5:
            self.checks.append(_check("battery_reserve", False,
                {"reserve_fraction": reserve_fraction}, {"maximum": 0.5},
                "The mission reserve cannot be represented by PX4 battery thresholds.",
                "任务保留电量超过 PX4 阈值可表达的上限。"))
        for name, wanted in expected.items():
            if name.startswith("_") or name in {"link_loss_policy", "link_loss_timeout_s", "accepted_px4_overrides"}:
                continue
            actual = values.get(name)
            if actual is None:
                continue
            if name == "COM_RC_IN_MODE":
                ok = 0 <= actual <= 8 and float(actual).is_integer()
                self.checks.append(_check("parameter:COM_RC_IN_MODE", ok, {"read": actual},
                    {"allowed": "PX4 enum 0..8; operator-selected"},
                    "RC/MAVLink input mode is a valid operator-selected PX4 enum.",
                    "RC/MAVLink 输入模式是有效的 PX4 枚举，且由操作员选择。"))
                continue
            ok = math.isclose(actual, float(wanted), rel_tol=0, abs_tol=1e-3)
            if name in {"GF_MAX_HOR_DIST", "GF_MAX_VER_DIST"}:
                ok = actual >= float(wanted) and actual > 0
            if name == "BAT_LOW_THR":
                ok = (0.12 <= actual <= 0.5 and actual > float(values.get("BAT_CRIT_THR", 0))
                      and actual >= reserve_fraction)
            if name == "BAT_CRIT_THR":
                ok = 0.05 <= actual <= 0.5 and actual >= reserve_fraction
            if name == "COM_DL_LOSS_T":
                ok = 5 <= actual <= 300
            if name == "COM_OF_LOSS_T":
                ok = 0 <= actual <= 60
            if name == "GF_MAX_VER_DIST":
                ok = 0 < actual <= 10000 and actual >= float(wanted)
            if name == "GF_MAX_HOR_DIST":
                ok = 0 < actual <= 10000 and actual >= float(wanted)
            if not ok and name in overrides and name in _PARAM_ACTIONS:
                override = overrides[name]
                actual_rank = _PARAM_ACTIONS[name].get(int(actual)) if float(actual).is_integer() else None
                wanted_rank = _PARAM_ACTIONS[name].get(int(wanted)) if float(wanted).is_integer() else None
                ok = (isinstance(override, Mapping) and override.get("accepted") is True
                      and isinstance(override.get("reason"), str) and bool(override["reason"].strip())
                      and actual_rank is not None and wanted_rank is not None
                      and actual_rank >= wanted_rank)
            self.checks.append(_check("parameter:" + name, ok, {"read": actual}, {"expected": wanted},
                f"{name} is consistent with {action} and the operating envelope.", f"{name} 与 {action} 策略及运行范围一致。"))

    def run(self) -> dict[str, Any]:
        self.checks = []
        values = self._params()
        self._health()
        self._matrix(values)
        return self.report()

    def apply(self) -> dict[str, Any]:
        expected = build_parameter_matrix(self.package)
        invalid = []
        if not 5 <= float(expected["COM_DL_LOSS_T"]) <= 300:
            invalid.append("COM_DL_LOSS_T")
        if not 0 <= float(expected["COM_OF_LOSS_T"]) <= 60:
            invalid.append("COM_OF_LOSS_T")
        if not 0 < float(expected["GF_MAX_HOR_DIST"]) <= 10000:
            invalid.append("GF_MAX_HOR_DIST")
        if not 0 < float(expected["GF_MAX_VER_DIST"]) <= 10000:
            invalid.append("GF_MAX_VER_DIST")
        if invalid:
            self.checks = [_check("matrix_values", False, {"invalid": invalid},
                {"PX4_metadata_ranges": True},
                "The requested matrix contains values outside PX4 metadata ranges; no write was sent.",
                "请求矩阵超出 PX4 元数据范围，未发送任何写入。")]
            return self.report()
        for name, wanted in expected.items():
            if name in _PARAMS and wanted is not None:
                actual = self.vehicle.write_parameter(name, float(wanted), timeout_s=self.timeout_s, retries=self.retries)
                self.parameters[name] = actual
        return self.run()

    def report(self) -> dict[str, Any]:
        blocking = [c for c in self.checks if c.blocking and not c.passed]
        return {"passed": not blocking, "blocking_failures": [c.name for c in blocking],
                "parameters": dict(self.parameters), "checks": [c.as_dict() for c in self.checks]}

