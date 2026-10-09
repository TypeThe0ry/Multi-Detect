"""Receive-only source-filtered raw telemetry for an owned software-SIM collector.

One caller owns recv, persistence and dispatch. Foreign packets remain in the
ordered capture but never update the accepted-source cache or reach consumers.
Source IDs are an admission filter, NOT sender authentication. Packet bytes and
the caller's decoded projection are both saved; this module does not verify the
decoder, context qualification, clock synchronization, EKF origin or clearance.
It never creates a ContextSnapshot, grant, permit, teacher label or flight command.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
from pathlib import Path

from multidetect.autonomy.serialization import canonical_json, require_int, require_real

CONTRACT = "multidetect.onboard-teacher-raw-telemetry-v1"
MAX_RECORDS = 8192
MAX_PACKET_BYTES = 4096
MAX_ENVELOPE_BYTES = 16384
MAX_TOTAL_BYTES = 64 * 1024 * 1024
FALSE_CLAIMS = {
    name: False
    for name in (
        "source_authenticated",
        "decoded_projection_independently_verified",
        "telemetry_qualified_for_flight",
        "context_created",
        "clock_mapping_verified",
        "coordinate_origin_verified",
        "route_clearance_verified",
        "mission_approved",
        "mission_submitted",
        "mission_executed",
        "teacher_expert_verified",
        "train_admitted",
        "optimizer_performed",
        "execution_authority",
        "hardware_control_enabled",
        "final_release_allowed",
    )
}


def _text(value, name):
    if type(value) is not str or not value or value != value.strip() or len(value) > 256:
        raise ValueError(f"{name} requires bounded canonical text")
    return value


def _write_new(path, payload):
    with path.open("xb") as stream:
        if stream.write(payload) != len(payload):
            raise OSError("Short raw telemetry write")
        stream.flush()
        os.fsync(stream.fileno())
    expected = hashlib.sha256(payload).hexdigest()
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()

    def identity(stat):
        return (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)

    if identity(before) != identity(after) or raw != payload:
        raise ValueError("Persisted telemetry differs from the received capture")
    return {"path": path.as_posix(), "bytes": len(raw), "sha256": expected}


class TeacherTelemetryCapture:
    """Bounded single-thread owner. Any relevant failure permanently poisons it.

    ``reader(timeout_s)`` is the ONLY network receive seam. It must not update a
    telemetry cache before this filter. ``clock()`` is a caller-declared common
    monotonic observer clock; receive times are preserved without translating
    ``time_boot_ms``. Consumers get deep copies only after payload fsync. A slow
    fsync therefore remains observable in ``dispatch_ready_at_s``.
    """

    def __init__(
        self,
        directory,
        *,
        reader,
        clock,
        clock_id,
        transaction_id,
        local_origin_id,
        expected_system_id=1,
        expected_component_id=1,
        max_records=MAX_RECORDS,
    ):
        if not callable(reader) or not callable(clock):
            raise TypeError("One reader and one observer clock are required")
        self.clock_id = _text(clock_id, "clock_id")
        self.transaction_id = _text(transaction_id, "transaction_id")
        self.local_origin_id = _text(local_origin_id, "local_origin_id")
        self.expected_system_id = require_int(
            expected_system_id, "expected_system_id", minimum=1, maximum=255
        )
        self.expected_component_id = require_int(
            expected_component_id, "expected_component_id", minimum=1, maximum=255
        )
        self.max_records = require_int(max_records, "max_records", minimum=1, maximum=MAX_RECORDS)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(exist_ok=False)
        self._reader, self._clock = reader, clock
        self._owner = threading.get_ident()
        self._receiving = False
        self._failure = None
        self._closed = False
        self._last_time = None
        self._latest = {}
        self._records = []
        self._bytes = 0

    def _open(self):
        if threading.get_ident() != self._owner:
            self._failure = "Receiver accessed by another thread"
            raise RuntimeError(self._failure)
        if self._closed or self._failure is not None:
            raise RuntimeError("Telemetry capture is closed or invalid")

    def _now(self):
        now = require_real(self._clock(), "observer time", minimum=0)
        if self._last_time is not None and now < self._last_time:
            raise ValueError("Observer clock regressed")
        self._last_time = now
        return now

    def _persist(self, message, received_at_s):
        if len(self._records) >= self.max_records:
            raise ValueError("Raw telemetry capacity exhausted; no truncation")
        system_id = require_int(message.get_srcSystem(), "source system", minimum=0, maximum=255)
        component_id = require_int(
            message.get_srcComponent(), "source component", minimum=0, maximum=255
        )
        packet_seq = require_int(message.get_seq(), "packet sequence", minimum=0, maximum=255)
        kind = message.get_type()
        if type(kind) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", kind) is None:
            raise ValueError("Invalid decoded MAVLink message type")
        payload = message.get_msgbuf()
        if not isinstance(payload, (bytes, bytearray)) or not 0 < len(payload) <= MAX_PACKET_BYTES:
            raise ValueError("Bounded nonempty original packet bytes required")
        payload = bytes(payload)
        fields = copy.deepcopy(message.to_dict())
        if type(fields) is not dict or fields.get("mavpackettype") != kind:
            raise ValueError("Decoded projection does not name the original message type")
        canonical_json(fields)  # Reject unsupported projections, NaN and Infinity, never coerce.
        if any(
            name in fields
            for name in (
                "message_type",
                "system_id",
                "component_id",
                "source_system_id",
                "source_component_id",
            )
        ):
            raise ValueError("Decoded fields collide with source envelope metadata")
        accepted = (system_id, component_id) == (
            self.expected_system_id,
            self.expected_component_id,
        )
        row = {
            "record_index": len(self._records),
            "received_at_s": received_at_s,
            "clock_id": self.clock_id,
            "transaction_id": self.transaction_id,
            "local_origin_id": self.local_origin_id,
            "system_id": system_id,
            "component_id": component_id,
            "packet_sequence": packet_seq,
            "packet_sha256": hashlib.sha256(payload).hexdigest(),
            "source_accepted": accepted,
            "message": {
                **fields,
                "message_type": kind,
                "source_system_id": system_id,
                "source_component_id": component_id,
            },
            "claims": dict(FALSE_CLAIMS),
        }
        raw = (canonical_json(row) + "\n").encode("utf-8")
        if len(raw) > MAX_ENVELOPE_BYTES or self._bytes + len(raw) + len(payload) > MAX_TOTAL_BYTES:
            raise ValueError("Raw telemetry byte budget exhausted; no truncation")
        stem = f"receive-{len(self._records):05d}"
        packet = _write_new(self.directory / f"{stem}.bin", payload)
        envelope = _write_new(self.directory / f"{stem}.json", raw)
        ready = self._now()
        row["dispatch_ready_at_s"] = ready
        row["envelope"] = envelope
        row["packet"] = packet
        self._records.append(
            {
                "envelope": envelope,
                "packet": packet,
                "source_accepted": accepted,
                "message_type": kind,
                "received_at_s": received_at_s,
                "dispatch_ready_at_s": ready,
            }
        )
        self._bytes += len(raw) + len(payload)
        if accepted:
            self._latest[kind] = copy.deepcopy(row)
        return row

    def receive(self, timeout_s):
        """Return ``(original_message, detached_envelope)`` or None before deadline.

        Every foreign packet is recorded before another receive. It is never
        returned to downstream transaction/outcome/interval consumers. No arrival
        ordering, mission sequence or sensor timestamp is rewritten.
        """
        self._open()
        if self._receiving:
            self._failure = "Reentrant receive is not permitted"
            raise RuntimeError(self._failure)
        self._receiving = True
        try:
            timeout = require_real(timeout_s, "timeout_s", minimum=0, maximum=10)
            deadline = self._now() + timeout
            while True:
                remaining = max(0.0, deadline - self._now())
                message = self._reader(remaining)
                received = self._now()
                if self._failure is not None:
                    raise RuntimeError(self._failure)
                if message is None:
                    return None
                row = self._persist(message, received)
                if self._failure is not None:
                    raise RuntimeError(self._failure)
                if row["source_accepted"]:
                    return message, copy.deepcopy(row)
                if self._last_time >= deadline:
                    return None
        except BaseException as error:
            self._failure = f"{type(error).__name__}: {error}"[:1024]
            raise
        finally:
            self._receiving = False

    def latest_message(self, message_type, *, now_s, maximum_age_s):
        """Source-admitted decoded data ONLY; not a GPS/battery/flight qualification."""
        self._open()
        kind = _text(message_type, "message_type")
        now = require_real(now_s, "now_s", minimum=0)
        age = require_real(maximum_age_s, "maximum_age_s", minimum=0, maximum=10)
        row = self._latest.get(kind)
        if row is None or not 0 <= now - row["received_at_s"] <= age:
            return None
        return copy.deepcopy(row)

    def seal(self):
        self._open()
        return self._finish("recorded", None)

    def abort(self, reason):
        if threading.get_ident() != self._owner or self._closed:
            raise RuntimeError("Capture receipt may only be closed once by its owner")
        return self._finish("aborted", self._failure or _text(reason, "abort reason"))

    def _finish(self, status, error):
        self._closed = True
        receipt = {
            "contract": CONTRACT,
            "status": status,
            "error": error,
            "clock_id": self.clock_id,
            "transaction_id": self.transaction_id,
            "local_origin_id": self.local_origin_id,
            "expected_system_id": self.expected_system_id,
            "expected_component_id": self.expected_component_id,
            "record_count": len(self._records),
            "accepted_source_count": sum(row["source_accepted"] for row in self._records),
            "foreign_source_count": sum(not row["source_accepted"] for row in self._records),
            "payload_bytes": self._bytes,
            "records": copy.deepcopy(self._records),
            "claims": dict(FALSE_CLAIMS),
        }
        binding = _write_new(
            self.directory / "capture-receipt.json",
            (canonical_json(receipt) + "\n").encode("utf-8"),
        )
        return {**receipt, "receipt": binding}
