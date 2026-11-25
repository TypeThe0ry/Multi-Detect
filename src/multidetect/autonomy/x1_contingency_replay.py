"""CPU replay and synthetic observation overlays. No simulator/vehicle transport."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from heapq import merge
from itertools import chain
from pathlib import Path

from .serialization import strict_json_loads
from .x1_contingency_state import GROUPS, SOURCES, X1StateBuilder, sample_value

# The injection timestamp is also the event timestamp; end is exclusive.
FAULT_GROUPS = {
    "wind_step": "wind", "battery_drop": "battery", "link_loss": "link", "target_lost": "target",
    "obstacle_conflict": "obstacle", "terrain_conflict": "terrain", "gps_degraded": "gps",
    "health_degraded": "ekf", "transition_problem": "vtol",
}
_REQUIRED_VALUES = {
    "wind_step": {"north_mps", "east_mps", "down_mps"}, "battery_drop": {"pct"},
    "obstacle_conflict": {"clearance_m"}, "terrain_conflict": {"clearance_m"},
    "gps_degraded": {"fix_type"}, "health_degraded": {"flags"}, "transition_problem": {"state"},
}
_FIXED_VALUES = {"link_loss": {"gcs": "down"}, "target_lost": {"visible": False, "track_state": "lost"},
                 "obstacle_conflict": {"conflict": True}, "terrain_conflict": {"conflict": True}}


@dataclass(frozen=True, slots=True)
class Fault:
    at_ms: int
    end_ms: int
    kind: str
    group: str
    values: dict


def _nonnegative_int(n) -> bool:
    return type(n) is int and n >= 0


def load_faults(path: Path) -> tuple[Fault, ...]:
    doc = strict_json_loads(path.read_bytes(), name="X1 faults")
    if (not isinstance(doc, dict) or set(doc) != {"schema", "faults"} or doc["schema"] != "x1.contingency.faults.v1"
            or not isinstance(doc["faults"], list) or len(doc["faults"]) > 128):
        raise ValueError("invalid fault document")
    faults = []
    for item in doc["faults"]:
        if not isinstance(item, dict) or set(item) != {"at_ms", "duration_ms", "type", "values"}:
            raise ValueError("invalid fault entry")
        start, duration, kind, values = (item[k] for k in ("at_ms", "duration_ms", "type", "values"))
        required = _REQUIRED_VALUES.get(kind, set()) if isinstance(kind, str) else set()
        optional = {"ramp_ms"} if kind == "wind_step" else set()
        if (not _nonnegative_int(start) or not _nonnegative_int(duration) or not duration
                or not isinstance(kind, str) or kind not in FAULT_GROUPS or not isinstance(values, dict)
                or not required <= values.keys() <= (required | optional) or any(v is None for v in values.values())):
            raise ValueError("invalid fault parameters")
        group = FAULT_GROUPS[kind]
        validated = {k: sample_value(group, k, v) for k, v in values.items() if k in required}
        if "ramp_ms" in values:
            ramp = values["ramp_ms"]
            if type(ramp) not in (int, float) or not 0 <= ramp or ramp * 2 > duration:
                raise ValueError("wind_step ramp_ms must be finite and in [0, duration_ms / 2]")
            validated["ramp_ms"] = ramp
        if kind == "transition_problem" and validated["state"] not in {"front_transition", "back_transition"}:
            raise ValueError("fault must specify a transition phase")
        faults.append(Fault(start, start + duration, kind, group, {**validated, **_FIXED_VALUES.get(kind, {})}))
    ends = {}
    for f in sorted(faults, key=lambda f: (f.at_ms, f.kind)):
        if f.at_ms < ends.get(f.group, 0):
            raise ValueError("overlapping faults in the same group")
        ends[f.group] = f.end_ms
    return tuple(sorted(faults, key=lambda f: (f.at_ms, f.kind)))


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")


def _mav_rows(lines: Iterable[str]) -> Iterator[tuple[int, int, dict]]:
    origin = last = None
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        # Historical recorder rows can contain NaN. The state builder nulls
        # non-finite selected fields; the model wire decoder remains strict.
        row = json.loads(line)
        timestamp = row.get("receive_monotonic_ns") if isinstance(row, dict) else None
        if not _nonnegative_int(timestamp) or (last is not None and timestamp < last):
            raise ValueError(f"invalid MAVLink receive clock at line {number}")
        origin = timestamp if origin is None else origin
        last = timestamp
        yield (timestamp - origin) // 1_000_000, 0, row


def _sidecar_rows(lines: Iterable[str]) -> Iterator[tuple[int, int, dict]]:
    last = 0
    for line in lines:
        if not line.strip():
            continue
        row = strict_json_loads(line, name="X1 observation sidecar")
        if (not isinstance(row, dict) or not _nonnegative_int(row.get("t_ms")) or row["t_ms"] < last
                or not isinstance(row.get("source"), str) or row["source"] not in SOURCES - {"mavlink", "fault"}):
            raise ValueError("invalid sidecar row or clock")
        last = row["t_ms"]
        yield last, 1, row


def _apply_sidecar(builder: X1StateBuilder, t_ms, row):
    if set(row) == {"t_ms", "source", "group", "values"} and isinstance(row["group"], str) and isinstance(row["values"], dict):
        builder.observe(t_ms, row["group"], row["values"], source=row["source"])
    elif ("event" in row and isinstance(row["event"], str)
          and set(row) <= {"t_ms", "source", "event", "confidence", "severity"}):
        builder.event(t_ms, row["event"], source=row["source"],
                      confidence=row.get("confidence"), severity=row.get("severity"))
    else:
        raise ValueError("invalid observation sidecar fields")


def replay_states(mavlink: Path, *, mission_id: str, revision: int = 1, every_ms: int = 1000,
                  sidecar: Path | None = None, faults: tuple[Fault, ...] = (),
                  until_ms: int | None = None, vehicle_system: int = 1, vehicle_component: int = 1) -> Iterator[dict]:
    """Stream regular ticks and exact event/fault boundaries; do not read future observations."""
    if not _nonnegative_int(every_ms) or not every_ms or (until_ms is not None and not _nonnegative_int(until_ms)):
        raise ValueError("invalid replay interval")
    builder = X1StateBuilder(vehicle_system=vehicle_system, vehicle_component=vehicle_component)
    boundaries = {f.at_ms for f in faults} | {f.end_ms for f in faults}
    starts: dict[int, list[Fault]] = {}
    for f in faults:
        starts.setdefault(f.at_ms, []).append(f)
    with ExitStack() as stack:
        mav = iter(_mav_rows(stack.enter_context(_open(mavlink))))
        first = next(mav, None)
        if first is None:
            raise ValueError("empty MAVLink recording")
        merged = iter(merge(chain((first,), mav),
                            _sidecar_rows(stack.enter_context(_open(sidecar))) if sidecar else (),
                            ((t, 2, {}) for t in sorted(boundaries)), key=lambda row: row[:2]))
        row = next(merged, None)
        periodic = 0
        while row is not None:
            tick = min(periodic, row[0])
            if until_ms is not None and tick > until_ms:
                break
            event_tick = tick in boundaries
            while row is not None and row[0] == tick:
                ts, origin, data = row
                if origin == 0:
                    builder.ingest_mavlink(ts, data)
                elif origin == 1:
                    _apply_sidecar(builder, ts, data)
                    event_tick |= "event" in data
                row = next(merged, None)
            for fault in starts.get(tick, ()):
                builder.event(tick, fault.kind, source="fault")
            if tick == periodic or event_tick:
                snapshot = builder.snapshot(tick, mission_id=mission_id, revision=revision)
                for fault in faults:
                    if fault.at_ms <= tick < fault.end_ms:
                        baseline = snapshot["telemetry"][fault.group] or {}
                        # Preserve same-tick base fields only; don't refresh stale
                        # ancillary fields under the synthetic source/age.
                        values = {field: baseline.get(field) if baseline.get("age_ms") == 0 else None
                                  for field in GROUPS[fault.group][1]}
                        values.update((k, v) for k, v in fault.values.items() if k in values)
                        snapshot["telemetry"][fault.group] = {**values, "source": "fault", "age_ms": 0}
                yield snapshot
            if tick == periodic:
                periodic += every_ms
