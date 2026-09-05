"""Read-only intake checks for the stratified detector replay-set.

``ReplaySetManifest`` and ``evaluate_replay_set`` already provide the strict
data and accuracy contracts.  This module is the operator-facing bridge
between those contracts: it explains *which* part of a pending replay-set is
missing and emits a deterministic, hash-bound readiness report.  It never
changes the manifest, labels, predictions or model configuration.

The report deliberately keeps the deployment and accuracy gates separate.  A
SIM fixture can therefore be useful for checking frame plumbing while still
being reported as pending until deployment recordings, reviewed labels and a
reviewed accuracy report are actually supplied.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .autonomy.serialization import AutonomyContractError
from .evaluation import load_ground_truth_jsonl, load_prediction_jsonl
from .replay_set import (
    REPLAY_SET_POSITIVE_STRATA,
    REPLAY_SET_STRATA,
    load_replay_set_manifest,
)
from .replay_set_evaluation import accuracy_metrics_are_defined, evaluate_replay_set

_ACCURACY_REPORT_FIELDS = (
    "structurally_valid",
    "deployment_domain_evidence_complete",
    "predictions_complete",
    "metrics_evaluated",
    "metrics_gate_passed",
    "positive_strata_coverage_passed",
    "reviewed",
    "accuracy_validated",
    "gate_passed",
)
_THRESHOLD_FIELDS = (
    "iou_threshold",
    "confidence_threshold",
    "minimum_precision",
    "minimum_recall",
    "maximum_false_alarm_frame_rate",
    "maximum_missed_detection_frame_rate",
    "maximum_latency_p95_ms",
)


def check_replay_set_readiness(
    manifest_path: str | Path,
    *,
    accuracy_report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect a replay-set and return an actionable readiness report.

    The manifest and every referenced raw/truth/prediction file are loaded
    through :func:`load_replay_set_manifest`, so hashes and frame alignment are
    re-checked before the report is produced.  If an accuracy report is
    supplied, its manifest bindings are checked and the exact evaluator
    request encoded in that report is run again.  A report is never accepted
    merely because it contains ``gate_passed=true`` text.
    """

    source = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = load_replay_set_manifest(source)
    raw_payload = source.read_bytes()
    manifest_sha256 = hashlib.sha256(raw_payload).hexdigest()

    stratum_stats: dict[str, dict[str, Any]] = {
        stratum: {
            "frame_count": 0,
            "positive_truth_object_count": 0,
            "prediction_detection_count": 0,
            "truth_labels": {},
            "prediction_labels": {},
            "entry_ids": [],
        }
        for stratum in REPLAY_SET_STRATA
    }
    entries: list[dict[str, Any]] = []
    total_truth_objects = 0
    total_prediction_detections = 0

    for entry in manifest.entries:
        truth_path = entry.ground_truth.resolve(
            source.parent, name=f"{entry.entry_id} ground_truth"
        )
        truth = load_ground_truth_jsonl(truth_path)
        truth_labels = Counter(item.label for frame in truth for item in frame.objects)
        truth_count = sum(truth_labels.values())
        total_truth_objects += truth_count

        prediction_path = None
        predictions = ()
        prediction_labels: Counter[str] = Counter()
        if entry.predictions is not None:
            prediction_path = entry.predictions.resolve(
                source.parent, name=f"{entry.entry_id} predictions"
            )
            predictions = load_prediction_jsonl(prediction_path)
            prediction_labels = Counter(
                item.label for frame in predictions for item in frame.detections
            )
        prediction_count = sum(prediction_labels.values())
        total_prediction_detections += prediction_count

        entry_checks = {
            "deployment_recording_source": entry.source_kind == "deployment_recording",
            "annotations_reviewed": entry.annotations_reviewed,
            "ground_truth_present": True,
            "prediction_stream_present": entry.predictions is not None,
            "positive_truth_evidence": truth_count > 0,
            "positive_prediction_evidence": prediction_count > 0,
        }
        entries.append(
            {
                "entry_id": entry.entry_id,
                "strata": list(entry.strata),
                "source_kind": entry.source_kind,
                "annotations_reviewed": entry.annotations_reviewed,
                "frame_count": entry.frame_count,
                "ground_truth": {
                    "path": entry.ground_truth.path,
                    "frame_count": len(truth),
                    "positive_object_count": truth_count,
                    "label_counts": dict(sorted(truth_labels.items())),
                },
                "predictions": (
                    {
                        "path": entry.predictions.path,
                        "frame_count": len(predictions),
                        "detection_count": prediction_count,
                        "label_counts": dict(sorted(prediction_labels.items())),
                    }
                    if entry.predictions is not None
                    else None
                ),
                "checks": entry_checks,
            }
        )
        for stratum in entry.strata:
            stats = stratum_stats.setdefault(
                stratum,
                {
                    "frame_count": 0,
                    "positive_truth_object_count": 0,
                    "prediction_detection_count": 0,
                    "truth_labels": {},
                    "prediction_labels": {},
                    "entry_ids": [],
                },
            )
            stats["frame_count"] += len(truth)
            stats["positive_truth_object_count"] += truth_count
            stats["prediction_detection_count"] += prediction_count
            stats["entry_ids"].append(entry.entry_id)
            truth_counter = Counter(stats["truth_labels"])
            truth_counter.update(truth_labels)
            stats["truth_labels"] = dict(sorted(truth_counter.items()))
            prediction_counter = Counter(stats["prediction_labels"])
            prediction_counter.update(prediction_labels)
            stats["prediction_labels"] = dict(sorted(prediction_counter.items()))

    covered_strata = {stratum for item in manifest.entries for stratum in item.strata}
    missing_strata = tuple(
        stratum for stratum in REPLAY_SET_STRATA if stratum not in covered_strata
    )
    required_strata_ok = (
        set(manifest.required_strata) == set(REPLAY_SET_STRATA)
        and not missing_strata
        and bool(manifest.entries)
    )
    source_ok = bool(entries) and all(
        item["checks"]["deployment_recording_source"] for item in entries
    )
    reviewed_ok = bool(entries) and all(
        item["checks"]["annotations_reviewed"] for item in entries
    )
    ground_truth_ok = bool(entries) and all(
        item["checks"]["ground_truth_present"] for item in entries
    )
    predictions_ok = bool(entries) and all(
        item["checks"]["prediction_stream_present"] for item in entries
    )
    positive_truth_ok = total_truth_objects > 0
    positive_predictions_ok = total_prediction_detections > 0
    positive_strata_truth_ok = all(
        stratum_stats[stratum]["positive_truth_object_count"] > 0
        for stratum in REPLAY_SET_POSITIVE_STRATA
    )
    positive_strata_predictions_ok = all(
        stratum_stats[stratum]["prediction_detection_count"] > 0
        for stratum in REPLAY_SET_POSITIVE_STRATA
    )
    manifest_gate_passed = bool(
        required_strata_ok and source_ok and reviewed_ok and ground_truth_ok
    )

    accuracy_report = _inspect_accuracy_report(
        source,
        manifest_sha256=manifest_sha256,
        canonical_sha256=manifest.canonical_hash,
        accuracy_report_path=accuracy_report_path,
    )
    accuracy_gate_passed = bool(accuracy_report["gate_passed"])
    checks = {
        "required_strata": required_strata_ok,
        "deployment_recording_source": source_ok,
        "annotations_reviewed": reviewed_ok,
        "ground_truth_complete": ground_truth_ok,
        "predictions_complete": predictions_ok,
        "positive_truth_evidence": positive_truth_ok,
        "positive_prediction_evidence": positive_predictions_ok,
        "positive_truth_by_stratum": positive_strata_truth_ok,
        "positive_prediction_by_stratum": positive_strata_predictions_ok,
        "manifest_gate_passed": manifest_gate_passed,
        "accuracy_report_bound": bool(accuracy_report["manifest_bound"]
                                      and accuracy_report["canonical_bound"]),
        "accuracy_report_gate_passed": accuracy_gate_passed,
    }

    blocking_reasons: list[str] = []
    if not required_strata_ok:
        missing = ", ".join(missing_strata) or "required_strata mismatch"
        blocking_reasons.append(f"required deployment strata are incomplete: {missing}")
    if not source_ok:
        blocking_reasons.extend(
            f"entry {item['entry_id']} source_kind={item['source_kind']!r}; "
            "expected 'deployment_recording'"
            for item in entries
            if not item["checks"]["deployment_recording_source"]
        )
    if not reviewed_ok:
        blocking_reasons.extend(
            f"entry {item['entry_id']} annotations_reviewed=false"
            for item in entries
            if not item["checks"]["annotations_reviewed"]
        )
    if not ground_truth_ok:
        blocking_reasons.append("no verified ground-truth stream is present")
    if not predictions_ok:
        blocking_reasons.extend(
            f"entry {item['entry_id']} predictions.jsonl is missing"
            for item in entries
            if not item["checks"]["prediction_stream_present"]
        )
    if not positive_truth_ok:
        blocking_reasons.append(
            "ground-truth contains no positive detector object; accuracy denominators are undefined"
        )
    if not positive_predictions_ok:
        blocking_reasons.append(
            "predictions contain no detector positive; run the model on at least one positive frame"
        )
    if not positive_strata_truth_ok:
        missing_positive_truth = ", ".join(
            stratum
            for stratum in REPLAY_SET_POSITIVE_STRATA
            if stratum_stats[stratum]["positive_truth_object_count"] <= 0
        )
        blocking_reasons.append(
            "positive ground-truth coverage is missing for deployment strata: "
            f"{missing_positive_truth}"
        )
    if not positive_strata_predictions_ok:
        missing_positive_predictions = ", ".join(
            stratum
            for stratum in REPLAY_SET_POSITIVE_STRATA
            if stratum_stats[stratum]["prediction_detection_count"] <= 0
        )
        blocking_reasons.append(
            "positive detector prediction coverage is missing for deployment strata: "
            f"{missing_positive_predictions}"
        )
    blocking_reasons.extend(accuracy_report["failure_reasons"])
    blocking_reasons = _unique(blocking_reasons)

    return {
        "event": "replay_set_readiness_checked",
        "schema_version": 1,
        "manifest_path": str(source),
        "manifest_sha256": manifest_sha256,
        "canonical_sha256": manifest.canonical_hash,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "required_strata": list(manifest.required_strata),
        "missing_strata": list(missing_strata),
        "entries": entries,
        "strata": {
            key: {
                **value,
                "entry_ids": list(value["entry_ids"]),
            }
            for key, value in sorted(stratum_stats.items())
        },
        "checks": checks,
        "manifest_gate_passed": manifest_gate_passed,
        "accuracy_gate_passed": accuracy_gate_passed,
        "accuracy_report": accuracy_report,
        "blocking_reasons": blocking_reasons,
        "next_actions": _next_actions(
            checks,
            missing_strata=missing_strata,
            accuracy_report_path=accuracy_report_path,
        ),
        "gate_passed": bool(manifest_gate_passed and accuracy_gate_passed),
    }


