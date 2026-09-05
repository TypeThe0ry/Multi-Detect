"""Build a deterministic four-mode E7 release-gate manifest.

The release-gate evaluator (:mod:`multidetect.release_gate`) deliberately only
consumes an already prepared manifest.  This module is the small, explicit
writer used to prepare that manifest from *mode-specific* evidence files.  It
does not try to discover evidence in ``artifacts/`` and it never promotes a
goal snapshot, fixture, or dry-run document into a release claim.

The writer is intentionally fail-closed:

* all four evidence paths are required and must be regular JSON files;
* every evidence document must identify its runtime mode and contain an
  explicit boolean ``gate_passed`` (a false value remains closed);
* visual and mission acceptance default to ``not_claimed``;
* HIL/REAL hardware and real-control acceptance default to ``not_claimed``;
* replay/SIM hardware and real-control default to ``not_required``;
* the generated mode ``gate_passed`` is true only when the evidence gate and
  all mode status requirements are true.
    * an optional ``ReplaySetManifest`` can be hash-bound to the top-level REPLAY
      gate; a pending set remains closed and a final release requires it.  An
      optional ``replay_set_accuracy_evidence`` report binds the independent IoU
      review to the same raw/canonical replay-set hashes.  An optional
      ``replay_set_calibration_verification`` artifact independently re-computes
      the advisory threshold report and is bound to the same replay-set hashes.
* an optional REAL field-link preflight report can be hash-bound to the
  top-level ``real_field_link`` record; the evaluator re-verifies the report
  and the final release channel requires a ready binding.
* an optional REAL authenticated alert-link bench report can be hash-bound to
  the top-level ``real_alert_link`` record; the writer requires a completed
  non-loopback authenticated ACK bench and the final release channel requires
  the binding.
* an optional REAL target-geolocation accuracy report can be hash-bound to the
  top-level ``real_target_accuracy`` record; the evaluator recomputes the
  WGS-84 errors and the final release channel requires a passing binding.
* an optional REAL target-state capture report can be hash-bound to the
  top-level ``real_target_state_evidence`` record; the capture verifier
  rechecks the persisted ``TGT`` GPS/RANGE gate and the final release channel
  requires a passing binding.
* an optional deterministic SIM S4 safety artifact can be hash-bound to the
  top-level ``simulator_safety`` record; the writer requires the source and
  verifier to pass, and the evaluator re-hashes/re-verifies it on every read.
* an optional deterministic actuator safety software-bench artifact can be
  hash-bound to the top-level ``actuator_safety`` record; the writer requires
  the source and verifier to pass, and the evaluator re-hashes/re-verifies it
  on every read.  The final channel requires this shared executioner contract.
* an optional deterministic three-airframe PX4 Mission transaction artifact
  can be hash-bound to ``px4_airframe_transaction``; the writer requires all
  three durable journals to pass, and the evaluator re-opens them on every
  read so the final release cannot silently skip an airframe.
* an optional three-airframe PX4 reported-position response matrix can be
  hash-bound to ``px4_reported_position_response_matrix``; its verifier
  reopens all three Docker acceptance reports, scenario manifests, parity
  identity and source tools so simulated motion cannot be replaced by a
  stale or pose-injected claim.
* an optional six-observation PX4 Mission feedback matrix can be hash-bound to
  ``px4_mission_feedback``; the writer requires one receive-only observation
  for every HIL/REAL and VTOL/fixed-wing/multirotor combination.  The evaluator
  re-opens every executable Mission, signed runtime profile and raw
  observation, and binds the matrix to the current four-mode
  ``px4_mission_path_hash``.
* an optional deterministic three-airframe SIM scenario matrix can be
  hash-bound to ``simulator_scenario_matrix``; the writer and evaluator require
  all three scenario modes, AI/authority execution, and the embedded canonical
  digest to pass.  When the product version is the final ``0.3.0`` label, the
  matrix must also opt into automatic full fault coverage; a nominal-only
  prerelease matrix is never promoted into the final channel.
* an optional deterministic three-airframe 3-D control binding can be
  hash-bound to ``simulator_3d_control``; the writer requires one VTOL,
  fixed-wing and multirotor record and the evaluator re-opens every source
  record on each read.  It remains a software/asset gate and never claims
  external PX4/Gazebo or hardware execution.
* an optional deterministic three-airframe scene-stream set can be hash-bound
  to ``simulator_scene_stream_set``; the writer requires one VTOL, fixed-wing
  and multirotor source and the evaluator re-opens every stream on each read.
  It is a software/SIM provenance gate and never claims external runtime,
  hardware, or operator visual acceptance.
* an optional six-source external SIM runtime/live-AI binding can be
  hash-bound to ``simulator_external_evidence``; the writer requires one
  runtime and one AI report for every airframe plus an independently generated
  revalidation artifact.  The evaluator reopens the join and recomputes all
  six source files on every read.  It remains software-only and never claims
  hardware or operator visual acceptance.
* an optional six-strata field-evidence bundle can be hash-bound to
  ``field_evidence_bundle``; its serialized inputs and summary are
  re-evaluated on every read, and the final release channel requires a
  passing real-domain bundle.

No manifest-generation timestamp is emitted.  A REAL field-link binding does
carry the source report's observation timestamp and configured freshness
window; the evaluator intentionally rechecks the current age, so an ageing
field report can close an otherwise unchanged binding.  Evidence paths in the
output are relative to the manifest directory, which keeps the document
portable while retaining the hash binding.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .actuator_safety import verify_actuator_safety_evidence
from .alert_link_bench import verify_alert_link_bench_report
from .field_evidence_bundle import verify_field_evidence_bundle_report
from .field_link_preflight import (
    DEFAULT_FIELD_LINK_MAX_AGE_S,
    verify_field_link_report,
)
from .field_session_capture import verify_field_session_capture
from .geolocation_accuracy import verify_geolocation_accuracy_report
from .model_review import verify_model_review_bundle
from .product_version import ProductVersion, load_product_version
from .qgc_visual_acceptance import verify_record as verify_qgc_visual_acceptance
from .release_gate import PARITY_FIELDS, REQUIRED_MODES
from .replay_set import load_replay_set_manifest
from .replay_set_calibration import verify_replay_set_calibration_report
from .replay_set_evaluation import (
    accuracy_metrics_are_defined,
    positive_strata_coverage_is_complete,
)
from .simulator.safety_evidence import verify_file as verify_simulator_safety_file
from .simulator.scenario_matrix import (
    verify_file as verify_simulator_scenario_matrix_file,
)
from .simulator.three_d_control import build_binding as build_simulator_3d_control_binding
from .simulator_combined_evidence import build_binding as build_combined_sim_binding
from .simulator_external_evidence import (
    REQUIRED_VEHICLES as EXTERNAL_REQUIRED_VEHICLES,
)
from .simulator_external_evidence import (
    revalidate_simulator_external_evidence,
)

MANIFEST_SCHEMA_VERSION = 1
# Keep the evaluator's naming available to callers that treat the writer and
# evaluator as one release-gate surface.
RELEASE_GATE_SCHEMA_VERSION = MANIFEST_SCHEMA_VERSION
STATUS_VALUES = ("passed", "not_required", "not_claimed", "pending")
_STATUS_SET = frozenset(STATUS_VALUES)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FIXTURE_MARKER_KEYS = (
    "fixture",
    "is_fixture",
    "dry_run",
    "dry-run",
    "goal_snapshot",
    "goal-snapshot",
)
_FIXTURE_KIND_VALUES = frozenset(
    {
        "fixture",
        "dry_run",
        "dry-run",
        "goal_snapshot",
        "goal-snapshot",
    }
)


class ReleaseGateManifestError(ValueError):
    """Raised when release-gate manifest inputs are incomplete or unsafe."""


def sha256_file(path: str | Path) -> str:
    """Return the lowercase SHA-256 digest of a regular file."""

    source = Path(path)
    if not source.is_file():
        raise ReleaseGateManifestError(f"evidence file does not exist: {source}")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReleaseGateManifestError(f"unable to read evidence file {source}: {exc}") from exc
    return digest.hexdigest()


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value.strip()) is None:
        raise ReleaseGateManifestError(f"{name} must be 64 hexadecimal characters")
    return value.strip().lower()


def _resolve_file(value: str | Path, *, name: str) -> Path:
    source = Path(value).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ReleaseGateManifestError(f"{name} does not exist: {source}") from exc
    if not resolved.is_file():
        raise ReleaseGateManifestError(f"{name} must be a regular file: {resolved}")
    return resolved


def _load_json_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        # PowerShell 5.1 emits a UTF-8 BOM for the JSON written by the
        # Windows acceptance wrappers.  ``utf-8-sig`` accepts that input and
        # remains identical for ordinary BOM-less UTF-8 artifacts.
        raw = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateManifestError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReleaseGateManifestError(f"{name} must contain a JSON object")
    return raw


def _reject_json_constant(value: str) -> None:
    """Reject non-finite JSON numbers in release-gate evidence."""

    raise ReleaseGateManifestError(f"non-finite JSON number {value}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject ambiguous last-key-wins JSON objects."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseGateManifestError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _artifact_runtime_mode(artifact: Mapping[str, Any]) -> str | None:
    """Read the same runtime identity forms accepted by the evaluator."""

    for key in ("runtime_mode", "mode"):
        value = artifact.get(key)
        if isinstance(value, str):
            return value.strip().lower()
    runtime = artifact.get("runtime")
    if isinstance(runtime, Mapping):
        value = runtime.get("mode")
        if isinstance(value, str):
            return value.strip().lower()
    return None


def _fixture_marker(artifact: Mapping[str, Any]) -> str | None:
    """Return an explicit non-release marker, without guessing from filenames."""

    for key in _FIXTURE_MARKER_KEYS:
        if artifact.get(key) is True:
            return key
    for key in ("evidence_kind", "artifact_kind", "source_kind"):
        value = artifact.get(key)
        if isinstance(value, str) and value.strip().lower() in _FIXTURE_KIND_VALUES:
            return f"{key}={value.strip()}"
    return None


def _read_evidence(path: Path, *, mode: str) -> tuple[str, str | None, bool]:
    """Validate one mode artifact and return ``(sha, event, gate_passed)``."""

    artifact = _load_json_object(path, name=f"{mode} evidence")
    marker = _fixture_marker(artifact)
    if marker is not None:
        raise ReleaseGateManifestError(
            f"{mode} evidence is explicitly marked {marker!r}; "
            "goal snapshots/fixtures cannot be release evidence"
        )
    runtime_mode = _artifact_runtime_mode(artifact)
    if runtime_mode is None:
        raise ReleaseGateManifestError(
            f"{mode} evidence must declare runtime_mode (goal snapshots are not evidence)"
        )
    if runtime_mode != mode:
        raise ReleaseGateManifestError(
            f"{mode} evidence runtime mode is {runtime_mode!r}, expected {mode!r}"
        )
    artifact_gate = artifact.get("gate_passed")
    if type(artifact_gate) is not bool:
        raise ReleaseGateManifestError(
            f"{mode} evidence gate_passed must be a boolean (goal snapshots are not evidence)"
        )
    event = artifact.get("event")
    if event is not None and (not isinstance(event, str) or not event.strip()):
        raise ReleaseGateManifestError(f"{mode} evidence event must be a non-empty string")
    return sha256_file(path), event.strip() if isinstance(event, str) else None, artifact_gate


def _normalise_status(value: object, *, name: str) -> str:
    if not isinstance(value, str) or value.strip().lower() not in _STATUS_SET:
        raise ReleaseGateManifestError(f"{name} must be one of {', '.join(STATUS_VALUES)}")
    return value.strip().lower()


def default_mode_statuses(mode: str) -> dict[str, str]:
    """Return fail-closed status defaults for a runtime mode."""

    normalized = mode.strip().lower()
    if normalized not in REQUIRED_MODES:
        raise ReleaseGateManifestError(f"unsupported runtime mode: {mode!r}")
    hardware = "not_required" if normalized in {"replay", "sim"} else "not_claimed"
    real_control = "not_required" if normalized in {"replay", "sim"} else "not_claimed"
    return {
        "operator_visual_acceptance": "not_claimed",
        "hardware_acceptance": hardware,
        "real_control_acceptance": real_control,
        "mission_path_acceptance": "not_claimed",
    }


def _normalise_statuses(
    mode: str,
    supplied: Mapping[str, object] | None,
) -> dict[str, str]:
    statuses = default_mode_statuses(mode)
    if supplied is None:
        return statuses
    aliases = {
        "visual": "operator_visual_acceptance",
        "visual_acceptance": "operator_visual_acceptance",
        "operator_visual": "operator_visual_acceptance",
        "operator_visual_acceptance": "operator_visual_acceptance",
        "mission": "mission_path_acceptance",
        "mission_acceptance": "mission_path_acceptance",
        "mission_path": "mission_path_acceptance",
        "mission_path_acceptance": "mission_path_acceptance",
        "hardware": "hardware_acceptance",
        "hardware_acceptance": "hardware_acceptance",
        "real_control": "real_control_acceptance",
        "real-control": "real_control_acceptance",
        "real_control_acceptance": "real_control_acceptance",
        "real-control-acceptance": "real_control_acceptance",
    }
    normalized_keys = {str(key).strip().lower(): key for key in supplied}
    unknown = sorted(key for key in normalized_keys if key not in aliases)
    if unknown:
        raise ReleaseGateManifestError(f"{mode} has unknown status field(s): {unknown!r}")
    seen_fields: set[str] = set()
    for key, value in supplied.items():
        normalized_key = str(key).strip().lower()
        canonical = aliases[normalized_key]
        normalized = _normalise_status(value, name=f"{mode}.{canonical}")
        if canonical in seen_fields and statuses[canonical] != normalized:
            raise ReleaseGateManifestError(f"{mode} supplies conflicting aliases for {canonical}")
        statuses[canonical] = normalized
        seen_fields.add(canonical)
    return statuses


def _status_gate(mode: str, statuses: Mapping[str, str]) -> bool:
    if statuses.get("operator_visual_acceptance") != "passed":
        return False
    if statuses.get("mission_path_acceptance") != "passed":
        return False
    for field in ("hardware_acceptance", "real_control_acceptance"):
        value = statuses.get(field)
        if mode in {"hil", "real"}:
            if value != "passed":
                return False
        elif value not in {"not_required", "passed"}:
            return False
    return True


def _normalise_parity(parity: Mapping[str, object]) -> dict[str, str]:
    nested = parity.get("parity")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise ReleaseGateManifestError("parity field must be an object")
        parity = nested
    unknown = sorted(set(parity).difference(PARITY_FIELDS))
    missing = sorted(set(PARITY_FIELDS).difference(parity))
    if unknown:
        raise ReleaseGateManifestError(f"parity has unknown field(s): {unknown!r}")
    if missing:
        raise ReleaseGateManifestError(f"parity is missing field(s): {missing!r}")
    return {field: _require_sha256(parity[field], f"parity.{field}") for field in PARITY_FIELDS}


def load_parity_hashes(path: str | Path) -> dict[str, str]:
    """Load parity hashes from a direct object or a ``{"parity": ...}`` object."""

    parity_path = _resolve_file(path, name="parity JSON")
    document = _load_json_object(parity_path, name="parity JSON")
    nested = document.get("parity")
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise ReleaseGateManifestError("parity JSON 'parity' field must be an object")
        document = dict(nested)
    return _normalise_parity(document)


def _relative_path(path: Path, *, manifest_directory: Path) -> str:
    try:
        relative = os.path.relpath(path, manifest_directory)
    except ValueError as exc:
        raise ReleaseGateManifestError(
            "evidence and manifest output must be on the same filesystem drive"
        ) from exc
    return Path(relative).as_posix()


def _resolve_bound_file(value: str | Path, *, base: Path, name: str) -> Path:
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = base / source
    return _resolve_file(source, name=name)


def _replay_set_calibration_verification_record(
    verification_artifact: str | Path,
    *,
    replay_manifest: Path,
    replay_manifest_sha256: str,
    replay_canonical_sha256: str,
    output_identity: Path,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Hash-bind a verifier artifact to one replay-set manifest.

    The artifact's ``gate_passed`` means the calibration report was
    independently re-computed without tampering.  It deliberately does not
    require ``report_promotion_ready``: calibration remains advisory and the
    separate replay-set accuracy gate decides release readiness.
    """

    resolved = _resolve_file(
        verification_artifact,
        name="replay_set calibration verification artifact",
    )
    if resolved in {output_identity, replay_manifest}:
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact must be separate from "
            "the replay-set manifest and release-gate output"
        )
    document = _load_json_object(
        resolved,
        name="replay_set calibration verification artifact",
    )
    if document.get("event") != "replay_set_threshold_calibration_verified":
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact has the wrong event type"
        )
    if document.get("schema_version") != 1:
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact schema_version must be 1"
        )
    artifact_gate = document.get("gate_passed")
    if type(artifact_gate) is not bool:
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact gate_passed must be a boolean"
        )
    if not artifact_gate:
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact gate_passed is false"
        )
    checks = document.get("checks")
    required_checks = {
        "report_json_valid",
        "report_event_valid",
        "report_schema_version_valid",
        "manifest_available",
        "manifest_sha256_matches",
        "canonical_sha256_matches",
        "recomputed_report_matches",
    }
    if not isinstance(checks, Mapping) or set(checks) != required_checks:
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact checks are incomplete"
        )
    if any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact contains a failed check"
        )

    report_value = document.get("report_path")
    if not isinstance(report_value, str) or not report_value.strip():
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact report_path is missing"
        )
    report_path = _resolve_bound_file(
        report_value,
        base=resolved.parent,
        name="replay_set calibration report",
    )
    if report_path in {resolved, replay_manifest, output_identity}:
        raise ReleaseGateManifestError(
            "replay_set calibration report must be separate from bound artifacts"
        )

    artifact_manifest_value = document.get("manifest_path")
    if not isinstance(artifact_manifest_value, str) or not artifact_manifest_value.strip():
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact manifest_path is missing"
        )
    artifact_manifest_path = _resolve_bound_file(
        artifact_manifest_value,
        base=resolved.parent,
        name="replay_set calibration artifact manifest",
    )
    if artifact_manifest_path != replay_manifest:
        raise ReleaseGateManifestError(
            "replay_set calibration verification artifact is bound to a different "
            "replay-set manifest path"
        )

    verification = verify_replay_set_calibration_report(
        report_path,
        manifest_path=replay_manifest,
    )
    if verification.get("gate_passed") is not True:
        reasons = verification.get("reasons")
        detail = "; ".join(item for item in reasons if isinstance(item, str))
        raise ReleaseGateManifestError(
            "replay_set calibration verifier did not pass" + (f": {detail}" if detail else "")
        )

    report_sha256 = sha256_file(report_path)
    declared_report_sha256 = document.get("report_sha256")
    if (
        not isinstance(declared_report_sha256, str)
        or not _SHA256_RE.fullmatch(declared_report_sha256)
        or declared_report_sha256.lower() != report_sha256.lower()
    ):
        raise ReleaseGateManifestError(
            "replay_set calibration verification report SHA-256 does not match"
        )
    verified_report_sha256 = verification.get("report_sha256")
    if (
        not isinstance(verified_report_sha256, str)
        or verified_report_sha256.lower() != report_sha256.lower()
    ):
        raise ReleaseGateManifestError(
            "replay_set calibration verifier returned a different report SHA-256"
        )

    report_document = _load_json_object(report_path, name="replay_set calibration report")
    report_manifest_sha256 = report_document.get("manifest_sha256")
    report_canonical_sha256 = report_document.get("canonical_sha256")
    if (
        not isinstance(report_manifest_sha256, str)
        or report_manifest_sha256.lower() != replay_manifest_sha256.lower()
    ):
        raise ReleaseGateManifestError(
            "replay_set calibration report is bound to a different manifest SHA-256"
        )
    if (
        not isinstance(report_canonical_sha256, str)
        or report_canonical_sha256.lower() != replay_canonical_sha256.lower()
    ):
        raise ReleaseGateManifestError(
            "replay_set calibration report is bound to a different manifest canonical hash"
        )

    for field in ("report_promotion_ready", "recomputed_promotion_ready"):
        value = verification.get(field)
        if type(value) is not bool or document.get(field) != value:
            raise ReleaseGateManifestError(
                f"replay_set calibration verification {field} does not match the verifier result"
            )
    if document.get("manifest_path") != str(replay_manifest):
        # The generated verifier normally stores an absolute path.  A copied
        # artifact may use a relative spelling, so compare resolved identities.
        if artifact_manifest_path != replay_manifest:
            raise ReleaseGateManifestError(
                "replay_set calibration verification manifest_path does not match"
            )

    return {
        "path": _relative_path(resolved, manifest_directory=manifest_directory),
        "sha256": sha256_file(resolved),
        "event": document["event"],
        "schema_version": document["schema_version"],
        "report_path": _relative_path(report_path, manifest_directory=manifest_directory),
        "report_sha256": report_sha256,
        "manifest_sha256": replay_manifest_sha256,
        "canonical_sha256": replay_canonical_sha256,
        "report_promotion_ready": verification["report_promotion_ready"],
        "recomputed_promotion_ready": verification["recomputed_promotion_ready"],
        "gate_passed": True,
    }


