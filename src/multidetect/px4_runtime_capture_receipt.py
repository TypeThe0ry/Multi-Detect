"""Create and verify a portable receipt for one PX4 runtime capture.

The capture producer emits a semantic summary with absolute paths because it
is primarily a local diagnostic.  This module turns that summary into a
portable, byte-exact receipt: six source/sidecar files are listed by role,
their relative paths and SHA-256 digests are recorded, and the summary itself
is hashed separately.  Verification re-resolves every path beneath one base
directory and refuses moved, replaced, symlinked, or tampered files.

The receipt is evidence of a software receive-only capture and a passed
five-source join.  It deliberately carries no hardware, flight, or release
claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .autonomy.serialization import (
    AutonomyContractError,
    require_bool,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

SCHEMA = "multidetect.px4-runtime-capture-receipt"
SCHEMA_VERSION = 1
EVENT = "px4_runtime_capture_receipt_created"
ERROR_EVENT = "px4_runtime_capture_receipt_error"
VERIFY_EVENT = "px4_runtime_capture_receipt_verified"
CAPTURE_SCHEMA = "multidetect.px4-runtime-capture"
CAPTURE_EVENT = "px4_runtime_capture_completed"
SOURCE_COUNT = 6
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "px4-runtime-capture-receipt.schema.json"
CAPTURE_SCHEMA_PATH = ROOT / "configs" / "schemas" / "px4-runtime-capture.schema.json"
_ROLES: tuple[tuple[str, str], ...] = (
    ("mission", "mission"),
    ("runtime_observation", "runtime_observation"),
    ("runtime_profile", "runtime_profile"),
    ("mission_feedback", "mission_feedback"),
    ("endpoint_vehicle_identity", "endpoint_vehicle_identity"),
    ("join", "join"),
)
_CLAIM_NAMES = (
    "release_eligible",
    "hardware_acceptance",
    "hil_acceptance",
    "real_acceptance",
    "operator_visual_acceptance",
)
_MAX_JSON_BYTES = 2 * 1024 * 1024


class Px4RuntimeCaptureReceiptError(AutonomyContractError):
    """Raised when a capture receipt or its file closure is not trustworthy."""


def _regular_directory(value: str | Path, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise Px4RuntimeCaptureReceiptError(
            f"{label} must be a regular non-symbolic-link directory"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise Px4RuntimeCaptureReceiptError(f"{label} cannot be resolved: {exc}") from exc
    if resolved.is_symlink() or not resolved.is_dir():
        raise Px4RuntimeCaptureReceiptError(
            f"{label} must be a regular non-symbolic-link directory"
        )
    return resolved


def _assert_no_symlink_components(path: Path, *, base: Path, label: str) -> None:
    """Reject symlinks in an existing path between ``base`` and ``path``."""

    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise Px4RuntimeCaptureReceiptError(
            f"{label} must resolve inside base directory {base}"
        ) from exc
    cursor = base
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise Px4RuntimeCaptureReceiptError(f"{label} cannot contain a symbolic link")


def _resolve_input(value: str | Path, *, base: Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.is_symlink():
        raise Px4RuntimeCaptureReceiptError(f"{label} cannot be a symbolic link")
    # Inspect the lexical path before resolving it.  Checking only the
    # resolved path would erase a directory symlink from the walk and allow a
    # source to cross an otherwise trusted-looking path component.
    _assert_no_symlink_components(candidate.absolute(), base=base, label=label)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Px4RuntimeCaptureReceiptError(
            f"{label} must resolve inside base directory {base}"
        ) from exc
    _assert_no_symlink_components(candidate.absolute(), base=base, label=label)
    if not resolved.is_file():
        raise Px4RuntimeCaptureReceiptError(f"{label} must be a regular file")
    return resolved


def _resolve_output(value: str | Path, *, base: Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if candidate.exists() and candidate.is_symlink():
        raise Px4RuntimeCaptureReceiptError(f"{label} cannot be a symbolic link")
    _assert_no_symlink_components(candidate.absolute(), base=base, label=label)
    try:
        parent = candidate.parent.resolve(strict=False)
        parent.relative_to(base)
    except (OSError, RuntimeError, ValueError) as exc:
        raise Px4RuntimeCaptureReceiptError(
            f"{label} must resolve inside base directory {base}"
        ) from exc
    _assert_no_symlink_components(candidate.parent.absolute(), base=base, label=label)
    parent.mkdir(parents=True, exist_ok=True)
    resolved = parent / candidate.name
    if resolved == base or not resolved.name:
        raise Px4RuntimeCaptureReceiptError(f"{label} must be a file path")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise Px4RuntimeCaptureReceiptError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _read_json(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise Px4RuntimeCaptureReceiptError(f"cannot read {label}: {exc}") from exc
    if not payload:
        raise Px4RuntimeCaptureReceiptError(f"{label} is empty")
    if len(payload) > _MAX_JSON_BYTES:
        raise Px4RuntimeCaptureReceiptError(f"{label} exceeds {_MAX_JSON_BYTES} bytes")
    try:
        raw = strict_json_loads(payload, name=label)
    except (TypeError, ValueError) as exc:
        raise Px4RuntimeCaptureReceiptError(f"cannot parse {label}: {exc}") from exc
    if not isinstance(raw, dict):
        raise Px4RuntimeCaptureReceiptError(f"{label} must be a JSON object")
    return payload, raw


def _validate_document_schema(document: Mapping[str, Any], *, path: Path, label: str) -> None:
    try:
        schema_payload = path.read_text(encoding="utf-8")
        schema = json.loads(schema_payload)
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
            key=lambda item: list(item.absolute_path),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise Px4RuntimeCaptureReceiptError(f"cannot validate {label} schema: {exc}") from exc
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise Px4RuntimeCaptureReceiptError(
            f"{label} schema validation failed at {location}: {first.message}"
        )


def _portable_relative(path: Path, *, base: Path, label: str) -> str:
    try:
        value = path.relative_to(base).as_posix()
    except ValueError as exc:
        raise Px4RuntimeCaptureReceiptError(
            f"{label} must resolve inside base directory {base}"
        ) from exc
    portable = Path(value)
    if (
        not value
        or "\\" in value
        or portable.is_absolute()
        or ".." in portable.parts
        or portable.name in {"", ".", ".."}
    ):
        raise Px4RuntimeCaptureReceiptError(f"{label} is not a portable relative path")
    return value


def _load_capture_summary(
    value: str | Path,
    *,
    base: Path,
) -> tuple[Path, bytes, dict[str, Any]]:
    path = _resolve_input(value, base=base, label="capture summary")
    payload, document = _read_json(path, label="capture summary")
    _validate_document_schema(document, path=CAPTURE_SCHEMA_PATH, label="capture summary")
    if document["schema"] != CAPTURE_SCHEMA or document["event"] != CAPTURE_EVENT:
        raise Px4RuntimeCaptureReceiptError("capture summary is not a completed PX4 capture")
    if document["join"]["gate_passed"] is not True:
        raise Px4RuntimeCaptureReceiptError("capture summary join gate is not passed")
    if document["mission_feedback"]["gate_passed"] is not True:
        raise Px4RuntimeCaptureReceiptError("capture summary Mission feedback gate is not passed")
    if document["endpoint_vehicle_identity"]["gate_passed"] is not True:
        raise Px4RuntimeCaptureReceiptError(
            "capture summary endpoint/vehicle identity gate is not passed"
        )
    claims = document["claims"]
    if any(claims[name] is not False for name in _CLAIM_NAMES):
        raise Px4RuntimeCaptureReceiptError("capture summary contains a forbidden acceptance claim")
    return path, payload, document


def _source_entries(summary: Mapping[str, Any], *, base: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role, section_name in _ROLES:
        section = summary.get(section_name)
        if not isinstance(section, Mapping):
            raise Px4RuntimeCaptureReceiptError(
                f"capture summary section is invalid: {section_name}"
            )
        raw_path = require_text(section.get("path"), f"capture summary {section_name}.path")
        resolved = _resolve_input(raw_path, base=base, label=f"{section_name} source")
        relative = _portable_relative(resolved, base=base, label=f"{section_name} source")
        folded = relative.casefold()
        if folded in seen:
            raise Px4RuntimeCaptureReceiptError("capture summary source paths must be unique")
        seen.add(folded)
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise Px4RuntimeCaptureReceiptError(
                f"cannot stat {section_name} source: {exc}"
            ) from exc
        if size < 1:
            raise Px4RuntimeCaptureReceiptError(f"{section_name} source is empty")
        entries.append(
            {
                "role": role,
                "path": relative,
                "bytes": size,
                "sha256": _sha256_file(resolved),
            }
        )
    if len(entries) != SOURCE_COUNT:
        raise Px4RuntimeCaptureReceiptError("PX4 capture receipt must contain exactly six sources")
    return entries


def _claims() -> dict[str, bool]:
    return {name: False for name in _CLAIM_NAMES}


def _gates() -> dict[str, bool]:
    return {
        "capture_summary_completed": True,
        "mission_feedback": True,
        "endpoint_vehicle_identity": True,
        "join": True,
    }


def _assemble_document(
    *,
    runtime_mode: str,
    summary_relative: str,
    summary_bytes: int,
    summary_sha256: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": EVENT,
        "runtime_mode": runtime_mode,
        "source_count": SOURCE_COUNT,
        "capture_summary": {
            "path": summary_relative,
            "bytes": summary_bytes,
            "sha256": summary_sha256,
        },
        "files": files,
        "gates": _gates(),
        "gate_passed": True,
        "portable": True,
        "claims": _claims(),
    }


def _atomic_write(path: Path, document: Mapping[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise Px4RuntimeCaptureReceiptError(f"refusing to overwrite existing receipt: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _expected_document(
    summary_path: Path,
    summary_payload: bytes,
    summary: Mapping[str, Any],
    *,
    base: Path,
    expected_runtime_mode: str | None,
) -> dict[str, Any]:
    runtime_mode = require_text(summary.get("runtime_mode"), "capture summary runtime_mode").lower()
    if runtime_mode not in {"hil", "real"}:
        raise Px4RuntimeCaptureReceiptError("capture summary runtime_mode must be hil or real")
    if expected_runtime_mode is not None:
        expected = require_text(expected_runtime_mode, "expected runtime_mode").lower()
        if expected not in {"hil", "real"}:
            raise Px4RuntimeCaptureReceiptError("expected runtime_mode must be hil or real")
        if runtime_mode != expected:
            raise Px4RuntimeCaptureReceiptError(
                "capture summary runtime_mode does not match expectation"
            )
    summary_relative = _portable_relative(summary_path, base=base, label="capture summary")
    files = _source_entries(summary, base=base)
    source_paths = {item["path"].casefold() for item in files}
    if summary_relative.casefold() in source_paths:
        raise Px4RuntimeCaptureReceiptError(
            "capture summary must differ from every PX4 capture source"
        )
    return _assemble_document(
        runtime_mode=runtime_mode,
        summary_relative=summary_relative,
        summary_bytes=len(summary_payload),
        summary_sha256=hashlib.sha256(summary_payload).hexdigest(),
        files=files,
    )


def build_px4_runtime_capture_receipt(
    capture_summary: str | Path,
    base_directory: str | Path,
    output: str | Path,
    *,
    expected_runtime_mode: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build and immediately verify one portable PX4 capture receipt."""

    base = _regular_directory(base_directory, label="base directory")
    summary_path, summary_payload, summary = _load_capture_summary(capture_summary, base=base)
    output_path = _resolve_output(output, base=base, label="receipt output")
    if output_path == summary_path:
        raise Px4RuntimeCaptureReceiptError("receipt output must differ from capture summary")
    document = _expected_document(
        summary_path,
        summary_payload,
        summary,
        base=base,
        expected_runtime_mode=expected_runtime_mode,
    )
    output_relative = _portable_relative(output_path, base=base, label="receipt output")
    occupied = {document["capture_summary"]["path"].casefold()} | {
        item["path"].casefold() for item in document["files"]
    }
    if output_relative.casefold() in occupied:
        raise Px4RuntimeCaptureReceiptError(
            "receipt output must differ from the summary and all capture sources"
        )
    _validate_document_schema(document, path=SCHEMA_PATH, label="capture receipt")
    _atomic_write(output_path, document, overwrite=overwrite)
    verify_px4_runtime_capture_receipt(
        output_path,
        base_directory=base,
        expected_runtime_mode=document["runtime_mode"],
    )
    return document