def _inspect_accuracy_report(
    manifest_path: Path,
    *,
    manifest_sha256: str,
    canonical_sha256: str,
    accuracy_report_path: str | Path | None,
) -> dict[str, Any]:
    """Validate and recompute an optional evaluator report."""

    result: dict[str, Any] = {
        "present": accuracy_report_path is not None,
        "path": None,
        "sha256": None,
        "manifest_bound": False,
        "canonical_bound": False,
        "recomputed": False,
        "reported_gate_passed": False,
        "recomputed_gate_passed": False,
        "gate_passed": False,
        "failure_reasons": [],
    }
    reasons: list[str] = result["failure_reasons"]
    if accuracy_report_path is None:
        reasons.append("accuracy report was not supplied")
        return result

    requested_report = Path(accuracy_report_path).expanduser()
    try:
        if requested_report.is_symlink():
            result["path"] = str(requested_report)
            reasons.append("accuracy report path must not be a symbolic link")
            return result
        report_source = requested_report.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        reasons.append(f"accuracy report cannot be resolved: {exc}")
        return result
    result["path"] = str(report_source)
    try:
        payload = report_source.read_bytes()
        document = _strict_json_loads(payload, name="accuracy report")
    except OSError as exc:
        reasons.append(f"accuracy report is not valid UTF-8 JSON: {exc}")
        return result
    except AutonomyContractError as exc:
        reasons.append(str(exc))
        return result
    result["sha256"] = hashlib.sha256(payload).hexdigest()
    if not isinstance(document, Mapping):
        reasons.append("accuracy report root must be a JSON object")
        return result
    if document.get("event") != "replay_set_accuracy_evaluation_completed":
        reasons.append("accuracy report has the wrong event type")
    if document.get("schema_version") != 1:
        reasons.append("accuracy report schema_version must be 1")
    report_manifest_sha = document.get("manifest_sha256")
    report_canonical_sha = document.get("canonical_sha256")
    result["manifest_bound"] = (
        isinstance(report_manifest_sha, str)
        and report_manifest_sha.lower() == manifest_sha256.lower()
    )
    result["canonical_bound"] = (
        isinstance(report_canonical_sha, str)
        and report_canonical_sha.lower() == canonical_sha256.lower()
    )
    if not result["manifest_bound"]:
        reasons.append("accuracy report manifest SHA-256 does not match the replay-set")
    if not result["canonical_bound"]:
        reasons.append("accuracy report canonical SHA-256 does not match the replay-set")

    for field in _ACCURACY_REPORT_FIELDS:
        if document.get(field) is not True:
            reasons.append(f"accuracy report {field} is not true")
    if document.get("failure_reasons") != []:
        reasons.append("accuracy report contains failure_reasons")
    if not accuracy_metrics_are_defined(document.get("metrics")):
        reasons.append("accuracy report aggregate precision/recall are undefined")

    thresholds = document.get("thresholds")
    reviewer = document.get("reviewer")
    model_binding = document.get("model_binding")
    model_artifact_sha256 = None
    model_id = None
    model_version = None
    if model_binding is not None:
        if not isinstance(model_binding, Mapping):
            reasons.append("accuracy report model_binding must be an object or null")
        else:
            model_artifact_sha256 = model_binding.get("artifact_sha256")
            model_id = model_binding.get("model_id")
            model_version = model_binding.get("model_version")
    if not isinstance(thresholds, Mapping) or any(
        field not in thresholds for field in _THRESHOLD_FIELDS
    ):
        reasons.append("accuracy report thresholds are incomplete")
        return result
    if type(document.get("reviewed")) is not bool:
        reasons.append("accuracy report reviewed must be a boolean")
        return result

    try:
        recomputed = evaluate_replay_set(
            manifest_path,
            iou_threshold=thresholds["iou_threshold"],
            confidence_threshold=thresholds["confidence_threshold"],
            minimum_precision=thresholds["minimum_precision"],
            minimum_recall=thresholds["minimum_recall"],
            maximum_false_alarm_frame_rate=thresholds[
                "maximum_false_alarm_frame_rate"
            ],
            maximum_missed_detection_frame_rate=thresholds[
                "maximum_missed_detection_frame_rate"
            ],
            maximum_latency_p95_ms=thresholds["maximum_latency_p95_ms"],
            reviewed=document["reviewed"],
            reviewer=reviewer,
            model_artifact_sha256=model_artifact_sha256,
            model_id=model_id,
            model_version=model_version,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        reasons.append(f"accuracy report could not be recomputed: {exc}")
        return result

    result["recomputed"] = True
    result["recomputed_gate_passed"] = recomputed["gate_passed"] is True
    result["reported_gate_passed"] = (
        document.get("gate_passed") is True and document.get("accuracy_validated") is True
    )
    comparable_fields = (
        "dataset_id",
        "dataset_version",
        "required_strata",
        "coverage",
        "structurally_valid",
        "deployment_domain_evidence_complete",
        "predictions_complete",
        "metrics_evaluated",
        "metrics_gate_passed",
        "positive_strata",
        "positive_stratum_coverage",
        "positive_strata_coverage_passed",
        "reviewed",
        "reviewer",
        "model_binding",
        "thresholds",
        "entries",
        "metrics",
        "failure_reasons",
        "accuracy_validated",
        "gate_passed",
    )
    if any(document.get(field) != recomputed.get(field) for field in comparable_fields):
        reasons.append("accuracy report does not match a fresh evaluator recomputation")
    if not result["recomputed_gate_passed"]:
        reasons.extend(
            f"recomputed accuracy: {reason}" for reason in recomputed["failure_reasons"]
        )
    result["gate_passed"] = bool(
        result["manifest_bound"]
        and result["canonical_bound"]
        and result["reported_gate_passed"]
        and result["recomputed_gate_passed"]
        and not reasons
    )
    result["failure_reasons"] = _unique(reasons)
    return result


def _strict_json_loads(payload: bytes, *, name: str) -> object:
    """Decode hash-bound evaluator evidence without JSON ambiguity."""

    def reject_constant(value: str) -> None:
        raise AutonomyContractError(f"{name} contains non-finite JSON number {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AutonomyContractError(f"{name} contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutonomyContractError(f"{name} is not valid UTF-8 JSON: {exc}") from exc


def _next_actions(
    checks: Mapping[str, bool],
    *,
    missing_strata: tuple[str, ...],
    accuracy_report_path: str | Path | None,
) -> list[str]:
    actions: list[str] = []
    if not checks["required_strata"]:
        strata = ", ".join(missing_strata) or "day/night/haze/reflection/smoke_only/no_fire"
        actions.append(f"collect and add the missing deployment strata: {strata}")
    if not checks["deployment_recording_source"]:
        actions.append(
            "replace SIM/lab entries with real deployment recordings and regenerate the manifest"
        )
    if not checks["annotations_reviewed"]:
        actions.append(
            "complete independent annotation review, then regenerate with --annotations-reviewed"
        )
    if not checks["predictions_complete"]:
        actions.append(
            "run the pinned detector on every recorded frame and write predictions.jsonl"
        )
    if not checks["positive_truth_evidence"]:
        actions.append("add at least one genuine positive ground-truth object")
    if not checks["positive_prediction_evidence"]:
        actions.append(
            "verify the detector emits at least one positive prediction on a positive frame"
        )
    if not checks["positive_truth_by_stratum"]:
        actions.append(
            "add independently reviewed positive ground truth in every positive deployment stratum"
        )
    if not checks["positive_prediction_by_stratum"]:
        actions.append(
            "run the pinned detector and retain a positive prediction in every "
            "positive deployment stratum"
        )
    if not checks["accuracy_report_bound"] or not checks["accuracy_report_gate_passed"]:
        if accuracy_report_path is None:
            actions.append(
                "run scripts/evaluate_replay_set.py with thresholds and "
                "--reviewed --reviewer after collection"
            )
        else:
            actions.append(
                "fix the pending accuracy report, then rerun the evaluator and readiness check"
            )
    return _unique(actions)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


__all__ = ["check_replay_set_readiness"]