def _replay_set_record(
    replay_set_manifest: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    accuracy_evidence: str | Path | None = None,
    calibration_verification: str | Path | None = None,
) -> dict[str, Any]:
    """Hash-bind a stratified replay-set manifest into the release manifest."""

    resolved = _resolve_file(replay_set_manifest, name="replay_set manifest")
    if resolved == output_identity:
        raise ReleaseGateManifestError("release-gate output cannot also be the replay_set manifest")
    replay_set = load_replay_set_manifest(resolved)
    manifest_sha256 = sha256_file(resolved)
    record: dict[str, Any] = {
        "manifest_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "manifest_sha256": manifest_sha256,
        "canonical_sha256": replay_set.canonical_hash,
        "gate_passed": replay_set.is_complete,
        "accuracy_validated": replay_set.claims["accuracy_validated"],
    }
    if accuracy_evidence is not None:
        accuracy_path = _resolve_file(accuracy_evidence, name="replay_set accuracy evidence")
        if accuracy_path in {resolved, output_identity}:
            raise ReleaseGateManifestError(
                "replay_set accuracy evidence must be separate from the replay-set "
                "manifest and release-gate output"
            )
        accuracy = _load_json_object(accuracy_path, name="replay_set accuracy evidence")
        if accuracy.get("event") != "replay_set_accuracy_evaluation_completed":
            raise ReleaseGateManifestError("replay_set accuracy evidence has the wrong event type")
        if accuracy.get("schema_version") != 1:
            raise ReleaseGateManifestError("replay_set accuracy evidence schema_version must be 1")
        accuracy_manifest_sha = accuracy.get("manifest_sha256")
        if (
            not isinstance(accuracy_manifest_sha, str)
            or accuracy_manifest_sha.lower() != manifest_sha256.lower()
        ):
            raise ReleaseGateManifestError(
                "replay_set accuracy evidence is bound to a different manifest SHA-256"
            )
        accuracy_canonical_sha = accuracy.get("canonical_sha256")
        if (
            not isinstance(accuracy_canonical_sha, str)
            or accuracy_canonical_sha.lower() != replay_set.canonical_hash.lower()
        ):
            raise ReleaseGateManifestError(
                "replay_set accuracy evidence is bound to a different manifest canonical hash"
            )
        for field in ("gate_passed", "accuracy_validated"):
            if type(accuracy.get(field)) is not bool:
                raise ReleaseGateManifestError(
                    f"replay_set accuracy evidence {field} must be a boolean"
                )
        if (
            accuracy.get("gate_passed") is True or accuracy.get("accuracy_validated") is True
        ) and not accuracy_metrics_are_defined(accuracy.get("metrics")):
            raise ReleaseGateManifestError(
                "replay_set accuracy evidence aggregate precision/recall are undefined"
            )
        if (
            accuracy.get("gate_passed") is True or accuracy.get("accuracy_validated") is True
        ) and not positive_strata_coverage_is_complete(accuracy):
            raise ReleaseGateManifestError(
                "replay_set accuracy evidence positive-stratum coverage is incomplete"
            )
        record["accuracy_evidence"] = {
            "path": _relative_path(accuracy_path, manifest_directory=manifest_directory),
            "sha256": sha256_file(accuracy_path),
            "gate_passed": accuracy["gate_passed"],
            "accuracy_validated": accuracy["accuracy_validated"],
        }
    if calibration_verification is not None:
        record["calibration_verification"] = _replay_set_calibration_verification_record(
            calibration_verification,
            replay_manifest=resolved,
            replay_manifest_sha256=manifest_sha256,
            replay_canonical_sha256=replay_set.canonical_hash,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
        )
    return record


def _normalise_nonnegative_float(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReleaseGateManifestError(f"{name} must be a finite non-negative number")
    try:
        normalised = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ReleaseGateManifestError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(normalised) or normalised < 0.0:
        raise ReleaseGateManifestError(f"{name} must be a finite non-negative number")
    return normalised


def _real_field_link_record(
    preflight_report: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    max_age_s: float,
) -> dict[str, Any]:
    """Create a hash-bound REAL field-link preflight record.

    The writer deliberately preserves an unready report as an explicit
    ``gate_passed=false`` diagnostic on the prerelease line.  The evaluator
    rechecks the file later, and the final release channel requires this
    record to be present and ready.
    """

    resolved = _resolve_file(preflight_report, name="real_field_link preflight report")
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the real_field_link preflight report"
        )
    verification = verify_field_link_report(
        resolved,
        require_ready=True,
        max_age_s=max_age_s,
    )
    reasons = [str(reason) for reason in verification.get("reasons", [])]
    record: dict[str, Any] = {
        "report_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "report_sha256": sha256_file(resolved),
        "report_bytes": resolved.stat().st_size,
        "max_age_s": max_age_s,
        "report_timestamp_epoch_s": verification.get("report_timestamp_epoch_s"),
        "hardware_link_ready": bool(verification.get("hardware_link_ready")),
        "gate_passed": bool(verification.get("gate_passed")),
    }
    summary = verification.get("summary")
    if isinstance(summary, Mapping):
        # Preserve the passive route decision in the release manifest so the
        # final REAL gate records which aircraft CIDR was expected.  These
        # fields remain optional for historical reports that predate route
        # probing; the strict verifier still enforces them when requested.
        record["route_checks_requested"] = bool(summary.get("route_checks_requested", False))
        record["route_checks_passed"] = summary.get("route_checks_passed")
        record["route_source_cidr"] = summary.get("route_source_cidr")
    if reasons:
        record["reasons"] = reasons
    return record


