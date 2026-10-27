"""Fail-closed PX4 admission checks for the real-aircraft runtime."""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from multidetect.autonomy.mission_package import MissionPackage

_PARAMS = (
    "NAV_DLL_ACT", "COM_DL_LOSS_T", "NAV_RCL_ACT", "COM_RC_IN_MODE",
    "COM_OF_LOSS_T", "COM_OBL_RC_ACT", "GF_ACTION", "GF_MAX_HOR_DIST",
    "GF_MAX_VER_DIST", "BAT_LOW_THR", "BAT_CRIT_THR", "COM_LOW_BAT_ACT",
)


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
        return {
            "name": self.name, "passed": self.passed, "blocking": self.blocking,
            "values": dict(self.values), "expected": dict(self.expected),
            "explanation_en": self.explanation_en, "explanation_zh": self.explanation_zh,
        }


def _check(name: str, passed: bool, values: Mapping[str, Any], expected: Mapping[str, Any],
           en: str, zh: str, *, blocking: bool = True) -> PreflightCheck:
    return PreflightCheck(name, bool(passed), blocking, values, expected, en, zh)


def build_parameter_matrix(package: MissionPackage | Mapping[str, Any]) -> dict[str, Any]:
    document = package.document if isinstance(package, MissionPackage) else package
    policy = document["link_loss_policy"]
    action = policy["action"]
    timeout = float(policy.get("timeout_s", 0))
    area = document["operating_area"]
    budget = document["budget"]
    corners = [(area["north_min_m"], area["east_min_m"]),
               (area["north_min_m"], area["east_max_m"]),
               (area["north_max_m"], area["east_min_m"]),
               (area["north_max_m"], area["east_max_m"])]
    horizontal = max(math.hypot(n - document["home"][0], e - document["home"][1])
                     for n, e in corners)
    matrix = {
        "NAV_DLL_ACT": {"continue": 0, "continue_then_return": 0,
                         "hold_then_return": 0, "return_now": 2, "land_now": 3}[action],
        "COM_DL_LOSS_T": max(5, math.ceil(timeout)) if timeout else 5,
        "NAV_RCL_ACT": None,
        "COM_RC_IN_MODE": None,
        "COM_OF_LOSS_T": timeout or 1.0,
        "COM_OBL_RC_ACT": document.get("px4_offboard_loss_action"),
        "GF_ACTION": 5 if action == "land_now" else 3,
        "GF_MAX_HOR_DIST": horizontal,
        "GF_MAX_VER_DIST": area["max_altitude_m"],
        "BAT_LOW_THR": None,
        "BAT_CRIT_THR": None,
        "COM_LOW_BAT_ACT": None,
        "link_loss_policy": action,
        "link_loss_timeout_s": timeout,
        "accepted_px4_overrides": document.get("accepted_px4_overrides", {}),
    }
    capacity = budget.get("battery_capacity_wh")
    if capacity is not None:
        reserve = budget["reserve_energy_wh"] / capacity
        matrix["BAT_LOW_THR"] = max(0.12, min(0.5, reserve + 0.05))
        matrix["BAT_CRIT_THR"] = max(0.05, min(0.5, reserve))
    return matrix


