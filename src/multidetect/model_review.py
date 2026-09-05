"""Hash-bound review envelope for a fire/smoke ``N x 6`` model candidate.

Model manifests describe the artifact contract, while replay reports describe
the data used to measure it.  This module joins those two records without
modifying either input.  The resulting envelope is deliberately conservative:
it can say that a candidate is *eligible for governance approval*, but it never
sets ``production_approved`` and synthetic HIL artifacts can never become
eligible.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model_manifest import ModelManifestError, VerifiedModelArtifact, verify_model_manifest
from .replay_set import REPLAY_SET_POSITIVE_STRATA

MODEL_REVIEW_SCHEMA_VERSION = 1
MODEL_REVIEW_REQUIRED_REVIEWS = (
    "provenance",
    "dataset_rights",
    "security",
    "quality",
)
MODEL_REVIEW_STATUSES = ("pending", "passed", "failed")


class ModelReviewError(ValueError):
    """Raised when a model-review request cannot be formed safely."""


@dataclass(frozen=True, slots=True)
class ModelReviewBundle:
    """The immutable result of preparing a model-review evidence envelope."""

    output_path: Path
    document: dict[str, Any]

    @property
    def production_approval_eligible(self) -> bool:
        return self.document["production_approval_eligible"] is True


def prepare_model_review_bundle(
    manifest_path: str | Path,
    model_path: str | Path,
    output_path: str | Path,
    *,
    model_check_report_path: str | Path | None = None,
    accuracy_report_path: str | Path | None = None,
    review_decisions: Mapping[str, str] | None = None,
    reviewer: str | None = None,
    reviewed_at_utc: str | None = None,
    overwrite: bool = False,
) -> ModelReviewBundle:
    """Create a read-only, hash-bound model review bundle.

    ``review_decisions`` records independent governance decisions but does not
    mutate the candidate manifest.  Eligibility requires all four decisions,
    a passing ``model-check`` report, a passing deployment-domain accuracy
    report bound to the exact artifact hash, and a non-synthetic candidate.
    Missing or malformed evidence is represented in ``failure_reasons`` so the
    command is useful while collecting data; malformed model manifests remain
    hard contract errors.
    """

    manifest = _resolve_input(manifest_path, "model manifest")
    artifact = _resolve_input(model_path, "model artifact")
    output = Path(output_path).expanduser()
    if output.exists() and output.is_symlink():
        raise ModelReviewError("model review output path must not be a symbolic link")
    if not isinstance(overwrite, bool):
        raise ModelReviewError("overwrite must be a boolean")
    if output.exists() and not overwrite:
        raise ModelReviewError(f"model review output already exists: {output}")

    try:
        verified = verify_model_manifest(
            manifest,
            artifact,
            expected_class_names=("fire", "smoke"),
            expected_model_role="fire_candidate",
            expected_output_format="N_x_6",
            expected_native_output_format="post_nms_N_x_6",
        )
    except (ModelManifestError, OSError, ValueError) as exc:
        raise ModelReviewError(f"candidate model manifest verification failed: {exc}") from exc

    manifest_payload = manifest.read_bytes()
    artifact_sha256 = _sha256_file(artifact)
    decisions = _normalize_review_decisions(review_decisions)
    passed_reviews = [
        name for name, status in decisions.items() if status == "passed"
    ]
    normalized_reviewer = _normalize_reviewer(reviewer, required=bool(passed_reviews))
    normalized_reviewed_at = _normalize_review_timestamp(
        reviewed_at_utc,
        required=bool(passed_reviews),
    )
    if normalized_reviewed_at is None and passed_reviews:
        normalized_reviewed_at = _utc_now()

    model_check = _inspect_model_check_report(
        model_check_report_path,
        artifact_sha256=artifact_sha256,
        verified=verified,
    )
    accuracy = _inspect_accuracy_report(
        accuracy_report_path,
        artifact_sha256=artifact_sha256,
        model_id=verified.model_id,
        model_version=verified.model_version,
    )

    failure_reasons: list[str] = []
    if verified.status != "quarantined":
        failure_reasons.append(
            f"candidate manifest status {verified.status!r} is not quarantined"
        )
    if verified.synthetic_hil_only:
        failure_reasons.append("synthetic HIL artifacts can never be production approved")
    if verified.production_approved:
        failure_reasons.append("input manifest already claims production approval")
    for name, status in decisions.items():
        if status == "pending":
            failure_reasons.append(f"{name} review is pending")
        elif status == "failed":
            failure_reasons.append(f"{name} review failed")
    failure_reasons.extend(
        f"model-check evidence: {reason}" for reason in model_check["failure_reasons"]
    )
    failure_reasons.extend(
        f"accuracy evidence: {reason}" for reason in accuracy["failure_reasons"]
    )
    failure_reasons = _unique(failure_reasons)
    eligible = not failure_reasons
    review_status = (
        "rejected"
        if any(status == "failed" for status in decisions.values())
        else "reviewed"
        if all(status == "passed" for status in decisions.values())
        else "pending"
    )
    document: dict[str, Any] = {
        "event": "model_review_bundle_prepared",
        "schema_version": MODEL_REVIEW_SCHEMA_VERSION,
        "model": {
            "model_id": verified.model_id,
            "model_version": verified.model_version,
            "model_role": verified.model_role,
            "status": verified.status,
            "synthetic_hil_only": verified.synthetic_hil_only,
            "manifest_path": str(manifest),
            "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
            "artifact_path": str(artifact),
            "artifact_sha256": artifact_sha256,
            "output_format": verified.output_format,
            "output_coordinates": verified.output_coordinates,
            "class_names": list(verified.class_names),
            "manifest_verified": True,
        },
        "reviews": {
            name: {
                "status": decisions[name],
                "reviewer": normalized_reviewer if decisions[name] == "passed" else None,
                "reviewed_at_utc": (
                    normalized_reviewed_at if decisions[name] == "passed" else None
                ),
            }
            for name in MODEL_REVIEW_REQUIRED_REVIEWS
        },
        "evidence": {
            "model_check": model_check,
            "deployment_accuracy": accuracy,
        },
        "review_status": review_status,
        "production_approval_eligible": eligible,
        "production_approved": False,
        "flight_control_enabled": False,
        "physical_release_enabled": False,
        "failure_reasons": failure_reasons,
        "next_actions": _next_actions(
            decisions,
            model_check=model_check,
            accuracy=accuracy,
            synthetic_hil_only=verified.synthetic_hil_only,
        ),
    }
    _atomic_write_json(output, document, overwrite=overwrite)
    return ModelReviewBundle(output.resolve(), document)


def verify_model_review_bundle(
    bundle_path: str | Path,
    *,
    require_eligible: bool = False,
) -> dict[str, Any]:
    """Re-open and verify a hash-bound model-review bundle.

    Verification deliberately re-runs the candidate-manifest, model-check and
    deployment-accuracy checks instead of trusting the summary booleans in the
    bundle.  This makes the bundle safe to reference from the release-gate
    manifest: changing a source report or its summary cannot turn a pending
    candidate into an eligible one without regenerating a matching envelope.
    """

    result: dict[str, Any] = {
        "present": False,
        "path": None,
        "sha256": None,
        "passed": False,
        "production_approval_eligible": False,
        "reasons": [],
        "document": None,
    }
    reasons: list[str] = result["reasons"]
    try:
        bundle = _resolve_input(bundle_path, "model review bundle")
        payload = bundle.read_bytes()
        document = _strict_json_loads(payload, "model review bundle")
    except (OSError, ModelReviewError) as exc:
        reasons.append(str(exc))
        return result
    result.update(
        {
            "present": True,
            "path": str(bundle),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "document": document,
        }
    )
    if not isinstance(document, Mapping):
        reasons.append("model review bundle root must be a JSON object")
        return result

    expected_top_level = {
        "event",
        "schema_version",
        "model",
        "reviews",
        "evidence",
        "review_status",
        "production_approval_eligible",
        "production_approved",
        "flight_control_enabled",
        "physical_release_enabled",
        "failure_reasons",
        "next_actions",
    }
    unknown = sorted(set(document).difference(expected_top_level))
    if unknown:
        reasons.append(f"unknown model review field(s): {unknown!r}")
    if document.get("event") != "model_review_bundle_prepared":
        reasons.append("event must be model_review_bundle_prepared")
    if document.get("schema_version") != MODEL_REVIEW_SCHEMA_VERSION:
        reasons.append("schema_version must be 1")

    model = document.get("model")
    if not isinstance(model, Mapping):
        reasons.append("model review model must be an object")
        return _finish_model_review_verification(result, reasons, require_eligible)
    required_model_fields = (
        "model_id",
        "model_version",
        "model_role",
        "status",
        "synthetic_hil_only",
        "manifest_path",
        "manifest_sha256",
        "artifact_path",
        "artifact_sha256",
        "output_format",
        "output_coordinates",
        "class_names",
        "manifest_verified",
    )
    for field in required_model_fields:
        if field not in model:
            reasons.append(f"model.{field} is missing")

    manifest: Path | None = None
    artifact: Path | None = None
    try:
        manifest = _resolve_bound_path(
            model.get("manifest_path"), bundle.parent, "model review manifest"
        )
    except ModelReviewError as exc:
        reasons.append(str(exc))
    try:
        artifact = _resolve_bound_path(
            model.get("artifact_path"), bundle.parent, "model review artifact"
        )
    except ModelReviewError as exc:
        reasons.append(str(exc))

    verified: VerifiedModelArtifact | None = None
    artifact_sha256: str | None = None
    if manifest is not None and artifact is not None:
        try:
            verified = verify_model_manifest(
                manifest,
                artifact,
                expected_class_names=("fire", "smoke"),
                expected_model_role="fire_candidate",
                expected_output_format="N_x_6",
                expected_native_output_format="post_nms_N_x_6",
            )
            artifact_sha256 = verified.artifact_sha256
        except (ModelManifestError, OSError, ValueError) as exc:
            reasons.append(f"candidate model manifest verification failed: {exc}")
    if verified is not None and manifest is not None:
        expected_model = {
            "model_id": verified.model_id,
            "model_version": verified.model_version,
            "model_role": verified.model_role,
            "status": verified.status,
            "synthetic_hil_only": verified.synthetic_hil_only,
            "output_format": verified.output_format,
            "output_coordinates": verified.output_coordinates,
            "class_names": list(verified.class_names),
            "manifest_verified": True,
        }
        for field, expected in expected_model.items():
            if model.get(field) != expected:
                reasons.append(f"model.{field} does not match the verified manifest")
        actual_manifest_sha256 = _sha256_file(manifest)
        if not _same_sha256(model.get("manifest_sha256"), actual_manifest_sha256):
            reasons.append("model.manifest_sha256 does not match the manifest")
        if not _same_sha256(model.get("artifact_sha256"), verified.artifact_sha256):
            reasons.append("model.artifact_sha256 does not match the artifact")
        # A synthetic HIL manifest is an explicit, closed test artifact.  It
        # is structurally valid input for a pending review envelope, even
        # though ``prepare_model_review_bundle`` records it as ineligible and
        # production approval remains impossible.  Accept that status here so
        # existing synthetic diagnostics can still be referenced by the
        # release-gate manifest; ``require_eligible=True`` below keeps the
        # final release decision fail-closed.
        if verified.status != "quarantined" and not (
            verified.status == "synthetic_hil" and verified.synthetic_hil_only
        ):
            reasons.append("model candidate status must remain quarantined")
        if verified.production_approved:
            reasons.append("model candidate manifest claims production approval")
    elif artifact is not None:
        artifact_sha256 = _sha256_file(artifact)

    decisions = _extract_review_decisions(document.get("reviews"), reasons)
    evidence = document.get("evidence")
    if not isinstance(evidence, Mapping):
        reasons.append("evidence must be an object")
        evidence = {}
    model_check_entry = evidence.get("model_check")
    accuracy_entry = evidence.get("deployment_accuracy")
    if artifact_sha256 is None or verified is None:
        model_check = _empty_evidence_result(model_check_entry)
        accuracy = _empty_evidence_result(accuracy_entry)
        if artifact_sha256 is None:
            model_check["failure_reasons"].append("candidate artifact could not be verified")
            accuracy["failure_reasons"].append("candidate artifact could not be verified")
    else:
        model_check = _inspect_model_check_report(
            _evidence_path_value(model_check_entry),
            artifact_sha256=artifact_sha256,
            verified=verified,
        )
        accuracy = _inspect_accuracy_report(
            _evidence_path_value(accuracy_entry),
            artifact_sha256=artifact_sha256,
            model_id=verified.model_id,
            model_version=verified.model_version,
        )
    _compare_evidence_result(model_check_entry, model_check, "model_check", reasons)
    _compare_evidence_result(accuracy_entry, accuracy, "deployment_accuracy", reasons)

    failure_reasons: list[str] = []
    if verified is None:
        failure_reasons.append("candidate model manifest could not be verified")
    elif verified.status != "quarantined":
        failure_reasons.append(f"candidate manifest status {verified.status!r} is not quarantined")
    if verified is not None and verified.synthetic_hil_only:
        failure_reasons.append("synthetic HIL artifacts can never be production approved")
    if verified is not None and verified.production_approved:
        failure_reasons.append("input manifest already claims production approval")
    for name, status in decisions.items():
        if status == "pending":
            failure_reasons.append(f"{name} review is pending")
        elif status == "failed":
            failure_reasons.append(f"{name} review failed")
    failure_reasons.extend(
        f"model-check evidence: {reason}" for reason in model_check["failure_reasons"]
    )
    failure_reasons.extend(
        f"accuracy evidence: {reason}" for reason in accuracy["failure_reasons"]
    )
    failure_reasons = _unique(failure_reasons)
    expected_eligible = not failure_reasons
    expected_review_status = (
        "rejected"
        if any(status == "failed" for status in decisions.values())
        else "reviewed"
        if all(status == "passed" for status in decisions.values())
        else "pending"
    )
    if document.get("review_status") != expected_review_status:
        reasons.append("review_status does not match the recomputed review state")
    if document.get("production_approval_eligible") is not expected_eligible:
        reasons.append("production_approval_eligible does not match recomputed evidence")
    result["production_approval_eligible"] = expected_eligible
    for field in ("production_approved", "flight_control_enabled", "physical_release_enabled"):
        if document.get(field) is not False:
            reasons.append(f"{field} must remain false in a model review bundle")
    if document.get("failure_reasons") != failure_reasons:
        reasons.append("failure_reasons do not match recomputed evidence")
    expected_actions = _next_actions(
        decisions,
        model_check=model_check,
        accuracy=accuracy,
        synthetic_hil_only=verified.synthetic_hil_only if verified is not None else False,
    )
    if document.get("next_actions") != expected_actions:
        reasons.append("next_actions do not match recomputed evidence")
    if require_eligible and not expected_eligible:
        reasons.append("model review bundle is not production-approval eligible")
    return _finish_model_review_verification(result, reasons, require_eligible)


def _finish_model_review_verification(
    result: dict[str, Any],
    reasons: list[str],
    require_eligible: bool,
) -> dict[str, Any]:
    if require_eligible and not result["production_approval_eligible"]:
        marker = "model review bundle is not production-approval eligible"
        if marker not in reasons:
            reasons.append(marker)
    result["passed"] = not reasons
    return result


def _resolve_bound_path(value: object, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ModelReviewError(f"{label} path is missing or empty")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return _resolve_input(candidate, label)


def _same_sha256(left: object, right: str) -> bool:
    return isinstance(left, str) and _is_sha256(left) and left.lower() == right.lower()


def _extract_review_decisions(
    value: object,
    reasons: list[str],
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        reasons.append("reviews must be an object")
        return {name: "pending" for name in MODEL_REVIEW_REQUIRED_REVIEWS}
    unknown = sorted(set(value).difference(MODEL_REVIEW_REQUIRED_REVIEWS))
    if unknown:
        reasons.append(f"unknown model review decision(s): {unknown!r}")
    decisions: dict[str, str] = {}
    for name in MODEL_REVIEW_REQUIRED_REVIEWS:
        item = value.get(name)
        if not isinstance(item, Mapping):
            reasons.append(f"reviews.{name} must be an object")
            decisions[name] = "pending"
            continue
        status = item.get("status")
        if not isinstance(status, str) or status not in MODEL_REVIEW_STATUSES:
            reasons.append(f"reviews.{name}.status is invalid")
            status = "pending"
        decisions[name] = status
        reviewer = item.get("reviewer")
        reviewed_at = item.get("reviewed_at_utc")
        if status == "passed":
            if not isinstance(reviewer, str) or not reviewer.strip():
                reasons.append(f"reviews.{name}.reviewer is required for passed review")
            if reviewed_at is None:
                reasons.append(f"reviews.{name}.reviewed_at_utc is required for passed review")
            else:
                try:
                    _normalize_review_timestamp(reviewed_at, required=True)
                except ModelReviewError as exc:
                    reasons.append(f"reviews.{name}: {exc}")
        elif reviewer is not None or reviewed_at is not None:
            reasons.append(f"reviews.{name} metadata must be null unless status is passed")
    return decisions


def _evidence_path_value(value: object) -> str | Path | None:
    if not isinstance(value, Mapping):
        return None
    path = value.get("path")
    return path if isinstance(path, str) and path.strip() else None


def _empty_evidence_result(value: object) -> dict[str, Any]:
    present = isinstance(value, Mapping) and value.get("present") is True
    return {
        "present": present,
        "path": None,
        "sha256": None,
        "bound_to_artifact": False,
        "passed": False,
        "failure_reasons": ["candidate artifact could not be verified"],
    }


def _compare_evidence_result(
    declared: object,
    recomputed: Mapping[str, Any],
    label: str,
    reasons: list[str],
) -> None:
    if not isinstance(declared, Mapping):
        reasons.append(f"evidence.{label} must be an object")
        return
    for field in ("present", "path", "sha256", "bound_to_artifact", "passed", "failure_reasons"):
        if declared.get(field) != recomputed.get(field):
            reasons.append(f"evidence.{label}.{field} does not match recomputed evidence")


def _inspect_model_check_report(
    report_path: str | Path | None,
    *,
    artifact_sha256: str,
    verified: VerifiedModelArtifact,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": report_path is not None,
        "path": None,
        "sha256": None,
        "bound_to_artifact": False,
        "passed": False,
        "failure_reasons": [],
    }
    reasons: list[str] = result["failure_reasons"]
    document = _load_evidence_document(report_path, "model-check", result, reasons)
    if document is None:
        return result
    if document.get("event") != "onnx_model_validated":
        reasons.append("event must be onnx_model_validated")
    if document.get("post_nms_output_contract") != "Nx6":
        reasons.append("post_nms_output_contract must be Nx6")
    if document.get("manifest_validated") is not True:
        reasons.append("manifest_validated is not true")
    if document.get("manifest_status") != verified.status:
        reasons.append("manifest_status does not match the candidate manifest")
    if document.get("model_role") != verified.model_role:
        reasons.append("model_role does not match fire_candidate")
    if document.get("class_names") != list(verified.class_names):
        reasons.append("class_names do not match the fire/smoke manifest")
    if document.get("production_approved") is not False:
        reasons.append("model-check evidence must not claim production approval")
    if document.get("synthetic_hil_only") is not False:
        reasons.append("model-check evidence is synthetic HIL or missing its marker")
    if document.get("hardware_control_enabled") is not False:
        reasons.append("model-check evidence exposed hardware control")
    reported_sha = document.get("model_sha256")
    result["bound_to_artifact"] = (
        isinstance(reported_sha, str) and reported_sha.lower() == artifact_sha256
    )
    if not result["bound_to_artifact"]:
        reasons.append("model-check model_sha256 does not match the artifact")
    result["passed"] = not reasons
    return result


def _inspect_accuracy_report(
    report_path: str | Path | None,
    *,
    artifact_sha256: str,
    model_id: str,
    model_version: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": report_path is not None,
        "path": None,
        "sha256": None,
        "bound_to_artifact": False,
        "passed": False,
        "failure_reasons": [],
    }
    reasons: list[str] = result["failure_reasons"]
    document = _load_evidence_document(report_path, "deployment accuracy", result, reasons)
    if document is None:
        return result
    if document.get("event") != "replay_set_accuracy_evaluation_completed":
        reasons.append("event must be replay_set_accuracy_evaluation_completed")
    if document.get("schema_version") != 1:
        reasons.append("schema_version must be 1")
    for field in (
        "deployment_domain_evidence_complete",
        "predictions_complete",
        "metrics_evaluated",
        "metrics_gate_passed",
        "positive_strata_coverage_passed",
        "reviewed",
        "accuracy_validated",
        "gate_passed",
    ):
        if document.get(field) is not True:
            reasons.append(f"{field} is not true")
    if document.get("failure_reasons") != []:
        reasons.append("accuracy report contains failure_reasons")
    if not _accuracy_metrics_are_defined(document.get("metrics")):
        reasons.append("accuracy report aggregate precision/recall are undefined")
    if document.get("positive_strata") != list(REPLAY_SET_POSITIVE_STRATA):
        reasons.append("accuracy report positive strata are incomplete")
    coverage = document.get("positive_stratum_coverage")
    if not _positive_strata_coverage_is_complete(coverage):
        reasons.append("accuracy report positive-stratum coverage is incomplete")
    thresholds = document.get("thresholds")
    if not isinstance(thresholds, Mapping) or any(
        field not in thresholds
        for field in (
            "iou_threshold",
            "confidence_threshold",
            "minimum_precision",
            "minimum_recall",
            "maximum_false_alarm_frame_rate",
            "maximum_missed_detection_frame_rate",
            "maximum_latency_p95_ms",
        )
    ):
        reasons.append("accuracy report thresholds are incomplete")
    if not _is_sha256(document.get("manifest_sha256")):
        reasons.append("accuracy report manifest_sha256 is missing or invalid")
    if not _is_sha256(document.get("canonical_sha256")):
        reasons.append("accuracy report canonical_sha256 is missing or invalid")
    reviewer = document.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        reasons.append("accuracy report reviewer is missing")
    binding = document.get("model_binding")
    if not isinstance(binding, Mapping):
        reasons.append("accuracy report is not bound to a model artifact")
    else:
        reported_sha = binding.get("artifact_sha256")
        result["bound_to_artifact"] = (
            isinstance(reported_sha, str) and reported_sha.lower() == artifact_sha256
        )
        if not result["bound_to_artifact"]:
            reasons.append("accuracy report model_binding artifact SHA-256 does not match")
        if binding.get("model_id") not in {None, model_id}:
            reasons.append("accuracy report model_binding model_id does not match")
        if binding.get("model_version") not in {None, model_version}:
            reasons.append("accuracy report model_binding model_version does not match")
    result["passed"] = not reasons
    return result


def _load_evidence_document(
    report_path: str | Path | None,
    label: str,
    result: dict[str, Any],
    reasons: list[str],
) -> Mapping[str, Any] | None:
    if report_path is None:
        reasons.append(f"{label} report was not supplied")
        return None
    try:
        report = _resolve_input(report_path, f"{label} report")
        payload = report.read_bytes()
        document = _strict_json_loads(payload, label)
    except (OSError, ModelReviewError) as exc:
        reasons.append(str(exc))
        return None
    if not isinstance(document, Mapping):
        reasons.append(f"{label} report root must be a JSON object")
        return None
    result["path"] = str(report)
    result["sha256"] = hashlib.sha256(payload).hexdigest()
    return document


def _normalize_review_decisions(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {name: "pending" for name in MODEL_REVIEW_REQUIRED_REVIEWS}
    if not isinstance(value, Mapping):
        raise ModelReviewError("review_decisions must be an object")
    unknown = set(value) - set(MODEL_REVIEW_REQUIRED_REVIEWS)
    if unknown:
        raise ModelReviewError(f"unknown model review decision(s): {', '.join(sorted(unknown))}")
    normalized: dict[str, str] = {}
    for name in MODEL_REVIEW_REQUIRED_REVIEWS:
        status = value.get(name, "pending")
        if not isinstance(status, str) or status not in MODEL_REVIEW_STATUSES:
            choices = ", ".join(MODEL_REVIEW_STATUSES)
            raise ModelReviewError(f"{name} review must be one of: {choices}")
        normalized[name] = status
    return normalized


def _normalize_reviewer(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ModelReviewError("reviewer is required when a review is passed")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelReviewError("reviewer must be a non-empty string")
    return value.strip()


def _normalize_review_timestamp(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            return None
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelReviewError("reviewed_at_utc must be a non-empty ISO-8601 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ModelReviewError("reviewed_at_utc must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelReviewError("reviewed_at_utc must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_actions(
    decisions: Mapping[str, str],
    *,
    model_check: Mapping[str, Any],
    accuracy: Mapping[str, Any],
    synthetic_hil_only: bool,
) -> list[str]:
    actions: list[str] = []
    if synthetic_hil_only:
        actions.append("replace the synthetic HIL artifact with a reviewed deployment candidate")
    for name, status in decisions.items():
        if status == "pending":
            actions.append(f"record an independent {name} review decision")
        elif status == "failed":
            actions.append(f"resolve the failed {name} review and record a new decision")
    if not model_check["passed"]:
        actions.append("run model-check and bind its report to the exact candidate artifact")
    if not accuracy["passed"]:
        actions.append(
            "collect reviewed deployment strata, then run evaluate-replay-set with "
            "--model-artifact and --reviewed"
        )
    return _unique(actions)


def _accuracy_metrics_are_defined(metrics: object) -> bool:
    if not isinstance(metrics, Mapping):
        return False
    overall = metrics.get("overall")
    if not isinstance(overall, Mapping):
        return False
    return all(_finite_probability(overall.get(name)) for name in ("precision", "recall"))


def _finite_probability(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _positive_strata_coverage_is_complete(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value) != set(REPLAY_SET_POSITIVE_STRATA):
        return False
    for stratum in REPLAY_SET_POSITIVE_STRATA:
        item = value.get(stratum)
        if not isinstance(item, Mapping):
            return False
        if any(
            not isinstance(item.get(field), int)
            or isinstance(item.get(field), bool)
            or item.get(field) <= 0
            for field in ("positive_truth_object_count", "positive_prediction_count")
        ):
            return False
    return True


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _resolve_input(path: str | Path, label: str) -> Path:
    requested = Path(path).expanduser()
    if requested.is_symlink():
        raise ModelReviewError(f"{label} path must not be a symbolic link")
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ModelReviewError(f"{label} does not exist: {requested}") from exc
    if not resolved.is_file():
        raise ModelReviewError(f"{label} is not a file: {resolved}")
    return resolved


def _strict_json_loads(payload: bytes, label: str) -> object:
    def reject_constant(value: str) -> None:
        raise ModelReviewError(f"{label} contains non-finite JSON number {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ModelReviewError(f"{label} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelReviewError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _atomic_write_json(path: Path, document: Mapping[str, Any], *, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise ModelReviewError(f"model review output already exists: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = [
    "MODEL_REVIEW_REQUIRED_REVIEWS",
    "MODEL_REVIEW_SCHEMA_VERSION",
    "MODEL_REVIEW_STATUSES",
    "ModelReviewBundle",
    "ModelReviewError",
    "prepare_model_review_bundle",
    "verify_model_review_bundle",
]
