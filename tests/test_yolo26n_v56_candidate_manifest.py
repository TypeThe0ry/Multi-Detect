from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.model_manifest import ModelManifestError, verify_model_manifest

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "artifacts" / "models" / "coco-yolo26n-traditional" / "yolo26n-traditional.onnx"
MANIFEST = (
    ROOT
    / "configs"
    / "models"
    / "ultralytics_yolo26n_coco80_v56_quarantined_candidate.manifest.json"
)
DEPLOYMENT_DESCRIPTOR = ROOT / "configs" / "models" / "ultralytics_yolo26n_coco80_trt86_raw.json"
PINNED_MODEL_SHA256 = "7ff7b97cad2fbb68d4fc6a74ed5dad67beb91f535e8cc25d5f0d97ae42d2e410"
COCO80_CLASS_NAMES = tuple(
    "person,bicycle,car,motorcycle,airplane,bus,train,truck,boat,traffic light,fire hydrant,"
    "stop sign,parking meter,bench,bird,cat,dog,horse,sheep,cow,elephant,bear,zebra,giraffe,"
    "backpack,umbrella,handbag,tie,suitcase,frisbee,skis,snowboard,sports ball,kite,"
    "baseball bat,baseball glove,skateboard,surfboard,tennis racket,bottle,wine glass,cup,"
    "fork,knife,spoon,bowl,banana,apple,sandwich,orange,broccoli,carrot,hot dog,pizza,donut,"
    "cake,chair,couch,potted plant,bed,dining table,toilet,tv,laptop,mouse,remote,keyboard,"
    "cell phone,microwave,oven,toaster,sink,refrigerator,book,clock,vase,scissors,teddy bear,"
    "hair drier,toothbrush".split(",")
)


def test_v56_manifest_is_strict_hash_bound_and_quarantined() -> None:
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert MANIFEST != DEPLOYMENT_DESCRIPTOR
    assert DEPLOYMENT_DESCRIPTOR.is_file()
    assert raw["status"] == "quarantined"
    assert raw["model_role"] == "safety_object_evidence"
    assert raw["intended_use"] == "rgb_safety_object_evidence_generation_only"
    assert raw["output"]["native_export"] == {
        "format": "ultralytics_raw_xywh_class_scores",
        "nms_embedded": False,
    }
    assert raw["output"]["adapter_contract"]["box_format"] == "letterbox_xyxy_px"
    assert raw["export"]["artifact_sha256"] == PINNED_MODEL_SHA256
    assert raw["governance"]["production_approved"] is False
    assert raw["validation"] == {
        "sample_count": 0,
        "deployment_domain_metrics": {},
        "validated_at_utc": None,
    }
    assert [item["id"] for item in raw["classes"]] == list(range(80))
    assert tuple(item["source_name"] for item in raw["classes"]) == COCO80_CLASS_NAMES
    assert tuple(item["canonical_label"] for item in raw["classes"]) == COCO80_CLASS_NAMES

    verified = verify_model_manifest(
        MANIFEST,
        MODEL,
        expected_class_names=COCO80_CLASS_NAMES,
        expected_output_coordinates="letterbox_xyxy_px",
        expected_model_role="safety_object_evidence",
        expected_output_format="N_x_6",
        expected_native_output_format="ultralytics_raw_xywh_class_scores",
    )

    assert verified.artifact_sha256 == PINNED_MODEL_SHA256
    assert verified.status == "quarantined"
    assert verified.production_approved is False
    assert verified.model_role == "safety_object_evidence"


def test_v56_candidate_fails_closed_when_production_approval_is_required() -> None:
    with pytest.raises(ModelManifestError, match="model is not production approved"):
        verify_model_manifest(
            MANIFEST,
            MODEL,
            expected_class_names=COCO80_CLASS_NAMES,
            expected_output_coordinates="letterbox_xyxy_px",
            expected_model_role="safety_object_evidence",
            expected_output_format="N_x_6",
            expected_native_output_format="ultralytics_raw_xywh_class_scores",
            require_production_approved=True,
        )
