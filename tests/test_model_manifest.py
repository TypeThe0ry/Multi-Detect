from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.model_manifest import (
    ModelManifestError,
    create_candidate_model_manifest,
    create_semantic_context_model_manifest,
    sha256_file,
    verify_checkpoint_bytes,
    verify_model_manifest,
    write_candidate_model_manifest,
)


def _manifest(model: Path, *, approved: bool = False) -> dict:
    return {
        "schema_version": 1,
        "model_id": "fire-smoke-test",
        "model_version": "test-v1",
        "status": "approved" if approved else "quarantined",
        "model_role": "fire_candidate",
        "intended_use": "rgb_detection_candidate_generation_only",
        "prohibited_uses": [
            "payload_release_authorization",
            "direct_payload_release",
            "flight_control",
            "person_safety_clearance",
        ],
        "classes": [
            {"id": 0, "source_name": "fire", "canonical_label": "flame"},
            {"id": 1, "source_name": "smoke", "canonical_label": "smoke"},
        ],
        "output": {
            "adapter_contract": {
                "format": "N_x_6",
                "fields": ["x1", "y1", "x2", "y2", "confidence", "class_id"],
                "box_format": "normalized_xyxy",
                "box_range": [0.0, 1.0],
            }
        },
        "export": {"artifact_sha256": sha256_file(model)},
        "governance": {"production_approved": approved},
    }


def _write_manifest(tmp_path: Path, model: Path, *, approved: bool = False) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest(model, approved=approved)), encoding="utf-8")
    return path


def test_manifest_binds_hash_classes_and_intended_use(tmp_path: Path) -> None:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"deterministic-onnx-placeholder")
    manifest = _write_manifest(tmp_path, model)

    verified = verify_model_manifest(
        manifest,
        model,
        expected_class_names=("fire", "smoke"),
        expected_output_coordinates="normalized_xyxy",
    )

    assert verified.artifact_sha256 == sha256_file(model)
    assert verified.model_version == "test-v1"
    assert verified.production_approved is False
    assert verified.output_coordinates == "normalized_xyxy"
    assert verified.model_role == "fire_candidate"


def test_manifest_rejects_artifact_hash_mismatch(tmp_path: Path) -> None:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"original")
    manifest = _write_manifest(tmp_path, model)
    model.write_bytes(b"changed")

    with pytest.raises(ModelManifestError, match="SHA-256 mismatch"):
        verify_model_manifest(manifest, model)


def test_manifest_rejects_runtime_class_order_mismatch(tmp_path: Path) -> None:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, model)

    with pytest.raises(ModelManifestError, match="class order"):
        verify_model_manifest(
            manifest,
            model,
            expected_class_names=("smoke", "fire"),
        )


def test_production_gate_requires_approved_status_and_governance(tmp_path: Path) -> None:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"model")
    candidate = _write_manifest(tmp_path, model, approved=False)

    with pytest.raises(ModelManifestError, match="not production approved"):
        verify_model_manifest(candidate, model, require_production_approved=True)

    approved = _write_manifest(tmp_path, model, approved=True)
    verified = verify_model_manifest(approved, model, require_production_approved=True)
    assert verified.production_approved is True


def test_manifest_rejects_runtime_coordinate_contract_mismatch(tmp_path: Path) -> None:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"model")
    manifest = _write_manifest(tmp_path, model)

    with pytest.raises(ModelManifestError, match="output coordinates"):
        verify_model_manifest(
            manifest,
            model,
            expected_output_coordinates="letterbox_xyxy_px",
        )


