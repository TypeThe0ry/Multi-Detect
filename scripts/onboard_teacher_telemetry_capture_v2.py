"""Packet-first, reversible typed telemetry evidence; never flight authority.

The SHA-authenticated v1 owns receiving, clocks, filtering and poisoning. V2
replaces persistence only: original packet fsync precedes projection access.
The typed sidecar retains to_dict() value types and IEEE bits; it does not undo
the MAVLink decoder's own earlier formatting. The compatible ``message``
projection retains native JSON values, projects numeric sequences as lists and
tags non-finite/binary values; those tags are NOT valid qualified numbers.
Array byte order is explicit; this decoder rejects a different host byte order.
No independent MAVLink decoding, source authentication or authority is claimed.
"""

from __future__ import annotations

import array
import copy
import hashlib
import importlib
import math
import re
import struct
import sys
from pathlib import Path

BASE_BYTES = 11835
BASE_SHA256 = "7758abc0e123e51f8f871dce060796347e63e2858759a58a7078fa669309ea40"
_BASE_PATH = Path(__file__).with_name("onboard_teacher_telemetry_capture.py").resolve()
_base_raw = _BASE_PATH.read_bytes()
if len(_base_raw) != BASE_BYTES or hashlib.sha256(_base_raw).hexdigest() != BASE_SHA256:
    raise RuntimeError("Frozen teacher capture v1 source differs")
_base = importlib.import_module("scripts.onboard_teacher_telemetry_capture")
if Path(_base.__file__).resolve() != _BASE_PATH:
    raise RuntimeError("Frozen teacher capture v1 module origin differs")

canonical_json = _base.canonical_json
require_int = _base.require_int
FALSE_CLAIMS = _base.FALSE_CLAIMS
MAX_PACKET_BYTES = _base.MAX_PACKET_BYTES
MAX_ENVELOPE_BYTES = _base.MAX_ENVELOPE_BYTES
MAX_TOTAL_BYTES = _base.MAX_TOTAL_BYTES
_write_new = _base._write_new
CONTRACT = "multidetect.onboard-teacher-raw-telemetry-v2"
ENCODING = "multidetect.teacher-typed-values-v1"
TAG = "$multidetect_teacher_typed_v1"
MAX_DEPTH = 16
MAX_NODES = 2048
MAX_INTEGER_BITS = 4096


class _Budget:
    def __init__(self):
        self.nodes = self.bytes = 0
        self.active = set()

    def visit(self, depth):
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            raise ValueError("Typed projection depth/node budget exhausted")

    def charge(self, count):
        self.bytes += count
        if self.bytes > MAX_ENVELOPE_BYTES:
            raise ValueError("Typed projection content byte budget exhausted")

    def text(self, value):
        if type(value) is not str or len(value) > MAX_ENVELOPE_BYTES:
            raise ValueError("Bounded typed text required")
        self.charge(len(value.encode("utf-8")))
        return value


