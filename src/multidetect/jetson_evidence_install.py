"""Verify a source-bound receipt for an isolated Jetson evidence-tool install."""

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
from .jetson_evidence_package import verify_jetson_evidence_package

_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")


def verify_jetson_evidence_installation(receipt: str | Path) -> dict[str, Any]:
    """Reopen the install receipt, remote output and exact source package."""

    receipt_path = _regular_file(receipt, "Jetson evidence install receipt")
    receipt_payload = receipt_path.read_bytes()
    raw = strict_json_loads(receipt_payload, name="Jetson evidence install receipt")
    required = frozenset(
        {
            "schema_version",
            "event",
            "installed_at_utc",
            "deployment_id",
            "host_alias",
            "remote_target",
            "runtime_python",
            "source_git_head",
            "package_receipt",
            "archive_sha256",
            "remote_output",
            "remote_checks",
            "service_restart_requested",
            "sudo_used",
            "remote_install_verified",
            "hardware_acceptance",
            "release_ready",
        }
    )
    document = strict_object(raw, name="JetsonEvidenceToolsInstallation", required=required)
    require_schema_version(document["schema_version"])
    if document["event"] != "jetson_evidence_tools_installed":
        raise AutonomyContractError("Jetson evidence install receipt event is invalid")
    _require_timestamp(document["installed_at_utc"])
    deployment_id = require_text(document["deployment_id"], "deployment_id")
    if _DEPLOYMENT_ID.fullmatch(deployment_id) is None:
        raise AutonomyContractError("Jetson evidence install deployment ID is invalid")
    host_alias = require_text(document["host_alias"], "host_alias")
    if re.fullmatch(r"[A-Za-z0-9._:-]+", host_alias) is None:
        raise AutonomyContractError("Jetson evidence install host alias is invalid")
    remote_target = _require_posix_absolute(document["remote_target"], "remote_target")
    runtime_python = _require_posix_absolute(document["runtime_python"], "runtime_python")
    source_head = require_text(document["source_git_head"], "source_git_head")
    if _GIT_HEAD.fullmatch(source_head) is None:
        raise AutonomyContractError("Jetson evidence install source Git HEAD is invalid")

    package_ref = _artifact_reference(document["package_receipt"], "package_receipt")
    package_path = _regular_file(
        receipt_path.parent / package_ref["file_name"], "bound Jetson package receipt"
    )
    _verify_bound_file(package_path, package_ref, "Jetson package receipt")
    package = verify_jetson_evidence_package(package_path)
    if package["package_only"]:
        raise AutonomyContractError("installed Jetson package receipt must not be package-only")
    if package["deployment_id"] != deployment_id or package["source_git_head"] != source_head:
        raise AutonomyContractError("Jetson install and package source identity differ")
    if package["archive_file_sha256"] != require_sha256(
        document["archive_sha256"], "archive_sha256"
    ):
        raise AutonomyContractError("Jetson install archive identity differs from package")
    expected_target = f"{package['remote_tools_root'].rstrip('/')}/{deployment_id}"
    if remote_target != expected_target or runtime_python != package["runtime_python"]:
        raise AutonomyContractError("Jetson install target or Python differs from package")

    remote_ref = strict_object(
        document["remote_output"],
        name="remote_output",
        required=frozenset(
            {"file_name", "file_bytes", "file_sha256", "ready_marker", "run_marker_present"}
        ),
    )
    remote_artifact = _artifact_reference(remote_ref, "remote_output", allow_extra=True)
    remote_path = _regular_file(
        receipt_path.parent / remote_artifact["file_name"], "bound remote deployment output"
    )
    _verify_bound_file(remote_path, remote_artifact, "remote deployment output")
    try:
        remote_text = remote_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise AutonomyContractError("remote deployment output must be UTF-8") from exc
    lines = remote_text.splitlines()
    expected_ready = f"JETSON_EVIDENCE_TOOLS_READY={remote_target}"
    claimed_ready = require_text(remote_ref["ready_marker"], "remote_output.ready_marker")
    ready_lines = [line for line in lines if line.startswith("JETSON_EVIDENCE_TOOLS_READY=")]
    run_lines = [line for line in lines if line.startswith("JETSON_EVIDENCE_RUN=")]
    if ready_lines != [expected_ready] or claimed_ready != expected_ready:
        raise AutonomyContractError("Jetson remote ready marker is missing, duplicated or changed")
    run_marker_present = require_bool(
        remote_ref["run_marker_present"], "remote_output.run_marker_present"
    )
    if run_marker_present is not True:
        raise AutonomyContractError("Jetson install receipt must bind the run marker")
    if len(run_lines) != 1:
        raise AutonomyContractError("Jetson remote run marker is missing or duplicated")

    checks = strict_object(
        document["remote_checks"],
        name="remote_checks",
        required=frozenset(
            {
                "package_manifest_verified",
                "python_compileall_passed",
                "bash_syntax_passed",
                "module_imports_passed",
                "recorder_executable",
                "atomic_install_completed",
            }
        ),
    )
    for name, value in checks.items():
        if require_bool(value, f"remote_checks.{name}") is not True:
            raise AutonomyContractError(f"Jetson remote check must pass: {name}")
    for field in ("service_restart_requested", "sudo_used", "hardware_acceptance", "release_ready"):
        if require_bool(document[field], field):
            raise AutonomyContractError(f"Jetson install {field} must remain false")
    if require_bool(document["remote_install_verified"], "remote_install_verified") is not True:
        raise AutonomyContractError("Jetson install receipt must assert remote installation")

    return {
        "event": "jetson_evidence_tools_installation_verified",
        "receipt_file_sha256": hashlib.sha256(receipt_payload).hexdigest(),
        "receipt_file_bytes": len(receipt_payload),
        "deployment_id": deployment_id,
        "host_alias": host_alias,
        "remote_target": remote_target,
        "source_git_head": source_head,
        "package_receipt_sha256": package_ref["file_sha256"],
        "archive_sha256": package["archive_file_sha256"],
        "remote_output_sha256": remote_artifact["file_sha256"],
        "package_verified": True,
        "remote_markers_verified": True,
        "all_remote_checks_passed": True,
        "remote_install_verified": True,
        "hardware_acceptance": False,
        "release_ready": False,
        "passed": True,
    }


