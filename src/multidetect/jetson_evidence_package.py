"""Independently verify an isolated Jetson evidence-tool deployment package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

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

_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")
_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._/-]+)$")


def verify_jetson_evidence_package(receipt: str | Path) -> dict[str, Any]:
    """Verify receipt, archive topology and every packaged source byte."""

    receipt_path = _regular_file(receipt, "Jetson evidence package receipt")
    receipt_payload = receipt_path.read_bytes()
    raw = strict_json_loads(receipt_payload, name="Jetson evidence package receipt")
    required = frozenset(
        {
            "schema_version",
            "event",
            "deployment_id",
            "created_at_utc",
            "source_git_head",
            "source_dirty",
            "archive_name",
            "archive_bytes",
            "archive_sha256",
            "file_count",
            "source_manifest_sha256",
            "package_self_verified",
            "verified_file_count",
            "remote_tools_root",
            "runtime_python",
            "package_only",
            "service_restart_requested",
            "sudo_used",
            "release_ready",
        }
    )
    document = strict_object(raw, name="JetsonEvidenceToolsPackage", required=required)
    require_schema_version(document["schema_version"])
    if document["event"] != "jetson_evidence_capture_package_created":
        raise AutonomyContractError("Jetson evidence package receipt event is invalid")
    deployment_id = require_text(document["deployment_id"], "deployment_id")
    if _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
        raise AutonomyContractError("Jetson evidence deployment ID is invalid")
    _require_timestamp(document["created_at_utc"])
    source_head = require_text(document["source_git_head"], "source_git_head")
    if _GIT_HEAD.fullmatch(source_head) is None:
        raise AutonomyContractError("Jetson evidence package source Git HEAD is invalid")
    source_dirty = require_bool(document["source_dirty"], "source_dirty")
    archive_name = require_text(document["archive_name"], "archive_name")
    if Path(archive_name).name != archive_name or not archive_name.endswith(".tar"):
        raise AutonomyContractError("Jetson evidence package archive name is invalid")
    archive_path = _regular_file(receipt_path.parent / archive_name, "Jetson evidence archive")
    archive_payload_bytes = archive_path.stat().st_size
    if require_int(document["archive_bytes"], "archive_bytes", minimum=1) != archive_payload_bytes:
        raise AutonomyContractError("Jetson evidence package archive byte count changed")
    archive_hash = _sha256_file(archive_path)
    if require_sha256(document["archive_sha256"], "archive_sha256") != archive_hash:
        raise AutonomyContractError("Jetson evidence package archive SHA-256 changed")
    file_count = require_int(document["file_count"], "file_count", minimum=1)
    manifest_hash = require_sha256(document["source_manifest_sha256"], "source_manifest_sha256")
    if require_bool(document["package_self_verified"], "package_self_verified") is not True:
        raise AutonomyContractError("Jetson evidence package must be generator-self-verified")
    if require_int(document["verified_file_count"], "verified_file_count", minimum=1) != file_count:
        raise AutonomyContractError("Jetson evidence package verified file count mismatch")
    _require_posix_absolute(document["remote_tools_root"], "remote_tools_root")
    _require_posix_absolute(document["runtime_python"], "runtime_python")
    package_only = require_bool(document["package_only"], "package_only")
    for field in ("service_restart_requested", "sudo_used", "release_ready"):
        if require_bool(document[field], field):
            raise AutonomyContractError(f"Jetson evidence package {field} must remain false")

    members, file_members = _closed_tar_members(archive_path)
    manifest_member = file_members.get("source-sha256.txt")
    if manifest_member is None:
        raise AutonomyContractError("Jetson evidence package source manifest is missing")
    with tarfile.open(archive_path, mode="r:*") as archive:
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise AutonomyContractError("Jetson evidence package source manifest is unreadable")
        manifest_payload = manifest_stream.read()
        if hashlib.sha256(manifest_payload).hexdigest() != manifest_hash:
            raise AutonomyContractError("Jetson evidence package source manifest SHA-256 changed")
        expected = _parse_manifest(manifest_payload)
        if len(expected) != file_count:
            raise AutonomyContractError("Jetson evidence package source manifest count mismatch")
        actual_paths = set(file_members) - {"source-sha256.txt"}
        if actual_paths != set(expected):
            missing = sorted(set(expected) - actual_paths)
            unexpected = sorted(actual_paths - set(expected))
            raise AutonomyContractError(
                "Jetson evidence archive inventory differs: "
                f"missing={missing}, unexpected={unexpected}"
            )
        total_source_bytes = 0
        for path, expected_hash in expected.items():
            stream = archive.extractfile(file_members[path])
            if stream is None:
                raise AutonomyContractError(f"Jetson evidence package file is unreadable: {path}")
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            if digest.hexdigest() != expected_hash:
                raise AutonomyContractError(
                    f"Jetson evidence package file SHA-256 mismatch: {path}"
                )
            total_source_bytes += size

    return {
        "event": "jetson_evidence_capture_package_verified",
        "receipt_file_sha256": hashlib.sha256(receipt_payload).hexdigest(),
        "receipt_file_bytes": len(receipt_payload),
        "archive_file_sha256": archive_hash,
        "archive_file_bytes": archive_payload_bytes,
        "deployment_id": deployment_id,
        "source_git_head": source_head,
        "source_dirty": source_dirty,
        "package_only": package_only,
        "remote_tools_root": require_text(document["remote_tools_root"], "remote_tools_root"),
        "runtime_python": require_text(document["runtime_python"], "runtime_python"),
        "tar_member_count": len(members),
        "verified_source_file_count": file_count,
        "verified_source_bytes": total_source_bytes,
        "path_safety_verified": True,
        "closed_inventory_verified": True,
        "all_source_hashes_verified": True,
        "remote_install_verified": False,
        "hardware_acceptance": False,
        "release_ready": False,
        "passed": True,
    }


def _closed_tar_members(
    archive_path: Path,
) -> tuple[list[tarfile.TarInfo], dict[str, tarfile.TarInfo]]:
    with tarfile.open(archive_path, mode="r:*") as archive:
        members = archive.getmembers()
    if not members:
        raise AutonomyContractError("Jetson evidence archive is empty")
    seen: set[str] = set()
    files: dict[str, tarfile.TarInfo] = {}
    for member in members:
        path = _portable_member_path(member.name)
        if path is None:
            if member.isdir():
                continue
            raise AutonomyContractError("Jetson evidence archive root entry must be a directory")
        folded = path.casefold()
        if folded in seen:
            raise AutonomyContractError("Jetson evidence archive contains duplicate paths")
        seen.add(folded)
        if member.isdir():
            continue
        if not member.isfile():
            raise AutonomyContractError(
                f"Jetson evidence archive contains link or special entry: {path}"
            )
        files[path] = member
    return members, files


def _portable_member_path(value: str) -> str | None:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in {"", "."}:
        return None
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or any(part in {"", "."} for part in path.parts):
        raise AutonomyContractError(f"unsafe path in Jetson evidence archive: {value}")
    return path.as_posix()


def _parse_manifest(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AutonomyContractError("Jetson evidence source manifest must be ASCII") from exc
    expected: dict[str, str] = {}
    seen: set[str] = set()
    for line in text.splitlines():
        match = _MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise AutonomyContractError("Jetson evidence source manifest line is invalid")
        digest, raw_path = match.groups()
        path = _portable_member_path(raw_path)
        if path is None or path == "source-sha256.txt":
            raise AutonomyContractError("Jetson evidence source manifest path is invalid")
        folded = path.casefold()
        if folded in seen:
            raise AutonomyContractError(
                "Jetson evidence source manifest paths must be unique without case collisions"
            )
        seen.add(folded)
        expected[path] = digest
    if not expected:
        raise AutonomyContractError("Jetson evidence source manifest is empty")
    return expected


def _require_timestamp(value: Any) -> None:
    text = require_text(value, "created_at_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyContractError("Jetson evidence package timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomyContractError("Jetson evidence package timestamp must include timezone")


def _require_posix_absolute(value: Any, name: str) -> str:
    text = require_text(value, name)
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise AutonomyContractError(f"{name} must be a safe absolute POSIX path")
    return text


def _regular_file(value: str | Path, name: str) -> Path:
    path = Path(value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise AutonomyContractError(f"{name} must be a regular non-symlink file")
    return path.resolve(strict=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independently verify a Jetson evidence-tool tar and package receipt"
    )
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_jetson_evidence_package(args.receipt)
    except (AutonomyContractError, OSError, tarfile.TarError, ValueError) as exc:
        raise SystemExit(f"Jetson evidence package verification failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["verify_jetson_evidence_package"]