def test_candidate_manifest_initializer_binds_artifact_without_approving_it(
    tmp_path: Path,
) -> None:
    model = tmp_path / "fire.onnx"
    model.write_bytes(b"candidate-model")
    document = create_candidate_model_manifest(
        model,
        model_id="fire-smoke-candidate",
        model_version="candidate-v1",
        class_names=("fire", "smoke"),
        input_width=640,
        input_height=640,
        output_coordinates="normalized_xyxy",
        source_description="user-provided isolated ONNX export",
    )
    manifest = write_candidate_model_manifest(tmp_path / "fire.manifest.json", document)

    verified = verify_model_manifest(
        manifest,
        model,
        expected_class_names=("fire", "smoke"),
        expected_output_coordinates="normalized_xyxy",
    )

    assert verified.status == "quarantined"
    assert verified.production_approved is False
    assert document["validation"]["sample_count"] == 0


def test_raw_yolo_manifest_declares_host_postprocessing_and_is_runtime_checked(
    tmp_path: Path,
) -> None:
    engine = tmp_path / "common.engine"
    engine.write_bytes(b"target-built-raw-yolo-engine")
    document = create_candidate_model_manifest(
        engine,
        model_id="common-raw",
        model_version="candidate-v1",
        class_names=("person", "car"),
        input_width=640,
        input_height=640,
        output_coordinates="letterbox_xyxy_px",
        source_description="target-built TensorRT raw YOLO engine",
        model_role="safety_object_evidence",
        native_output_format="ultralytics_raw_xywh_class_scores",
    )
    manifest = write_candidate_model_manifest(tmp_path / "common.manifest.json", document)

    assert document["output"]["native_export"] == {
        "format": "ultralytics_raw_xywh_class_scores",
        "nms_embedded": False,
    }
    assert document["export"]["artifact_format"] == "tensorrt_engine"
    verified = verify_model_manifest(
        manifest,
        engine,
        expected_class_names=("person", "car"),
        expected_model_role="safety_object_evidence",
        expected_native_output_format="ultralytics_raw_xywh_class_scores",
    )
    assert verified.output_format == "N_x_6"

    with pytest.raises(ModelManifestError, match="native output format"):
        verify_model_manifest(
            manifest,
            engine,
            expected_native_output_format="post_nms_N_x_6",
        )


def test_safety_object_manifest_role_is_distinct_from_fire_candidate(tmp_path: Path) -> None:
    model = tmp_path / "person-safety.onnx"
    model.write_bytes(b"safety-object-candidate")
    document = create_candidate_model_manifest(
        model,
        model_id="person-safety-candidate",
        model_version="candidate-v1",
        class_names=("person", "firefighter"),
        input_width=640,
        input_height=640,
        output_coordinates="normalized_xyxy",
        source_description="independently governed safety object model",
        model_role="safety_object_evidence",
    )
    manifest = write_candidate_model_manifest(tmp_path / "safety.manifest.json", document)

    verified = verify_model_manifest(
        manifest,
        model,
        expected_class_names=("person", "firefighter"),
        expected_model_role="safety_object_evidence",
    )

    assert verified.model_role == "safety_object_evidence"
    assert verified.intended_use == "rgb_safety_object_evidence_generation_only"
    with pytest.raises(ModelManifestError, match="model role"):
        verify_model_manifest(
            manifest,
            model,
            expected_model_role="fire_candidate",
        )


def test_independent_rgb_fire_verifier_has_a_distinct_manifest_role(tmp_path: Path) -> None:
    model = tmp_path / "rgb-fire-verifier.onnx"
    model.write_bytes(b"independent-rgb-fire-verifier")
    document = create_candidate_model_manifest(
        model,
        model_id="rgb-fire-verifier",
        model_version="verifier-v1",
        class_names=("fire", "smoke"),
        input_width=640,
        input_height=640,
        output_coordinates="normalized_xyxy",
        source_description="separately trained and governed RGB fire verifier",
        model_role="fire_verifier",
    )
    manifest = write_candidate_model_manifest(tmp_path / "verifier.json", document)

    verified = verify_model_manifest(
        manifest,
        model,
        expected_class_names=("fire", "smoke"),
        expected_model_role="fire_verifier",
    )

    assert verified.model_role == "fire_verifier"
    assert verified.intended_use == "independent_rgb_fire_corroboration_only"
    with pytest.raises(ModelManifestError, match="model role"):
        verify_model_manifest(
            manifest,
            model,
            expected_model_role="fire_candidate",
        )


