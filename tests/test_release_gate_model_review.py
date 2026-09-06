from __future__ import annotations

import hashlib
import json
from pathlib import Path

from multidetect.model_manifest import (
    create_candidate_model_manifest,
    write_candidate_model_manifest,
)
from multidetect.model_review import prepare_model_review_bundle
from multidetect.release_gate import PARITY_FIELDS, evaluate_release_gate
from multidetect.release_gate_manifest import build_release_gate_manifest
from multidetect.replay_set import REPLAY_SET_POSITIVE_STRATA

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_MANIFEST = REPO_ROOT / "configs" / "product-version.json"


def _write_mode_artifact(path: Path, mode: str) -> str:
    path.write_text(
        json.dumps(
            {
                "event": f"{mode}_acceptance_finished",
                "gate_passed": True,
                "runtime_mode": mode,
            }
        ),
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _four_mode_evidence(tmp_path: Path) -> dict[str, Path]:
    return {mode: tmp_path / f"{mode}.json" for mode in ("replay", "sim", "hil", "real")}


def _build_modes(
    tmp_path: Path,
) -> tuple[dict[str, Path], dict[str, dict[str, str]], dict[str, str]]:
    evidence = _four_mode_evidence(tmp_path)
    statuses: dict[str, dict[str, str]] = {}
    for mode, path in evidence.items():
        _write_mode_artifact(path, mode)
        if mode in {"hil", "real"}:
            statuses[mode] = {
                "operator_visual_acceptance": "passed",
                "mission_path_acceptance": "passed",
                "hardware_acceptance": "passed",
                "real_control_acceptance": "passed",
            }
        else:
            statuses[mode] = {
                "operator_visual_acceptance": "passed",
                "mission_path_acceptance": "passed",
                "hardware_acceptance": "not_required",
                "real_control_acceptance": "not_required",
            }
    parity = {field: chr(97 + index) * 64 for index, field in enumerate(PARITY_FIELDS)}
    return evidence, statuses, parity


def _model_review_bundle(tmp_path: Path, *, pending: bool = False) -> Path:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"release-gate-model")
    manifest = write_candidate_model_manifest(
        tmp_path / "fire.manifest.json",
        create_candidate_model_manifest(
            model,
            model_id="fire-candidate",
            model_version="candidate-v1",
            class_names=("fire", "smoke"),
            input_width=640,
            input_height=640,
            output_coordinates="normalized_xyxy",
            source_description="release gate model review fixture",
        ),
    )
    bundle_path = tmp_path / "model-review.json"
    if pending:
        prepare_model_review_bundle(manifest, model, bundle_path)
        return bundle_path
    model_check = tmp_path / "model-check.json"
    model_check.write_text(
        json.dumps(
            {
                "event": "onnx_model_validated",
                "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
                "post_nms_output_contract": "Nx6",
                "class_names": ["fire", "smoke"],
                "manifest_validated": True,
                "manifest_status": "quarantined",
                "model_role": "fire_candidate",
                "production_approved": False,
                "synthetic_hil_only": False,
                "hardware_control_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
    accuracy = tmp_path / "accuracy.json"
    accuracy.write_text(
        json.dumps(
            {
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
                    "artifact_sha256": model_sha,
                    "model_id": "fire-candidate",
                    "model_version": "candidate-v1",
                },
            }
        ),
        encoding="utf-8",
    )
    review_decisions = {
        "provenance": "passed",
        "dataset_rights": "passed",
        "security": "passed",
        "quality": "passed",
    }
    prepare_model_review_bundle(
        manifest,
        model,
        bundle_path,
        model_check_report_path=model_check,
        accuracy_report_path=accuracy,
        review_decisions=review_decisions,
        reviewer="qa-team",
        reviewed_at_utc="2026-09-04T02:00:00Z",
    )
    return bundle_path


def test_release_gate_manifest_binds_and_revalidates_model_review(tmp_path: Path) -> None:
    evidence, statuses, parity = _build_modes(tmp_path)
    bundle = _model_review_bundle(tmp_path)
    output = tmp_path / "release-gate.json"

    document = build_release_gate_manifest(
        evidence,
        parity=parity,
        statuses=statuses,
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        model_review_bundle=bundle,
    )

    assert document["model_review"]["gate_passed"] is True
    assert document["model_review"]["production_approval_eligible"] is True
    output.write_text(json.dumps(document), encoding="utf-8")
    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert report["model_review"]["passed"] is True
    assert report["gate_passed"] is True
    assert report["release_allowed"] is False

    bundle.write_text(bundle.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    tampered = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)
    assert tampered["model_review"]["passed"] is False
    assert any("SHA-256" in reason for reason in tampered["model_review"]["reasons"])


def test_release_gate_manifest_keeps_pending_model_review_closed(tmp_path: Path) -> None:
    evidence, statuses, parity = _build_modes(tmp_path)
    bundle = _model_review_bundle(tmp_path, pending=True)
    output = tmp_path / "release-gate-pending.json"

    document = build_release_gate_manifest(
        evidence,
        parity=parity,
        statuses=statuses,
        output_path=output,
        version_manifest_path=VERSION_MANIFEST,
        model_review_bundle=bundle,
    )
    output.write_text(json.dumps(document), encoding="utf-8")

    report = evaluate_release_gate(output, version_manifest_path=VERSION_MANIFEST)

    assert document["model_review"]["gate_passed"] is False
    assert report["model_review"]["passed"] is False
    assert report["gate_passed"] is False
    assert any("gate_passed is false" in reason for reason in report["model_review"]["reasons"])
