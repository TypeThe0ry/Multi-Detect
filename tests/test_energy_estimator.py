"""CPU-only onboard reserve/return decisions from synthetic MAVLink telemetry."""

from dataclasses import replace

import pytest
from test_mission_supervisor import build

from multidetect.autonomy.energy_estimator import EnergyConfig, OnboardEnergyEstimator


def config(**kwargs):
    return EnergyConfig(
        capacity_wh=100,
        reserve_energy_wh=10,
        reserve_time_s=30,
        nominal_voltage_v=22.2,
        cutoff_voltage_v=18.2,
        **kwargs,
    )


def battery(pct=80, voltage=22200, current=2000, consumed=-1, **kwargs):
    return {
        "mavpackettype": "BATTERY_STATUS",
        "id": 0,
        "voltages": [voltage] + [65535] * 9,
        "current_battery": current,
        "battery_remaining": pct,
        "current_consumed": consumed,
        **kwargs,
    }


def estimate(model, now=0, distance=30, altitude=20, return_altitude=20):
    return model.estimate(
        now_s=now,
        pose_ned=(distance, 0, -altitude),
        home_ne=(0, 0),
        return_altitude_m=return_altitude,
        mission_remaining_s=1000 - now,
    )


def test_steady_drain_decreases_energy_and_time_to_reserve():
    model = OnboardEnergyEstimator(config())
    readings = []
    for t in range(0, 31, 5):
        model.ingest(battery(), received_at_s=t)
        readings.append(estimate(model, t))
    assert all(
        a["remaining_energy_wh"] > b["remaining_energy_wh"]
        for a, b in zip(readings, readings[1:], strict=False)
    )
    assert readings[-1]["remaining_energy_wh"] == pytest.approx(80 - 500 * 30 / 3600)
    assert readings[-1]["time_to_reserve_s"] < readings[0]["time_to_reserve_s"]
    assert readings[-1]["action"] == "continue"


def test_sudden_voltage_sag_triggers_land_before_soc_changes():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(), received_at_s=0)
    assert estimate(model)["action"] == "continue"
    model.ingest(battery(voltage=18200), received_at_s=1)
    result = estimate(model, 1)
    assert result["remaining_energy_wh"] == 0
    assert result["voltage_sag_factor"] == 0
    assert result["action"] == "land_now"


@pytest.mark.parametrize(
    "message",
    [
        {},
        battery(pct=-1, current=-1),
        {
            "mavpackettype": "SYS_STATUS",
            "battery_remaining": -1,
            "voltage_battery": 65535,
            "current_battery": -1,
        },
    ],
)
def test_missing_or_sentinel_battery_uses_time_budget_only(message):
    model = OnboardEnergyEstimator(config())
    model.ingest(message, received_at_s=0)
    result = estimate(model)
    assert result["source"] == "mission_time_budget_only"
    assert result["remaining_energy_wh"] is None
    assert result["return_energy_wh"] is None
    assert result["time_to_reserve_s"] == 970
    assert "time budget only" in result["reason"]


def test_far_home_returns_earlier_and_insufficient_rtl_lands():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(pct=20), received_at_s=0)
    near, far, impossible = (estimate(model, distance=d) for d in (10, 250, 600))
    assert near["action"] == "continue"
    assert far["action"] == "return_now"
    assert far["action_margin_wh"] < 0
    assert far["remaining_energy_wh"] >= far["return_energy_wh"]
    assert impossible["action"] == "land_now"


def test_climb_landing_safety_factor_and_headwind_are_included():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(), received_at_s=0)
    baseline = estimate(model, distance=60)
    climb = estimate(model, distance=60, return_altitude=40)
    assert climb["climb_m"] == 20
    assert climb["descent_m"] == 40
    assert climb["return_energy_wh"] > baseline["return_energy_wh"]
    model.ingest({"mavpackettype": "WIND_COV", "wind_x": 4, "wind_y": 0}, received_at_s=0)
    wind = estimate(model, distance=60)
    assert wind["return_ground_speed_mps"] == 1
    assert wind["return_energy_wh"] > baseline["return_energy_wh"]
    assert wind["return_energy_wh"] == pytest.approx(1.3 * (400 * 60 + 360 * 35) / 3600)


def test_crosswind_exceeding_cruise_capability_lands():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(), received_at_s=0)
    model.ingest({"mavpackettype": "WIND_COV", "wind_x": 0, "wind_y": 6}, received_at_s=0)
    assert estimate(model)["action"] == "land_now"


def test_stale_cached_battery_never_refreshes_or_replenishes():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(), received_at_s=0)
    initial = estimate(model)
    assert not model.ingest(battery(), received_at_s=0)
    stale = estimate(model, now=6)
    assert stale["action"] == "return_now"
    assert stale["battery_age_s"] == 6
    assert stale["remaining_energy_wh"] < initial["remaining_energy_wh"]
    model.ingest(battery(pct=100), received_at_s=7)
    assert estimate(model, now=7)["remaining_energy_wh"] < initial["remaining_energy_wh"]