def _artifact_reference(value: Any, name: str, *, allow_extra: bool = False) -> dict[str, Any]:
    required = frozenset({"file_name", "file_bytes", "file_sha256"})
    if allow_extra:
        item = dict(value) if isinstance(value, dict) else value
        if not isinstance(item, dict) or not required.issubset(item):
            raise AutonomyContractError(f"{name} must contain a complete artifact reference")
    else:
        item = strict_object(value, name=name, required=required)
    file_name = require_text(item["file_name"], f"{name}.file_name")
    if Path(file_name).name != file_name:
        raise AutonomyContractError(f"{name}.file_name must be a sibling file name")
    return {
        "file_name": file_name,
        "file_bytes": require_int(item["file_bytes"], f"{name}.file_bytes", minimum=1),
        "file_sha256": require_sha256(item["file_sha256"], f"{name}.file_sha256"),
    }


def _verify_bound_file(path: Path, reference: dict[str, Any], name: str) -> None:
    if path.stat().st_size != reference["file_bytes"]:
        raise AutonomyContractError(f"{name} byte count changed")
    if _sha256_file(path) != reference["file_sha256"]:
        raise AutonomyContractError(f"{name} SHA-256 changed")


def _require_timestamp(value: Any) -> None:
    text = require_text(value, "installed_at_utc")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutonomyContractError("Jetson install timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutonomyContractError("Jetson install timestamp must include timezone")


def _require_posix_absolute(value: Any, name: str) -> str:
    text = require_text(value, name)
    path = PurePosixPath(text)
    if not path.is_absolute() or ".." in path.parts:
        raise AutonomyContractError(f"{name} must be a safe absolute POSIX path")
    return text.rstrip("/") or "/"


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
        description="Verify a source-bound isolated Jetson evidence-tool installation receipt"
    )
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_jetson_evidence_installation(args.receipt)
    except (AutonomyContractError, OSError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(f"Jetson evidence installation verification failed: {exc}") from exc
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["verify_jetson_evidence_installation"]