def _encode(value, budget, depth):
    budget.visit(depth)
    kind = type(value)
    if value is None:
        return ["none"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        if value.bit_length() > MAX_INTEGER_BITS:
            raise ValueError("Typed integer bit budget exhausted")
        return ["int", budget.text(str(value))]
    if kind is float:
        return ["float64", struct.pack(">d", value).hex()]
    if kind is str:
        return ["str", budget.text(value)]
    if kind in (bytes, bytearray):
        budget.charge(len(value))
        return [kind.__name__, bytes(value).hex()]
    if kind is array.array:
        if len(value) > MAX_NODES:
            raise ValueError("Typed array element budget exhausted")
        budget.charge(len(value) * value.itemsize)
        return ["array", value.typecode, value.itemsize, sys.byteorder, value.tobytes().hex()]
    if kind not in (dict, list, tuple):
        raise TypeError(f"Unsupported typed projection type: {kind.__name__}")
    if len(value) > MAX_NODES or id(value) in budget.active:
        raise ValueError("Oversize or cyclic typed projection")
    budget.active.add(id(value))
    try:
        if kind is dict:
            entries = []
            for key, item in value.items():
                if type(key) is not str or len(key) > 256:
                    raise ValueError("Bounded string projection keys required")
                entries.append([budget.text(key), _encode(item, budget, depth + 1)])
            return ["dict", entries]
        return [kind.__name__, [_encode(item, budget, depth + 1) for item in value]]
    finally:
        budget.active.remove(id(value))


def encode_typed_value(value):
    """A fully tagged tree: user lists/dicts can never impersonate a type tag."""
    node = _encode(value, _Budget(), 0)
    if len(canonical_json({"value": node}).encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise ValueError("Typed projection encoded byte budget exhausted")
    return node


def _decode(node, budget, depth):
    budget.visit(depth)
    if type(node) is not list or not node or type(node[0]) is not str:
        raise ValueError("Invalid typed node")
    tag = node[0]
    if tag == "none" and len(node) == 1:
        return None
    if len(node) == 2:
        value = node[1]
        if tag == "bool" and type(value) is bool:
            return value
        if tag == "str":
            return budget.text(value)
        if tag == "int":
            text = budget.text(value)
            if re.fullmatch(r"0|-?[1-9][0-9]*", text) is None or len(text) > 1235:
                raise ValueError("Noncanonical or oversize typed integer")
            result = int(text)
            if result.bit_length() > MAX_INTEGER_BITS:
                raise ValueError("Typed integer bit budget exhausted")
            return result
        if tag in ("float64", "bytes", "bytearray"):
            text = budget.text(value)
            if re.fullmatch(r"(?:[0-9a-f]{2})*", text) is None:
                raise ValueError("Canonical lowercase hexadecimal required")
            raw = bytes.fromhex(text)
            if tag == "float64":
                if len(raw) != 8:
                    raise ValueError("Binary64 requires exactly eight bytes")
                return struct.unpack(">d", raw)[0]
            return raw if tag == "bytes" else bytearray(raw)
        if tag in ("list", "tuple", "dict"):
            if type(value) is not list or len(value) > MAX_NODES:
                raise ValueError("Bounded typed entries required")
            if tag == "dict":
                result = {}
                for entry in value:
                    if type(entry) is not list or len(entry) != 2:
                        raise ValueError("Invalid typed dictionary entry")
                    key = budget.text(entry[0])
                    if len(key) > 256 or key in result:
                        raise ValueError("Duplicate or oversize typed dictionary key")
                    result[key] = _decode(entry[1], budget, depth + 1)
                return result
            decoded = [_decode(item, budget, depth + 1) for item in value]
            return decoded if tag == "list" else tuple(decoded)
    if tag == "array" and len(node) == 5:
        _, typecode, itemsize, byteorder, data = node
        if type(typecode) is not str or len(typecode) != 1 or typecode not in array.typecodes:
            raise ValueError("Unknown array typecode")
        if type(itemsize) is not int or byteorder != sys.byteorder:
            raise ValueError("Array item size/host byte order differs")
        text = budget.text(data)
        if re.fullmatch(r"(?:[0-9a-f]{2})*", text) is None:
            raise ValueError("Canonical array hexadecimal required")
        result = array.array(typecode)
        if result.itemsize != itemsize or len(text) % (2 * itemsize):
            raise ValueError("Array payload/item size mismatch")
        result.frombytes(bytes.fromhex(text))
        return result
    raise ValueError("Unknown or malformed typed node")


def decode_typed_value(node):
    result = _decode(node, _Budget(), 0)
    if canonical_json({"value": encode_typed_value(result)}) != canonical_json({"value": node}):
        raise ValueError("Typed node did not round-trip canonically")
    return result


def _project(node):
    tag = node[0]
    if tag == "none":
        return None
    if tag == "bool" or tag == "str":
        return node[1]
    if tag == "int":
        return int(node[1])
    if tag == "float64":
        value = struct.unpack(">d", bytes.fromhex(node[1]))[0]
        return value if math.isfinite(value) else {TAG: node}
    if tag in ("list", "tuple"):
        return [_project(value) for value in node[1]]
    if tag == "array":
        value = decode_typed_value(node)
        return [_project(encode_typed_value(item)) for item in value]
    if tag == "dict":
        if any(key == TAG for key, _ in node[1]):
            return {TAG: node}  # escape original dictionaries that resemble a transport tag
        return {key: _project(value) for key, value in node[1]}
    return {TAG: node}  # binary values are never lossy str/repr or invented numeric values


class TeacherTelemetryCaptureV2(_base.TeacherTelemetryCapture):
    """Same ctor/receive/latest API. A failure leaves durable orphan evidence.

    Completed records alone may update the accepted-source cache. Packet or
    sidecar/envelope failures permanently poison the inherited owner. An orphan
    is not a received/qualified downstream sample; abort records its real stage.
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
        max_records=_base.MAX_RECORDS,
    ):
        super().__init__(
            directory,
            reader=reader,
            clock=clock,
            clock_id=clock_id,
            transaction_id=transaction_id,
            local_origin_id=local_origin_id,
            expected_system_id=expected_system_id,
            expected_component_id=expected_component_id,
            max_records=max_records,
        )
        self._attempts = []

    def _save(self, path, raw):
        if self._bytes + len(raw) > MAX_TOTAL_BYTES:
            raise ValueError("Raw telemetry total byte budget exhausted; no truncation")
        result = _write_new(path, raw)
        self._bytes += len(raw)
        return result

    def _persist(self, message, received_at_s):
        if len(self._attempts) >= self.max_records:
            raise ValueError("Raw telemetry capacity exhausted; no truncation")
        payload = message.get_msgbuf()
        if type(payload) not in (bytes, bytearray) or not 0 < len(payload) <= MAX_PACKET_BYTES:
            raise ValueError("Bounded nonempty original packet bytes required")
        payload = bytes(payload)
        index = len(self._attempts)
        stem = f"receive-{index:05d}"
        packet_path = self.directory / f"{stem}.bin"
        attempt = {
            "record_index": index,
            "received_at_s": received_at_s,
            "packet": {
                "path": packet_path.as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            },
            "packet_durable": False,
            "status": "packet-write-pending",
            "message_type": None,
            "source_system_id": None,
            "source_component_id": None,
            "projection_error": None,
        }
        self._attempts.append(attempt)
        try:
            packet = self._save(packet_path, payload)  # MUST precede every decoded accessor
            attempt.update(packet=packet, packet_durable=True, status="packet-durable")
            pending = {
                "contract": CONTRACT,
                "record_index": index,
                "received_at_s": received_at_s,
                "clock_id": self.clock_id,
                "transaction_id": self.transaction_id,
                "local_origin_id": self.local_origin_id,
                "packet": packet,
                "projection_complete": False,
                "claims": dict(FALSE_CLAIMS),
            }
            attempt["pending"] = self._save(
                self.directory / f"{stem}-pending.json",
                (canonical_json(pending) + "\n").encode(),
            )
            kind = message.get_type()
            attempt["message_type"] = kind if type(kind) is str and len(kind) <= 64 else None
            if type(kind) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", kind) is None:
                raise ValueError("Invalid decoded MAVLink message type")
            system = require_int(message.get_srcSystem(), "source system", minimum=0, maximum=255)
            component = require_int(
                message.get_srcComponent(),
                "source component",
                minimum=0,
                maximum=255,
            )
            attempt.update(source_system_id=system, source_component_id=component)
            sequence = require_int(message.get_seq(), "packet sequence", minimum=0, maximum=255)
            fields = message.to_dict()
            if type(fields) is not dict or fields.get("mavpackettype") != kind:
                raise ValueError("Decoded projection does not name the original message type")
            if any(
                key in fields
                for key in (
                    "message_type",
                    "system_id",
                    "component_id",
                    "source_system_id",
                    "source_component_id",
                )
            ):
                raise ValueError("Decoded fields collide with source envelope metadata")
            encoded = encode_typed_value(fields)
            typed = {
                "contract": ENCODING,
                "record_index": index,
                "packet_sha256": packet["sha256"],
                "message_type": kind,
                "value": encoded,
            }
            typed_raw = (canonical_json(typed) + "\n").encode()
            if len(typed_raw) > MAX_ENVELOPE_BYTES:
                raise ValueError("Typed sidecar byte budget exhausted")
            projection = _project(encoded)
            accepted = (system, component) == (self.expected_system_id, self.expected_component_id)
            row = {
                "record_index": index,
                "received_at_s": received_at_s,
                "clock_id": self.clock_id,
                "transaction_id": self.transaction_id,
                "local_origin_id": self.local_origin_id,
                "system_id": system,
                "component_id": component,
                "packet_sequence": sequence,
                "packet_sha256": packet["sha256"],
                "source_accepted": accepted,
                "message": {
                    **projection,
                    "message_type": kind,
                    "source_system_id": system,
                    "source_component_id": component,
                },
                "claims": dict(FALSE_CLAIMS),
            }
            raw = (canonical_json(row) + "\n").encode()
            if len(raw) > MAX_ENVELOPE_BYTES:
                raise ValueError("Raw telemetry envelope byte budget exhausted")
            attempt["typed_projection"] = self._save(
                self.directory / f"{stem}-typed.json",
                typed_raw,
            )
            envelope = self._save(self.directory / f"{stem}.json", raw)
            attempt["envelope"] = envelope
            ready = self._now()
            row.update(dispatch_ready_at_s=ready, envelope=envelope, packet=packet)
            detached = copy.deepcopy(row) if accepted else None
            record = {
                "envelope": envelope,
                "packet": packet,
                "source_accepted": accepted,
                "message_type": kind,
                "received_at_s": received_at_s,
                "dispatch_ready_at_s": ready,
                "typed_projection": attempt["typed_projection"],
                "pending": attempt["pending"],
            }
            self._records.append(record)
            attempt["status"] = "projection-complete"
            if accepted:
                self._latest[kind] = detached
            return row
        except BaseException as error:
            attempt["projection_error"] = f"{type(error).__name__}: {error}"[:1024]
            if attempt["status"] != "projection-complete":
                attempt["status"] = (
                    "orphan-packet" if attempt["packet_durable"] else "packet-write-failed"
                )
            raise

    def _finish(self, status, error):
        self._closed = True
        receipt = {
            "contract": CONTRACT,
            "encoding": ENCODING,
            "status": status,
            "error": error,
            "clock_id": self.clock_id,
            "transaction_id": self.transaction_id,
            "local_origin_id": self.local_origin_id,
            "expected_system_id": self.expected_system_id,
            "expected_component_id": self.expected_component_id,
            "record_count": len(self._records),
            "packet_attempt_count": len(self._attempts),
            "accepted_source_count": sum(r["source_accepted"] for r in self._records),
            "foreign_source_count": sum(not r["source_accepted"] for r in self._records),
            "payload_bytes": self._bytes,
            "records": copy.deepcopy(self._records),
            "payload_bytes_scope": (
                "fsynced packet/pending/typed/envelope files; final receipt excluded"
            ),
            "packet_attempts": copy.deepcopy(self._attempts),
            "orphan_packet_count": sum(
                r["status"] != "projection-complete" for r in self._attempts
            ),
            "packet_first_fsync_required": True,
            "claims": dict(FALSE_CLAIMS),
            "typed_sidecar_is_lossless": True,
            "compatible_projection_sequence_types_normalized_to_lists": True,
            "legacy_source": {
                "path": _BASE_PATH.as_posix(),
                "bytes": BASE_BYTES,
                "sha256": BASE_SHA256,
            },
        }
        binding = _write_new(
            self.directory / "capture-receipt.json",
            (canonical_json(receipt) + "\n").encode(),
        )
        return {**receipt, "receipt": binding}
