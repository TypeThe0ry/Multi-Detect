from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from enum import Enum
from math import isfinite
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any

from .domain import AuditEvent


def _json_safe(value: Any, active_containers: set[int]) -> Any:
    """Create a detached value containing only strict JSON data types."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("audit details cannot contain non-finite numbers")
        return value
    if isinstance(value, Enum):
        return _json_safe(value.value, active_containers)

    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active_containers:
            raise ValueError("audit details cannot contain cyclic structures")
        active_containers.add(marker)
        try:
            snapshot: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError("audit detail keys must be strings")
                snapshot[key] = _json_safe(item, active_containers)
            return snapshot
        finally:
            active_containers.remove(marker)

    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active_containers:
            raise ValueError("audit details cannot contain cyclic structures")
        active_containers.add(marker)
        try:
            return [_json_safe(item, active_containers) for item in value]
        finally:
            active_containers.remove(marker)

    raise TypeError(f"audit details value is not JSON serializable: {type(value).__name__}")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class AuditLog:
    """Thread-safe, append-only in-memory audit event log."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._events: list[AuditEvent] = []
        self._next_sequence = 1

    def append(
        self,
        event_type: str,
        timestamp_s: float,
        details: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        """Append caller-supplied event data without inferring secret fields."""

        timestamp = float(timestamp_s)
        if not isfinite(timestamp):
            raise ValueError("audit event timestamp must be finite")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("audit event_type cannot be empty")

        raw_details: Mapping[str, Any] = {} if details is None else details
        if not isinstance(raw_details, Mapping):
            raise TypeError("audit details must be a mapping")
        safe_details = _json_safe(raw_details, set())
        frozen_details = _freeze(safe_details)

        with self._lock:
            event = AuditEvent(
                sequence=self._next_sequence,
                timestamp_s=timestamp,
                event_type=event_type,
                details=frozen_details,
            )
            self._events.append(event)
            self._next_sequence += 1
            return event

    def events(self) -> tuple[AuditEvent, ...]:
        """Return an immutable, internally detached snapshot of current events."""

        with self._lock:
            return tuple(self._events)

    def write_jsonl(self, path: str | Path) -> None:
        """Atomically replace *path* with a UTF-8 JSON Lines snapshot."""

        destination = Path(path)
        snapshot = self.events()
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(
                file_descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                for event in snapshot:
                    record = {
                        "sequence": event.sequence,
                        "timestamp_s": event.timestamp_s,
                        "event_type": event.event_type,
                        "details": _thaw(event.details),
                    }
                    handle.write(
                        json.dumps(
                            record,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


InMemoryAuditLog = AuditLog
