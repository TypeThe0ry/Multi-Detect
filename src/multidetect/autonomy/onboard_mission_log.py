"""Durable onboard results and an optional, asynchronous telemetry outbox.

The JSONL file is the queue: memory use does not grow during a long outage.
Delivery is at least once, so receivers should deduplicate by mission and sequence.
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    """Encode the plain containers that loop records carry (sets, dataclasses).

    A count record holds the co-visible track ids as a set and an appearance
    dataclass; logging it raised TypeError and crashed every supervised count.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _encoded(document: Mapping[str, Any]) -> bytes:
    text = json.dumps(document, ensure_ascii=False, allow_nan=False, default=_json_default)
    return (text + "\n").encode("utf-8")


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(_encoded(document))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


# High-rate bookkeeping written every tick or heartbeat.  These are flushed to
# the OS but not fsynced one by one: a per-record fsync under the log lock
# stalled the control loop and the link thread for up to ~6 s on a busy disk
# (SITL run8).  Any later durable record's fsync also persists them.
ROUTINE_KINDS = frozenset(
    {
        "decision",
        "estimates",
        "execution",
        "perception_verdict",
        "perception_error",
        "sim_ground_event",
    }
)


def is_routine_record(kind: str, payload: Mapping[str, Any]) -> bool:
    if kind in ROUTINE_KINDS:
        return True
    return kind in {"ground_event", "ground_event_rejected"} and (
        payload.get("event_type") == "heartbeat"
    )


class OnboardMissionLog:
    """Append every record; sync each result before exposing it to a downlink sender.

    Results and safety records (plan, findings, alerts, escalations, operator
    commands, the final summary, any kind not listed as routine) are fsynced
    before ``append`` returns.  Routine records (``is_routine_record``) are
    written and flushed but not fsynced individually, so a slow disk cannot
    stall the control loop at tick rate; a power loss can lose only the
    routine tail after the last durable record.

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
                if not is_routine_record(kind, payload):
                    os.fsync(handle.fileno())
            self._sequence += 1
            return record

    def finish(self, timestamp: float | str, summary: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the final result in the stream and an atomically replaced file."""
        with self._lock:
            record = self.append("final_summary", timestamp, summary=dict(summary))
            _atomic_json(self.summary_path, record)
            return record


# Records the operator always needs, in addition to the rules in DownlinkSelector.
ALWAYS_DOWNLINK_KINDS = frozenset(
    {
        "mission_started",
        "mission_refused",
        "plan",
        "plan_approved",
        "plan_rejected",
        "finding",
        "alert",
        "escalation",
        "envelope_violation",
        "replay_state_lost",
        "final_summary",
        "retask_received",
        "retask_planning",
        "retask_validated",
        "retask_approval",
        "retask_switched",
        "retask_rejected",
    }
)
# Records that overtake routine records still waiting in the backlog.
PRIORITY_DOWNLINK_KINDS = frozenset(
    {"plan", "plan_approved", "plan_rejected", "finding", "alert", "escalation", "final_summary",
     "retask_approval", "retask_switched", "retask_rejected"}
)
DECISION_KEEPALIVE_S = 30.0


def is_priority_record(record: Mapping[str, Any]) -> bool:
    return record.get("kind") in PRIORITY_DOWNLINK_KINDS


class DownlinkSelector:
    """Choose which durable log records go over the operator link.

    Every record stays in ``mission.jsonl`` (the evidence); the link carries
    only what an operator acts on.  The TUNNEL channel moves 44 bytes per
    acknowledged chunk, and a 1 Hz heartbeat/decision stream starved findings
    and the final summary in SITL (run7: 1175 heartbeat records and 558 mostly
    identical decisions; 735 of 2051 records delivered at exit).

    * always: ``ALWAYS_DOWNLINK_KINDS`` (mission start/refusal, the plan and its
      approval/rejection, findings, alerts, escalations, envelope violations,
      the final summary);
    * ``decision``: only when ``(action, reason)`` differs from the last
      downlinked decision, plus a keepalive every ``DECISION_KEEPALIVE_S`` of
      record time;
    * ``ground_event`` / ``ground_event_rejected``: all but routine heartbeats;
    * everything else (execution, estimates, perception verdicts, SIM
      bookkeeping) is local evidence only; the 1 Hz status document carries
      live progress.

    ``wants`` is pure; ``commit`` records a decision once it was delivered, so
    a failed send is retried rather than mistaken for a repeat.
    """

    def __init__(self, keepalive_s: float = DECISION_KEEPALIVE_S) -> None:
        self.keepalive_s = keepalive_s
        self._decision: tuple[Any, Any] | None = None
        self._decision_at: float | None = None

    @staticmethod
    def _stamp(record: Mapping[str, Any]) -> float | None:
        value = record.get("timestamp")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def wants(self, record: Mapping[str, Any]) -> bool:
        kind = record.get("kind")
        if kind in ALWAYS_DOWNLINK_KINDS:
            return True
        if kind in {"ground_event", "ground_event_rejected"}:
            return record.get("event_type") != "heartbeat"
        if kind != "decision":
            return False
        if (record.get("action"), record.get("reason")) != self._decision:
            return True
        stamp = self._stamp(record)
        return (
            stamp is None
            or self._decision_at is None
            or not 0 <= stamp - self._decision_at < self.keepalive_s
        )

    def commit(self, record: Mapping[str, Any]) -> None:
        if record.get("kind") == "decision":
            self._decision = (record.get("action"), record.get("reason"))
            self._decision_at = self._stamp(record)


