from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.materialize_gazebo_3d_annotation_capture import (
    AnnotationMaterializationError,
    materialize,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs/scenarios/gazebo-3d-annotation-capture-v1.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _elements(path: Path, tag: str):
    root = ET.parse(path).getroot()
    return [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == tag]


def test_annotation_wrappers_are_capture_only_and_have_3d_sensors() -> None:
    vehicle = ROOT / "simulation/gazebo/annotation/multidetect_annotation_x500_v1/model.sdf"
    target = ROOT / "simulation/gazebo/annotation/multidetect_annotation_target_v1/model.sdf"
    sensors = _elements(vehicle, "sensor")
    assert {item.get("type") for item in sensors} == {"boundingbox_camera", "segmentation"}
    topics = {
        next(child.text.strip() for child in sensor if child.tag == "topic")
        for sensor in sensors
    }
    assert topics == {
        "multidetect/annotation/bounding_boxes_3d",
        "multidetect/annotation/instance_segmentation",
    }
    assert not _elements(vehicle, "plugin")
    plugins = _elements(target, "plugin")
    assert len(plugins) == 1
    assert plugins[0].get("filename") == "gz-sim-label-system"
    assert next(child.text.strip() for child in plugins[0] if child.tag == "label") == "42"


def test_materializer_changes_only_the_temporary_world(tmp_path: Path) -> None:
    source = ROOT / "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf"
    source_hash = _sha256(source)
    output = tmp_path / "annotation-world.sdf"
    binding_path = tmp_path / "binding.json"
    resource_root = tmp_path / "annotation-models"
    binding = materialize(
        output_world=output,
        output_binding=binding_path,
        output_resource_root=resource_root,
    )

    assert source_hash == _sha256(source)
    assert output.is_file()
    assert binding["status"] == "prepared_not_executed"
    assert binding["external_gazebo_executed"] is False
    assert binding["training_ready"] is False
    assert binding["runtime_boundary"]["runtime_consumes_annotation_topics"] is False
    assert binding["resource_root"]["mount_read_only"] is True
    assert binding["resource_root"]["file_count"] >= 8
    assert (resource_root / "multidetect_annotation_x500_v1/model.sdf").is_file()
    assert (resource_root / "multidetect_annotation_target_v1/model.sdf").is_file()
    assert (resource_root / "x500_mono_cam/model.sdf").is_file()
    assert (resource_root / "multidetect_search_target_v1/model.sdf").is_file()
    document = ET.parse(output).getroot()
    uris = [
        (child.text or "").strip()
        for include in document.iter()
        if include.tag.rsplit("}", 1)[-1] == "include"
        for child in include
        if child.tag.rsplit("}", 1)[-1] == "uri"
    ]
    assert "model://multidetect_annotation_target_v1" in uris
    assert "model://multidetect_search_target_v1" not in uris
    assert json.loads(binding_path.read_text(encoding="utf-8")) == binding


def test_materializer_rejects_stale_base_world_hash(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["base_world"]["sha256"] = "0" * 64
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationMaterializationError, match="base world hash mismatch"):
        materialize(manifest_path=stale, output_world=tmp_path / "out.sdf")


def test_materializer_rejects_runtime_truth_boundary(tmp_path: Path) -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["runtime_boundary"]["truth_injected_into_policy"] = True
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(AnnotationMaterializationError, match="runtime boundary"):
        materialize(manifest_path=invalid, output_world=tmp_path / "out.sdf")
