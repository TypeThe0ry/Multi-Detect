from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from typing import Any, Final

from pymavlink import mavutil

MAG_MESSAGE_TYPES: Final = frozenset(
    {"RAW_IMU", "SCALED_IMU", "SCALED_IMU2", "SCALED_IMU3", "HIGHRES_IMU"}
)
LAST_MESSAGE_TYPES: Final = frozenset(
    {
        "HEARTBEAT",
        "SYS_STATUS",
        "ATTITUDE",
        "ATTITUDE_QUATERNION",
        "GPS_RAW_INT",
        "GPS2_RAW",
        "GLOBAL_POSITION_INT",
        "LOCAL_POSITION_NED",
        "VFR_HUD",
        "SCALED_PRESSURE",
        "WIND_COV",
        "ODOMETRY",
        "VISION_POSITION_ESTIMATE",
        "VISION_SPEED_ESTIMATE",
        "OPTICAL_FLOW_RAD",
        "ESTIMATOR_STATUS",
        "EKF_STATUS_REPORT",
        "RAW_IMU",
        "SCALED_IMU",
        "SCALED_IMU2",
        "SCALED_IMU3",
        "HIGHRES_IMU",
    }
)
SENSOR_3D_MAG: Final = 1 << 2
SENSOR_DIFFERENTIAL_PRESSURE: Final = 1 << 4
SENSOR_GPS: Final = 1 << 5
SENSOR_OPTICAL_FLOW: Final = 1 << 6
SENSOR_VISION_POSITION: Final = 1 << 7
VISUAL_ODOMETRY_MESSAGE_TYPES: Final = frozenset(
    {"ODOMETRY", "VISION_POSITION_ESTIMATE", "VISION_SPEED_ESTIMATE"}
)


def _parameter_is_sensor_relevant(parameter_id: str) -> bool:
    return parameter_id.startswith(
        (
            "CAL_MAG",
            "SENS_MAG",
            "GPS_",
            "SENS_GPS",
            "EKF2_GPS",
            "ASPD_",
            "SENS_DPRES",
            "EKF2_ARSP",
            "EKF2_EV",
            "EKF2_OF",
            "SENS_FLOW",
            "UAVCAN_SUB_ASPD",
            "UAVCAN_SUB_DPRES",
            "UAVCAN_SUB_GPS",
            "SYS_HAS_MAG",
            "SYS_HAS_GPS",
            "SYS_HAS_NUM_ASPD",
        )
    )


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _sensor_flag_summary(*, present: int, enabled: int, health: int, bit: int) -> dict[str, bool]:
    return {
        "present": bool(present & bit),
        "enabled": bool(enabled & bit),
        "healthy": bool(health & bit),
    }