def test_environment_risk_manifest_role_is_executable_not_just_declared(tmp_path: Path) -> None:
    model = tmp_path / "environment.onnx"
    model.write_bytes(b"environment-risk-candidate")
    document = create_candidate_model_manifest(
        model,
        model_id="environment-risk",
        model_version="candidate-v1",
        class_names=("power_line", "flammable_tank"),
        input_width=640,
        input_height=640,
        output_coordinates="normalized_xyxy",
        source_description="separate environment evidence domain",
        model_role="environment_risk_evidence",
    )
    manifest = write_candidate_model_manifest(tmp_path / "environment.json", document)

    verified = verify_model_manifest(
        manifest,
        model,
        expected_class_names=("power_line", "flammable_tank"),
        expected_model_role="environment_risk_evidence",
        expected_output_format="N_x_6",
    )

    assert verified.model_role == "environment_risk_evidence"
    assert verified.output_format == "N_x_6"
    assert verified.output_coordinates == "normalized_xyxy"


def test_semantic_context_manifest_has_no_box_or_confidence_contract(tmp_path: Path) -> None:
    model = tmp_path / "city.onnx"
    model.write_bytes(b"categorical-semantic-context")
    document = create_semantic_context_model_manifest(
        model,
        model_id="city-semsegformer",
        model_version="deployable-onnx-v1",
        class_names=("road", "building"),
        input_width=1820,
        input_height=1024,
        output_name="output",
        source_description="official categorical scene segmentation",
    )
    manifest = write_candidate_model_manifest(tmp_path / "semantic.json", document)

    verified = verify_model_manifest(
        manifest,
        model,
        expected_class_names=("road", "building"),
        expected_model_role="semantic_scene_context",
        expected_output_format="categorical_H_W_1",
    )

    assert verified.output_coordinates is None
    assert verified.output_format == "categorical_H_W_1"
    assert verified.intended_use == "rgb_semantic_scene_context_advisory_only"

    document["output"]["adapter_contract"]["confidence_available"] = True
    tampered = tmp_path / "semantic-tampered.json"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ModelManifestError, match="without confidence"):
        verify_model_manifest(tampered, model)


def test_manifest_rejects_intended_use_that_does_not_match_role(tmp_path: Path) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    raw = _manifest(model)
    raw["model_role"] = "safety_object_evidence"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ModelManifestError, match="intended_use"):
        verify_model_manifest(manifest, model)


def test_candidate_manifest_writer_does_not_overwrite_without_force(tmp_path: Path) -> None:
    destination = tmp_path / "manifest.json"
    destination.write_text("existing", encoding="utf-8")

    with pytest.raises(ModelManifestError, match="already exists"):
        write_candidate_model_manifest(destination, {"schema_version": 1})


def test_checkpoint_byte_verifier_never_needs_deserialization(tmp_path: Path) -> None:
    checkpoint = tmp_path / "best.pt"
    checkpoint.write_bytes(b"opaque-untrusted-checkpoint-bytes")
    digest = sha256_file(checkpoint)

    verification = verify_checkpoint_bytes(
        checkpoint,
        expected_size_bytes=checkpoint.stat().st_size,
        expected_sha256=digest,
    )

    assert verification.matches is True
    assert verification.actual_sha256 == digest


def test_checkpoint_byte_verifier_reports_mismatch_without_loading(tmp_path: Path) -> None:
    checkpoint = tmp_path / "best.pt"
    checkpoint.write_bytes(b"unexpected")

    verification = verify_checkpoint_bytes(
        checkpoint,
        expected_size_bytes=999,
        expected_sha256="0" * 64,
    )

    assert verification.matches is False
    assert verification.size_matches is False
    assert verification.sha256_matches is False
