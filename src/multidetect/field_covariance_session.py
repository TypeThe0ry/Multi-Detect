"""Run and verify one receipt-bound Pixhawk covariance field capture transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .compat import UTC
from .field_covariance_campaign import (
    DEFAULT_INTERVAL_S,
    DEFAULT_MINIMUM_COVARIANCES,
    DEFAULT_MINIMUM_ODOMETRY,
    DEFAULT_PROBE_INTERVAL_S,
    DEFAULT_PROBE_SAMPLES,
    DEFAULT_SAMPLES,
    FieldCovarianceCampaignError,
    campaign_status,
    capture_next,
    load_campaign_config,
)
from .field_covariance_journal import (
    CaptureLease,
    acquire_capture_lease,
    verify_capture_lease_document,
)
from .field_covariance_workspace import (
    CAMPAIGN_NAME,
    FieldCovarianceWorkspaceError,
    verify_field_covariance_workspace_receipt,
)
from .online_covariance_preflight import (
    OnlineCovariancePreflightError,
    load_online_covariance_preflight_observation,
)

SCHEMA = "multidetect.pixhawk-covariance-field-session-record/v1"
SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "pixhawk-covariance-field-session-record.schema.json"
_ATTEMPT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class FieldCovarianceSessionError(ValueError):
    """Raised when a single-leaf field session cannot be trusted."""


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
        raise FieldCovarianceSessionError(f"cannot read {name}: {exc}") from exc
    if not isinstance(value, dict):
        raise FieldCovarianceSessionError(f"{name} must be a JSON object")
    return value


def _validate_schema(document: Mapping[str, object]) -> None:
    schema = _json_object(SCHEMA_PATH, name="field session JSON Schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except Exception as exc:
        raise FieldCovarianceSessionError(f"field session schema validation failed: {exc}") from exc


def _regular_file(value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise FieldCovarianceSessionError(f"{name} path is missing")
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise FieldCovarianceSessionError(f"{name} cannot be a symbolic link")
    try:
        path = candidate.resolve(strict=True)
    except OSError as exc:
        raise FieldCovarianceSessionError(f"{name} cannot be resolved: {exc}") from exc
    if not path.is_file():
        raise FieldCovarianceSessionError(f"{name} must be a regular file")
    return path


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


def _attempt_name(value: str | None, timestamp: datetime) -> str:
    attempt = (
        timestamp.astimezone(UTC).strftime("%Y%m%dt%H%M%S%fz") + "-" + uuid.uuid4().hex[:8]
        if value is None
        else value
    )
    if _ATTEMPT_ID.fullmatch(attempt) is None:
        raise FieldCovarianceSessionError("attempt_id is not a safe file-name token")
    return attempt


def _capture_and_seal(
    *,
    config: object,
    receipt: Mapping[str, object],
    receipt_file: Path,
    campaign_path: Path,
    timestamp: datetime,
    key: tuple[str, str],
    leaf_ordinal: int,
    probe_path: Path,
    record_path: Path,
    lease: CaptureLease,
    samples: int,
    interval_seconds: float,
    minimum_odometry_messages: int,
    minimum_accepted_covariances: int,
    probe_samples: int,
    probe_interval_seconds: float,
) -> tuple[int, dict[str, object]]:
    exit_code, capture = capture_next(
        config,
        samples=samples,
        interval_seconds=interval_seconds,
        minimum_odometry_messages=minimum_odometry_messages,
        minimum_accepted_covariances=minimum_accepted_covariances,
        dry_run=False,
        probe_samples=probe_samples,
        probe_interval_seconds=probe_interval_seconds,
        probe_report_out=probe_path,
    )
    if exit_code != 0 or capture.get("capture_verified") is not True:
        return exit_code or 1, {
            "schema": "multidetect.pixhawk-covariance-field-session-attempt/v1",
            "runtime_mode": key[0],
            "vehicle_class": key[1],
            "leaf_ordinal": leaf_ordinal,
            "capture_lease_id": lease.document["lease_id"],
            "online_preflight_path": str(probe_path) if probe_path.exists() else None,
            "record_written": False,
            "capture": capture,
            "gate_passed": False,
        }

    archived_probe = load_online_covariance_preflight_observation(
        probe_path,
        maximum_age_seconds=None,
        require_current_next_leaf=False,
    )
    after = campaign_status(config)
    report = next(
        item for item in after["records"] if (item["runtime_mode"], item["vehicle_class"]) == key
    )
    if report["status"] != "passed" or int(after["passed_count"]) != leaf_ordinal:
        raise FieldCovarianceSessionError("captured leaf did not become the next verified report")
    leaf = config.leaves[leaf_ordinal - 1]
    if leaf.key != key:
        raise FieldCovarianceSessionError("campaign leaf order changed during capture")
    record: dict[str, object] = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "receipt": {
            "path": str(receipt_file),
            "bytes": receipt_file.stat().st_size,
            "file_sha256": _sha256(receipt_file),
            "receipt_sha256": receipt["receipt_sha256"],
            "workspace_sha256": receipt["workspace_sha256"],
        },
        "capture_lease": lease.document,
        "campaign": {
            "path": str(campaign_path),
            "file_sha256": _sha256(campaign_path),
            "leaf_ordinal": leaf_ordinal,
            "runtime_mode": key[0],
            "vehicle_class": key[1],
        },
        "online_preflight": {
            "path": str(probe_path),
            "bytes": probe_path.stat().st_size,
            "file_sha256": _sha256(probe_path),
            "evidence_sha256": archived_probe["evidence_sha256"],
            "verified_before_capture": capture["online_preflight_verified_before_capture"],
        },
        "covariance_report": {
            "path": str(leaf.report_path),
            "bytes": report["report_bytes"],
            "file_sha256": report["report_sha256"],
            "evidence_sha256": report["evidence_sha256"],
            "records_verified": report["records_verified"],
            "firmware_identity_hash": report["firmware_identity_hash"],
        },
        "capture_result": {
            "capture_process_exit_code": capture["capture_process_exit_code"],
            "capture_verified": True,
            "passed_count_after": after["passed_count"],
            "pending_count_after": after["pending_count"],
            "campaign_complete_after": after["complete"],
        },
        "claims": {
            "release_eligible": False,
            "hardware_acceptance": False,
            "hil_acceptance": False,
            "real_acceptance": False,
            "flight_acceptance": False,
        },
    }
    record["record_sha256"] = _canonical_sha256(record)
    _validate_schema(record)
    try:
        _write_new_json(record_path, record)
        verified = verify_field_covariance_session_record(record_path)
    except BaseException:
        record_path.unlink(missing_ok=True)
        raise
    return 0, verified


def run_next_field_covariance_session(
    receipt_path: str | Path,
    journal_directory: str | Path,
    *,
    samples: int = DEFAULT_SAMPLES,
    interval_seconds: float = DEFAULT_INTERVAL_S,
    minimum_odometry_messages: int = DEFAULT_MINIMUM_ODOMETRY,
    minimum_accepted_covariances: int = DEFAULT_MINIMUM_COVARIANCES,
    probe_samples: int = DEFAULT_PROBE_SAMPLES,
    probe_interval_seconds: float = DEFAULT_PROBE_INTERVAL_S,
    created_at: datetime | None = None,
    attempt_id: str | None = None,
) -> tuple[int, dict[str, object]]:
    """Verify receipt, capture one current leaf, and seal a successful session record."""

    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    receipt_file = _regular_file(receipt["receipt_path"], name="workspace receipt")
    workspace_root = Path(str(receipt["destination_workspace_root"]))
    campaign_path = (workspace_root / CAMPAIGN_NAME).resolve(strict=True)
    config = load_campaign_config(campaign_path)
    before = campaign_status(config)
    if before["failed_count"]:
        raise FieldCovarianceSessionError("campaign has a failed immutable report")
    if before["complete"]:
        raise FieldCovarianceSessionError("campaign is complete")
    next_leaf = before["next_leaf"]
    if not isinstance(next_leaf, Mapping):
        raise FieldCovarianceSessionError("campaign has no safe next leaf")
    key = (str(next_leaf["runtime_mode"]), str(next_leaf["vehicle_class"]))
    leaf_ordinal = int(before["passed_count"]) + 1

    timestamp = datetime.now(UTC) if created_at is None else created_at
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise FieldCovarianceSessionError("created_at must be timezone-aware")
    attempt = _attempt_name(attempt_id, timestamp)
    journal_candidate = Path(journal_directory).expanduser()
    if journal_candidate.is_symlink():
        raise FieldCovarianceSessionError("journal directory cannot be a symbolic link")
    journal = journal_candidate.resolve(strict=False)
    try:
        journal.relative_to(workspace_root)
    except ValueError:
        pass
    else:
        raise FieldCovarianceSessionError("journal directory must remain outside workspace")
    journal.mkdir(parents=True, exist_ok=True)
    if journal.is_symlink() or not journal.is_dir():
        raise FieldCovarianceSessionError("journal path must be a regular directory")
    prefix = f"{leaf_ordinal:02d}-{key[0]}-{key[1]}-{attempt}"
    probe_path = journal / f"{prefix}.online-preflight.json"
    record_path = journal / f"{prefix}.session.json"
    if (
        probe_path.exists()
        or probe_path.is_symlink()
        or record_path.exists()
        or record_path.is_symlink()
    ):
        raise FieldCovarianceSessionError("session artifact already exists")

    with acquire_capture_lease(
        receipt_file,
        journal,
        leaf_ordinal=leaf_ordinal,
        runtime_mode=key[0],
        vehicle_class=key[1],
    ) as lease:
        return _capture_and_seal(
            config=config,
            receipt=receipt,
            receipt_file=receipt_file,
            campaign_path=campaign_path,
            timestamp=timestamp,
            key=key,
            leaf_ordinal=leaf_ordinal,
            probe_path=probe_path,
            record_path=record_path,
            lease=lease,
            samples=samples,
            interval_seconds=interval_seconds,
            minimum_odometry_messages=minimum_odometry_messages,
            minimum_accepted_covariances=minimum_accepted_covariances,
            probe_samples=probe_samples,
            probe_interval_seconds=probe_interval_seconds,
        )


def verify_field_covariance_session_record(path: str | Path) -> dict[str, object]:
    """Re-open one successful session record and all receipt/probe/report sources."""

    record_path = _regular_file(str(path), name="field session record")
    record = _json_object(record_path, name="field session record")
    _validate_schema(record)
    without_hash = dict(record)
    claimed_hash = without_hash.pop("record_sha256")
    if claimed_hash != _canonical_sha256(without_hash):
        raise FieldCovarianceSessionError("field session record digest mismatch")

    receipt_record = record["receipt"]
    receipt_path = _regular_file(receipt_record["path"], name="workspace receipt")
    if (
        receipt_path.stat().st_size != receipt_record["bytes"]
        or _sha256(receipt_path) != receipt_record["file_sha256"]
    ):
        raise FieldCovarianceSessionError("workspace receipt file changed")
    receipt = verify_field_covariance_workspace_receipt(receipt_path)
    if (
        receipt["receipt_sha256"] != receipt_record["receipt_sha256"]
        or receipt["workspace_sha256"] != receipt_record["workspace_sha256"]
    ):
        raise FieldCovarianceSessionError("workspace receipt identity changed")

    lease = verify_capture_lease_document(record["capture_lease"])
    if lease["receipt"] != {
        "path": str(receipt_path),
        "file_sha256": _sha256(receipt_path),
        "receipt_sha256": receipt["receipt_sha256"],
        "workspace_sha256": receipt["workspace_sha256"],
    }:
        raise FieldCovarianceSessionError("session capture lease receipt changed")

    campaign_record = record["campaign"]
    campaign_path = _regular_file(campaign_record["path"], name="campaign")
    expected_campaign = Path(str(receipt["destination_workspace_root"])) / CAMPAIGN_NAME
    if (
        campaign_path != expected_campaign.resolve(strict=True)
        or _sha256(campaign_path) != campaign_record["file_sha256"]
    ):
        raise FieldCovarianceSessionError("session campaign changed")
    config = load_campaign_config(campaign_path)
    ordinal = int(campaign_record["leaf_ordinal"])
    leaf = config.leaves[ordinal - 1]
    key = (campaign_record["runtime_mode"], campaign_record["vehicle_class"])
    if leaf.key != key:
        raise FieldCovarianceSessionError("session leaf no longer matches campaign order")
    if lease["campaign"] != {
        "path": str(campaign_path),
        "file_sha256": _sha256(campaign_path),
        "leaf": {
            "leaf_ordinal": ordinal,
            "runtime_mode": key[0],
            "vehicle_class": key[1],
        },
    }:
        raise FieldCovarianceSessionError("session capture lease campaign changed")

    probe_record = record["online_preflight"]
    probe_path = _regular_file(probe_record["path"], name="online preflight")
    if (
        probe_path.stat().st_size != probe_record["bytes"]
        or _sha256(probe_path) != probe_record["file_sha256"]
    ):
        raise FieldCovarianceSessionError("session online preflight changed")
    probe = load_online_covariance_preflight_observation(
        probe_path,
        maximum_age_seconds=None,
        require_current_next_leaf=False,
    )
    if (
        probe["evidence_sha256"] != probe_record["evidence_sha256"]
        or (probe["runtime_mode"], probe["vehicle_class"]) != key
        or probe["source_binding"]["campaign_config_path"] != str(campaign_path)
    ):
        raise FieldCovarianceSessionError("session online preflight identity changed")

    status = campaign_status(config)
    report = status["records"][ordinal - 1]
    report_record = record["covariance_report"]
    if (
        report["status"] != "passed"
        or Path(report_record["path"]).resolve(strict=True) != leaf.report_path
        or report["report_bytes"] != report_record["bytes"]
        or report["report_sha256"] != report_record["file_sha256"]
        or report["evidence_sha256"] != report_record["evidence_sha256"]
        or report["records_verified"] != report_record["records_verified"]
        or report["firmware_identity_hash"] != report_record["firmware_identity_hash"]
    ):
        raise FieldCovarianceSessionError("session covariance report changed")
    if any(item["status"] != "passed" for item in status["records"][:ordinal]):
        raise FieldCovarianceSessionError("campaign prefix before session leaf is no longer valid")
    result = record["capture_result"]
    if (
        result["passed_count_after"] != ordinal
        or result["pending_count_after"] != 6 - ordinal
        or result["campaign_complete_after"] is not (ordinal == 6)
    ):
        raise FieldCovarianceSessionError("session capture progression is inconsistent")
    return {
        **record,
        "record_path": str(record_path),
        "gate_passed": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-next")
    run.add_argument("--receipt", required=True, type=Path)
    run.add_argument("--journal-dir", required=True, type=Path)
    run.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    run.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_S)
    run.add_argument("--minimum-odometry-messages", type=int, default=DEFAULT_MINIMUM_ODOMETRY)
    run.add_argument(
        "--minimum-accepted-covariances",
        type=int,
        default=DEFAULT_MINIMUM_COVARIANCES,
    )
    run.add_argument("--probe-samples", type=int, default=DEFAULT_PROBE_SAMPLES)
    run.add_argument(
        "--probe-interval-seconds",
        type=float,
        default=DEFAULT_PROBE_INTERVAL_S,
    )
    verify = subparsers.add_parser("verify-record")
    verify.add_argument("--record", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run-next":
            exit_code, result = run_next_field_covariance_session(
                args.receipt,
                args.journal_dir,
                samples=args.samples,
                interval_seconds=args.interval_seconds,
                minimum_odometry_messages=args.minimum_odometry_messages,
                minimum_accepted_covariances=args.minimum_accepted_covariances,
                probe_samples=args.probe_samples,
                probe_interval_seconds=args.probe_interval_seconds,
            )
        else:
            result = verify_field_covariance_session_record(args.record)
            exit_code = 0
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return exit_code
    except (
        FieldCovarianceCampaignError,
        FieldCovarianceSessionError,
        FieldCovarianceWorkspaceError,
        OnlineCovariancePreflightError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FieldCovarianceSessionError",
    "SCHEMA",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "run_next_field_covariance_session",
    "verify_field_covariance_session_record",
]
