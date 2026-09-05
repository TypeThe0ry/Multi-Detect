"""Exclusive capture leases and verified indexes for covariance field journals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .compat import UTC
from .field_covariance_campaign import REQUIRED_COMBINATIONS, campaign_status, load_campaign_config
from .field_covariance_workspace import (
    CAMPAIGN_NAME,
    FieldCovarianceWorkspaceError,
    verify_field_covariance_workspace_receipt,
)

LEASE_SCHEMA = "multidetect.pixhawk-covariance-field-capture-lease/v1"
RECOVERY_SCHEMA = "multidetect.pixhawk-covariance-field-lease-recovery/v1"
INDEX_SCHEMA = "multidetect.pixhawk-covariance-field-journal-index/v1"
SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "pixhawk-covariance-field-journal.schema.json"
LEASE_FILE = "lease.json"
RECOVERY_FILE = "recovery.json"
_LEASE_ID_LENGTH = 32


class FieldCovarianceJournalError(ValueError):
    """Raised when a journal lease or index cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: dict[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FieldCovarianceJournalError(f"cannot read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FieldCovarianceJournalError(f"{name} must be a JSON object")
    return value


def _validate(document: dict[str, object]) -> None:
    schema = _json_object(SCHEMA_PATH, name="field journal JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise FieldCovarianceJournalError(f"field journal schema validation failed: {exc}") from exc


def _with_digest(document: dict[str, object], field: str) -> dict[str, object]:
    value = dict(document)
    value[field] = _canonical_sha256(value)
    _validate(value)
    return value


def _verify_digest(document: dict[str, object], field: str, *, name: str) -> None:
    claimed = document.get(field)
    unsigned = dict(document)
    unsigned.pop(field, None)
    if claimed != _canonical_sha256(unsigned):
        raise FieldCovarianceJournalError(f"{name} digest mismatch")


def _write_new(path: Path, document: dict[str, object]) -> None:
    payload = (json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


def _timestamp(value: datetime | None) -> tuple[datetime, str]:
    timestamp = datetime.now(UTC) if value is None else value
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise FieldCovarianceJournalError("timestamp must be timezone-aware")
    return timestamp, timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _owner() -> dict[str, object]:
    return {"hostname": socket.gethostname(), "pid": os.getpid()}


def _receipt_record(receipt: dict[str, object]) -> dict[str, object]:
    path = Path(str(receipt["receipt_path"])).resolve(strict=True)
    return {
        "path": str(path),
        "file_sha256": _sha256(path),
        "receipt_sha256": receipt["receipt_sha256"],
        "workspace_sha256": receipt["workspace_sha256"],
    }


def _journal_path(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceJournalError("journal directory cannot be a symbolic link")
    path = candidate.resolve(strict=False)
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise FieldCovarianceJournalError("journal path must be a regular directory")
    return path


def _lease_directory(journal: Path, workspace_sha256: object) -> Path:
    if not isinstance(workspace_sha256, str) or len(workspace_sha256) != 64:
        raise FieldCovarianceJournalError("workspace identity is invalid")
    return journal / f".{workspace_sha256}.capture-lease"


def _load_lease(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_dir():
        raise FieldCovarianceJournalError("capture lease path is not a regular directory")
    lease_path = path / LEASE_FILE
    if lease_path.is_symlink() or not lease_path.is_file():
        raise FieldCovarianceJournalError("capture lease has no regular lease document")
    return verify_capture_lease_document(
        _json_object(lease_path, name="capture lease")
    )


def verify_capture_lease_document(document: dict[str, object]) -> dict[str, object]:
    """Verify an active or session-embedded capture lease document."""

    lease = dict(document)
    _validate(lease)
    if lease.get("schema") != LEASE_SCHEMA:
        raise FieldCovarianceJournalError("capture lease schema is wrong")
    _verify_digest(lease, "lease_sha256", name="capture lease")
    return lease


def _windows_process_state(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return "dead" if ctypes.get_last_error() == 87 else "unknown"
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return "unknown"
        return "alive" if exit_code.value == still_active else "dead"
    finally:
        kernel32.CloseHandle(handle)


def _owner_process_state(owner: object) -> str:
    if not isinstance(owner, dict):
        return "unknown"
    if str(owner.get("hostname", "")).casefold() != socket.gethostname().casefold():
        return "remote_or_unknown"
    pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return "unknown"
    if pid == os.getpid():
        return "alive"
    if os.name == "nt":
        return _windows_process_state(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


@dataclass(slots=True)
class CaptureLease:
    directory: Path
    document: dict[str, object]
    released: bool = False

    def __enter__(self) -> CaptureLease:
        return self

    def release(self) -> None:
        if self.released:
            return
        current = _load_lease(self.directory)
        if current != self.document:
            raise FieldCovarianceJournalError("capture lease changed while held")
        (self.directory / LEASE_FILE).unlink()
        self.directory.rmdir()
        self.released = True

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.release()


def acquire_capture_lease(
    receipt_path: str | Path,
    journal_directory: str | Path,
    *,
    leaf_ordinal: int,
    runtime_mode: str,
    vehicle_class: str,
    acquired_at: datetime | None = None,
    lease_id: str | None = None,
) -> CaptureLease:
    """Atomically acquire one campaign-wide lease; never remove a pre-existing lease."""

    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    journal = _journal_path(journal_directory)
    workspace = Path(str(receipt["destination_workspace_root"])).resolve(strict=True)
    try:
        journal.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise FieldCovarianceJournalError("journal directory must remain outside workspace")
    identifier = uuid.uuid4().hex if lease_id is None else lease_id
    if len(identifier) != _LEASE_ID_LENGTH or any(
        char not in "0123456789abcdef" for char in identifier
    ):
        raise FieldCovarianceJournalError("lease_id must be 32 lowercase hexadecimal characters")
    _, acquired_text = _timestamp(acquired_at)
    campaign_path = (workspace / CAMPAIGN_NAME).resolve(strict=True)
    config = load_campaign_config(campaign_path)
    if not 1 <= leaf_ordinal <= len(config.leaves):
        raise FieldCovarianceJournalError("lease leaf ordinal is invalid")
    leaf = config.leaves[leaf_ordinal - 1]
    if leaf.key != (runtime_mode, vehicle_class):
        raise FieldCovarianceJournalError("lease leaf does not match campaign order")
    lease_dir = _lease_directory(journal, receipt["workspace_sha256"])
    try:
        lease_dir.mkdir()
    except FileExistsError as exc:
        existing = _load_lease(lease_dir)
        raise FieldCovarianceJournalError(
            "capture lease already held: "
            f"lease_id={existing['lease_id']} owner={existing['owner']}"
        ) from exc
    try:
        lease = _with_digest(
            {
                "schema": LEASE_SCHEMA,
                "schema_version": SCHEMA_VERSION,
                "lease_id": identifier,
                "acquired_at_utc": acquired_text,
                "owner": _owner(),
                "receipt": _receipt_record(receipt),
                "campaign": {
                    "path": str(campaign_path),
                    "file_sha256": _sha256(campaign_path),
                    "leaf": {
                        "leaf_ordinal": leaf_ordinal,
                        "runtime_mode": runtime_mode,
                        "vehicle_class": vehicle_class,
                    },
                },
                "claims": {
                    "release_eligible": False,
                    "hardware_acceptance": False,
                    "flight_acceptance": False,
                },
            },
            "lease_sha256",
        )
        _write_new(lease_dir / LEASE_FILE, lease)
    except BaseException:
        (lease_dir / LEASE_FILE).unlink(missing_ok=True)
        lease_dir.rmdir()
        raise
    return CaptureLease(lease_dir, lease)


def capture_lease_status(
    receipt_path: str | Path, journal_directory: str | Path
) -> dict[str, object]:
    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    journal = _journal_path(journal_directory)
    lease_dir = _lease_directory(journal, receipt["workspace_sha256"])
    if not lease_dir.exists():
        return {
            "state": "absent",
            "lease_directory": str(lease_dir),
            "workspace_sha256": receipt["workspace_sha256"],
            "gate_passed": True,
        }
    lease = _load_lease(lease_dir)
    if lease["receipt"] != _receipt_record(receipt):
        raise FieldCovarianceJournalError("capture lease belongs to different receipt state")
    return {
        "state": "held",
        "lease_directory": str(lease_dir),
        "lease": lease,
        "owner_process_state": _owner_process_state(lease["owner"]),
        "gate_passed": False,
    }


def recover_dead_capture_lease(
    receipt_path: str | Path,
    journal_directory: str | Path,
    *,
    expected_lease_id: str,
    reason: str,
    recovered_at: datetime | None = None,
) -> dict[str, object]:
    """Archive an exact same-host lease only after its owning PID is proven dead."""

    if not reason.strip() or len(reason) > 500:
        raise FieldCovarianceJournalError("recovery reason must contain 1 to 500 characters")
    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    journal = _journal_path(journal_directory)
    lease_dir = _lease_directory(journal, receipt["workspace_sha256"])
    lease = _load_lease(lease_dir)
    if lease["lease_id"] != expected_lease_id:
        raise FieldCovarianceJournalError("expected lease ID does not match active lease")
    owner_state = _owner_process_state(lease["owner"])
    if owner_state != "dead":
        raise FieldCovarianceJournalError(
            f"capture lease owner is not proven dead: {owner_state}"
        )
    _, recovered_text = _timestamp(recovered_at)
    lease_path = lease_dir / LEASE_FILE
    recovery = _with_digest(
        {
            "schema": RECOVERY_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "recovered_at_utc": recovered_text,
            "reason": reason.strip(),
            "lease_id": lease["lease_id"],
            "lease_file_sha256": _sha256(lease_path),
            "lease_sha256": lease["lease_sha256"],
            "owner_state": "dead",
            "recovered_by": _owner(),
            "receipt": _receipt_record(receipt),
            "claims": {
                "release_eligible": False,
                "hardware_acceptance": False,
                "flight_acceptance": False,
            },
        },
        "recovery_sha256",
    )
    recovery_path = lease_dir / RECOVERY_FILE
    _write_new(recovery_path, recovery)
    archive_parent = journal / "recovered-leases"
    archive_parent.mkdir(exist_ok=True)
    archive = archive_parent / str(lease["lease_id"])
    if archive.exists() or archive.is_symlink():
        recovery_path.unlink(missing_ok=True)
        raise FieldCovarianceJournalError("lease recovery archive already exists")
    try:
        os.replace(lease_dir, archive)
    except BaseException:
        recovery_path.unlink(missing_ok=True)
        raise
    return {
        **recovery,
        "archive_directory": str(archive),
        "gate_passed": True,
    }


def build_field_covariance_journal_index(
    receipt_path: str | Path,
    journal_directory: str | Path,
    *,
    indexed_at: datetime | None = None,
) -> dict[str, object]:
    """Verify every successful session and reconcile its exact prefix with campaign reports."""

    from .field_covariance_session import verify_field_covariance_session_record

    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    journal = _journal_path(journal_directory)
    _, indexed_text = _timestamp(indexed_at)
    campaign_path = (
        Path(str(receipt["destination_workspace_root"])) / CAMPAIGN_NAME
    ).resolve(strict=True)
    config = load_campaign_config(campaign_path)
    status = campaign_status(config)
    records: list[dict[str, object]] = []
    referenced_probes: set[Path] = set()
    for path in sorted(journal.glob("*.session.json")):
        verified = verify_field_covariance_session_record(path)
        if (
            verified["receipt"]["receipt_sha256"] != receipt["receipt_sha256"]
            or verified["receipt"]["workspace_sha256"] != receipt["workspace_sha256"]
        ):
            raise FieldCovarianceJournalError("journal session belongs to another receipt")
        campaign = verified["campaign"]
        records.append(
            {
                "leaf_ordinal": campaign["leaf_ordinal"],
                "runtime_mode": campaign["runtime_mode"],
                "vehicle_class": campaign["vehicle_class"],
                "path": str(path.resolve(strict=True)),
                "file_sha256": _sha256(path),
                "record_sha256": verified["record_sha256"],
            }
        )
        referenced_probes.add(Path(verified["online_preflight"]["path"]).resolve(strict=True))
    records.sort(key=lambda item: int(item["leaf_ordinal"]))
    ordinals = [int(item["leaf_ordinal"]) for item in records]
    if ordinals != list(range(1, len(records) + 1)):
        raise FieldCovarianceJournalError(
            "journal sessions are duplicate or not a contiguous prefix"
        )
    expected_prefix = REQUIRED_COMBINATIONS[: len(records)]
    for item, expected in zip(records, expected_prefix, strict=True):
        if (item["runtime_mode"], item["vehicle_class"]) != expected:
            raise FieldCovarianceJournalError("journal session order does not match campaign")
    if len(records) != status["passed_count"]:
        raise FieldCovarianceJournalError(
            "journal session count does not match verified campaign report count"
        )
    all_probes = {path.resolve(strict=True) for path in journal.glob("*.online-preflight.json")}
    blocked_probes = all_probes - referenced_probes
    next_leaf = status["next_leaf"]
    next_summary = (
        None
        if next_leaf is None
        else {
            "leaf_ordinal": len(records) + 1,
            "runtime_mode": next_leaf["runtime_mode"],
            "vehicle_class": next_leaf["vehicle_class"],
        }
    )
    return _with_digest(
        {
            "schema": INDEX_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "indexed_at_utc": indexed_text,
            "journal_directory": str(journal),
            "receipt": _receipt_record(receipt),
            "campaign": {
                "path": str(campaign_path),
                "file_sha256": _sha256(campaign_path),
            },
            "records": records,
            "blocked_probe_count": len(blocked_probes),
            "passed_count": status["passed_count"],
            "pending_count": status["pending_count"],
            "complete": status["complete"],
            "next_leaf": next_summary,
            "claims": {
                "release_eligible": False,
                "hardware_acceptance": False,
                "flight_acceptance": False,
            },
        },
        "index_sha256",
    )


def write_field_covariance_journal_index(
    receipt_path: str | Path,
    journal_directory: str | Path,
    output: str | Path,
) -> dict[str, object]:
    index = build_field_covariance_journal_index(receipt_path, journal_directory)
    destination = Path(output).expanduser().resolve(strict=False)
    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    workspace = Path(str(receipt["destination_workspace_root"])).resolve(strict=True)
    try:
        destination.relative_to(workspace)
    except ValueError:
        pass
    else:
        raise FieldCovarianceJournalError("journal index must remain outside workspace")
    if destination.is_symlink() or destination.exists():
        raise FieldCovarianceJournalError("journal index output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_new(destination, index)
    return {**index, "index_path": str(destination), "gate_passed": True}


def verify_field_covariance_journal_index(path: str | Path) -> dict[str, object]:
    from .field_covariance_session import verify_field_covariance_session_record

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceJournalError("journal index cannot be a symbolic link")
    index_path = candidate.resolve(strict=True)
    index = _json_object(index_path, name="field journal index")
    _validate(index)
    if index.get("schema") != INDEX_SCHEMA:
        raise FieldCovarianceJournalError("field journal index schema is wrong")
    _verify_digest(index, "index_sha256", name="field journal index")
    receipt_path = Path(index["receipt"]["path"])
    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    if index["receipt"] != _receipt_record(receipt):
        raise FieldCovarianceJournalError("journal index receipt changed")
    journal = Path(index["journal_directory"])
    if journal.is_symlink() or not journal.resolve(strict=True).is_dir():
        raise FieldCovarianceJournalError("indexed journal directory changed")
    journal = journal.resolve(strict=True)
    campaign_path = Path(index["campaign"]["path"]).resolve(strict=True)
    if _sha256(campaign_path) != index["campaign"]["file_sha256"]:
        raise FieldCovarianceJournalError("journal index campaign changed")
    config = load_campaign_config(campaign_path)
    status = campaign_status(config)
    records = index["records"]
    record_count = len(records)
    expected_next = (
        None
        if record_count == len(REQUIRED_COMBINATIONS)
        else {
            "leaf_ordinal": record_count + 1,
            "runtime_mode": REQUIRED_COMBINATIONS[record_count][0],
            "vehicle_class": REQUIRED_COMBINATIONS[record_count][1],
        }
    )
    if (
        index["passed_count"] != record_count
        or index["pending_count"] != len(REQUIRED_COMBINATIONS) - record_count
        or index["complete"] is not (record_count == len(REQUIRED_COMBINATIONS))
        or index["next_leaf"] != expected_next
    ):
        raise FieldCovarianceJournalError("journal index progression is inconsistent")
    for ordinal, record in enumerate(records, start=1):
        record_path = Path(record["path"]).resolve(strict=True)
        if record_path.parent != journal:
            raise FieldCovarianceJournalError("indexed session escaped journal directory")
        if _sha256(record_path) != record["file_sha256"]:
            raise FieldCovarianceJournalError("indexed session file changed")
        verified = verify_field_covariance_session_record(record_path)
        if (
            verified["record_sha256"] != record["record_sha256"]
            or verified["receipt"]["receipt_sha256"]
            != receipt["receipt_sha256"]
            or verified["receipt"]["workspace_sha256"]
            != receipt["workspace_sha256"]
            or record["leaf_ordinal"] != ordinal
            or (record["runtime_mode"], record["vehicle_class"])
            != REQUIRED_COMBINATIONS[ordinal - 1]
        ):
            raise FieldCovarianceJournalError("indexed session identity changed")
    if status["passed_count"] < index["passed_count"]:
        raise FieldCovarianceJournalError("campaign progress regressed behind journal index")
    return {**index, "index_path": str(index_path), "gate_passed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("journal-status")
    status.add_argument("--receipt", required=True, type=Path)
    status.add_argument("--journal-dir", required=True, type=Path)
    status.add_argument("--out", type=Path)
    lease = subparsers.add_parser("lease-status")
    lease.add_argument("--receipt", required=True, type=Path)
    lease.add_argument("--journal-dir", required=True, type=Path)
    recover = subparsers.add_parser("recover-lease")
    recover.add_argument("--receipt", required=True, type=Path)
    recover.add_argument("--journal-dir", required=True, type=Path)
    recover.add_argument("--expected-lease-id", required=True)
    recover.add_argument("--reason", required=True)
    verify = subparsers.add_parser("verify-index")
    verify.add_argument("--index", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "journal-status":
            if args.out is None:
                result = build_field_covariance_journal_index(args.receipt, args.journal_dir)
                exit_code = 0
            else:
                result = write_field_covariance_journal_index(
                    args.receipt, args.journal_dir, args.out
                )
                exit_code = 0
        elif args.command == "lease-status":
            result = capture_lease_status(args.receipt, args.journal_dir)
            exit_code = 0 if result["state"] == "absent" else 1
        elif args.command == "recover-lease":
            result = recover_dead_capture_lease(
                args.receipt,
                args.journal_dir,
                expected_lease_id=args.expected_lease_id,
                reason=args.reason,
            )
            exit_code = 0
        else:
            result = verify_field_covariance_journal_index(args.index)
            exit_code = 0
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return exit_code
    except (FieldCovarianceJournalError, FieldCovarianceWorkspaceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CaptureLease",
    "FieldCovarianceJournalError",
    "INDEX_SCHEMA",
    "LEASE_SCHEMA",
    "RECOVERY_SCHEMA",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "acquire_capture_lease",
    "build_field_covariance_journal_index",
    "capture_lease_status",
    "recover_dead_capture_lease",
    "verify_capture_lease_document",
    "verify_field_covariance_journal_index",
    "write_field_covariance_journal_index",
]