def verify_px4_runtime_capture_receipt(
    receipt: str | Path,
    *,
    base_directory: str | Path | None = None,
    expected_runtime_mode: str | None = None,
) -> dict[str, Any]:
    """Recompute every receipt digest and verify the closed six-file set."""

    receipt_candidate = Path(receipt).expanduser()
    if receipt_candidate.is_symlink() or not receipt_candidate.is_file():
        raise Px4RuntimeCaptureReceiptError("receipt must be a regular non-symbolic-link file")
    receipt_anchor = receipt_candidate.absolute()
    base = (
        _regular_directory(base_directory, label="base directory")
        if base_directory is not None
        else receipt_anchor.parent
    )
    receipt_path = _resolve_input(receipt_candidate, base=base, label="receipt")
    _, document = _read_json(receipt_path, label="capture receipt")
    _validate_document_schema(document, path=SCHEMA_PATH, label="capture receipt")
    required = frozenset(
        {
            "schema",
            "schema_version",
            "event",
            "runtime_mode",
            "source_count",
            "capture_summary",
            "files",
            "gates",
            "gate_passed",
            "portable",
            "claims",
        }
    )
    strict_object(document, name="Px4RuntimeCaptureReceipt", required=required)
    if document["schema"] != SCHEMA or document["event"] != EVENT:
        raise Px4RuntimeCaptureReceiptError("capture receipt event is invalid")
    require_schema_version(document["schema_version"], expected=SCHEMA_VERSION)
    if require_bool(document["gate_passed"], "gate_passed") is not True:
        raise Px4RuntimeCaptureReceiptError("capture receipt gate is not passed")
    if require_bool(document["portable"], "portable") is not True:
        raise Px4RuntimeCaptureReceiptError("capture receipt must be portable")
    if (
        require_int(
            document["source_count"], "source_count", minimum=SOURCE_COUNT, maximum=SOURCE_COUNT
        )
        != SOURCE_COUNT
    ):
        raise Px4RuntimeCaptureReceiptError("capture receipt source_count must be six")
    runtime_mode = require_text(document["runtime_mode"], "runtime_mode").lower()
    if runtime_mode not in {"hil", "real"}:
        raise Px4RuntimeCaptureReceiptError("runtime_mode must be hil or real")
    if (
        expected_runtime_mode is not None
        and runtime_mode != require_text(expected_runtime_mode, "expected runtime_mode").lower()
    ):
        raise Px4RuntimeCaptureReceiptError(
            "capture receipt runtime_mode does not match expectation"
        )
    summary_meta = strict_object(
        document["capture_summary"],
        name="capture_summary",
        required=frozenset({"path", "bytes", "sha256"}),
    )
    summary_relative = require_text(summary_meta["path"], "capture_summary.path")
    _portable_relative(base / summary_relative, base=base, label="capture_summary.path")
    summary_path = _resolve_input(summary_relative, base=base, label="capture summary")
    summary_payload, summary = _load_capture_summary(summary_path, base=base)[1:]
    if require_int(summary_meta["bytes"], "capture_summary.bytes", minimum=1) != len(
        summary_payload
    ):
        raise Px4RuntimeCaptureReceiptError("capture summary byte count does not match receipt")
    summary_hash = require_sha256(summary_meta["sha256"], "capture_summary.sha256")
    if summary_hash != hashlib.sha256(summary_payload).hexdigest():
        raise Px4RuntimeCaptureReceiptError("capture summary SHA-256 does not match receipt")

    expected = _expected_document(
        summary_path,
        summary_payload,
        summary,
        base=base,
        expected_runtime_mode=runtime_mode,
    )
    if document != expected:
        raise Px4RuntimeCaptureReceiptError(
            "capture receipt does not match current summary or sources"
        )

    files = document["files"]
    if not isinstance(files, list) or len(files) != SOURCE_COUNT:
        raise Px4RuntimeCaptureReceiptError("capture receipt files must contain six entries")
    seen: set[str] = set()
    for index, entry in enumerate(files):
        item = strict_object(
            entry,
            name=f"files[{index}]",
            required=frozenset({"role", "path", "bytes", "sha256"}),
        )
        role = require_text(item["role"], f"files[{index}].role")
        relative = require_text(item["path"], f"files[{index}].path")
        folded = relative.casefold()
        if folded in seen:
            raise Px4RuntimeCaptureReceiptError("capture receipt file paths must be unique")
        seen.add(folded)
        source = _resolve_input(relative, base=base, label=f"files[{index}].path")
        size = require_int(item["bytes"], f"files[{index}].bytes", minimum=1)
        if size != source.stat().st_size:
            raise Px4RuntimeCaptureReceiptError(f"files[{index}] byte count does not match")
        digest = require_sha256(item["sha256"], f"files[{index}].sha256")
        if digest != _sha256_file(source):
            raise Px4RuntimeCaptureReceiptError(f"files[{index}] SHA-256 does not match")
        if role not in {name for name, _ in _ROLES}:
            raise Px4RuntimeCaptureReceiptError(f"files[{index}] has an unknown role")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-summary", type=Path, required=True)
    parser.add_argument("--base-directory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime-mode", choices=("hil", "real"))
    parser.add_argument("--overwrite", action="store_true")
    return parser