class DownlinkQueue:
    """Stream persisted records in a daemon thread when a link is available.

    The sender must return only after its receiver acknowledges the record.
    Returning False or raising retains that record for the next reconnect or
    notify. A stalled sender cannot stall append, flight, or bounded shutdown.

    ``selector`` (default ``DownlinkSelector``) keeps routine records off the
    link; the cursor still passes them, so ``acknowledged_sequence`` reaches the
    end of the log.  A priority record (``PRIORITY_DOWNLINK_KINDS``) further
    ahead in the backlog is sent before the next routine record.  The cursor
    stays sequential, so after a restart such a record may be delivered twice;
    receivers deduplicate by mission and sequence.
    """

    SCAN_AHEAD_BYTES = 1 << 20

    def __init__(
        self,
        log: OnboardMissionLog,
        sender: Callable[[dict[str, Any]], object] | None,
        selector: DownlinkSelector | None = None,
    ) -> None:
        self.log = log
        self.sender = sender
        self.selector = selector if selector is not None else DownlinkSelector()
        self.last_error: Exception | None = None
        self._connected = threading.Event()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._offset = 0
        self._sequence = 0
        self._restore_cursor()
        self._acknowledged = self._sequence  # the durable cursor
        # Priority records found between the cursor and the end of the log.
        self._scan_offset, self._scan_sequence = self._offset, self._sequence
        self._ahead: list[dict[str, Any]] = []
        self._sent_ahead: set[int] = set()
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
        return self._acknowledged

    def notify(self) -> None:
        self._wake.set()

    # Readers take no append lock: a record line is complete once it ends in a
    # newline, so link I/O never delays the control loop's ``append``.
    def _next_record(self) -> tuple[dict[str, Any], int] | None:
        if not self.log.path.exists():
            return None
        with self.log.path.open("rb") as handle:
            handle.seek(self._offset)
            line = handle.readline()
            if not line.endswith(b"\n"):
                return None  # nothing new, or an append still being written
            return self.log._parse_record(line, self._sequence + 1), handle.tell()

    def _scan_ahead(self) -> None:
        """Index priority records between the cursor and the end of the log."""
        if self._scan_sequence < self._sequence:
            self._scan_offset, self._scan_sequence = self._offset, self._sequence
        if not self.log.path.exists():
            return
        with self.log.path.open("rb") as handle:
            handle.seek(self._scan_offset)
            budget = self.SCAN_AHEAD_BYTES
            while budget > 0:
                line = handle.readline()
                if not line.endswith(b"\n"):
                    return
                record = self.log._parse_record(line, self._scan_sequence + 1)
                self._scan_offset, self._scan_sequence = handle.tell(), record["sequence"]
                budget -= len(line)
                if is_priority_record(record):
                    self._ahead.append(record)

    def _persist_cursor(self) -> None:
        if self._acknowledged == self._sequence:
            return
        cursor = {
            "mission_id": self.log.mission_id,
            "sequence": self._sequence,
            "offset": self._offset,
        }
        _atomic_json(self.log.cursor_path, cursor)
        self._acknowledged = self._sequence

    def _advance(self, sequence: int, offset: int, *, persist: bool = True) -> None:
        """Move past a record; skipped runs persist the cursor once, at their end."""
        self._offset, self._sequence = offset, sequence
        if persist:
            self._persist_cursor()
        self._sent_ahead.discard(sequence)
        while self._ahead and self._ahead[0]["sequence"] <= sequence:
            self._ahead.pop(0)

    def _deliver(self, record: dict[str, Any]) -> bool:
        """Send one record; False when the queue closed while it was in flight."""
        sender = self.sender
        if sender is None:
            return False
        if sender(record) is False:
            raise RuntimeError("downlink receiver did not acknowledge record")
        self.last_error = None
        self.selector.commit(record)
        return not self._closed.is_set()

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
                        self._persist_cursor()
                        break
                    record, offset = pending
                    sequence = record["sequence"]
                    if sequence in self._sent_ahead:
                        self._advance(sequence, offset, persist=False)
                        continue
                    if not is_priority_record(record):
                        self._scan_ahead()
                        ahead = next(
                            (
                                item
                                for item in self._ahead
                                if item["sequence"] not in self._sent_ahead
                            ),
                            None,
                        )
                        if ahead is not None:
                            # Overtake: the cursor stays on the routine record.
                            if not self._deliver(ahead):
                                break
                            self._sent_ahead.add(ahead["sequence"])
                            continue
                    if not self.selector.wants(record):
                        self._advance(sequence, offset, persist=False)
                        continue
                    if not self._deliver(record):
                        break
                    self._advance(sequence, offset)
                except Exception as error:
                    self.last_error = error
                    break
            try:
                self._persist_cursor()  # skipped records before a stop or error
            except OSError as error:
                self.last_error = error

    def close(self) -> None:
        self._closed.set()
        self._wake.set()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=0.05)
