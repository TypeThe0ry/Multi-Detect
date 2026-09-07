"""Strict verification for native target-observation recording manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .compat import UTC
from .target_observation_diagnostics import (
    TargetObservationDiagnosticsError,
    load_target_observation_diagnostics,
    write_json_atomic,
)

SCHEMA = "multidetect.target-observation-recording-manifest.v1"
VERIFICATION_SCHEMA = "multidetect.target-observation-recording-manifest-verification.v1"
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "configs" / "schemas" / "target-observation-recording-manifest.schema.json"
MANIFEST_NAME = "target-observation-recording-manifest.json"
MAXIMUM_MANIFEST_BYTES = 2 * 1024 * 1024


class TargetObservationRecordingManifestError(ValueError):
    """Raised when a recording manifest or its directory cannot be trusted."""


def _reject_constant(value: str) -> None:
    raise TargetObservationRecordingManifestError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TargetObservationRecordingManifestError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _parse_utc(value: object, *, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TargetObservationRecordingManifestError(f"{name} must be a date-time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TargetObservationRecordingManifestError(f"{name} must be a date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TargetObservationRecordingManifestError(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _read_regular_file(path: Path, *, maximum_bytes: int, name: str) -> tuple[Path, bytes]:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise TargetObservationRecordingManifestError(f"{name} cannot be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise TargetObservationRecordingManifestError(f"{name} cannot be resolved: {exc}") from exc
    if not resolved.is_file():
        raise TargetObservationRecordingManifestError(f"{name} must be a regular file")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise TargetObservationRecordingManifestError(f"cannot read {name}: {exc}") from exc
    if not payload:
        raise TargetObservationRecordingManifestError(f"{name} is empty")
    if len(payload) > maximum_bytes:
        raise TargetObservationRecordingManifestError(f"{name} exceeds {maximum_bytes} bytes")
    return resolved, payload


def _load_manifest(path: str | Path) -> tuple[Path, bytes, dict[str, Any]]:
    resolved, payload = _read_regular_file(
        Path(path), maximum_bytes=MAXIMUM_MANIFEST_BYTES, name="recording manifest"
    )
    if resolved.name != MANIFEST_NAME:
        raise TargetObservationRecordingManifestError(
            f"recording manifest must be named {MANIFEST_NAME}"
        )
    try:
        value = json.loads(
            payload.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except TargetObservationRecordingManifestError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TargetObservationRecordingManifestError(
            f"cannot parse recording manifest: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise TargetObservationRecordingManifestError("recording manifest must be an object")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda item: list(item.path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        raise TargetObservationRecordingManifestError(
            f"recording manifest schema validation failed at {location}: {first.message}"
        )
    return resolved, payload, value


def verify_target_observation_recording_manifest(path: str | Path) -> dict[str, object]:
    """Verify a native manifest, every referenced snapshot, and directory closure."""

    manifest_path, manifest_payload, document = _load_manifest(path)
    started_at = _parse_utc(document["started_at_utc"], name="started_at_utc")
    ended_at = _parse_utc(document["ended_at_utc"], name="ended_at_utc")
    if ended_at < started_at:
        raise TargetObservationRecordingManifestError("ended_at_utc precedes started_at_utc")
    snapshots = document["snapshots"]
    if document["sample_count"] != len(snapshots):
        raise TargetObservationRecordingManifestError(
            "sample_count does not equal the number of snapshots"
        )
    if document["sample_count"] > document["maximum_samples"]:
        raise TargetObservationRecordingManifestError("sample_count exceeds maximum_samples")

    directory = manifest_path.parent
    seen_names: set[str] = set()
    verified_snapshots: list[dict[str, object]] = []
    previous_local_now_ms: int | None = None
    referenced_paths: set[Path] = set()
    for index, entry in enumerate(snapshots):
        relative_path = entry["relative_path"]
        relative = Path(relative_path)
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name != relative_path:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} relative_path must be one file name"
            )
        folded = relative_path.casefold()
        if folded in seen_names:
            raise TargetObservationRecordingManifestError(
                f"duplicate snapshot relative_path: {relative_path}"
            )
        seen_names.add(folded)
        candidate = directory / relative_path
        resolved, payload = _read_regular_file(
            candidate,
            maximum_bytes=1024 * 1024,
            name=f"snapshot {index}",
        )
        if resolved.parent != directory:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} resolves outside the recording directory"
            )
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if len(payload) != entry["bytes"]:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} byte count does not match manifest"
            )
        if actual_sha256 != entry["sha256"]:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} SHA-256 does not match manifest"
            )
        try:
            _, snapshot = load_target_observation_diagnostics(resolved)
        except TargetObservationDiagnosticsError as exc:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} failed diagnostics verification: {exc}"
            ) from exc
        if resolved.read_bytes() != payload:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} changed while it was being verified"
            )
        generated_at = _parse_utc(snapshot["generated_at_utc"], name="generated_at_utc")
        if generated_at < started_at or generated_at > ended_at:
            raise TargetObservationRecordingManifestError(
                f"snapshot {index} timestamp is outside the recording interval"
            )
        local_now_ms = int(snapshot["local_now_ms"])
        if previous_local_now_ms is not None and local_now_ms <= previous_local_now_ms:
            raise TargetObservationRecordingManifestError(
                "manifest snapshots are not in strictly increasing local_now_ms order"
            )
        previous_local_now_ms = local_now_ms
        referenced_paths.add(resolved)
        verified_snapshots.append(
            {
                "index": index,
                "relative_path": relative_path,
                "path": str(resolved),
                "bytes": len(payload),
                "sha256": actual_sha256,
                "generated_at_utc": snapshot["generated_at_utc"],
                "local_now_ms": local_now_ms,
                "locatable": bool(snapshot["readiness"]["locatable"]),
            }
        )

    discovered_paths: set[Path] = set()
    for item in directory.glob("target-observation-diagnostics-*.json"):
        if item.is_symlink() or not item.is_file():
            raise TargetObservationRecordingManifestError(
                f"recording directory contains a non-regular snapshot entry: {item.name}"
            )
        discovered_paths.add(item.resolve())
    if discovered_paths != referenced_paths:
        missing = sorted(str(item.name) for item in referenced_paths - discovered_paths)
        extra = sorted(str(item.name) for item in discovered_paths - referenced_paths)
        raise TargetObservationRecordingManifestError(
            f"recording directory is not closed: missing={missing}, unreferenced={extra}"
        )

    return {
        "schema": VERIFICATION_SCHEMA,
        "schema_version": 1,
        "manifest_path": str(manifest_path),
        "manifest_bytes": len(manifest_payload),
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "session_directory": str(directory),
        "session_id": document["session_id"],
        "started_at_utc": document["started_at_utc"],
        "ended_at_utc": document["ended_at_utc"],
        "interval_ms": document["interval_ms"],
        "maximum_samples": document["maximum_samples"],
        "sample_count": document["sample_count"],
        "stop_reason": document["stop_reason"],
        "snapshots": verified_snapshots,
        "directory_closed": True,
        "advisory_only": True,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "gate_passed": True,
        "reasons": [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a native MultiDetectGCS target-observation recording manifest."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify_target_observation_recording_manifest(args.manifest)
    except (TargetObservationRecordingManifestError, OSError, ValueError) as exc:
        report = {
            "schema": VERIFICATION_SCHEMA,
            "schema_version": 1,
            "advisory_only": True,
            "flight_control_enabled": False,
            "physical_release_enabled": False,
            "gate_passed": False,
            "reasons": [str(exc)],
        }
    if args.out is not None:
        write_json_atomic(args.out, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["gate_passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MANIFEST_NAME",
    "SCHEMA",
    "SCHEMA_PATH",
    "TargetObservationRecordingManifestError",
    "main",
    "verify_target_observation_recording_manifest",
]