def _error_document(exc: BaseException) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "event": ERROR_EVENT,
        "error_type": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = build_px4_runtime_capture_receipt(
            args.capture_summary,
            args.base_directory,
            args.out,
            expected_runtime_mode=args.runtime_mode,
            overwrite=args.overwrite,
        )
    except (OSError, TypeError, ValueError, AutonomyContractError) as exc:
        print(json.dumps(_error_document(exc), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def verify_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a PX4 runtime capture receipt.")
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--base-directory", type=Path)
    parser.add_argument("--runtime-mode", choices=("hil", "real"))
    return parser


def verify_main(argv: Sequence[str] | None = None) -> int:
    args = verify_parser().parse_args(argv)
    try:
        document = verify_px4_runtime_capture_receipt(
            args.receipt,
            base_directory=args.base_directory,
            expected_runtime_mode=args.runtime_mode,
        )
    except (OSError, TypeError, ValueError, AutonomyContractError) as exc:
        print(json.dumps(_error_document(exc), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    verified = dict(document)
    verified["event"] = VERIFY_EVENT
    print(json.dumps(verified, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAPTURE_EVENT",
    "CAPTURE_SCHEMA",
    "ERROR_EVENT",
    "EVENT",
    "Px4RuntimeCaptureReceiptError",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SOURCE_COUNT",
    "build_parser",
    "build_px4_runtime_capture_receipt",
    "main",
    "verify_main",
    "verify_parser",
    "verify_px4_runtime_capture_receipt",
]
