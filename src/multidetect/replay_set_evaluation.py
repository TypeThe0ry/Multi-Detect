"""Hash-bound accuracy evaluation for a stratified :mod:`replay_set`.

``ReplaySetManifest`` deliberately answers a provenance question: are the raw
recording, reviewed truth and (when present) predictions complete and bound to
one dataset identity?  This module answers the separate quality question by
running the project's normal frame-aligned IoU evaluator for every stratum.

The evaluator never edits the replay-set manifest.  Its output carries both the
raw and canonical manifest digests, every referenced prediction/truth digest,
the exact evaluator thresholds and an explicit independent-review flag.  A
report can therefore be attached to a release-gate manifest without turning a
directory or a prediction file into an unreviewed accuracy claim.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .evaluation import (
    GroundTruthFrame,
    PredictionFrame,
    evaluate_detections,
    evaluation_document,
    load_ground_truth_jsonl,
    load_prediction_jsonl,
)
from .replay_set import (
    REPLAY_SET_POSITIVE_STRATA,
    REPLAY_SET_STRATA,
    load_replay_set_manifest,
)


def evaluate_replay_set(
    manifest_path: str | Path,
    *,
    iou_threshold: float = 0.5,
    confidence_threshold: float = 0.25,
    minimum_precision: float | None = None,
    minimum_recall: float | None = None,
    maximum_false_alarm_frame_rate: float | None = None,
    maximum_missed_detection_frame_rate: float | None = None,
    maximum_latency_p95_ms: float | None = None,
    reviewed: bool = False,
    reviewer: str | None = None,
    model_artifact_sha256: str | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    prediction_run_receipt: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate all prediction streams bound by a replay-set manifest.

    The function returns a report even when collection is pending or a
    prediction stream is absent.  In those cases ``gate_passed`` and
    ``accuracy_validated`` are false and ``failure_reasons`` names the exact
    missing condition.  Invalid JSON, hashes or frame alignment remain hard
    contract errors from :func:`load_replay_set_manifest`.

    ``reviewed`` is an explicit independent quality-review acknowledgement;
    metrics alone never promote ``accuracy_validated``.  ``reviewer`` is
    optional metadata, but a non-empty value is required when ``reviewed`` is
    true so an evidence report cannot silently claim an anonymous review.
    """

    source = Path(manifest_path).expanduser().resolve(strict=True)
    _validate_thresholds(
        iou_threshold=iou_threshold,
        confidence_threshold=confidence_threshold,
        minimum_precision=minimum_precision,
        minimum_recall=minimum_recall,
        maximum_false_alarm_frame_rate=maximum_false_alarm_frame_rate,
        maximum_missed_detection_frame_rate=maximum_missed_detection_frame_rate,
        maximum_latency_p95_ms=maximum_latency_p95_ms,
    )
    if type(reviewed) is not bool:
        raise ValueError("reviewed must be a boolean")
    normalized_reviewer = _normalize_reviewer(reviewer, reviewed=reviewed)
    manifest = load_replay_set_manifest(source)
    raw_payload = source.read_bytes()
    prediction_run_binding: dict[str, Any] | None = None
    if prediction_run_receipt is not None:
        from .replay_prediction_run import verify_replay_predictions

        receipt_path = Path(prediction_run_receipt).expanduser().resolve(strict=True)
        expected_receipt = source.parent / "prediction-run.receipt.json"
        if receipt_path != expected_receipt:
            raise ValueError(
                "prediction_run_receipt must be prediction-run.receipt.json "
                "beside the replay manifest"
            )
        verified_run = verify_replay_predictions(source.parent)
        derived = {
            "model_artifact_sha256": str(verified_run["model_artifact_sha256"]),
            "model_id": str(verified_run["model_id"]),
            "model_version": str(verified_run["model_version"]),
        }
        supplied = {
            "model_artifact_sha256": model_artifact_sha256,
            "model_id": model_id,
            "model_version": model_version,
        }
        for field, value in supplied.items():
            if value is not None and value.strip().lower() != derived[field].lower():
                raise ValueError(f"{field} conflicts with verified prediction run receipt")
        model_artifact_sha256 = derived["model_artifact_sha256"]
        model_id = derived["model_id"]
        model_version = derived["model_version"]
        prediction_run_binding = {
            "receipt_path": str(receipt_path),
            "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            "receipt_canonical_hash": verified_run["canonical_hash"],
            "model_manifest_sha256": verified_run["model_manifest_sha256"],
            "runtime_inference_executed": verified_run["runtime_inference_executed"],
            "synthetic_hil_only": verified_run["synthetic_hil_only"],
        }
    model_binding = _normalize_model_binding(
        model_artifact_sha256=model_artifact_sha256,
        model_id=model_id,
        model_version=model_version,
    )
    failure_reasons: list[str] = []
    if not manifest.is_complete:
        failure_reasons.append("replay-set deployment coverage is incomplete")
    if set(manifest.required_strata) != set(REPLAY_SET_STRATA):
        failure_reasons.append("accuracy evaluation requires all six deployment strata")

    entries: list[dict[str, Any]] = []
    combined_truth: list[GroundTruthFrame] = []
    combined_predictions: list[PredictionFrame] = []
    predictions_complete = True
    positive_stratum_coverage: dict[str, dict[str, int]] = {
        stratum: {
            "positive_truth_object_count": 0,
            "positive_prediction_count": 0,
        }
        for stratum in REPLAY_SET_POSITIVE_STRATA
    }

    for entry in manifest.entries:
        entry_report: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "strata": list(entry.strata),
            "frame_count": entry.frame_count,
            "annotations_reviewed": entry.annotations_reviewed,
            "ground_truth": entry.ground_truth.to_mapping(),
            "predictions": (
                entry.predictions.to_mapping() if entry.predictions is not None else None
            ),
            "metrics": None,
            "gate_passed": False,
            "failure_reasons": [],
        }
        ground_truth_path = entry.ground_truth.resolve(
            source.parent, name=f"{entry.entry_id} ground_truth"
        )
        truth = load_ground_truth_jsonl(ground_truth_path)
        positive_truth_object_count = sum(len(frame.objects) for frame in truth)
        if entry.predictions is None:
            predictions_complete = False
            positive_prediction_count = 0
            reason = f"entry {entry.entry_id} predictions are missing"
            entry_report["failure_reasons"].append(reason)
            failure_reasons.append(reason)
            entry_report["positive_truth_object_count"] = positive_truth_object_count
            entry_report["positive_prediction_count"] = positive_prediction_count
            for stratum in entry.strata:
                if stratum in positive_stratum_coverage:
                    coverage = positive_stratum_coverage[stratum]
                    coverage["positive_truth_object_count"] += positive_truth_object_count
            entries.append(entry_report)
            continue

        predictions_path = entry.predictions.resolve(
            source.parent, name=f"{entry.entry_id} predictions"
        )
        predictions = load_prediction_jsonl(predictions_path)
        positive_prediction_count = sum(len(frame.detections) for frame in predictions)
        entry_metrics = evaluate_detections(
            truth,
            predictions,
            iou_threshold=iou_threshold,
            confidence_threshold=confidence_threshold,
        )
        entry_report["metrics"] = evaluation_document(entry_metrics)
        entry_report["gate_passed"] = True
        entry_report["positive_truth_object_count"] = positive_truth_object_count
        entry_report["positive_prediction_count"] = positive_prediction_count
        for stratum in entry.strata:
            if stratum in positive_stratum_coverage:
                coverage = positive_stratum_coverage[stratum]
                coverage["positive_truth_object_count"] += positive_truth_object_count
                coverage["positive_prediction_count"] += positive_prediction_count
        # Prefix frame IDs while retaining the original frame order.  A field
        # replay set may contain the same camera-local IDs in more than one
        # stratum; the prefix keeps the aggregate evaluator exact and unique.
        combined_truth.extend(
            GroundTruthFrame(f"{entry.entry_id}:{frame.frame_id}", frame.objects) for frame in truth
        )
        combined_predictions.extend(
            PredictionFrame(
                f"{entry.entry_id}:{frame.frame_id}",
                frame.detections,
                frame.inference_latency_ms,
            )
            for frame in predictions
        )
        entries.append(entry_report)

    aggregate: dict[str, Any] | None = None
    metrics_gate_passed = False
    positive_strata_coverage_passed = True
    for stratum in REPLAY_SET_POSITIVE_STRATA:
        coverage = positive_stratum_coverage[stratum]
        truth_count = coverage["positive_truth_object_count"]
        prediction_count = coverage["positive_prediction_count"]
        if truth_count <= 0:
            positive_strata_coverage_passed = False
            failure_reasons.append(f"{stratum} stratum contains no positive ground-truth object")
        if prediction_count <= 0:
            positive_strata_coverage_passed = False
            failure_reasons.append(f"{stratum} stratum contains no detector positive prediction")
    if predictions_complete and combined_truth and combined_predictions:
        aggregate_report = evaluate_detections(
            tuple(combined_truth),
            tuple(combined_predictions),
            iou_threshold=iou_threshold,
            confidence_threshold=confidence_threshold,
        )
        aggregate = evaluation_document(aggregate_report)
        metric_failures = _metric_failure_reasons(
            aggregate,
            minimum_precision=minimum_precision,
            minimum_recall=minimum_recall,
            maximum_false_alarm_frame_rate=maximum_false_alarm_frame_rate,
            maximum_missed_detection_frame_rate=maximum_missed_detection_frame_rate,
            maximum_latency_p95_ms=maximum_latency_p95_ms,
        )
        if metric_failures:
            failure_reasons.extend(metric_failures)
        metrics_gate_passed = (
            not metric_failures
            and manifest.is_complete
            and set(manifest.required_strata) == set(REPLAY_SET_STRATA)
            and positive_strata_coverage_passed
        )
    elif predictions_complete:
        failure_reasons.append("replay-set contains no evaluable frames")

    if reviewed and not predictions_complete:
        failure_reasons.append(
            "accuracy review cannot be acknowledged before predictions are complete"
        )
    if not reviewed:
        failure_reasons.append("independent accuracy review is not acknowledged")

    accuracy_validated = bool(metrics_gate_passed and reviewed)
    return {
        "event": "replay_set_accuracy_evaluation_completed",
        "schema_version": 1,
        "manifest_path": str(source),
        "manifest_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "canonical_sha256": manifest.canonical_hash,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "required_strata": list(manifest.required_strata),
        "coverage": manifest.coverage(),
        "positive_strata": list(REPLAY_SET_POSITIVE_STRATA),
        "positive_stratum_coverage": positive_stratum_coverage,
        "positive_strata_coverage_passed": positive_strata_coverage_passed,
        "structurally_valid": True,
        "deployment_domain_evidence_complete": manifest.is_complete,
        "predictions_complete": predictions_complete,
        "metrics_evaluated": aggregate is not None,
        "metrics_gate_passed": metrics_gate_passed,
        "reviewed": reviewed,
        "reviewer": normalized_reviewer,
        "model_binding": model_binding,
        "prediction_run_binding": prediction_run_binding,
        "thresholds": {
            "iou_threshold": iou_threshold,
            "confidence_threshold": confidence_threshold,
            "minimum_precision": minimum_precision,
            "minimum_recall": minimum_recall,
            "maximum_false_alarm_frame_rate": maximum_false_alarm_frame_rate,
            "maximum_missed_detection_frame_rate": maximum_missed_detection_frame_rate,
            "maximum_latency_p95_ms": maximum_latency_p95_ms,
        },
        "entries": entries,
        "metrics": aggregate,
        "failure_reasons": _unique_reasons(failure_reasons),
        "accuracy_validated": accuracy_validated,
        "gate_passed": accuracy_validated,
    }


