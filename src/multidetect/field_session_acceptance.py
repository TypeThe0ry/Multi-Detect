from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .autonomy.serialization import strict_json_loads
from .field_link_preflight import verify_field_link_report
from .mavlink_gps import (
    GNSS_CANONICAL_MAX_GPS_AGE_S,
    GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    MAVLINK_GPS_FIX_TYPE_MAX,
    MAVLINK_QUALIFIED_FIX_TYPE_MIN,
    MAVLINK_QUALIFIED_SATELLITES_MIN,
    MAVLINK_SATELLITES_MAX,
)

_REQUIRED_SEQUENCE = ("DET", "TRK", "LCK", "TGT")
_STATE_ALIASES = {
    "DET": "DET",
    "DETECT": "DET",
    "DETECTED": "DET",
    "ACQUIRE": "DET",
    "ACQUIRED": "DET",
    "TRK": "TRK",
    "TRACK": "TRK",
    "TRACKING": "TRK",
    "FOLLOW": "TRK",
    "LCK": "LCK",
    "LOCK": "LCK",
    "LOCKED": "LCK",
    "TGT": "TGT",
    "TARGET": "TGT",
    "TARGET_CONFIRMED": "TGT",
    "CONFIRMED": "TGT",
    "ALIGNED": "TGT",
}
_GPS_SAMPLE_KEYS = (
    "gps_sample",
    "GPS_RAW_INT",
    "gps_raw_int",
    "gps",
    "gps_quality",
    "vertical",
)
_RANGE_SAMPLE_KEYS = ("range", "range_status", "RANGE_STATUS")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _integer(value: object) -> int | None:
    """Read a strict integer wire field without accepting bool/float coercion."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _coordinate(container: Mapping[str, object], *names: str) -> float | None:
    """Read a coordinate in degrees, with an explicit MAVLink e7 fallback."""

    for name in names:
        value = _number(container.get(name))
        if value is not None:
            return value
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value or None


def _nested(record: Mapping[str, object], *names: str) -> Mapping[str, object]:
    """Find a named mapping through the AuditLog/details nesting envelope."""

    # Prefer a raw/sample mapping over a derived quality summary even when a
    # producer duplicated both at the top level and below target_geolocation.
    # Matching is case-insensitive so parsed MAVLink records using
    # ``gps_raw_int`` are treated the same as the canonical uppercase name.
    priority = {name.casefold(): index for index, name in enumerate(names)}
    matches: list[tuple[int, int, int, Mapping[str, object]]] = []

    def find(current: Mapping[str, object], seen: set[int], depth: int) -> None:
        current_id = id(current)
        if current_id in seen:
            return
        seen.add(current_id)
        for key, value in current.items():
            if isinstance(value, Mapping):
                rank = priority.get(str(key).casefold())
                if rank is not None:
                    # Empty placeholders should lose to a populated mapping
                    # at the same semantic priority, while preserving the
                    # caller-provided priority across different field names.
                    matches.append((rank, 0 if value else 1, depth, value))
                find(value, seen, depth + 1)

    find(record, set(), 0)
    if not matches:
        return record
    matches.sort(key=lambda item: item[:3])
    return matches[0][3]


def _normalize_record(record: Mapping[str, object]) -> dict[str, object]:
    """Flatten the repository audit envelope while retaining raw metadata.

    ``AuditLog`` emits ``event_type`` and nests the payload below ``details``;
    field capture tools generally emit the same values at the top level.  The
    validator consumes both forms so operators can point it at a live audit
    JSONL file without a pre-conversion step.  Top-level values win over
    ``details`` when a key appears in both places (the envelope is authoritative
    for timestamps and event identity).
    """

    details = record.get("details")
    normalized: dict[str, object] = {}
    if isinstance(details, Mapping):
        normalized.update(details)
    normalized.update({key: value for key, value in record.items() if key != "details"})
    normalized["_details"] = details if isinstance(details, Mapping) else {}
    return normalized


def _event_text(record: Mapping[str, object]) -> str:
    """Return a stable uppercase event identity for flat and audit records."""

    values = [record.get("event"), record.get("event_type")]
    details = record.get("_details")
    if isinstance(details, Mapping):
        values.extend(details.get(key) for key in ("event", "action", "command", "reason"))
    return " ".join(value for value in (_text(item) for item in values) if value)


def _is_cancel_event(record: Mapping[str, object]) -> bool:
    event = _event_text(record)
    if "CANCEL" in event or "UNTRACK" in event or "DESELECT" in event:
        return True
    for key in ("cancelled", "canceled", "cancel", "cancel_requested", "selection_cancelled"):
        value = record.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().casefold() in {"true", "yes", "1"}:
            return True
    return False


def _extract_state(record: Mapping[str, object]) -> str | None:
    # A cancellation event often contains the state name (for example
    # ``cancel_trk``).  It is a control event, not a new tracking state.
    if _is_cancel_event(record):
        return None
    for key in ("state", "target_state", "tracking_state", "mode", "phase"):
        state = _text(record.get(key))
        if state in _STATE_ALIASES:
            return _STATE_ALIASES[state]
    # Event names are deliberately matched on explicit state tokens only.  A
    # generic ``tracking.unified_target_pool_changed`` event is telemetry, not
    # proof that the operator entered TRK.
    raw_event = _text(record.get("event"))
    event_sources = [raw_event] if raw_event else []
    raw_event_type = _text(record.get("event_type"))
    if raw_event_type and any(
        marker in raw_event_type
        for marker in (
            ".STATE",
            "_STATE",
            "STATE_",
            "TRANSITION",
            "TARGET_CONFIRMED",
            ".DETECTED",
            ".LOCKED",
            ".LCK",
            ".TRK",
            ".TGT",
        )
    ):
        event_sources.append(raw_event_type)
    for source in event_sources:
        event_tokens = source.replace(".", " ").replace("/", " ").replace("-", " ").split()
        for token in event_tokens:
            if token in _STATE_ALIASES:
                return _STATE_ALIASES[token]
    return None


def _age(record: Mapping[str, object], container: Mapping[str, object], *keys: str) -> float | None:
    for key in keys:
        value = _number(container.get(key))
        if value is None:
            value = _number(record.get(key))
        if value is not None:
            return value
    return None


def _gps_quality(
    record: Mapping[str, object],
    *,
    now_s: float | None,
    max_age_s: float,
    min_satellites: int,
    max_hacc_m: float,
    max_vacc_m: float,
) -> tuple[bool, str]:
    gps = _nested(record, *_GPS_SAMPLE_KEYS)
    position_healthy = gps.get("position_healthy", record.get("position_healthy"))
    if position_healthy is not True:
        return False, "position_health_unavailable"
    latitude = _coordinate(gps, "latitude_deg", "lat_deg")
    longitude = _coordinate(gps, "longitude_deg", "lon_deg")
    if latitude is None:
        latitude = _coordinate(record, "latitude_deg", "lat_deg")
    if longitude is None:
        longitude = _coordinate(record, "longitude_deg", "lon_deg")
    if latitude is None:
        latitude_e7 = _number(gps.get("lat_e7", gps.get("lat")))
        if latitude_e7 is None:
            latitude_e7 = _number(record.get("lat_e7", record.get("lat")))
        if latitude_e7 is not None:
            latitude = latitude_e7 / 10_000_000.0 if abs(latitude_e7) > 90.0 else latitude_e7
    if longitude is None:
        longitude_e7 = _number(gps.get("lon_e7", gps.get("lon")))
        if longitude_e7 is None:
            longitude_e7 = _number(record.get("lon_e7", record.get("lon")))
        if longitude_e7 is not None:
            longitude = longitude_e7 / 10_000_000.0 if abs(longitude_e7) > 180.0 else longitude_e7
    if (
        latitude is None
        or longitude is None
        or not -90.0 < latitude < 90.0
        or not -180.0 <= longitude <= 180.0
    ):
        return False, "coordinates_invalid"
    fix = _integer(gps.get("fix_type", gps.get("gps_fix_type", gps.get("fix"))))
    sats = _integer(
        gps.get("satellites_visible", gps.get("satellites", gps.get("sat_count")))
    )
    hacc = _number(gps.get("h_acc_m", gps.get("horizontal_accuracy_m", gps.get("h_acc"))))
    vacc = _number(gps.get("v_acc_m", gps.get("vertical_accuracy_m", gps.get("v_acc"))))
    # MAVLink ``GPS_RAW_INT`` reports eph/epv in centimetres.  Keep the
    # canonical field in metres and reject the 65535 unknown sentinel through
    # the normal upper-bound gate.
    if hacc is None:
        eph_cm = _number(gps.get("eph_cm", gps.get("eph")))
        if eph_cm is not None:
            hacc = eph_cm / 100.0
    if vacc is None:
        epv_cm = _number(gps.get("epv_cm", gps.get("epv")))
        if epv_cm is not None:
            vacc = epv_cm / 100.0
    age = _age(
        record,
        gps,
        "age_s",
        "gps_age_s",
        "gps_raw_age_s",
        "gps_raw_message_age_s",
        "observed_age_s",
    )
    observed = _number(gps.get("observed_at_s", record.get("gps_observed_at_s")))
    if age is None and now_s is not None and observed is not None:
        age = now_s - observed
    if (
        fix is None
        or fix < MAVLINK_QUALIFIED_FIX_TYPE_MIN
        or fix > MAVLINK_GPS_FIX_TYPE_MAX
    ):
        return False, "fix_type<3"
    if (
        sats is None
        or sats < max(MAVLINK_QUALIFIED_SATELLITES_MIN, min_satellites)
        or sats > MAVLINK_SATELLITES_MAX
    ):
        return False, "satellites_invalid"
    if hacc is None or hacc < 0.0 or hacc > max_hacc_m:
        return False, "h_acc_invalid"
    if vacc is None or vacc < 0.0 or vacc > max_vacc_m:
        return False, "v_acc_invalid"
    if age is None or age < 0.0 or age > max_age_s:
        return False, "gps_age_invalid"
    return True, "qualified"


def _range_quality(record: Mapping[str, object], max_age_s: float) -> tuple[bool, str]:
    range_data = _nested(record, *_RANGE_SAMPLE_KEYS)
    validity = _text(range_data.get("validity", range_data.get("status")))
    if validity not in {"VALID", "DEGRADED"}:
        return False, "range_unavailable"
    source_age = _age(record, range_data, "source_age_s", "source_frame_age_s")
    freshness = _age(record, range_data, "data_freshness_s", "freshness_s")
    if source_age is None or source_age < 0.0 or source_age > max_age_s:
        return False, "source_age_invalid"
    if freshness is None or freshness < 0.0 or freshness > max_age_s:
        return False, "data_freshness_invalid"
    distance = _number(
        range_data.get(
            "distance_m",
            range_data.get("range_m", range_data.get("slant_range_m")),
        )
    )
    if distance is None or distance <= 0.0:
        return False, "distance_invalid"
    return True, "qualified"


def _canonical_upper_threshold(value: object, canonical: float) -> float:
    """Cap a diagnostic upper bound at the outdoor canonical policy.

    Field reports are often revalidated on a different workstation.  A
    caller may tighten a limit for diagnostics, but a relaxed or malformed
    value must never widen the acceptance surface.  Returning ``-1`` for an
    invalid bound makes every non-negative sample fail the corresponding
    quality check while keeping this validator's report-oriented API intact.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return -1.0
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        return -1.0
    return min(numeric, canonical)


