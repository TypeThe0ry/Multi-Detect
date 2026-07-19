from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModelManifestError(ValueError):
    """Raised when a model artifact is not bound to a valid manifest."""


PINNED_LEGACY_CHECKPOINT_SIZE_BYTES = 14_758_954
PINNED_LEGACY_CHECKPOINT_SHA256 = "d1eae6859229ac1f5699c60f9445fa054dafc6a2cc59f00fc30ea6379dc3247e"


@dataclass(frozen=True, slots=True)
class VerifiedModelArtifact:
    model_id: str
    model_version: str
    artifact_sha256: str
    status: str
    production_approved: bool
    class_names: tuple[str, ...]
    intended_use: str
    output_coordinates: str | None
    output_format: str
    synthetic_hil_only: bool
    model_role: str


@dataclass(frozen=True, slots=True)
class CheckpointByteVerification:
    path: Path
    actual_size_bytes: int
    actual_sha256: str
    expected_size_bytes: int
    expected_sha256: str
    size_matches: bool
    sha256_matches: bool

    @property
    def matches(self) -> bool:
        return self.size_matches and self.sha256_matches


def verify_checkpoint_bytes(
    path: str | Path,
    *,
    expected_size_bytes: int,
    expected_sha256: str,
) -> CheckpointByteVerification:
    checkpoint = Path(path)
    if not checkpoint.is_file():
        raise ModelManifestError(f"checkpoint file does not exist: {checkpoint}")
    if expected_size_bytes <= 0:
        raise ModelManifestError("expected checkpoint size must be positive")
    normalized_digest = expected_sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_digest):
        raise ModelManifestError("expected checkpoint SHA-256 must be 64 hex characters")
    actual_size = checkpoint.stat().st_size
    actual_digest = sha256_file(checkpoint)
    return CheckpointByteVerification(
        path=checkpoint,
        actual_size_bytes=actual_size,
        actual_sha256=actual_digest,
        expected_size_bytes=expected_size_bytes,
        expected_sha256=normalized_digest,
        size_matches=actual_size == expected_size_bytes,
        sha256_matches=actual_digest == normalized_digest,
    )


def create_candidate_model_manifest(
    model_path: str | Path,
    *,
    model_id: str,
    model_version: str,
    class_names: tuple[str, ...],
    input_width: int,
    input_height: int,
    output_coordinates: str,
    source_description: str,
    model_role: str = "fire_candidate",
    native_output_format: str = "post_nms_N_x_6",
) -> dict[str, Any]:
    artifact = Path(model_path)
    if not artifact.is_file():
        raise ModelManifestError(f"model artifact does not exist: {artifact}")
    if not model_id.strip() or not model_version.strip() or not source_description.strip():
        raise ModelManifestError("model ID, version and source description are required")
    if not class_names or any(not name.strip() for name in class_names):
        raise ModelManifestError("at least one non-empty class name is required")
    if input_width <= 0 or input_height <= 0:
        raise ModelManifestError("model input dimensions must be positive")
    if output_coordinates not in {"normalized_xyxy", "letterbox_xyxy_px"}:
        raise ModelManifestError("model output coordinates are unsupported")
    if native_output_format not in {
        "post_nms_N_x_6",
        "ultralytics_raw_xywh_class_scores",
    }:
        raise ModelManifestError("model native output format is unsupported")
    intended_use_by_role = {
        "fire_candidate": "rgb_detection_candidate_generation_only",
        "fire_verifier": "independent_rgb_fire_corroboration_only",
        "safety_object_evidence": "rgb_safety_object_evidence_generation_only",
        "environment_risk_evidence": "rgb_environment_risk_evidence_generation_only",
    }
    if model_role not in intended_use_by_role:
        raise ModelManifestError("model role is unsupported")
    adapter_contract: dict[str, Any] = {
        "format": "N_x_6",
        "fields": ["x1", "y1", "x2", "y2", "confidence", "class_id"],
        "box_format": output_coordinates,
    }
    if output_coordinates == "normalized_xyxy":
        adapter_contract["box_range"] = [0.0, 1.0]
    return {
        "schema_version": 1,
        "model_id": model_id.strip(),
        "model_version": model_version.strip(),
        "status": "quarantined",
        "model_role": model_role,
        "intended_use": intended_use_by_role[model_role],
        "prohibited_uses": [
            "payload_release_authorization",
            "direct_payload_release",
            "flight_control",
            "person_safety_clearance",
        ],
        "source": {
            "description": source_description.strip(),
            "provenance_review": "pending",
        },
        "classes": [
            {
                "id": index,
                "source_name": name.strip().lower(),
                "canonical_label": "flame"
                if name.strip().lower() == "fire"
                else name.strip().lower(),
            }
            for index, name in enumerate(class_names)
        ],
        "input": {
            "sensor": "rgb",
            "color_space": "RGB",
            "tensor_layout": "NCHW",
            "dtype": "float32",
            "shape": [1, 3, input_height, input_width],
            "resize": "letterbox",
        },
        "output": {
            "native_export": {
                "format": native_output_format,
                "nms_embedded": native_output_format == "post_nms_N_x_6",
            },
            "adapter_contract": adapter_contract,
        },
        "export": {
            "artifact_format": (
                "tensorrt_engine" if artifact.suffix.lower() in {".engine", ".plan"} else "onnx"
            ),
            "artifact_path": artifact.name,
            "artifact_sha256": sha256_file(artifact),
            "tool_versions": {},
        },
        "validation": {
            "sample_count": 0,
            "deployment_domain_metrics": {},
            "validated_at_utc": None,
        },
        "governance": {
            "software_license_review": "pending",
            "dataset_rights_review": "pending",
            "security_review": "pending",
            "model_quality_review": "pending",
            "production_approved": False,
            "reviewer": None,
            "reviewed_at_utc": None,
        },
    }


