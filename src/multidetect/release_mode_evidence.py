"""Prepare explicit, hash-bound evidence records for the four release modes.

The mode acceptance runners intentionally emit rich, runner-specific JSON.  The
E7 release gate consumes a much smaller contract: a regular JSON file must
declare its runtime mode and an explicit boolean ``gate_passed``.  This module
creates a deterministic envelope around one already-produced runner artifact.

The envelope never upgrades an artifact.  A missing or false source gate stays
false, and a source without a runtime identity requires an explicit CLI opt-in
so that a caller cannot silently turn an arbitrary goal snapshot into release
evidence.  The source path, event, mode and SHA-256 are retained for audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .release_gate import REQUIRED_MODES

MODE_EVIDENCE_SCHEMA_VERSION = 1


class ReleaseModeEvidenceError(ValueError):
    """Raised when a source artifact cannot be safely enveloped."""


def _resolve_file(value: str | Path, *, name: str) -> Path:
    source = Path(value).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ReleaseModeEvidenceError(f"{name} does not exist: {source}") from exc
    if not resolved.is_file():
        raise ReleaseModeEvidenceError(f"{name} must be a regular file: {resolved}")
    return resolved


def _load_object(path: Path) -> dict[str, Any]:
    try:
        # Windows PowerShell 5.1's ``Set-Content -Encoding utf8`` emits a
        # UTF-8 BOM.  Native acceptance wrappers use that writer for their
        # evidence JSON, so accept both BOM and BOM-less UTF-8 while keeping
        # every other decoding error fail-closed.
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReleaseModeEvidenceError(f"source evidence is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseModeEvidenceError("source evidence must contain a JSON object")
    return value


def _reject_json_constant(value: str) -> None:
    """Reject non-finite JSON numbers in hash-bound source evidence."""

    raise ValueError(f"non-finite JSON number {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject last-key-wins ambiguity in source evidence."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_mode(document: Mapping[str, Any]) -> str | None:
    for key in ("runtime_mode", "mode"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    runtime = document.get("runtime")
    if isinstance(runtime, Mapping):
        value = runtime.get("mode")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _relative_path(path: Path, *, directory: Path) -> str:
    try:
        return Path(os.path.relpath(path, directory)).as_posix()
    except ValueError as exc:
        raise ReleaseModeEvidenceError(
            "source and output must be on the same filesystem drive"
        ) from exc


def build_release_mode_evidence(
    source_path: str | Path,
    *,
    mode: str,
    output_path: str | Path,
    allow_missing_source_runtime_mode: bool = False,
    allow_source_runtime_mode_mismatch: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic release-mode envelope from one explicit source.

    ``gate_passed`` is copied only when the source contains a real boolean;
    otherwise it is ``false``.  A source mode mismatch is always an error.
    """

    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in REQUIRED_MODES:
        raise ReleaseModeEvidenceError(
            f"mode must be one of {', '.join(REQUIRED_MODES)}"
        )
    source = _resolve_file(source_path, name="source evidence")
    output = Path(output_path).expanduser().resolve()
    if source == output:
        raise ReleaseModeEvidenceError("output cannot overwrite the source evidence")
    document = _load_object(source)
    source_mode = _runtime_mode(document)
    if source_mode is None and not allow_missing_source_runtime_mode:
        raise ReleaseModeEvidenceError(
            "source evidence does not declare runtime_mode; pass "
            "--allow-missing-source-runtime-mode only after selecting the mode explicitly"
        )
    mode_override_reason: str | None = None
    if source_mode is not None and source_mode != normalized_mode:
        if not allow_source_runtime_mode_mismatch:
            raise ReleaseModeEvidenceError(
                f"source runtime mode is {source_mode!r}, expected {normalized_mode!r}"
            )
        mode_override_reason = (
            f"source runtime_mode {source_mode!r} was explicitly projected to "
            f"{normalized_mode!r} by the caller"
        )

    source_event = document.get("event")
    if source_event is not None and (
        not isinstance(source_event, str) or not source_event.strip()
    ):
        raise ReleaseModeEvidenceError(
            "source evidence event must be a non-empty string when present"
        )

    source_gate = document.get("gate_passed")
    gate_passed = source_gate if type(source_gate) is bool else False
    reasons: list[str] = []
    if type(source_gate) is not bool:
        reasons.append("source gate_passed is missing or not boolean")
    elif not source_gate:
        reasons.append("source gate_passed is false")
    if source_mode is None:
        reasons.append("source runtime_mode was supplied by explicit caller selection")
    if mode_override_reason is not None:
        reasons.append(mode_override_reason)

    output_directory = output.parent
    result: dict[str, Any] = {
        "schema_version": MODE_EVIDENCE_SCHEMA_VERSION,
        "event": f"{normalized_mode}_release_mode_evidence_prepared",
        "runtime_mode": normalized_mode,
        "gate_passed": gate_passed,
        "source": {
            "path": _relative_path(source, directory=output_directory),
            "sha256": _sha256(source),
            "event": source_event.strip() if isinstance(source_event, str) else None,
            "runtime_mode": source_mode,
            "gate_passed": source_gate if type(source_gate) is bool else None,
        },
        "acceptance_scope": "explicit_source_gate_projection",
    }
    if notes is not None:
        result["notes"] = notes
    if reasons:
        result["reasons"] = reasons
    return result


def write_release_mode_evidence(
    destination: str | Path,
    document: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> Path:
    """Atomically write an envelope with stable UTF-8 JSON bytes."""

    output = Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise ReleaseModeEvidenceError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser shared by the installed and source wrappers."""

    parser = argparse.ArgumentParser(
        description=(
            "Wrap one explicit REPLAY/SIM/HIL/REAL runner artifact. "
            "Missing source gates stay false."
        )
    )
    parser.add_argument("--mode", required=True, choices=REQUIRED_MODES)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--allow-missing-source-runtime-mode",
        action="store_true",
        help="allow explicit caller mode selection when the source has no runtime_mode",
    )
    parser.add_argument(
        "--allow-source-runtime-mode-mismatch",
        action="store_true",
        help="allow an explicit caller projection when the source declares another mode",
    )
    parser.add_argument("--notes")
    parser.add_argument("--no-overwrite", action="store_true")
    parser.add_argument(
        "--allow-incomplete",
        "--report-only",
        action="store_true",
        help="return zero when the source gate is pending/false",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the source evidence envelope CLI."""

    args = build_parser().parse_args(argv)
    try:
        document = build_release_mode_evidence(
            args.source,
            mode=args.mode,
            output_path=args.out,
            allow_missing_source_runtime_mode=args.allow_missing_source_runtime_mode,
            allow_source_runtime_mode_mismatch=args.allow_source_runtime_mode_mismatch,
            notes=args.notes,
        )
        destination = write_release_mode_evidence(
            args.out,
            document,
            overwrite=not args.no_overwrite,
        )
    except (ReleaseModeEvidenceError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"evidence_written={destination}", file=sys.stderr)
    return 0 if document["gate_passed"] or args.allow_incomplete else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "MODE_EVIDENCE_SCHEMA_VERSION",
    "ReleaseModeEvidenceError",
    "build_parser",
    "build_release_mode_evidence",
    "main",
    "write_release_mode_evidence",
]
