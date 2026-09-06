"""Read-only MAVLink GNSS capture for field bring-up.

The existing :mod:`multidetect.mavlink_sensor_observer` reports sensor health
without retaining coordinates.  A field geolocation run needs the opposite:
the raw ``GPS_RAW_INT`` wire tuple, its receive time, and the exact reason a
sample did or did not qualify.  This module records that tuple without sending
heartbeats, stream requests, parameters, missions, or flight-control frames.

The resulting document is an advisory sensor-capture artifact.  It can prove
that a receiver produced qualified samples during a bounded observation
window, but it is not target truth, a depth calibration, HIL, REAL, or release
evidence by itself.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .mavlink_gps import (
    GNSS_CANONICAL_MAX_GPS_AGE_S,
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_SATELLITES_MIN,
    normalize_gps_fix_type,
    normalize_satellites_visible,
)

GNSS_CAPTURE_SCHEMA_VERSION = 1
GNSS_CAPTURE_EVIDENCE_TYPE = "multidetect.gnss-capture.v1"
DEFAULT_MAX_HORIZONTAL_ACCURACY_M = GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
DEFAULT_MAX_VERTICAL_ACCURACY_M = GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M
DEFAULT_GPS_MAX_AGE_S = GNSS_CANONICAL_MAX_GPS_AGE_S
CANONICAL_MAX_HORIZONTAL_ACCURACY_M = GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
CANONICAL_MAX_VERTICAL_ACCURACY_M = GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M
CANONICAL_MAX_GPS_AGE_S = GNSS_CANONICAL_MAX_GPS_AGE_S
_MAVLINK_INT32_MAX = (1 << 31) - 1
_MAVLINK_INT32_MIN = -(1 << 31)
_MAVLINK_UINT16_MAX = (1 << 16) - 1


class GnssCaptureError(ValueError):
    """Raised when a GNSS capture cannot satisfy its wire contract."""


def _finite_number(
    value: object,
    name: str = "value",
    *,
    non_negative: bool = False,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (non_negative and number < 0.0):
        return None
    return number


def _wire_int(
    value: object,
    *,
    minimum: int,
    maximum: int,
    unknown: int | None = None,
) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if unknown is not None and value == unknown:
        return None
    if not minimum <= value <= maximum:
        return None
    return int(value)


def _coordinate_e7(values: Mapping[str, object]) -> tuple[float, float] | None:
    latitude_e7 = _wire_int(
        values.get("lat"),
        minimum=_MAVLINK_INT32_MIN,
        maximum=_MAVLINK_INT32_MAX,
        unknown=_MAVLINK_INT32_MAX,
    )
    longitude_e7 = _wire_int(
        values.get("lon"),
        minimum=_MAVLINK_INT32_MIN,
        maximum=_MAVLINK_INT32_MAX,
        unknown=_MAVLINK_INT32_MAX,
    )
    if latitude_e7 is None or longitude_e7 is None:
        return None
    latitude_deg = latitude_e7 / 10_000_000.0
    longitude_deg = longitude_e7 / 10_000_000.0
    if not (
        math.isfinite(latitude_deg)
        and math.isfinite(longitude_deg)
        and -90.0 < latitude_deg < 90.0
        and -180.0 <= longitude_deg <= 180.0
    ):
        return None
    return latitude_deg, longitude_deg


def _accuracy_cm(value: object) -> float | None:
    wire = _wire_int(value, minimum=0, maximum=_MAVLINK_UINT16_MAX, unknown=_MAVLINK_UINT16_MAX)
    return None if wire is None else wire / 100.0


def _message_values(message: object) -> dict[str, object]:
    to_dict = getattr(message, "to_dict", None)
    if callable(to_dict):
        decoded = to_dict()
        if isinstance(decoded, Mapping):
            return dict(decoded)
    return {
        name: value
        for name in (
            "time_usec",
            "fix_type",
            "satellites_visible",
            "lat",
            "lon",
            "alt",
            "eph",
            "epv",
            "vel",
            "cog",
            "relative_alt",
            "vx",
            "vy",
            "vz",
            "hdg",
        )
        if (value := getattr(message, name, None)) is not None
    }


def _raw_gps_record(
    message_type: str,
    values: Mapping[str, object],
    *,
    observed_at_monotonic_s: float,
    observed_at_epoch_s: float,
    max_horizontal_accuracy_m: float,
    max_vertical_accuracy_m: float,
    max_age_s: float,
) -> dict[str, object]:
    fix_type = normalize_gps_fix_type(values.get("fix_type"))
    satellites_wire = values.get("satellites_visible")
    satellites = normalize_satellites_visible(satellites_wire)
    coordinates = _coordinate_e7(values)
    altitude_mm = _wire_int(
        values.get("alt"),
        minimum=_MAVLINK_INT32_MIN,
        maximum=_MAVLINK_INT32_MAX,
        unknown=_MAVLINK_INT32_MAX,
    )
    horizontal_accuracy_m = _accuracy_cm(values.get("eph"))
    vertical_accuracy_m = _accuracy_cm(values.get("epv"))
    age_s = 0.0
    reason: str | None = None
    if fix_type is None:
        reason = "fix_type_invalid"
    elif fix_type < MAVLINK_QUALIFIED_FIX_TYPE_MIN:
        reason = "fix_type_insufficient"
    elif satellites is None:
        reason = "satellites_unknown"
    elif satellites < MAVLINK_QUALIFIED_SATELLITES_MIN:
        reason = "satellites_insufficient"
    elif coordinates is None:
        reason = "coordinates_invalid"
    elif altitude_mm is None:
        reason = "altitude_invalid"
    elif horizontal_accuracy_m is None:
        reason = "horizontal_accuracy_unknown"
    elif horizontal_accuracy_m > max_horizontal_accuracy_m:
        reason = "horizontal_accuracy_exceeded"
    elif vertical_accuracy_m is None:
        reason = "vertical_accuracy_unknown"
    elif vertical_accuracy_m > max_vertical_accuracy_m:
        reason = "vertical_accuracy_exceeded"
    elif age_s > max_age_s:
        reason = "sample_stale"

    record: dict[str, object] = {
        "kind": "gps_raw",
        "message_type": message_type,
        "observed_at_monotonic_s": observed_at_monotonic_s,
        "observed_at_epoch_s": observed_at_epoch_s,
        "gps_observed_at_s": observed_at_monotonic_s,
        "gps_age_s": age_s,
        "gps_freshness": "fresh" if age_s <= max_age_s else "stale",
        "fix_type": fix_type,
        "satellites_visible": satellites,
        "satellites_visible_wire": satellites_wire,
        "latitude_deg": coordinates[0] if coordinates is not None else None,
        "longitude_deg": coordinates[1] if coordinates is not None else None,
        "altitude_m": altitude_mm / 1_000.0 if altitude_mm is not None else None,
        "horizontal_accuracy_m": horizontal_accuracy_m,
        "vertical_accuracy_m": vertical_accuracy_m,
        "velocity_mps": (
            _finite_number(values.get("vel"), non_negative=True) / 100.0
            if _finite_number(values.get("vel"), non_negative=True) is not None
            else None
        ),
        "course_deg": (
            _finite_number(values.get("cog"), non_negative=True) / 100.0
            if _finite_number(values.get("cog"), non_negative=True) is not None
            and float(values["cog"]) < 65_535.0
            else None
        ),
        "mavlink_time_usec": _wire_int(values.get("time_usec"), minimum=0, maximum=(1 << 64) - 1),
        "qualified": reason is None,
        "qualification_reason": reason,
    }
    return record


def _global_position_record(
    values: Mapping[str, object],
    *,
    observed_at_monotonic_s: float,
    observed_at_epoch_s: float,
) -> dict[str, object]:
    coordinates = _coordinate_e7(values)
    altitude_mm = _wire_int(
        values.get("alt"),
        minimum=_MAVLINK_INT32_MIN,
        maximum=_MAVLINK_INT32_MAX,
        unknown=_MAVLINK_INT32_MAX,
    )
    return {
        "kind": "global_position",
        "message_type": "GLOBAL_POSITION_INT",
        "observed_at_monotonic_s": observed_at_monotonic_s,
        "observed_at_epoch_s": observed_at_epoch_s,
        "latitude_deg": coordinates[0] if coordinates is not None else None,
        "longitude_deg": coordinates[1] if coordinates is not None else None,
        "altitude_m": altitude_mm / 1_000.0 if altitude_mm is not None else None,
        "relative_altitude_m": (
            _finite_number(values.get("relative_alt")) / 1_000.0
            if _finite_number(values.get("relative_alt")) is not None
            else None
        ),
        "velocity_north_mps": (
            _finite_number(values.get("vx")) / 100.0
            if _finite_number(values.get("vx")) is not None
            else None
        ),
        "velocity_east_mps": (
            _finite_number(values.get("vy")) / 100.0
            if _finite_number(values.get("vy")) is not None
            else None
        ),
        "velocity_down_mps": (
            _finite_number(values.get("vz")) / 100.0
            if _finite_number(values.get("vz")) is not None
            else None
        ),
        "heading_deg": (
            _finite_number(values.get("hdg"), non_negative=True) / 100.0
            if _finite_number(values.get("hdg"), non_negative=True) is not None
            and float(values["hdg"]) < 36_000.0
            else None
        ),
        "coordinate_valid": coordinates is not None,
    }


def capture_gnss_samples(
    endpoint: str,
    *,
    duration_seconds: float = 15.0,
    minimum_qualified_samples: int = 1,
    max_horizontal_accuracy_m: float = DEFAULT_MAX_HORIZONTAL_ACCURACY_M,
    max_vertical_accuracy_m: float = DEFAULT_MAX_VERTICAL_ACCURACY_M,
    max_age_s: float = DEFAULT_GPS_MAX_AGE_S,
    max_samples: int | None = None,
    connection_factory: Callable[..., Any] | None = None,
    monotonic_clock: Callable[[], float] = time.monotonic,
    epoch_clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    """Capture raw/fused GNSS messages without transmitting MAVLink frames."""

    if not isinstance(endpoint, str) or not endpoint.strip():
        raise GnssCaptureError("endpoint must be a non-empty string")
    if (
        isinstance(duration_seconds, bool)
        or not math.isfinite(duration_seconds)
        or duration_seconds <= 0
    ):
        raise GnssCaptureError("duration_seconds must be finite and positive")
    if (
        isinstance(minimum_qualified_samples, bool)
        or not isinstance(minimum_qualified_samples, int)
        or minimum_qualified_samples <= 0
    ):
        raise GnssCaptureError("minimum_qualified_samples must be a positive integer")
    for value, name in (
        (max_horizontal_accuracy_m, "max_horizontal_accuracy_m"),
        (max_vertical_accuracy_m, "max_vertical_accuracy_m"),
        (max_age_s, "max_age_s"),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0.0:
            raise GnssCaptureError(f"{name} must be finite and non-negative")
    if max_samples is not None and (
        isinstance(max_samples, bool) or not isinstance(max_samples, int) or max_samples <= 0
    ):
        raise GnssCaptureError("max_samples must be a positive integer when supplied")

    if connection_factory is None:
        try:
            from pymavlink import mavutil
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise GnssCaptureError("pymavlink is required for live GNSS capture") from exc
        connection_factory = mavutil.mavlink_connection

    started_monotonic = float(monotonic_clock())
    started_epoch = float(epoch_clock())
    if not math.isfinite(started_monotonic) or not math.isfinite(started_epoch):
        raise GnssCaptureError("capture clocks must return finite values")
    connection = connection_factory(
        endpoint,
        autoreconnect=False,
        robust_parsing=True,
    )
    records: list[dict[str, object]] = []
    message_count = 0
    bad_data_count = 0
    decode_errors: list[str] = []
    stop_reason = "duration"
    try:
        while True:
            now_monotonic = float(monotonic_clock())
            if not math.isfinite(now_monotonic):
                raise GnssCaptureError("capture clock returned a non-finite value")
            remaining = duration_seconds - (now_monotonic - started_monotonic)
            if remaining <= 0.0:
                stop_reason = "duration"
                break
            if max_samples is not None and len(records) >= max_samples:
                stop_reason = "max_samples"
                break
            message = connection.recv_match(
                blocking=True,
                timeout=min(0.5, max(0.0, remaining)),
            )
            if message is None:
                continue
            message_type = getattr(message, "get_type", lambda: "")()
            if message_type == "BAD_DATA":
                bad_data_count += 1
                continue
            message_count += 1
            if message_type not in {"GPS_RAW_INT", "GPS2_RAW", "GLOBAL_POSITION_INT"}:
                continue
            observed_monotonic = float(monotonic_clock())
            observed_epoch = float(epoch_clock())
            if not math.isfinite(observed_monotonic) or not math.isfinite(observed_epoch):
                raise GnssCaptureError("capture clocks must return finite values")
            values = _message_values(message)
            try:
                if message_type in {"GPS_RAW_INT", "GPS2_RAW"}:
                    record = _raw_gps_record(
                        message_type,
                        values,
                        observed_at_monotonic_s=observed_monotonic,
                        observed_at_epoch_s=observed_epoch,
                        max_horizontal_accuracy_m=max_horizontal_accuracy_m,
                        max_vertical_accuracy_m=max_vertical_accuracy_m,
                        max_age_s=max_age_s,
                    )
                else:
                    record = _global_position_record(
                        values,
                        observed_at_monotonic_s=observed_monotonic,
                        observed_at_epoch_s=observed_epoch,
                    )
            except (TypeError, ValueError, OverflowError) as exc:
                decode_errors.append(f"{message_type}: {exc}")
                continue
            records.append(record)
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    ended_monotonic = float(monotonic_clock())
    ended_epoch = float(epoch_clock())
    if not math.isfinite(ended_monotonic) or not math.isfinite(ended_epoch):
        raise GnssCaptureError("capture clocks must return finite values")
    raw_records = [record for record in records if record.get("kind") == "gps_raw"]
    global_records = [record for record in records if record.get("kind") == "global_position"]
    qualified_count = sum(record.get("qualified") is True for record in raw_records)
    threshold_policy_passed = (
        max_horizontal_accuracy_m <= CANONICAL_MAX_HORIZONTAL_ACCURACY_M
        and max_vertical_accuracy_m <= CANONICAL_MAX_VERTICAL_ACCURACY_M
        and max_age_s <= CANONICAL_MAX_GPS_AGE_S
    )
    summary = {
        "message_count": message_count,
        "bad_data_count": bad_data_count,
        "record_count": len(records),
        "gps_raw_samples": len(raw_records),
        "gps_raw_qualified_samples": qualified_count,
        "global_position_samples": len(global_records),
        "minimum_qualified_samples": minimum_qualified_samples,
        "max_horizontal_accuracy_m": max_horizontal_accuracy_m,
        "max_vertical_accuracy_m": max_vertical_accuracy_m,
        "max_age_s": max_age_s,
        "decode_error_count": len(decode_errors),
        "stop_reason": stop_reason,
        "qualified_sample_gate_passed": qualified_count >= minimum_qualified_samples,
        "threshold_policy_passed": threshold_policy_passed,
        "gate_passed": (
            qualified_count >= minimum_qualified_samples
            and not decode_errors
            and threshold_policy_passed
        ),
    }
    return {
        "schema_version": GNSS_CAPTURE_SCHEMA_VERSION,
        "event": "multidetect.gnss-capture.completed",
        "evidence_type": GNSS_CAPTURE_EVIDENCE_TYPE,
        "endpoint": endpoint,
        "started_at_epoch_s": started_epoch,
        "ended_at_epoch_s": ended_epoch,
        "duration_seconds": max(0.0, ended_monotonic - started_monotonic),
        "read_only": True,
        "messages_transmitted": 0,
        "contains_flight_control": False,
        "contains_truth": False,
        "ground_truth_isolated": True,
        "advisory_only": True,
        "hardware_acceptance": "not_claimed",
        "real_flight_acceptance": "not_claimed",
        "decode_errors": decode_errors,
        "records": records,
        "summary": summary,
        "notes": [
            "The capture is receive-only and does not send MAVLink frames.",
            "GPS_RAW_INT qualification uses fix_type 3..8, known satellites 6..254,",
            "valid WGS-84 coordinates/altitude, hAcc<=6 m and vAcc<=10 m.",
            "Thresholds may be tightened for diagnostics but cannot be relaxed past the",
            "canonical hAcc<=6 m, vAcc<=10 m and GPS age<=1.5 s policy gate.",
            "A qualified sample is a sensor sub-gate; it does not prove target truth,",
            "camera/range freshness, actuator safety, HIL, REAL or outdoor accuracy.",
        ],
    }


def write_gnss_capture_report(
    path: str | Path,
    report: Mapping[str, object],
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write a GNSS capture JSON report."""

    if type(overwrite) is not bool:
        raise GnssCaptureError("overwrite must be a boolean")
    destination = Path(path).expanduser().resolve()
    if destination.suffix.lower() != ".json":
        raise GnssCaptureError("GNSS capture report must use a .json suffix")
    if destination.exists() and not overwrite:
        raise FileExistsError(f"GNSS capture report already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(report, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _valid_number(value: object, *, non_negative: bool = False) -> bool:
    """Return whether ``value`` is a finite JSON number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and (not non_negative or number >= 0.0)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate object keys instead of silently accepting last-wins JSON."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        result[key] = value
    return result


def _valid_optional_number(value: object, *, non_negative: bool = False) -> bool:
    return value is None or _valid_number(value, non_negative=non_negative)


def _valid_optional_integer(
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _verify_raw_record(
    record: Mapping[str, object],
    index: int,
    *,
    max_horizontal_accuracy_m: float,
    max_vertical_accuracy_m: float,
    max_age_s: float,
) -> list[str]:
    reasons: list[str] = []
    prefix = f"records[{index}]"
    if record.get("message_type") not in {"GPS_RAW_INT", "GPS2_RAW"}:
        reasons.append(f"{prefix}.message_type is invalid")
    for field in ("observed_at_monotonic_s", "observed_at_epoch_s", "gps_observed_at_s"):
        if not _valid_number(record.get(field), non_negative=True):
            reasons.append(f"{prefix}.{field} must be a finite non-negative number")
    if not _valid_number(record.get("gps_age_s"), non_negative=True):
        reasons.append(f"{prefix}.gps_age_s must be a finite non-negative number")
    elif record.get("gps_age_s", 0.0) > max_age_s:
        if record.get("gps_freshness") == "fresh":
            reasons.append(f"{prefix}.freshness is inconsistent with gps_age_s")
    if record.get("gps_freshness") not in {"fresh", "stale"}:
        reasons.append(f"{prefix}.gps_freshness must be fresh or stale")
    if not _valid_optional_integer(record.get("fix_type"), minimum=0, maximum=8):
        reasons.append(f"{prefix}.fix_type is outside MAVLink range")
    if not _valid_optional_integer(record.get("satellites_visible"), minimum=0, maximum=254):
        reasons.append(f"{prefix}.satellites_visible is outside the known range")
    if not _valid_optional_integer(record.get("satellites_visible_wire"), minimum=0, maximum=255):
        reasons.append(f"{prefix}.satellites_visible_wire is outside MAVLink range")
    latitude = record.get("latitude_deg")
    longitude = record.get("longitude_deg")
    if latitude is not None and (not _valid_number(latitude) or not -90.0 < float(latitude) < 90.0):
        reasons.append(f"{prefix}.latitude_deg is outside the open WGS-84 latitude interval")
    if longitude is not None and (
        not _valid_number(longitude) or not -180.0 <= float(longitude) <= 180.0
    ):
        reasons.append(f"{prefix}.longitude_deg is outside the WGS-84 longitude interval")
    if not _valid_optional_number(record.get("altitude_m")):
        reasons.append(f"{prefix}.altitude_m must be finite or null")
    for field in ("horizontal_accuracy_m", "vertical_accuracy_m", "velocity_mps", "course_deg"):
        if not _valid_optional_number(record.get(field), non_negative=True):
            reasons.append(f"{prefix}.{field} must be finite and non-negative or null")
    if not _valid_optional_integer(record.get("mavlink_time_usec"), minimum=0):
        reasons.append(f"{prefix}.mavlink_time_usec must be a non-negative integer or null")
    qualified = record.get("qualified")
    reason = record.get("qualification_reason")
    if type(qualified) is not bool:
        reasons.append(f"{prefix}.qualified must be a boolean")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        reasons.append(f"{prefix}.qualification_reason must be a non-empty string or null")
    if type(qualified) is bool and (
        (qualified and reason is not None) or (not qualified and reason is None)
    ):
        reasons.append(f"{prefix}.qualified and qualification_reason are inconsistent")

    if qualified is True:
        if (
            not isinstance(record.get("fix_type"), int)
            or record["fix_type"] < MAVLINK_QUALIFIED_FIX_TYPE_MIN
        ):
            reasons.append(
                f"{prefix}.qualified requires fix_type >= {MAVLINK_QUALIFIED_FIX_TYPE_MIN}"
            )
        if (
            not isinstance(record.get("satellites_visible"), int)
            or record["satellites_visible"] < MAVLINK_QUALIFIED_SATELLITES_MIN
        ):
            reasons.append(
                f"{prefix}.qualified requires known satellites >= "
                f"{MAVLINK_QUALIFIED_SATELLITES_MIN}"
            )
        if latitude is None or longitude is None:
            reasons.append(f"{prefix}.qualified requires valid coordinates")
        if record.get("altitude_m") is None:
            reasons.append(f"{prefix}.qualified requires altitude")
        horizontal = record.get("horizontal_accuracy_m")
        vertical = record.get("vertical_accuracy_m")
        if not _valid_number(horizontal, non_negative=True) or float(horizontal) > (
            max_horizontal_accuracy_m
        ):
            reasons.append(f"{prefix}.qualified exceeds horizontal accuracy gate")
        if not _valid_number(vertical, non_negative=True) or float(vertical) > (
            max_vertical_accuracy_m
        ):
            reasons.append(f"{prefix}.qualified exceeds vertical accuracy gate")
        if (
            not _valid_number(record.get("gps_age_s"), non_negative=True)
            or float(record["gps_age_s"]) > max_age_s
        ):
            reasons.append(f"{prefix}.qualified exceeds GPS freshness gate")
        if record.get("gps_freshness") != "fresh":
            reasons.append(f"{prefix}.qualified requires fresh GPS")
    return reasons


def _verify_global_record(record: Mapping[str, object], index: int) -> list[str]:
    reasons: list[str] = []
    prefix = f"records[{index}]"
    if record.get("message_type") != "GLOBAL_POSITION_INT":
        reasons.append(f"{prefix}.message_type is invalid")
    for field in ("observed_at_monotonic_s", "observed_at_epoch_s"):
        if not _valid_number(record.get(field), non_negative=True):
            reasons.append(f"{prefix}.{field} must be a finite non-negative number")
    latitude = record.get("latitude_deg")
    longitude = record.get("longitude_deg")
    coordinate_valid = record.get("coordinate_valid")
    coordinates_valid = (
        _valid_number(latitude)
        and _valid_number(longitude)
        and -90.0 < float(latitude) < 90.0
        and -180.0 <= float(longitude) <= 180.0
    )
    if not _valid_optional_number(latitude) or (
        latitude is not None and not -90.0 < float(latitude) < 90.0
    ):
        reasons.append(f"{prefix}.latitude_deg is outside the open WGS-84 latitude interval")
    if not _valid_optional_number(longitude) or (
        longitude is not None and not -180.0 <= float(longitude) <= 180.0
    ):
        reasons.append(f"{prefix}.longitude_deg is outside the WGS-84 longitude interval")
    if type(coordinate_valid) is not bool:
        reasons.append(f"{prefix}.coordinate_valid must be a boolean")
    elif coordinate_valid != coordinates_valid:
        reasons.append(f"{prefix}.coordinate_valid does not match coordinates")
    if not _valid_optional_number(record.get("altitude_m")):
        reasons.append(f"{prefix}.altitude_m must be finite or null")
    for field in (
        "relative_altitude_m",
        "velocity_north_mps",
        "velocity_east_mps",
        "velocity_down_mps",
    ):
        if not _valid_optional_number(record.get(field)):
            reasons.append(f"{prefix}.{field} must be finite or null")
    if not _valid_optional_number(record.get("heading_deg"), non_negative=True):
        reasons.append(f"{prefix}.heading_deg must be finite and non-negative or null")
    return reasons


def verify_gnss_capture_report(
    report: str | Path | Mapping[str, object], *, require_gate_passed: bool = False
) -> dict[str, object]:
    """Verify a saved capture's structure, record invariants and quality counts."""

    if type(require_gate_passed) is not bool:
        raise GnssCaptureError("require_gate_passed must be a boolean")
    reasons: list[str] = []
    path: Path | None = None
    if isinstance(report, (str, Path)):
        path = Path(report).expanduser().resolve()
        try:
            document = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return {"passed": False, "gate_passed": False, "reasons": [str(exc)]}
    else:
        document = report
    if not isinstance(document, Mapping):
        return {"passed": False, "gate_passed": False, "reasons": ["report must be a JSON object"]}
    if document.get("schema_version") != GNSS_CAPTURE_SCHEMA_VERSION:
        reasons.append("unsupported schema_version")
    if document.get("event") != "multidetect.gnss-capture.completed":
        reasons.append("unexpected event")
    if document.get("evidence_type") != GNSS_CAPTURE_EVIDENCE_TYPE:
        reasons.append("unexpected evidence_type")
    expected_constants: dict[str, object] = {
        "read_only": True,
        "messages_transmitted": 0,
        "contains_flight_control": False,
        "contains_truth": False,
        "ground_truth_isolated": True,
        "advisory_only": True,
        "hardware_acceptance": "not_claimed",
        "real_flight_acceptance": "not_claimed",
    }
    for field, expected in expected_constants.items():
        if document.get(field) != expected:
            reasons.append(f"{field} must remain {expected!r}")
    endpoint = document.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        reasons.append("endpoint must be a non-empty string")
    for field in ("started_at_epoch_s", "ended_at_epoch_s", "duration_seconds"):
        if not _valid_number(document.get(field), non_negative=True):
            reasons.append(f"{field} must be a finite non-negative number")
    records = document.get("records")
    summary = document.get("summary")
    if not isinstance(records, list):
        reasons.append("records must be an array")
        records = []
    if not isinstance(summary, Mapping):
        reasons.append("summary must be an object")
        summary = {}
    raw_records = []
    global_records = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            reasons.append(f"records[{index}] must be an object")
            continue
        kind = record.get("kind")
        if kind == "gps_raw":
            raw_records.append(record)
            reasons.extend(
                _verify_raw_record(
                    record,
                    index,
                    max_horizontal_accuracy_m=(
                        float(summary.get("max_horizontal_accuracy_m", 6.0))
                        if _valid_number(
                            summary.get("max_horizontal_accuracy_m"), non_negative=True
                        )
                        else 6.0
                    ),
                    max_vertical_accuracy_m=(
                        float(summary.get("max_vertical_accuracy_m", 10.0))
                        if _valid_number(summary.get("max_vertical_accuracy_m"), non_negative=True)
                        else 10.0
                    ),
                    max_age_s=(
                        float(summary.get("max_age_s", DEFAULT_GPS_MAX_AGE_S))
                        if _valid_number(summary.get("max_age_s"), non_negative=True)
                        else DEFAULT_GPS_MAX_AGE_S
                    ),
                )
            )
        elif kind == "global_position":
            global_records.append(record)
            reasons.extend(_verify_global_record(record, index))
        else:
            reasons.append(f"records[{index}].kind is unsupported")
    qualified_count = sum(record.get("qualified") is True for record in raw_records)
    expected_counts = {
        "record_count": len(records),
        "gps_raw_samples": len(raw_records),
        "gps_raw_qualified_samples": qualified_count,
        "global_position_samples": len(global_records),
    }
    for field, expected in expected_counts.items():
        if summary.get(field) != expected:
            reasons.append(f"summary.{field} does not match records")
    integer_fields = (
        "message_count",
        "bad_data_count",
        "record_count",
        "gps_raw_samples",
        "gps_raw_qualified_samples",
        "global_position_samples",
        "decode_error_count",
    )
    for field in integer_fields:
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append(f"summary.{field} must be a non-negative integer")
    for field in ("max_horizontal_accuracy_m", "max_vertical_accuracy_m", "max_age_s"):
        if not _valid_number(summary.get(field), non_negative=True):
            reasons.append(f"summary.{field} must be finite and non-negative")
    threshold_policy_passed = (
        _valid_number(summary.get("max_horizontal_accuracy_m"), non_negative=True)
        and float(summary["max_horizontal_accuracy_m"]) <= CANONICAL_MAX_HORIZONTAL_ACCURACY_M
        and _valid_number(summary.get("max_vertical_accuracy_m"), non_negative=True)
        and float(summary["max_vertical_accuracy_m"]) <= CANONICAL_MAX_VERTICAL_ACCURACY_M
        and _valid_number(summary.get("max_age_s"), non_negative=True)
        and float(summary["max_age_s"]) <= CANONICAL_MAX_GPS_AGE_S
    )
    if summary.get("stop_reason") not in {"duration", "max_samples"}:
        reasons.append("summary.stop_reason is invalid")
    for field in ("qualified_sample_gate_passed", "gate_passed"):
        if type(summary.get(field)) is not bool:
            reasons.append(f"summary.{field} must be a boolean")
    if type(summary.get("threshold_policy_passed")) is not bool:
        reasons.append("summary.threshold_policy_passed must be a boolean")
    elif summary.get("threshold_policy_passed") is not threshold_policy_passed:
        reasons.append("summary.threshold_policy_passed does not match canonical limits")
    declared_qualified_gate = summary.get("qualified_sample_gate_passed")
    declared_gate = summary.get("gate_passed")
    minimum = summary.get("minimum_qualified_samples")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
        reasons.append("summary.minimum_qualified_samples must be a positive integer")
        minimum = 1
    decode_errors = document.get("decode_errors")
    if not isinstance(decode_errors, list) or any(
        not isinstance(item, str) or not item.strip() for item in decode_errors
    ):
        reasons.append("decode_errors must be an array of non-empty strings")
        decode_errors = []
    if summary.get("decode_error_count") != len(decode_errors):
        reasons.append("summary.decode_error_count does not match decode_errors")
    if isinstance(summary.get("message_count"), int) and summary["message_count"] < len(records):
        reasons.append("summary.message_count cannot be lower than record_count")
    recomputed_qualified_gate = qualified_count >= minimum
    recomputed_gate = recomputed_qualified_gate and not decode_errors and threshold_policy_passed
    if declared_qualified_gate is not recomputed_qualified_gate:
        reasons.append("summary.qualified_sample_gate_passed does not match sample count")
    if declared_gate is not recomputed_gate:
        reasons.append("summary.gate_passed does not match recomputed quality gate")
    if require_gate_passed and not recomputed_gate:
        reasons.append("GNSS capture quality gate is not passed")
    return {
        "schema_version": GNSS_CAPTURE_SCHEMA_VERSION,
        "path": str(path) if path is not None else None,
        "passed": not reasons,
        "gate_passed": recomputed_gate and not reasons,
        "record_count": len(records),
        "qualified_sample_count": qualified_count,
        "decode_error_count": len(decode_errors),
        "reasons": reasons,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="udpin:0.0.0.0:14562")
    parser.add_argument("--duration-seconds", type=float, default=15.0)
    parser.add_argument("--minimum-qualified-samples", type=int, default=1)
    parser.add_argument(
        "--max-horizontal-accuracy-m",
        type=float,
        default=GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    )
    parser.add_argument(
        "--max-vertical-accuracy-m",
        type=float,
        default=GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    )
    parser.add_argument("--max-age-s", type=float, default=DEFAULT_GPS_MAX_AGE_S)
    parser.add_argument("--max-samples", type=int)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = capture_gnss_samples(
            args.endpoint,
            duration_seconds=args.duration_seconds,
            minimum_qualified_samples=args.minimum_qualified_samples,
            max_horizontal_accuracy_m=args.max_horizontal_accuracy_m,
            max_vertical_accuracy_m=args.max_vertical_accuracy_m,
            max_age_s=args.max_age_s,
            max_samples=args.max_samples,
        )
        output = write_gnss_capture_report(args.output, report, overwrite=args.overwrite)
    except (GnssCaptureError, FileExistsError, OSError, TypeError, ValueError) as exc:
        print(f"GNSS capture failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"output": str(output), **report["summary"]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["summary"]["gate_passed"] else 1  # type: ignore[index]


def verify_main(argv: Sequence[str] | None = None) -> int:
    """Verify a saved GNSS capture without opening a MAVLink connection."""

    parser = argparse.ArgumentParser(
        description=(
            "Verify a saved Multi-Detect GNSS capture; no MAVLink connection or network "
            "probe is performed."
        )
    )
    parser.add_argument("report", type=Path, help="saved GNSS capture JSON report")
    parser.add_argument(
        "--require-gate",
        action="store_true",
        help="return failure unless the recomputed GNSS quality gate is passed",
    )
    args = parser.parse_args(argv)
    try:
        result = verify_gnss_capture_report(args.report, require_gate_passed=args.require_gate)
    except (GnssCaptureError, OSError, TypeError, ValueError) as exc:
        print(f"GNSS capture verification failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] else 1


__all__ = [
    "CANONICAL_MAX_GPS_AGE_S",
    "CANONICAL_MAX_HORIZONTAL_ACCURACY_M",
    "CANONICAL_MAX_VERTICAL_ACCURACY_M",
    "DEFAULT_GPS_MAX_AGE_S",
    "GNSS_CAPTURE_EVIDENCE_TYPE",
    "GNSS_CAPTURE_SCHEMA_VERSION",
    "GnssCaptureError",
    "build_parser",
    "capture_gnss_samples",
    "main",
    "verify_main",
    "verify_gnss_capture_report",
    "write_gnss_capture_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
