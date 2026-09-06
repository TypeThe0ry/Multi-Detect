from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from multidetect.model_manifest import (
    create_candidate_model_manifest,
    write_candidate_model_manifest,
)
from multidetect.model_review import prepare_model_review_bundle, verify_model_review_bundle
from multidetect.replay_set import REPLAY_SET_POSITIVE_STRATA


def _candidate(tmp_path: Path, *, synthetic: bool = False) -> tuple[Path, Path]:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"reviewable-fire-model")
    document = create_candidate_model_manifest(
        model,
        model_id="fire-candidate",
        model_version="candidate-v1",
        class_names=("fire", "smoke"),
        input_width=640,
        input_height=640,
        output_coordinates="normalized_xyxy",
        source_description="independently exported fire/smoke candidate",
    )
    if synthetic:
        document["status"] = "synthetic_hil"
        document["synthetic_hil_only"] = True
    manifest = write_candidate_model_manifest(tmp_path / "fire.manifest.json", document)
    return model, manifest


def _model_check(model: Path, *, synthetic: bool = False) -> dict[str, object]:
    return {
        "event": "onnx_model_validated",
        "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "post_nms_output_contract": "Nx6",
        "class_names": ["fire", "smoke"],
        "manifest_validated": True,
        "manifest_status": "synthetic_hil" if synthetic else "quarantined",
        "model_role": "fire_candidate",
        "production_approved": False,
        "synthetic_hil_only": synthetic,
        "hardware_control_enabled": False,
    }


def _accuracy(model: Path) -> dict[str, object]:
    model_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()
    return {
        "event": "replay_set_accuracy_evaluation_completed",
        "schema_version": 1,
        "deployment_domain_evidence_complete": True,
        "predictions_complete": True,
        "metrics_evaluated": True,
        "metrics_gate_passed": True,
        "positive_strata_coverage_passed": True,
        "reviewed": True,
        "accuracy_validated": True,
        "gate_passed": True,
        "failure_reasons": [],
        "metrics": {"overall": {"precision": 0.99, "recall": 0.98}},
        "positive_strata": list(REPLAY_SET_POSITIVE_STRATA),
        "positive_stratum_coverage": {
            stratum: {
                "positive_truth_object_count": 10,
                "positive_prediction_count": 10,
            }
            for stratum in REPLAY_SET_POSITIVE_STRATA
        },
        "manifest_sha256": "a" * 64,
        "canonical_sha256": "b" * 64,
        "reviewer": "qa-team",
        "thresholds": {
            "iou_threshold": 0.5,
            "confidence_threshold": 0.25,
            "minimum_precision": None,
            "minimum_recall": None,
            "maximum_false_alarm_frame_rate": None,
            "maximum_missed_detection_frame_rate": None,
            "maximum_latency_p95_ms": None,
        },
        "model_binding": {
            "artifact_sha256": model_sha256,
            "model_id": "fire-candidate",
            "model_version": "candidate-v1",
        },
    }


def _write_json(path: Path, document: dict[str, object]) -> Path:
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return path


def _passed_reviews() -> dict[str, str]:
    return {
        "provenance": "passed",
        "dataset_rights": "passed",
        "security": "passed",
        "quality": "passed",
    }


def test_model_review_bundle_is_pending_without_review_or_runtime_evidence(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path)
    output = tmp_path / "review.json"

    bundle = prepare_model_review_bundle(manifest, model, output)

    assert bundle.production_approval_eligible is False
    assert bundle.document["production_approved"] is False
    assert bundle.document["flight_control_enabled"] is False
    assert bundle.document["physical_release_enabled"] is False
    assert bundle.document["review_status"] == "pending"
    assert any(
        "provenance review is pending" in reason for reason in bundle.document["failure_reasons"]
    )
    assert any("model-check evidence" in reason for reason in bundle.document["failure_reasons"])
    assert output.is_file()


def test_model_review_bundle_requires_exact_bound_reports_before_eligibility(
    tmp_path: Path,
) -> None:
    model, manifest = _candidate(tmp_path)
    model_check = _write_json(tmp_path / "model-check.json", _model_check(model))
    accuracy = _write_json(tmp_path / "accuracy.json", _accuracy(model))
    original_manifest = manifest.read_bytes()
    output = tmp_path / "review.json"

    bundle = prepare_model_review_bundle(
        manifest,
        model,
        output,
        model_check_report_path=model_check,
        accuracy_report_path=accuracy,
        review_decisions=_passed_reviews(),
        reviewer="qa-team",
        reviewed_at_utc="2026-09-04T02:00:00Z",
    )

    assert bundle.production_approval_eligible is True
    assert bundle.document["review_status"] == "reviewed"
    assert bundle.document["failure_reasons"] == []
    assert bundle.document["production_approved"] is False
    assert manifest.read_bytes() == original_manifest
    assert (
        bundle.document["model"]["artifact_sha256"]
        == hashlib.sha256(model.read_bytes()).hexdigest()
    )


