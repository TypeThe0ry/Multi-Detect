from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections import deque
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
    """Thread-safe audit log with optional streaming JSON Lines persistence."""

    def __init__(
        self,
        *,
        stream_path: str | Path | None = None,
        max_in_memory_events: int | None = None,
        fsync_every_events: int = 1,
        fsync_event_prefixes: tuple[str, ...] = (),
        stream_append: bool = False,
    ) -> None:
        if max_in_memory_events is not None and max_in_memory_events <= 0:
            raise ValueError("max_in_memory_events must be positive when supplied")
        if fsync_every_events <= 0:
            raise ValueError("fsync_every_events must be positive")
        if any(not isinstance(prefix, str) or not prefix for prefix in fsync_event_prefixes):
            raise ValueError("fsync_event_prefixes must contain non-empty strings")
        self._lock = RLock()
        self._events: deque[AuditEvent] = deque(maxlen=max_in_memory_events)
        self._next_sequence = 1
        self._event_count = 0
        self._fsync_every_events = fsync_every_events
        self._fsync_event_prefixes = fsync_event_prefixes
        self._closed = False
        self._stream_path = Path(stream_path).resolve() if stream_path is not None else None
        if stream_append and self._stream_path is None:
            raise ValueError("stream_append requires stream_path")
        self._stream_session_id = uuid.uuid4().hex if self._stream_path is not None else None
        self._stream = None
        if self._stream_path is not None:
            self._stream_path.parent.mkdir(parents=True, exist_ok=True)
            if stream_append:
                _truncate_incomplete_jsonl_tail(self._stream_path)
            mode = "a" if stream_append else "w"
            self._stream = self._stream_path.open(mode, encoding="utf-8", newline="\n")

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
            if self._closed:
                raise RuntimeError("audit log is closed")
            event = AuditEvent(
                sequence=self._next_sequence,
                timestamp_s=timestamp,
                event_type=event_type,
                details=frozen_details,
            )
            self._events.append(event)
            self._next_sequence += 1
            self._event_count += 1
            if self._stream is not None:
                self._stream.write(_encode_event(event, session_id=self._stream_session_id))
                self._stream.write("\n")
                self._stream.flush()
                if self._event_count % self._fsync_every_events == 0 or event.event_type.startswith(
                    self._fsync_event_prefixes
                ):
                    os.fsync(self._stream.fileno())
            return event

    def events(self) -> tuple[AuditEvent, ...]:
        """Return an immutable, internally detached snapshot of current events."""

        with self._lock:
            return tuple(self._events)

    def write_jsonl(self, path: str | Path) -> None:
        """Atomically replace *path* with a UTF-8 JSON Lines snapshot."""

        destination = Path(path).resolve()
        with self._lock:
            if self._stream_path is not None and destination == self._stream_path:
                if self._stream is not None:
                    self._stream.flush()
                    os.fsync(self._stream.fileno())
                return
        snapshot = self.events()
        destination.parent.mkdir(parents=True, exist_ok=True)
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
                    handle.write(_encode_event(event))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    def __len__(self) -> int:
        with self._lock:
            return self._event_count

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()


def _encode_event(event: AuditEvent, *, session_id: str | None = None) -> str:
    record = {
        "sequence": event.sequence,
        "timestamp_s": event.timestamp_s,
        "event_type": event.event_type,
        "details": _thaw(event.details),
    }
    if session_id is not None:
        record["session_id"] = session_id
    return json.dumps(
        record,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _truncate_incomplete_jsonl_tail(path: Path) -> None:
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb+") as handle:
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return
        position = handle.tell() - 1
        while position > 0:
            chunk_start = max(0, position - 4096)
            handle.seek(chunk_start)
            chunk = handle.read(position - chunk_start)
            newline_index = chunk.rfind(b"\n")
            if newline_index >= 0:
                handle.truncate(chunk_start + newline_index + 1)
                return
            position = chunk_start
        handle.truncate(0)


InMemoryAuditLog = AuditLog