def create_semantic_context_model_manifest(
    model_path: str | Path,
    *,
    model_id: str,
    model_version: str,
    class_names: tuple[str, ...],
    input_width: int,
    input_height: int,
    output_name: str,
    source_description: str,
) -> dict[str, Any]:
    artifact = Path(model_path)
    if not artifact.is_file():
        raise ModelManifestError(f"model artifact does not exist: {artifact}")
    if not model_id.strip() or not model_version.strip() or not source_description.strip():
        raise ModelManifestError("model ID, version and source description are required")
    if not class_names or any(not name.strip() for name in class_names):
        raise ModelManifestError("at least one non-empty semantic class name is required")
    if input_width <= 0 or input_height <= 0 or not output_name.strip():
        raise ModelManifestError("semantic model dimensions and output name are required")
    return {
        "schema_version": 1,
        "model_id": model_id.strip(),
        "model_version": model_version.strip(),
        "status": "quarantined",
        "model_role": "semantic_scene_context",
        "intended_use": "rgb_semantic_scene_context_advisory_only",
        "prohibited_uses": [
            "payload_release_authorization",
            "direct_payload_release",
            "flight_control",
            "person_safety_clearance",
        ],
        "source": {
            "description": source_description.strip(),
            "provenance_review": "pending",
        },
        "classes": [
            {
                "id": index,
                "source_name": name.strip().lower(),
                "canonical_label": name.strip().lower(),
            }
            for index, name in enumerate(class_names)
        ],
        "input": {
            "sensor": "rgb",
            "color_space": "RGB",
            "tensor_layout": "NCHW",
            "dtype": "float32",
            "shape": [1, 3, input_height, input_width],
            "resize": "stretch",
        },
        "output": {
            "native_export": {
                "name": output_name.strip(),
                "format": "categorical_class_id_mask",
            },
            "adapter_contract": {
                "format": "categorical_H_W_1",
                "fields": ["class_id"],
                "tensor_layout": "NHWC",
                "shape": [1, input_height, input_width, 1],
                "confidence_available": False,
            },
        },
        "export": {
            "artifact_format": "onnx",
            "artifact_path": artifact.name,
            "artifact_sha256": sha256_file(artifact),
            "tool_versions": {},
        },
        "validation": {
            "sample_count": 0,
            "deployment_domain_metrics": {},
            "validated_at_utc": None,
        },
        "governance": {
            "software_license_review": "pending",
            "dataset_rights_review": "pending",
            "security_review": "pending",
            "model_quality_review": "pending",
            "production_approved": False,
            "reviewer": None,
            "reviewed_at_utc": None,
        },
    }


