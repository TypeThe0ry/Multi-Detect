"""Compact X1 observation / event contract; no action selection."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping

STATE_SCHEMA = "x1.contingency.state.v1"
EVENT_KINDS = frozenset({
    "wind_step", "battery_drop", "link_loss", "link_restored", "target_lost",
    "target_reacquired", "obstacle_conflict", "terrain_conflict", "gps_degraded",
    "health_degraded", "transition_problem",
})
MAX_EVENTS = 8
EVENT_TTL_MS = 15_000
TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,48}\Z")
# Each group retains its own provenance and freshness (not the most recent packet time).
GROUPS = {
    "battery": (5000, ("pct", "charge_state")),
    "wind": (5000, ("north_mps", "east_mps", "down_mps")),
    "link": (3000, ("gcs", "heartbeat_age_ms")),
    "fc": (3000, ("armed", "system_status")),
    "gps": (3000, ("fix_type", "satellites")),
    "ekf": (5000, ("flags", "horizontal_error_m")),
    "health": (3000, ("system_status",)),
    "agl": (2000, ("m",)),
    "terrain": (2000, ("clearance_m", "conflict")),
    "vtol": (1500, ("state", "landed_state")),
    "air": (2000, ("airspeed_mps",)),
    "target": (2000, ("visible", "track_state", "track_ref", "identity_confidence")),
    "navigation": (2000, ("home_distance_m", "remaining_time_s", "remaining_energy_wh")),
    "obstacle": (2000, ("conflict", "clearance_m")),
    "leg": (5000, ("seq", "total", "step_id", "progress")),
}
SOURCES = frozenset({"mavlink", "tracker", "terrain", "range", "obstacle", "executor", "link_monitor", "fault"})
VTOL_STATES = {1: "front_transition", 2: "back_transition", 3: "mc", 4: "fw"}
_FIELD_ENUMS = {
    ("link", "gcs"): {"up", "down"}, ("vtol", "state"): set(VTOL_STATES.values()),
    ("target", "track_state"): {"tentative", "tracking", "lost"},
}
_BOOL_FIELDS = {("fc", "armed"), ("target", "visible"), ("obstacle", "conflict"), ("terrain", "conflict")}
_TOKEN_FIELDS = {("leg", "step_id"), ("target", "track_ref")}


def compact_json(document: Mapping) -> str:
    return json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)


def require_token(value: object) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise ValueError("invalid opaque identifier")
    return value


def sample_value(group: str, field: str, value: object) -> object:
    """Reject invalid host sidecars and normalize unsupported MAVLink values to null."""
    key = group, field
    if value is None:
        return None
    if key in _FIELD_ENUMS:
        if isinstance(value, str) and value in _FIELD_ENUMS[key]:
            return value
    elif key in _BOOL_FIELDS:
        if type(value) is bool:
            return value
    elif key in _TOKEN_FIELDS:
        return require_token(value)
    elif type(value) in (int, float) and abs(value) <= 2**32 - 1 and math.isfinite(value):
        if field in {"seq", "total", "fix_type", "charge_state", "satellites", "landed_state", "system_status",
                     "flags", "enabled_mask", "healthy_mask", "heartbeat_age_ms"}:
            if type(value) is int and 0 <= value <= 2**32 - 1:
                return value
        elif field.endswith("_mps") and group == "wind":
            return round(value, 2)
        elif field == "clearance_m":
            return round(value, 2)  # A signed margin can be negative.
        elif value >= 0 and (field != "pct" or value <= 100):
            if field in {"progress", "identity_confidence"}:
                if value <= 1:
                    return round(value, 3)
            else:
                return round(value, 2)
    raise ValueError(f"invalid observation: {group}.{field}")


def _valid_or_null(group: str, field: str, value: object) -> object:
    try:
        return sample_value(group, field, value)
    except ValueError:
        return None


class X1StateBuilder:
    """One producer per run. Timestamps use a common nondecreasing host clock."""

    def __init__(self, *, vehicle_system: int = 1, vehicle_component: int = 1, battery_id: int = 0):
        self.vehicle = vehicle_system, vehicle_component
        self.battery_id = battery_id
        self._samples: dict[str, tuple[int, str, dict]] = {}
        self._leg_progress: tuple[int, dict] | None = None
        self._events: list[dict] = []
        self._event_id = 0
        self._last_ms = 0

    def _time(self, t_ms: int) -> None:
        if type(t_ms) is not int or t_ms < self._last_ms:
            raise ValueError("timestamps must be nonnegative integer ms and nondecreasing")
        self._last_ms = t_ms

    def observe(self, t_ms: int, group: str, values: Mapping, *, source: str) -> None:
        if group not in GROUPS or source not in SOURCES or not values.keys() <= set(GROUPS[group][1]):
            raise ValueError("unknown observation group, field or source")
        if group == "leg" and source != "mavlink" and not values.keys() <= {"step_id", "progress"}:
            raise ValueError("host leg observations must carry step_id/progress only")
        allowed = {"step_id", "progress"} if group == "leg" and source != "mavlink" else GROUPS[group][1]
        normalized = {field: sample_value(group, field, values.get(field)) for field in allowed}
        self._time(t_ms)
        if group == "leg" and source != "mavlink":
            self._leg_progress = t_ms, normalized
        else:
            self._samples[group] = t_ms, source, normalized

    def event(self, t_ms: int, kind: str, *, source: str, confidence: float | None = None,
              severity: str | None = None) -> None:
        if kind not in EVENT_KINDS or source not in SOURCES:
            raise ValueError("unknown event kind or source")
        confidence = sample_value("target", "identity_confidence", confidence)
        if severity not in (None, "info", "warning", "critical"):
            raise ValueError("unknown event severity")
        self._time(t_ms)
        self._event_id += 1
        self._events = [*self._events[-(MAX_EVENTS - 1):],
                        {"id": self._event_id, "type": kind, "t_ms": t_ms, "source": source,
                         "confidence": confidence, "severity": severity}]

    def ingest_mavlink(self, t_ms: int, row: Mapping) -> None:
        # The PX4 heartbeat is not a GCS heartbeat. Do not infer GCS link from any
        # system/component, packet loss, or a recorder that merely stayed alive.
        peer = row.get("_source_system"), row.get("_source_component")
        if any(type(part) is not int for part in peer) or peer != self.vehicle:
            return
        kind = row.get("message_type", row.get("mavpackettype"))
        r = row.get
        if kind == "BATTERY_STATUS" and type(r("id")) is int and r("id") == self.battery_id:
            remaining = r("battery_remaining")
            charge = r("charge_state")
            group, values = "battery", {"pct": remaining if type(remaining) is int and 0 <= remaining <= 100 else None,
                                         "charge_state": charge if type(charge) is int and charge in range(0, 7) else None}
        elif kind == "SYS_STATUS":
            group, values = "health", {"system_status": r("onboard_control_sensors_health")}
        elif kind == "WIND_COV":
            group, values = "wind", dict(zip(GROUPS["wind"][1], (r("wind_x"), r("wind_y"), r("wind_z")), strict=True))
        elif kind == "HEARTBEAT":
            mode = r("base_mode")
            group, values = "fc", {"armed": bool(mode & 128) if type(mode) is int and 0 <= mode <= 255 else None,
                                   "system_status": r("system_status")}
        elif kind == "GPS_RAW_INT":
            group, values = "gps", {"fix_type": r("fix_type"), "satellites": r("satellites_visible") if r("satellites_visible") != 255 else None}
        elif kind == "ESTIMATOR_STATUS":
            group, values = "ekf", {"flags": r("flags"), "horizontal_error_m": r("pos_horiz_accuracy")}
        elif kind == "EXTENDED_SYS_STATE":
            group, values = "vtol", {"state": VTOL_STATES.get(r("vtol_state")) if type(r("vtol_state")) is int else None,
                                     "landed_state": r("landed_state") if r("landed_state") in (1, 2, 3, 4) else None}
        elif kind == "VFR_HUD":
            group, values = "air", {"airspeed_mps": r("airspeed")}
        elif kind == "TERRAIN_REPORT":
            group, values = "agl", {"m": r("current_height")}
        elif kind == "MISSION_CURRENT":
            group, values = "leg", {field: r(field) if r(field) != 65535 else None for field in ("seq", "total")}
        else:
            return
        self.observe(t_ms, group, {k: _valid_or_null(group, k, v) for k, v in values.items()}, source="mavlink")

    def snapshot(self, t_ms: int, *, mission_id: str, revision: int) -> dict:
        require_token(mission_id)
        if type(revision) is not int or not 1 <= revision <= 2**31 - 1:
            raise ValueError("invalid mission revision")
        self._time(t_ms)
        telemetry = {}
        for group, (ttl, _) in GROUPS.items():
            timestamp, source, values = self._samples.get(group, (None, None, {}))
            age = t_ms - timestamp if timestamp is not None else None
            telemetry[group] = ({"age_ms": age, "source": source, **values}
                                if age is not None and age <= ttl else None)
        # The host's progress timestamp must neither refresh nor replace the MAVLink item index.
        leg = self._leg_progress
        host_leg = {"age_ms": t_ms - leg[0], **leg[1]} if leg and t_ms - leg[0] <= GROUPS["leg"][0] else None
        return {"schema": STATE_SCHEMA, "vehicle": "delta_tailsitter_gimbal_cam", "mission_id": mission_id,
                "revision": revision, "t_ms": t_ms, "telemetry": telemetry, "host_leg": host_leg,
                "events": [event.copy() for event in self._events if t_ms - event["t_ms"] <= EVENT_TTL_MS],
                "latest_event_id": self._event_id}
