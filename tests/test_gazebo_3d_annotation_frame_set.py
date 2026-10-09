from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.build_onboard_rendered_3d_dataset import build_dataset
from scripts.gazebo_3d_annotation_frame_set import (
    AnnotationFrameSetError,
    load_annotation_frame_set,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/scenarios/gazebo-3d-annotation-capture-v1.json"
SCHEMA = ROOT / "configs/schemas/gazebo-3d-annotation-frame-set.schema.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rgb_manifest(tmp_path: Path) -> Path:
    payload = bytes(range(12))
    frame_file = tmp_path / "frame-000000.bgr24"
    frame_file.write_bytes(payload)
    payload_hash = hashlib.sha256(payload).hexdigest()
    document = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "scenario_id": "urban_inspection_multirotor_001",
        "wire_version": 3,
        "manifest_sha256": "a" * 64,
        "scenario_manifest_hash": "b" * 64,
        "sensor_binding_hash": "c" * 64,
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "wire": "MDGZSIM3",
            "per_frame_wire_telemetry_persisted": True,
            "wire_record_recomputed_from_persisted_bytes": True,
        },
        "per_frame_sensor_evidence": {
            "gps_and_pose_values_covered_by_wire_record_sha256": True,
        },
        "repository_model_assets": {"assets_sha256": "d" * 64},
        "terrain_dem": {"file_sha256": "e" * 64},
        "claims": {"actual_gazebo_pixels_persisted": True},
        "frames": [
            {
                "sequence": 7,
                "frame_id": "gazebo-sim-000007",
                "file": frame_file.name,
                "sha256": payload_hash,
                "provenance": {
                    "width": 2,
                    "height": 2,
                    "captured_at_s": 12.5,
                    "frame_telemetry": {
                        "gps_fix_type": 3,
                        "gps_horizontal_accuracy_m": 1.5,
                        "altitude_agl_m": 10.0,
                        "velocity_north_mps": 1.0,
                        "velocity_east_mps": 0.5,
                        "gps_observed_at_s": 12.5,
                        "altitude_observed_at_s": 12.5,
                        "velocity_observed_at_s": 12.5,
                    },
                },
            }
        ],
    }
    path = tmp_path / "rgb.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _annotation_manifest(tmp_path: Path, rgb: Path) -> Path:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    frame = json.loads(rgb.read_text(encoding="utf-8"))["frames"][0]
    document = {
        "schema": "multidetect.gazebo-3d-annotation-frame-set",
        "schema_version": 1,
        "contract": "multidetect.gazebo-3d-annotation-frame-set-v1",
        "source_attribution": {
            "annotation": "actual_gazebo_capture_only_annotation_output",
            "capture_only": True,
            "truth_loaded_by_runtime": False,
            "runtime_consumes_annotation_topics": False,
        },
        "rgb_frame_set": {
            "path": rgb.as_posix(),
            "manifest_sha256": _sha256(rgb),
            "scenario_id": "urban_inspection_multirotor_001",
            "scenario_manifest_hash": "b" * 64,
        },
        "annotation_config": {
            "path": CONFIG.as_posix(),
            "sha256": _sha256(CONFIG),
        },
        "wrapper_binding": {
            "vehicle_wrapper_sha256": config["vehicle_wrapper"]["sha256"],
            "target_wrapper_sha256": config["target_wrapper"]["sha256"],
        },
        "topic_contract": {
            "bounding_box_3d": "multidetect/annotation/bounding_boxes_3d",
            "instance_segmentation": "multidetect/annotation/instance_segmentation",
        },
        "label_contract": {"label_id": 42, "class_name": "search_target"},
        "runtime_boundary": {
            "offline_supervision_only": True,
            "runtime_consumes_annotation_topics": False,
            "truth_injected_into_policy": False,
            "policy_input_contract": "rgb_depth_camera_info_pose_telemetry_only",
            "training_requires_independent_review": True,
        },
        "frames": [
            {
                "sequence": frame["sequence"],
                "frame_id": frame["frame_id"],
                "rgb_payload_sha256": frame["sha256"],
                "captured_at_s": 12.5,
                "bounding_box_3d": {
                    "topic": "multidetect/annotation/bounding_boxes_3d",
                    "label_id": 42,
                    "normalized_bbox": [0.2, 0.25, 0.7, 0.8],
                    "range_3d_m": [1.0, -2.0, 25.0],
                },
                "instance_segmentation": {
                    "topic": "multidetect/annotation/instance_segmentation",
                    "label_id": 42,
                    "labels_map_topic": "multidetect/annotation/instance_segmentation/labels_map",
                    "payload_sha256": "f" * 64,
                    "visible_bbox_normalized": [0.2, 0.25, 0.7, 0.8],
                    "visible_pixels": 10,
                },
            }
        ],
        "training_ready": False,
        "reviewed_annotation_manifest": None,
    }
    path = tmp_path / "annotations.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_schema_and_exact_binding(tmp_path: Path) -> None:
    rgb = _rgb_manifest(tmp_path)
    annotation = _annotation_manifest(tmp_path, rgb)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    document = json.loads(annotation.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    bound = load_annotation_frame_set(annotation, rgb_manifest=rgb)
    assert list(bound) == [(7, "gazebo-sim-000007", document["frames"][0]["rgb_payload_sha256"])]


def test_mismatched_rgb_hash_fails_closed(tmp_path: Path) -> None:
    rgb = _rgb_manifest(tmp_path)
    annotation = _annotation_manifest(tmp_path, rgb)
    document = json.loads(annotation.read_text(encoding="utf-8"))
    document["frames"][0]["rgb_payload_sha256"] = "0" * 64
    annotation.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationFrameSetError, match="exactly bind"):
        load_annotation_frame_set(annotation, rgb_manifest=rgb)


