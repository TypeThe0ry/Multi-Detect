"""Create and verify portable, byte-exact field evidence transfer receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_bool,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_json_loads,
    strict_object,
)

TRANSFER_INVENTORY_SCHEMA_VERSION = 1
TRANSFER_VERIFICATION_SCHEMA_VERSION = 1


def create_evidence_transfer_inventory(
    bundle_root: str | Path,
    inventory_out: str | Path,
    *,
    bundle_id: str | None = None,
    utc_now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Snapshot a closed evidence directory before transport.

    The inventory may live inside the bundle and is then excluded from its own
    file set. Existing outputs are never overwritten.
    """

    root = _regular_directory(bundle_root, "evidence bundle root")
    output = Path(inventory_out).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise AutonomyContractError("transfer inventory output already exists")
    excluded = _relative_if_within(output, root)
    files = _inventory_files(root, excluded=excluded)
    if not files:
        raise AutonomyContractError("evidence bundle must contain at least one regular file")
    identifier = str(uuid4()) if bundle_id is None else _require_uuid(bundle_id, "bundle_id")
    now = (utc_now or (lambda: datetime.now(timezone.utc)))()
    created_at = _format_utc(now, "transfer inventory timestamp")
    document: dict[str, Any] = {
        "event": "evidence_bundle_transfer_inventory_created",
        "schema_version": TRANSFER_INVENTORY_SCHEMA_VERSION,
        "bundle_id": identifier,
        "created_at_utc": created_at,
        "source_root_name": root.name,
        "inventory_path": excluded,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
        "bundle_content_hash": canonical_sha256(files),
        "claims": _closed_claims(),
    }
    document["canonical_hash"] = canonical_sha256(document)
    _atomic_write(output, document)
    verify_evidence_transfer_inventory(output)
    return document


def verify_evidence_transfer_inventory(inventory: str | Path) -> dict[str, Any]:
    """Verify the closed structure and canonical identity of an inventory."""

    path, payload, document = _load_inventory(inventory)
    return {
        "path": path,
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "file_bytes": len(payload),
        "document": document,
    }