def test_model_review_bundle_keeps_synthetic_hil_closed(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path, synthetic=True)
    model_check = _write_json(tmp_path / "model-check.json", _model_check(model, synthetic=True))
    accuracy = _write_json(tmp_path / "accuracy.json", _accuracy(model))

    bundle = prepare_model_review_bundle(
        manifest,
        model,
        tmp_path / "review.json",
        model_check_report_path=model_check,
        accuracy_report_path=accuracy,
        review_decisions=_passed_reviews(),
        reviewer="qa-team",
        reviewed_at_utc="2026-09-04T02:00:00Z",
    )

    assert bundle.production_approval_eligible is False
    assert any("synthetic HIL" in reason for reason in bundle.document["failure_reasons"])


def test_model_review_bundle_reopens_synthetic_hil_as_closed_pending_evidence(
    tmp_path: Path,
) -> None:
    """Synthetic diagnostics remain structurally bindable but never eligible."""

    model, manifest = _candidate(tmp_path, synthetic=True)
    output = tmp_path / "review.json"
    prepare_model_review_bundle(manifest, model, output)

    verified = verify_model_review_bundle(output)

    assert verified["passed"] is True
    assert verified["production_approval_eligible"] is False
    assert verified["reasons"] == []


def test_model_review_bundle_rejects_model_check_hash_mismatch(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path)
    check = _model_check(model)
    check["model_sha256"] = "0" * 64
    model_check = _write_json(tmp_path / "model-check.json", check)
    accuracy = _write_json(tmp_path / "accuracy.json", _accuracy(model))

    bundle = prepare_model_review_bundle(
        manifest,
        model,
        tmp_path / "review.json",
        model_check_report_path=model_check,
        accuracy_report_path=accuracy,
        review_decisions=_passed_reviews(),
        reviewer="qa-team",
    )

    assert bundle.production_approval_eligible is False
    assert any(
        "model-check model_sha256" in reason for reason in bundle.document["failure_reasons"]
    )


def test_model_review_bundle_requires_reviewer_for_passed_decision(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path)

    with pytest.raises(ValueError, match="reviewer is required"):
        prepare_model_review_bundle(
            manifest,
            model,
            tmp_path / "review.json",
            review_decisions=_passed_reviews(),
        )


def test_model_review_bundle_reopens_and_recomputes_bound_evidence(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path)
    model_check = _write_json(tmp_path / "model-check.json", _model_check(model))
    accuracy = _write_json(tmp_path / "accuracy.json", _accuracy(model))
    output = tmp_path / "review.json"
    prepare_model_review_bundle(
        manifest,
        model,
        output,
        model_check_report_path=model_check,
        accuracy_report_path=accuracy,
        review_decisions=_passed_reviews(),
        reviewer="qa-team",
        reviewed_at_utc="2026-09-04T02:00:00Z",
    )

    verified = verify_model_review_bundle(output, require_eligible=True)

    assert verified["passed"] is True
    assert verified["production_approval_eligible"] is True
    assert verified["reasons"] == []


def test_model_review_bundle_reopen_detects_tampered_source(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path)
    model_check = _write_json(tmp_path / "model-check.json", _model_check(model))
    accuracy = _write_json(tmp_path / "accuracy.json", _accuracy(model))
    output = tmp_path / "review.json"
    prepare_model_review_bundle(
        manifest,
        model,
        output,
        model_check_report_path=model_check,
        accuracy_report_path=accuracy,
        review_decisions=_passed_reviews(),
        reviewer="qa-team",
        reviewed_at_utc="2026-09-04T02:00:00Z",
    )
    model_check.write_text(model_check.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    verified = verify_model_review_bundle(output, require_eligible=True)

    assert verified["passed"] is False
    assert any("SHA-256" in reason or "recomputed" in reason for reason in verified["reasons"])


def test_model_review_bundle_reopen_rejects_pending_bundle_when_required(tmp_path: Path) -> None:
    model, manifest = _candidate(tmp_path)
    output = tmp_path / "review.json"
    prepare_model_review_bundle(manifest, model, output)

    verified = verify_model_review_bundle(output, require_eligible=True)

    assert verified["passed"] is False
    assert verified["production_approval_eligible"] is False
    assert any("not production-approval eligible" in reason for reason in verified["reasons"])


def test_model_review_schema_keeps_control_and_approval_closed() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1] / "configs/schemas/model-review-bundle.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["additionalProperties"] is False
    assert schema["properties"]["production_approved"]["const"] is False
    assert schema["properties"]["flight_control_enabled"]["const"] is False
    assert schema["properties"]["physical_release_enabled"]["const"] is False
    assert schema["properties"]["model"]["$ref"] == "#/$defs/model"