def _real_alert_link_record(
    bench_report: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Create a hash-bound REAL authenticated alert-link bench record.

    The report is accepted only after the same offline verifier used by the
    standalone CLI recomputes a passing result.  In addition, the source must
    be the explicit hardware event with a concrete non-loopback endpoint and
    authenticated/correlated ACKs.  This keeps a loopback software baseline
    out of the final release manifest even if its ``passed`` bit is copied.
    """

    requested = Path(bench_report).expanduser()
    if requested.is_symlink():
        raise ReleaseGateManifestError("real alert-link bench report must not be a symbolic link")
    resolved = _resolve_file(bench_report, name="real_alert_link bench report")
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the real_alert_link bench report"
        )
    document = _load_json_object(resolved, name="real_alert_link bench report")
    verification = verify_alert_link_bench_report(document, require_passed=True)
    if verification.get("gate_passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(f"real_alert_link bench report verifier failed{detail}")

    expected_contract: dict[str, object] = {
        "event": "alert_link_bench_passed",
        "schema_version": 1,
        "hardware_observed": True,
        "simulation_only": False,
        "remote_is_loopback": False,
        "correlated_ack_verified": True,
        "authenticated_ack_verified": True,
    }
    for field, expected in expected_contract.items():
        actual = document.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise ReleaseGateManifestError(
                "real_alert_link bench report "
                f"{field} does not identify a REAL authenticated hardware bench"
            )

    reasons_field = document.get("reasons")
    if not isinstance(reasons_field, list) or any(
        not isinstance(reason, str) for reason in reasons_field
    ):
        raise ReleaseGateManifestError(
            "real_alert_link bench report reasons must be a list of strings"
        )

    record: dict[str, Any] = {
        "report_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "report_sha256": sha256_file(resolved),
        "report_bytes": resolved.stat().st_size,
    }
    for field in (
        "event",
        "schema_version",
        "hardware_observed",
        "simulation_only",
        "remote_host",
        "remote_port",
        "remote_is_loopback",
        "hardware_id",
        "requested_round_trips",
        "round_trip_samples",
        "failed_round_trips",
        "transmission_attempts",
        "missing_ack_attempts",
        "packet_loss_rate",
        "ack_latency_p50_ms",
        "ack_latency_p95_ms",
        "correlated_ack_verified",
        "authenticated_ack_verified",
        "checks",
        "thresholds",
        "reasons",
    ):
        value = document.get(field)
        if isinstance(value, Mapping):
            record[field] = dict(value)
        elif isinstance(value, list):
            record[field] = list(value)
        else:
            record[field] = value
    # The source verifier passed with ``require_passed=True``; expose that
    # result as the manifest-level gate bit rather than trusting a copied
    # source field.
    record["gate_passed"] = True
    return record


def _real_target_accuracy_record(
    accuracy_report: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Create a hash-bound REAL target-geolocation accuracy record."""

    resolved = _resolve_file(accuracy_report, name="real_target_accuracy report")
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the real_target_accuracy report"
        )
    document = _load_json_object(resolved, name="real_target_accuracy report")
    verification = verify_geolocation_accuracy_report(
        document,
        require_gate_passed=False,
    )
    if verification.get("passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(f"real_target_accuracy report verifier failed{detail}")
    measurement = document.get("measurement")
    distance_metrics = document.get("distance_error_m")
    if not isinstance(measurement, Mapping) or not isinstance(distance_metrics, Mapping):
        raise ReleaseGateManifestError(
            "real_target_accuracy report must contain measurement and distance_error_m"
        )
    requirements = document.get("requirements")
    if not isinstance(requirements, Mapping) or not any(
        requirements.get(field) is not None
        for field in (
            "maximum_p95_m",
            "minimum_one_sigma_coverage",
            "minimum_two_sigma_coverage",
        )
    ):
        raise ReleaseGateManifestError(
            "real_target_accuracy report must declare an explicit accuracy threshold"
        )
    record: dict[str, Any] = {
        "report_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "report_sha256": sha256_file(resolved),
        "report_bytes": resolved.stat().st_size,
        "evidence_type": document.get("evidence_type"),
        "schema_version": document.get("schema_version"),
        "ground_truth_isolated": document.get("ground_truth_isolated"),
        "advisory_only": document.get("advisory_only"),
        "contains_flight_control": document.get("contains_flight_control"),
        "gate_passed": document.get("gate_passed"),
        "sample_count": measurement.get("sample_count"),
        "p95_error_m": distance_metrics.get("p95_m"),
        "one_sigma_coverage": measurement.get("one_sigma_coverage"),
        "two_sigma_coverage": measurement.get("two_sigma_coverage"),
    }
    report_reasons = document.get("reasons")
    if report_reasons:
        if not isinstance(report_reasons, list) or any(
            not isinstance(reason, str) for reason in report_reasons
        ):
            raise ReleaseGateManifestError(
                "real_target_accuracy report reasons must be a list of strings"
            )
        record["reasons"] = list(report_reasons)
    return record


def _real_target_state_evidence_record(
    capture_report: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Create a hash-bound REAL target-state capture record.

    The capture report is deliberately kept separate from the surveyed
    accuracy report: it proves that a live session had fresh GPS and
    ``RANGE_STATUS`` samples while the operator state was ``TGT``.  The
    writer stores only compact provenance/summary fields; the evaluator later
    reopens the complete capture report and runs the same verifier again.
    """

    resolved = _resolve_file(
        capture_report,
        name="real_target_state_evidence capture report",
    )
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the real_target_state_evidence report"
        )
    document = _load_json_object(
        resolved,
        name="real_target_state_evidence capture report",
    )
    verification = verify_field_session_capture(resolved)
    reasons = [
        str(reason)
        for reason in verification.get("reasons", [])
        if isinstance(reason, str) and reason
    ]
    capture = document.get("capture")
    validation = document.get("validation")
    if not isinstance(capture, Mapping):
        raise ReleaseGateManifestError(
            "real_target_state_evidence capture report must contain capture object"
        )
    if not isinstance(validation, Mapping):
        raise ReleaseGateManifestError(
            "real_target_state_evidence capture report must contain validation object"
        )

    strict_requested = capture.get("require_target_state_evidence")
    if strict_requested is not True:
        reasons.append(
            "real_target_state_evidence capture must enable require_target_state_evidence"
        )
    target_state = validation.get("target_state_evidence")
    if not isinstance(target_state, Mapping):
        reasons.append("real_target_state_evidence validation block is missing")
        target_state = {}
    if target_state.get("required") is not True:
        reasons.append("real_target_state_evidence validation required must be true")
    if target_state.get("state") != "TGT":
        reasons.append("real_target_state_evidence validation state must be TGT")
    if target_state.get("valid") is not True:
        reasons.append("real_target_state_evidence validation valid must be true")
    target_reasons = target_state.get("reasons")
    if target_reasons is not None:
        if not isinstance(target_reasons, list) or any(
            not isinstance(reason, str) for reason in target_reasons
        ):
            reasons.append(
                "real_target_state_evidence validation reasons must be a list of strings"
            )
        else:
            reasons.extend(str(reason) for reason in target_reasons)

    captured_records = capture.get("captured_records")
    if (
        isinstance(captured_records, bool)
        or not isinstance(captured_records, int)
        or captured_records < 1
    ):
        raise ReleaseGateManifestError(
            "real_target_state_evidence captured_records must be a positive integer"
        )
    state_path = validation.get("state_path")
    gps_records = target_state.get("gps_qualified_records")
    range_records = target_state.get("range_qualified_records")
    if not isinstance(state_path, list) or any(
        not isinstance(item, str) or not item.strip() for item in state_path
    ):
        raise ReleaseGateManifestError(
            "real_target_state_evidence state_path must be an array of state strings"
        )
    for name, value in (
        ("gps_qualified_records", gps_records),
        ("range_qualified_records", range_records),
    ):
        if not isinstance(value, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value
        ):
            raise ReleaseGateManifestError(
                f"real_target_state_evidence {name} must be an array of non-negative integers"
            )

    report_gate = document.get("gate_passed")
    if type(report_gate) is not bool:
        reasons.append("real_target_state_evidence capture gate_passed must be boolean")
        report_gate = False
    if report_gate is not True:
        reasons.append("real_target_state_evidence capture gate_passed is false")
    if verification.get("gate_passed") is not True:
        reasons.append("real_target_state_evidence capture verifier did not pass")

    # Deduplicate while preserving the verifier's deterministic order.
    reasons = list(dict.fromkeys(reasons))
    record: dict[str, Any] = {
        "report_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "report_sha256": sha256_file(resolved),
        "report_bytes": resolved.stat().st_size,
        "schema_version": document.get("schema_version"),
        "require_target_state_evidence": strict_requested is True,
        "gate_passed": not reasons,
        "captured_records": captured_records,
        "state_path": list(state_path),
        "gps_qualified_records": list(gps_records),
        "range_qualified_records": list(range_records),
    }
    if reasons:
        record["reasons"] = reasons
    return record


def _field_evidence_bundle_record(
    field_evidence_bundle: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a hash-bound six-strata field-evidence binding.

    The bundle is an advisory, read-only join of the field link, GNSS,
    execution, target-event, media-timeline and accuracy artifacts.  A
    diagnostic bundle with ``gate_passed=false`` is retained for prerelease
    bring-up, but its serialized summary must still be independently
    re-evaluatable and the report must require the real (non-SIM) domain.
    """

    requested = Path(field_evidence_bundle).expanduser()
    if requested.is_symlink():
        raise ReleaseGateManifestError("field evidence bundle report must not be a symbolic link")
    resolved = _resolve_file(field_evidence_bundle, name="field evidence bundle report")
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the field evidence bundle report"
        )
    verification = verify_field_evidence_bundle_report(
        resolved,
        require_gate_passed=False,
        require_real_domain=True,
        expected_release_label=release_label,
    )
    if verification.get("passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(f"field evidence bundle report verifier failed{detail}")
    document = verification.get("report")
    if not isinstance(document, Mapping):
        raise ReleaseGateManifestError(
            "field evidence bundle report verifier returned no report object"
        )
    checks = document.get("checks")
    strata = document.get("strata")
    if not isinstance(checks, Mapping) or not isinstance(strata, Mapping):
        raise ReleaseGateManifestError(
            "field evidence bundle report must contain checks and strata objects"
        )
    summary: dict[str, bool] = {}
    for name in (
        "all_inputs_present",
        "all_strata_passed",
        "cross_bindings_passed",
        "hashes_complete",
        "real_domain_required",
    ):
        value = checks.get(name)
        if type(value) is not bool:
            raise ReleaseGateManifestError(f"field evidence bundle checks.{name} must be a boolean")
        summary[name] = value
    strata_summary: dict[str, bool] = {}
    for name in (
        "field_link",
        "gnss",
        "execution",
        "target_event",
        "media_timeline",
        "accuracy",
    ):
        entry = strata.get(name)
        if not isinstance(entry, Mapping) or type(entry.get("gate_passed")) is not bool:
            raise ReleaseGateManifestError(
                f"field evidence bundle strata.{name}.gate_passed must be a boolean"
            )
        strata_summary[name] = bool(entry["gate_passed"])
    if summary["real_domain_required"] is not True:
        raise ReleaseGateManifestError(
            "field evidence bundle must require the real deployment domain"
        )

    record: dict[str, Any] = {
        "report_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "report_sha256": sha256_file(resolved),
        "report_bytes": resolved.stat().st_size,
        "evidence_type": document.get("evidence_type"),
        "schema_version": document.get("schema_version"),
        "release_label": document.get("release_label"),
        "bundle_id": document.get("bundle_id"),
        "gate_passed": document.get("gate_passed"),
        "release_eligible": document.get("release_eligible"),
        "hardware_acceptance": document.get("hardware_acceptance"),
        "real_flight_acceptance": document.get("real_flight_acceptance"),
        "software_only": document.get("software_only"),
        "read_only_evidence": document.get("read_only_evidence"),
        "advisory_only": document.get("advisory_only"),
        "ground_truth_isolated": document.get("ground_truth_isolated"),
        "contains_flight_control": document.get("contains_flight_control"),
        "contains_truth": document.get("contains_truth"),
        **summary,
        "strata": strata_summary,
    }
    return record


def _simulator_safety_record(
    simulator_safety_evidence: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a hash-bound record for the deterministic SIM S4 evidence.

    Unlike the optional REAL preflight diagnostic, S4 is a deterministic
    software gate.  A caller can bind it only after both the source artifact
    and the independent verifier pass; this keeps the generated manifest
    valid against ``simulatorSafetyRecord`` instead of emitting a record that
    is already known to be false.
    """

    resolved = _resolve_file(simulator_safety_evidence, name="simulator_safety evidence")
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the simulator_safety evidence"
        )
    artifact = _load_json_object(resolved, name="simulator_safety evidence")
    if artifact.get("event") != "simulator_safety_acceptance_finished":
        raise ReleaseGateManifestError("simulator_safety evidence has the wrong event type")
    if artifact.get("runtime_mode") != "sim":
        raise ReleaseGateManifestError("simulator_safety evidence runtime_mode must be 'sim'")
    if artifact.get("release_label") != release_label:
        raise ReleaseGateManifestError(
            "simulator_safety evidence release_label does not match the product manifest"
        )
    if artifact.get("gate_passed") is not True:
        raise ReleaseGateManifestError("simulator_safety evidence gate_passed must be true")
    verification = verify_simulator_safety_file(
        resolved,
        expected_release_label=release_label,
    )
    if verification.get("gate_passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(f"simulator_safety evidence verifier did not pass{detail}")
    checks = verification.get("checks")
    if not isinstance(checks, Mapping):
        raise ReleaseGateManifestError(
            "simulator_safety evidence verifier checks must be an object"
        )
    if verification.get("event") != "simulator_safety_evidence_verified":
        raise ReleaseGateManifestError(
            "simulator_safety evidence verifier returned the wrong event"
        )
    return {
        "evidence_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "evidence_sha256": sha256_file(resolved),
        "event": artifact.get("event"),
        "runtime_mode": artifact.get("runtime_mode"),
        "release_label": artifact.get("release_label"),
        "gate_passed": bool(
            artifact.get("gate_passed") is True and verification.get("gate_passed") is True
        ),
        "verifier_event": verification.get("event"),
        "verifier_exit_code": 0 if verification.get("gate_passed") is True else 1,
        "checks": dict(checks),
    }


def _actuator_safety_record(
    actuator_safety_evidence: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict hash-bound record for the actuator safety bench.

    The actuator bench is deliberately a software-only HIL contract.  It
    proves bounded semantic-to-PWM mapping and the neutral/cancel/lost-lock/
    watchdog transitions, while explicitly carrying the fact that no
    physical output was present.  A release manifest may bind it only after
    the independent verifier recomputes the canonical result.
    """

    resolved = _resolve_file(
        actuator_safety_evidence,
        name="actuator_safety evidence",
    )
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be the actuator_safety evidence"
        )
    artifact = _load_json_object(resolved, name="actuator_safety evidence")
    expected_fields = (
        ("event", "actuator_safety_software_acceptance_finished"),
        ("runtime_mode", "HIL"),
        ("release_label", release_label),
        ("gate_passed", True),
        ("software_only", True),
        ("physical_outputs_present", False),
        ("propulsion_disconnected", "not_applicable_in_software_fixture"),
    )
    for field, expected in expected_fields:
        if artifact.get(field) != expected:
            raise ReleaseGateManifestError(f"actuator_safety evidence {field} must be {expected!r}")

    verification = verify_actuator_safety_evidence(
        artifact,
        expected_release_label=release_label,
    )
    if verification.get("event") != "actuator_safety_software_evidence_verified":
        raise ReleaseGateManifestError("actuator_safety evidence verifier returned the wrong event")
    if verification.get("runtime_mode") != "HIL":
        raise ReleaseGateManifestError(
            "actuator_safety evidence verifier runtime_mode must be 'HIL'"
        )
    if verification.get("release_label") != release_label:
        raise ReleaseGateManifestError(
            "actuator_safety evidence verifier release_label does not match the product manifest"
        )
    if verification.get("gate_passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(f"actuator_safety evidence verifier did not pass{detail}")
    checks = verification.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError(
            "actuator_safety evidence verifier checks must be an object with all values true"
        )
    return {
        "evidence_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "evidence_sha256": sha256_file(resolved),
        "event": artifact.get("event"),
        "runtime_mode": artifact.get("runtime_mode"),
        "release_label": artifact.get("release_label"),
        "gate_passed": True,
        "software_only": artifact.get("software_only"),
        "physical_outputs_present": artifact.get("physical_outputs_present"),
        "propulsion_disconnected": artifact.get("propulsion_disconnected"),
        "verifier_event": verification.get("event"),
        "verifier_exit_code": 0,
        "checks": dict(checks),
    }


def _simulator_scenario_matrix_record(
    scenario_matrix_evidence: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict binding for the deterministic three-airframe matrix."""

    resolved = _resolve_file(
        scenario_matrix_evidence,
        name="simulator_scenario_matrix evidence",
    )
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be simulator_scenario_matrix evidence"
        )
    artifact = _load_json_object(resolved, name="simulator_scenario_matrix evidence")
    try:
        verification = verify_simulator_scenario_matrix_file(
            resolved,
            expected_release_label=release_label,
            require_all_airframes=True,
            require_fault_coverage=(release_label == "0.3.0"),
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ReleaseGateManifestError(
            f"simulator_scenario_matrix evidence verifier failed: {exc}"
        ) from exc
    if verification.get("gate_passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(
            f"simulator_scenario_matrix evidence verifier did not pass{detail}"
        )
    checks = verification.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError(
            "simulator_scenario_matrix verifier checks must be an object of true values"
        )
    canonical_sha = artifact.get("sha256")
    if not isinstance(canonical_sha, str) or _SHA256_RE.fullmatch(canonical_sha) is None:
        raise ReleaseGateManifestError(
            "simulator_scenario_matrix evidence sha256 must be 64 hexadecimal characters"
        )

    claims = artifact.get("claims")
    if not isinstance(claims, Mapping):
        raise ReleaseGateManifestError(
            "simulator_scenario_matrix evidence claims must be an object"
        )
    bound_claims: dict[str, Any] = {}
    for key in (
        "shared_truth_free_scenario_runner",
        "shared_ai_authority_watchdog_path",
        "all_selected_scenarios_passed",
    ):
        bound_claims[key] = claims.get(key)

    return {
        "evidence_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "evidence_sha256": sha256_file(resolved),
        "evidence_bytes": resolved.stat().st_size,
        "canonical_sha256": canonical_sha.lower(),
        "schema_version": artifact.get("schema_version"),
        "event": artifact.get("event"),
        "product": artifact.get("product"),
        "release_label": artifact.get("release_label"),
        "gate_passed": artifact.get("gate_passed"),
        "deterministic_gate_passed": artifact.get("deterministic_gate_passed"),
        "scenario_count": artifact.get("scenario_count"),
        "scenario_ids": list(artifact.get("scenario_ids", [])),
        "modes": list(artifact.get("modes", [])),
        "required_modes": list(artifact.get("required_modes", [])),
        "enable_ai": artifact.get("enable_ai"),
        "arm_on_start": artifact.get("arm_on_start"),
        "repeatability_checked": artifact.get("repeatability_checked"),
        "truth_loaded": artifact.get("truth_loaded"),
        "truth_free": artifact.get("truth_free"),
        "claims": bound_claims,
        "verifier_event": verification.get("event"),
        "verifier_exit_code": 0,
        "checks": dict(checks),
    }


def _simulator_combined_record(
    combined_evidence: str | Path,
    *,
    release_mode_evidence: str | Path | None,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict hash-bound binding for the combined Windows SIM run."""

    if release_mode_evidence is None:
        raise ReleaseGateManifestError(
            "simulator_combined_evidence requires simulator_combined_release_mode_evidence"
        )
    combined_path = _resolve_file(combined_evidence, name="simulator_combined evidence")
    release_mode_path = _resolve_file(
        release_mode_evidence,
        name="simulator_combined release-mode evidence",
    )
    if combined_path == output_identity or release_mode_path == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be simulator_combined evidence"
        )
    try:
        binding = build_combined_sim_binding(
            combined_path,
            release_mode_evidence_path=release_mode_path,
            manifest_directory=manifest_directory,
            expected_release_label=release_label,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ReleaseGateManifestError(
            f"simulator_combined evidence verifier did not pass: {exc}"
        ) from exc
    return binding


def _simulator_external_evidence_record(
    external_evidence: str | Path,
    *,
    external_revalidation_evidence: str | Path | None,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
    parity: Mapping[str, str],
) -> dict[str, Any]:
    """Create a strict E7 binding for the six-source external SIM join.

    The goal-acceptance wrapper already produces a join and a second
    ``--join`` revalidation file.  Keeping that pair as a first-class E7
    binding closes the seam where a passing goal snapshot could otherwise be
    detached from the release manifest.  The writer intentionally requires
    both files, recomputes the source join, checks the parity hashes against
    the four-mode manifest input, and stores compact source hashes for audit
    readability.  A failed source or revalidation is an input error rather
    than an incomplete release record.
    """

    if external_revalidation_evidence is None:
        raise ReleaseGateManifestError(
            "simulator_external_evidence requires simulator_external_evidence_revalidation"
        )

    join_path = _resolve_file(
        external_evidence,
        name="simulator_external_evidence join",
    )
    revalidation_path = _resolve_file(
        external_revalidation_evidence,
        name="simulator_external_evidence revalidation",
    )
    if output_identity in {join_path, revalidation_path}:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be simulator_external_evidence"
        )
    if join_path == revalidation_path:
        raise ReleaseGateManifestError(
            "simulator_external_evidence join and revalidation must be different files"
        )

    join = _load_json_object(join_path, name="simulator_external_evidence join")
    if join.get("schema_version") != 1:
        raise ReleaseGateManifestError("simulator_external_evidence join schema_version must be 1")
    if join.get("event") != "simulator_external_runtime_ai_evidence_verified":
        raise ReleaseGateManifestError("simulator_external_evidence join has the wrong event type")
    if join.get("release_label") != release_label:
        raise ReleaseGateManifestError(
            "simulator_external_evidence join release_label does not match the product manifest"
        )
    if join.get("gate_passed") is not True:
        raise ReleaseGateManifestError("simulator_external_evidence join gate_passed must be true")

    shared = join.get("shared")
    if not isinstance(shared, Mapping):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join shared field must be an object"
        )
    for field in (
        "image_digest_consistent",
        "release_label_consistent",
        "truth_free_perception",
        "hardware_control_disabled",
    ):
        if shared.get(field) is not True:
            raise ReleaseGateManifestError(
                f"simulator_external_evidence join shared.{field} must be true"
            )
    for field in ("visual_accuracy", "real_hardware"):
        if shared.get(field) != "not_claimed":
            raise ReleaseGateManifestError(
                f"simulator_external_evidence join shared.{field} must be 'not_claimed'"
            )

    claims = join.get("claims")
    if not isinstance(claims, Mapping):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join claims field must be an object"
        )
    expected_claims: dict[str, object] = {
        "external_runtime_gate": "passed",
        "ai_target_policy_gate": "passed",
        "hardware_acceptance": "not_claimed",
        "operator_visual_acceptance": "not_claimed",
        "truth_free": True,
    }
    for field, expected in expected_claims.items():
        if claims.get(field) != expected:
            raise ReleaseGateManifestError(
                f"simulator_external_evidence join claims.{field} must be {expected!r}"
            )

    parity_record = join.get("parity")
    if not isinstance(parity_record, Mapping):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join parity field must be an object"
        )
    parity_hashes = parity_record.get("hashes")
    if not isinstance(parity_hashes, Mapping):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join parity.hashes must be an object"
        )
    normalized_join_parity = _normalise_parity(parity_hashes)
    normalized_expected_parity = _normalise_parity(parity)
    if normalized_join_parity != normalized_expected_parity:
        raise ReleaseGateManifestError(
            "simulator_external_evidence join parity hashes do not match "
            "the release-gate parity input"
        )
    join_parity_path_value = parity_record.get("path")
    if not isinstance(join_parity_path_value, str) or not join_parity_path_value.strip():
        raise ReleaseGateManifestError("simulator_external_evidence join parity.path is missing")
    join_parity_path = _resolve_bound_file(
        join_parity_path_value,
        base=join_path.parent,
        name="simulator_external_evidence parity source",
    )
    join_parity_sha = sha256_file(join_parity_path)
    declared_join_parity_sha = parity_record.get("sha256")
    if (
        not isinstance(declared_join_parity_sha, str)
        or not _SHA256_RE.fullmatch(declared_join_parity_sha)
        or declared_join_parity_sha.lower() != join_parity_sha.lower()
    ):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join parity SHA-256 does not match"
        )
    declared_join_parity_bytes = parity_record.get("bytes")
    if (
        type(declared_join_parity_bytes) is not int
        or declared_join_parity_bytes != join_parity_path.stat().st_size
    ):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join parity byte count does not match"
        )
    if parity_record.get("release_label") != release_label:
        raise ReleaseGateManifestError(
            "simulator_external_evidence join parity release_label does not match"
        )

    vehicles = join.get("vehicles")
    if not isinstance(vehicles, Mapping) or set(vehicles) != set(EXTERNAL_REQUIRED_VEHICLES):
        raise ReleaseGateManifestError(
            "simulator_external_evidence join vehicles must contain exactly "
            f"{EXTERNAL_REQUIRED_VEHICLES!r}"
        )
    source_hashes: dict[str, dict[str, dict[str, Any]]] = {}
    source_paths: set[Path] = set()
    for vehicle in EXTERNAL_REQUIRED_VEHICLES:
        pair = vehicles.get(vehicle)
        if not isinstance(pair, Mapping):
            raise ReleaseGateManifestError(
                f"simulator_external_evidence join {vehicle} record must be an object"
            )
        source_hashes[vehicle] = {}
        for role in ("runtime", "ai"):
            record = pair.get(role)
            if not isinstance(record, Mapping):
                raise ReleaseGateManifestError(
                    f"simulator_external_evidence join {vehicle} {role} record must be an object"
                )
            source_path_value = record.get("path")
            if not isinstance(source_path_value, str) or not source_path_value.strip():
                raise ReleaseGateManifestError(
                    f"simulator_external_evidence join {vehicle} {role} path is missing"
                )
            source_path = _resolve_bound_file(
                source_path_value,
                base=join_path.parent,
                name=f"simulator_external_evidence {vehicle} {role} source",
            )
            if source_path in {join_path, revalidation_path, join_parity_path}:
                raise ReleaseGateManifestError(
                    "simulator_external_evidence source files must be separate from "
                    "the join, revalidation and parity files"
                )
            if source_path in source_paths:
                raise ReleaseGateManifestError(
                    "simulator_external_evidence source files must be distinct"
                )
            source_paths.add(source_path)
            actual_sha = sha256_file(source_path)
            actual_bytes = source_path.stat().st_size
            declared_sha = record.get("sha256")
            if (
                not isinstance(declared_sha, str)
                or not _SHA256_RE.fullmatch(declared_sha)
                or declared_sha.lower() != actual_sha.lower()
            ):
                raise ReleaseGateManifestError(
                    f"simulator_external_evidence {vehicle} {role} source SHA-256 does not match"
                )
            declared_bytes = record.get("bytes")
            if type(declared_bytes) is not int or declared_bytes != actual_bytes:
                raise ReleaseGateManifestError(
                    f"simulator_external_evidence {vehicle} {role} source byte count does not match"
                )
            if record.get("gate_passed") is not True:
                raise ReleaseGateManifestError(
                    f"simulator_external_evidence {vehicle} {role} gate_passed must be true"
                )
            if role == "ai":
                for field in (
                    "ai_policy_gate_passed",
                    "target_policy_gate_passed",
                    "target_exercise_accepted",
                ):
                    if record.get(field) is not True:
                        raise ReleaseGateManifestError(
                            f"simulator_external_evidence {vehicle} AI {field} must be true"
                        )
            source_hashes[vehicle][role] = {
                "sha256": actual_sha,
                "bytes": actual_bytes,
            }

    join_sha = sha256_file(join_path)
    try:
        revalidation = revalidate_simulator_external_evidence(
            join_path,
            expected_release_label=release_label,
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise ReleaseGateManifestError(
            f"simulator_external_evidence revalidation failed: {exc}"
        ) from exc
    if revalidation.get("source_recomputed") is not True:
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation did not recompute sources"
        )
    if revalidation.get("gate_passed") is not True:
        reasons = [
            str(reason)
            for reason in revalidation.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation did not pass" + detail
        )

    revalidation_doc = _load_json_object(
        revalidation_path,
        name="simulator_external_evidence revalidation",
    )
    if revalidation_doc.get("schema_version") != 1:
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation schema_version must be 1"
        )
    if revalidation_doc.get("event") != "simulator_external_runtime_ai_evidence_revalidated":
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation has the wrong event type"
        )
    declared_report_path = revalidation_doc.get("report_path")
    if not isinstance(declared_report_path, str) or not declared_report_path.strip():
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation report_path is missing"
        )
    bound_report_path = _resolve_bound_file(
        declared_report_path,
        base=revalidation_path.parent,
        name="simulator_external_evidence revalidation join path",
    )
    if bound_report_path != join_path:
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation is bound to a different join"
        )
    if revalidation_doc.get("report_sha256") != join_sha:
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation join SHA-256 does not match"
        )
    if revalidation_doc.get("report_bytes") != join_path.stat().st_size:
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation join byte count does not match"
        )
    for field, expected in (
        ("source_recomputed", True),
        ("release_label", release_label),
        ("expected_release_label", release_label),
        ("gate_passed", True),
    ):
        if revalidation_doc.get(field) != expected:
            raise ReleaseGateManifestError(
                f"simulator_external_evidence revalidation {field} does not match"
            )
    if revalidation_doc.get("reasons") != []:
        raise ReleaseGateManifestError(
            "simulator_external_evidence revalidation reasons must be empty"
        )

    join_revalidation_sha = sha256_file(revalidation_path)
    checks = {
        "join_schema_valid": True,
        "join_event_valid": True,
        "join_release_label_matches": True,
        "join_gate_passed": True,
        "join_shared_boundaries_passed": True,
        "join_claims_passed": True,
        "join_parity_matches": True,
        "six_sources_distinct": len(source_paths) == 6,
        "six_sources_hash_bound": len(source_hashes) == 3,
        "revalidation_schema_valid": True,
        "revalidation_join_matches": True,
        "revalidation_source_recomputed": True,
        "revalidation_gate_passed": True,
    }
    if any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError(
            "simulator_external_evidence binding checks did not all pass"
        )

    return {
        "join_path": _relative_path(join_path, manifest_directory=manifest_directory),
        "join_sha256": join_sha,
        "join_bytes": join_path.stat().st_size,
        "revalidation_path": _relative_path(
            revalidation_path,
            manifest_directory=manifest_directory,
        ),
        "revalidation_sha256": join_revalidation_sha,
        "revalidation_bytes": revalidation_path.stat().st_size,
        "release_label": release_label,
        "gate_passed": True,
        "source_recomputed": True,
        "parity": {
            "path": _relative_path(join_parity_path, manifest_directory=manifest_directory),
            "sha256": join_parity_sha,
            "bytes": join_parity_path.stat().st_size,
            "release_label": release_label,
            "hashes": normalized_join_parity,
        },
        "source_hashes": source_hashes,
        "checks": checks,
    }


