"""Capture and validate a live field-session JSONL stream.

The field runtime already emits an :class:`~multidetect.audit.AuditLog` stream.
This module deliberately stays on the read-only side of that boundary: it
tails the stream, copies complete JSON objects to a separate evidence file,
and runs ``validate_field_session`` on the captured prefix.  It never opens a
MAVLink connection and never sends a control packet.

Both flat field records and the ``AuditLog`` ``event_type/details`` envelope
are preserved byte-for-byte at the semantic JSON-object level.  A capture
report carries the source hash, filtering decisions, tail errors and the
validator result so a partial or corrupted stream cannot be mistaken for a
passing field gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

from .autonomy.serialization import strict_json_loads
from .field_link_preflight import verify_field_link_report
from .field_session_acceptance import (
    _STATE_ALIASES,
    _extract_state,
    _normalize_record,
    load_jsonl,
    validate_field_session,
)

DEFAULT_POLL_INTERVAL_S = 0.25
DEFAULT_IDLE_TIMEOUT_S = 10.0
_MAX_READ_LINE_BYTES = 32 * 1024 * 1024


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _finite_non_negative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return converted


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _canonical_state(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in _STATE_ALIASES:
        allowed = ", ".join(sorted({"DET", "TRK", "LCK", "TGT"}))
        raise ValueError(f"stop state must resolve to one of {allowed}")
    return _STATE_ALIASES[normalized]


def _record_sequence(record: Mapping[str, object]) -> int | None:
    value = record.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return int(value)


def _record_timestamp(record: Mapping[str, object]) -> float | None:
    for key in ("timestamp_s", "observed_at_s"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        converted = float(value)
        if math.isfinite(converted):
            return converted
    details = record.get("details")
    if isinstance(details, Mapping):
        for key in ("timestamp_s", "observed_at_s"):
            value = details.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            converted = float(value)
            if math.isfinite(converted):
                return converted
    return None


def _sha256_file(path: Path) -> tuple[str | None, int]:
    if not path.exists() or not path.is_file():
        return None, 0
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _json_line(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _open_source(path: Path) -> BinaryIO:
    return path.open("rb")


def _read_available_lines(
    handle: BinaryIO,
    *,
    follow: bool,
    line_number: int,
) -> tuple[list[tuple[int, bytes, int]], int, bool]:
    """Read complete source lines and return ``(lines, next_line, partial)``.

    In follow mode an unterminated final line is left in the file for the
    producer to finish.  In one-shot mode the final line is accepted as a
    normal JSONL record, which matches the validator's ``splitlines`` loader.
    """

    lines: list[tuple[int, bytes, int]] = []
    partial = False
    while True:
        position = handle.tell()
        raw = handle.readline(_MAX_READ_LINE_BYTES + 1)
        if not raw:
            break
        if len(raw) > _MAX_READ_LINE_BYTES:
            # Do not consume the remainder as a potentially valid event.  The
            # caller records a source error and advances to the next newline.
            while raw and not raw.endswith(b"\n"):
                raw = handle.readline(_MAX_READ_LINE_BYTES + 1)
            lines.append(
                (
                    line_number,
                    b"__multidetect_line_too_large__",
                    handle.tell(),
                )
            )
            line_number += 1
            continue
        if not raw.endswith(b"\n"):
            if follow:
                handle.seek(position)
                partial = True
                break
        lines.append((line_number, raw, handle.tell()))
        line_number += 1
    return lines, line_number, partial


def _source_error(line_number: int, reason: str) -> dict[str, object]:
    return {"line": line_number, "reason": reason}


def _sha256_prefix(path: Path, size: int) -> tuple[str | None, int]:
    """Hash the first ``size`` bytes of a source stream.

    A field capture is an append-only prefix even when the producer keeps
    writing after the stop state.  Keeping this digest separate from the
    final whole-file hash prevents later records from changing the identity
    of the evidence that was actually copied.
    """

    if size < 0 or not path.exists() or not path.is_file():
        return None, 0
    digest = hashlib.sha256()
    remaining = size
    read_bytes = 0
    try:
        with path.open("rb") as handle:
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                read_bytes += len(chunk)
                remaining -= len(chunk)
    except OSError:
        return None, read_bytes
    return digest.hexdigest(), read_bytes


def _build_link_preflight_binding(
    link_preflight: str | Path | None,
    *,
    require_link_preflight: bool,
) -> dict[str, Any] | None:
    """Resolve and snapshot an optional link-preflight evidence binding."""

    if type(require_link_preflight) is not bool:
        raise ValueError("require_link_preflight must be a boolean")
    if link_preflight is None and not require_link_preflight:
        return None
    if link_preflight is None:
        return {
            "required": True,
            "path": None,
            "report_sha256": None,
            "report_bytes": 0,
            "hardware_link_ready": False,
            "summary": None,
            "verified": False,
            "checks": {},
            "reasons": ["link_preflight path is required"],
        }
    if not isinstance(link_preflight, (str, Path)):
        raise ValueError("link_preflight must be a path")
    path_text = str(link_preflight).strip()
    if not path_text:
        return {
            "required": True,
            "path": None,
            "report_sha256": None,
            "report_bytes": 0,
            "hardware_link_ready": False,
            "summary": None,
            "verified": False,
            "checks": {},
            "reasons": ["link_preflight path is empty"],
        }
    path = Path(path_text).expanduser().resolve()
    result = verify_field_link_report(path, require_ready=True)
    return {
        "required": True,
        "path": str(path),
        "report_sha256": result.get("report_sha256"),
        "report_bytes": result.get("report_bytes", 0),
        "hardware_link_ready": bool(result.get("hardware_link_ready")),
        "summary": result.get("summary"),
        "verified": bool(result.get("gate_passed")),
        "checks": result.get("checks", {}),
        "reasons": list(result.get("reasons", [])),
    }


def capture_field_session(
    source: str | Path,
    output: str | Path,
    *,
    follow: bool = False,
    session_id: str | None = None,
    start_sequence: int | None = None,
    start_timestamp_s: float | None = None,
    stop_state: str | None = None,
    max_records: int | None = None,
    duration_s: float | None = None,
    idle_timeout_s: float | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    overwrite: bool = False,
    link_preflight: str | Path | None = None,
    require_link_preflight: bool = False,
    require_target_state_evidence: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Copy a field JSONL stream and validate the captured records.

    ``follow=False`` performs one deterministic pass.  ``follow=True`` keeps
    reading appended complete lines until ``stop_state``, ``max_records``,
    ``duration_s`` or ``idle_timeout_s`` fires; callers may also interrupt a
    CLI capture with Ctrl+C.  The destination is flushed and fsynced after
    every accepted record so a stopped process leaves a usable prefix.  When
    ``require_target_state_evidence`` is enabled, the strict validator binds
    qualified GPS and range samples to the current ``TGT`` context and stores
    that choice in the capture report for deterministic re-verification.
    """

    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    if source_path == output_path:
        raise ValueError("source and output must be different files")
    if not source_path.exists() and not follow:
        raise FileNotFoundError(source_path)
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"capture output already exists: {output_path}; pass overwrite=True or --overwrite"
        )
    if session_id is not None:
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id must be non-empty when supplied")
    if start_sequence is not None:
        start_sequence = _positive_integer(start_sequence, "start_sequence")
    if start_timestamp_s is not None:
        start_timestamp_s = _finite_non_negative(start_timestamp_s, "start_timestamp_s")
    if max_records is not None:
        max_records = _positive_integer(max_records, "max_records")
    if duration_s is not None:
        duration_s = _finite_non_negative(duration_s, "duration_s")
        if duration_s == 0.0:
            raise ValueError("duration_s must be greater than zero")
    if idle_timeout_s is not None:
        idle_timeout_s = _finite_non_negative(idle_timeout_s, "idle_timeout_s")
        if idle_timeout_s == 0.0:
            raise ValueError("idle_timeout_s must be greater than zero")
    if not math.isfinite(poll_interval_s) or poll_interval_s <= 0.0:
        raise ValueError("poll_interval_s must be finite and greater than zero")
    canonical_stop_state = _canonical_state(stop_state) if stop_state is not None else None
    link_binding = _build_link_preflight_binding(
        link_preflight,
        require_link_preflight=require_link_preflight,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = clock()
    last_new_record_at = started_at
    records: list[Mapping[str, object]] = []
    source_errors: list[dict[str, object]] = []
    selection_errors: list[dict[str, object]] = []
    filtered_records = 0
    source_records_seen = 0
    source_lines_seen = 0
    source_prefix_bytes_consumed = 0
    captured_line_numbers: list[int] = []
    observed_session_ids: set[str] = set()
    stop_reason = "eof"
    incomplete_tail = False
    source_missing_observed = False
    interrupted = False

    with output_path.open("w", encoding="utf-8", newline="\n") as destination:
        handle: BinaryIO | None = None
        byte_offset = 0
        line_number = 1
        try:
            while True:
                now = clock()
                if duration_s is not None and now - started_at >= duration_s:
                    stop_reason = "duration"
                    break
                if (
                    follow
                    and idle_timeout_s is not None
                    and now - last_new_record_at >= idle_timeout_s
                ):
                    stop_reason = "idle_timeout"
                    break

                if handle is None:
                    if not source_path.exists():
                        source_missing_observed = True
                        if not follow:
                            raise FileNotFoundError(source_path)
                        if idle_timeout_s is None and duration_s is None:
                            # An unbounded follow with a missing source would
                            # spin forever; keep the CLI explicit and fail
                            # closed instead.
                            stop_reason = "source_missing"
                            break
                        sleep(poll_interval_s)
                        continue
                    handle = _open_source(source_path)
                    handle.seek(byte_offset)

                lines, next_line_number, partial = _read_available_lines(
                    handle,
                    follow=follow,
                    line_number=line_number,
                )
                line_number = next_line_number
                source_lines_seen = max(source_lines_seen, line_number - 1)
                incomplete_tail = incomplete_tail or partial
                for line_no, raw_line, end_offset in lines:
                    # ``_read_available_lines`` reads ahead in order to
                    # avoid blocking on a regular file.  Only advance the
                    # evidence prefix for lines actually processed below;
                    # if stop_state fires, later read-ahead lines stay out of
                    # the captured source identity.
                    source_prefix_bytes_consumed = max(
                        source_prefix_bytes_consumed,
                        end_offset,
                    )
                    if raw_line == b"__multidetect_line_too_large__":
                        source_errors.append(_source_error(line_no, "line_too_large"))
                        continue
                    raw = raw_line.rstrip(b"\r\n")
                    if not raw.strip():
                        continue
                    try:
                        # Accept the Windows BOM on the first source line;
                        # ordinary records remain strict UTF-8 afterwards.
                        decoded = raw.decode("utf-8-sig" if line_no == 1 else "utf-8")
                    except UnicodeDecodeError:
                        source_errors.append(_source_error(line_no, "invalid_utf8"))
                        continue
                    try:
                        value = strict_json_loads(decoded, name=f"source line {line_no}")
                    except (json.JSONDecodeError, ValueError):
                        source_errors.append(_source_error(line_no, "invalid_json"))
                        continue
                    if not isinstance(value, Mapping):
                        source_errors.append(_source_error(line_no, "record_not_object"))
                        continue
                    source_records_seen += 1
                    record = dict(value)
                    record_session_id = record.get("session_id")
                    if isinstance(record_session_id, str) and record_session_id:
                        observed_session_ids.add(record_session_id)
                    if session_id is not None and record_session_id != session_id:
                        filtered_records += 1
                        continue
                    sequence = _record_sequence(record)
                    if start_sequence is not None:
                        if sequence is None:
                            selection_errors.append(
                                _source_error(line_no, "sequence_missing_or_invalid")
                            )
                            filtered_records += 1
                            continue
                        if sequence < start_sequence:
                            filtered_records += 1
                            continue
                    if start_timestamp_s is not None:
                        timestamp = _record_timestamp(record)
                        if timestamp is None:
                            selection_errors.append(
                                _source_error(line_no, "timestamp_missing_or_invalid")
                            )
                            filtered_records += 1
                            continue
                        if timestamp < start_timestamp_s:
                            filtered_records += 1
                            continue
                    records.append(record)
                    captured_line_numbers.append(line_no)
                    destination.write(_json_line(record))
                    destination.write("\n")
                    destination.flush()
                    os.fsync(destination.fileno())
                    last_new_record_at = clock()
                    if canonical_stop_state is not None:
                        state = _extract_state(_normalize_record(record))
                        if state == canonical_stop_state:
                            stop_reason = f"stop_state:{canonical_stop_state}"
                            break
                    if max_records is not None and len(records) >= max_records:
                        stop_reason = "max_records"
                        break
                else:
                    if not follow:
                        stop_reason = "eof"
                        break
                    now = clock()
                    if duration_s is not None and now - started_at >= duration_s:
                        stop_reason = "duration"
                        break
                    if idle_timeout_s is not None and now - last_new_record_at >= idle_timeout_s:
                        stop_reason = "idle_timeout"
                        break
                    sleep(poll_interval_s)
                    continue
                break
        except KeyboardInterrupt:
            interrupted = True
            stop_reason = "interrupt"
        finally:
            if handle is not None:
                handle.close()

    ended_at = clock()
    source_sha256, source_bytes = _sha256_file(source_path)
    source_prefix_sha256, source_prefix_bytes = _sha256_prefix(
        source_path,
        source_prefix_bytes_consumed,
    )
    output_sha256, output_bytes = _sha256_file(output_path)
    validation = validate_field_session(
        records,
        require_target_state_evidence=require_target_state_evidence,
    )
    capture_errors = [*source_errors, *selection_errors]
    if incomplete_tail:
        capture_errors.append({"reason": "incomplete_source_tail"})
    report: dict[str, Any] = {
        "schema_version": 1,
        "capture": {
            "source_path": str(source_path),
            "output_path": str(output_path),
            "follow": follow,
            "session_id": session_id,
            "observed_session_ids": sorted(observed_session_ids),
            "start_sequence": start_sequence,
            "start_timestamp_s": start_timestamp_s,
            "stop_state": canonical_stop_state,
            "max_records": max_records,
            "duration_s": duration_s,
            "idle_timeout_s": idle_timeout_s,
            "poll_interval_s": poll_interval_s,
            "started_monotonic_s": started_at,
            "ended_monotonic_s": ended_at,
            "elapsed_s": max(0.0, ended_at - started_at),
            "stop_reason": stop_reason,
            "interrupted": interrupted,
            "source_missing_observed": source_missing_observed,
            "source_lines_seen": source_lines_seen,
            "source_records_seen": source_records_seen,
            "filtered_records": filtered_records,
            "captured_records": len(records),
            "captured_line_numbers": captured_line_numbers,
            "incomplete_source_tail": incomplete_tail,
            "source_errors": source_errors,
            "selection_errors": selection_errors,
            "capture_errors": capture_errors,
            "require_target_state_evidence": bool(require_target_state_evidence),
            "source_sha256": source_sha256,
            "source_bytes": source_bytes,
            "source_hash_scope": "whole_file_at_finalize",
            "source_prefix_sha256": source_prefix_sha256,
            "source_prefix_bytes": source_prefix_bytes,
            "source_prefix_bytes_consumed": source_prefix_bytes_consumed,
            "source_prefix_hash_scope": "bytes_consumed_by_capture",
            "source_prefix_complete": (
                source_prefix_sha256 is not None
                and source_prefix_bytes == source_prefix_bytes_consumed
            ),
            "captured_output_sha256": output_sha256,
            "captured_output_bytes": output_bytes,
        },
        "validation": validation,
    }
    if link_binding is not None:
        report["link_preflight"] = link_binding
    link_gate_passed = link_binding is None or bool(link_binding.get("verified"))
    report["gate_passed"] = (
        not capture_errors and bool(validation["gate_passed"]) and link_gate_passed
    )
    return report


def _resolve_report_path(value: object, *, report_directory: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"capture report {name} is missing or empty")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = report_directory / path
    return path.resolve()


def _legacy_validation_view(validation: Mapping[str, object]) -> dict[str, object]:
    """Drop fields added by the strict target-state gate for old reports.

    Capture reports written before the target-state gate do not contain the
    ``state_context`` sensor provenance or the top-level
    ``target_state_evidence`` block.  Keep those reports verifiable when the
    persisted capture did not request the new option; newly written reports
    still use an exact validation comparison below.
    """

    view = dict(validation)
    view.pop("target_state_evidence", None)
    for source_name in ("gps", "range"):
        source = view.get(source_name)
        if not isinstance(source, Mapping):
            continue
        source_view = dict(source)
        results = source_view.get("results")
        if isinstance(results, list):
            source_view["results"] = [
                {key: value for key, value in item.items() if key != "state_context"}
                if isinstance(item, Mapping)
                else item
                for item in results
            ]
        view[source_name] = source_view
    return view


def verify_field_session_capture(report: str | Path) -> dict[str, Any]:
    """Verify a previously written capture report without opening MAVLink.

    The source file is allowed to grow after capture.  Verification therefore
    binds the consumed source prefix and the copied output, while exposing a
    non-failing ``source_changed_after_capture`` diagnostic for later lines.
    The original report's gate is compared with a fresh validation of the
    copied JSONL; any missing or inconsistent provenance closes the result.
    """

    report_path = Path(report).expanduser().resolve()
    reasons: list[str] = []
    checks: dict[str, Any] = {}
    try:
        raw = strict_json_loads(
            report_path.read_text(encoding="utf-8-sig"),
            name="capture report",
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return {
            "schema_version": 1,
            "report_path": str(report_path),
            "checks": checks,
            "reasons": [f"capture report cannot be loaded: {exc}"],
            "gate_passed": False,
        }
    if not isinstance(raw, Mapping):
        return {
            "schema_version": 1,
            "report_path": str(report_path),
            "checks": checks,
            "reasons": ["capture report must contain a JSON object"],
            "gate_passed": False,
        }
    if raw.get("schema_version") != 1:
        reasons.append("unsupported capture report schema_version")
    capture = raw.get("capture")
    validation = raw.get("validation")
    if not isinstance(capture, Mapping):
        reasons.append("capture report capture field must be an object")
        capture = {}
    if not isinstance(validation, Mapping):
        reasons.append("capture report validation field must be an object")
        validation = {}

    try:
        source_path = _resolve_report_path(
            capture.get("source_path"),
            report_directory=report_path.parent,
            name="source_path",
        )
    except ValueError as exc:
        source_path = None
        reasons.append(str(exc))
    try:
        output_path = _resolve_report_path(
            capture.get("output_path"),
            report_directory=report_path.parent,
            name="output_path",
        )
    except ValueError as exc:
        output_path = None
        reasons.append(str(exc))

    expected_output_sha = capture.get("captured_output_sha256")
    expected_output_bytes = capture.get("captured_output_bytes")
    output_sha, output_bytes = (None, 0)
    if output_path is None:
        checks["output"] = {"available": False}
    else:
        output_sha, output_bytes = _sha256_file(output_path)
        output_check = {
            "path": str(output_path),
            "available": output_sha is not None,
            "sha256": output_sha,
            "bytes": output_bytes,
            "expected_sha256": expected_output_sha,
            "expected_bytes": expected_output_bytes,
        }
        checks["output"] = output_check
        if not isinstance(expected_output_sha, str) or not _is_sha256(expected_output_sha):
            reasons.append("captured_output_sha256 is missing or invalid")
        elif output_sha is None or output_sha.lower() != expected_output_sha.lower():
            reasons.append("captured output SHA-256 does not match")
        if (
            isinstance(expected_output_bytes, bool)
            or not isinstance(expected_output_bytes, int)
            or expected_output_bytes < 0
        ):
            reasons.append("captured_output_bytes is missing or invalid")
        elif output_sha is not None and output_bytes != expected_output_bytes:
            reasons.append("captured output byte count does not match")

    expected_prefix_sha = capture.get("source_prefix_sha256")
    expected_prefix_bytes = capture.get("source_prefix_bytes_consumed")
    prefix_sha, prefix_bytes = (None, 0)
    source_sha, source_bytes = (None, 0)
    if source_path is None:
        checks["source"] = {"available": False}
    else:
        source_sha, source_bytes = _sha256_file(source_path)
        if (
            isinstance(expected_prefix_bytes, bool)
            or not isinstance(expected_prefix_bytes, int)
            or expected_prefix_bytes < 0
        ):
            reasons.append("source_prefix_bytes_consumed is missing or invalid")
            expected_prefix_bytes = 0
        prefix_sha, prefix_bytes = _sha256_prefix(source_path, expected_prefix_bytes)
        source_check = {
            "path": str(source_path),
            "available": source_sha is not None,
            "sha256": source_sha,
            "bytes": source_bytes,
            "expected_sha256": capture.get("source_sha256"),
            "prefix_sha256": prefix_sha,
            "prefix_bytes": prefix_bytes,
            "expected_prefix_sha256": expected_prefix_sha,
            "expected_prefix_bytes": expected_prefix_bytes,
            "source_changed_after_capture": (
                isinstance(capture.get("source_sha256"), str)
                and source_sha is not None
                and source_sha.lower() != capture["source_sha256"].lower()
            ),
        }
        checks["source"] = source_check
        if not isinstance(expected_prefix_sha, str) or not _is_sha256(expected_prefix_sha):
            reasons.append("source_prefix_sha256 is missing or invalid")
        elif (
            prefix_sha is None
            or prefix_bytes != expected_prefix_bytes
            or prefix_sha.lower() != expected_prefix_sha.lower()
        ):
            reasons.append("source consumed-prefix SHA-256 does not match")
        expected_prefix_available = capture.get("source_prefix_bytes")
        if (
            isinstance(expected_prefix_available, bool)
            or not isinstance(expected_prefix_available, int)
            or expected_prefix_available < 0
        ):
            reasons.append("source_prefix_bytes is missing or invalid")
        elif prefix_bytes != expected_prefix_available:
            reasons.append("source consumed-prefix byte count does not match")
        if capture.get("source_prefix_complete") is not True:
            reasons.append("source consumed-prefix was not complete at capture time")
        if source_sha is None:
            reasons.append("source file is unavailable for prefix verification")

    fresh_validation: dict[str, Any] | None = None
    if output_path is not None and output_sha is not None:
        try:
            require_target_state_evidence = capture.get(
                "require_target_state_evidence",
                False,
            )
            if not isinstance(require_target_state_evidence, bool):
                reasons.append("capture require_target_state_evidence must be boolean")
                require_target_state_evidence = False
            fresh_validation = validate_field_session(
                load_jsonl(output_path),
                require_target_state_evidence=require_target_state_evidence,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            reasons.append(f"captured output cannot be validated: {exc}")
        else:
            if (
                "target_state_evidence" not in validation
                and "require_target_state_evidence" not in capture
            ):
                # Reports produced before pre.8 strict TGT evidence remain
                # readable and verifiable under their legacy session-wide
                # sensor semantics.
                checks["validation_match"] = _legacy_validation_view(fresh_validation) == dict(
                    validation
                )
            else:
                checks["validation_match"] = dict(fresh_validation) == dict(validation)
            if not checks["validation_match"]:
                reasons.append("capture report validation does not match output JSONL")
    else:
        checks["validation_match"] = False
        reasons.append("captured output is unavailable for validation")

    # A capture can optionally bind to the exact read-only field-link
    # preflight used to bring up the aircraft.  Legacy reports have no such
    # field and retain their original offline semantics; once the field is
    # present, its path, bytes, SHA-256 and readiness summary are all checked
    # again so a later report mutation cannot turn an offline/failed link into
    # accepted evidence.
    link_binding = raw.get("link_preflight")
    link_gate_passed = True
    if link_binding is not None:
        if not isinstance(link_binding, Mapping):
            reasons.append("link_preflight must be an object")
            checks["link_preflight"] = {"available": False}
            link_gate_passed = False
        else:
            required = link_binding.get("required")
            if required is not True:
                reasons.append("link_preflight required must be true")
            try:
                link_path = _resolve_report_path(
                    link_binding.get("path"),
                    report_directory=report_path.parent,
                    name="link_preflight.path",
                )
            except ValueError as exc:
                link_path = None
                reasons.append(str(exc))
            expected_link_sha = link_binding.get("report_sha256")
            expected_link_bytes = link_binding.get("report_bytes")
            link_result: dict[str, Any] | None = None
            if link_path is not None:
                link_result = verify_field_link_report(
                    link_path,
                    require_ready=True,
                )
            current_link_sha = link_result.get("report_sha256") if link_result else None
            current_link_bytes = link_result.get("report_bytes", 0) if link_result else 0
            checks["link_preflight"] = {
                "path": str(link_path) if link_path is not None else None,
                "expected_sha256": expected_link_sha,
                "sha256": current_link_sha,
                "expected_bytes": expected_link_bytes,
                "bytes": current_link_bytes,
                "verified": bool(link_result and link_result.get("gate_passed")),
                "hardware_link_ready": bool(link_result and link_result.get("hardware_link_ready")),
            }
            if not isinstance(expected_link_sha, str) or not _is_sha256(expected_link_sha):
                reasons.append("link_preflight report_sha256 is missing or invalid")
            elif current_link_sha is None or current_link_sha.lower() != expected_link_sha.lower():
                reasons.append("link_preflight report SHA-256 does not match")
            if (
                isinstance(expected_link_bytes, bool)
                or not isinstance(expected_link_bytes, int)
                or expected_link_bytes < 0
            ):
                reasons.append("link_preflight report_bytes is missing or invalid")
            elif current_link_sha is not None and current_link_bytes != expected_link_bytes:
                reasons.append("link_preflight report byte count does not match")
            if link_result is None:
                link_gate_passed = False
            else:
                link_gate_passed = bool(link_result.get("gate_passed"))
                for reason in link_result.get("reasons", []):
                    reasons.append(f"link_preflight: {reason}")
                expected_ready = link_binding.get("hardware_link_ready")
                if type(expected_ready) is not bool:
                    reasons.append("link_preflight hardware_link_ready is missing or invalid")
                elif expected_ready != bool(link_result.get("hardware_link_ready")):
                    reasons.append("link_preflight hardware_link_ready does not match")
                expected_summary = link_binding.get("summary")
                if expected_summary != link_result.get("summary"):
                    reasons.append("link_preflight summary does not match")
                expected_verified = link_binding.get("verified")
                if type(expected_verified) is not bool:
                    reasons.append("link_preflight verified is missing or invalid")
                elif expected_verified != bool(link_result.get("gate_passed")):
                    reasons.append("link_preflight verified does not match")

    report_gate = raw.get("gate_passed")
    if type(report_gate) is not bool:
        reasons.append("capture report gate_passed must be a boolean")
        report_gate = False
    capture_errors = capture.get("capture_errors")
    if not isinstance(capture_errors, list):
        reasons.append("capture_errors must be a list")
        capture_errors = []
    fresh_gate = bool(fresh_validation and fresh_validation.get("gate_passed"))
    derived_gate = not capture_errors and fresh_gate and link_gate_passed
    checks["derived_gate_passed"] = derived_gate
    if report_gate is not derived_gate:
        reasons.append("capture report gate_passed does not match derived gate")
    checks["report_gate_passed"] = report_gate
    return {
        "schema_version": 1,
        "report_path": str(report_path),
        "checks": checks,
        "reasons": reasons,
        "gate_passed": not reasons and report_gate is True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="live AuditLog/field JSONL source")
    parser.add_argument("--output", type=Path, required=True, help="captured session JSONL")
    parser.add_argument(
        "--report-out",
        type=Path,
        help="capture report JSON (default: <output>.capture.json)",
    )
    parser.add_argument("--follow", action="store_true", help="tail appended records")
    parser.add_argument("--session-id", help="copy only records with this AuditLog session_id")
    parser.add_argument("--start-sequence", type=int)
    parser.add_argument("--start-timestamp-s", type=float)
    parser.add_argument(
        "--stop-state",
        help="stop after the first state (DET/TRK/LCK/TGT) or alias is captured",
    )
    parser.add_argument("--max-records", type=int)
    parser.add_argument("--duration-s", type=float)
    parser.add_argument(
        "--idle-timeout-s",
        type=float,
        default=None,
        help="in follow mode, stop after this long without a new complete line",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=DEFAULT_POLL_INTERVAL_S,
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--require-gate",
        action="store_true",
        help="return exit code 1 unless capture and field validation both pass",
    )
    parser.add_argument(
        "--link-preflight",
        type=Path,
        help=(
            "bind this capture to a saved field-link preflight; the report must be "
            "hash-stable and have hardware_link_ready=true"
        ),
    )
    parser.add_argument(
        "--require-link-preflight",
        action="store_true",
        help="require --link-preflight and keep the capture gate closed without it",
    )
    parser.add_argument(
        "--require-target-state-evidence",
        action="store_true",
        help=(
            "require fresh, qualified GPS and RANGE_STATUS samples while the "
            "active state context is TGT"
        ),
    )
    return parser


def _default_report_path(output: Path) -> Path:
    return output.with_suffix(f"{output.suffix}.capture.json")


def verify_main(argv: list[str] | None = None) -> int:
    """CLI entry point for capture-report provenance verification."""

    parser = argparse.ArgumentParser(description=verify_field_session_capture.__doc__)
    parser.add_argument("report", type=Path, help="capture report JSON")
    parser.add_argument(
        "--require-gate",
        action="store_true",
        help="return exit code 1 unless provenance and validation both pass",
    )
    args = parser.parse_args(argv)
    result = verify_field_session_capture(args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["gate_passed"] or not args.require_gate else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = capture_field_session(
            args.source,
            args.output,
            follow=args.follow,
            session_id=args.session_id,
            start_sequence=args.start_sequence,
            start_timestamp_s=args.start_timestamp_s,
            stop_state=args.stop_state,
            max_records=args.max_records,
            duration_s=args.duration_s,
            idle_timeout_s=args.idle_timeout_s,
            poll_interval_s=args.poll_interval_s,
            overwrite=args.overwrite,
            link_preflight=args.link_preflight,
            require_link_preflight=args.require_link_preflight,
            require_target_state_evidence=args.require_target_state_evidence,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        raise SystemExit(f"field session capture failed: {exc}") from exc
    report_path = args.report_out or _default_report_path(args.output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    capture = report["capture"]
    print(
        f"captured_records={capture['captured_records']} "
        f"stop_reason={capture['stop_reason']} "
        f"gate_passed={report['gate_passed']}"
    )
    print(f"session_jsonl={args.output}")
    print(f"capture_report={report_path}")
    return 0 if (report["gate_passed"] or not args.require_gate) else 1


__all__ = [
    "capture_field_session",
    "verify_field_session_capture",
    "verify_main",
    "build_parser",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
