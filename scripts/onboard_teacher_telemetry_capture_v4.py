"""NEW finite lifecycle writer using unchanged frozen V3 wire/dual-commit codec.

The resource version is V4, not an increased legacy V3 promise. Exact NEW
transport admission is required; no old typed gate or module constant changes.
"""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

from scripts import onboard_teacher_telemetry_capture_v3 as frozen
from scripts.onboard_teacher_sortie_budget_v1 import TeacherSortieLifecycleBudget

CONTRACT = "multidetect.onboard-teacher-lifecycle-telemetry-v4"


class TeacherTelemetryCaptureV4(frozen.TeacherTelemetryCaptureV3):
    def __init__(self, directory, *, budget, clock, clock_id, **identity):
        if type(budget) is not TeacherSortieLifecycleBudget:
            raise TypeError("Exact finite lifecycle budget required")
        if clock_id != budget.clock_id or not callable(clock):
            raise ValueError("Writer must use the once-bound actual lifecycle clock")
        if "max_records" in identity:
            raise ValueError("NEW resource ceiling comes only from the original bound budget")
        self._budget = budget
        self._budget_sha256 = budget.canonical_hash
        self._bound_clock = clock
        self._dual_started_at = None
        self._rate_previous_s = budget.started_at_s
        self._rate_tokens = float(budget.protocol_packet_reserve)
        budget.assert_phase("rtl_land", now_s=clock(), clock_id=clock_id)
        # Only bootstrap file/header/codec creation is reused. The NEW V4
        # contract explicitly owns its separate immutable record/data limits.
        super().__init__(directory, clock=clock, clock_id=clock_id, max_records=8192, **identity)
        self.max_records = budget.record_limit

    @property
    def budget(self):
        return self._budget

    def _now(self):
        if (
            self.budget.canonical_hash != self._budget_sha256
            or self._clock != self._bound_clock
            or self.max_records != self.budget.record_limit
        ):
            raise ValueError("Original lifecycle writer budget/clock/record binding changed")
        now = super()._now()
        if self.budget.remaining_s(now, clock_id=self.clock_id) <= 0:
            raise ValueError("Original finite lifecycle deadline exhausted")
        if (
            self._dual_started_at is not None
            and now - self._dual_started_at > self.budget.maximum_dual_commit_s
        ):
            raise ValueError("Declared dual-commit latency exceeded before cache publication")
        if self._dual_started_at is not None and self._batches:
            batch = self._batches[-1]
            if "projection_committed_at_s" in batch:
                for index in batch["record_indices"]:
                    attempt = self._attempts[index]
                    record = {
                        **attempt,
                        "status": "dual-committed",
                        "raw_committed_at_s": batch["raw_committed_at_s"],
                        "projection_committed_at_s": batch["projection_committed_at_s"],
                    }
                    # Charge the complete batch metadata per packet: a safe
                    # overestimate, never an unbounded final-receipt surprise.
                    if (
                        len(frozen._raw_json(attempt))
                        + len(frozen._raw_json(record))
                        + len(frozen._raw_json(batch))
                        > self.budget.maximum_receipt_record_bytes
                    ):
                        raise ValueError("Final receipt record bound exceeded before publication")
        return now

    def append_batch(self, samples):
        self._open()
        now = self._now()
        self.budget.assert_phase("rtl_land", now_s=now, clock_id=self.clock_id)
        if type(samples) not in (list, tuple) or not 1 <= len(samples) <= frozen.MAX_BATCH:
            raise ValueError("Original bounded batch required")
        # Bounded token bucket at original receive times, not batch drain time.
        # No sender getter/decode happens before raw commit. Idle time cannot
        # accumulate an unlimited burst. This is enforced, not a tested rate.
        try:
            for pair in samples:
                if type(pair) not in (list, tuple) or len(pair) != 2:
                    raise ValueError("Original message/receive-time pairs required")
                received = frozen.require_real(
                    pair[1], "received time", minimum=self._rate_previous_s, maximum=now
                )
                self._rate_tokens = min(
                    self.budget.protocol_packet_reserve,
                    self._rate_tokens
                    + (received - self._rate_previous_s) * self.budget.packet_rate_ceiling_hz,
                )
                if self._rate_tokens < 1:
                    raise ValueError("Original lifecycle packet-rate/burst ceiling exceeded")
                self._rate_tokens -= 1
                self._rate_previous_s = received
        except BaseException as error:
            self._failure = frozen._error(error)
            raise
        self._dual_started_at = now
        try:
            return super().append_batch(samples)
        finally:
            self._dual_started_at = None

    def _projection(self, original, attempt):
        row, typed = super()._projection(original, attempt)
        raw = frozen._raw_json(
            {
                "encoding": frozen._codec.ENCODING,
                "row": row,
                "typed_value": typed,
                "raw_record": attempt["raw_record"],
            }
        )
        if len(raw) > self.budget.maximum_projection_record_bytes:
            raise ValueError("Declared per-record projection bound exceeded; no publication")
        return row, typed

    def _append(self, stage, raw, batch):
        # Same original raw-first/fsync/readback/identity algorithm, with the
        # NEW explicit total and per-record resource ceilings.
        stream = self._streams[stage]
        path = Path(stream.name)
        before = path.stat()
        if path.is_symlink() or frozen._identity(before) != self._stats[stage]:
            raise ValueError("Journal identity changed before append")
        if stage == "raw" and any(
            self._attempts[i]["raw_record"]["bytes"] > self.budget.maximum_raw_record_bytes
            for i in batch["record_indices"]
        ):
            raise ValueError("Declared per-record raw bound exceeded before write")
        if self._charged_bytes + len(raw) > self.budget.telemetry_byte_limit:
            raise ValueError("NEW finite telemetry byte resource exhausted")
        offset = self._sizes[stage]
        expected = {
            "path": path.as_posix(),
            "offset": offset,
            "bytes": len(raw),
            "sha256": frozen._sha(raw),
        }
        batch[stage] = {"slice": expected, "fsync_returned": False, "verified": False}
        counter = self._commits[stage]
        counter["attempted"] += 1
        self._charged_bytes += len(raw)
        stream.seek(offset)
        if stream.write(raw) != len(raw):
            raise OSError(f"Short {stage} journal write")
        stream.flush()
        os.fsync(stream.fileno())
        counter["fsync_returned"] += 1
        batch[stage]["fsync_returned"] = True
        after_write = path.stat()
        if (after_write.st_dev, after_write.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("Journal identity changed during append")
        stream.seek(offset)
        checked = stream.read(len(raw) + 1)
        after_read = path.stat()
        if (
            checked != raw
            or after_read.st_size != offset + len(raw)
            or frozen._identity(after_write) != frozen._identity(after_read)
        ):
            raise ValueError("Journal bytes/identity differ after fsync")
        self._sizes[stage] += len(raw)
        self._digests[stage].update(raw)
        self._stats[stage] = frozen._identity(after_read)
        self._durable_bytes += len(raw)
        counter["verified"] += 1
        batch[stage]["verified"] = True
        return expected

    def _save_new(self, path, raw):
        self._now()
        snapshot_charged = getattr(self, "_snapshot_charged", 0)
        if snapshot_charged + len(raw) > self.budget.snapshot_bytes:
            raise ValueError("NEW finite snapshot allocation exhausted")
        if self._charged_bytes + len(raw) > self.budget.telemetry_byte_limit:
            raise ValueError("NEW finite snapshot/telemetry byte budget exhausted")
        self._charged_bytes += len(raw)
        self._snapshot_charged = snapshot_charged + len(raw)
        result = frozen._codec._write_new(path, raw)
        self._durable_bytes += len(raw)
        return result

    def _journal_info(self, stage):
        path = Path(self._streams[stage].name)
        before = path.stat()
        if path.is_symlink() or before.st_size > self.budget.telemetry_byte_limit:
            raise ValueError("NEW journal source path/size differs")
        full, prefix = hashlib.sha256(), hashlib.sha256()
        observed, verified = 0, self._sizes[stage]
        with path.open("rb") as stream:
            while raw := stream.read(1024 * 1024):
                full.update(raw)
                prefix.update(raw[: max(0, min(len(raw), verified - observed))])
                observed += len(raw)
        after = path.stat()
        if frozen._identity(before) != frozen._identity(after) or observed != before.st_size:
            raise ValueError("Journal identity changed during bounded source read")
        return {
            "path": path.as_posix(),
            "bytes": observed,
            "sha256": full.hexdigest(),
            "verified_prefix_bytes": verified,
            "verified_prefix_sha256": self._digests[stage].hexdigest(),
            "verified_prefix_matches": observed >= verified
            and prefix.hexdigest() == self._digests[stage].hexdigest()
            and (before.st_dev, before.st_ino) == self._stats[stage][2:],
            "last_verified_file_identity_matches": frozen._identity(before) == self._stats[stage],
            "uncommitted_tail_bytes": max(0, observed - verified),
        }

    def lifecycle_mapping(self):
        return {
            "contract": CONTRACT,
            "journal_wire_contract": frozen.CONTRACT,
            "budget": self.budget.to_mapping(),
            "budget_sha256": self.budget.canonical_hash,
            "record_limit": self.max_records,
            "telemetry_byte_limit": self.budget.telemetry_byte_limit,
            "final_receipt_byte_limit": self.budget.final_receipt_byte_limit,
            "records_attempted": len(self._attempts),
            "bytes_charged": self._charged_bytes,
            "claims": {
                "rate_measured": False,
                "flight_authorized": False,
                "flight_executed": False,
            },
        }

    def _finish(self, status, reason):
        self._owner_check()
        if self._closed or self._busy:
            raise RuntimeError("Capture can close only once outside a writer operation")
        self._closed = True
        close_errors = self._close_streams()
        journals, evidence_error = {}, None
        try:
            frozen._verify_codec()
            journals = self._journals(require_complete=status == "recorded")
            for snapshot in self._snapshots:
                for binding in snapshot["files"] + (
                    [snapshot["manifest"]] if "manifest" in snapshot else []
                ):
                    path = Path(binding["path"])
                    if path.is_symlink() or path.stat().st_size != binding["bytes"]:
                        raise ValueError("Materialized snapshot source path/size changed")
                    raw = path.read_bytes()
                    if len(raw) != binding["bytes"] or frozen._sha(raw) != binding["sha256"]:
                        raise ValueError("Materialized evidence changed before capture close")
        except BaseException as error:
            evidence_error = frozen._error(error)
        failure = evidence_error or ("; ".join(close_errors) if close_errors else None)
        if failure:
            status, reason = "aborted", failure
        receipt = {
            "contract": CONTRACT,
            "journal_wire_contract": frozen.CONTRACT,
            "encoding": frozen._codec.ENCODING,
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
            "final_receipt_in_lifecycle_budget": True,
            "resource_budget": self.budget.to_mapping(),
            "resource_budget_sha256": self._budget_sha256,
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
            "claims": dict(frozen.FALSE_CLAIMS),
            "codec_source": {
                "path": frozen._CODEC_PATH.as_posix(),
                "bytes": frozen.CODEC_BYTES,
                "sha256": frozen.CODEC_SHA256,
            },
        }
        raw = frozen._raw_json(receipt)
        if len(raw) > self.budget.final_receipt_byte_limit:
            raise RuntimeError("Capture already closed: bounded final receipt allocation exceeded")
        binding = frozen._codec._write_new(self.directory / "capture-receipt.json", raw)
        if failure:
            raise RuntimeError(f"Capture close refused: {failure}; receipt={binding['path']}")
        return {**receipt, "receipt": binding}


__all__ = ["TeacherTelemetryCaptureV4"]