def _canonical_min_satellites(value: object) -> int:
    """Keep the known-satellite floor at the canonical outdoor minimum."""

    if isinstance(value, bool) or not isinstance(value, int):
        return MAVLINK_SATELLITES_MAX + 1
    return max(MAVLINK_QUALIFIED_SATELLITES_MIN, value)


_TARGET_IDENTITY_KEYS = frozenset(
    {
        "target_id",
        "source_target_id",
        "primary_target_id",
        "selected_target_id",
        "range_target_id",
    }
)


def _direct_target_ids(record: Mapping[str, object]) -> set[str]:
    """Return identity fields owned directly by one mapping."""

    return {
        item.strip()
        for key, item in record.items()
        if str(key).casefold() in _TARGET_IDENTITY_KEYS
        and isinstance(item, str)
        and item.strip()
    }


def _target_ids(record: Mapping[str, object]) -> tuple[str, ...]:
    """Return target identifiers carried by one flat or AuditLog record.

    The field validator must not pair a GPS sample for one target with a range
    sample for another.  AuditLog producers place ``target_id`` at the event
    root, while a few capture adapters retain ``source_target_id`` or
    ``primary_target_id`` below a nested evidence object.  Collect only these
    identity-bearing keys and ignore arbitrary text/IDs in diagnostics.
    """

    found: set[str] = set()

    def visit(value: object, *, depth: int = 0) -> None:
        if depth > 8 or not isinstance(value, Mapping):
            return
        for key, item in value.items():
            if str(key).casefold() in _TARGET_IDENTITY_KEYS and isinstance(item, str):
                normalized = item.strip()
                if normalized:
                    found.add(normalized)
            if isinstance(item, Mapping):
                visit(item, depth=depth + 1)

    visit(record)
    return tuple(sorted(found))


