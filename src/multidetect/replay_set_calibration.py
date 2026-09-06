"""Per-class confidence calibration for a hash-bound replay set.

The replay-set evaluator answers whether a prediction stream meets the
deployment accuracy gate.  This module answers the smaller tuning question:
which confidence threshold is worth reviewing for each class, and how stable
is that recommendation across the six lighting/content strata?

Calibration is intentionally advisory.  A report never promotes a model or a
release by itself.  ``promotion_ready`` is true only when the manifest is a
complete deployment recording, every prediction stream is present, and an
independent reviewer explicitly acknowledges the result.  Synthetic or
camera-only SIM fixtures therefore remain useful for wiring and diagnostics
without becoming field accuracy evidence.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .autonomy.serialization import strict_json_loads
from .calibration import calibrate_class_thresholds, evaluate_class_thresholds
from .evaluation import (
    GroundTruthFrame,
    PredictionFrame,
    evaluation_document,
    load_ground_truth_jsonl,
    load_prediction_jsonl,
)
from .replay_set import REPLAY_SET_STRATA, load_replay_set_manifest


class ReplaySetCalibrationError(ValueError):
    """Raised when a replay-set calibration request is malformed."""


def calibrate_replay_set_thresholds(
    manifest_path: str | Path,
    *,
    thresholds: Iterable[float],
    current_thresholds: Mapping[str, float] | None = None,
    iou_threshold: float = 0.5,
    beta: float = 0.5,
    minimum_recall: float = 0.70,
    stability_tolerance: float = 0.10,
    reviewed: bool = False,
    reviewer: str | None = None,
    prediction_run_receipt: str | Path | None = None,
    accuracy_report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Sweep thresholds over all bound predictions and report strata stability.

    ``manifest_path`` is reloaded and all referenced files are hash-verified by
    :func:`load_replay_set_manifest` before any metrics are calculated.  Frame
    IDs are prefixed with their manifest entry ID so two camera-local streams
    cannot accidentally collide in the aggregate.
    """

    source = Path(manifest_path).expanduser().resolve(strict=True)
    if type(reviewed) is not bool:
        raise ReplaySetCalibrationError("reviewed must be a boolean")
    normalized_reviewer = _normalize_reviewer(reviewer, reviewed=reviewed)
    grid = _validated_threshold_grid(thresholds)
    if not math.isfinite(float(iou_threshold)) or not 0.0 < float(iou_threshold) <= 1.0:
        raise ReplaySetCalibrationError("iou_threshold must be in (0, 1]")
    if not math.isfinite(float(beta)) or float(beta) <= 0.0:
        raise ReplaySetCalibrationError("beta must be finite and positive")
    if (
        not math.isfinite(float(minimum_recall))
        or float(minimum_recall) < 0.0
        or float(minimum_recall) > 1.0
    ):
        raise ReplaySetCalibrationError("minimum_recall must be in [0, 1]")
    if (
        not math.isfinite(float(stability_tolerance))
        or float(stability_tolerance) < 0.0
        or float(stability_tolerance) > 1.0
    ):
        raise ReplaySetCalibrationError("stability_tolerance must be in [0, 1]")
    normalized_current = _normalize_current_thresholds(current_thresholds)

    manifest = load_replay_set_manifest(source)
    raw_payload = source.read_bytes()
    prediction_run_binding: dict[str, object] | None = None
    accuracy_report_binding: dict[str, object] | None = None
    if (prediction_run_receipt is None) != (accuracy_report_path is None):
        raise ReplaySetCalibrationError(
            "prediction_run_receipt and accuracy_report_path must be supplied together"
        )
    if prediction_run_receipt is not None and accuracy_report_path is not None:
        from .replay_prediction_run import verified_prediction_run_bindings
        from .replay_set_readiness import check_replay_set_readiness

        bindings = verified_prediction_run_bindings(source, prediction_run_receipt)
        prediction_run_binding = dict(bindings["prediction_run_binding"])
        requested_accuracy = Path(accuracy_report_path).expanduser()
        if requested_accuracy.is_symlink():
            raise ReplaySetCalibrationError("accuracy_report_path must not be a symbolic link")
        accuracy_source = requested_accuracy.resolve(strict=True)
        readiness = check_replay_set_readiness(
            source,
            accuracy_report_path=accuracy_source,
        )
        if readiness.get("gate_passed") is not True:
            details = "; ".join(str(reason) for reason in readiness.get("blocking_reasons", []))
            raise ReplaySetCalibrationError(
                "accuracy report did not pass fresh readiness recomputation"
                + (f": {details}" if details else "")
            )
        accuracy_payload = accuracy_source.read_bytes()
        accuracy_document = strict_json_loads(accuracy_payload, name="accuracy report")
        if not isinstance(accuracy_document, Mapping):
            raise ReplaySetCalibrationError("accuracy report root must be a JSON object")
        if accuracy_document.get("model_binding") != bindings["model_binding"]:
            raise ReplaySetCalibrationError(
                "accuracy report model binding does not match the verified prediction run"
            )
        if accuracy_document.get("prediction_run_binding") != prediction_run_binding:
            raise ReplaySetCalibrationError(
                "accuracy report prediction binding does not match the verified prediction run"
            )
        accuracy_report_binding = {
            "path": str(accuracy_source),
            "sha256": hashlib.sha256(accuracy_payload).hexdigest(),
            "manifest_sha256": str(accuracy_document["manifest_sha256"]),
            "canonical_sha256": str(accuracy_document["canonical_sha256"]),
            "prediction_receipt_sha256": str(prediction_run_binding["receipt_sha256"]),
            "prediction_receipt_canonical_hash": str(
                prediction_run_binding["receipt_canonical_hash"]
            ),
            "model_artifact_sha256": str(bindings["model_binding"]["artifact_sha256"]),
            "reviewed": accuracy_document.get("reviewed") is True,
            "accuracy_validated": accuracy_document.get("accuracy_validated") is True,
            "gate_passed": accuracy_document.get("gate_passed") is True,
        }
    failure_reasons: list[str] = []
    if not manifest.is_complete:
        failure_reasons.append("replay-set deployment coverage is incomplete")
    if set(manifest.required_strata) != set(REPLAY_SET_STRATA):
        failure_reasons.append("calibration requires all six deployment strata")

    entries: list[dict[str, Any]] = []
    aggregate_truth: list[GroundTruthFrame] = []
    aggregate_predictions: list[PredictionFrame] = []
    stratum_frames: dict[str, tuple[list[GroundTruthFrame], list[PredictionFrame]]] = {
        stratum: ([], []) for stratum in manifest.required_strata
    }
    predictions_complete = True

    for entry in manifest.entries:
        truth_path = entry.ground_truth.resolve(
            source.parent, name=f"{entry.entry_id} ground_truth"
        )
        truth = load_ground_truth_jsonl(truth_path)
        entry_report: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "strata": list(entry.strata),
            "frame_count": entry.frame_count,
            "annotations_reviewed": entry.annotations_reviewed,
            "ground_truth": entry.ground_truth.to_mapping(),
            "predictions": (
                entry.predictions.to_mapping() if entry.predictions is not None else None
            ),
            "calibration": None,
            "current_metrics": None,
            "failure_reasons": [],
        }
        if entry.predictions is None:
            predictions_complete = False
            reason = f"entry {entry.entry_id} predictions are missing"
            entry_report["failure_reasons"].append(reason)
            failure_reasons.append(reason)
            entries.append(entry_report)
            continue

        predictions_path = entry.predictions.resolve(
            source.parent, name=f"{entry.entry_id} predictions"
        )
        predictions = load_prediction_jsonl(predictions_path)
        prefixed_truth = tuple(
            GroundTruthFrame(f"{entry.entry_id}:{frame.frame_id}", frame.objects) for frame in truth
        )
        prefixed_predictions = tuple(
            PredictionFrame(
                f"{entry.entry_id}:{frame.frame_id}",
                frame.detections,
                frame.inference_latency_ms,
            )
            for frame in predictions
        )
        aggregate_truth.extend(prefixed_truth)
        aggregate_predictions.extend(prefixed_predictions)
        for stratum in entry.strata:
            stratum_truth, stratum_predictions = stratum_frames[stratum]
            stratum_truth.extend(prefixed_truth)
            stratum_predictions.extend(prefixed_predictions)

        entry_calibration = calibrate_class_thresholds(
            prefixed_truth,
            prefixed_predictions,
            thresholds=grid,
            iou_threshold=float(iou_threshold),
            beta=float(beta),
            minimum_recall=float(minimum_recall),
        )
        entry_report["calibration"] = entry_calibration
        if normalized_current is not None:
            entry_report["current_metrics"] = evaluation_document(
                evaluate_class_thresholds(
                    prefixed_truth,
                    prefixed_predictions,
                    thresholds=normalized_current,
                    iou_threshold=float(iou_threshold),
                )
            )
        entries.append(entry_report)

    aggregate_calibration: dict[str, Any] | None = None
    current_metrics: dict[str, Any] | None = None
    if predictions_complete and aggregate_truth and aggregate_predictions:
        aggregate_calibration = calibrate_class_thresholds(
            tuple(aggregate_truth),
            tuple(aggregate_predictions),
            thresholds=grid,
            iou_threshold=float(iou_threshold),
            beta=float(beta),
            minimum_recall=float(minimum_recall),
        )
        if normalized_current is not None:
            current_metrics = evaluation_document(
                evaluate_class_thresholds(
                    tuple(aggregate_truth),
                    tuple(aggregate_predictions),
                    thresholds=normalized_current,
                    iou_threshold=float(iou_threshold),
                )
            )
    elif predictions_complete:
        failure_reasons.append("replay-set contains no evaluable frames")

    strata: dict[str, dict[str, Any]] = {}
    for stratum in REPLAY_SET_STRATA:
        truth, predictions = stratum_frames.get(stratum, ([], []))
        if not truth or not predictions:
            strata[stratum] = {
                "frame_count": len(truth),
                "available": False,
                "calibration": None,
                "current_metrics": None,
            }
            if stratum in manifest.required_strata:
                failure_reasons.append(f"stratum {stratum} has no complete prediction stream")
            continue
        stratum_calibration = calibrate_class_thresholds(
            tuple(truth),
            tuple(predictions),
            thresholds=grid,
            iou_threshold=float(iou_threshold),
            beta=float(beta),
            minimum_recall=float(minimum_recall),
        )
        stratum_current = None
        if normalized_current is not None:
            stratum_current = evaluation_document(
                evaluate_class_thresholds(
                    tuple(truth),
                    tuple(predictions),
                    thresholds=normalized_current,
                    iou_threshold=float(iou_threshold),
                )
            )
        strata[stratum] = {
            "frame_count": len(truth),
            "available": True,
            "calibration": stratum_calibration,
            "current_metrics": stratum_current,
        }

    stability = _build_stability_summary(
        strata,
        tolerance=float(stability_tolerance),
    )

    recommendations = (
        aggregate_calibration.get("recommendations", [])
        if aggregate_calibration is not None
        else []
    )
    if not recommendations:
        failure_reasons.append("no class threshold recommendations were produced")
    if not reviewed:
        failure_reasons.append("independent threshold review is not acknowledged")
    promotion_ready = bool(
        manifest.is_complete
        and predictions_complete
        and set(manifest.required_strata) == set(REPLAY_SET_STRATA)
        and aggregate_calibration is not None
        and recommendations
        and reviewed
    )

    return {
        "event": "replay_set_threshold_calibration_completed",
        "schema_version": 1,
        "manifest_path": str(source),
        "manifest_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "canonical_sha256": manifest.canonical_hash,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "required_strata": list(manifest.required_strata),
        "coverage": manifest.coverage(),
        "deployment_domain_evidence_complete": manifest.is_complete,
        "predictions_complete": predictions_complete,
        "prediction_run_binding": prediction_run_binding,
        "accuracy_report_binding": accuracy_report_binding,
        "provenance_complete": bool(
            prediction_run_binding is not None and accuracy_report_binding is not None
        ),
        "threshold_grid": list(grid),
        "iou_threshold": float(iou_threshold),
        "beta": float(beta),
        "minimum_recall": float(minimum_recall),
        "stability_tolerance": float(stability_tolerance),
        "current_thresholds": normalized_current,
        "current_metrics": current_metrics,
        "entries": entries,
        "strata": strata,
        "stability": stability,
        "calibration": aggregate_calibration,
        "reviewed": reviewed,
        "reviewer": normalized_reviewer,
        "promotion_ready": promotion_ready,
        "failure_reasons": _unique_reasons(failure_reasons),
    }