def verify_evidence_bundle_transfer(
    inventory: str | Path,
    destination_root: str | Path,
    verification_out: str | Path,
    *,
    utc_now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Re-inventory a copied bundle and save a fail-closed destination receipt."""

    verified_inventory = verify_evidence_transfer_inventory(inventory)
    source = verified_inventory["document"]
    root = _regular_directory(destination_root, "destination evidence bundle root")
    output = Path(verification_out).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise AutonomyContractError("transfer verification output already exists")
    if _relative_if_within(output, root) is not None:
        raise AutonomyContractError("transfer verification output must be outside destination root")

    excluded = source["inventory_path"]
    observed = _inventory_files(root, excluded=excluded)
    expected = source["files"]
    if observed != expected:
        raise AutonomyContractError(_inventory_difference(expected, observed))
    bundle_hash = canonical_sha256(observed)
    if bundle_hash != source["bundle_content_hash"]:
        raise AutonomyContractError("destination bundle content hash changed")

    now = (utc_now or (lambda: datetime.now(timezone.utc)))()
    document: dict[str, Any] = {
        "event": "evidence_bundle_transfer_verified",
        "schema_version": TRANSFER_VERIFICATION_SCHEMA_VERSION,
        "bundle_id": source["bundle_id"],
        "verified_at_utc": _format_utc(now, "transfer verification timestamp"),
        "source_inventory": {
            "file_name": verified_inventory["path"].name,
            "file_bytes": verified_inventory["file_bytes"],
            "file_sha256": verified_inventory["file_sha256"],
            "canonical_hash": source["canonical_hash"],
        },
        "destination_root_name": root.name,
        "file_count": len(observed),
        "total_bytes": sum(item["bytes"] for item in observed),
        "destination_files": observed,
        "source_bundle_content_hash": source["bundle_content_hash"],
        "destination_bundle_content_hash": bundle_hash,
        "byte_exact_copy": True,
        "claims": _closed_claims(),
    }
    document["canonical_hash"] = canonical_sha256(document)
    _atomic_write(output, document)
    verify_evidence_bundle_transfer_receipt(output)
    return document


def verify_evidence_bundle_transfer_receipt(receipt: str | Path) -> dict[str, Any]:
    path = _regular_file(receipt, "transfer verification receipt")
    raw = strict_json_loads(path.read_bytes(), name="transfer verification receipt")
    required = frozenset(
        {
            "event", "schema_version", "bundle_id", "verified_at_utc", "source_inventory",
            "destination_root_name", "file_count", "total_bytes", "destination_files",
            "source_bundle_content_hash", "destination_bundle_content_hash", "byte_exact_copy",
            "claims", "canonical_hash",
        }
    )
    document = strict_object(raw, name="EvidenceBundleTransferVerification", required=required)
    require_schema_version(
        document["schema_version"], expected=TRANSFER_VERIFICATION_SCHEMA_VERSION
    )
    if document["event"] != "evidence_bundle_transfer_verified":
        raise AutonomyContractError("transfer verification event is invalid")
    _require_uuid(document["bundle_id"], "bundle_id")
    _parse_utc(document["verified_at_utc"], "verified_at_utc")
    source = strict_object(
        document["source_inventory"],
        name="source_inventory",
        required=frozenset({"file_name", "file_bytes", "file_sha256", "canonical_hash"}),
    )
    require_text(source["file_name"], "source_inventory.file_name")
    require_int(source["file_bytes"], "source_inventory.file_bytes", minimum=1)
    require_sha256(source["file_sha256"], "source_inventory.file_sha256")
    require_sha256(source["canonical_hash"], "source_inventory.canonical_hash")
    files = _validate_file_entries(document["destination_files"])
    _validate_totals(document, files)
    source_hash = require_sha256(
        document["source_bundle_content_hash"], "source_bundle_content_hash"
    )
    destination_hash = require_sha256(
        document["destination_bundle_content_hash"], "destination_bundle_content_hash"
    )
    if source_hash != destination_hash or destination_hash != canonical_sha256(files):
        raise AutonomyContractError("transfer verification bundle content hashes differ")
    if require_bool(document["byte_exact_copy"], "byte_exact_copy") is not True:
        raise AutonomyContractError("transfer verification must assert byte_exact_copy")
    _validate_claims(document["claims"])
    _verify_canonical_hash(document)
    return document


def _load_inventory(inventory: str | Path) -> tuple[Path, bytes, dict[str, Any]]:
    path = _regular_file(inventory, "transfer inventory")
    payload = path.read_bytes()
    raw = strict_json_loads(payload, name="transfer inventory")
    required = frozenset(
        {
            "event", "schema_version", "bundle_id", "created_at_utc", "source_root_name",
            "inventory_path", "file_count", "total_bytes", "files", "bundle_content_hash",
            "claims", "canonical_hash",
        }
    )
    document = strict_object(raw, name="EvidenceBundleTransferInventory", required=required)
    require_schema_version(document["schema_version"], expected=TRANSFER_INVENTORY_SCHEMA_VERSION)
    if document["event"] != "evidence_bundle_transfer_inventory_created":
        raise AutonomyContractError("transfer inventory event is invalid")
    _require_uuid(document["bundle_id"], "bundle_id")
    _parse_utc(document["created_at_utc"], "created_at_utc")
    require_text(document["source_root_name"], "source_root_name")
    inventory_path = document["inventory_path"]
    if inventory_path is not None:
        _validate_relative_path(require_text(inventory_path, "inventory_path"))
    files = _validate_file_entries(document["files"])
    _validate_totals(document, files)
    if require_sha256(
        document["bundle_content_hash"], "bundle_content_hash"
    ) != canonical_sha256(files):
        raise AutonomyContractError("transfer inventory bundle content hash mismatch")
    _validate_claims(document["claims"])
    _verify_canonical_hash(document)
    return path, payload, document


def _inventory_files(root: Path, *, excluded: str | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = candidate.relative_to(root).as_posix()
        if relative == excluded:
            continue
        if candidate.is_symlink():
            raise AutonomyContractError(f"evidence bundle contains symlink: {relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise AutonomyContractError(f"evidence bundle contains non-regular entry: {relative}")
        _validate_relative_path(relative)
        folded = relative.casefold()
        if folded in seen:
            raise AutonomyContractError("evidence bundle contains case-colliding paths")
        seen.add(folded)
        entries.append(
            {"path": relative, "bytes": candidate.stat().st_size, "sha256": _sha256_file(candidate)}
        )
    return entries


def _validate_file_entries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AutonomyContractError("transfer file inventory must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous: str | None = None
    for index, raw in enumerate(value):
        item = strict_object(
            raw,
            name=f"files[{index}]",
            required=frozenset({"path", "bytes", "sha256"}),
        )
        path = require_text(item["path"], f"files[{index}].path")
        _validate_relative_path(path)
        folded = path.casefold()
        if folded in seen or (previous is not None and folded <= previous):
            raise AutonomyContractError("transfer file inventory paths must be unique and sorted")
        seen.add(folded)
        previous = folded
        result.append(
            {
                "path": path,
                "bytes": require_int(item["bytes"], f"files[{index}].bytes", minimum=0),
                "sha256": require_sha256(item["sha256"], f"files[{index}].sha256"),
            }
        )
    return result


def _validate_totals(document: dict[str, Any], files: list[dict[str, Any]]) -> None:
    if require_int(document["file_count"], "file_count", minimum=1) != len(files):
        raise AutonomyContractError("transfer file count mismatch")
    if require_int(document["total_bytes"], "total_bytes", minimum=0) != sum(
        item["bytes"] for item in files
    ):
        raise AutonomyContractError("transfer total byte count mismatch")


def _closed_claims() -> dict[str, bool]:
    return {"hardware_acceptance": False, "release_ready": False}


def _validate_claims(value: Any) -> None:
    claims = strict_object(
        value,
        name="claims",
        required=frozenset({"hardware_acceptance", "release_ready"}),
    )
    for name in ("hardware_acceptance", "release_ready"):
        if require_bool(claims[name], f"claims.{name}") is not False:
            raise AutonomyContractError(f"transfer {name} claim must remain false")


def _verify_canonical_hash(document: dict[str, Any]) -> None:
    claimed = require_sha256(document["canonical_hash"], "canonical_hash")
    unhashed = dict(document)
    unhashed.pop("canonical_hash")
    if canonical_sha256(unhashed) != claimed:
        raise AutonomyContractError("transfer document canonical hash mismatch")


def _inventory_difference(expected: list[dict[str, Any]], observed: list[dict[str, Any]]) -> str:
    expected_map = {item["path"]: item for item in expected}
    observed_map = {item["path"]: item for item in observed}
    missing = sorted(set(expected_map) - set(observed_map))
    unexpected = sorted(set(observed_map) - set(expected_map))
    changed = sorted(
        path
        for path in set(expected_map) & set(observed_map)
        if expected_map[path] != observed_map[path]
    )
    return (
        "destination evidence bundle differs: "
        f"missing={missing}, unexpected={unexpected}, changed={changed}"
    )


def _relative_if_within(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    value = relative.as_posix()
    _validate_relative_path(value)
    return value


def _validate_relative_path(value: str) -> None:
    candidate = Path(value)
    if not value or "\\" in value or candidate.is_absolute() or ".." in candidate.parts:
        raise AutonomyContractError(f"invalid portable evidence path: {value!r}")


def _regular_directory(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_dir():
        raise AutonomyContractError(f"{label} must be a regular non-symlink directory")
    return path.resolve(strict=True)


def _regular_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise AutonomyContractError(f"{label} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _require_uuid(value: Any, name: str) -> str:
    text = require_text(value, name)
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise AutonomyContractError(f"{name} must be a UUID") from exc
    return str(parsed)


def _format_utc(value: datetime, name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AutonomyContractError(f"{name} must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: Any, name: str) -> datetime:
    text = require_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyContractError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AutonomyContractError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Snapshot and verify a byte-exact evidence transfer"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="create the source-side inventory")
    snapshot.add_argument("bundle_root")
    snapshot.add_argument("inventory_out")
    snapshot.add_argument("--bundle-id")
    verify = subparsers.add_parser(
        "verify", help="verify the copied destination and save a receipt"
    )
    verify.add_argument("inventory")
    verify.add_argument("destination_root")
    verify.add_argument("verification_out")
    receipt = subparsers.add_parser("verify-receipt", help="verify a saved destination receipt")
    receipt.add_argument("receipt")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "snapshot":
        result = create_evidence_transfer_inventory(
            args.bundle_root, args.inventory_out, bundle_id=args.bundle_id
        )
    elif args.command == "verify":
        result = verify_evidence_bundle_transfer(
            args.inventory, args.destination_root, args.verification_out
        )
    else:
        result = verify_evidence_bundle_transfer_receipt(args.receipt)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