def test_runtime_annotation_input_is_rejected(tmp_path: Path) -> None:
    rgb = _rgb_manifest(tmp_path)
    annotation = _annotation_manifest(tmp_path, rgb)
    document = json.loads(annotation.read_text(encoding="utf-8"))
    document["frames"][0]["runtime_policy_input"] = [0.0]
    annotation.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationFrameSetError, match="runtime/truth input"):
        load_annotation_frame_set(annotation, rgb_manifest=rgb)


def test_rendered_builder_keeps_annotation_out_of_runtime_features(tmp_path: Path) -> None:
    rgb = _rgb_manifest(tmp_path)
    annotation = _annotation_manifest(tmp_path, rgb)
    document = build_dataset(
        [rgb],
        depth_manifests=[],
        detector_path=None,
        detector_confidence=0.25,
        max_frames_per_manifest=None,
        annotation_frame_set=annotation,
    )
    assert document["training_ready"] is False
    assert document["annotation_supervision_only"] is True
    row = document["rows"][0]
    assert row["annotation_supervision"]["label_id"] == 42
    assert row["source"]["event_kind"] == "target_lost"
    assert row["source"]["truth_injected"] is False


def test_rendered_builder_can_require_exact_annotation_supervision(tmp_path: Path) -> None:
    rgb = _rgb_manifest(tmp_path)
    annotation = _annotation_manifest(tmp_path, rgb)
    document = build_dataset(
        [rgb],
        depth_manifests=[],
        detector_path=None,
        detector_confidence=0.25,
        max_frames_per_manifest=None,
        annotation_frame_set=annotation,
        require_annotation_supervision=True,
    )
    assert document["annotation_supervision_only"] is True
    assert document["rows"][0]["source"]["annotation_frame_bound"] is True


def test_rendered_builder_annotation_requirement_fails_without_frame_set(tmp_path: Path) -> None:
    rgb = _rgb_manifest(tmp_path)
    with pytest.raises(ValueError, match="requires a hash-bound Gazebo 3-D annotation"):
        build_dataset(
            [rgb],
            depth_manifests=[],
            detector_path=None,
            detector_confidence=0.25,
            max_frames_per_manifest=None,
            require_annotation_supervision=True,
        )