def verify_replay_set_calibration_report(
    report_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
    prediction_run_receipt: str | Path | None = None,
    accuracy_report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Independently re-compute and verify a calibration report.

    The calibration writer is deliberately advisory, so a report must not be
    trusted merely because it has the expected JSON shape.  This verifier
    reloads the hash-bound replay-set, checks the manifest digests, re-runs the
    exact calibration request encoded in the report, and compares the complete
    JSON document after normalizing machine-specific path strings.  A manifest
    override is available for reports copied from another checkout; the
    content hashes still have to match the copied manifest.
    """

    report_source = Path(report_path).expanduser().resolve(strict=True)
    report_payload = report_source.read_bytes()
    report_sha256 = hashlib.sha256(report_payload).hexdigest()
    report_bytes = len(report_payload)
    reasons: list[str] = []
    checks: dict[str, bool] = {
        "report_json_valid": False,
        "report_event_valid": False,
        "report_schema_version_valid": False,
        "manifest_available": False,
        "manifest_sha256_matches": False,
        "canonical_sha256_matches": False,
        "recomputed_report_matches": False,
    }

    try:
        document = strict_json_loads(report_payload, name="calibration report")
    except (UnicodeError, ValueError) as exc:
        reasons.append(f"calibration report is not valid UTF-8 JSON: {exc}")
        return _calibration_verification_document(
            report_source,
            report_sha256=report_sha256,
            report_bytes=report_bytes,
            manifest_path=None,
            document=None,
            checks=checks,
            reasons=reasons,
            recomputed=None,
        )

    checks["report_json_valid"] = isinstance(document, Mapping)
    if not checks["report_json_valid"]:
        reasons.append("calibration report root must be a JSON object")
        return _calibration_verification_document(
            report_source,
            report_sha256=report_sha256,
            report_bytes=report_bytes,
            manifest_path=None,
            document=None,
            checks=checks,
            reasons=reasons,
            recomputed=None,
        )

    checks["report_event_valid"] = document.get("event") == (
        "replay_set_threshold_calibration_completed"
    )
    checks["report_schema_version_valid"] = document.get("schema_version") == 1
    if not checks["report_event_valid"]:
        reasons.append("calibration report event is invalid")
    if not checks["report_schema_version_valid"]:
        reasons.append("calibration report schema_version is invalid")

    manifest_value: object = manifest_path
    if manifest_value is None:
        manifest_value = document.get("manifest_path")
    resolved_manifest: Path | None = None
    if not isinstance(manifest_value, (str, Path)) or not str(manifest_value).strip():
        reasons.append("calibration report manifest_path is missing")
    else:
        try:
            resolved_manifest = Path(manifest_value).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            reasons.append(f"calibration report manifest_path is unavailable: {exc}")

    recomputed: dict[str, Any] | None = None
    if resolved_manifest is not None:
        try:
            manifest = load_replay_set_manifest(resolved_manifest)
            raw_manifest = resolved_manifest.read_bytes()
        except (OSError, ReplaySetCalibrationError, ValueError, TypeError) as exc:
            reasons.append(f"replay-set manifest could not be loaded: {exc}")
        else:
            checks["manifest_available"] = True
            observed_manifest_sha256 = hashlib.sha256(raw_manifest).hexdigest()
            expected_manifest_sha256 = document.get("manifest_sha256")
            checks["manifest_sha256_matches"] = expected_manifest_sha256 == observed_manifest_sha256
            if not checks["manifest_sha256_matches"]:
                reasons.append(
                    "calibration report manifest_sha256 does not match the bound manifest"
                )
            expected_canonical_sha256 = document.get("canonical_sha256")
            checks["canonical_sha256_matches"] = (
                expected_canonical_sha256 == manifest.canonical_hash
            )
            if not checks["canonical_sha256_matches"]:
                reasons.append(
                    "calibration report canonical_sha256 does not match the bound manifest"
                )

            try:
                prediction_binding = document.get("prediction_run_binding")
                accuracy_binding = document.get("accuracy_report_binding")
                bound_prediction_receipt: str | Path | None = prediction_run_receipt
                bound_accuracy_report: str | Path | None = accuracy_report_path
                if bound_prediction_receipt is None and isinstance(prediction_binding, Mapping):
                    bound_prediction_receipt = prediction_binding.get("receipt_path")
                if bound_accuracy_report is None and isinstance(accuracy_binding, Mapping):
                    bound_accuracy_report = accuracy_binding.get("path")
                recomputed = calibrate_replay_set_thresholds(
                    resolved_manifest,
                    thresholds=document.get("threshold_grid"),
                    current_thresholds=document.get("current_thresholds"),
                    iou_threshold=document.get("iou_threshold"),
                    beta=document.get("beta"),
                    minimum_recall=document.get("minimum_recall"),
                    stability_tolerance=document.get("stability_tolerance"),
                    reviewed=document.get("reviewed"),
                    reviewer=document.get("reviewer"),
                    prediction_run_receipt=bound_prediction_receipt,
                    accuracy_report_path=bound_accuracy_report,
                )
            except (
                OSError,
                RuntimeError,
                ReplaySetCalibrationError,
                TypeError,
                ValueError,
                KeyError,
            ) as exc:
                reasons.append(f"calibration report request could not be re-computed: {exc}")
            else:
                checks["recomputed_report_matches"] = _json_equivalent(
                    _normalize_report_paths(_with_legacy_provenance_defaults(document)),
                    _normalize_report_paths(recomputed),
                )
                if not checks["recomputed_report_matches"]:
                    reasons.append("calibration report differs from deterministic re-computation")

    return _calibration_verification_document(
        report_source,
        report_sha256=report_sha256,
        report_bytes=report_bytes,
        manifest_path=resolved_manifest,
        document=document,
        checks=checks,
        reasons=reasons,
        recomputed=recomputed,
    )


def _validated_threshold_grid(thresholds: Iterable[float]) -> tuple[float, ...]:
    try:
        grid = tuple(sorted(set(float(value) for value in thresholds)))
    except (TypeError, ValueError) as exc:
        raise ReplaySetCalibrationError(
            "calibration thresholds must be finite values in [0, 1]"
        ) from exc
    if not grid or any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in grid):
        raise ReplaySetCalibrationError("calibration thresholds must be finite values in [0, 1]")
    return grid


def _calibration_verification_document(
    report_path: Path,
    *,
    report_sha256: str,
    report_bytes: int,
    manifest_path: Path | None,
    document: Mapping[str, Any] | None,
    checks: Mapping[str, bool],
    reasons: Sequence[str],
    recomputed: Mapping[str, Any] | None,
) -> dict[str, Any]:
    report_promotion_ready = (
        document.get("promotion_ready") if isinstance(document, Mapping) else None
    )
    recomputed_promotion_ready = (
        recomputed.get("promotion_ready") if isinstance(recomputed, Mapping) else None
    )
    report_provenance_complete = (
        document.get("provenance_complete") is True if isinstance(document, Mapping) else None
    )
    recomputed_provenance_complete = (
        recomputed.get("provenance_complete") is True if isinstance(recomputed, Mapping) else None
    )
    unique_reasons = _unique_reasons(list(reasons))
    return {
        "event": "replay_set_threshold_calibration_verified",
        "schema_version": 1,
        "report_path": str(report_path),
        "report_sha256": report_sha256,
        "report_bytes": report_bytes,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
        "report_promotion_ready": report_promotion_ready,
        "recomputed_promotion_ready": recomputed_promotion_ready,
        "report_provenance_complete": report_provenance_complete,
        "recomputed_provenance_complete": recomputed_provenance_complete,
        "checks": dict(checks),
        "gate_passed": not unique_reasons,
        "reasons": unique_reasons,
    }


def _normalize_report_paths(value: object, *, key: str | None = None) -> object:
    """Replace checkout-specific path strings before semantic report comparison."""

    if isinstance(value, Mapping):
        return {
            name: _normalize_report_paths(
                "<path>"
                if isinstance(item, str)
                and (name == "path" or name == "manifest_path" or name.endswith("_path"))
                else item,
                key=name,
            )
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_report_paths(item, key=key) for item in value]
    return value


def _with_legacy_provenance_defaults(document: Mapping[str, Any]) -> dict[str, Any]:
    """Upgrade a pre-v112 advisory report for deterministic compatibility checks."""

    normalized = dict(document)
    normalized.setdefault("prediction_run_binding", None)
    normalized.setdefault("accuracy_report_binding", None)
    normalized.setdefault("provenance_complete", False)
    return normalized


def _json_equivalent(left: object, right: object) -> bool:
    """Compare JSON values with strict bool/type handling and float tolerance."""

    if type(left) is not type(right):
        if isinstance(left, (int, float)) and not isinstance(left, bool):
            if isinstance(right, (int, float)) and not isinstance(right, bool):
                return math.isclose(float(left), float(right), rel_tol=1.0e-12, abs_tol=1.0e-9)
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(
            _json_equivalent(left[name], right[name]) for name in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equivalent(item_left, item_right)
            for item_left, item_right in zip(left, right, strict=True)
        )
    return left == right


def parse_threshold_grid(raw: str) -> tuple[float, ...]:
    """Parse ``start:stop:step`` or comma-separated threshold values."""

    text = raw.strip()
    if not text:
        raise ReplaySetCalibrationError("threshold grid cannot be empty")
    try:
        if ":" not in text:
            values = tuple(float(value.strip()) for value in text.split(",") if value.strip())
        else:
            parts = tuple(part.strip() for part in text.split(":"))
            if len(parts) != 3:
                raise ReplaySetCalibrationError("threshold range must be start:stop:step")
            start, stop, step = (float(value) for value in parts)
            if step <= 0.0 or stop < start:
                raise ReplaySetCalibrationError(
                    "threshold range requires stop >= start and a positive step"
                )
            count = int(round((stop - start) / step))
            if count > 1000:
                raise ReplaySetCalibrationError(
                    "threshold range cannot contain more than 1001 values"
                )
            values = tuple(round(start + index * step, 10) for index in range(count + 1))
    except (TypeError, ValueError) as exc:
        raise ReplaySetCalibrationError(
            "threshold grid must contain finite numeric values"
        ) from exc
    return _validated_threshold_grid(values)


def parse_current_thresholds(raw: str) -> dict[str, float] | None:
    """Parse ``label=value`` pairs used for the current-threshold comparison."""

    if not isinstance(raw, str):
        raise ReplaySetCalibrationError("current thresholds must be text")
    text = raw.strip()
    if not text or text.lower() in {"none", "off"}:
        return None
    result: dict[str, float] = {}
    for item in text.split(","):
        label, separator, threshold = item.partition("=")
        if not separator or not label.strip():
            raise ReplaySetCalibrationError("current thresholds must use label=value")
        try:
            result[label.strip().lower()] = float(threshold.strip())
        except ValueError as exc:
            raise ReplaySetCalibrationError(
                "current thresholds must contain numeric values"
            ) from exc
    return _normalize_current_thresholds(result)


def _normalize_current_thresholds(
    thresholds: Mapping[str, float] | None,
) -> dict[str, float] | None:
    if thresholds is None:
        return None
    if not isinstance(thresholds, Mapping):
        raise ReplaySetCalibrationError("current_thresholds must be an object")
    result: dict[str, float] = {}
    for raw_label, raw_threshold in thresholds.items():
        if not isinstance(raw_label, str) or not raw_label.strip():
            raise ReplaySetCalibrationError("current threshold labels cannot be empty")
        if (
            isinstance(raw_threshold, bool)
            or not isinstance(raw_threshold, (int, float))
            or not math.isfinite(float(raw_threshold))
            or not 0.0 <= float(raw_threshold) <= 1.0
        ):
            raise ReplaySetCalibrationError("current thresholds must be finite values in [0, 1]")
        label = raw_label.strip().lower()
        if label == "fire":
            label = "flame"
        if label in result:
            raise ReplaySetCalibrationError(f"duplicate current threshold label: {label}")
        result[label] = float(raw_threshold)
    if not result:
        raise ReplaySetCalibrationError("current_thresholds cannot be empty")
    return dict(sorted(result.items()))


def _normalize_reviewer(value: str | None, *, reviewed: bool) -> str | None:
    if value is None:
        if reviewed:
            raise ReplaySetCalibrationError("reviewer must be supplied when reviewed is true")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ReplaySetCalibrationError("reviewer must be a non-empty string")
    return value.strip()


def _build_stability_summary(
    strata: Mapping[str, Mapping[str, Any]],
    *,
    tolerance: float,
) -> dict[str, dict[str, Any]]:
    """Summarize how far per-class recommendations move across strata.

    A class is marked stable when at least two available strata recommend it
    and the absolute threshold range is within ``tolerance``.  A stratum with
    no positive examples (for example ``no_fire``) is reported as missing a
    recommendation instead of being treated as a zero-valued threshold.
    """

    observations: dict[str, list[tuple[str, float]]] = {}
    available_strata = [
        stratum
        for stratum in REPLAY_SET_STRATA
        if isinstance(strata.get(stratum), Mapping) and strata[stratum].get("available") is True
    ]
    for stratum in available_strata:
        calibration = strata[stratum].get("calibration")
        if not isinstance(calibration, Mapping):
            continue
        recommendations = calibration.get("recommendations")
        if not isinstance(recommendations, list):
            continue
        for recommendation in recommendations:
            if not isinstance(recommendation, Mapping):
                continue
            raw_label = recommendation.get("label")
            raw_threshold = recommendation.get("threshold")
            if not isinstance(raw_label, str) or not raw_label.strip():
                continue
            if (
                isinstance(raw_threshold, bool)
                or not isinstance(raw_threshold, (int, float))
                or not math.isfinite(float(raw_threshold))
                or not 0.0 <= float(raw_threshold) <= 1.0
            ):
                continue
            label = raw_label.strip().lower()
            observations.setdefault(label, []).append((stratum, float(raw_threshold)))

    summary: dict[str, dict[str, Any]] = {}
    for label, values in sorted(observations.items()):
        thresholds = [threshold for _stratum, threshold in values]
        recommended_strata = [stratum for stratum, _threshold in values]
        missing_strata = [
            stratum for stratum in available_strata if stratum not in recommended_strata
        ]
        threshold_min = min(thresholds)
        threshold_max = max(thresholds)
        summary[label] = {
            "recommended_strata": recommended_strata,
            "missing_recommendation_strata": missing_strata,
            "recommendation_count": len(values),
            "available_strata_count": len(available_strata),
            "coverage_ratio": len(values) / len(REPLAY_SET_STRATA),
            "thresholds_by_stratum": {stratum: threshold for stratum, threshold in values},
            "threshold_min": threshold_min,
            "threshold_max": threshold_max,
            "threshold_range": threshold_max - threshold_min,
            "threshold_mean": sum(thresholds) / len(thresholds),
            "stable_at_tolerance": bool(
                len(values) >= 2 and threshold_max - threshold_min <= tolerance
            ),
        }
    return summary


def _unique_reasons(reasons: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            result.append(reason)
    return result


__all__ = [
    "ReplaySetCalibrationError",
    "calibrate_replay_set_thresholds",
    "parse_current_thresholds",
    "parse_threshold_grid",
    "verify_replay_set_calibration_report",
]
