from types import SimpleNamespace

from multidetect.onboard_runtime.preflight import PX4PreflightGate, build_parameter_matrix


def package(action="return_now"):
    policy = {"action": action}
    if action.endswith("return") and action != "return_now":
        policy["timeout_s"] = 7
    return {
        "home": [0, 0],
        "link_loss_policy": policy,
        "operating_area": {"north_min_m": -10, "north_max_m": 10,
                           "east_min_m": -20, "east_max_m": 20,
                           "min_altitude_m": 2, "max_altitude_m": 30},
        "budget": {"energy_wh": 100, "reserve_energy_wh": 20,
                   "time_s": 1000, "reserve_time_s": 100},
    }


class FakeVehicle:
    def __init__(self, values):
        self.values = dict(values)
        self.writes = []
        now = 10.0
        self.state = SimpleNamespace(
            sensor_present=1063, sensor_enabled=1063, sensor_health=1063, sys_status_monotonic=now,
            estimator_flags=127, estimator_monotonic=now,
            gps_fix_type=3, gps_satellites=10, gps_hdop=1.5, gps_monotonic=now,
            home_ned=(0, 0, 0), home_geodetic=(1, 2, 3), home_monotonic=now,
            battery_remaining=80, battery_monotonic=now,
        )

    def read_parameter(self, name, **_):
        return self.values[name]

    def write_parameter(self, name, value, **_):
        self.writes.append((name, value))
        self.values[name] = value
        return value

    def snapshot(self):
        return self.state


def values_for(pkg):
    matrix = build_parameter_matrix(pkg)
    result = {name: value for name, value in matrix.items()
              if name.startswith(("NAV_", "COM_", "GF_", "BAT_")) and value is not None}
    result["NAV_RCL_ACT"] = 2
    result["COM_RC_IN_MODE"] = 0
    result["COM_LOW_BAT_ACT"] = 3
    result["COM_OBL_RC_ACT"] = 3
    result["BAT_LOW_THR"] = 0.25
    result["BAT_CRIT_THR"] = 0.2
    result["COM_LOW_BAT_ACT"] = 3
    return result


def test_matrix_is_conservative_and_covers_rectangle():
    matrix = build_parameter_matrix(package("return_now"))
    assert matrix["NAV_DLL_ACT"] == 2
    assert matrix["GF_MAX_HOR_DIST"] > 20
    assert matrix["GF_MAX_VER_DIST"] == 30
    assert matrix["BAT_CRIT_THR"] is None
    assert matrix["BAT_LOW_THR"] is None


def test_preflight_passes_health_and_matrix_and_apply_verifies():
    pkg = package()
    vehicle = FakeVehicle(values_for(pkg))
    gate = PX4PreflightGate(vehicle, pkg, clock=lambda: 10.0)
    report = gate.run()
    assert report["passed"] is True
    assert all(check["explanation_zh"] and check["explanation_en"] for check in report["checks"])
    applied = gate.apply()
    assert applied["passed"] is True
    assert len(vehicle.writes) == 0


def test_preflight_refuses_missing_battery_and_mismatched_link_action():
    pkg = package("return_now")
    values = values_for(pkg)
    values["NAV_DLL_ACT"] = 0
    vehicle = FakeVehicle(values)
    vehicle.state.battery_remaining = None
    report = PX4PreflightGate(vehicle, pkg, clock=lambda: 10.0).run()
    assert report["passed"] is False
    assert "battery_telemetry" in report["blocking_failures"]
    assert "parameter:NAV_DLL_ACT" in report["blocking_failures"]





