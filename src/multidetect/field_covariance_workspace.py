"""Portable, hash-bound workspace bundles for field covariance campaigns.

The bundle moves only static campaign inputs.  Live Pixhawk identity and
covariance observations are deliberately excluded: after relocation the
receiving machine must run its own ``preflight -> probe-next -> capture-next``
sequence against its local transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .compat import UTC
from .field_covariance_campaign import (
    REQUIRED_COMBINATIONS,
    CampaignConfig,
    campaign_preflight,
    campaign_status,
    load_campaign_config,
)

SCHEMA = "multidetect.pixhawk-covariance-field-workspace/v1"
SCHEMA_VERSION = 1
RECEIPT_SCHEMA = "multidetect.pixhawk-covariance-field-workspace-receipt/v1"
RECEIPT_SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "pixhawk-covariance-field-workspace.schema.json"
RECEIPT_SCHEMA_PATH = (
    ROOT / "configs" / "schemas" / "pixhawk-covariance-field-workspace-receipt.schema.json"
)
PARITY_PATH = ROOT / "configs" / "release-parity.json"
MANIFEST_NAME = "workspace.manifest.json"
CAMPAIGN_NAME = "campaign.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


class FieldCovarianceWorkspaceError(ValueError):
    """Raised when a portable field workspace cannot be trusted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
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
        raise FieldCovarianceWorkspaceError(f"cannot read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FieldCovarianceWorkspaceError(f"{name} must be a JSON object")
    return value


def _validate_document(document: Mapping[str, object], *, schema_path: Path, name: str) -> None:
    schema = _json_object(schema_path, name=f"{name} JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise FieldCovarianceWorkspaceError(f"{name} schema validation failed: {exc}") from exc


def _validate_schema(document: Mapping[str, object]) -> None:
    _validate_document(document, schema_path=SCHEMA_PATH, name="workspace")


def _validate_receipt_schema(document: Mapping[str, object]) -> None:
    _validate_document(document, schema_path=RECEIPT_SCHEMA_PATH, name="workspace receipt")


def _relative_path(value: object, *, name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise FieldCovarianceWorkspaceError(f"{name} must be a normalized POSIX relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or str(path) != value
        or any(
            part in {"", ".", ".."} or _PATH_SEGMENT.fullmatch(part) is None for part in path.parts
        )
    ):
        raise FieldCovarianceWorkspaceError(f"{name} escapes the workspace")
    return path


def _bundle_file(root: Path, value: object, *, name: str) -> Path:
    relative = _relative_path(value, name=name)
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise FieldCovarianceWorkspaceError(f"{name} cannot be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FieldCovarianceWorkspaceError(f"{name} cannot be resolved: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FieldCovarianceWorkspaceError(f"{name} escapes the workspace") from exc
    if not resolved.is_file():
        raise FieldCovarianceWorkspaceError(f"{name} must be a regular file")
    return resolved


def _current_parity() -> tuple[str, str, str]:
    parity = _json_object(PARITY_PATH, name="release parity")
    release_label = parity.get("release_label")
    hashes = parity.get("parity")
    px4_hash = hashes.get("px4_mission_path_hash") if isinstance(hashes, Mapping) else None
    if (
        not isinstance(release_label, str)
        or not release_label
        or not isinstance(px4_hash, str)
        or _SHA256.fullmatch(px4_hash) is None
    ):
        raise FieldCovarianceWorkspaceError("release parity has no valid PX4 Mission identity")
    return release_label, px4_hash.lower(), _sha256(PARITY_PATH)


def _source_record(path: Path, relative: str) -> dict[str, object]:
    return {
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _workspace_entry(
    leaf: object, source_root: Path
) -> tuple[dict[str, object], dict[str, object]]:
    mode = leaf.runtime_mode
    vehicle = leaf.vehicle_class
    prefix = PurePosixPath("sources", mode, vehicle)
    relative = {
        "mission": str(prefix / "mission.json"),
        "mission_feedback": str(prefix / "mission-feedback.json"),
        "runtime_profile": str(prefix / "runtime-profile.json"),
    }
    sources = {
        "mission": _source_record(leaf.mission_path, relative["mission"]),
        "mission_feedback": _source_record(
            leaf.mission_feedback_path, relative["mission_feedback"]
        ),
        "runtime_profile": _source_record(leaf.runtime_profile_path, relative["runtime_profile"]),
    }
    campaign_entry = {
        "runtime_mode": mode,
        "vehicle_class": vehicle,
        "endpoint": leaf.endpoint,
        "baud": leaf.baud,
        "mission_path": relative["mission"],
        "mission_feedback_path": relative["mission_feedback"],
        "runtime_profile_path": relative["runtime_profile"],
        "report_path": str(PurePosixPath("reports", mode, f"{vehicle}.json")),
    }
    manifest_entry = {
        "runtime_mode": mode,
        "vehicle_class": vehicle,
        "endpoint": leaf.endpoint,
        "baud": leaf.baud,
        "sources": sources,
        "report_path": campaign_entry["report_path"],
    }
    for name, record in sources.items():
        destination = source_root.joinpath(*PurePosixPath(record["path"]).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(getattr(leaf, f"{name}_path"), destination)
    return campaign_entry, manifest_entry


def _preflight_summary(preflight: Mapping[str, object]) -> dict[str, object]:
    return {
        "schema": preflight.get("schema"),
        "schema_version": preflight.get("schema_version"),
        "ready_count": preflight.get("ready_count"),
        "blocked_count": preflight.get("blocked_count"),
        "gate_passed": preflight.get("gate_passed"),
    }


def export_field_covariance_workspace(
    config: CampaignConfig,
    destination: str | Path,
    *,
    created_at: datetime | None = None,
) -> Path:
    """Export six qualified static leaves into one atomic portable directory."""

    if not isinstance(config, CampaignConfig):
        raise FieldCovarianceWorkspaceError("config must be a loaded CampaignConfig")
    preflight = campaign_preflight(config)
    if preflight.get("gate_passed") is not True:
        raise FieldCovarianceWorkspaceError("source preflight blocked workspace export")
    release_label, px4_hash, parity_sha256 = _current_parity()
    if config.release_label != release_label:
        raise FieldCovarianceWorkspaceError("campaign release label is not current")
    timestamp = datetime.now(UTC) if created_at is None else created_at
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise FieldCovarianceWorkspaceError("created_at must be timezone-aware")

    requested = Path(destination).expanduser()
    if requested.is_symlink() or requested.exists():
        raise FieldCovarianceWorkspaceError(f"workspace destination already exists: {requested}")
    requested.parent.mkdir(parents=True, exist_ok=True)
    parent = requested.parent.resolve(strict=True)
    output = parent / requested.name
    temporary = Path(tempfile.mkdtemp(prefix=f".{requested.name}.", dir=parent))
    try:
        campaign_entries: list[dict[str, object]] = []
        manifest_entries: list[dict[str, object]] = []
        for leaf in config.leaves:
            campaign_entry, manifest_entry = _workspace_entry(leaf, temporary)
            campaign_entries.append(campaign_entry)
            manifest_entries.append(manifest_entry)
        campaign_document = {
            "schema": "multidetect.pixhawk-covariance-field-campaign/v1",
            "schema_version": 1,
            "release_label": release_label,
            "entries": campaign_entries,
        }
        campaign_path = temporary / CAMPAIGN_NAME
        campaign_path.write_text(
            json.dumps(campaign_document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest: dict[str, object] = {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "release_label": release_label,
            "created_at_utc": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "campaign": _source_record(campaign_path, CAMPAIGN_NAME),
            "source_campaign_sha256": _sha256(config.path),
            "entries": manifest_entries,
            "source_preflight": _preflight_summary(preflight),
            "release_parity": {
                "path": "configs/release-parity.json",
                "file_sha256": parity_sha256,
                "px4_mission_path_hash": px4_hash,
            },
            "claims": {
                "live_identity_observed": False,
                "covariance_observed": False,
                "hardware_acceptance": False,
                "hil_acceptance": False,
                "real_acceptance": False,
                "flight_acceptance": False,
            },
        }
        manifest["workspace_sha256"] = _canonical_sha256(manifest)
        _validate_schema(manifest)
        (temporary / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        verify_field_covariance_workspace(temporary)
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output


def _expected_files(manifest: Mapping[str, object]) -> set[str]:
    expected = {MANIFEST_NAME, CAMPAIGN_NAME}
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise FieldCovarianceWorkspaceError("workspace entries must be an array")
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise FieldCovarianceWorkspaceError(f"workspace entry {index} must be an object")
        sources = entry.get("sources")
        if not isinstance(sources, Mapping):
            raise FieldCovarianceWorkspaceError(f"workspace entry {index} has no sources")
        for name in ("mission", "mission_feedback", "runtime_profile"):
            source = sources.get(name)
            if not isinstance(source, Mapping):
                raise FieldCovarianceWorkspaceError(f"workspace entry {index}.{name} is invalid")
            expected.add(str(_relative_path(source.get("path"), name=f"entry {index}.{name}")))
    return expected


def verify_field_covariance_workspace(
    path: str | Path, *, allow_campaign_reports: bool = False
) -> dict[str, object]:
    """Verify static bytes and optionally the campaign's declared generated reports."""

    if not isinstance(allow_campaign_reports, bool):
        raise FieldCovarianceWorkspaceError("allow_campaign_reports must be boolean")

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceWorkspaceError("workspace root cannot be a symbolic link")
    root = candidate.resolve(strict=True)
    if not root.is_dir():
        raise FieldCovarianceWorkspaceError("workspace root must be a directory")
    manifest_path = _bundle_file(root, MANIFEST_NAME, name="workspace manifest")
    manifest = _json_object(manifest_path, name="workspace manifest")
    _validate_schema(manifest)
    claimed_hash = manifest["workspace_sha256"]
    without_hash = dict(manifest)
    del without_hash["workspace_sha256"]
    if claimed_hash != _canonical_sha256(without_hash):
        raise FieldCovarianceWorkspaceError("workspace digest mismatch")

    expected_files = _expected_files(manifest)
    actual_files: set[str] = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise FieldCovarianceWorkspaceError("workspace cannot contain symbolic links")
        if item.is_file():
            actual_files.add(item.relative_to(root).as_posix())
    report_paths = {
        str(_relative_path(entry["report_path"], name="campaign report"))
        for entry in manifest["entries"]
    }
    allowed_files = expected_files | (report_paths if allow_campaign_reports else set())
    if not expected_files.issubset(actual_files) or not actual_files.issubset(allowed_files):
        raise FieldCovarianceWorkspaceError(
            f"workspace file set mismatch: missing={sorted(expected_files - actual_files)!r}, "
            f"unexpected={sorted(actual_files - allowed_files)!r}"
        )

    release_label, px4_hash, parity_sha256 = _current_parity()
    if manifest["release_label"] != release_label or manifest["release_parity"] != {
        "path": "configs/release-parity.json",
        "file_sha256": parity_sha256,
        "px4_mission_path_hash": px4_hash,
    }:
        raise FieldCovarianceWorkspaceError("workspace release parity is not current")

    campaign_record = manifest["campaign"]
    campaign_path = _bundle_file(root, campaign_record["path"], name="portable campaign")
    if (
        campaign_path.stat().st_size != campaign_record["bytes"]
        or _sha256(campaign_path) != campaign_record["sha256"]
    ):
        raise FieldCovarianceWorkspaceError("portable campaign hash or size mismatch")
    config = load_campaign_config(campaign_path)
    if config.release_label != release_label:
        raise FieldCovarianceWorkspaceError("portable campaign release label is not current")

    entries = manifest["entries"]
    by_key = {(entry["runtime_mode"], entry["vehicle_class"]): entry for entry in entries}
    if tuple(by_key) != REQUIRED_COMBINATIONS:
        raise FieldCovarianceWorkspaceError("workspace leaf order or coverage is not exact")
    for leaf in config.leaves:
        entry = by_key[leaf.key]
        if entry["endpoint"] != leaf.endpoint or entry["baud"] != leaf.baud:
            raise FieldCovarianceWorkspaceError(
                f"{leaf.runtime_mode}/{leaf.vehicle_class} transport mismatch"
            )
        for name, source_path in (
            ("mission", leaf.mission_path),
            ("mission_feedback", leaf.mission_feedback_path),
            ("runtime_profile", leaf.runtime_profile_path),
        ):
            record = entry["sources"][name]
            expected_path = _bundle_file(root, record["path"], name=f"{leaf.key}.{name}")
            if expected_path != source_path:
                raise FieldCovarianceWorkspaceError(f"{leaf.key}.{name} path mismatch")
            if (
                source_path.stat().st_size != record["bytes"]
                or _sha256(source_path) != record["sha256"]
            ):
                raise FieldCovarianceWorkspaceError(f"{leaf.key}.{name} hash or size mismatch")
        try:
            report_relative = leaf.report_path.relative_to(root).as_posix()
        except ValueError as exc:
            raise FieldCovarianceWorkspaceError(f"{leaf.key} report escapes workspace") from exc
        if entry["report_path"] != report_relative:
            raise FieldCovarianceWorkspaceError(f"{leaf.key} report path mismatch")

    preflight = campaign_preflight(config)
    if preflight.get("gate_passed") is not True:
        raise FieldCovarianceWorkspaceError("portable workspace source preflight is blocked")
    if _preflight_summary(preflight) != manifest["source_preflight"]:
        raise FieldCovarianceWorkspaceError("portable workspace preflight summary mismatch")
    status = campaign_status(config)
    if status["failed_count"]:
        raise FieldCovarianceWorkspaceError("portable workspace has a failed campaign report")
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "workspace_root": str(root),
        "campaign_path": str(campaign_path),
        "release_label": release_label,
        "workspace_sha256": claimed_hash,
        "leaf_count": len(config.leaves),
        "source_file_count": len(expected_files) - 2,
        "campaign_report_count": status["passed_count"],
        "gate_passed": True,
        "next_command": (
            f"python -m multidetect.field_covariance_campaign preflight --config {campaign_path}"
        ),
        "claims": dict(manifest["claims"]),
    }


def _require_new_path(path: str | Path, *, name: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink() or requested.exists():
        raise FieldCovarianceWorkspaceError(f"{name} already exists: {requested}")
    return requested.resolve(strict=False)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _write_new_json(path: Path, document: Mapping[str, object]) -> None:
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


def receive_field_covariance_workspace(
    source: str | Path,
    destination: str | Path,
    receipt_path: str | Path,
    *,
    received_at: datetime | None = None,
) -> dict[str, object]:
    """Verify removable media, atomically copy it locally, and write an external receipt."""

    source_result = verify_field_covariance_workspace(source)
    source_root = Path(source_result["workspace_root"])
    output = _require_new_path(destination, name="workspace destination")
    receipt_output = _require_new_path(receipt_path, name="workspace receipt")
    if _is_within(output, source_root):
        raise FieldCovarianceWorkspaceError("workspace destination cannot be inside source")
    if _is_within(receipt_output, source_root) or _is_within(receipt_output, output):
        raise FieldCovarianceWorkspaceError(
            "workspace receipt must remain outside source and destination bundles"
        )
    timestamp = datetime.now(UTC) if received_at is None else received_at
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise FieldCovarianceWorkspaceError("received_at must be timezone-aware")
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt_output.parent.mkdir(parents=True, exist_ok=True)

    manifest = _json_object(source_root / MANIFEST_NAME, name="workspace manifest")
    expected_files = _expected_files(manifest)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    published = False
    try:
        for relative in sorted(expected_files):
            source_file = _bundle_file(source_root, relative, name=f"source file {relative}")
            destination_file = temporary.joinpath(*PurePosixPath(relative).parts)
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination_file)
        temporary_result = verify_field_covariance_workspace(temporary)
        if temporary_result["workspace_sha256"] != source_result["workspace_sha256"]:
            raise FieldCovarianceWorkspaceError("received workspace identity changed during copy")
        os.replace(temporary, output)
        published = True
        destination_result = verify_field_covariance_workspace(output)
        release_label, px4_hash, parity_sha256 = _current_parity()
        campaign_path = output / CAMPAIGN_NAME
        receipt: dict[str, object] = {
            "schema": RECEIPT_SCHEMA,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "received_at_utc": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "source_workspace_root": str(source_root),
            "destination_workspace_root": str(output),
            "workspace_sha256": destination_result["workspace_sha256"],
            "release_label": release_label,
            "release_parity": {
                "path": "configs/release-parity.json",
                "file_sha256": parity_sha256,
                "px4_mission_path_hash": px4_hash,
            },
            "verification": {
                "source_gate_passed": True,
                "destination_gate_passed": True,
                "leaf_count": destination_result["leaf_count"],
                "source_file_count": destination_result["source_file_count"],
                "total_json_file_count": len(expected_files),
            },
            "next_action": {
                "argv": [
                    "python",
                    "-m",
                    "multidetect.field_covariance_campaign",
                    "preflight",
                    f"--config={campaign_path}",
                ]
            },
            "claims": dict(destination_result["claims"]),
        }
        receipt["receipt_sha256"] = _canonical_sha256(receipt)
        _validate_receipt_schema(receipt)
        _write_new_json(receipt_output, receipt)
        return {
            **receipt,
            "receipt_path": str(receipt_output),
            "gate_passed": True,
        }
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        if published:
            shutil.rmtree(output, ignore_errors=True)
        receipt_output.unlink(missing_ok=True)
        raise


def verify_field_covariance_workspace_receipt(path: str | Path) -> dict[str, object]:
    """Re-open a local receipt and its received workspace without requiring source media."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceWorkspaceError("workspace receipt cannot be a symbolic link")
    receipt_path = candidate.resolve(strict=True)
    if not receipt_path.is_file():
        raise FieldCovarianceWorkspaceError("workspace receipt must be a regular file")
    receipt = _json_object(receipt_path, name="workspace receipt")
    _validate_receipt_schema(receipt)
    claimed_hash = receipt["receipt_sha256"]
    without_hash = dict(receipt)
    del without_hash["receipt_sha256"]
    if claimed_hash != _canonical_sha256(without_hash):
        raise FieldCovarianceWorkspaceError("workspace receipt digest mismatch")
    destination_value = receipt["destination_workspace_root"]
    destination = Path(destination_value)
    if not destination.is_absolute() or str(destination) != destination_value:
        raise FieldCovarianceWorkspaceError("receipt destination must be an absolute local path")
    result = verify_field_covariance_workspace(destination, allow_campaign_reports=True)
    release_label, px4_hash, parity_sha256 = _current_parity()
    if (
        result["workspace_sha256"] != receipt["workspace_sha256"]
        or result["release_label"] != receipt["release_label"]
        or receipt["release_parity"]
        != {
            "path": "configs/release-parity.json",
            "file_sha256": parity_sha256,
            "px4_mission_path_hash": px4_hash,
        }
        or release_label != receipt["release_label"]
    ):
        raise FieldCovarianceWorkspaceError("workspace receipt no longer matches current state")
    expected_argv = [
        "python",
        "-m",
        "multidetect.field_covariance_campaign",
        "preflight",
        f"--config={destination / CAMPAIGN_NAME}",
    ]
    if receipt["next_action"]["argv"] != expected_argv or receipt["claims"] != result["claims"]:
        raise FieldCovarianceWorkspaceError("workspace receipt derived fields do not match")
    return {
        **receipt,
        "receipt_path": str(receipt_path),
        "gate_passed": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--config", required=True, type=Path)
    export.add_argument("--out", required=True, type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle", required=True, type=Path)
    receive = subparsers.add_parser("receive")
    receive.add_argument("--bundle", required=True, type=Path)
    receive.add_argument("--out", required=True, type=Path)
    receive.add_argument("--receipt", required=True, type=Path)
    verify_receipt = subparsers.add_parser("verify-receipt")
    verify_receipt.add_argument("--receipt", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            root = export_field_covariance_workspace(load_campaign_config(args.config), args.out)
            result = verify_field_covariance_workspace(root)
        elif args.command == "verify":
            result = verify_field_covariance_workspace(args.bundle)
        elif args.command == "receive":
            result = receive_field_covariance_workspace(args.bundle, args.out, args.receipt)
        else:
            result = verify_field_covariance_workspace_receipt(args.receipt)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (FieldCovarianceWorkspaceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CAMPAIGN_NAME",
    "FieldCovarianceWorkspaceError",
    "MANIFEST_NAME",
    "RECEIPT_SCHEMA",
    "RECEIPT_SCHEMA_PATH",
    "RECEIPT_SCHEMA_VERSION",
    "SCHEMA",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "export_field_covariance_workspace",
    "receive_field_covariance_workspace",
    "verify_field_covariance_workspace",
    "verify_field_covariance_workspace_receipt",
]
