"""Cross-check external SIM runtime and live-AI evidence.

The Docker acceptance runners intentionally emit one rich JSON document per
vehicle.  This module is the small, fail-closed join point for those files:
it checks all three vehicle variants, requires six distinct runtime/AI source
files, binds the current release-parity file, and verifies that runtime and AI
evidence describe the same scenario identity, image digest, truth-free
boundary, and hardware-safety boundary.  It does not contact Docker, PX4,
Gazebo, QGC, or any aircraft endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autonomy.serialization import strict_json_loads

SCHEMA_VERSION = 1
RUNTIME_SCHEMA = "multidetect.simulator-docker-acceptance"
REQUIRED_VEHICLES = ("vtol", "fixed-wing", "multirotor")
PARITY_FIELDS = (
    "ui_contract_hash",
    "core_algorithm_hash",
    "task_contract_hash",
    "safety_policy_hash",
    "px4_mission_path_hash",
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MISSING = object()
_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-fA-F]{64}$")
_RELEASE_LABEL_RE = re.compile(
    r"v(?P<label>\d+\.\d+\.\d+(?:-pre\.[1-9]\d*)?)\b",
    re.IGNORECASE,
)


class SimulatorExternalEvidenceError(ValueError):
    """Raised when external SIM evidence cannot be joined safely."""


def _resolve_file(value: str | Path, *, name: str) -> Path:
    source = Path(value).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SimulatorExternalEvidenceError(f"{name} does not exist: {source}") from exc
    if not resolved.is_file():
        raise SimulatorExternalEvidenceError(f"{name} must be a regular file: {resolved}")
    return resolved


def _load_object(path: Path, *, name: str) -> dict[str, Any]:
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8-sig"), name=name)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SimulatorExternalEvidenceError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SimulatorExternalEvidenceError(f"{name} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lookup(value: object, path: str) -> object:
    current = value
    for component in path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            return _MISSING
        current = current[component]
    return current


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _normalise_evidence_paths(
    values: Mapping[str, str | Path],
    *,
    role: str,
) -> dict[str, str | Path]:
    normalized: dict[str, str | Path] = {}
    for raw_key, path in values.items():
        key = str(raw_key).strip().lower()
        if not key:
            raise SimulatorExternalEvidenceError(f"{role} evidence keys must be non-empty")
        if key in normalized:
            raise SimulatorExternalEvidenceError(f"duplicate {role} evidence key: {key!r}")
        normalized[key] = path
    return normalized


def _truth_free_passes(document: Mapping[str, Any]) -> bool:
    return (
        _lookup(document, "plan.truth_free") is True
        and _lookup(document, "safety.truth_free") is True
        and _lookup(document, "scenario.truth_loaded_by_runtime") is False
    )


def _hardware_boundary_passes(document: Mapping[str, Any]) -> bool:
    return (
        _lookup(document, "hardware.hardware_control_enabled") is False
        and _lookup(document, "safety.hardware_control_enabled") is False
        and _lookup(document, "safety.real_pixhawk_contacted") is False
        and _lookup(document, "hardware.status") == "not_claimed"
    )


def _vehicle_from_document(document: Mapping[str, Any]) -> str | None:
    candidates: list[str] = []
    for path in (
        "scenario.scenario_id",
        "scenario.vehicle_model",
        "scenario.airframe_profile_id",
        "plan.scenario_id",
        "plan.vehicle.model",
        "plan.vehicle.model_name",
    ):
        value = _lookup(document, path)
        if isinstance(value, str):
            candidates.append(value.casefold())
    text = " ".join(candidates)
    if "fixed" in text and "wing" in text:
        return "fixed-wing"
    if "vtol" in text:
        return "vtol"
    if "multirotor" in text or "multicopter" in text or "quadcopter" in text:
        return "multirotor"
    return None


def _truth_free(document: Mapping[str, Any], *, role: str, reasons: list[str]) -> None:
    for path in ("plan.truth_free", "safety.truth_free"):
        value = _lookup(document, path)
        if value is not True:
            _append_reason(reasons, f"{role}: {path} must be true")
    value = _lookup(document, "scenario.truth_loaded_by_runtime")
    if value is not False:
        _append_reason(reasons, f"{role}: scenario.truth_loaded_by_runtime must be false")


def _safety_boundary(document: Mapping[str, Any], *, role: str, reasons: list[str]) -> None:
    for path in (
        "hardware.hardware_control_enabled",
        "safety.hardware_control_enabled",
        "safety.real_pixhawk_contacted",
    ):
        value = _lookup(document, path)
        if value is not False:
            _append_reason(reasons, f"{role}: {path} must be false")
    hardware_status = _lookup(document, "hardware.status")
    if hardware_status != "not_claimed":
        _append_reason(reasons, f"{role}: hardware.status must remain 'not_claimed'")


def _validate_common(
    document: Mapping[str, Any],
    *,
    role: str,
    expected_vehicle: str,
    reasons: list[str],
) -> None:
    if document.get("schema") != RUNTIME_SCHEMA:
        _append_reason(reasons, f"{role}: schema must be {RUNTIME_SCHEMA!r}")
    if document.get("schema_version") != SCHEMA_VERSION:
        _append_reason(reasons, f"{role}: schema_version must be {SCHEMA_VERSION}")
    if _vehicle_from_document(document) != expected_vehicle:
        _append_reason(reasons, f"{role}: vehicle identity does not match {expected_vehicle!r}")
    for field in ("gate_passed", "preflight_gate_passed", "runtime_gate_passed"):
        if document.get(field) is not True:
            _append_reason(reasons, f"{role}: {field} must be true")
    if document.get("execution_requested") is not True:
        _append_reason(reasons, f"{role}: execution_requested must be true")
    if _lookup(document, "telemetry.qualified") is not True:
        _append_reason(reasons, f"{role}: telemetry.qualified must be true")
    if _lookup(document, "container_runtime.external_process_started") is not True:
        _append_reason(reasons, f"{role}: external_process_started must be true")
    cleanup = _lookup(document, "container_runtime.cleanup")
    if not isinstance(cleanup, Mapping):
        _append_reason(reasons, f"{role}: container_runtime.cleanup must be an object")
    else:
        if cleanup.get("attempted") is not True or cleanup.get("removed") is not True:
            _append_reason(reasons, f"{role}: container cleanup must be attempted and removed")
        errors = cleanup.get("errors")
        if not isinstance(errors, list) or errors:
            _append_reason(reasons, f"{role}: container cleanup errors must be empty")
    _truth_free(document, role=role, reasons=reasons)
    _safety_boundary(document, role=role, reasons=reasons)

    image = _lookup(document, "plan.image")
    container_image = _lookup(document, "container_runtime.image")
    if not isinstance(image, str) or _IMAGE_DIGEST_RE.fullmatch(image) is None:
        _append_reason(reasons, f"{role}: plan.image must be a digest-pinned image")
    if not isinstance(container_image, str) or _IMAGE_DIGEST_RE.fullmatch(container_image) is None:
        _append_reason(reasons, f"{role}: container_runtime.image must be a digest-pinned image")
    if image != container_image:
        _append_reason(reasons, f"{role}: plan.image and container_runtime.image differ")
    scenario_id = _lookup(document, "scenario.scenario_id")
    manifest_hash = _lookup(document, "scenario.scenario_manifest_hash")
    plan_manifest_hash = _lookup(document, "plan.scenario_manifest_hash")
    if not isinstance(scenario_id, str) or not scenario_id:
        _append_reason(reasons, f"{role}: scenario.scenario_id is missing")
    if not isinstance(manifest_hash, str) or not _SHA256_RE.fullmatch(manifest_hash):
        _append_reason(reasons, f"{role}: scenario.scenario_manifest_hash must be SHA-256")
    if manifest_hash != plan_manifest_hash:
        _append_reason(reasons, f"{role}: scenario and plan manifest hashes differ")
    source_manifest_hash = _lookup(document, "scenario.manifest_sha256")
    if not isinstance(source_manifest_hash, str) or not _SHA256_RE.fullmatch(source_manifest_hash):
        _append_reason(reasons, f"{role}: scenario.manifest_sha256 must be SHA-256")
    gps = _lookup(document, "telemetry.gps")
    if not isinstance(gps, Mapping):
        _append_reason(reasons, f"{role}: telemetry.gps must be an object")
    else:
        fix_type = gps.get("fix_type")
        satellites = gps.get("satellites_visible")
        if isinstance(fix_type, bool) or not isinstance(fix_type, int) or fix_type < 3:
            _append_reason(reasons, f"{role}: telemetry.gps.fix_type must be >= 3")
        if isinstance(satellites, bool) or not isinstance(satellites, int) or satellites < 6:
            _append_reason(reasons, f"{role}: telemetry.gps.satellites_visible must be >= 6")


def _validate_ai(document: Mapping[str, Any], *, role: str, reasons: list[str]) -> None:
    for field in ("ai_policy_gate_passed", "target_policy_gate_passed"):
        if document.get(field) is not True:
            _append_reason(reasons, f"{role}: {field} must be true")
    for field in ("ai_steps", "ai_policy_steps"):
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _append_reason(reasons, f"{role}: {field} must be a positive integer")
    if _lookup(document, "ai.armed_observed") is not True:
        _append_reason(reasons, f"{role}: ai.armed_observed must be true")
    target = document.get("target_exercise")
    if not isinstance(target, Mapping):
        _append_reason(reasons, f"{role}: target_exercise must be an object")
        return
    if target.get("accepted") is not True:
        _append_reason(reasons, f"{role}: target_exercise.accepted must be true")
    if target.get("truth_free") is not True:
        _append_reason(reasons, f"{role}: target_exercise.truth_free must be true")
    if target.get("visual_accuracy") != "not_claimed":
        _append_reason(reasons, f"{role}: target_exercise.visual_accuracy must be 'not_claimed'")
    decision = target.get("decision")
    if not isinstance(decision, Mapping) or decision.get("action") != "track":
        _append_reason(reasons, f"{role}: target decision action must be 'track'")


def _parity_record(path: Path, *, release_label: str | None, reasons: list[str]) -> dict[str, Any]:
    document = _load_object(path, name="parity manifest")
    parity = document.get("parity")
    if not isinstance(parity, Mapping):
        _append_reason(reasons, "parity manifest: parity must be an object")
        parity = {}
    hashes: dict[str, str] = {}
    for field in PARITY_FIELDS:
        value = parity.get(field)
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            _append_reason(reasons, f"parity manifest: {field} must be a SHA-256 string")
        else:
            hashes[field] = value.lower()
    manifest_release_label = document.get("release_label")
    if release_label is not None and manifest_release_label != release_label:
        _append_reason(
            reasons,
            "parity manifest: release_label does not match the external evidence",
        )
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "release_label": manifest_release_label,
        "hashes": hashes,
    }


def _release_label(document: Mapping[str, Any]) -> str | None:
    value = document.get("release_status")
    if not isinstance(value, str):
        return None
    match = _RELEASE_LABEL_RE.search(value)
    return match.group("label") if match else None


def build_simulator_external_evidence(
    runtime_evidence: Mapping[str, str | Path],
    ai_evidence: Mapping[str, str | Path],
    *,
    parity_path: str | Path,
) -> dict[str, Any]:
    """Build a hash-bound three-vehicle SIM runtime/AI verification record."""

    runtime_paths = _normalise_evidence_paths(runtime_evidence, role="runtime")
    ai_paths = _normalise_evidence_paths(ai_evidence, role="AI")
    expected = set(REQUIRED_VEHICLES)
    if set(runtime_paths) != expected:
        raise SimulatorExternalEvidenceError(
            f"runtime evidence keys must be exactly {REQUIRED_VEHICLES!r}"
        )
    if set(ai_paths) != expected:
        raise SimulatorExternalEvidenceError(
            f"AI evidence keys must be exactly {REQUIRED_VEHICLES!r}"
        )

    reasons: list[str] = []
    records: dict[str, Any] = {}
    loaded_pairs: dict[str, dict[str, dict[str, Any]]] = {}
    source_path_owners: dict[Path, str] = {}
    release_labels: set[str] = set()
    image_digests: set[str] = set()
    for vehicle in REQUIRED_VEHICLES:
        runtime_path = _resolve_file(runtime_paths[vehicle], name=f"{vehicle} runtime evidence")
        ai_path = _resolve_file(ai_paths[vehicle], name=f"{vehicle} AI evidence")
        if runtime_path == ai_path:
            _append_reason(
                reasons,
                f"{vehicle}: runtime and AI evidence must be distinct files",
            )
        for role, source_path in (("runtime", runtime_path), ("AI", ai_path)):
            owner = f"{vehicle} {role}"
            previous_owner = source_path_owners.get(source_path)
            if previous_owner is not None:
                _append_reason(
                    reasons,
                    f"{owner}: source file is reused by {previous_owner}; six "
                    "runtime/AI evidence files must be distinct",
                )
            else:
                source_path_owners[source_path] = owner
        runtime = _load_object(runtime_path, name=f"{vehicle} runtime evidence")
        ai = _load_object(ai_path, name=f"{vehicle} AI evidence")
        loaded_pairs[vehicle] = {"runtime": runtime, "ai": ai}
        runtime_role = f"{vehicle} runtime"
        ai_role = f"{vehicle} AI"
        _validate_common(runtime, role=runtime_role, expected_vehicle=vehicle, reasons=reasons)
        _validate_common(ai, role=ai_role, expected_vehicle=vehicle, reasons=reasons)
        _validate_ai(ai, role=ai_role, reasons=reasons)
        runtime_label = _release_label(runtime)
        ai_label = _release_label(ai)
        if runtime_label is None:
            _append_reason(
                reasons,
                f"{runtime_role}: release_status must contain a valid release label",
            )
        else:
            release_labels.add(runtime_label)
        if ai_label is None:
            _append_reason(reasons, f"{ai_role}: release_status must contain a valid release label")
        else:
            release_labels.add(ai_label)
        if runtime_label != ai_label:
            _append_reason(reasons, f"{vehicle}: runtime and AI release labels differ")
        runtime_image = _lookup(runtime, "plan.image")
        ai_image = _lookup(ai, "plan.image")
        if isinstance(runtime_image, str) and _IMAGE_DIGEST_RE.fullmatch(runtime_image):
            image_digests.add(runtime_image)
        if isinstance(ai_image, str) and _IMAGE_DIGEST_RE.fullmatch(ai_image):
            image_digests.add(ai_image)
        for field_path in (
            "scenario.scenario_id",
            "scenario.scenario_manifest_hash",
            "scenario.manifest_sha256",
        ):
            if _lookup(runtime, field_path) != _lookup(ai, field_path):
                _append_reason(reasons, f"{vehicle}: runtime and AI {field_path} differ")
        records[vehicle] = {
            "runtime": {
                "path": str(runtime_path),
                "sha256": _sha256(runtime_path),
                "bytes": runtime_path.stat().st_size,
                "scenario_id": _lookup(runtime, "scenario.scenario_id"),
                "scenario_manifest_sha256": _lookup(runtime, "scenario.scenario_manifest_hash"),
                "image": runtime_image,
                "gate_passed": runtime.get("gate_passed") is True,
            },
            "ai": {
                "path": str(ai_path),
                "sha256": _sha256(ai_path),
                "bytes": ai_path.stat().st_size,
                "scenario_id": _lookup(ai, "scenario.scenario_id"),
                "scenario_manifest_sha256": _lookup(ai, "scenario.scenario_manifest_hash"),
                "image": ai_image,
                "gate_passed": ai.get("gate_passed") is True,
                "ai_policy_gate_passed": ai.get("ai_policy_gate_passed") is True,
                "target_policy_gate_passed": ai.get("target_policy_gate_passed") is True,
                "target_exercise_accepted": _lookup(ai, "target_exercise.accepted") is True,
            },
        }

    if len(release_labels) != 1:
        _append_reason(reasons, "external evidence must use one release label")
    release_label = next(iter(release_labels), None)
    if len(image_digests) != 1:
        _append_reason(reasons, "all external evidence must use one digest-pinned image")
    image = next(iter(image_digests), None)
    parity = _parity_record(
        _resolve_file(parity_path, name="parity manifest"),
        release_label=release_label,
        reasons=reasons,
    )
    if not release_label:
        _append_reason(
            reasons,
            "external evidence release_status must contain a valid release label",
        )

    gate_passed = not reasons
    all_documents = [
        pair[role]
        for vehicle in REQUIRED_VEHICLES
        for pair in (loaded_pairs[vehicle],)
        for role in ("runtime", "ai")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "simulator_external_runtime_ai_evidence_verified",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "release_label": release_label,
        "vehicles": records,
        "shared": {
            "image": image,
            "image_digest_consistent": len(image_digests) == 1,
            "release_label_consistent": len(release_labels) == 1,
            "truth_free_perception": all(
                _truth_free_passes(document) for document in all_documents
            ),
            "hardware_control_disabled": all(
                _hardware_boundary_passes(document) for document in all_documents
            ),
            "visual_accuracy": "not_claimed",
            "real_hardware": "not_claimed",
        },
        "parity": parity,
        "claims": {
            "external_runtime_gate": "passed" if gate_passed else "failed",
            "ai_target_policy_gate": "passed" if gate_passed else "failed",
            "hardware_acceptance": "not_claimed",
            "operator_visual_acceptance": "not_claimed",
            "truth_free": all(_truth_free_passes(document) for document in all_documents),
        },
        "gate_passed": gate_passed,
        "reasons": reasons,
    }


def revalidate_simulator_external_evidence(
    evidence_path: str | Path,
    *,
    expected_release_label: str | None = None,
) -> dict[str, Any]:
    """Recompute and compare an existing external-SIM join artifact.

    The normal join command reads six source files and writes a summary.  This
    helper is the second, independent evidence seam: it reads the stored source
    paths from an existing summary, rebuilds the join from those files, and
    compares every gate-bearing field (including source hashes, byte counts,
    parity and claims).  A changed source file, moved path, or edited summary
    therefore fails closed instead of being accepted because its old
    ``gate_passed`` value still says ``true``.
    """

    report_path = _resolve_file(evidence_path, name="external evidence join")
    report = _load_object(report_path, name="external evidence join")
    reasons: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        _append_reason(reasons, f"join: schema_version must be {SCHEMA_VERSION}")
    if report.get("event") != "simulator_external_runtime_ai_evidence_verified":
        _append_reason(reasons, "join: event is not a verified external runtime/AI join")
    if expected_release_label is not None and report.get("release_label") != expected_release_label:
        _append_reason(reasons, "join: release_label does not match the expected release")

    vehicle_records = report.get("vehicles")
    runtime_paths: dict[str, Path] = {}
    ai_paths: dict[str, Path] = {}
    resolved_records: dict[str, dict[str, dict[str, Any]]] = {}
    if not isinstance(vehicle_records, Mapping):
        _append_reason(reasons, "join: vehicles must be an object")
    else:
        if set(vehicle_records) != set(REQUIRED_VEHICLES):
            _append_reason(reasons, "join: vehicles must contain exactly the required airframes")
        for vehicle in REQUIRED_VEHICLES:
            pair = vehicle_records.get(vehicle)
            if not isinstance(pair, Mapping):
                _append_reason(reasons, f"join: {vehicle} record must be an object")
                continue
            resolved_records[vehicle] = {}
            for role, destination in (("runtime", runtime_paths), ("ai", ai_paths)):
                record = pair.get(role)
                if not isinstance(record, Mapping):
                    _append_reason(reasons, f"join: {vehicle} {role} record must be an object")
                    continue
                source_path = record.get("path")
                if not isinstance(source_path, str) or not source_path.strip():
                    _append_reason(reasons, f"join: {vehicle} {role} source path is missing")
                    continue
                try:
                    resolved = _resolve_file(source_path, name=f"{vehicle} {role} source")
                except SimulatorExternalEvidenceError as exc:
                    _append_reason(reasons, str(exc))
                    continue
                destination[vehicle] = resolved
                normalized_record = dict(record)
                normalized_record["path"] = str(resolved)
                resolved_records[vehicle][role] = normalized_record

    parity = report.get("parity")
    parity_path: Path | None = None
    normalized_parity: dict[str, Any] | None = None
    if not isinstance(parity, Mapping):
        _append_reason(reasons, "join: parity must be an object")
    else:
        source_path = parity.get("path")
        if not isinstance(source_path, str) or not source_path.strip():
            _append_reason(reasons, "join: parity source path is missing")
        else:
            try:
                parity_path = _resolve_file(source_path, name="join parity source")
            except SimulatorExternalEvidenceError as exc:
                _append_reason(reasons, str(exc))
            else:
                normalized_parity = dict(parity)
                normalized_parity["path"] = str(parity_path)

    recomputed: dict[str, Any] | None = None
    if not reasons:
        try:
            recomputed = build_simulator_external_evidence(
                runtime_paths,
                ai_paths,
                parity_path=parity_path,  # type: ignore[arg-type]
            )
        except SimulatorExternalEvidenceError as exc:
            _append_reason(reasons, f"join: source recomputation failed: {exc}")

    if not reasons and recomputed is not None:
        actual_view = {
            "schema_version": report.get("schema_version"),
            "event": report.get("event"),
            "release_label": report.get("release_label"),
            "vehicles": resolved_records,
            "shared": report.get("shared"),
            "parity": normalized_parity,
            "claims": report.get("claims"),
            "gate_passed": report.get("gate_passed"),
            "reasons": report.get("reasons"),
        }
        expected_view = {
            "schema_version": recomputed.get("schema_version"),
            "event": recomputed.get("event"),
            "release_label": recomputed.get("release_label"),
            "vehicles": recomputed.get("vehicles"),
            "shared": recomputed.get("shared"),
            "parity": recomputed.get("parity"),
            "claims": recomputed.get("claims"),
            "gate_passed": recomputed.get("gate_passed"),
            "reasons": recomputed.get("reasons"),
        }
        if actual_view != expected_view:
            _append_reason(reasons, "join: stored summary does not match source recomputation")

    gate_passed = not reasons and report.get("gate_passed") is True
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "simulator_external_runtime_ai_evidence_revalidated",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
        "report_bytes": report_path.stat().st_size,
        "source_recomputed": recomputed is not None,
        "release_label": report.get("release_label"),
        "expected_release_label": expected_release_label,
        "gate_passed": gate_passed,
        "reasons": reasons,
    }


def write_simulator_external_evidence(
    destination: str | Path,
    document: Mapping[str, Any],
    *,
    overwrite: bool = True,
) -> Path:
    """Write verification evidence atomically as UTF-8 JSON."""

    output = Path(destination).expanduser().resolve()
    if output.exists() and not overwrite:
        raise SimulatorExternalEvidenceError(f"output already exists: {output}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Cross-check VTOL/fixed-wing/multirotor Docker runtime and live-AI "
            "acceptance evidence without contacting external systems."
        )
    )
    for vehicle in REQUIRED_VEHICLES:
        flag = vehicle.replace("-", "_")
        parser.add_argument(
            f"--{vehicle}-runtime",
            dest=f"{flag}_runtime",
            type=Path,
        )
        parser.add_argument(f"--{vehicle}-ai", dest=f"{flag}_ai", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--join",
        "--verify-join",
        dest="join",
        type=Path,
        help="recompute and verify an existing join artifact instead of building one",
    )
    parser.add_argument("--parity-json", "--parity-file", type=Path)
    parser.add_argument("--expected-release-label", type=str)
    parser.add_argument("--out", "--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.join is not None:
        try:
            document = revalidate_simulator_external_evidence(
                args.join,
                expected_release_label=args.expected_release_label,
            )
            write_simulator_external_evidence(args.out, document)
        except SimulatorExternalEvidenceError as exc:
            print(f"simulator external evidence: {exc}", flush=True)
            return 2
        print(json.dumps(document, ensure_ascii=False, indent=2))
        return 0 if document["gate_passed"] else 1

    missing = [
        flag
        for flag in (
            "vtol_runtime",
            "vtol_ai",
            "fixed_wing_runtime",
            "fixed_wing_ai",
            "multirotor_runtime",
            "multirotor_ai",
            "parity_json",
        )
        if getattr(args, flag) is None
    ]
    if missing:
        parser.error(
            "source join mode requires "
            + ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        )
    runtime = {
        "vtol": args.vtol_runtime,
        "fixed-wing": args.fixed_wing_runtime,
        "multirotor": args.multirotor_runtime,
    }
    ai = {
        "vtol": args.vtol_ai,
        "fixed-wing": args.fixed_wing_ai,
        "multirotor": args.multirotor_ai,
    }
    try:
        document = build_simulator_external_evidence(runtime, ai, parity_path=args.parity_json)
        write_simulator_external_evidence(args.out, document)
    except SimulatorExternalEvidenceError as exc:
        print(f"simulator external evidence: {exc}", flush=True)
        return 2
    print(json.dumps(document, ensure_ascii=False, indent=2))
    return 0 if document["gate_passed"] else 1


__all__ = [
    "PARITY_FIELDS",
    "REQUIRED_VEHICLES",
    "RUNTIME_SCHEMA",
    "SCHEMA_VERSION",
    "SimulatorExternalEvidenceError",
    "build_parser",
    "build_simulator_external_evidence",
    "main",
    "revalidate_simulator_external_evidence",
    "write_simulator_external_evidence",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the console/module CLI
    raise SystemExit(main())
