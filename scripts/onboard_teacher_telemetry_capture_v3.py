"""Bounded single-writer raw/projection journals, not a socket or flight owner.

Raw batches are fsynced and read back before decoded access. Projection batches
are then fsynced and read back before atomic cache publication. A partial tail
is evidence, never an accepted message. Caller receive timestamps and sender
times are preserved; materialization does not make an old message fresh.
The pinned V2 codec preserves to_dict() types/bits, not prior decoder formatting.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import re
import struct
import threading
from pathlib import Path

CODEC_BYTES = 18202
CODEC_SHA256 = "d1a5a4c82086bcff8d9bf89c86ba99902097d424b7a4e3594bd89564d24bb634"
_CODEC_PATH = Path(__file__).with_name("onboard_teacher_telemetry_capture_v2.py").resolve()


def _verify_codec():
    raw = _CODEC_PATH.read_bytes()
    if len(raw) != CODEC_BYTES or hashlib.sha256(raw).hexdigest() != CODEC_SHA256:
        raise RuntimeError("Frozen teacher capture V2 codec source differs")
    legacy = _CODEC_PATH.with_name("onboard_teacher_telemetry_capture.py").read_bytes()
    if (
        len(legacy) != 11835
        or hashlib.sha256(legacy).hexdigest()
        != "7758abc0e123e51f8f871dce060796347e63e2858759a58a7078fa669309ea40"
    ):
        raise RuntimeError("Frozen teacher capture V1 codec dependency differs")


_verify_codec()
_codec = importlib.import_module("scripts.onboard_teacher_telemetry_capture_v2")
if Path(_codec.__file__).resolve() != _CODEC_PATH:
    raise RuntimeError("Frozen teacher capture V2 module origin differs")
canonical_json = _codec.canonical_json
require_int = _codec.require_int
require_real = _codec._base.require_real
FALSE_CLAIMS = _codec.FALSE_CLAIMS
CONTRACT = "multidetect.onboard-teacher-raw-telemetry-v3"
MAX_BATCH = 16
MAX_RECORDS = 8192
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PACKET_BYTES = 4096
MAX_PROJECTION_BYTES = 40 * 1024
RAW_MAGIC = b"MDTEACHER-RAW-V3\n"
PROJECTION_MAGIC = b'{"contract":"multidetect.teacher-projection-journal-v3"}\n'


def _raw_json(value):
    return (canonical_json(value) + "\n").encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _identity(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def _error(error):
    return f"{type(error).__name__}: {error}"[:1024]


class TeacherTelemetryCaptureV3:
    """One thread owns append/cache/materialize/close; caller owns RX and batching.

    append_batch returns detached journal rows, including foreign rows. Only
    source-accepted dual-committed rows can be latest. Neither journal rows nor
    receive IDs authenticate a sender or qualify a context. seal/abort are once
    only, and failures require abort; they never refund an attempted record.
    """

    def __init__(
        self,
        directory,
        *,
        clock,
        clock_id,
        transaction_id,
        local_origin_id,
        expected_system_id=1,
        expected_component_id=1,
        max_records=MAX_RECORDS,
    ):
        if not callable(clock):
            raise TypeError("One caller-declared monotonic clock required")
        self.clock_id = _codec._base._text(clock_id, "clock_id")
        self.transaction_id = _codec._base._text(transaction_id, "transaction_id")
        self.local_origin_id = _codec._base._text(local_origin_id, "local_origin_id")
        self.expected_system_id = require_int(
            expected_system_id, "expected_system_id", minimum=1, maximum=255
        )
        self.expected_component_id = require_int(
            expected_component_id, "expected_component_id", minimum=1, maximum=255
        )
        self.max_records = require_int(max_records, "max_records", minimum=1, maximum=MAX_RECORDS)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(exist_ok=False)
        self._clock, self._owner = clock, threading.get_ident()
        self._busy = self._closed = False
        self._failure = self._last_time = self._last_received = None
        self._latest, self._records, self._attempts, self._batches, self._snapshots = (
            {},
            [],
            [],
            [],
            [],
        )
        self._charged_bytes = self._durable_bytes = 0
        self._streams, self._sizes, self._digests, self._stats = {}, {}, {}, {}
        self._commits = {
            stage: {"attempted": 0, "fsync_returned": 0, "verified": 0}
            for stage in ("raw", "projection")
        }
        try:
            for stage, name, magic in (
                ("raw", "raw-packets.journal", RAW_MAGIC),
                ("projection", "projections.jsonl", PROJECTION_MAGIC),
            ):
                stream = (self.directory / name).open("xb+", buffering=0)
                self._streams[stage] = stream
                if stream.write(magic) != len(magic):
                    raise OSError("Short journal header write")
                stream.flush()
                os.fsync(stream.fileno())
                self._sizes[stage] = len(magic)
                self._digests[stage] = hashlib.sha256(magic)
                self._stats[stage] = _identity(Path(stream.name).stat())
                self._charged_bytes += len(magic)
                self._durable_bytes += len(magic)
        except BaseException:
            self._close_streams()
            raise

    def _owner_check(self):
        if threading.get_ident() != self._owner:
            self._failure = "Writer accessed by another thread"
            raise RuntimeError(self._failure)

    def _open(self):
        self._owner_check()
        if self._closed or self._failure is not None:
            raise RuntimeError("Telemetry journals are closed or poisoned")

    def _begin(self):
        self._open()
        if self._busy:
            self._failure = "Reentrant writer operation"
            raise RuntimeError(self._failure)
        self._busy = True

    def _now(self):
        now = require_real(self._clock(), "writer clock", minimum=0)
        if self._last_time is not None and now < self._last_time:
            raise ValueError("Writer clock regressed")
        if self._failure is not None:
            raise RuntimeError(self._failure)
        self._last_time = now
        return now

    def _append(self, stage, raw, batch):
        stream = self._streams[stage]
        path = Path(stream.name)
        before = path.stat()
        if path.is_symlink() or _identity(before) != self._stats[stage]:
            raise ValueError("Journal identity changed before append")
        if self._charged_bytes + len(raw) > MAX_TOTAL_BYTES:
            raise ValueError("Telemetry data byte budget exhausted; no truncation")
        offset = self._sizes[stage]
        expected = {
            "path": path.as_posix(),
            "offset": offset,
            "bytes": len(raw),
            "sha256": _sha(raw),
        }
        batch[stage] = {"slice": expected, "fsync_returned": False, "verified": False}
        counter = self._commits[stage]
        counter["attempted"] += 1
        self._charged_bytes += len(raw)  # partial attempts remain spent; capture is poisoned
        stream.seek(offset)
        if stream.write(raw) != len(raw):
            raise OSError(f"Short {stage} journal write")
        stream.flush()
        os.fsync(stream.fileno())
        counter["fsync_returned"] += 1
        batch[stage]["fsync_returned"] = True
        after_write = path.stat()
        if (after_write.st_dev, after_write.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Journal file identity changed during append")
        stream.seek(offset)
        checked = stream.read(len(raw) + 1)
        after_read = path.stat()
        if (
            checked != raw
            or after_read.st_size != offset + len(raw)
            or _identity(after_write) != _identity(after_read)
        ):
            raise ValueError("Journal batch bytes/identity differ after fsync")
        self._sizes[stage] += len(raw)
        self._digests[stage].update(raw)
        self._stats[stage] = _identity(after_read)
        self._durable_bytes += len(raw)
        counter["verified"] += 1
        batch[stage]["verified"] = True
        return expected

    def _projection(self, original, attempt):
        kind = original.get_type()
        if type(kind) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", kind) is None:
            raise ValueError("Invalid decoded MAVLink message type")
        system = require_int(original.get_srcSystem(), "source system", minimum=0, maximum=255)
        component = require_int(
            original.get_srcComponent(), "source component", minimum=0, maximum=255
        )
        sequence = require_int(original.get_seq(), "packet sequence", minimum=0, maximum=255)
        fields = original.to_dict()
        if type(fields) is not dict or fields.get("mavpackettype") != kind:
            raise ValueError("Decoded projection differs from original kind")
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
            raise ValueError("Decoded fields collide with source envelope")
        typed = _codec.encode_typed_value(fields)
        row = {
            "record_index": attempt["record_index"],
            "received_at_s": attempt["received_at_s"],
            "clock_id": self.clock_id,
            "transaction_id": self.transaction_id,
            "local_origin_id": self.local_origin_id,
            "system_id": system,
            "component_id": component,
            "packet_sequence": sequence,
            "packet_sha256": attempt["packet_sha256"],
            "source_accepted": (system, component)
            == (self.expected_system_id, self.expected_component_id),
            "message": {
                **_codec._project(typed),
                "message_type": kind,
                "source_system_id": system,
                "source_component_id": component,
            },
            "claims": dict(FALSE_CLAIMS),
        }
        if len(_raw_json(row)) > _codec.MAX_ENVELOPE_BYTES:
            raise ValueError("Compatible envelope byte budget exhausted")
        attempt.update(message_type=kind, source_accepted=row["source_accepted"])
        return row, typed

    def append_batch(self, samples):
        """Append 1..16 (original_message, actual_received_at_s) pairs atomically.

        Before raw commit the only message accessor is get_msgbuf(). All raw
        packet clones are owned by this call; callers must not mutate messages.
        A raw/schema validation failure before the write has no durable packet
        claim. Earlier successfully dual-committed batches remain in the receipt.
        """
        self._begin()
        batch = {"batch_index": len(self._batches), "status": "pending", "error": None}
        self._batches.append(batch)
        try:
            _verify_codec()
            started = self._now()
            batch["started_at_s"] = started
            if type(samples) not in (list, tuple) or not 1 <= len(samples) <= MAX_BATCH:
                raise ValueError("A bounded nonempty batch of at most 16 samples required")
            if len(self._attempts) + len(samples) > self.max_records:
                raise ValueError("Telemetry record budget exhausted; no truncation")
            raw_parts, originals, attempts = [], [], []
            offset = self._sizes["raw"]
            last_received = self._last_received
            for pair in samples:
                if type(pair) not in (list, tuple) or len(pair) != 2:
                    raise TypeError("Each sample must be an original message/time pair")
                original, received = pair
                received = require_real(received, "received_at_s", minimum=0)
                if received > started or (last_received is not None and received < last_received):
                    raise ValueError("Receive times are future or out of order")
                payload = original.get_msgbuf()
                if (
                    type(payload) not in (bytes, bytearray)
                    or not 1 <= len(payload) <= MAX_PACKET_BYTES
                ):
                    raise ValueError("Bounded original packet bytes required")
                payload = bytes(payload)
                attempt = {
                    "record_index": len(self._attempts),
                    "received_at_s": received,
                    "packet_bytes": len(payload),
                    "packet_sha256": _sha(payload),
                    "status": "raw-not-written",
                    "message_type": None,
                    "source_accepted": None,
                }
                header = _raw_json({"contract": CONTRACT, **attempt})
                framed = struct.pack("<I", len(header)) + header + payload
                attempt["raw_record"] = {
                    "path": Path(self._streams["raw"].name).as_posix(),
                    "offset": offset,
                    "bytes": len(framed),
                    "sha256": _sha(framed),
                    "packet_offset": offset + 4 + len(header),
                    "packet_bytes": len(payload),
                    "packet_sha256": _sha(payload),
                }
                self._attempts.append(attempt)
                attempts.append(attempt)
                originals.append(original)
                raw_parts.append(framed)
                offset += len(framed)
                last_received = received
            batch["record_indices"] = [a["record_index"] for a in attempts]
            self._append("raw", b"".join(raw_parts), batch)
            for attempt in attempts:
                attempt["status"] = "raw-committed"
            raw_at = self._now()
            batch["raw_committed_at_s"] = raw_at
            rows, projections = [], []
            offset = self._sizes["projection"]
            for original, attempt in zip(originals, attempts, strict=True):
                # Reject caller mutation across raw fsync; never project a different packet.
                packet_now = original.get_msgbuf()
                if (
                    type(packet_now) not in (bytes, bytearray)
                    or len(packet_now) != attempt["packet_bytes"]
                    or _sha(bytes(packet_now)) != attempt["packet_sha256"]
                ):
                    raise ValueError("Original packet changed after raw commit")
                row, typed = self._projection(original, attempt)
                document = {
                    "encoding": _codec.ENCODING,
                    "row": row,
                    "typed_value": typed,
                    "raw_record": attempt["raw_record"],
                }
                raw = _raw_json(document)
                if len(raw) > MAX_PROJECTION_BYTES:
                    raise ValueError("Projection journal record byte budget exhausted")
                attempt["projection_record"] = {
                    "path": Path(self._streams["projection"].name).as_posix(),
                    "offset": offset,
                    "bytes": len(raw),
                    "sha256": _sha(raw),
                }
                offset += len(raw)
                projections.append(raw)
                rows.append(
                    {
                        **row,
                        "raw_record": attempt["raw_record"],
                        "projection_record": attempt["projection_record"],
                        "raw_committed_at_s": raw_at,
                    }
                )
            self._append("projection", b"".join(projections), batch)
            for attempt in attempts:
                attempt["status"] = "projection-committed-unpublished"
            ready = self._now()
            batch["projection_committed_at_s"] = ready
            batch["durations_s"] = {
                "raw": raw_at - started,
                "projection": ready - raw_at,
                "total": ready - started,
            }
            for row in rows:
                row["projection_committed_at_s"] = row["dispatch_ready_at_s"] = ready
            detached = copy.deepcopy(rows)
            new_latest = dict(self._latest)
            for row in detached:
                if row["source_accepted"]:
                    new_latest[row["message"]["message_type"]] = copy.deepcopy(row)
            new_records = self._records + [
                {
                    **copy.deepcopy(a),
                    "status": "dual-committed",
                    "raw_committed_at_s": raw_at,
                    "projection_committed_at_s": ready,
                }
                for a in attempts
            ]
            self._now()  # no publication if the clock/errors changed during detachment
            _verify_codec()
            for attempt in attempts:
                attempt["status"] = "dual-committed"
            batch["status"] = "dual-committed"
            self._records, self._latest, self._last_received = (
                new_records,
                new_latest,
                last_received,
            )
            return detached
        except BaseException as error:
            self._failure = batch["error"] = _error(error)
            batch["status"] = "refused"
            raise
        finally:
            self._busy = False

    def latest_message(self, kind, *, now_s, maximum_age_s):
        self._open()
        kind = _codec._base._text(kind, "message_type")
        now = require_real(now_s, "now_s", minimum=0)
        age = require_real(maximum_age_s, "maximum_age_s", minimum=0, maximum=60)
        row = self._latest.get(kind)
        if (
            row is None
            or not 0 <= now - row["received_at_s"] <= age
            or row["dispatch_ready_at_s"] > now
        ):
            return None
        return copy.deepcopy(row)

    def _journal_info(self, stage):
        path = Path(self._streams[stage].name)
        before = path.stat()
        if path.is_symlink() or before.st_size > MAX_TOTAL_BYTES:
            raise ValueError("Journal source path/size differs")
        raw = path.read_bytes()
        after = path.stat()
        if _identity(before) != _identity(after):
            raise ValueError("Journal identity changed during source read")
        verified = self._sizes[stage]
        matches = (
            len(raw) >= verified
            and _sha(raw[:verified]) == self._digests[stage].hexdigest()
            and (before.st_dev, before.st_ino) == self._stats[stage][2:]
        )
        return {
            "path": path.as_posix(),
            "bytes": len(raw),
            "sha256": _sha(raw),
            "verified_prefix_bytes": verified,
            "verified_prefix_sha256": self._digests[stage].hexdigest(),
            "verified_prefix_matches": matches,
            "last_verified_file_identity_matches": _identity(before) == self._stats[stage],
            "uncommitted_tail_bytes": max(0, len(raw) - verified),
        }

    def _journals(self, *, require_complete):
        result = {stage: self._journal_info(stage) for stage in self._streams}
        if require_complete and any(
            not value["verified_prefix_matches"]
            or value["uncommitted_tail_bytes"]
            or not value["last_verified_file_identity_matches"]
            for value in result.values()
        ):
            raise ValueError("Journals differ from complete committed evidence")
        return result

    def _read_slice(self, stage, binding):
        stream = self._streams[stage]
        if binding["path"] != Path(stream.name).as_posix():
            raise ValueError("Journal slice path differs")
        offset, size = binding["offset"], binding["bytes"]
        if offset < 0 or size <= 0 or offset + size > self._sizes[stage]:
            raise ValueError("Slice is outside committed journal prefix")
        stream.seek(offset)
        raw = stream.read(size)
        if len(raw) != size or _sha(raw) != binding["sha256"]:
            raise ValueError("Journal record slice bytes/SHA differ")
        return raw

    def _save_new(self, path, raw):
        if self._charged_bytes + len(raw) > MAX_TOTAL_BYTES:
            raise ValueError("Telemetry data byte budget exhausted; no truncation")
        self._charged_bytes += len(raw)
        result = _codec._write_new(path, raw)
        self._durable_bytes += len(raw)
        return result

    def materialize_snapshot(self, kinds, output_directory, *, now_s, maximum_age_s=2):
        """Return exact legacy rows; manifest is a separate source-aware proof.

        This function does NOT qualify skew, typed quality, home, endurance or
        context. Missing/stale messages reject. Even valid export timestamps
        cannot replace the original received_at_s or sender clock.
        """
        self._begin()
        snapshot = {"status": "pending", "files": [], "error": None}
        self._snapshots.append(snapshot)
        try:
            started = self._now()
            now = require_real(now_s, "now_s", minimum=0)
            if now > started:
                raise ValueError("Snapshot now_s is in the future")
            age = require_real(maximum_age_s, "maximum_age_s", minimum=0, maximum=2)
            if (
                type(kinds) not in (list, tuple)
                or not 1 <= len(kinds) <= 16
                or len(set(kinds)) != len(kinds)
            ):
                raise ValueError("Bounded unique snapshot kinds required")
            selected = {
                kind: self.latest_message(kind, now_s=now, maximum_age_s=age) for kind in kinds
            }
            if any(row is None for row in selected.values()):
                raise ValueError("Snapshot message is missing/stale/future; no fallback")
            _verify_codec()
            before = self._journals(require_complete=True)
            directory = Path(output_directory).resolve()
            directory.mkdir(exist_ok=False)
            snapshot["directory"] = directory.as_posix()
            result, proofs = {}, {}
            for kind, row in selected.items():
                raw_record = self._read_slice("raw", row["raw_record"])
                header_size = struct.unpack("<I", raw_record[:4])[0]
                packet = raw_record[4 + header_size :]
                if (
                    len(packet) != row["raw_record"]["packet_bytes"]
                    or _sha(packet) != row["packet_sha256"]
                ):
                    raise ValueError("Selected packet differs from raw journal")
                projection = json.loads(self._read_slice("projection", row["projection_record"]))
                envelope_row = {
                    key: value
                    for key, value in row.items()
                    if key
                    not in (
                        "raw_record",
                        "projection_record",
                        "raw_committed_at_s",
                        "projection_committed_at_s",
                        "dispatch_ready_at_s",
                    )
                }
                if (
                    canonical_json(projection["row"]) != canonical_json(envelope_row)
                    or projection["raw_record"] != row["raw_record"]
                ):
                    raise ValueError("Selected cached projection differs from journal")
                stem = f"receive-{row['record_index']:05d}"
                packet_binding = self._save_new(directory / f"{stem}.bin", packet)
                snapshot["files"].append(packet_binding)
                envelope = self._save_new(directory / f"{stem}.json", _raw_json(envelope_row))
                snapshot["files"].append(envelope)
                result[kind] = {**envelope_row, "packet": packet_binding, "envelope": envelope}
                proofs[kind] = {
                    key: copy.deepcopy(row[key])
                    for key in (
                        "record_index",
                        "received_at_s",
                        "raw_record",
                        "projection_record",
                        "raw_committed_at_s",
                        "projection_committed_at_s",
                    )
                }
            materialized = self._now()
            if any(
                not 0 <= materialized - row["received_at_s"] <= age for row in selected.values()
            ):
                raise ValueError("Snapshot became stale during materialization")
            for row in result.values():
                row["dispatch_ready_at_s"] = materialized
            manifest = {
                "contract": CONTRACT,
                "clock_id": self.clock_id,
                "transaction_id": self.transaction_id,
                "local_origin_id": self.local_origin_id,
                "journal_sources": before,
                "selected": proofs,
                "rows": result,
                "materialized_at_s": materialized,
                "started_at_s": started,
                "received_times_renewed": False,
                "claims": dict(FALSE_CLAIMS),
            }
            binding = self._save_new(directory / "snapshot-manifest.json", _raw_json(manifest))
            snapshot["manifest"] = binding
            detached = copy.deepcopy(result)
            if self._journals(require_complete=True) != before:
                raise ValueError("Journal sources changed during materialization")
            _verify_codec()
            finished = self._now()
            if any(not 0 <= finished - row["received_at_s"] <= age for row in selected.values()):
                raise ValueError("Snapshot became stale during final source checks")
            snapshot.update(status="materialized", finished_at_s=finished)
            return detached
        except BaseException as error:
            self._failure = snapshot["error"] = _error(error)
            snapshot["status"] = "refused"
            raise
        finally:
            self._busy = False

    def _close_streams(self):
        failures = []
        for stream in self._streams.values():
            try:
                stream.close()
            except BaseException as error:
                failures.append(_error(error))
        return failures

    def _finish(self, status, reason):
        self._owner_check()
        if self._closed or self._busy:
            raise RuntimeError("Capture can close only once outside a writer operation")
        self._closed = True
        close_errors = self._close_streams()
        journals, evidence_error = {}, None
        try:
            _verify_codec()
            journals = self._journals(require_complete=status == "recorded")
            for snapshot in self._snapshots:
                for binding in snapshot["files"] + (
                    [snapshot["manifest"]] if "manifest" in snapshot else []
                ):
                    raw = Path(binding["path"]).read_bytes()
                    if len(raw) != binding["bytes"] or _sha(raw) != binding["sha256"]:
                        raise ValueError("Materialized evidence changed before capture close")
        except BaseException as error:
            evidence_error = _error(error)
        failure = evidence_error or ("; ".join(close_errors) if close_errors else None)
        if failure:
            status, reason = "aborted", failure
        receipt = {
            "contract": CONTRACT,
            "encoding": _codec.ENCODING,
            "status": status,
            "error": reason,
            "clock_id": self.clock_id,
            "transaction_id": self.transaction_id,
            "local_origin_id": self.local_origin_id,
            "expected_system_id": self.expected_system_id,
            "expected_component_id": self.expected_component_id,
            "record_count": len(self._records),
            "packet_attempt_count": len(self._attempts),
            "accepted_source_count": sum(r["source_accepted"] is True for r in self._records),
            "foreign_source_count": sum(r["source_accepted"] is False for r in self._records),
            "orphan_packet_count": sum(r["status"] != "dual-committed" for r in self._attempts),
            "data_budget_charged_bytes": self._charged_bytes,
            "verified_durable_data_bytes": self._durable_bytes,
            "payload_bytes_scope": (
                "raw/projection journals plus materialized packets/envelopes/manifests; "
                "final receipt excluded"
            ),
            "commit_counts": copy.deepcopy(self._commits),
            "journal_sources": journals,
            "records": copy.deepcopy(self._records),
            "packet_attempts": copy.deepcopy(self._attempts),
            "batches": copy.deepcopy(self._batches),
            "snapshots": copy.deepcopy(self._snapshots),
            "close_errors": close_errors,
            "evidence_error": evidence_error,
            "raw_commit_before_decoded_access": True,
            "dual_commit_before_cache": True,
            "owns_socket": False,
            "claims": dict(FALSE_CLAIMS),
            "codec_source": {
                "path": _CODEC_PATH.as_posix(),
                "bytes": CODEC_BYTES,
                "sha256": CODEC_SHA256,
            },
        }
        binding = _codec._write_new(self.directory / "capture-receipt.json", _raw_json(receipt))
        if failure:
            raise RuntimeError(f"Capture close refused: {failure}; receipt={binding['path']}")
        return {**receipt, "receipt": binding}

    def seal(self):
        self._open()
        return self._finish("recorded", None)

    def abort(self, reason):
        reason = _codec._base._text(reason, "abort reason")
        return self._finish("aborted", self._failure or reason)