def positive_strata_coverage_is_complete(document: Mapping[str, Any]) -> bool:
    """Check the serialized report's positive coverage contract.

    Release-gate consumers receive a report rather than the evaluator's live
    counters.  They must therefore verify that every positive deployment
    stratum is named and has non-zero reviewed truth and detector counts,
    instead of trusting only the aggregate gate flag.
    """

    if document.get("positive_strata") != list(REPLAY_SET_POSITIVE_STRATA):
        return False
    coverage = document.get("positive_stratum_coverage")
    if not isinstance(coverage, Mapping):
        return False
    if set(coverage) != set(REPLAY_SET_POSITIVE_STRATA):
        return False
    for stratum in REPLAY_SET_POSITIVE_STRATA:
        value = coverage.get(stratum)
        if not isinstance(value, Mapping):
            return False
        for field in ("positive_truth_object_count", "positive_prediction_count"):
            count = value.get(field)
            if type(count) is not int or count <= 0:
                return False
    return document.get("positive_strata_coverage_passed") is True


def _validate_thresholds(
    *,
    iou_threshold: float,
    confidence_threshold: float,
    minimum_precision: float | None,
    minimum_recall: float | None,
    maximum_false_alarm_frame_rate: float | None,
    maximum_missed_detection_frame_rate: float | None,
    maximum_latency_p95_ms: float | None,
) -> None:
    _bounded_probability(iou_threshold, "iou_threshold", minimum_exclusive=True)
    _bounded_probability(confidence_threshold, "confidence_threshold")
    for name, value in (
        ("minimum_precision", minimum_precision),
        ("minimum_recall", minimum_recall),
        ("maximum_false_alarm_frame_rate", maximum_false_alarm_frame_rate),
        ("maximum_missed_detection_frame_rate", maximum_missed_detection_frame_rate),
    ):
        if value is not None:
            _bounded_probability(value, name)
    if maximum_latency_p95_ms is not None:
        if (
            isinstance(maximum_latency_p95_ms, bool)
            or not isinstance(maximum_latency_p95_ms, (int, float))
            or not math.isfinite(float(maximum_latency_p95_ms))
            or float(maximum_latency_p95_ms) < 0
        ):
            raise ValueError("maximum_latency_p95_ms must be finite and non-negative")


