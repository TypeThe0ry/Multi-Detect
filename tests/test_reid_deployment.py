from __future__ import annotations

import json
from pathlib import Path

from multidetect.appearance_reid import NVIDIA_TAO_REID_V1_2_SHA256
from multidetect.vehicle_reid import OPENVINO_VEHICLE_REID_0001_SHA384

ROOT = Path(__file__).resolve().parents[1]


def test_person_reid_manifest_is_pinned_and_cannot_be_used_for_vehicle_or_fire_identity() -> None:
    document = json.loads(
        (ROOT / "configs/models/nvidia_tao_person_reid_v1_2.json").read_text(encoding="utf-8")
    )

    assert document["model_role"] == "person_reid"
    assert document["artifact_sha256"] == NVIDIA_TAO_REID_V1_2_SHA256
    assert document["output"]["feature_size"] == 256
    assert document["output"]["l2_normalization_required"] is True
    assert set(document["validated_labels"]) == {"person", "firefighter"}
    assert {"vehicle", "flame", "fire", "smoke", "hotspot"}.issubset(
        document["prohibited_identity_labels"]
    )
    assert document["production_approved"] is False
    assert document["flight_control_enabled"] is False
    assert document["physical_release_enabled"] is False


def test_jetson_reid_build_is_fp16_dynamic_batch_and_refuses_live_contention() -> None:
    script = (ROOT / "scripts/build_jetson_reid_engine.sh").read_text(encoding="utf-8")

    assert NVIDIA_TAO_REID_V1_2_SHA256 in script
    assert "--minShapes=input:1x3x256x128" in script
    assert "--optShapes=input:8x3x256x128" in script
    assert "--maxShapes=input:10x3x256x128" in script
    assert "--fp16" in script
    assert "--skipInference" in script
    assert "pgrep -f 'multidetect live-camera'" in script
    assert "refusing a concurrent TensorRT engine build" in script
    assert "systemctl" not in script
    assert "kill " not in script


def test_vehicle_reid_descriptor_is_pinned_and_identity_domains_are_disjoint() -> None:
    document = json.loads(
        (ROOT / "configs/models/openvino_vehicle_reid_0001.json").read_text(encoding="utf-8")
    )

    assert document["model_role"] == "vehicle_reid"
    assert document["artifact_sha384"] == OPENVINO_VEHICLE_REID_0001_SHA384
    assert document["input"]["shape"] == ["batch", 3, 208, 208]
    assert document["output"]["feature_size"] == 512
    assert set(document["validated_labels"]) == {"bus", "car", "truck", "vehicle"}
    assert {"person", "firefighter", "motorcycle", "bicycle"}.issubset(
        document["prohibited_identity_labels"]
    )
    assert document["production_approved"] is False
    assert document["flight_control_enabled"] is False
    assert document["physical_release_enabled"] is False


def test_jetson_vehicle_reid_build_is_pinned_and_refuses_live_contention() -> None:
    script = (ROOT / "scripts/build_jetson_vehicle_reid_engine.sh").read_text(encoding="utf-8")

    assert OPENVINO_VEHICLE_REID_0001_SHA384 in script
    assert "--minShapes=input:1x3x208x208" in script
    assert "--optShapes=input:4x3x208x208" in script
    assert "--maxShapes=input:8x3x208x208" in script
    assert "--fp16" in script
    assert "--skipInference" in script
    assert "pgrep -f 'multidetect live-camera'" in script
    assert "refusing a concurrent TensorRT engine build" in script
    assert "systemctl" not in script
    assert "kill " not in script
