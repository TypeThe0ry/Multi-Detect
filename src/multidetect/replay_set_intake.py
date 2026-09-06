"""Immutable intake of deployment recordings for replay-set annotation.

This stage deliberately binds only raw recordings.  It never creates ground
truth, predictions, review approval, or an accuracy claim.  Annotators add
those artifacts later and the existing :mod:`multidetect.replay_set` builder
performs the deployment-evidence checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .autonomy.serialization import (
    AutonomyContractError,
    canonical_sha256,
    require_int,
    require_schema_version,
    require_sha256,
    require_text,
    strict_object,
)
from .backends.raw_replay import RawSensorReplayRecording
from .deployment_recording_attestation import (
    ATTESTATION_NAME,
    verify_deployment_recording_attestation,
)
from .replay_set import REPLAY_SET_STRATA

INTAKE_RECEIPT_NAME = "intake.receipt.json"


def prepare_deployment_replay_set(
    sources: Mapping[str, str | Path],
    output: str | Path,
    *,
    dataset_id: str,
    dataset_version: str,
) -> dict[str, object]:
    """Copy six explicitly assigned raw recordings into an immutable staging set.

    ``output`` must not exist.  Work is assembled in a sibling temporary
    directory, fully reopened and hash-verified, then renamed into place.
    """

    normalized = _normalize_sources(sources)
    output_path = Path(output).absolute()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise AutonomyContractError(f"replay intake output already exists: {output_path}")

    temporary = Path(tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=output_path.parent))
    try:
        entries: list[dict[str, object]] = []
        seen_source_roots: set[Path] = set()
        seen_recording_hashes: set[str] = set()
        for stratum in REPLAY_SET_STRATA:
            source_argument = normalized[stratum]
            if source_argument.is_symlink():
                raise AutonomyContractError(f"stratum {stratum} source must not be a symlink")
            source_root = source_argument.resolve(strict=True)
            if not source_root.is_dir():
                raise AutonomyContractError(f"stratum {stratum} source is not a directory")
            if source_root in seen_source_roots:
                raise AutonomyContractError("each stratum must use a distinct source directory")
            seen_source_roots.add(source_root)
            manifest_path = source_root / "recording.manifest.json"
            _require_regular_without_symlinks(source_root, manifest_path, stratum=stratum)
            recording = RawSensorReplayRecording.from_manifest(manifest_path)
            source_attestation = verify_deployment_recording_attestation(
                source_root, expected_stratum=stratum
            )
            if recording.recording_hash in seen_recording_hashes:
                raise AutonomyContractError("each stratum must use a distinct recording")
            seen_recording_hashes.add(recording.recording_hash)
            raw_manifest = _load_json_object(manifest_path, name=f"{stratum} recording manifest")
            events = raw_manifest.get("events")
            if not isinstance(events, dict):
                raise AutonomyContractError(f"stratum {stratum} manifest events must be an object")
            event_relative = _relative_path(events.get("path"), f"{stratum} events.path")

            relative_files = {
                "recording.manifest.json",
                ATTESTATION_NAME,
                event_relative,
            }
            relative_files.update(frame.image.relative_path for frame in recording.frames)
            destination_root = temporary / stratum
            artifacts: list[dict[str, object]] = []
            for relative in sorted(relative_files):
                source_file = source_root / Path(*PurePosixPath(relative).parts)
                _require_regular_without_symlinks(source_root, source_file, stratum=stratum)
                payload = source_file.read_bytes()
                destination = destination_root / Path(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
                artifacts.append(
                    {
                        "path": f"{stratum}/{relative}",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "bytes": len(payload),
                    }
                )

            staged_manifest = destination_root / "recording.manifest.json"
            staged_recording = RawSensorReplayRecording.from_manifest(staged_manifest)
            if staged_recording.recording_hash != recording.recording_hash:
                raise AutonomyContractError(f"stratum {stratum} recording changed during intake")
            entries.append(
                {
                    "stratum": stratum,
                    "recording_id": recording.recording_id,
                    "recording_hash": recording.recording_hash,
                    "frame_count": len(recording.frames),
                    "source_attestation": {
                        "path": f"{stratum}/{ATTESTATION_NAME}",
                        "canonical_hash": source_attestation["canonical_hash"],
                        "captured_by": source_attestation["captured_by"],
                        "capture_device_id": source_attestation["capture_device_id"],
                        "capture_site": source_attestation["capture_site"],
                        "source_type": source_attestation["source_type"],
                        "started_at_utc": source_attestation["started_at_utc"],
                        "ended_at_utc": source_attestation["ended_at_utc"],
                    },
                    "artifacts": artifacts,
                    "annotation_state": "pending",
                    "prediction_state": "pending",
                }
            )

        document: dict[str, object] = {
            "schema_version": 1,
            "dataset_id": require_text(dataset_id, "dataset_id"),
            "dataset_version": require_text(dataset_version, "dataset_version"),
            "source_kind": "deployment_recording",
            "required_strata": list(REPLAY_SET_STRATA),
            "entries": entries,
            "claims": {
                "annotations_reviewed": False,
                "predictions_complete": False,
                "accuracy_validated": False,
                "release_ready": False,
            },
            "next_command": (
                "python scripts/create_replay_set_manifest.py . "
                "--out replay-set.manifest.json --dataset-id "
                f"{dataset_id} --dataset-version {dataset_version} "
                "--source-kind deployment_recording --annotations-reviewed"
            ),
        }
        document["canonical_hash"] = canonical_sha256(document)
        _write_json(temporary / INTAKE_RECEIPT_NAME, document)
        verify_deployment_replay_intake(temporary)
        if output_path.exists() or output_path.is_symlink():
            raise AutonomyContractError(f"replay intake output already exists: {output_path}")
        os.rename(temporary, output_path)
        return verify_deployment_replay_intake(output_path)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_deployment_replay_intake(root: str | Path) -> dict[str, object]:
    """Fail closed when a receipt, copied recording, or referenced asset changed."""

    requested_root = Path(root)
    if requested_root.is_symlink():
        raise AutonomyContractError("replay intake root must not be a symlink")
    root_path = requested_root.resolve(strict=True)
    receipt_path = root_path / INTAKE_RECEIPT_NAME
    _require_regular_without_symlinks(root_path, receipt_path, stratum="receipt")
    raw = _load_json_object(receipt_path, name="replay intake receipt")
    value = strict_object(
        raw,
        name="ReplaySetIntakeReceipt",
        required=frozenset(
            {
                "schema_version",
                "dataset_id",
                "dataset_version",
                "source_kind",
                "required_strata",
                "entries",
                "claims",
                "next_command",
                "canonical_hash",
            }
        ),
    )
    require_schema_version(value["schema_version"])
    require_text(value["dataset_id"], "dataset_id")
    require_text(value["dataset_version"], "dataset_version")
    next_command = require_text(value["next_command"], "next_command")
    if value["source_kind"] != "deployment_recording":
        raise AutonomyContractError("intake source_kind must be deployment_recording")
    if value["required_strata"] != list(REPLAY_SET_STRATA):
        raise AutonomyContractError("intake required_strata must match the canonical six strata")
    claims = strict_object(
        value["claims"],
        name="ReplaySetIntakeClaims",
        required=frozenset(
            {"annotations_reviewed", "predictions_complete", "accuracy_validated", "release_ready"}
        ),
    )
    if any(item is not False for item in claims.values()):
        raise AutonomyContractError(
            "intake receipt must not assert review, prediction, or release claims"
        )
    entries = value["entries"]
    if not isinstance(entries, list) or len(entries) != len(REPLAY_SET_STRATA):
        raise AutonomyContractError("intake entries must contain exactly six strata")

    observed_strata: list[str] = []
    for index, raw_entry in enumerate(entries):
        entry = strict_object(
            raw_entry,
            name=f"entries[{index}]",
            required=frozenset(
                {
                    "stratum",
                    "recording_id",
                    "recording_hash",
                    "frame_count",
                    "source_attestation",
                    "artifacts",
                    "annotation_state",
                    "prediction_state",
                }
            ),
        )
        stratum = require_text(entry["stratum"], f"entries[{index}].stratum")
        observed_strata.append(stratum)
        require_text(entry["recording_id"], f"entries[{index}].recording_id")
        expected_recording_hash = require_sha256(
            entry["recording_hash"], f"entries[{index}].recording_hash"
        )
        source_attestation = strict_object(
            entry["source_attestation"],
            name=f"entries[{index}].source_attestation",
            required=frozenset(
                {
                    "path",
                    "canonical_hash",
                    "captured_by",
                    "capture_device_id",
                    "capture_site",
                    "source_type",
                    "started_at_utc",
                    "ended_at_utc",
                }
            ),
        )
        if source_attestation["path"] != f"{stratum}/{ATTESTATION_NAME}":
            raise AutonomyContractError(f"stratum {stratum} source attestation path is invalid")
        frame_count = require_int(entry["frame_count"], f"entries[{index}].frame_count", minimum=1)
        if entry["annotation_state"] != "pending" or entry["prediction_state"] != "pending":
            raise AutonomyContractError(
                "intake annotation and prediction states must remain pending"
            )
        artifacts = entry["artifacts"]
        if not isinstance(artifacts, list) or not artifacts:
            raise AutonomyContractError(f"stratum {stratum} artifacts must not be empty")
        seen_paths: set[str] = set()
        for artifact_index, raw_artifact in enumerate(artifacts):
            artifact = strict_object(
                raw_artifact,
                name=f"entries[{index}].artifacts[{artifact_index}]",
                required=frozenset({"path", "sha256", "bytes"}),
            )
            relative = _relative_path(artifact["path"], "artifact.path")
            if relative in seen_paths or not relative.startswith(f"{stratum}/"):
                raise AutonomyContractError(
                    f"stratum {stratum} has invalid or duplicate artifact path"
                )
            seen_paths.add(relative)
            path = root_path / Path(*PurePosixPath(relative).parts)
            _require_regular_without_symlinks(root_path, path, stratum=stratum)
            payload = path.read_bytes()
            expected_bytes = require_int(artifact["bytes"], "artifact.bytes", minimum=1)
            expected_hash = require_sha256(artifact["sha256"], "artifact.sha256")
            if (
                len(payload) != expected_bytes
                or hashlib.sha256(payload).hexdigest() != expected_hash
            ):
                raise AutonomyContractError(f"intake artifact hash mismatch: {relative}")
        recording = RawSensorReplayRecording.from_manifest(
            root_path / stratum / "recording.manifest.json"
        )
        verified_attestation = verify_deployment_recording_attestation(
            root_path / stratum, expected_stratum=stratum
        )
        for field in (
            "canonical_hash",
            "captured_by",
            "capture_device_id",
            "capture_site",
            "source_type",
            "started_at_utc",
            "ended_at_utc",
        ):
            if source_attestation[field] != verified_attestation[field]:
                raise AutonomyContractError(
                    f"stratum {stratum} source attestation {field} mismatch"
                )
        recording_document = _load_json_object(
            root_path / stratum / "recording.manifest.json",
            name=f"{stratum} recording manifest",
        )
        events = recording_document.get("events")
        if not isinstance(events, dict):
            raise AutonomyContractError(f"stratum {stratum} manifest events must be an object")
        event_relative = _relative_path(events.get("path"), f"{stratum} events.path")
        expected_paths = {
            f"{stratum}/recording.manifest.json",
            f"{stratum}/{ATTESTATION_NAME}",
            f"{stratum}/{event_relative}",
            *(f"{stratum}/{frame.image.relative_path}" for frame in recording.frames),
        }
        if seen_paths != expected_paths:
            raise AutonomyContractError(f"stratum {stratum} artifact inventory is incomplete")
        if (
            recording.recording_hash != expected_recording_hash
            or len(recording.frames) != frame_count
        ):
            raise AutonomyContractError(f"stratum {stratum} recording identity mismatch")

    if observed_strata != list(REPLAY_SET_STRATA):
        raise AutonomyContractError("intake entries must be in canonical stratum order")
    expected_canonical = require_sha256(value["canonical_hash"], "canonical_hash")
    unsigned = dict(value)
    unsigned.pop("canonical_hash")
    if canonical_sha256(unsigned) != expected_canonical:
        raise AutonomyContractError("intake receipt canonical_hash mismatch")
    return {
        "event": "deployment_replay_set_intake_verified",
        "root": str(root_path),
        "dataset_id": value["dataset_id"],
        "dataset_version": value["dataset_version"],
        "entry_count": len(entries),
        "frame_count": sum(int(entry["frame_count"]) for entry in entries),
        "canonical_hash": expected_canonical,
        "annotation_state": "pending",
        "prediction_state": "pending",
        "deployment_sources_attested": True,
        "release_ready": False,
        "next_command": next_command,
    }


def _normalize_sources(sources: Mapping[str, str | Path]) -> dict[str, Path]:
    if not isinstance(sources, Mapping):
        raise AutonomyContractError("sources must be a stratum-to-directory mapping")
    if set(sources) != set(REPLAY_SET_STRATA):
        missing = sorted(set(REPLAY_SET_STRATA) - set(sources))
        extra = sorted(set(sources) - set(REPLAY_SET_STRATA))
        raise AutonomyContractError(
            f"sources must contain exactly six strata; missing={missing}, extra={extra}"
        )
    return {stratum: Path(sources[stratum]).absolute() for stratum in REPLAY_SET_STRATA}


def _relative_path(raw: object, name: str) -> str:
    text = require_text(raw, name).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or path.as_posix() in {"", "."} or ".." in path.parts:
        raise AutonomyContractError(f"{name} must be a contained relative path")
    if any(":" in part or part in {"", "."} for part in path.parts):
        raise AutonomyContractError(f"{name} contains an unsupported path component")
    return path.as_posix()


def _require_regular_without_symlinks(root: Path, path: Path, *, stratum: str) -> None:
    candidate = path.absolute()
    try:
        relative = candidate.relative_to(root.absolute())
    except ValueError as exc:
        raise AutonomyContractError(f"stratum {stratum} file escapes its source directory") from exc
    current = root.absolute()
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise AutonomyContractError(f"stratum {stratum} contains a symlink: {relative}")
    if not candidate.is_file():
        raise AutonomyContractError(f"stratum {stratum} requires regular file: {relative}")


def _load_json_object(path: Path, *, name: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite number {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise AutonomyContractError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AutonomyContractError(f"{name} must be an object")
    return value


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


__all__ = [
    "INTAKE_RECEIPT_NAME",
    "prepare_deployment_replay_set",
    "verify_deployment_replay_intake",
]
