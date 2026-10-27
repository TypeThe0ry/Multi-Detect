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


def test_link_loss_policy_only_maps_link_parameters_and_offboard_action_is_constrained():
    pkg = package("continue_then_return")
    matrix = build_parameter_matrix(pkg)
    assert matrix["NAV_DLL_ACT"] == 0
    assert matrix["COM_DL_LOSS_T"] == 7
    assert matrix["NAV_RCL_ACT"] is None
    assert matrix["COM_RC_IN_MODE"] is None
    assert matrix["COM_LOW_BAT_ACT"] is None
    pkg["px4_offboard_loss_action"] = 4
    assert build_parameter_matrix(pkg)["COM_OBL_RC_ACT"] == 4


def test_battery_capacity_absence_only_warns_and_does_not_write_bat_params():
    pkg = package()
    vehicle = FakeVehicle(values_for(pkg))
    report = PX4PreflightGate(vehicle, pkg, clock=lambda: 10.0).run()
    warning = next(c for c in report["checks"] if c["name"] == "battery_reserve_mapping")
    assert warning["passed"] and not warning["blocking"]
    assert not any(name.startswith("BAT_") for name, _ in vehicle.writes)


def test_apply_writes_only_differences_and_rolls_back_on_write_failure():
    pkg = package()
    values = values_for(pkg)
    values["NAV_DLL_ACT"] = 0
    values["GF_ACTION"] = 0
    vehicle = FakeVehicle(values)
    original = vehicle.write_parameter
    calls = []

    def failing_write(name, value, **kwargs):
        calls.append((name, value))
        if len(calls) == 2:
            raise RuntimeError("write failed")
        return original(name, value, **kwargs)

    vehicle.write_parameter = failing_write
    report = PX4PreflightGate(vehicle, pkg, clock=lambda: 10.0).apply()
    assert report["passed"] is False
    assert report["checks"][0]["name"] == "apply"
    assert vehicle.values["NAV_DLL_ACT"] == values["NAV_DLL_ACT"]


def test_battery_capacity_populates_thresholds_but_low_battery_action_is_checked_only():
    pkg = package()
    pkg["budget"]["battery_capacity_wh"] = 100
    matrix = build_parameter_matrix(pkg)
    assert matrix["BAT_LOW_THR"] > matrix["BAT_CRIT_THR"]
    assert matrix["COM_LOW_BAT_ACT"] is None


def test_offboard_loss_action_outside_return_land_hold_is_blocking():
    pkg = package()
    pkg["px4_offboard_loss_action"] = 2
    values = values_for(pkg)
    values["COM_OBL_RC_ACT"] = 2
    report = PX4PreflightGate(FakeVehicle(values), pkg, clock=lambda: 10.0).run()
    assert "parameter:COM_OBL_RC_ACT" in report["blocking_failures"]


def test_float32_quantized_geofence_limit_satisfies_package_bound():
    pkg = package()
    values = values_for(pkg)
    wanted = build_parameter_matrix(pkg)["GF_MAX_HOR_DIST"]
    values["GF_MAX_HOR_DIST"] = float(__import__("struct").unpack(
        "<f", __import__("struct").pack("<f", wanted)
    )[0])
    report = PX4PreflightGate(FakeVehicle(values), pkg, clock=lambda: 10.0).run()
    check = next(row for row in report["checks"] if row["name"] == "parameter:GF_MAX_HOR_DIST")
    assert check["passed"] is True


def test_gps_hdop_above_two_is_blocking():
    pkg = package()
    vehicle = FakeVehicle(values_for(pkg))
    vehicle.state.gps_hdop = 2.01
    report = PX4PreflightGate(vehicle, pkg, clock=lambda: 10.0).run()
    assert "gnss" in report["blocking_failures"]