class PX4PreflightGate:
    def __init__(self, vehicle: Any, package: MissionPackage | Mapping[str, Any], *,
                 timeout_s: float = 1.0, retries: int = 3,
                 clock=time.monotonic) -> None:
        self.vehicle = vehicle
        self.package = package
        self.timeout_s = timeout_s
        self.retries = retries
        self.clock = clock
        self.checks: list[PreflightCheck] = []
        self.parameters: dict[str, float] = {}

    def _params(self) -> dict[str, float]:
        for name in _PARAMS:
            try:
                self.parameters[name] = float(self.vehicle.read_parameter(
                    name, timeout_s=self.timeout_s, retries=self.retries))
            except Exception as exc:
                self.checks.append(_check("parameter_readback", False,
                    {"parameter": name, "error": str(exc)}, {"readable": True},
                    "PX4 parameter readback is unavailable.", "PX4 参数回读不可用。"))
                break
        return self.parameters

    def _health(self) -> None:
        state = self.vehicle.snapshot()
        now = self.clock()

        def get(name: str, default: Any = None) -> Any:
            return getattr(state, name, default)

        def fresh(stamp: float | None) -> bool:
            return stamp is not None and 0 <= now - stamp <= self.timeout_s * 3

        required_bits = 1 | 2 | 4 | 32
        present, enabled, health = (get("sensor_present"), get("sensor_enabled"),
                                    get("sensor_health"))
        sensor_ok = (present is not None and enabled is not None and health is not None
                     and int(present) & required_bits == required_bits
                     and int(enabled) & required_bits == required_bits
                     and int(health) & required_bits == required_bits
                     and fresh(get("sys_status_monotonic")))
        self.checks.append(_check("sensor_health", sensor_ok,
            {"present": present, "enabled": enabled, "health": health},
            {"required_bits": required_bits}, "Required PX4 sensors are healthy.",
            "PX4 必要传感器健康。"))

        flags = get("estimator_flags")
        estimator_ok = (flags is not None and int(flags) & 0x07 == 0x07
                        and int(flags) & 0x18 and int(flags) & 0x60
                        and fresh(get("estimator_monotonic")))
        self.checks.append(_check("estimator", estimator_ok, {"flags": flags},
            {"required": "attitude velocity position"}, "Estimator is ready.",
            "估计器状态就绪。"))

        fix, satellites, hdop = (get("gps_fix_type"), get("gps_satellites"),
                                 get("gps_hdop"))
        gps_ok = (fix is not None and fix >= 3 and satellites is not None and satellites >= 6
                  and hdop is not None and hdop <= 2.0 and fresh(get("gps_monotonic")))
        self.checks.append(_check("gnss", gps_ok,
            {"fix_type": fix, "satellites": satellites, "gps_hdop": hdop},
            {"fix_type": ">=3", "satellites": ">=6", "gps_hdop": "<=2.0"},
            "GNSS fix, satellites and HDOP are fresh and sufficient.",
            "GNSS 定位、卫星数和 HDOP 新鲜且满足要求。"))

        home_requested = False
        if hasattr(self.vehicle, "request_home_position"):
            try:
                self.vehicle.request_home_position()
                home_requested = True
            except Exception:
                self.checks.append(_check("home_request", False, {}, {"reply": True},
                    "HOME_POSITION request failed.", "HOME_POSITION 请求失败。"))
        if home_requested:
            deadline = now + max(3.0, self.timeout_s * 3)
            while self.clock() < deadline:
                current = self.vehicle.snapshot()
                if (getattr(current, "home_ned", None) is not None
                        and getattr(current, "home_geodetic", None) is not None
                        and getattr(current, "home_monotonic", 0) >= now):
                    state = current
                    break
                time.sleep(0.01)
        home_ok = (get("home_ned") is not None and get("home_geodetic") is not None
                   and fresh(get("home_monotonic")))
        self.checks.append(_check("home", home_ok,
            {"home_ned": get("home_ned"), "home_geodetic": get("home_geodetic")},
            {"known": True}, "PX4 HOME_POSITION is known and fresh.",
            "PX4 HOME_POSITION 已知且新鲜。"))

        battery = get("battery_remaining")
        battery_stamp = get("battery_monotonic")
        self.checks.append(_check("battery_telemetry", battery is not None and fresh(battery_stamp),
            {"remaining_pct": battery}, {"fresh": True},
            "Battery telemetry is fresh.", "电池遥测新鲜。"))

    def _matrix(self, values: Mapping[str, float]) -> None:
        expected = build_parameter_matrix(self.package)
        document = (self.package.document if isinstance(self.package, MissionPackage)
                    else self.package)
        budget = document["budget"]
        if budget.get("battery_capacity_wh") is None:
            self.checks.append(_check("battery_reserve_mapping", True, {},
                {"battery_capacity_wh": "provided"},
                "Battery capacity is absent; BAT_* values are left untouched.",
                "未提供电池容量，保持 BAT_* 现值。", blocking=False))

        for name, wanted in expected.items():
            if name.startswith("_") or name in {"link_loss_policy", "link_loss_timeout_s",
                                                  "accepted_px4_overrides"}:
                continue
            actual = values.get(name)
            if wanted is None:
                continue
            if actual is None:
                continue
            ok = math.isclose(actual, float(wanted), rel_tol=0, abs_tol=1e-3)
            if name.startswith("GF_MAX_"):
                ok = actual > 0 and actual >= float(wanted)
            elif name == "COM_DL_LOSS_T":
                ok = math.isclose(actual, float(wanted), rel_tol=0, abs_tol=0)
            elif name == "COM_OF_LOSS_T":
                ok = 0 <= actual <= 60
            elif name == "BAT_LOW_THR":
                reserve = budget["reserve_energy_wh"] / budget["battery_capacity_wh"]
                ok = (0.12 <= actual <= 0.5
                      and actual > values.get("BAT_CRIT_THR", 0)
                      and actual >= reserve)
            elif name == "BAT_CRIT_THR":
                reserve = budget["reserve_energy_wh"] / budget["battery_capacity_wh"]
                ok = 0.05 <= actual <= 0.5 and actual >= reserve
            self.checks.append(_check("parameter:" + name, ok, {"read": actual},
                {"expected": wanted}, f"{name} is consistent with the package.",
                f"{name} 与任务包一致。"))

        self.checks.append(_check("parameter:COM_RC_IN_MODE",
            values.get("COM_RC_IN_MODE") is not None and 0 <= values["COM_RC_IN_MODE"] <= 8,
            {"read": values.get("COM_RC_IN_MODE")}, {"allowed": "PX4 enum 0..8"},
            "RC input mode is a valid operator-selected enum.", "RC 输入模式是有效的操作员枚举。"))
        self.checks.append(_check("parameter:COM_LOW_BAT_ACT",
            values.get("COM_LOW_BAT_ACT") in {2, 3}, {"read": values.get("COM_LOW_BAT_ACT")},
            {"safe": "Return/Land"}, "Low-battery action is Return or Land.",
            "低电量动作必须是返航或降落。"))
        offboard = expected.get("COM_OBL_RC_ACT")
        if offboard is None:
            offboard = values.get("COM_OBL_RC_ACT")
        self.checks.append(_check("parameter:COM_OBL_RC_ACT", offboard in {3, 4, 5},
            {"read": values.get("COM_OBL_RC_ACT")}, {"safe": "Return/Land/Hold"},
            "Offboard-loss action is safe.", "Offboard 丢失动作安全。"))

    def run(self) -> dict[str, Any]:
        self.checks = []
        values = self._params()
        self._health()
        self._matrix(values)
        return self.report()

    def apply(self) -> dict[str, Any]:
        expected = build_parameter_matrix(self.package)
        invalid = self._invalid_targets(expected)
        if invalid:
            self.checks = [_check("matrix_values", False, {"invalid": invalid},
                {"PX4_metadata_ranges": True}, "Package targets are outside PX4 ranges.",
                "任务包目标值超出 PX4 范围。")]
            return self.report()
        previous: dict[str, float] = {}
        written: list[str] = []
        try:
            for name in _PARAMS:
                previous[name] = float(self.vehicle.read_parameter(
                    name, timeout_s=self.timeout_s, retries=self.retries))
            for name, wanted in expected.items():
                if name not in _PARAMS or wanted is None:
                    continue
                if math.isclose(previous[name], float(wanted), rel_tol=0, abs_tol=1e-3):
                    continue
                written.append(name)
                self.vehicle.write_parameter(name, float(wanted), timeout_s=self.timeout_s,
                                             retries=self.retries)
        except Exception as exc:
            rollback = self._rollback(written, previous)
            failed = [name for name, ok in rollback.items() if not ok]
            error = str(exc)
            if failed:
                error += "; PX4 parameters may be partially modified: " + ", ".join(failed)
            self.checks = [_check("apply", False,
                {"written": written, "rollback": rollback, "error": error},
                {"rollback": "best effort"}, "PX4 write failed; rollback was attempted.",
                "PX4 写入失败，已尝试回滚。")]
            if failed:
                self.checks.append(_check("apply_rollback", False, {"rollback": rollback}, {},
                    error, "PX4 参数可能部分修改：" + ", ".join(failed)))
            return self.report()
        report = self.run()
        if not report["passed"]:
            rollback = self._rollback(written, previous)
            failed = [name for name, ok in rollback.items() if not ok]
            report["checks"].append(_check("apply_rollback", not failed,
                {"rollback": rollback}, {"verified": True},
                "Post-apply admission failed; rollback was attempted.",
                "应用后准入失败，已尝试回滚。").as_dict())
            if failed:
                report["passed"] = False
                report["blocking_failures"].append(
                    "PX4 parameters may be partially modified: " + ", ".join(failed))
        return report

    def _rollback(self, names: list[str], previous: Mapping[str, float]) -> dict[str, bool]:
        results = {}
        for name in reversed(names):
            try:
                self.vehicle.write_parameter(name, previous[name], timeout_s=self.timeout_s,
                                             retries=self.retries)
                actual = self.vehicle.read_parameter(name, timeout_s=self.timeout_s,
                                                     retries=self.retries)
                results[name] = math.isclose(actual, previous[name], rel_tol=0, abs_tol=0)
            except Exception:
                results[name] = False
        return results

    @staticmethod
    def _invalid_targets(expected: Mapping[str, Any]) -> list[str]:
        ranges = {"COM_DL_LOSS_T": (5, 300), "COM_OF_LOSS_T": (0, 60),
                  "GF_MAX_HOR_DIST": (0, 10000), "GF_MAX_VER_DIST": (0, 10000),
                  "BAT_LOW_THR": (0.12, 0.5), "BAT_CRIT_THR": (0.05, 0.5)}
        invalid = [name for name, (lo, hi) in ranges.items()
                   if expected.get(name) is not None and not lo <= float(expected[name]) <= hi]
        enum_ranges = {
            "NAV_DLL_ACT": {0, 1, 2, 3, 5, 6},
            "GF_ACTION": {1, 2, 3, 4, 5},
            "COM_OBL_RC_ACT": {3, 4, 5},
        }
        for name, allowed in enum_ranges.items():
            value = expected.get(name)
            if value is not None and (not float(value).is_integer() or int(value) not in allowed):
                invalid.append(name)
        if (expected.get("BAT_LOW_THR") is not None and expected.get("BAT_CRIT_THR") is not None
                and expected["BAT_LOW_THR"] <= expected["BAT_CRIT_THR"]):
            invalid.append("BAT_LOW_THR")
        return invalid

    def report(self) -> dict[str, Any]:
        failures = [check.name for check in self.checks if check.blocking and not check.passed]
        return {"passed": not failures, "blocking_failures": failures,
                "parameters": dict(self.parameters),
                "checks": [check.as_dict() for check in self.checks]}