def _target_ids_for_source(
    record: Mapping[str, object],
    source: Mapping[str, object],
    *source_names: str,
) -> tuple[tuple[str, ...], bool]:
    """Resolve IDs for one sensor sample and flag contradictory context.

    A field record often carries one event-level ``target_id`` while the GPS
    or range sample is nested below it.  That root ID is a valid fallback when
    the sample itself has no identity.  When both levels provide IDs, they
    must agree exactly; otherwise a GPS sample can be silently paired with a
    range sample belonging to a different target.
    """

    root_ids = _direct_target_ids(record)
    source_ids = set() if source is record else set(_target_ids(source))
    inherited_ids = set(root_ids)
    if source is not record and source_names:
        priority = {name.casefold(): index for index, name in enumerate(source_names)}
        matches: list[tuple[int, int, int, set[str]]] = []

        def find(
            current: Mapping[str, object],
            inherited: set[str],
            seen: set[int],
            depth: int,
        ) -> None:
            current_id = id(current)
            if current_id in seen:
                return
            path_seen = seen | {current_id}
            current_ids = inherited | _direct_target_ids(current)
            for key, value in current.items():
                if not isinstance(value, Mapping):
                    continue
                rank = priority.get(str(key).casefold())
                if value is source and rank is not None:
                    matches.append((rank, 0 if value else 1, depth + 1, current_ids))
                find(value, current_ids, path_seen, depth + 1)

        find(record, set(), set(), 0)
        if matches:
            matches.sort(key=lambda item: item[:3])
            inherited_ids = matches[0][3]
    context_ids = inherited_ids | source_ids
    contradictory = len(context_ids) > 1
    effective_ids = source_ids or inherited_ids or root_ids
    return tuple(sorted(effective_ids)), contradictory


