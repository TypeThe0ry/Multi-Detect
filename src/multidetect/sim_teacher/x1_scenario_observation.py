"""Pure projection of a bounded synthetic perception report for X1 scenarios."""

from __future__ import annotations

from collections.abc import Mapping

from multidetect.autonomy.x1_contingency_state import GROUPS, require_token
from multidetect.autonomy.x1_perception_tools import InjectedTargetReading

TARGET_FOUND_TTL_MS = GROUPS["target"][0]
_FIELDS = {"track_ref", "class", "bearing_deg", "elevation_deg", "position_estimate"}
_POSITION_FIELDS = {"lat_deg", "lon_deg", "alt_amsl_m", "error_bound_m"}


def _fields(value: object, required: set[str], name: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError(f"target_found.{name} must contain exactly: {', '.join(sorted(required))}")
    return value


def _token(value: object, field: str) -> str:
    try:
        return require_token(value)
    except ValueError as exc:
        raise ValueError(f"target_found.{field} must be a valid token") from exc


def _number(value: object, field: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"target_found.{field} is out of range")
    return float(value)


def validate_target_found(values: object) -> dict:
    """Validate the complete injected payload and return a detached normalized copy.

    Angles are in vehicle body FRD: bearing is signed from forward toward right
    and elevation is positive above the body forward/right plane. Position uses
    WGS84 coordinates, AMSL altitude and a positive 3-D error bound in meters.
    Position limits are input bounds, independent of the flight envelope.
    """
    values = _fields(values, _FIELDS, "values")
    pos = _fields(values["position_estimate"], _POSITION_FIELDS, "position_estimate")
    error = _number(pos["error_bound_m"], "position_estimate.error_bound_m", 0, 1000)
    if error == 0:
        raise ValueError("target_found.position_estimate.error_bound_m must be positive")
    return {
        "track_ref": _token(values["track_ref"], "track_ref"),
        "class": _token(values["class"], "class"),
        "bearing_deg": _number(values["bearing_deg"], "bearing_deg", -180, 180),
        "elevation_deg": _number(values["elevation_deg"], "elevation_deg", -90, 90),
        "position_estimate": {
            "lat_deg": _number(pos["lat_deg"], "position_estimate.lat_deg", -90, 90),
            "lon_deg": _number(pos["lon_deg"], "position_estimate.lon_deg", -180, 180),
            "alt_amsl_m": _number(pos["alt_amsl_m"], "position_estimate.alt_amsl_m", -500, 10_000),
            "error_bound_m": error,
        },
    }


def project_target_found(
    values: object, *, observed_ms: int, now_ms: int, end_ms: int | None = None,
) -> dict | None:
    """Project one observation using timestamps in the same caller-supplied clock.

    Ingest ``target_status`` once at ``t_ms`` in state builder clock order,
    using ``source``. A perception provider can expose ``detection`` with the
    same provenance and must keep ``observed_ms`` fixed across reads. The caller
    owns event deduplication and clearing any cached status at an optional
    exclusive ``end_ms`` cutoff earlier than the state builder TTL.
    """
    detection = validate_target_found(values)
    for name, stamp in (("observed_ms", observed_ms), ("now_ms", now_ms), ("end_ms", end_ms)):
        if name == "end_ms" and stamp is None:
            continue
        if type(stamp) is not int or stamp < 0:
            raise ValueError(f"target_found.{name} must be a nonnegative integer")
    if end_ms is not None and end_ms <= observed_ms:
        raise ValueError("target_found.end_ms must be after observed_ms")
    age_ms = now_ms - observed_ms
    if not 0 <= age_ms <= TARGET_FOUND_TTL_MS or (end_ms is not None and now_ms >= end_ms):
        return None
    # A single detection indicates a tentative track and supplies no identity confidence.
    return {
        "source": "fault",
        "t_ms": observed_ms,
        "age_ms": age_ms,
        "target_status": {"visible": True, "track_state": "tentative", "track_ref": detection["track_ref"]},
        "detection": detection,
    }


def injected_target_reading(report: Mapping | None) -> InjectedTargetReading | None:
    """Adapt the original measurement, without refreshing its capture time."""
    if report is None or report.get("source") != "fault":
        return None
    detection = report["detection"]
    position = detection["position_estimate"]
    return InjectedTargetReading(report["t_ms"], detection["track_ref"], detection["class"],
                                 detection["bearing_deg"], detection["elevation_deg"],
                                 tuple(position[k] for k in ("lat_deg", "lon_deg", "alt_amsl_m")),
                                 position["error_bound_m"])