def test_sys_status_units_and_consumed_mah_constraint():
    model = OnboardEnergyEstimator(config())
    model.ingest(
        {
            "mavpackettype": "SYS_STATUS",
            "voltage_battery": 22200,
            "current_battery": 2000,
            "battery_remaining": 80,
        },
        received_at_s=0,
    )
    assert estimate(model)["remaining_energy_wh"] == 80
    model.ingest(battery(consumed=2000), received_at_s=1)
    assert estimate(model, 1)["remaining_energy_wh"] == pytest.approx(100 - 44.4)
    model.ingest(battery(consumed=2500), received_at_s=2)
    assert estimate(model, 2)["remaining_energy_wh"] == pytest.approx(100 - 55.5)


def test_power_learning_is_phase_specific_and_ignores_unarmed():
    model = OnboardEnergyEstimator(config())
    for t, velocity in enumerate(((0, 0, 0), (2, 0, 0), (0, 0, -2))):
        model.ingest({"mavpackettype": "HEARTBEAT", "base_mode": 128}, received_at_s=t)
        model.ingest(
            {
                "mavpackettype": "LOCAL_POSITION_NED",
                **dict(zip(("vx", "vy", "vz"), velocity, strict=True)),
            },
            received_at_s=t,
        )
        model.ingest(battery(current=3000 + t * 1000), received_at_s=t)
    assert model.powers(2) == pytest.approx({"hover": 666, "translate": 888, "climb": 1110})
    assert model.powers(100) == {"hover": 360, "translate": 400, "climb": 500}


def test_foreign_pack_and_source_ignored():
    model = OnboardEnergyEstimator(config())
    assert not model.ingest(battery(id=1), received_at_s=0)
    assert not model.ingest(battery(source_system=2), received_at_s=0)
    assert estimate(model)["source"] == "mission_time_budget_only"


def test_old_sys_status_cannot_override_new_battery_fields_or_age():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(voltage=20200, current=4000), received_at_s=10)
    first = estimate(model, 10)
    assert not model.ingest(
        {
            "mavpackettype": "SYS_STATUS",
            "voltage_battery": 22200,
            "battery_remaining": 100,
            "current_battery": 1000,
        },
        received_at_s=9,
    )
    model.ingest(battery(voltage=22200), received_at_s=11)
    second = estimate(model, 11)
    assert second["remaining_energy_wh"] < first["remaining_energy_wh"]
    assert second["voltage_sag_factor"] == first["voltage_sag_factor"]
    assert second["battery_age_s"] == 0


def test_first_delayed_battery_is_projected_from_original_receipt_time():
    model = OnboardEnergyEstimator(config())
    estimate(model, 100)
    model.ingest(battery(pct=20), received_at_s=0)
    result = estimate(model, 100, distance=300)
    assert result["remaining_energy_wh"] == pytest.approx(20 - 500 * 100 / 3600)
    assert result["action"] == "land_now"


def test_counter_without_soc_anchor_cannot_assume_fully_charged_pack():
    model = OnboardEnergyEstimator(config())
    model.ingest(battery(pct=-1, consumed=0), received_at_s=0)
    assert estimate(model)["source"] == "mission_time_budget_only"


@pytest.mark.parametrize(
    "field,value",
    [
        ("safety_factor", 0.9),
        ("cruise_speed_mps", 0),
        ("capacity_wh", float("nan")),
        ("battery_id", 1.5),
    ],
)
def test_invalid_configuration_rejected(field, value):
    with pytest.raises(ValueError):
        replace(config(), **{field: value})


def test_supervisor_consumes_energy_and_logs_numeric_margin_without_ground(tmp_path):
    supervisor, now, log = build(tmp_path)
    supervisor.ingest_energy_telemetry(battery(pct=12))
    assert supervisor.evaluate(pose_ned=(100, 0, -20)) == "return_now"
    assert supervisor.energy_estimate["action_margin_wh"] < 0
    assert "action_margin_wh" in log.path.read_text(encoding="utf-8")
    now[0] += 1
    supervisor.ingest_energy_telemetry(battery(pct=1))
    assert supervisor.evaluate(pose_ned=(100, 0, -20)) == "land_now"
    action, info = supervisor.next_action(pose_ned=(100, 0, -20), yaw_deg=0)
    assert action.kind == "stop"
    assert info["energy"]["return_energy_wh"] > info["energy"]["remaining_energy_wh"]


def test_supervisor_no_battery_keeps_package_deadline_and_reports_fallback(tmp_path):
    supervisor, now, _ = build(tmp_path)
    assert supervisor.evaluate(pose_ned=(50, 0, -20)) == "continue"
    assert supervisor.energy_estimate["source"] == "mission_time_budget_only"
    now[0] += 86400 - 60
    assert supervisor.evaluate(pose_ned=(50, 0, -20)) == "return_now"