def validate_field_session(
    records: Sequence[Mapping[str, object]],
    *,
    max_gps_age_s: float = GNSS_CANONICAL_MAX_GPS_AGE_S,
    max_range_age_s: float = GNSS_CANONICAL_MAX_GPS_AGE_S,
    min_satellites: int = MAVLINK_QUALIFIED_SATELLITES_MIN,
    max_hacc_m: float = GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M,
    max_vacc_m: float = GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M,
    require_target_state_evidence: bool = False,
) -> dict[str, Any]:
    """Recompute the field DET/TRK/LCK/TGT evidence contract.

    Upper GPS/RANGE limits are diagnostic knobs only: values looser than the
    canonical outdoor policy are capped at hAcc ``6 m``, vAcc ``10 m`` and
    source age ``1.5 s``.  This keeps a report verifier from promoting stale
    or low-quality telemetry when a producer supplied relaxed options.

    ``require_target_state_evidence`` enables the live-field variant of the
    gate: qualified sensor records must be observed while the active state
    context is ``TGT``.  Sensor-only records inherit the latest state and a
    cancel event clears it, preventing pre-cancel telemetry from being reused
    for a later target location.
    """
    states: list[str] = []
    gps_results: list[dict[str, object]] = []
    range_results: list[dict[str, object]] = []
    malformed = 0
    cancel_seen = False
    reacquired_after_cancel = False
    previous_state: str | None = None
    previous_timestamp_s: float | None = None
    timestamp_non_decreasing = True
    timestamp_errors: list[dict[str, object]] = []
    qualified_gps: list[tuple[int, tuple[str, ...], bool, str | None]] = []
    qualified_range: list[tuple[int, tuple[str, ...], bool, str | None]] = []
    target_binding_errors: list[dict[str, object]] = []
    active_state: str | None = None
    effective_max_gps_age_s = _canonical_upper_threshold(
        max_gps_age_s, GNSS_CANONICAL_MAX_GPS_AGE_S
    )
    effective_max_range_age_s = _canonical_upper_threshold(
        max_range_age_s, GNSS_CANONICAL_MAX_GPS_AGE_S
    )
    effective_max_hacc_m = _canonical_upper_threshold(
        max_hacc_m, GNSS_CANONICAL_MAX_HORIZONTAL_ACCURACY_M
    )
    effective_max_vacc_m = _canonical_upper_threshold(
        max_vacc_m, GNSS_CANONICAL_MAX_VERTICAL_ACCURACY_M
    )
    effective_min_satellites = _canonical_min_satellites(min_satellites)
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            malformed += 1
            continue
        normalized = _normalize_record(record)
        if "timestamp_s" in normalized:
            timestamp_key = "timestamp_s"
        elif "observed_at_s" in normalized:
            timestamp_key = "observed_at_s"
        else:
            timestamp_key = None
        timestamp = _number(normalized.get(timestamp_key)) if timestamp_key else None
        if timestamp is not None and timestamp >= 0.0:
            if previous_timestamp_s is not None and timestamp < previous_timestamp_s:
                timestamp_non_decreasing = False
                timestamp_errors.append(
                    {
                        "index": index,
                        "previous_timestamp_s": previous_timestamp_s,
                        "timestamp_s": timestamp,
                        "reason": "timestamp_decreased",
                    }
                )
            previous_timestamp_s = timestamp
        else:
            timestamp_non_decreasing = False
            reason = "timestamp_missing" if timestamp_key is None else "timestamp_invalid"
            timestamp_errors.append(
                {"index": index, "reason": reason}
            )
        cancel_event = _is_cancel_event(normalized)
        if cancel_event:
            cancel_seen = True
        state = _extract_state(normalized)
        if cancel_seen and state in {"DET", "TRK"}:
            # Re-acquisition is an event, not a state transition.  A target
            # can be cancelled while already in TRK and then reacquired into
            # TRK again, so do not require an intermediate DET record here.
            reacquired_after_cancel = True
        if state and state != previous_state:
            states.append(state)
            previous_state = state
        # Sensor-only records inherit the latest operator state.  A cancel
        # event deliberately clears that context, so a post-cancel stale
        # sample cannot be counted as evidence for the previous TGT state.
        if cancel_event:
            active_state = None
        elif state is not None:
            active_state = state
        state_context = active_state
        now_s = timestamp if timestamp is not None and timestamp >= 0.0 else None
        gps = _nested(normalized, *_GPS_SAMPLE_KEYS)
        if any(
            key in gps
            for key in (
                "fix_type",
                "gps_fix_type",
                "satellites_visible",
                "h_acc_m",
                "v_acc_m",
                "eph_cm",
                "epv_cm",
                "latitude_deg",
                "longitude_deg",
                "lat_deg",
                "lon_deg",
                "lat",
                "lon",
            )
        ):
            qualified, reason = _gps_quality(
                normalized,
                now_s=now_s,
                max_age_s=effective_max_gps_age_s,
                min_satellites=effective_min_satellites,
                max_hacc_m=effective_max_hacc_m,
                max_vacc_m=effective_max_vacc_m,
            )
            target_ids, contradictory_ids = _target_ids_for_source(
                normalized, gps, *_GPS_SAMPLE_KEYS
            )
            gps_results.append(
                {
                    "index": index,
                    "qualified": qualified,
                    "reason": reason,
                    "target_ids": list(target_ids),
                    "target_id_context_valid": not contradictory_ids,
                    "state_context": state_context,
                }
            )
            if qualified:
                qualified_gps.append((index, target_ids, contradictory_ids, state_context))
                if contradictory_ids:
                    target_binding_errors.append(
                        {
                            "index": index,
                            "source": "gps",
                            "reason": "target_id_context_conflict",
                            "target_ids": list(target_ids),
                        }
                    )
        range_data = _nested(normalized, *_RANGE_SAMPLE_KEYS)
        if any(
            key in range_data
            for key in ("validity", "status", "distance_m", "range_m", "slant_range_m")
        ):
            qualified, reason = _range_quality(normalized, effective_max_range_age_s)
            target_ids, contradictory_ids = _target_ids_for_source(
                normalized, range_data, *_RANGE_SAMPLE_KEYS
            )
            range_results.append(
                {
                    "index": index,
                    "qualified": qualified,
                    "reason": reason,
                    "target_ids": list(target_ids),
                    "target_id_context_valid": not contradictory_ids,
                    "state_context": state_context,
                }
            )
            if qualified:
                qualified_range.append((index, target_ids, contradictory_ids, state_context))
                if contradictory_ids:
                    target_binding_errors.append(
                        {
                            "index": index,
                            "source": "range",
                            "reason": "target_id_context_conflict",
                            "target_ids": list(target_ids),
                        }
                    )
    cursor = 0
    for state in states:
        if cursor < len(_REQUIRED_SEQUENCE) and state == _REQUIRED_SEQUENCE[cursor]:
            cursor += 1
    sequence_valid = cursor == len(_REQUIRED_SEQUENCE)
    gps_target_ids = {
        target_id
        for _index, target_ids, _contradictory, _state_context in qualified_gps
        for target_id in target_ids
    }
    range_target_ids = {
        target_id
        for _index, target_ids, _contradictory, _state_context in qualified_range
        for target_id in target_ids
    }
    matched_target_ids = sorted(gps_target_ids.intersection(range_target_ids))
    # A non-empty intersection is not sufficient evidence of identity.  For
    # example, a malformed stream can carry GPS samples for ``target-a`` and
    # ``target-b`` while RANGE samples only carry ``target-a``.  The previous
    # intersection-only gate accepted that stream when those samples arrived
    # in separate records, silently combining measurements from two objects.
    # A field-session gate represents one selected target, so every qualified
    # source in a validation window must expose the same single target ID.
    cross_record_mismatches = []
    if gps_target_ids and range_target_ids and gps_target_ids != range_target_ids:
        cross_record_mismatches.append(
            {
                "gps_only": sorted(gps_target_ids - range_target_ids),
                "range_only": sorted(range_target_ids - gps_target_ids),
            }
        )
    multiple_target_ids = sorted(gps_target_ids | range_target_ids) if len(
        gps_target_ids | range_target_ids
    ) > 1 else []
    same_record_pairs = sorted(
        {
            gps_index
            for gps_index, gps_ids, _gps_contradictory, _gps_state_context in qualified_gps
            for range_index, range_ids, _range_contradictory, _range_state_context
            in qualified_range
            if gps_index == range_index
            and (not gps_ids or not range_ids or set(gps_ids).intersection(range_ids))
        }
    )
    same_record_without_id = sorted(
        {
            gps_index
            for gps_index, gps_ids, _gps_contradictory, _gps_state_context in qualified_gps
            for range_index, range_ids, _range_contradictory, _range_state_context
            in qualified_range
            if gps_index == range_index and not gps_ids and not range_ids
        }
    )
    mismatched_same_record = sorted(
        {
            gps_index
            for gps_index, gps_ids, _gps_contradictory, _gps_state_context in qualified_gps
            for range_index, range_ids, _range_contradictory, _range_state_context
            in qualified_range
            if gps_index == range_index
            and gps_ids
            and range_ids
            and not set(gps_ids).intersection(range_ids)
        }
    )
    if (
        target_binding_errors
        or mismatched_same_record
        or cross_record_mismatches
        or multiple_target_ids
    ):
        target_binding_valid = False
    elif matched_target_ids:
        # ``matched_target_ids`` is kept for provenance, but the gate requires
        # exactly one shared identity rather than any overlap between sets.
        target_binding_valid = (
            gps_target_ids == range_target_ids and len(matched_target_ids) == 1
        )
    elif gps_target_ids and range_target_ids:
        # Two known, disjoint identity sets cannot be rescued by an unrelated
        # same-frame sample elsewhere in the session.
        target_binding_valid = False
    else:
        # With no identity on one or both sources, only an explicitly shared
        # record is safe enough to establish the association.
        target_binding_valid = bool(same_record_pairs)
    target_state_gps = [
        item
        for item in gps_results
        if item.get("qualified") is True
        and item.get("state_context") == "TGT"
        and item.get("target_id_context_valid") is True
    ]
    target_state_range = [
        item
        for item in range_results
        if item.get("qualified") is True
        and item.get("state_context") == "TGT"
        and item.get("target_id_context_valid") is True
    ]
    target_state_reasons: list[str] = []
    if not target_state_gps:
        target_state_reasons.append("target_state_gps_missing")
    if not target_state_range:
        target_state_reasons.append("target_state_range_missing")
    target_state_evidence_valid = not target_state_reasons
    base_gate_passed = (
        malformed == 0
        and sequence_valid
        and timestamp_non_decreasing
        and any(bool(item["qualified"]) for item in gps_results)
        and any(bool(item["qualified"]) for item in range_results)
        and target_binding_valid
        and (not cancel_seen or reacquired_after_cancel)
    )
    return {
        "schema_version": 1,
        "record_count": len(records),
        "malformed_records": malformed,
        "state_path": states,
        "required_state_sequence": list(_REQUIRED_SEQUENCE),
        "state_sequence_valid": sequence_valid,
        "timestamps": {
            "non_decreasing": timestamp_non_decreasing,
            "errors": timestamp_errors,
        },
        "cancel_seen": cancel_seen,
        "reacquired_after_cancel": reacquired_after_cancel,
        "gps": {
            "samples": len(gps_results),
            "qualified": sum(bool(item["qualified"]) for item in gps_results),
            "results": gps_results,
        },
        "range": {
            "samples": len(range_results),
            "qualified": sum(bool(item["qualified"]) for item in range_results),
            "results": range_results,
        },
        "target_binding": {
            "valid": target_binding_valid,
            "gps_target_ids": sorted(gps_target_ids),
            "range_target_ids": sorted(range_target_ids),
            "matched_target_ids": matched_target_ids,
            "same_record_pairs": same_record_pairs,
            # Keep the first field-session report spelling for downstream
            # evidence readers while exposing the more precise pair name.
            "same_record_without_id": same_record_without_id,
            "mismatched_same_record": mismatched_same_record,
            "cross_record_mismatches": cross_record_mismatches,
            "multiple_target_ids": multiple_target_ids,
            "errors": target_binding_errors,
        },
        "target_state_evidence": {
            "required": bool(require_target_state_evidence),
            "state": "TGT",
            "gps_qualified_records": [int(item["index"]) for item in target_state_gps],
            "range_qualified_records": [int(item["index"]) for item in target_state_range],
            "valid": target_state_evidence_valid,
            "reasons": target_state_reasons,
        },
        "gate_passed": base_gate_passed
        and (not require_target_state_evidence or target_state_evidence_valid),
    }