def _simulator_3d_control_record(
    evidence_paths: Sequence[str | Path],
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict binding for the three deterministic 3-D records."""

    normalized = [Path(value).expanduser().resolve() for value in evidence_paths]
    if output_identity in normalized:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be simulator_3d_control evidence"
        )
    try:
        return build_simulator_3d_control_binding(
            normalized,
            manifest_directory=manifest_directory,
            expected_release_label=release_label,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ReleaseGateManifestError(
            f"simulator_3d_control evidence verifier did not pass: {exc}"
        ) from exc


def _simulator_scene_stream_set_record(
    evidence_paths: Sequence[str | Path],
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict binding for the three deterministic scene streams."""

    normalized = [Path(value).expanduser().resolve() for value in evidence_paths]
    if output_identity in normalized:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be simulator_scene_stream_set evidence"
        )
    try:
        # Keep this import lazy: scene-stream verification imports the simulator
        # runner and should not be initialized by ordinary release-gate imports.
        from .simulator.scene_stream_set import build_binding

        return build_binding(
            normalized,
            manifest_directory=manifest_directory,
            expected_release_label=release_label,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        raise ReleaseGateManifestError(
            f"simulator_scene_stream_set evidence verifier did not pass: {exc}"
        ) from exc


def _px4_airframe_transaction_record(
    transaction_evidence: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict binding for the shared three-airframe Mission run."""

    resolved = _resolve_file(
        transaction_evidence,
        name="px4_airframe_transaction evidence",
    )
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be px4_airframe_transaction evidence"
        )
    artifact = _load_json_object(resolved, name="px4_airframe_transaction evidence")
    if artifact.get("schema") != "multidetect.px4-airframe-mission-transaction-acceptance":
        raise ReleaseGateManifestError("px4_airframe_transaction evidence has the wrong schema")
    if artifact.get("schema_version") != 1:
        raise ReleaseGateManifestError("px4_airframe_transaction evidence schema_version must be 1")
    if _artifact_runtime_mode(artifact) != "sim":
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence runtime_mode must be 'sim'"
        )
    if artifact.get("release_label") != release_label:
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence release_label does not match the product manifest"
        )
    if artifact.get("gate_passed") is not True:
        raise ReleaseGateManifestError("px4_airframe_transaction evidence gate_passed must be true")
    if artifact.get("shared_dispatcher") != "compile_executable_mission":
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence dispatcher must be compile_executable_mission"
        )
    if artifact.get("shared_transport") != "in_process_mission_item_int":
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence transport must be in_process_mission_item_int"
        )
    if artifact.get("truth_free") is not True:
        raise ReleaseGateManifestError("px4_airframe_transaction evidence truth_free must be true")
    if artifact.get("physical_output_enabled") is not False:
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence physical output must be disabled"
        )

    entries = artifact.get("entries")
    expected_classes = ("fixed-wing", "multirotor", "vtol")
    class_names: list[str] = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping) and isinstance(entry.get("vehicle_class"), str):
                class_names.append(entry["vehicle_class"])
    if tuple(sorted(class_names)) != tuple(sorted(expected_classes)):
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence must contain fixed-wing, "
            "multirotor and vtol exactly once"
        )

    # Import lazily to keep direct release-manifest CLI startup independent of
    # the acceptance runner's mission module graph.
    from .px4_mission_acceptance import (
        verify_px4_mission_transaction_acceptance,
    )

    verification = verify_px4_mission_transaction_acceptance(artifact)
    if verification.get("gate_passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("errors", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(
            f"px4_airframe_transaction evidence verifier did not pass{detail}"
        )
    checks = verification.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError(
            "px4_airframe_transaction verifier checks must be an object of true values"
        )
    artifact_checks = artifact.get("checks")
    if not isinstance(artifact_checks, Mapping) or dict(artifact_checks) != dict(checks):
        raise ReleaseGateManifestError(
            "px4_airframe_transaction evidence checks do not match verifier output"
        )

    return {
        "evidence_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "evidence_sha256": sha256_file(resolved),
        "evidence_bytes": resolved.stat().st_size,
        "schema": artifact["schema"],
        "schema_version": artifact["schema_version"],
        "runtime_mode": "sim",
        "release_label": artifact["release_label"],
        "gate_passed": True,
        "shared_dispatcher": artifact["shared_dispatcher"],
        "shared_transport": artifact["shared_transport"],
        "truth_free": True,
        "physical_output_enabled": False,
        "vehicle_classes": list(expected_classes),
        "checks": dict(checks),
    }


def _px4_reported_position_response_matrix_record(
    matrix_evidence: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict binding for external PX4-reported motion by three airframes."""

    resolved = _resolve_file(
        matrix_evidence,
        name="px4_reported_position_response_matrix evidence",
    )
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be px4_reported_position_response_matrix evidence"
        )
    artifact = _load_json_object(resolved, name="px4_reported_position_response_matrix evidence")
    try:
        from scripts.verify_px4_reported_position_response_matrix import verify_file

        verification = verify_file(resolved)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ReleaseGateManifestError(
            f"px4_reported_position_response_matrix verifier failed: {exc}"
        ) from exc
    if verification.get("gate_passed") is not True:
        detail = "; ".join(str(reason) for reason in verification.get("reasons", []) if reason)
        raise ReleaseGateManifestError(
            "px4_reported_position_response_matrix verifier did not pass"
            + (f": {detail}" if detail else "")
        )
    checks = verification.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError(
            "px4_reported_position_response_matrix verifier checks must all be true"
        )
    if artifact.get("release_label") != release_label:
        raise ReleaseGateManifestError(
            "px4_reported_position_response_matrix release_label does not match product"
        )
    entries = artifact.get("entries")
    vehicle_classes = (
        sorted(
            entry.get("vehicle_class")
            for entry in entries
            if isinstance(entries, list) and isinstance(entry, Mapping)
        )
        if isinstance(entries, list)
        else []
    )
    return {
        "evidence_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "evidence_sha256": sha256_file(resolved),
        "evidence_bytes": resolved.stat().st_size,
        "schema": artifact.get("schema"),
        "schema_version": artifact.get("schema_version"),
        "release_label": artifact.get("release_label"),
        "gate_passed": True,
        "matrix_sha256": artifact.get("matrix_sha256"),
        "vehicle_classes": vehicle_classes,
        "truth_free": artifact.get("truth_free"),
        "hardware_acceptance": artifact.get("hardware_acceptance"),
        "hil_acceptance": artifact.get("hil_acceptance"),
        "real_acceptance": artifact.get("real_acceptance"),
        "checks": dict(checks),
    }


def _px4_reported_position_response_campaign_record(
    campaign_evidence: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
) -> dict[str, Any]:
    """Create a strict binding for PX4 response repeats and injected faults."""

    resolved = _resolve_file(
        campaign_evidence,
        name="px4_reported_position_response_campaign evidence",
    )
    if resolved == output_identity:
        raise ReleaseGateManifestError(
            "release-gate output cannot also be px4 response campaign evidence"
        )
    artifact = _load_json_object(resolved, name="px4 response campaign evidence")
    try:
        from scripts.verify_px4_reported_position_response_campaign import verify_file

        verification = verify_file(resolved)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise ReleaseGateManifestError(f"px4 response campaign verifier failed: {exc}") from exc
    if verification.get("gate_passed") is not True:
        detail = "; ".join(str(reason) for reason in verification.get("reasons", []) if reason)
        raise ReleaseGateManifestError(
            "px4 response campaign verifier did not pass" + (f": {detail}" if detail else "")
        )
    checks = verification.get("checks")
    if not isinstance(checks, Mapping) or any(value is not True for value in checks.values()):
        raise ReleaseGateManifestError("px4 response campaign checks must all be true")
    if artifact.get("release_label") != release_label:
        raise ReleaseGateManifestError("px4 response campaign release_label does not match product")
    positives = artifact.get("positive_runs")
    faults = artifact.get("fault_runs")
    return {
        "evidence_path": _relative_path(resolved, manifest_directory=manifest_directory),
        "evidence_sha256": sha256_file(resolved),
        "evidence_bytes": resolved.stat().st_size,
        "schema": artifact.get("schema"),
        "schema_version": artifact.get("schema_version"),
        "release_label": artifact.get("release_label"),
        "gate_passed": True,
        "campaign_sha256": artifact.get("campaign_sha256"),
        "positive_run_count": len(positives) if isinstance(positives, list) else 0,
        "fault_run_count": len(faults) if isinstance(faults, list) else 0,
        "vehicle_classes": sorted(
            artifact.get("statistics", {})
            if isinstance(artifact.get("statistics"), Mapping)
            else {}
        ),
        "truth_free": artifact.get("truth_free"),
        "hardware_acceptance": artifact.get("hardware_acceptance"),
        "hil_acceptance": artifact.get("hil_acceptance"),
        "real_acceptance": artifact.get("real_acceptance"),
        "checks": dict(checks),
    }


def _qgc_operator_visual_acceptance_record(
    evidence_paths: Sequence[str | Path],
    *,
    output_identity: Path,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Bind one independently verified human QGC review per runtime mode."""

    if not evidence_paths:
        raise ReleaseGateManifestError("qgc_operator_visual_acceptance evidence must not be empty")
    by_mode: dict[str, dict[str, Any]] = {}
    resolved_seen: set[Path] = set()
    machine_sources_seen: set[str] = set()
    screenshots_seen: set[str] = set()
    for requested in evidence_paths:
        source = Path(requested).expanduser()
        if source.is_symlink():
            raise ReleaseGateManifestError(
                "qgc operator visual acceptance evidence must not be a symbolic link"
            )
        resolved = _resolve_file(requested, name="qgc operator visual acceptance evidence")
        if resolved == output_identity:
            raise ReleaseGateManifestError(
                "release-gate output cannot also be QGC visual acceptance evidence"
            )
        if resolved in resolved_seen:
            raise ReleaseGateManifestError(
                f"QGC visual acceptance evidence is duplicated: {resolved}"
            )
        resolved_seen.add(resolved)
        artifact = _load_json_object(resolved, name="QGC visual acceptance evidence")
        try:
            verification = verify_qgc_visual_acceptance(resolved)
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise ReleaseGateManifestError(f"QGC visual acceptance verifier failed: {exc}") from exc
        if verification.get("gate_passed") is not True:
            reasons = verification.get("reasons")
            detail = (
                "; ".join(str(reason) for reason in reasons if reason)
                if isinstance(reasons, list)
                else ""
            )
            raise ReleaseGateManifestError(
                "QGC visual acceptance evidence did not pass" + (f": {detail}" if detail else "")
            )
        mode_value = artifact.get("runtime_mode")
        mode = mode_value.strip().lower() if isinstance(mode_value, str) else ""
        if mode not in REQUIRED_MODES:
            raise ReleaseGateManifestError(
                f"QGC visual acceptance runtime_mode is invalid: {mode_value!r}"
            )
        if mode in by_mode:
            raise ReleaseGateManifestError(
                f"QGC visual acceptance supplies duplicate mode: {mode!r}"
            )
        review = artifact.get("review")
        if not isinstance(review, Mapping):
            raise ReleaseGateManifestError("QGC visual acceptance review is unavailable")
        positive = artifact.get("positive_visual_evidence")
        if not isinstance(positive, Mapping):
            raise ReleaseGateManifestError(
                "QGC visual acceptance positive_visual_evidence is unavailable"
            )
        machine_source = positive.get("machine_report")
        screenshot = positive.get("screenshot")
        executable = positive.get("qgc_executable")
        ui_contract = positive.get("ui_contract")
        if not all(
            isinstance(value, Mapping)
            for value in (machine_source, screenshot, executable, ui_contract)
        ):
            raise ReleaseGateManifestError(
                "QGC visual acceptance source identities are unavailable"
            )
        assert isinstance(machine_source, Mapping)
        assert isinstance(screenshot, Mapping)
        assert isinstance(executable, Mapping)
        assert isinstance(ui_contract, Mapping)
        machine_source_path = str(machine_source.get("path", ""))
        screenshot_path = str(screenshot.get("path", ""))
        if machine_source_path in machine_sources_seen:
            raise ReleaseGateManifestError(
                "QGC visual acceptance reuses one machine report across runtime modes"
            )
        if screenshot_path in screenshots_seen:
            raise ReleaseGateManifestError(
                "QGC visual acceptance reuses one screenshot across runtime modes"
            )
        machine_sources_seen.add(machine_source_path)
        screenshots_seen.add(screenshot_path)
        by_mode[mode] = {
            "runtime_mode": mode,
            "path": _relative_path(resolved, manifest_directory=manifest_directory),
            "sha256": sha256_file(resolved),
            "bytes": resolved.stat().st_size,
            "review_sha256": artifact.get("review_sha256"),
            "reviewer": review.get("reviewer"),
            "reviewed_at_utc": review.get("reviewed_at_utc"),
            "machine_run_id": machine_source.get("run_id"),
            "machine_report_sha256": machine_source.get("sha256"),
            "screenshot_sha256": screenshot.get("sha256"),
            "qgc_executable_sha256": executable.get("sha256"),
            "ui_contract_hash": ui_contract.get("ui_contract_hash"),
            "gate_passed": True,
        }
    records = [by_mode[mode] for mode in REQUIRED_MODES if mode in by_mode]
    covered_modes = [record["runtime_mode"] for record in records]
    complete = covered_modes == list(REQUIRED_MODES)
    return {
        "schema": "multidetect.qgc-operator-visual-acceptance-set",
        "schema_version": 1,
        "records": records,
        "covered_modes": covered_modes,
        "required_modes": list(REQUIRED_MODES),
        "complete": complete,
        "gate_passed": complete,
    }


def _px4_mission_feedback_record(
    feedback_evidence: Sequence[object],
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
    px4_mission_path_hash: str,
) -> dict[str, object]:
    """Create the exact HIL/REAL x three-airframe feedback binding."""

    try:
        # Keep the Mission/observer module graph off ordinary manifest imports.
        from .px4_mission_feedback_binding import (
            build_px4_mission_feedback_binding,
        )

        return build_px4_mission_feedback_binding(
            feedback_evidence,
            manifest_directory=manifest_directory,
            output_identity=output_identity,
            release_label=release_label,
            px4_mission_path_hash=px4_mission_path_hash,
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise ReleaseGateManifestError(
            f"px4_mission_feedback evidence verifier did not pass: {exc}"
        ) from exc


def _pixhawk_covariance_record(
    covariance_evidence: Sequence[object],
    *,
    output_identity: Path,
    manifest_directory: Path,
    release_label: str,
    px4_mission_path_hash: str,
) -> dict[str, object]:
    """Create the exact HIL/REAL x three-airframe covariance binding."""

    try:
        from .pixhawk_covariance_binding import build_pixhawk_covariance_binding

        return build_pixhawk_covariance_binding(
            covariance_evidence,
            manifest_directory=manifest_directory,
            output_identity=output_identity,
            release_label=release_label,
            px4_mission_path_hash=px4_mission_path_hash,
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        raise ReleaseGateManifestError(
            f"pixhawk_covariance evidence verifier did not pass: {exc}"
        ) from exc


def _model_review_record(
    model_review_bundle: str | Path,
    *,
    output_identity: Path,
    manifest_directory: Path,
) -> dict[str, Any]:
    """Create a strict release-gate binding for a model-review bundle."""

    requested = Path(model_review_bundle).expanduser()
    if requested.is_symlink():
        raise ReleaseGateManifestError("model-review bundle must not be a symbolic link")
    resolved = _resolve_file(requested, name="model-review bundle")
    if resolved == output_identity:
        raise ReleaseGateManifestError("release-gate output cannot also be the model-review bundle")
    verification = verify_model_review_bundle(resolved, require_eligible=False)
    if verification.get("passed") is not True:
        reasons = [
            str(reason)
            for reason in verification.get("reasons", [])
            if isinstance(reason, str) and reason
        ]
        detail = f": {'; '.join(reasons)}" if reasons else ""
        raise ReleaseGateManifestError(f"model-review bundle verification did not pass{detail}")
    document = verification.get("document")
    if not isinstance(document, Mapping):
        raise ReleaseGateManifestError("model-review bundle document must be an object")
    model = document.get("model")
    if not isinstance(model, Mapping):
        raise ReleaseGateManifestError("model-review bundle model must be an object")
    eligible = document.get("production_approval_eligible")
    if type(eligible) is not bool:
        raise ReleaseGateManifestError(
            "model-review bundle production_approval_eligible must be a boolean"
        )
    return {
        "path": _relative_path(resolved, manifest_directory=manifest_directory),
        "sha256": sha256_file(resolved),
        "model_id": model.get("model_id"),
        "model_version": model.get("model_version"),
        "model_artifact_sha256": model.get("artifact_sha256"),
        "review_status": document.get("review_status"),
        "production_approval_eligible": eligible,
        "production_approved": document.get("production_approved"),
        "flight_control_enabled": document.get("flight_control_enabled"),
        "physical_release_enabled": document.get("physical_release_enabled"),
        "gate_passed": eligible and verification.get("passed") is True,
        "reasons": list(document.get("failure_reasons", [])),
    }


def _normalise_mode_mapping(
    values: Mapping[str, Any] | None,
    *,
    name: str,
) -> dict[str, Any]:
    if values is None:
        return {}
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        mode = str(key).strip().lower()
        if mode not in REQUIRED_MODES:
            raise ReleaseGateManifestError(f"{name} has unknown runtime mode: {key!r}")
        if mode in normalized and normalized[mode] != value:
            raise ReleaseGateManifestError(f"{name} supplies duplicate mode aliases: {key!r}")
        normalized[mode] = value
    return normalized


def build_release_gate_manifest(
    evidence: Mapping[str, str | Path],
    *,
    parity: Mapping[str, object],
    statuses: Mapping[str, Mapping[str, object]] | None = None,
    output_path: str | Path,
    version_manifest_path: str | Path | None = None,
    artifact_events: Mapping[str, str] | None = None,
    notes: Mapping[str, str] | None = None,
    replay_set_manifest: str | Path | None = None,
    replay_set_accuracy_evidence: str | Path | None = None,
    replay_set_calibration_verification: str | Path | None = None,
    real_field_link_preflight: str | Path | None = None,
    real_field_link_max_age_s: float = DEFAULT_FIELD_LINK_MAX_AGE_S,
    simulator_safety_evidence: str | Path | None = None,
    simulator_scenario_matrix_evidence: str | Path | None = None,
    simulator_combined_evidence: str | Path | None = None,
    simulator_combined_release_mode_evidence: str | Path | None = None,
    simulator_external_evidence: str | Path | None = None,
    simulator_external_evidence_revalidation: str | Path | None = None,
    px4_airframe_transaction_evidence: str | Path | None = None,
    px4_reported_position_response_matrix_evidence: str | Path | None = None,
    px4_reported_position_response_campaign_evidence: str | Path | None = None,
    qgc_operator_visual_acceptance_evidence: Sequence[str | Path] | None = None,
    px4_mission_feedback_evidence: Sequence[object] | None = None,
    pixhawk_covariance_evidence: Sequence[object] | None = None,
    simulator_3d_control_evidence: Sequence[str | Path] | None = None,
    real_target_accuracy_report: str | Path | None = None,
    real_target_state_evidence_report: str | Path | None = None,
    actuator_safety_evidence: str | Path | None = None,
    field_evidence_bundle: str | Path | None = None,
    real_alert_link_bench_report: str | Path | None = None,
    model_review_bundle: str | Path | None = None,
    simulator_scene_stream_set_evidence: Sequence[str | Path] | None = None,
) -> dict[str, Any]:
    """Build a schema-v1 manifest from four explicit evidence paths.

    ``evidence`` must contain exactly ``replay``, ``sim``, ``hil`` and
    ``real``.  Status values are never inferred from evidence documents;
    omitted values use :func:`default_mode_statuses` and therefore keep the
    generated gate closed.  The optional REAL target-state capture is accepted
    only as an explicit hash-bound report; an unready report is retained as a
    prerelease diagnostic when ``--allow-incomplete`` is used.
    """

    output = Path(output_path).expanduser().resolve()
    manifest_directory = output.parent
    manifest_directory.mkdir(parents=True, exist_ok=True)
    normalized_evidence: dict[str, str | Path] = {}
    for key, value in evidence.items():
        mode = str(key).strip().lower()
        if mode in normalized_evidence and normalized_evidence[mode] != value:
            raise ReleaseGateManifestError(f"evidence supplies duplicate mode aliases: {key!r}")
        normalized_evidence[mode] = value
    missing_modes = sorted(set(REQUIRED_MODES).difference(normalized_evidence))
    unknown_modes = sorted(set(normalized_evidence).difference(REQUIRED_MODES))
    if missing_modes or unknown_modes:
        raise ReleaseGateManifestError(
            f"evidence modes must be exactly {list(REQUIRED_MODES)!r}; "
            f"missing={missing_modes!r}, unknown={unknown_modes!r}"
        )
    if output.exists():
        try:
            output_identity = output.resolve()
        except OSError:
            output_identity = output
    else:
        output_identity = output

    version: ProductVersion
    try:
        version = load_product_version(version_manifest_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReleaseGateManifestError(f"product version manifest is invalid: {exc}") from exc

    normalized_parity = _normalise_parity(parity)
    normalized_real_field_link_max_age_s = _normalise_nonnegative_float(
        real_field_link_max_age_s,
        name="real_field_link_max_age_s",
    )
    normalized_events = _normalise_mode_mapping(artifact_events, name="artifact_events")
    normalized_notes = _normalise_mode_mapping(notes, name="notes")
    if statuses is not None:
        normalized_status_input = _normalise_mode_mapping(statuses, name="statuses")
    else:
        normalized_status_input = {}
    # The mapping is populated below; validate optional metadata up front.
    for mode in normalized_events:
        if not isinstance(normalized_events[mode], str) or not normalized_events[mode].strip():
            raise ReleaseGateManifestError(f"{mode} artifact event must be a non-empty string")
    for mode in normalized_notes:
        if not isinstance(normalized_notes[mode], str):
            raise ReleaseGateManifestError(f"{mode} notes must be a string")

    resolved_paths: dict[str, Path] = {}
    path_owners: dict[Path, str] = {}
    records: dict[str, dict[str, Any]] = {}
    for mode in REQUIRED_MODES:
        resolved = _resolve_file(normalized_evidence[mode], name=f"{mode} evidence")
        if resolved == output_identity:
            raise ReleaseGateManifestError("manifest output cannot also be an evidence file")
        owner = path_owners.get(resolved)
        if owner is not None:
            raise ReleaseGateManifestError(
                f"evidence file {resolved} is reused by both {owner} and {mode}"
            )
        path_owners[resolved] = mode
        resolved_paths[mode] = resolved

    for mode in REQUIRED_MODES:
        evidence_sha, artifact_event, artifact_gate = _read_evidence(
            resolved_paths[mode], mode=mode
        )
        supplied_statuses = normalized_status_input.get(mode)
        if supplied_statuses is not None and not isinstance(supplied_statuses, Mapping):
            raise ReleaseGateManifestError(f"{mode} statuses must be an object")
        mode_statuses = _normalise_statuses(mode, supplied_statuses)
        record: dict[str, Any] = {
            "evidence_path": _relative_path(
                resolved_paths[mode], manifest_directory=manifest_directory
            ),
            "evidence_sha256": evidence_sha,
            "gate_passed": bool(artifact_gate and _status_gate(mode, mode_statuses)),
            "parity": dict(normalized_parity),
            "operator_visual_acceptance": mode_statuses["operator_visual_acceptance"],
            "hardware_acceptance": mode_statuses["hardware_acceptance"],
            "real_control_acceptance": mode_statuses["real_control_acceptance"],
            "mission_path_acceptance": mode_statuses["mission_path_acceptance"],
        }
        explicit_event = normalized_events.get(mode)
        if explicit_event is not None:
            explicit_event = explicit_event.strip()
            if artifact_event is not None and explicit_event != artifact_event:
                raise ReleaseGateManifestError(
                    f"{mode} artifact event override does not match evidence event"
                )
            record["artifact_event"] = explicit_event
        elif artifact_event is not None:
            record["artifact_event"] = artifact_event
        if mode in normalized_notes:
            record["notes"] = normalized_notes[mode]
        records[mode] = record

    replay_set_record = (
        _replay_set_record(
            replay_set_manifest,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            accuracy_evidence=replay_set_accuracy_evidence,
            calibration_verification=replay_set_calibration_verification,
        )
        if replay_set_manifest is not None
        else None
    )
    if (
        replay_set_accuracy_evidence is not None or replay_set_calibration_verification is not None
    ) and replay_set_manifest is None:
        raise ReleaseGateManifestError(
            "replay_set accuracy/calibration evidence requires replay_set_manifest"
        )

    real_field_link_record = (
        _real_field_link_record(
            real_field_link_preflight,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            max_age_s=normalized_real_field_link_max_age_s,
        )
        if real_field_link_preflight is not None
        else None
    )

    real_alert_link_record = (
        _real_alert_link_record(
            real_alert_link_bench_report,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
        )
        if real_alert_link_bench_report is not None
        else None
    )

    real_target_accuracy_record = (
        _real_target_accuracy_record(
            real_target_accuracy_report,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
        )
        if real_target_accuracy_report is not None
        else None
    )

    real_target_state_evidence_record = (
        _real_target_state_evidence_record(
            real_target_state_evidence_report,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
        )
        if real_target_state_evidence_report is not None
        else None
    )

    simulator_safety_record = (
        _simulator_safety_record(
            simulator_safety_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if simulator_safety_evidence is not None
        else None
    )

    actuator_safety_record = (
        _actuator_safety_record(
            actuator_safety_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if actuator_safety_evidence is not None
        else None
    )

    simulator_scenario_matrix_record = (
        _simulator_scenario_matrix_record(
            simulator_scenario_matrix_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if simulator_scenario_matrix_evidence is not None
        else None
    )

    simulator_combined_record = (
        _simulator_combined_record(
            simulator_combined_evidence,
            release_mode_evidence=simulator_combined_release_mode_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if simulator_combined_evidence is not None
        else None
    )
    if simulator_combined_release_mode_evidence is not None and simulator_combined_evidence is None:
        raise ReleaseGateManifestError(
            "simulator_combined_release_mode_evidence requires simulator_combined_evidence"
        )

    simulator_external_record = (
        _simulator_external_evidence_record(
            simulator_external_evidence,
            external_revalidation_evidence=simulator_external_evidence_revalidation,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
            parity=normalized_parity,
        )
        if simulator_external_evidence is not None
        else None
    )
    if simulator_external_evidence_revalidation is not None and simulator_external_evidence is None:
        raise ReleaseGateManifestError(
            "simulator_external_evidence_revalidation requires simulator_external_evidence"
        )

    px4_airframe_transaction_record = (
        _px4_airframe_transaction_record(
            px4_airframe_transaction_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if px4_airframe_transaction_evidence is not None
        else None
    )

    px4_reported_position_response_matrix_record = (
        _px4_reported_position_response_matrix_record(
            px4_reported_position_response_matrix_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if px4_reported_position_response_matrix_evidence is not None
        else None
    )

    px4_reported_position_response_campaign_record = (
        _px4_reported_position_response_campaign_record(
            px4_reported_position_response_campaign_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if px4_reported_position_response_campaign_evidence is not None
        else None
    )

    qgc_operator_visual_acceptance_record = (
        _qgc_operator_visual_acceptance_record(
            qgc_operator_visual_acceptance_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
        )
        if qgc_operator_visual_acceptance_evidence is not None
        else None
    )

    px4_mission_feedback_record = (
        _px4_mission_feedback_record(
            px4_mission_feedback_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
            px4_mission_path_hash=normalized_parity["px4_mission_path_hash"],
        )
        if px4_mission_feedback_evidence is not None
        else None
    )

    pixhawk_covariance_record = (
        _pixhawk_covariance_record(
            pixhawk_covariance_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
            px4_mission_path_hash=normalized_parity["px4_mission_path_hash"],
        )
        if pixhawk_covariance_evidence is not None
        else None
    )

    simulator_3d_control_record = (
        _simulator_3d_control_record(
            simulator_3d_control_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if simulator_3d_control_evidence is not None
        else None
    )

    simulator_scene_stream_set_record = (
        _simulator_scene_stream_set_record(
            simulator_scene_stream_set_evidence,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if simulator_scene_stream_set_evidence is not None
        else None
    )

    field_evidence_bundle_record = (
        _field_evidence_bundle_record(
            field_evidence_bundle,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
            release_label=version.release_label,
        )
        if field_evidence_bundle is not None
        else None
    )

    model_review_record = (
        _model_review_record(
            model_review_bundle,
            output_identity=output_identity,
            manifest_directory=manifest_directory,
        )
        if model_review_bundle is not None
        else None
    )

    # Keep insertion order stable for readable diffs and reproducible bytes.
    document: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": version.product,
        "release_label": version.release_label,
        "modes": records,
    }
    if real_field_link_record is not None:
        document["real_field_link"] = real_field_link_record
    if real_alert_link_record is not None:
        document["real_alert_link"] = real_alert_link_record
    if real_target_accuracy_record is not None:
        document["real_target_accuracy"] = real_target_accuracy_record
    if real_target_state_evidence_record is not None:
        document["real_target_state_evidence"] = real_target_state_evidence_record
    if replay_set_record is not None:
        document["replay_set"] = replay_set_record
    if simulator_safety_record is not None:
        document["simulator_safety"] = simulator_safety_record
    if actuator_safety_record is not None:
        document["actuator_safety"] = actuator_safety_record
    if simulator_scenario_matrix_record is not None:
        document["simulator_scenario_matrix"] = simulator_scenario_matrix_record
    if simulator_combined_record is not None:
        document["simulator_combined"] = simulator_combined_record
    if simulator_external_record is not None:
        document["simulator_external_evidence"] = simulator_external_record
    if px4_airframe_transaction_record is not None:
        document["px4_airframe_transaction"] = px4_airframe_transaction_record
    if px4_reported_position_response_matrix_record is not None:
        document["px4_reported_position_response_matrix"] = (
            px4_reported_position_response_matrix_record
        )
    if px4_reported_position_response_campaign_record is not None:
        document["px4_reported_position_response_campaign"] = (
            px4_reported_position_response_campaign_record
        )
    if qgc_operator_visual_acceptance_record is not None:
        document["qgc_operator_visual_acceptance"] = qgc_operator_visual_acceptance_record
    if px4_mission_feedback_record is not None:
        document["px4_mission_feedback"] = px4_mission_feedback_record
    if pixhawk_covariance_record is not None:
        document["pixhawk_covariance"] = pixhawk_covariance_record
    if simulator_3d_control_record is not None:
        document["simulator_3d_control"] = simulator_3d_control_record
    if simulator_scene_stream_set_record is not None:
        document["simulator_scene_stream_set"] = simulator_scene_stream_set_record
    if field_evidence_bundle_record is not None:
        document["field_evidence_bundle"] = field_evidence_bundle_record
    if model_review_record is not None:
        document["model_review"] = model_review_record
    return document


def write_release_gate_manifest(
    destination: str | Path,
    document: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> Path:
    """Atomically write a release-gate manifest and fsync the temporary file."""

    output = Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise ReleaseGateManifestError(f"release-gate manifest already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
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


def create_release_gate_manifest(
    output_path: str | Path,
    replay_evidence: str | Path,
    sim_evidence: str | Path,
    hil_evidence: str | Path,
    real_evidence: str | Path,
    parity: Mapping[str, object],
    statuses: Mapping[str, Mapping[str, object]] | None = None,
    version_manifest_path: str | Path | None = None,
    artifact_events: Mapping[str, str] | None = None,
    notes: Mapping[str, str] | None = None,
    overwrite: bool = True,
    replay_set_manifest: str | Path | None = None,
    replay_set_accuracy_evidence: str | Path | None = None,
    replay_set_calibration_verification: str | Path | None = None,
    real_field_link_preflight: str | Path | None = None,
    real_target_accuracy_report: str | Path | None = None,
    real_target_state_evidence_report: str | Path | None = None,
    simulator_safety_evidence: str | Path | None = None,
    simulator_scenario_matrix_evidence: str | Path | None = None,
    simulator_combined_evidence: str | Path | None = None,
    simulator_combined_release_mode_evidence: str | Path | None = None,
    simulator_external_evidence: str | Path | None = None,
    simulator_external_evidence_revalidation: str | Path | None = None,
    px4_airframe_transaction_evidence: str | Path | None = None,
    px4_reported_position_response_matrix_evidence: str | Path | None = None,
    px4_reported_position_response_campaign_evidence: str | Path | None = None,
    qgc_operator_visual_acceptance_evidence: Sequence[str | Path] | None = None,
    px4_mission_feedback_evidence: Sequence[object] | None = None,
    pixhawk_covariance_evidence: Sequence[object] | None = None,
    simulator_3d_control_evidence: Sequence[str | Path] | None = None,
    actuator_safety_evidence: str | Path | None = None,
    field_evidence_bundle: str | Path | None = None,
    real_alert_link_bench_report: str | Path | None = None,
    real_field_link_max_age_s: float = DEFAULT_FIELD_LINK_MAX_AGE_S,
    model_review_bundle: str | Path | None = None,
    simulator_scene_stream_set_evidence: Sequence[str | Path] | None = None,
) -> dict[str, Any]:
    """Convenience API that builds and atomically writes the manifest."""

    document = build_release_gate_manifest(
        {
            "replay": replay_evidence,
            "sim": sim_evidence,
            "hil": hil_evidence,
            "real": real_evidence,
        },
        parity=parity,
        statuses=statuses,
        output_path=output_path,
        version_manifest_path=version_manifest_path,
        artifact_events=artifact_events,
        notes=notes,
        replay_set_manifest=replay_set_manifest,
        replay_set_accuracy_evidence=replay_set_accuracy_evidence,
        replay_set_calibration_verification=replay_set_calibration_verification,
        real_field_link_preflight=real_field_link_preflight,
        real_field_link_max_age_s=real_field_link_max_age_s,
        real_target_accuracy_report=real_target_accuracy_report,
        real_target_state_evidence_report=real_target_state_evidence_report,
        simulator_safety_evidence=simulator_safety_evidence,
        actuator_safety_evidence=actuator_safety_evidence,
        simulator_scenario_matrix_evidence=simulator_scenario_matrix_evidence,
        simulator_combined_evidence=simulator_combined_evidence,
        simulator_combined_release_mode_evidence=simulator_combined_release_mode_evidence,
        simulator_external_evidence=simulator_external_evidence,
        simulator_external_evidence_revalidation=simulator_external_evidence_revalidation,
        px4_airframe_transaction_evidence=px4_airframe_transaction_evidence,
        px4_reported_position_response_matrix_evidence=(
            px4_reported_position_response_matrix_evidence
        ),
        px4_reported_position_response_campaign_evidence=(
            px4_reported_position_response_campaign_evidence
        ),
        qgc_operator_visual_acceptance_evidence=(qgc_operator_visual_acceptance_evidence),
        px4_mission_feedback_evidence=px4_mission_feedback_evidence,
        pixhawk_covariance_evidence=pixhawk_covariance_evidence,
        simulator_3d_control_evidence=simulator_3d_control_evidence,
        field_evidence_bundle=field_evidence_bundle,
        real_alert_link_bench_report=real_alert_link_bench_report,
        model_review_bundle=model_review_bundle,
        simulator_scene_stream_set_evidence=simulator_scene_stream_set_evidence,
    )
    write_release_gate_manifest(output_path, document, overwrite=overwrite)
    return document


def _parity_from_cli(args: argparse.Namespace) -> dict[str, str]:
    cli_values = {
        field: getattr(args, field) for field in PARITY_FIELDS if getattr(args, field) is not None
    }
    if args.parity_json is not None and cli_values:
        raise ReleaseGateManifestError(
            "choose --parity-json or the five CLI parity hashes, not both"
        )
    if args.parity_json is not None:
        return load_parity_hashes(args.parity_json)
    if len(cli_values) != len(PARITY_FIELDS):
        missing = sorted(set(PARITY_FIELDS).difference(cli_values))
        raise ReleaseGateManifestError(
            f"provide --parity-json or all five parity hashes; missing={missing!r}"
        )
    return _normalise_parity(cli_values)


def _status_arguments(parser: argparse.ArgumentParser, mode: str) -> None:
    prefix = f"--{mode}-"
    parser.add_argument(
        prefix + "evidence",
        prefix + "artifact",
        f"--{mode}",
        dest=f"{mode}_evidence",
        type=Path,
        required=True,
        help=f"{mode.upper()} acceptance evidence JSON",
    )
    parser.add_argument(
        prefix + "visual",
        prefix + "visual-acceptance",
        prefix + "operator-visual-acceptance",
        dest=f"{mode}_visual",
        default=None,
        metavar="STATUS",
        help="operator Fly View visual status (default: not_claimed)",
    )
    parser.add_argument(
        prefix + "mission",
        prefix + "mission-acceptance",
        prefix + "mission-path-acceptance",
        dest=f"{mode}_mission",
        default=None,
        metavar="STATUS",
        help="Mission path status (default: not_claimed)",
    )
    parser.add_argument(
        prefix + "hardware",
        prefix + "hardware-acceptance",
        dest=f"{mode}_hardware",
        default=None,
        metavar="STATUS",
        help="hardware status (mode-aware fail-closed default)",
    )
    parser.add_argument(
        prefix + "real-control",
        prefix + "real-control-acceptance",
        dest=f"{mode}_real_control",
        default=None,
        metavar="STATUS",
        help="real-control status (mode-aware fail-closed default)",
    )
    parser.add_argument(prefix + "artifact-event", dest=f"{mode}_event", metavar="EVENT")
    parser.add_argument(prefix + "notes", dest=f"{mode}_notes", metavar="TEXT")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a deterministic schema-v1 REPLAY/SIM/HIL/REAL E7 release-gate manifest. "
            "Evidence and acceptance statuses are explicit; missing statuses stay fail-closed."
        )
    )
    parser.add_argument("out_positional", nargs="?", type=Path, help="manifest output path")
    parser.add_argument(
        "--out",
        "--output",
        "--output-manifest",
        dest="out_option",
        type=Path,
        help="manifest output path",
    )
    parser.add_argument(
        "--version-manifest",
        "--product-version",
        dest="version_manifest",
        type=Path,
        help="canonical configs/product-version.json (default: repository manifest)",
    )
    parser.add_argument(
        "--replay-set-manifest",
        type=Path,
        help=(
            "optional stratified REPLAY manifest; it is hash-bound and must be complete "
            "for a final release"
        ),
    )
    parser.add_argument(
        "--replay-set-accuracy-evidence",
        type=Path,
        help=(
            "optional hash-bound IoU accuracy report produced by evaluate-replay-set; "
            "requires --replay-set-manifest"
        ),
    )
    parser.add_argument(
        "--replay-set-calibration-verification",
        type=Path,
        help=(
            "optional independently verified replay-set threshold-calibration artifact; "
            "requires --replay-set-manifest"
        ),
    )
    parser.add_argument(
        "--real-field-link-preflight",
        type=Path,
        help=(
            "optional saved REAL field-link preflight; it is hash-bound and "
            "required when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--real-field-link-max-age-s",
        type=float,
        default=DEFAULT_FIELD_LINK_MAX_AGE_S,
        help=(
            "maximum age in seconds for the bound REAL field-link observation "
            f"(default: {DEFAULT_FIELD_LINK_MAX_AGE_S:g})"
        ),
    )
    parser.add_argument(
        "--real-alert-link-bench",
        "--real-alert-link-report",
        dest="real_alert_link_bench_report",
        type=Path,
        help=(
            "optional hash-bound REAL authenticated alert-link bench report; "
            "it is required when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--real-target-accuracy-report",
        "--target-geolocation-accuracy-report",
        type=Path,
        help=(
            "optional offline WGS-84 target-geolocation accuracy report; it is "
            "hash-bound and required when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--real-target-state-evidence-report",
        "--target-state-evidence-report",
        type=Path,
        help=(
            "optional REAL field-session capture report produced with "
            "--require-target-state-evidence; it is hash-bound and required "
            "when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--simulator-safety-evidence",
        "--sim-safety-evidence",
        type=Path,
        help=(
            "optional deterministic SIM S4 safety evidence; it is hash-bound and "
            "required when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--actuator-safety-evidence",
        "--executioner-safety-evidence",
        type=Path,
        help=(
            "optional deterministic actuator safety software-bench evidence; "
            "it is hash-bound and required when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--simulator-scenario-matrix-evidence",
        "--sim-scenario-matrix-evidence",
        type=Path,
        help=(
            "optional deterministic three-airframe SIM scenario matrix; it is "
            "hash-bound and required when evaluating a final release"
        ),
    )
    parser.add_argument(
        "--simulator-combined-evidence",
        "--combined-sim-evidence",
        type=Path,
        help=(
            "optional strict Windows PX4/Gazebo/QGC combined-SIM evidence; "
            "requires --simulator-combined-release-mode-evidence"
        ),
    )
    parser.add_argument(
        "--simulator-combined-release-mode-evidence",
        "--combined-sim-release-mode-evidence",
        type=Path,
        help=("hash-bound sim release-mode envelope for --simulator-combined-evidence"),
    )
    parser.add_argument(
        "--simulator-external-evidence",
        "--external-sim-evidence",
        type=Path,
        help=(
            "optional hash-bound six-source external SIM runtime/live-AI join; "
            "requires --simulator-external-evidence-revalidation"
        ),
    )
    parser.add_argument(
        "--simulator-external-evidence-revalidation",
        "--external-sim-evidence-revalidation",
        type=Path,
        help=("independently generated --join revalidation for --simulator-external-evidence"),
    )
    parser.add_argument(
        "--px4-airframe-transaction-evidence",
        "--shared-px4-mission-transaction-evidence",
        type=Path,
        help=(
            "optional deterministic three-airframe PX4 Mission transaction "
            "evidence; binds VTOL/fixed-wing/multirotor durable journals and "
            "is required for the final release"
        ),
    )
    parser.add_argument(
        "--px4-reported-position-response-matrix-evidence",
        "--px4-response-matrix-evidence",
        type=Path,
        help=(
            "hash-bound fixed-wing, multirotor and VTOL external PX4-reported "
            "position-response matrix; required for final release"
        ),
    )
    parser.add_argument(
        "--px4-reported-position-response-campaign-evidence",
        "--px4-response-campaign-evidence",
        type=Path,
        help=(
            "hash-bound three-airframe PX4 response repeatability and "
            "fault-injection campaign; required for final release"
        ),
    )
    parser.add_argument(
        "--qgc-operator-visual-acceptance-evidence",
        "--qgc-visual-acceptance-evidence",
        dest="qgc_operator_visual_acceptance_evidence",
        action="append",
        type=Path,
        help=(
            "repeatable source-bound human QGC visual review; final release "
            "requires one independently verified record for REPLAY, SIM, HIL and REAL"
        ),
    )
    parser.add_argument(
        "--px4-mission-feedback-evidence",
        "--px4-mission-feedback-binding",
        dest="px4_mission_feedback_evidence",
        action="append",
        nargs=5,
        metavar=("MODE", "VEHICLE", "MISSION", "PROFILE", "REPORT"),
        help=(
            "repeatable receive-only Mission feedback binding; provide exactly "
            "HIL and REAL records for each of fixed-wing, multirotor and VTOL. "
            "Each value group is MODE VEHICLE MISSION_JSON "
            "SIGNED_RUNTIME_PROFILE_JSON REPORT_JSON"
        ),
    )
    parser.add_argument(
        "--pixhawk-covariance-evidence",
        dest="pixhawk_covariance_evidence",
        action="append",
        nargs=3,
        metavar=("MODE", "VEHICLE", "REPORT"),
        help=(
            "repeatable schema-v3 Pixhawk covariance observation; provide exactly "
            "HIL and REAL reports for fixed-wing, multirotor and VTOL"
        ),
    )
    parser.add_argument(
        "--simulator-3d-control-evidence",
        "--sim-3d-control-evidence",
        dest="simulator_3d_control_evidence",
        action="append",
        type=Path,
        help=(
            "repeatable deterministic 3-D SIM control evidence; provide exactly "
            "one VTOL, fixed-wing and multirotor record; it is hash-bound and "
            "required for the final release"
        ),
    )
    parser.add_argument(
        "--simulator-scene-stream-set-evidence",
        "--sim-scene-stream-set-evidence",
        dest="simulator_scene_stream_set_evidence",
        action="append",
        type=Path,
        help=(
            "repeatable deterministic three-airframe SIM scene-stream set evidence; "
            "provide exactly one VTOL, fixed-wing and multirotor record; it is "
            "hash-bound and required for the final release"
        ),
    )
    parser.add_argument(
        "--field-evidence-bundle",
        "--real-field-evidence-bundle",
        type=Path,
        help=(
            "optional hash-bound six-strata field evidence bundle; it must "
            "require the real deployment domain and is required for the final release"
        ),
    )
    parser.add_argument(
        "--model-review-bundle",
        "--model-review",
        type=Path,
        help=(
            "optional hash-bound fire/smoke model-review envelope; it is authoritative "
            "when supplied and required when evaluating a final release"
        ),
    )
    parity_group = parser.add_argument_group("shared parity hashes")
    parity_group.add_argument(
        "--parity-json",
        "--parity-file",
        "--parity",
        dest="parity_json",
        type=Path,
        help="JSON object containing the five parity hashes",
    )
    for field in PARITY_FIELDS:
        aliases = ["--" + field.replace("_", "-")]
        aliases.append("--parity-" + field.replace("_", "-"))
        parity_group.add_argument(*aliases, dest=field, metavar="SHA256")
    for mode in REQUIRED_MODES:
        _status_arguments(parser, mode)
    parser.add_argument(
        "--status",
        dest="status_specs",
        action="append",
        default=[],
        metavar="MODE.FIELD=STATUS",
        help=(
            "repeatable status override, e.g. --status real.visual=passed; "
            "field aliases visual/mission/hardware/real-control are accepted"
        ),
    )
    parser.add_argument(
        "--allow-incomplete",
        "--report-only",
        action="store_true",
        help="write a manifest with pending/not_claimed statuses and return zero",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="refuse to replace an existing output manifest",
    )
    return parser


def _parse_status_specs(specs: Sequence[str]) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    field_aliases = {
        "visual": "operator_visual_acceptance",
        "visual_acceptance": "operator_visual_acceptance",
        "operator_visual": "operator_visual_acceptance",
        "operator_visual_acceptance": "operator_visual_acceptance",
        "mission": "mission_path_acceptance",
        "mission_acceptance": "mission_path_acceptance",
        "mission_path": "mission_path_acceptance",
        "mission_path_acceptance": "mission_path_acceptance",
        "hardware": "hardware_acceptance",
        "hardware_acceptance": "hardware_acceptance",
        "real-control": "real_control_acceptance",
        "real-control-acceptance": "real_control_acceptance",
        "real_control": "real_control_acceptance",
        "real_control_acceptance": "real_control_acceptance",
    }
    for spec in specs:
        if "=" not in spec:
            raise ReleaseGateManifestError(
                f"status specification must be MODE.FIELD=STATUS: {spec!r}"
            )
        left, value = spec.split("=", 1)
        if "." not in left:
            raise ReleaseGateManifestError(
                f"status specification must be MODE.FIELD=STATUS: {spec!r}"
            )
        mode, field = (part.strip().lower() for part in left.split(".", 1))
        if mode not in REQUIRED_MODES:
            raise ReleaseGateManifestError(f"status specification has unknown mode: {mode!r}")
        canonical = field_aliases.get(field)
        if canonical is None:
            raise ReleaseGateManifestError(f"status specification has unknown field: {field!r}")
        normalized = _normalise_status(value, name=f"{mode}.{canonical}")
        mode_values = parsed.setdefault(mode, {})
        previous = mode_values.get(canonical)
        if previous is not None and previous != normalized:
            raise ReleaseGateManifestError(
                f"status specification supplies conflicting values for {mode}.{canonical}"
            )
        mode_values[canonical] = normalized
    return parsed


def _cli_statuses(args: argparse.Namespace) -> dict[str, dict[str, str]]:
    statuses: dict[str, dict[str, str]] = {}
    for mode in REQUIRED_MODES:
        values: dict[str, str] = {}
        for argument, field in (
            ("visual", "operator_visual_acceptance"),
            ("mission", "mission_path_acceptance"),
            ("hardware", "hardware_acceptance"),
            ("real_control", "real_control_acceptance"),
        ):
            value = getattr(args, f"{mode}_{argument}")
            if value is not None:
                values[field] = value
        statuses[mode] = values
    for mode, values in _parse_status_specs(args.status_specs).items():
        for field, value in values.items():
            previous = statuses[mode].get(field)
            if previous is not None and previous != value:
                raise ReleaseGateManifestError(f"status options conflict for {mode}.{field}")
            statuses[mode][field] = value
    return statuses


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = args.out_option or args.out_positional
    if output is None:
        parser.error("an output path is required via --out or positional argument")
    try:
        parity = _parity_from_cli(args)
        evidence = {mode: getattr(args, f"{mode}_evidence") for mode in REQUIRED_MODES}
        events = {
            mode: getattr(args, f"{mode}_event")
            for mode in REQUIRED_MODES
            if getattr(args, f"{mode}_event") is not None
        }
        notes = {
            mode: getattr(args, f"{mode}_notes")
            for mode in REQUIRED_MODES
            if getattr(args, f"{mode}_notes") is not None
        }
        document = build_release_gate_manifest(
            evidence,
            parity=parity,
            statuses=_cli_statuses(args),
            output_path=output,
            version_manifest_path=args.version_manifest,
            artifact_events=events,
            notes=notes,
            replay_set_manifest=args.replay_set_manifest,
            replay_set_accuracy_evidence=args.replay_set_accuracy_evidence,
            replay_set_calibration_verification=args.replay_set_calibration_verification,
            real_field_link_preflight=args.real_field_link_preflight,
            real_field_link_max_age_s=args.real_field_link_max_age_s,
            real_alert_link_bench_report=args.real_alert_link_bench_report,
            real_target_accuracy_report=args.real_target_accuracy_report,
            real_target_state_evidence_report=args.real_target_state_evidence_report,
            simulator_safety_evidence=args.simulator_safety_evidence,
            actuator_safety_evidence=args.actuator_safety_evidence,
            simulator_scenario_matrix_evidence=args.simulator_scenario_matrix_evidence,
            simulator_combined_evidence=args.simulator_combined_evidence,
            simulator_combined_release_mode_evidence=args.simulator_combined_release_mode_evidence,
            simulator_external_evidence=args.simulator_external_evidence,
            simulator_external_evidence_revalidation=args.simulator_external_evidence_revalidation,
            px4_airframe_transaction_evidence=args.px4_airframe_transaction_evidence,
            px4_reported_position_response_matrix_evidence=(
                args.px4_reported_position_response_matrix_evidence
            ),
            px4_reported_position_response_campaign_evidence=(
                args.px4_reported_position_response_campaign_evidence
            ),
            qgc_operator_visual_acceptance_evidence=(args.qgc_operator_visual_acceptance_evidence),
            px4_mission_feedback_evidence=args.px4_mission_feedback_evidence,
            pixhawk_covariance_evidence=args.pixhawk_covariance_evidence,
            simulator_3d_control_evidence=args.simulator_3d_control_evidence,
            simulator_scene_stream_set_evidence=args.simulator_scene_stream_set_evidence,
            field_evidence_bundle=args.field_evidence_bundle,
            model_review_bundle=args.model_review_bundle,
        )
        destination = write_release_gate_manifest(
            output,
            document,
            overwrite=not args.no_overwrite,
        )
        print(json.dumps(document, ensure_ascii=False, indent=2))
        incomplete = any(not record["gate_passed"] for record in document["modes"].values())
        # Optional top-level bindings are authoritative when supplied.  A
        # caller may deliberately write an unready prerelease diagnostic with
        # --allow-incomplete, but the strict writer path must not report
        # success for a manifest whose field-link/replay-set binding is false.
        for binding_name in (
            "replay_set",
            "real_field_link",
            "real_alert_link",
            "real_target_accuracy",
            "real_target_state_evidence",
            "simulator_safety",
            "actuator_safety",
            "simulator_scenario_matrix",
            "simulator_combined",
            "simulator_external_evidence",
            "px4_airframe_transaction",
            "px4_reported_position_response_matrix",
            "px4_reported_position_response_campaign",
            "qgc_operator_visual_acceptance",
            "px4_mission_feedback",
            "pixhawk_covariance",
            "simulator_3d_control",
            "simulator_scene_stream_set",
            "field_evidence_bundle",
            "model_review",
        ):
            binding = document.get(binding_name)
            if isinstance(binding, Mapping) and binding.get("gate_passed") is not True:
                incomplete = True
        if incomplete and not args.allow_incomplete:
            print(
                f"release-gate manifest written with gate_passed=false: {destination}",
                file=sys.stderr,
            )
            return 2
        return 0
    except (ReleaseGateManifestError, OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


# Small compatibility aliases keep callers from having to distinguish the
# "build" and "create-and-write" vocabulary used by the repository scripts.
build_manifest = build_release_gate_manifest
generate_release_gate_manifest = create_release_gate_manifest


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "RELEASE_GATE_SCHEMA_VERSION",
    "DEFAULT_FIELD_LINK_MAX_AGE_S",
    "ReleaseGateManifestError",
    "STATUS_VALUES",
    "build_parser",
    "build_manifest",
    "build_release_gate_manifest",
    "create_release_gate_manifest",
    "default_mode_statuses",
    "generate_release_gate_manifest",
    "load_parity_hashes",
    "main",
    "sha256_file",
    "write_release_gate_manifest",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