def _bounded_probability(value: float, name: str, *, minimum_exclusive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    lower_valid = number > 0 if minimum_exclusive else number >= 0
    if not math.isfinite(number) or not lower_valid or number > 1:
        interval = "(0, 1]" if minimum_exclusive else "[0, 1]"
        raise ValueError(f"{name} must be in {interval}")


def _normalize_reviewer(value: str | None, *, reviewed: bool) -> str | None:
    if value is None:
        if reviewed:
            raise ValueError("reviewer must be supplied when reviewed is true")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reviewer must be a non-empty string")
    return value.strip()


def _normalize_model_binding(
    *,
    model_artifact_sha256: str | None,
    model_id: str | None,
    model_version: str | None,
) -> dict[str, str] | None:
    """Normalize an optional binding to the exact model used for inference.

    A replay report without a model binding remains valid for transport and
    evaluator diagnostics.  Promotion-oriented consumers can require this
    field to prevent metrics from being detached from the artifact that
    produced the prediction stream.
    """

    values = (model_artifact_sha256, model_id, model_version)
    if all(value is None for value in values):
        return None
    if model_artifact_sha256 is None:
        raise ValueError("model_artifact_sha256 is required when model metadata is supplied")
    if not isinstance(model_artifact_sha256, str):
        raise ValueError("model_artifact_sha256 must be a 64-character SHA-256 string")
    digest = model_artifact_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("model_artifact_sha256 must be a 64-character SHA-256 string")
    normalized: dict[str, str] = {"artifact_sha256": digest}
    for name, value in (("model_id", model_id), ("model_version", model_version)):
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string when supplied")
        normalized[name] = value.strip()
    return normalized


def _metric_failure_reasons(
    metrics: Mapping[str, Any],
    *,
    minimum_precision: float | None,
    minimum_recall: float | None,
    maximum_false_alarm_frame_rate: float | None,
    maximum_missed_detection_frame_rate: float | None,
    maximum_latency_p95_ms: float | None,
) -> list[str]:
    overall = metrics.get("overall")
    if not isinstance(overall, Mapping):
        return ["aggregate metrics are missing overall results"]
    failures: list[str] = []
    # An all-negative/empty baseline has no denominator for precision or
    # recall.  Treating ``null`` as a passing score would allow a complete
    # directory plus an anonymous review to close the deployment accuracy
    # gate without testing a single detector positive.  Keep this check in
    # the replay-set gate (rather than changing the shared evaluator's useful
    # ``None`` semantics) so negative-only datasets remain valid diagnostics.
    if not accuracy_metrics_are_defined(metrics):
        failures.append(
            "aggregate metrics precision/recall are undefined; replay-set must contain "
            "at least one evaluable positive truth and prediction"
        )
    _check_minimum(failures, "overall precision", overall.get("precision"), minimum_precision)
    _check_minimum(failures, "overall recall", overall.get("recall"), minimum_recall)
    frame_count = metrics.get("frame_count")
    if not isinstance(frame_count, int) or isinstance(frame_count, bool) or frame_count <= 0:
        failures.append("aggregate metrics frame_count is invalid")
        return failures
    if maximum_false_alarm_frame_rate is not None:
        observed = _rate(metrics.get("false_alarm_frame_count"), frame_count)
        if observed is None or observed > maximum_false_alarm_frame_rate:
            failures.append(
                "false-alarm frame rate "
                f"{_format_metric(observed)} exceeds maximum {maximum_false_alarm_frame_rate:.6g}"
            )
    if maximum_missed_detection_frame_rate is not None:
        observed = _rate(metrics.get("missed_detection_frame_count"), frame_count)
        if observed is None or observed > maximum_missed_detection_frame_rate:
            failures.append(
                "missed-detection frame rate "
                f"{_format_metric(observed)} exceeds maximum "
                f"{maximum_missed_detection_frame_rate:.6g}"
            )
    if maximum_latency_p95_ms is not None:
        observed = metrics.get("inference_latency_p95_ms")
        if not _finite_number(observed) or float(observed) > maximum_latency_p95_ms:
            failures.append(
                "inference latency p95 "
                f"{_format_metric(observed)} ms exceeds maximum {maximum_latency_p95_ms:.6g} ms"
            )
    return failures


def _check_minimum(failures: list[str], name: str, observed: object, minimum: float | None) -> None:
    if minimum is None:
        return
    if not _finite_number(observed) or float(observed) < minimum:
        failures.append(f"{name} {_format_metric(observed)} is below minimum {minimum:.6g}")


def _rate(count: object, frame_count: int) -> float | None:
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count / frame_count


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def accuracy_metrics_are_defined(metrics: object) -> bool:
    """Return whether aggregate precision and recall have usable denominators.

    ``evaluate_detections`` intentionally represents an undefined metric as
    ``None`` for diagnostics such as an all-negative replay.  Release-quality
    replay evidence needs both aggregate metrics to be finite probabilities;
    keeping this predicate public lets the report writer and the independent
    release-gate verifier enforce the same rule without changing the shared
    evaluator's negative-only semantics.
    """

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


def _format_metric(value: object) -> str:
    return f"{float(value):.6g}" if _finite_number(value) else "undefined"


def _unique_reasons(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reasons))


__all__ = [
    "accuracy_metrics_are_defined",
    "evaluate_replay_set",
    "positive_strata_coverage_is_complete",
]
