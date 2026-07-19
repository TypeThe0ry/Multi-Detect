from __future__ import annotations

from multidetect.mavlink_sensor_observer import (
    _parameter_is_sensor_relevant,
    _safe_message_fields,
    build_sensor_fusion_readiness,
)


def test_sensor_parameter_filter_includes_gps_and_magnetometer_settings() -> None:
    assert _parameter_is_sensor_relevant("CAL_MAG0_ID") is True
    assert _parameter_is_sensor_relevant("SENS_MAG_MODE") is True
    assert _parameter_is_sensor_relevant("GPS_1_CONFIG") is True
    assert _parameter_is_sensor_relevant("EKF2_GPS_CTRL") is True
    assert _parameter_is_sensor_relevant("ASPD_PRIMARY") is True
    assert _parameter_is_sensor_relevant("SENS_DPRES_OFF") is True
    assert _parameter_is_sensor_relevant("EKF2_EV_CTRL") is True
    assert _parameter_is_sensor_relevant("COM_ARM_WO_GPS") is False


def test_gps_summary_does_not_retain_precise_coordinates() -> None:
    summary = _safe_message_fields(
        "GPS_RAW_INT",
        {
            "fix_type": 3,
            "satellites_visible": 14,
            "lat": 123456789,
            "lon": 234567890,
            "eph": 80,
            "epv": 120,
            "vel": 0,
            "cog": 65535,
        },
    )

    assert summary["position_nonzero"] is True
    assert "lat" not in summary
    assert "lon" not in summary


def test_sys_status_decodes_mag_and_gps_health_bits() -> None:
    summary = _safe_message_fields(
        "SYS_STATUS",
        {
            "onboard_control_sensors_present": (1 << 2) | (1 << 5),
            "onboard_control_sensors_enabled": (1 << 2) | (1 << 5),
            "onboard_control_sensors_health": 1 << 2,
            "voltage_battery": 24000,
        },
    )

    assert summary["mag"] == {"present": True, "enabled": True, "healthy": True}
    assert summary["gps"] == {"present": True, "enabled": True, "healthy": False}


def test_sys_status_decodes_differential_pressure_and_vision_bits() -> None:
    summary = _safe_message_fields(
        "SYS_STATUS",
        {
            "onboard_control_sensors_present": (1 << 4) | (1 << 7),
            "onboard_control_sensors_enabled": (1 << 4) | (1 << 7),
            "onboard_control_sensors_health": 1 << 4,
        },
    )

    assert summary["differential_pressure"] == {
        "present": True,
        "enabled": True,
        "healthy": True,
    }
    assert summary["vision_position"] == {
        "present": True,
        "enabled": True,
        "healthy": False,
    }


def test_nonfinite_airspeed_is_normalized_to_json_null() -> None:
    summary = _safe_message_fields(
        "VFR_HUD",
        {
            "airspeed": float("nan"),
            "groundspeed": 0.01,
            "heading": 39,
            "climb": 0.0,
        },
    )

    assert summary["airspeed_mps"] is None
    assert summary["airspeed_finite_nonnegative"] is False


def test_real_like_indoor_capture_reports_no_metric_velocity_fusion() -> None:
    readiness = build_sensor_fusion_readiness(
        last_messages={
            "SYS_STATUS": {
                "onboard_control_sensors_present": 81935,
                "onboard_control_sensors_enabled": 81935,
                "onboard_control_sensors_health": 52529215,
            },
            "GPS_RAW_INT": {
                "fix_type": 0,
                "satellites_visible": 0,
                "vel": 0,
            },
            "VFR_HUD": {
                "airspeed": float("nan"),
                "groundspeed": 0.004,
                "heading": 39,
            },
            "SCALED_PRESSURE": {"press_abs": 998.6, "press_diff": 0.0},
            "LOCAL_POSITION_NED": {"vx": 0.0, "vy": 0.0, "vz": 0.0},
        },
        message_types={
            "ATTITUDE": 132,
            "GPS_RAW_INT": 44,
            "VFR_HUD": 35,
            "SCALED_PRESSURE": 9,
            "LOCAL_POSITION_NED": 9,
            "ESTIMATOR_STATUS": 4,
        },
        relative_visual_motion_available=True,
    )

    assert readiness["status"] == "invalid"
    assert readiness["metric_velocity_sources"] == ()
    assert readiness["full_multisensor_ready"] is False
    assert readiness["gps"]["fix_valid"] is False
    assert readiness["air_data"]["airspeed_measurement_ready"] is False
    assert readiness["visual_motion"]["mode"] == "relative_only"
    assert "relative_visual_motion_without_metric_scale" in readiness["reasons"]
    assert readiness["onboard_estimator"]["attitude_observed"] is True
    assert readiness["onboard_estimator"]["local_velocity_observed"] is True
    assert readiness["messages_transmitted"] == 0


def test_gps_scaled_vio_and_air_data_report_full_multisensor_readiness() -> None:
    sensor_bits = (1 << 4) | (1 << 5) | (1 << 7)
    readiness = build_sensor_fusion_readiness(
        last_messages={
            "SYS_STATUS": {
                "onboard_control_sensors_present": sensor_bits,
                "onboard_control_sensors_enabled": sensor_bits,
                "onboard_control_sensors_health": sensor_bits,
            },
            "GPS_RAW_INT": {
                "fix_type": 3,
                "satellites_visible": 12,
                "vel": 1500,
            },
            "VFR_HUD": {"airspeed": 14.0, "groundspeed": 15.1, "heading": 0},
            "WIND_COV": {"wind_x": 1.0, "wind_y": 2.0},
            "ODOMETRY": {"vx": 15.2, "vy": 1.8, "quality": 90},
        },
        message_types={
            "ATTITUDE": 100,
            "GPS_RAW_INT": 20,
            "VFR_HUD": 20,
            "SCALED_PRESSURE": 10,
            "WIND_COV": 10,
            "ODOMETRY": 50,
        },
        visual_odometry_absolute_scale_proven=True,
        relative_visual_motion_available=True,
    )

    assert readiness["status"] == "valid"
    assert readiness["metric_velocity_sources"] == ("gps", "vio", "air_data")
    assert readiness["full_multisensor_ready"] is True
    assert readiness["absolute_scale_available"] is True
    assert readiness["visual_motion"]["mode"] == "metric_vio"
    assert readiness["reasons"] == ("gps_vio_air_data_fusion_ready",)
