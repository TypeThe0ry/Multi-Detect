"""Durable onboard results and an optional, asynchronous telemetry outbox.

The JSONL file is the queue: memory use does not grow during a long outage.
Delivery is at least once, so receivers should deduplicate by mission and sequence.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _encoded(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(_encoded(document))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class OnboardMissionLog:
    """Append and sync every result before exposing it to a downlink sender.

    Each directory belongs to one mission. An incomplete/corrupt existing log
    raises instead of truncating evidence or silently appending an invalid line.
    """

    def __init__(self, directory: str | Path, mission_id: str) -> None:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise ValueError("mission_id must be a non-empty string")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mission_id = mission_id
        self.path = self.directory / "mission.jsonl"
        self.summary_path = self.directory / "summary.json"
        self.cursor_path = self.directory / "downlink-cursor.json"
        self._lock = threading.RLock()
        self._sequence = 0
        if self.path.exists():
            with self.path.open("rb") as handle:
                for line in handle:
                    record = self._parse_record(line, self._sequence + 1)
                    self._sequence = record["sequence"]

    def _parse_record(self, line: bytes, sequence: int) -> dict[str, Any]:
        if not line.endswith(b"\n"):
            raise ValueError("onboard mission log has an incomplete tail")
        record = json.loads(line)
        if record.get("mission_id") != self.mission_id:
            raise ValueError("onboard mission log belongs to a different mission_id")
        if record.get("sequence") != sequence:
            raise ValueError("onboard mission log sequence is invalid")
        return record

    def append(self, kind: str, timestamp: float | str, **payload: Any) -> dict[str, Any]:
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if {"mission_id", "sequence"} & payload.keys():
            raise ValueError("payload cannot overwrite mission identity or sequence")
        with self._lock:
            record = {
                "mission_id": self.mission_id,
                "sequence": self._sequence + 1,
                "kind": kind,
                "timestamp": timestamp,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                **payload,
            }
            encoded = _encoded(record)
            with self.path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._sequence += 1
            return record

    def finish(self, timestamp: float | str, summary: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the final result in the stream and an atomically replaced file."""
        with self._lock:
            record = self.append("final_summary", timestamp, summary=dict(summary))
            _atomic_json(self.summary_path, record)
            return record


class DownlinkQueue:
    """Stream persisted records in a daemon thread when a link is available.

    The sender must return only after its receiver acknowledges the record.
    Returning False or raising retains that record for the next reconnect or
    notify. A stalled sender cannot stall append, flight, or bounded shutdown.
    """

    def __init__(
        self,
        log: OnboardMissionLog,
        sender: Callable[[dict[str, Any]], object] | None,
    ) -> None:
        self.log = log
        self.sender = sender
        self.last_error: Exception | None = None
        self._connected = threading.Event()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._offset = 0
        self._sequence = 0
        self._restore_cursor()
        self._worker = threading.Thread(
            target=self._run, name=f"mission-downlink-{log.mission_id}", daemon=True
        )
        self._worker.start()

    def _restore_cursor(self) -> None:
        # An invalid cursor causes replay, never a silently skipped result.
        if not self.log.cursor_path.exists():
            return
        try:
            cursor = json.loads(self.log.cursor_path.read_text(encoding="utf-8"))
            if cursor["mission_id"] != self.log.mission_id:
                return
            target = cursor["offset"]
            if type(target) is not int or target < 0:
                return
            offset = sequence = 0
            with self.log.path.open("rb") as handle:
                while offset < target:
                    line = handle.readline()
                    if not line:
                        return
                    self.log._parse_record(line, sequence + 1)
                    offset = handle.tell()
                    sequence += 1
            if offset == target and sequence == cursor["sequence"]:
                self._offset, self._sequence = offset, sequence
        except (OSError, ValueError, KeyError, TypeError):
            return

    def set_connected(self, connected: bool) -> None:
        if connected:
            self._connected.set()
            self.notify()
        else:
            self._connected.clear()

    def set_sender(self, sender: Callable[[dict[str, Any]], object] | None) -> None:
        self.sender = sender
        self.notify()

    @property
    def acknowledged_sequence(self) -> int:
        return self._sequence

    def notify(self) -> None:
        self._wake.set()

    def _next_record(self) -> tuple[dict[str, Any], int] | None:
        with self.log._lock:
            if not self.log.path.exists():
                return None
            with self.log.path.open("rb") as handle:
                handle.seek(self._offset)
                line = handle.readline()
                if not line:
                    return None
                return self.log._parse_record(line, self._sequence + 1), handle.tell()

    def _run(self) -> None:
        while not self._closed.is_set():
            self._wake.wait()
            self._wake.clear()
            while (
                not self._closed.is_set() and self._connected.is_set() and self.sender is not None
            ):
                try:
                    pending = self._next_record()
                    if pending is None:
                        break
                    record, offset = pending
                    sender = self.sender
                    if sender is None:
                        break
                    if sender(record) is False:
                        raise RuntimeError("downlink receiver did not acknowledge record")
                    if self._closed.is_set():
                        break
                    cursor = {
                        "mission_id": self.log.mission_id,
                        "sequence": record["sequence"],
                        "offset": offset,
                    }
                    _atomic_json(self.log.cursor_path, cursor)
                    self._offset, self._sequence = offset, record["sequence"]
                    self.last_error = None
                except Exception as error:
                    self.last_error = error
                    break

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=0.05)
