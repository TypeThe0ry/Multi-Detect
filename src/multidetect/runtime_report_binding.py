"""Bind a runtime result document to its canonical ``RuntimeRunReport``.

Runtime reports are now emitted by the integrated Replay/QGC and SIM runners,
but a sidecar is only useful to a reviewer when its identity is tied back to
the result that produced it.  This module is the small, read-only consumer for
that boundary.  It reopens both files, rejects ambiguous JSON, verifies the
persisted report hash, and checks the mode-specific completion contract without
starting a runtime or touching a vehicle.

The verifier intentionally understands the result shapes already emitted by
``integrated-replay``, ``integrated-replay-qgc`` and
``simulator.scenario_runner``.  Unknown result events remain fail-closed until
a mode-specific adapter is added, so a future producer cannot silently inherit
the wrong completion semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .autonomy.serialization import (
    AutonomyContractError,
    require_sha256,
    strict_json_loads,
)
from .runtime import RuntimeRunReport

SCHEMA = "multidetect.runtime-report-binding"
SCHEMA_VERSION = 1
EVENT = "runtime_report_binding_verified"
MAX_RESULT_BYTES = 256 * 1024 * 1024
MAX_REPORT_BYTES = 4 * 1024 * 1024

_INTEGRATED_EVENTS = frozenset({"integrated_replay_finished", "integrated_replay_qgc_finished"})
_CHECK_NAMES = (
    "result_json_valid",
    "runtime_report_valid",
    "result_event_supported",
    "result_gate_passed",
    "runtime_mode_matches",
    "descriptor_hash_matches",
    "input_hash_matches",
    "processed_frame_count_matches",
    "processed_frame_ids_match",
    "termination_contract_matches",
    "embedded_report_matches",
    "sim_truth_free",
    "sim_closed",
)


class RuntimeReportBindingError(ValueError):
    """Raised when a runtime result/report pair cannot be inspected safely."""


def _read_regular_file(path: str | Path, *, name: str, maximum_bytes: int) -> tuple[Path, bytes]:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise RuntimeReportBindingError(f"{name} must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeReportBindingError(f"{name} cannot be resolved: {exc}") from exc
    if not resolved.is_file():
        raise RuntimeReportBindingError(f"{name} must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise RuntimeReportBindingError(f"{name} cannot be read: {exc}") from exc
    if not payload:
        raise RuntimeReportBindingError(f"{name} is empty")
    if len(payload) > maximum_bytes:
        raise RuntimeReportBindingError(f"{name} exceeds {maximum_bytes} bytes")
    return resolved, payload


def _load_result(path: str | Path) -> tuple[Path, bytes, Mapping[str, Any]]:
    resolved, payload = _read_regular_file(
        path,
        name="runtime result",
        maximum_bytes=MAX_RESULT_BYTES,
    )
    try:
        value = strict_json_loads(payload, name="runtime result")
    except (AutonomyContractError, TypeError) as exc:
        raise RuntimeReportBindingError(str(exc)) from exc
    if not isinstance(value, Mapping):
        raise RuntimeReportBindingError("runtime result must contain a JSON object")
    return resolved, payload, value


def _load_report(path: str | Path) -> tuple[Path, bytes, RuntimeRunReport]:
    resolved, payload = _read_regular_file(
        path,
        name="RuntimeRunReport",
        maximum_bytes=MAX_REPORT_BYTES,
    )
    try:
        value = strict_json_loads(payload, name="RuntimeRunReport")
        report = RuntimeRunReport.from_mapping(value)
    except (AutonomyContractError, TypeError, ValueError) as exc:
        raise RuntimeReportBindingError(str(exc)) from exc
    return resolved, payload, report


def _normalise_mode(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalised = value.strip().lower()
    return normalised or None


def _hash_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return require_sha256(value, "hash")
    except AutonomyContractError:
        return None


def _explicit_or_derived_hash(value: object, derived: object) -> str | None:
    """Use an explicit expectation when present, preserving invalid input."""

    if value is not None:
        return _hash_or_none(value)
    return derived if isinstance(derived, str) else None


def _as_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _result_contract(
    result: Mapping[str, Any],
) -> dict[str, object]:
    """Derive identity and termination expectations from a known result shape."""

    event = result.get("event")
    if event in _INTEGRATED_EVENTS:
        nested = result.get("result")
        nested_result = nested if isinstance(nested, Mapping) else {}
        return {
            "supported": True,
            "runtime_mode": "replay",
            "descriptor_hash": _hash_or_none(result.get("runtime_descriptor_hash")),
            "input_hash": _hash_or_none(result.get("recording_hash")),
            "processed_count": _as_nonnegative_int(nested_result.get("processed_frames")),
            "processed_ids": None,
            "stream_exhausted": True,
            "max_frames": None,
            "embedded_required": True,
            "truth_free": result.get("ground_truth_isolated") is True,
            "closed": True,
        }

    if event == "simulator_scenario_run_finished":
        scenario_id = result.get("scenario_id")
        completed_frames = _as_nonnegative_int(result.get("completed_frames"))
        requested_frames = _as_nonnegative_int(result.get("requested_frames"))
        processed_ids: tuple[str, ...] | None = None
        if isinstance(scenario_id, str) and scenario_id.strip() and completed_frames is not None:
            processed_ids = tuple(f"sim:{scenario_id}:{index}" for index in range(completed_frames))
        return {
            "supported": True,
            "runtime_mode": "sim",
            "descriptor_hash": _hash_or_none(result.get("runtime_descriptor_hash")),
            "input_hash": _hash_or_none(
                result.get("runtime_input_hash") or result.get("scenario_manifest_hash")
            ),
            "processed_count": completed_frames,
            "processed_ids": processed_ids,
            "stream_exhausted": False,
            "max_frames": requested_frames,
            "embedded_required": False,
            "truth_free": result.get("truth_free") is True,
            "closed": result.get("closed") is True,
        }

    # Keep unknown result events fail-closed.  The fallback fields are retained
    # only for diagnostics; no completion contract is inferred without a
    # mode-specific adapter.
    nested = result.get("result")
    nested_result = nested if isinstance(nested, Mapping) else {}
    candidate_ids = result.get("processed_frame_ids")
    if candidate_ids is None:
        candidate_ids = nested_result.get("processed_frame_ids")
    processed_ids = (
        tuple(item for item in candidate_ids if isinstance(item, str))
        if isinstance(candidate_ids, Sequence) and not isinstance(candidate_ids, (str, bytes))
        else None
    )
    count = result.get("processed_frame_count")
    if count is None:
        count = nested_result.get("processed_frames")
    return {
        "supported": False,
        "runtime_mode": _normalise_mode(result.get("runtime_mode"))
        or _normalise_mode(result.get("mode")),
        "descriptor_hash": _hash_or_none(
            result.get("runtime_descriptor_hash") or result.get("descriptor_hash")
        ),
        "input_hash": _hash_or_none(
            result.get("runtime_input_hash")
            or result.get("input_hash")
            or result.get("recording_hash")
            or result.get("scenario_manifest_hash")
        ),
        "processed_count": _as_nonnegative_int(count),
        "processed_ids": processed_ids,
        "stream_exhausted": None,
        "max_frames": None,
        "embedded_required": False,
        "truth_free": True,
        "closed": True,
    }


def _embedded_report_matches(
    result: Mapping[str, Any],
    report: RuntimeRunReport,
    *,
    required: bool,
) -> tuple[bool, bool]:
    raw_embedded = result.get("runtime_run_report")
    if raw_embedded is None:
        return (not required, False)
    try:
        embedded = RuntimeRunReport.from_mapping(raw_embedded)
    except (AutonomyContractError, TypeError, ValueError):
        return False, True
    return embedded.to_mapping_with_hash() == report.to_mapping_with_hash(), True


def verify_runtime_report_binding(
    result_path: str | Path,
    runtime_report_path: str | Path,
    *,
    expected_runtime_mode: str | None = None,
    expected_descriptor_hash: str | None = None,
    expected_input_hash: str | None = None,
    require_embedded_report: bool = False,
    require_complete: bool = False,
) -> dict[str, Any]:
    """Verify one result document and its persisted ``RuntimeRunReport``.

    The returned mapping is deterministic for fixed input bytes and explicit
    expectations.  It is a diagnostic evidence document; it never mutates
    either input and never claims hardware or flight acceptance.
    """

    result_file, result_raw, result = _load_result(result_path)
    report_file, report_raw, report = _load_report(runtime_report_path)
    contract = _result_contract(result)
    event = result.get("event")

    expected_mode = _normalise_mode(expected_runtime_mode) or contract["runtime_mode"]
    descriptor_expected = _explicit_or_derived_hash(
        expected_descriptor_hash,
        contract["descriptor_hash"],
    )
    input_expected = _explicit_or_derived_hash(expected_input_hash, contract["input_hash"])
    embedded_required = require_embedded_report or bool(contract["embedded_required"])

    processed_count = contract["processed_count"]
    processed_ids = contract["processed_ids"]
    # Known producers must publish a derived count; silently accepting a
    # missing count would let a report bind to an under-specified result.
    count_matches = processed_count is not None and report.processed_frame_count == processed_count
    ids_matches = processed_ids is None or report.processed_frame_ids == processed_ids
    if contract["runtime_mode"] == "sim" and processed_ids is None:
        ids_matches = False
    if processed_ids is None:
        raw_ids = result.get("processed_frame_ids")
        if isinstance(raw_ids, Sequence) and not isinstance(raw_ids, (str, bytes, bytearray)):
            ids_matches = tuple(raw_ids) == report.processed_frame_ids

    mode_matches = expected_mode is None or expected_mode in {"replay", "sim", "hil", "real"}
    if expected_mode is not None and contract["runtime_mode"] is not None:
        mode_matches = expected_mode == contract["runtime_mode"]
    declared_mode = _normalise_mode(result.get("runtime_mode"))
    if declared_mode is not None:
        mode_matches = mode_matches and expected_mode == declared_mode

    descriptor_matches = (
        descriptor_expected is not None and report.descriptor_hash == descriptor_expected
    )
    input_matches = input_expected is not None and report.input_hash == input_expected

    expected_stream_exhausted = contract["stream_exhausted"]
    expected_max_frames = contract["max_frames"]
    termination_matches = True
    if expected_stream_exhausted is not None:
        termination_matches = report.stream_exhausted is expected_stream_exhausted
        if expected_stream_exhausted:
            termination_matches = termination_matches and report.max_frames is None
        else:
            termination_matches = (
                termination_matches
                and expected_max_frames is not None
                and report.max_frames == expected_max_frames
            )
    if require_complete:
        termination_matches = (
            termination_matches and report.stream_exhausted and report.max_frames is None
        )

    embedded_matches, embedded_present = _embedded_report_matches(
        result,
        report,
        required=embedded_required,
    )

    result_gate_passed = result.get("gate_passed") is True
    if event == "simulator_scenario_run_finished":
        result_gate_passed = result.get("terminal_reason") in {
            "frame_budget_complete",
            "fault_terminal",
        }

    checks = {
        "result_json_valid": True,
        "runtime_report_valid": True,
        "result_event_supported": bool(contract["supported"]),
        "result_gate_passed": result_gate_passed,
        "runtime_mode_matches": mode_matches,
        "descriptor_hash_matches": descriptor_matches,
        "input_hash_matches": input_matches,
        "processed_frame_count_matches": count_matches,
        "processed_frame_ids_match": ids_matches,
        "termination_contract_matches": termination_matches,
        "embedded_report_matches": embedded_matches,
        "sim_truth_free": contract["runtime_mode"] != "sim" or contract["truth_free"] is True,
        "sim_closed": contract["runtime_mode"] != "sim" or contract["closed"] is True,
    }
    reasons = [name for name in _CHECK_NAMES if not checks[name]]
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "result": {
            "path": str(result_file),
            "bytes": len(result_raw),
            "sha256": hashlib.sha256(result_raw).hexdigest(),
            "event": event,
            "runtime_mode": contract["runtime_mode"],
            "gate_passed": result.get("gate_passed"),
        },
        "runtime_report": {
            "path": str(report_file),
            "bytes": len(report_raw),
            "sha256": hashlib.sha256(report_raw).hexdigest(),
            "canonical_sha256": report.canonical_hash,
            **report.to_mapping(),
        },
        "expectations": {
            "runtime_mode": expected_mode,
            "descriptor_hash": descriptor_expected,
            "input_hash": input_expected,
            "require_embedded_report": embedded_required,
            "require_complete": require_complete,
            "embedded_report_present": embedded_present,
        },
        "checks": checks,
        "gate_passed": all(checks.values()),
        "reasons": reasons,
        "claim_boundary": {
            "software_runtime_identity_only": True,
            "hardware_acceptance": "not_claimed",
            "hil_acceptance": "not_claimed",
            "real_acceptance": "not_claimed",
            "flight_acceptance": "not_claimed",
        },
    }


def _write_json(path: Path, document: Mapping[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path, help="completed runtime result JSON")
    parser.add_argument("runtime_report", type=Path, help="RuntimeRunReport sidecar JSON")
    parser.add_argument("--runtime-mode", choices=("replay", "sim", "hil", "real"))
    parser.add_argument("--expected-descriptor-hash")
    parser.add_argument("--expected-input-hash")
    parser.add_argument(
        "--require-embedded-report",
        action="store_true",
        help="require result.runtime_run_report and match it byte-for-byte",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="require natural stream exhaustion instead of a bounded prefix",
    )
    parser.add_argument("--out", type=Path, help="optional JSON verification report")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = verify_runtime_report_binding(
            args.result,
            args.runtime_report,
            expected_runtime_mode=args.runtime_mode,
            expected_descriptor_hash=args.expected_descriptor_hash,
            expected_input_hash=args.expected_input_hash,
            require_embedded_report=args.require_embedded_report,
            require_complete=args.require_complete,
        )
        exit_code = 0 if document["gate_passed"] else 1
    except (OSError, RuntimeReportBindingError, TypeError, ValueError) as exc:
        document = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "event": EVENT,
            "checks": {"input_valid": False},
            "gate_passed": False,
            "reasons": [f"{type(exc).__name__}: {exc}"],
        }
        exit_code = 1
    if args.out is not None:
        _write_json(args.out, document)
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVENT",
    "RuntimeReportBindingError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "build_parser",
    "main",
    "verify_runtime_report_binding",
]
