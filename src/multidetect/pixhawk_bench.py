from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import VehicleTelemetry


@dataclass(frozen=True, slots=True)
class PixhawkBenchConfig:
    minimum_samples: int = 100
    sample_interval_seconds: float = 0.2
    maximum_qgc_age_seconds: float = 120.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_samples, bool)
            or not isinstance(self.minimum_samples, int)
            or self.minimum_samples <= 0
        ):
            raise ValueError("Pixhawk bench minimum samples must be a positive integer")
        for name, value, allow_zero in (
            ("sample interval", self.sample_interval_seconds, True),
            ("maximum QGC age", self.maximum_qgc_age_seconds, False),
        ):
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                raise ValueError(f"Pixhawk bench {name} must be finite and non-negative")


def load_qgc_telemetry_snapshot(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("QGC telemetry snapshot cannot be read as JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("QGC telemetry snapshot must be a JSON object")
    if value.get("schema_version") != 1:
        raise ValueError("QGC telemetry snapshot schema_version must equal 1")
    if value.get("hardware_model") != "Pixhawk V6X":
        raise ValueError("QGC telemetry snapshot hardware_model must be Pixhawk V6X")
    if not isinstance(value.get("firmware_version"), str) or not value["firmware_version"].strip():
        raise ValueError("QGC telemetry snapshot firmware_version is required")
    if value.get("airframe_stationary") is not True:
        raise ValueError("QGC telemetry comparison requires an airframe_stationary bench")
    captured = value.get("captured_at_utc")
    if not isinstance(captured, str):
        raise ValueError("QGC telemetry snapshot captured_at_utc is required")
    try:
        parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("QGC telemetry snapshot captured_at_utc is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("QGC telemetry snapshot captured_at_utc requires a timezone")
    fields = value.get("fields")
    if not isinstance(fields, Mapping):
        raise ValueError("QGC telemetry snapshot fields must be an object")
    _validate_qgc_fields(fields)
    return value


def run_pixhawk_v6x_bench(
    provider: Any,
    qgc_snapshot: Mapping[str, Any],
    config: PixhawkBenchConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    observed_at: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    snapshots: list[VehicleTelemetry] = []
    sample_times: list[float] = []
    reasons: list[str] = []
    for index in range(config.minimum_samples):
        now_s = clock()
        try:
            snapshot = provider.snapshot(now_s=now_s)
        except RuntimeError:
            reasons.append("Pixhawk telemetry receive failed during the bench")
            break
        snapshots.append(snapshot)
        sample_times.append(now_s)
        if index + 1 < config.minimum_samples and config.sample_interval_seconds > 0:
            sleeper(config.sample_interval_seconds)

    fresh_sample_count = sum(
        item.link_healthy is True and item.position_healthy is True for item in snapshots
    )
    if fresh_sample_count < config.minimum_samples:
        reasons.append(
            "Pixhawk did not provide the required number of fresh link and position samples"
        )
    messages_transmitted = int(getattr(provider, "messages_transmitted", -1))
    if getattr(provider, "is_read_only", False) is not True:
        reasons.append("Pixhawk provider is not read-only")
    if messages_transmitted != 0:
        reasons.append("Jetson transmitted messages to Pixhawk")

    latest = snapshots[-1] if snapshots else None
    qgc_comparison = _compare_qgc_fields(latest, qgc_snapshot.get("fields", {}))
    qgc_field_match = all(item["matched"] for item in qgc_comparison.values())
    if not qgc_field_match:
        reasons.append("Pixhawk telemetry does not match the QGC bench snapshot")

    timestamp = observed_at()
    if timestamp.tzinfo is None:
        raise ValueError("Pixhawk bench observation time must include a timezone")
    timestamp = timestamp.astimezone(UTC)
    qgc_fresh = _qgc_snapshot_is_fresh(
        qgc_snapshot,
        now=timestamp,
        maximum_age_seconds=config.maximum_qgc_age_seconds,
    )
    if not qgc_fresh:
        reasons.append("QGC telemetry snapshot is stale or in the future")

    link_loss_fail_closed = False
    if latest is not None and sample_times and fresh_sample_count > 0:
        stale_after = float(provider.config.stale_after_seconds)
        stale = provider.cached_snapshot(now_s=sample_times[-1] + stale_after + 0.001)
        link_loss_fail_closed = (
            stale.link_healthy is False and stale.position_healthy is False
        )
    if not link_loss_fail_closed:
        reasons.append("cached telemetry did not fail closed after the stale timeout")

    hardware_model = qgc_snapshot.get("hardware_model")
    firmware_version = qgc_snapshot.get("firmware_version")
    hardware_observed = fresh_sample_count > 0 and hardware_model == "Pixhawk V6X"
    passed = not reasons
    return {
        "event": f"pixhawk_v6x_bench_{'passed' if passed else 'failed'}",
        "observed_at_utc": timestamp.isoformat(),
        "hardware_observed": hardware_observed,
        "simulation_only": False,
        "passed": passed,
        "reasons": reasons,
        "hardware_model": hardware_model,
        "firmware_version": firmware_version,
        "sample_count": len(snapshots),
        "fresh_sample_count": fresh_sample_count,
        "sample_interval_seconds": config.sample_interval_seconds,
        "read_only": getattr(provider, "is_read_only", False),
        "messages_transmitted_by_jetson": messages_transmitted,
        "qgc_snapshot_fresh": qgc_fresh,
        "qgc_field_match": qgc_field_match,
        "qgc_comparison": qgc_comparison,
        "link_loss_fail_closed": link_loss_fail_closed,
        "link_loss_method": "cached_staleness_without_receive",
        "latest": _telemetry_document(latest) if latest is not None else None,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "hardware_control_enabled": False,
    }


def _validate_qgc_fields(fields: Mapping[str, Any]) -> None:
    numeric = (
        "latitude_deg",
        "longitude_deg",
        "altitude_agl_m",
        "heading_deg",
        "ground_speed_mps",
        "roll_deg",
        "pitch_deg",
        "battery_remaining_pct",
    )
    for name in numeric:
        value = fields.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"QGC telemetry field {name} must be finite")
    for name in ("satellites_visible", "mission_sequence"):
        value = fields.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"QGC telemetry field {name} must be a non-negative integer")
    if not isinstance(fields.get("armed"), bool):
        raise ValueError("QGC telemetry field armed must be boolean")
    if not isinstance(fields.get("flight_mode"), str) or not fields["flight_mode"].strip():
        raise ValueError("QGC telemetry field flight_mode is required")


def _compare_qgc_fields(
    telemetry: VehicleTelemetry | None,
    fields: object,
) -> dict[str, dict[str, Any]]:
    if telemetry is None or not isinstance(fields, Mapping):
        return {"telemetry": {"matched": False, "reason": "telemetry is unavailable"}}
    tolerances = {
        "latitude_deg": 0.0001,
        "longitude_deg": 0.0001,
        "altitude_agl_m": 2.0,
        "heading_deg": 10.0,
        "ground_speed_mps": 2.0,
        "roll_deg": 5.0,
        "pitch_deg": 5.0,
        "battery_remaining_pct": 5.0,
        "satellites_visible": 2.0,
        "mission_sequence": 0.0,
    }
    result: dict[str, dict[str, Any]] = {}
    for name, tolerance in tolerances.items():
        actual = getattr(telemetry, name)
        expected = fields.get(name)
        if actual is None or (isinstance(actual, float) and not math.isfinite(actual)):
            matched = False
            difference = None
        else:
            difference = abs(float(actual) - float(expected))
            if name == "heading_deg":
                difference %= 360.0
                difference = min(difference, 360.0 - difference)
            matched = difference <= tolerance
        result[name] = {
            "actual": actual if not isinstance(actual, float) or math.isfinite(actual) else None,
            "expected": expected,
            "tolerance": tolerance,
            "difference": difference,
            "matched": matched,
        }
    for name in ("armed", "flight_mode"):
        actual = getattr(telemetry, name)
        expected = fields.get(name)
        if name == "flight_mode" and isinstance(actual, str) and isinstance(expected, str):
            matched = actual.strip().upper() == expected.strip().upper()
        else:
            matched = actual == expected
        result[name] = {"actual": actual, "expected": expected, "matched": matched}
    return result


def _qgc_snapshot_is_fresh(
    snapshot: Mapping[str, Any], *, now: datetime, maximum_age_seconds: float
) -> bool:
    raw = snapshot.get("captured_at_utc")
    if not isinstance(raw, str):
        return False
    try:
        captured = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if captured.tzinfo is None:
        return False
    age = (now - captured.astimezone(UTC)).total_seconds()
    return 0 <= age <= maximum_age_seconds


def _telemetry_document(telemetry: VehicleTelemetry) -> dict[str, Any]:
    names = (
        "latitude_deg",
        "longitude_deg",
        "altitude_agl_m",
        "heading_deg",
        "ground_speed_mps",
        "roll_deg",
        "pitch_deg",
        "battery_remaining_pct",
        "satellites_visible",
        "armed",
        "flight_mode",
        "mission_sequence",
        "link_healthy",
        "position_healthy",
    )
    result: dict[str, Any] = {}
    for name in names:
        value = getattr(telemetry, name)
        result[name] = None if isinstance(value, float) and not math.isfinite(value) else value
    return result


__all__ = [
    "PixhawkBenchConfig",
    "load_qgc_telemetry_snapshot",
    "run_pixhawk_v6x_bench",
]