def write_candidate_model_manifest(
    destination: str | Path,
    document: Mapping[str, Any],
    *,
    overwrite: bool = False,
) -> Path:
    output = Path(destination)
    if output.exists() and not overwrite:
        raise ModelManifestError(f"model manifest already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return output


def verify_model_manifest(
    manifest_path: str | Path,
    model_path: str | Path,
    *,
    expected_class_names: tuple[str, ...] | None = None,
    expected_output_coordinates: str | None = None,
    expected_model_role: str | None = None,
    expected_output_format: str | None = None,
    expected_native_output_format: str | None = None,
    require_production_approved: bool = False,
) -> VerifiedModelArtifact:
    manifest_file = Path(manifest_path)
    artifact_file = Path(model_path)
    if not manifest_file.is_file():
        raise ModelManifestError(f"model manifest does not exist: {manifest_file}")
    if not artifact_file.is_file():
        raise ModelManifestError(f"model artifact does not exist: {artifact_file}")
    with manifest_file.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ModelManifestError("model manifest must be a JSON object")
    if raw.get("schema_version") != 1:
        raise ModelManifestError("unsupported model manifest schema_version")

    model_id = _required_text(raw, "model_id")
    model_version = _required_text(raw, "model_version")
    status = _required_text(raw, "status")
    synthetic_hil_only = raw.get("synthetic_hil_only") is True
    if (status == "synthetic_hil") != synthetic_hil_only:
        raise ModelManifestError(
            "synthetic HIL status and synthetic_hil_only marker must be set together"
        )
    intended_use = _required_text(raw, "intended_use")
    model_role = raw.get("model_role")
    if model_role is None and intended_use == "rgb_detection_candidate_generation_only":
        # Backward-compatible interpretation for schema-v1 fire manifests.
        model_role = "fire_candidate"
    intended_use_by_role = {
        "fire_candidate": "rgb_detection_candidate_generation_only",
        "fire_verifier": "independent_rgb_fire_corroboration_only",
        "safety_object_evidence": "rgb_safety_object_evidence_generation_only",
        "environment_risk_evidence": "rgb_environment_risk_evidence_generation_only",
        "semantic_scene_context": "rgb_semantic_scene_context_advisory_only",
    }
    if model_role not in intended_use_by_role:
        raise ModelManifestError("model manifest role is missing or unsupported")
    if intended_use != intended_use_by_role[model_role]:
        raise ModelManifestError("model intended_use does not match its declared role")
    if expected_model_role is not None and model_role != expected_model_role:
        raise ModelManifestError(
            f"manifest model role {model_role!r} does not match runtime {expected_model_role!r}"
        )

    prohibited_uses = raw.get("prohibited_uses")
    required_prohibitions = {
        "payload_release_authorization",
        "direct_payload_release",
        "flight_control",
        "person_safety_clearance",
    }
    if not isinstance(prohibited_uses, list) or not required_prohibitions.issubset(
        {str(item) for item in prohibited_uses}
    ):
        raise ModelManifestError("model manifest is missing required prohibited uses")

    classes = raw.get("classes")
    if not isinstance(classes, list) or not classes:
        raise ModelManifestError("model manifest classes must be a non-empty array")
    ordered_classes: list[str] = []
    for expected_id, item in enumerate(classes):
        if not isinstance(item, dict) or item.get("id") != expected_id:
            raise ModelManifestError("model class IDs must be contiguous and zero-based")
        ordered_classes.append(_required_text(item, "source_name").strip().lower())
    class_names = tuple(ordered_classes)
    if expected_class_names is not None:
        expected = tuple(name.strip().lower() for name in expected_class_names)
        if class_names != expected:
            raise ModelManifestError(
                f"manifest class order {class_names!r} does not match runtime {expected!r}"
            )

    output = _required_mapping(raw, "output")
    if expected_native_output_format is not None:
        native_export = _required_mapping(output, "native_export")
        native_output_format = _required_text(native_export, "format")
        if native_output_format != expected_native_output_format:
            raise ModelManifestError(
                f"manifest native output format {native_output_format!r} does not match runtime "
                f"{expected_native_output_format!r}"
            )
    adapter = _required_mapping(output, "adapter_contract")
    output_format = _required_text(adapter, "format")
    output_coordinates: str | None = None
    if model_role == "semantic_scene_context":
        if output_format != "categorical_H_W_1":
            raise ModelManifestError("semantic model adapter output must be categorical_H_W_1")
        if adapter.get("fields") != ["class_id"]:
            raise ModelManifestError("semantic model adapter must expose only class_id")
        if (
            adapter.get("tensor_layout") != "NHWC"
            or adapter.get("confidence_available") is not False
        ):
            raise ModelManifestError(
                "semantic model adapter must be NHWC categorical output without confidence"
            )
        if expected_output_coordinates is not None:
            raise ModelManifestError("semantic model output does not have box coordinates")
    else:
        if output_format != "N_x_6":
            raise ModelManifestError("model adapter output must be N_x_6")
        expected_fields = ["x1", "y1", "x2", "y2", "confidence", "class_id"]
        if adapter.get("fields") != expected_fields:
            raise ModelManifestError("model adapter fields do not match the strict Nx6 contract")
        output_coordinates = _required_text(adapter, "box_format")
        if output_coordinates not in {"normalized_xyxy", "letterbox_xyxy_px"}:
            raise ModelManifestError("model adapter box_format is unsupported")
        if output_coordinates == "normalized_xyxy" and adapter.get("box_range") != [0.0, 1.0]:
            raise ModelManifestError("normalized model output requires box_range [0.0, 1.0]")
        if (
            expected_output_coordinates is not None
            and output_coordinates != expected_output_coordinates
        ):
            raise ModelManifestError(
                "manifest output coordinates "
                f"{output_coordinates!r} do not match runtime {expected_output_coordinates!r}"
            )
    if expected_output_format is not None and output_format != expected_output_format:
        raise ModelManifestError(
            f"manifest output format {output_format!r} does not match runtime "
            f"{expected_output_format!r}"
        )

    export = _required_mapping(raw, "export")
    expected_digest = _required_text(export, "artifact_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise ModelManifestError("export.artifact_sha256 must be a 64-character hex digest")
    actual_digest = sha256_file(artifact_file)
    if actual_digest != expected_digest:
        raise ModelManifestError(
            f"model SHA-256 mismatch: expected {expected_digest}, received {actual_digest}"
        )

    governance = _required_mapping(raw, "governance")
    production_approved = governance.get("production_approved") is True
    if synthetic_hil_only and production_approved:
        raise ModelManifestError("a synthetic HIL model can never be production approved")
    if require_production_approved and (not production_approved or status != "approved"):
        raise ModelManifestError("model is not production approved")
    return VerifiedModelArtifact(
        model_id=model_id,
        model_version=model_version,
        artifact_sha256=actual_digest,
        status=status,
        production_approved=production_approved,
        class_names=class_names,
        intended_use=intended_use,
        output_coordinates=output_coordinates,
        output_format=output_format,
        synthetic_hil_only=synthetic_hil_only,
        model_role=model_role,
    )


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _required_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelManifestError(f"model manifest {key} must be a non-empty string")
    return value.strip()


def _required_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ModelManifestError(f"model manifest {key} must be an object")
    return value


__all__ = [
    "CheckpointByteVerification",
    "create_candidate_model_manifest",
    "create_semantic_context_model_manifest",
    "ModelManifestError",
    "PINNED_LEGACY_CHECKPOINT_SHA256",
    "PINNED_LEGACY_CHECKPOINT_SIZE_BYTES",
    "VerifiedModelArtifact",
    "sha256_file",
    "verify_model_manifest",
    "verify_checkpoint_bytes",
    "write_candidate_model_manifest",
]