def load_jsonl(path: Path) -> list[Mapping[str, object]]:
    records: list[Mapping[str, object]] = []
    # ``utf-8-sig`` accepts the BOM emitted by several Windows log writers;
    # strict_json_loads then rejects duplicate members and NaN/Infinity so a
    # last-key-wins or non-finite record cannot enter a field gate.
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = strict_json_loads(line, name=f"line {line_number}")
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"line {line_number} is not strict JSON: {exc}") from exc
        if not isinstance(value, Mapping):
            raise ValueError(f"line {line_number} must be a JSON object")
        records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a field DET/TRK/LCK/TGT session JSONL.")
    parser.add_argument("session", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-gps-age-s", type=float, default=1.5)
    parser.add_argument("--max-range-age-s", type=float, default=1.5)
    parser.add_argument(
        "--link-preflight",
        type=Path,
        help=(
            "bind validation to a saved field-link preflight whose SHA-256 and "
            "hardware_link_ready=true are rechecked"
        ),
    )
    parser.add_argument(
        "--require-link-preflight",
        action="store_true",
        help="require --link-preflight for a passing field-session gate",
    )
    parser.add_argument(
        "--require-target-state-evidence",
        action="store_true",
        help=(
            "require at least one fresh, qualified GPS and RANGE_STATUS sample "
            "while the active state context is TGT"
        ),
    )
    args = parser.parse_args()
    report = validate_field_session(
        load_jsonl(args.session),
        max_gps_age_s=args.max_gps_age_s,
        max_range_age_s=args.max_range_age_s,
        require_target_state_evidence=args.require_target_state_evidence,
    )
    if args.require_link_preflight or args.link_preflight is not None:
        if args.link_preflight is None:
            link_result: dict[str, Any] = {
                "report_path": None,
                "report_sha256": None,
                "report_bytes": 0,
                "hardware_link_ready": False,
                "summary": None,
                "checks": {},
                "reasons": ["link_preflight path is required"],
                "gate_passed": False,
            }
        else:
            link_result = verify_field_link_report(
                args.link_preflight,
                require_ready=True,
            )
        report["link_preflight"] = link_result
        report["gate_passed"] = bool(report["gate_passed"]) and bool(
            link_result.get("gate_passed")
        )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