def _safe_message_fields(message_type: str, values: dict[str, Any]) -> dict[str, Any]:
    if message_type == "HEARTBEAT":
        base_mode = int(values.get("base_mode", 0))
        return {
            "type": values.get("type"),
            "autopilot": values.get("autopilot"),
            "base_mode": base_mode,
            "system_status": values.get("system_status"),
            "armed": bool(base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
        }
    if message_type == "SYS_STATUS":
        present = int(values.get("onboard_control_sensors_present", 0))
        enabled = int(values.get("onboard_control_sensors_enabled", 0))
        health = int(values.get("onboard_control_sensors_health", 0))
        return {
            "mag": _sensor_flag_summary(
                present=present,
                enabled=enabled,
                health=health,
                bit=SENSOR_3D_MAG,
            ),
            "differential_pressure": _sensor_flag_summary(
                present=present,
                enabled=enabled,
                health=health,
                bit=SENSOR_DIFFERENTIAL_PRESSURE,
            ),
            "gps": _sensor_flag_summary(
                present=present,
                enabled=enabled,
                health=health,
                bit=SENSOR_GPS,
            ),
            "optical_flow": _sensor_flag_summary(
                present=present,
                enabled=enabled,
                health=health,
                bit=SENSOR_OPTICAL_FLOW,
            ),
            "vision_position": _sensor_flag_summary(
                present=present,
                enabled=enabled,
                health=health,
                bit=SENSOR_VISION_POSITION,
            ),
            "voltage_battery_mv": values.get("voltage_battery"),
        }
    if message_type in {"GPS_RAW_INT", "GPS2_RAW"}:
        lat = int(values.get("lat", 0) or 0)
        lon = int(values.get("lon", 0) or 0)
        return {
            "fix_type": values.get("fix_type"),
            "satellites_visible": values.get("satellites_visible"),
            "eph_cm": values.get("eph"),
            "epv_cm": values.get("epv"),
            "velocity_cm_s": values.get("vel"),
            "course_cdeg": values.get("cog"),
            "position_nonzero": lat != 0 or lon != 0,
        }
    if message_type == "GLOBAL_POSITION_INT":
        lat = int(values.get("lat", 0) or 0)
        lon = int(values.get("lon", 0) or 0)
        return {
            "position_nonzero": lat != 0 or lon != 0,
            "relative_alt_mm": values.get("relative_alt"),
            "heading_cdeg": values.get("hdg"),
        }
    if message_type == "ATTITUDE":
        return {
            "roll_rad": _json_safe_value(values.get("roll")),
            "pitch_rad": _json_safe_value(values.get("pitch")),
            "yaw_rad": _json_safe_value(values.get("yaw")),
            "rollspeed_rad_s": _json_safe_value(values.get("rollspeed")),
            "pitchspeed_rad_s": _json_safe_value(values.get("pitchspeed")),
            "yawspeed_rad_s": _json_safe_value(values.get("yawspeed")),
        }
    if message_type == "ATTITUDE_QUATERNION":
        return {
            key: _json_safe_value(values.get(key))
            for key in ("q1", "q2", "q3", "q4", "rollspeed", "pitchspeed", "yawspeed")
        }
    if message_type == "VFR_HUD":
        airspeed = _finite_number(values.get("airspeed"))
        return {
            "airspeed_mps": airspeed,
            "airspeed_finite_nonnegative": airspeed is not None and airspeed >= 0.0,
            "groundspeed_mps": _finite_number(values.get("groundspeed")),
            "heading_deg": _finite_number(values.get("heading")),
            "climb_mps": _finite_number(values.get("climb")),
        }
    if message_type == "SCALED_PRESSURE":
        differential_pressure = _finite_number(values.get("press_diff"))
        return {
            "absolute_pressure_hpa": _finite_number(values.get("press_abs")),
            "differential_pressure_hpa": differential_pressure,
            "differential_pressure_finite": differential_pressure is not None,
            "temperature_cdeg": values.get("temperature"),
        }
    if message_type == "LOCAL_POSITION_NED":
        return {
            key: _json_safe_value(values.get(key))
            for key in ("x", "y", "z", "vx", "vy", "vz")
        }
    if message_type == "WIND_COV":
        return {
            "wind_north_mps": _finite_number(values.get("wind_x")),
            "wind_east_mps": _finite_number(values.get("wind_y")),
            "wind_down_mps": _finite_number(values.get("wind_z")),
            "horizontal_variance": _finite_number(values.get("var_horiz")),
        }
    if message_type in VISUAL_ODOMETRY_MESSAGE_TYPES:
        return {
            key: _json_safe_value(values.get(key))
            for key in (
                "frame_id",
                "child_frame_id",
                "x",
                "y",
                "z",
                "vx",
                "vy",
                "vz",
                "quality",
                "reset_counter",
            )
            if key in values
        }
    if message_type == "OPTICAL_FLOW_RAD":
        return {
            key: _json_safe_value(values.get(key))
            for key in (
                "integrated_x",
                "integrated_y",
                "integrated_xgyro",
                "integrated_ygyro",
                "integrated_zgyro",
                "distance",
                "quality",
            )
        }
    if message_type in MAG_MESSAGE_TYPES:
        return {
            "xmag": values.get("xmag"),
            "ymag": values.get("ymag"),
            "zmag": values.get("zmag"),
        }
    return {
        key: _json_safe_value(value)
        for key, value in values.items()
        if key not in {"mavpackettype", "lat", "lon", "alt"}
    }


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _normalized_sys_status(last_messages: dict[str, dict[str, Any]]) -> dict[str, Any]:
    status = last_messages.get("SYS_STATUS", {})
    if any(key in status for key in ("gps", "differential_pressure", "vision_position")):
        return status
    return _safe_message_fields("SYS_STATUS", status)


def _sensor_present_enabled(status: dict[str, Any], sensor_name: str) -> bool:
    sensor = status.get(sensor_name, {})
    return bool(sensor.get("present")) and bool(sensor.get("enabled"))


def build_sensor_fusion_readiness(
    *,
    last_messages: dict[str, dict[str, Any]],
    message_types: dict[str, int],
    visual_odometry_absolute_scale_proven: bool = False,
    relative_visual_motion_available: bool = False,
) -> dict[str, Any]:
    """Summarize whether live MAVLink inputs can support metric velocity fusion."""

    if not isinstance(visual_odometry_absolute_scale_proven, bool):
        raise ValueError("visual-odometry scale proof flag must be boolean")
    if not isinstance(relative_visual_motion_available, bool):
        raise ValueError("relative visual-motion flag must be boolean")

    sys_status = _normalized_sys_status(last_messages)
    gps_status = sys_status.get("gps", {})
    differential_pressure_status = sys_status.get("differential_pressure", {})
    gps = last_messages.get("GPS_RAW_INT") or last_messages.get("GPS2_RAW") or {}
    fix_type = _integer(gps.get("fix_type"))
    satellites = _integer(gps.get("satellites_visible"))
    gps_velocity_cm_s = _finite_number(gps.get("velocity_cm_s", gps.get("vel")))
    gps_fix_valid = bool(
        fix_type is not None
        and fix_type >= 3
        and satellites is not None
        and satellites >= 6
    )
    gps_velocity_ready = bool(
        gps_fix_valid
        and gps_velocity_cm_s is not None
        and 0.0 <= gps_velocity_cm_s < 65_535.0
    )

    vfr_hud = last_messages.get("VFR_HUD", {})
    airspeed_mps = _finite_number(vfr_hud.get("airspeed_mps", vfr_hud.get("airspeed")))
    differential_pressure_present_enabled = _sensor_present_enabled(
        sys_status,
        "differential_pressure",
    )
    airspeed_measurement_ready = bool(
        differential_pressure_present_enabled
        and airspeed_mps is not None
        and airspeed_mps >= 0.0
    )

    wind = last_messages.get("WIND_COV", {})
    wind_north_mps = _finite_number(wind.get("wind_north_mps", wind.get("wind_x")))
    wind_east_mps = _finite_number(wind.get("wind_east_mps", wind.get("wind_y")))
    wind_ready = wind_north_mps is not None and wind_east_mps is not None
    attitude_ready = any(
        int(message_types.get(message_type, 0)) > 0
        for message_type in ("ATTITUDE", "ATTITUDE_QUATERNION")
    )
    local_estimator_velocity_observed = int(message_types.get("LOCAL_POSITION_NED", 0)) > 0
    air_data_velocity_ready = bool(airspeed_measurement_ready and wind_ready and attitude_ready)

    visual_odometry_observed = any(
        int(message_types.get(message_type, 0)) > 0
        for message_type in VISUAL_ODOMETRY_MESSAGE_TYPES
    )
    metric_visual_odometry_ready = bool(
        visual_odometry_observed and visual_odometry_absolute_scale_proven
    )
    optical_flow_observed = int(message_types.get("OPTICAL_FLOW_RAD", 0)) > 0

    metric_sources: list[str] = []
    if gps_velocity_ready:
        metric_sources.append("gps")
    if metric_visual_odometry_ready:
        metric_sources.append("vio")
    if air_data_velocity_ready:
        metric_sources.append("air_data")

    if len(metric_sources) >= 2:
        status = "valid"
    elif len(metric_sources) == 1:
        status = "degraded"
    else:
        status = "invalid"

    reasons: list[str] = []
    if not gps_fix_valid:
        reasons.append("gps_fix_invalid")
    if not _sensor_present_enabled(sys_status, "gps"):
        reasons.append("gps_sensor_not_present_enabled")
    if airspeed_mps is None or airspeed_mps < 0.0:
        reasons.append("airspeed_value_invalid")
    if not differential_pressure_present_enabled:
        reasons.append("differential_pressure_sensor_not_present_enabled")
    if not wind_ready:
        reasons.append("wind_estimate_missing")
    if not visual_odometry_observed:
        reasons.append("visual_odometry_stream_missing")
    elif not visual_odometry_absolute_scale_proven:
        reasons.append("vio_absolute_scale_unproven")
    if relative_visual_motion_available and not metric_visual_odometry_ready:
        reasons.append("relative_visual_motion_without_metric_scale")
    if not metric_sources:
        reasons.append("no_metric_velocity_source")
    elif len(metric_sources) == 1:
        reasons.append("single_metric_velocity_source")
    elif not (gps_velocity_ready and metric_visual_odometry_ready and air_data_velocity_ready):
        reasons.append("partial_multisensor_fusion")
    else:
        reasons.append("gps_vio_air_data_fusion_ready")

    if metric_visual_odometry_ready:
        visual_motion_mode = "metric_vio"
    elif visual_odometry_observed or relative_visual_motion_available or optical_flow_observed:
        visual_motion_mode = "relative_only"
    else:
        visual_motion_mode = "not_observed"

    return {
        "status": status,
        "reasons": tuple(dict.fromkeys(reasons)),
        "metric_velocity_sources": tuple(metric_sources),
        "metric_source_count": len(metric_sources),
        "minimum_metric_source_count": 2,
        "full_multisensor_ready": bool(
            gps_velocity_ready and metric_visual_odometry_ready and air_data_velocity_ready
        ),
        "absolute_scale_available": bool(metric_sources),
        "gps": {
            "message_observed": bool(
                int(message_types.get("GPS_RAW_INT", 0))
                or int(message_types.get("GPS2_RAW", 0))
            ),
            "sensor_present": bool(gps_status.get("present")),
            "sensor_enabled": bool(gps_status.get("enabled")),
            "sensor_healthy": bool(gps_status.get("healthy")),
            "fix_type": fix_type,
            "satellites_visible": satellites,
            "fix_valid": gps_fix_valid,
            "velocity_ready": gps_velocity_ready,
        },
        "air_data": {
            "differential_pressure_present": bool(
                differential_pressure_status.get("present")
            ),
            "differential_pressure_enabled": bool(
                differential_pressure_status.get("enabled")
            ),
            "differential_pressure_healthy": bool(
                differential_pressure_status.get("healthy")
            ),
            "scaled_pressure_observed": int(message_types.get("SCALED_PRESSURE", 0)) > 0,
            "airspeed_mps": airspeed_mps,
            "airspeed_measurement_ready": airspeed_measurement_ready,
            "wind_observed": wind_ready,
            "ground_velocity_ready": air_data_velocity_ready,
        },
        "visual_motion": {
            "mode": visual_motion_mode,
            "relative_visual_motion_available": relative_visual_motion_available,
            "optical_flow_observed": optical_flow_observed,
            "visual_odometry_observed": visual_odometry_observed,
            "absolute_scale_proven": visual_odometry_absolute_scale_proven,
            "metric_velocity_ready": metric_visual_odometry_ready,
        },
        "onboard_estimator": {
            "attitude_observed": attitude_ready,
            "local_velocity_observed": local_estimator_velocity_observed,
            "estimator_status_observed": int(message_types.get("ESTIMATOR_STATUS", 0)) > 0,
        },
        "read_only": True,
        "messages_transmitted": 0,
    }


def observe_sensors(
    *,
    endpoint: str,
    duration_seconds: float,
    visual_odometry_absolute_scale_proven: bool = False,
    relative_visual_motion_available: bool = False,
) -> dict[str, Any]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    connection = mavutil.mavlink_connection(
        endpoint,
        autoreconnect=False,
        robust_parsing=True,
    )
    started = time.monotonic()
    deadline = started + duration_seconds
    counts: Counter[str] = Counter()
    components: Counter[str] = Counter()
    last_messages: dict[str, dict[str, Any]] = {}
    parameters: dict[str, float] = {}
    status_text: list[dict[str, Any]] = []
    mag_stats: dict[str, dict[str, Any]] = {}

    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            message = connection.recv_match(blocking=True, timeout=min(0.5, remaining))
            if message is None:
                continue
            message_type = message.get_type()
            if message_type == "BAD_DATA":
                continue
            counts[message_type] += 1
            components[f"{message.get_srcSystem()}/{message.get_srcComponent()}"] += 1
            values = message.to_dict()

            if message_type in LAST_MESSAGE_TYPES:
                last_messages[message_type] = _safe_message_fields(message_type, values)

            if message_type == "PARAM_VALUE":
                parameter_id = values.get("param_id", "")
                if isinstance(parameter_id, bytes):
                    parameter_id = parameter_id.decode("ascii", "replace")
                parameter_id = str(parameter_id).rstrip("\x00")
                if _parameter_is_sensor_relevant(parameter_id):
                    parameters[parameter_id] = float(values.get("param_value", math.nan))

            if message_type == "STATUSTEXT":
                status_text.append(
                    {
                        "severity": values.get("severity"),
                        "text": str(values.get("text", "")).rstrip("\x00"),
                    }
                )
                status_text = status_text[-40:]

            if message_type in MAG_MESSAGE_TYPES:
                axes = [values.get("xmag"), values.get("ymag"), values.get("zmag")]
                if all(isinstance(axis, (int, float)) for axis in axes):
                    numeric_axes = [float(axis) for axis in axes]
                    stats = mag_stats.setdefault(
                        message_type,
                        {
                            "count": 0,
                            "min": [math.inf, math.inf, math.inf],
                            "max": [-math.inf, -math.inf, -math.inf],
                            "last": numeric_axes,
                        },
                    )
                    stats["count"] += 1
                    stats["last"] = numeric_axes
                    for index, axis in enumerate(numeric_axes):
                        stats["min"][index] = min(stats["min"][index], axis)
                        stats["max"][index] = max(stats["max"][index], axis)
    finally:
        connection.close()

    fusion_readiness = build_sensor_fusion_readiness(
        last_messages=last_messages,
        message_types=dict(counts),
        visual_odometry_absolute_scale_proven=visual_odometry_absolute_scale_proven,
        relative_visual_motion_available=relative_visual_motion_available,
    )
    return {
        "event": "mavlink_sensor_observation",
        "endpoint": endpoint,
        "duration_seconds": round(time.monotonic() - started, 3),
        "message_count": sum(counts.values()),
        "message_types": dict(sorted(counts.items())),
        "source_components": dict(sorted(components.items())),
        "last_messages": last_messages,
        "mag_stats": mag_stats,
        "sensor_parameters": dict(sorted(parameters.items())),
        "statustext": status_text,
        "sensor_fusion_readiness": fusion_readiness,
        "read_only": True,
        "messages_transmitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Passively observe GPS and magnetometer MAVLink telemetry without transmitting."
    )
    parser.add_argument("--endpoint", default="udpin:127.0.0.1:14562")
    parser.add_argument("--duration-seconds", type=float, default=15.0)
    parser.add_argument("--visual-odometry-absolute-scale-proven", action="store_true")
    parser.add_argument("--relative-visual-motion-available", action="store_true")
    args = parser.parse_args()
    report = observe_sensors(
        endpoint=args.endpoint,
        duration_seconds=args.duration_seconds,
        visual_odometry_absolute_scale_proven=args.visual_odometry_absolute_scale_proven,
        relative_visual_motion_available=args.relative_visual_motion_available,
    )
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_sensor_fusion_readiness",
    "observe_sensors",
]
