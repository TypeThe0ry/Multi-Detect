from __future__ import annotations

import re
from pathlib import Path

import pytest

import multidetect.perception_factory as factory_module
from multidetect.cli import build_parser
from multidetect.config import MissionConfig
from multidetect.model_manifest import (
    ModelManifestError,
    create_candidate_model_manifest,
    create_semantic_context_model_manifest,
    sha256_file,
    write_candidate_model_manifest,
)
from multidetect.perception_factory import build_production_perception
from multidetect.semantic_environment import CITYSEMSEGFORMER_LABELS
from multidetect.vision import (
    BrightNeutralLightVetoFilter,
    ClassConfidenceFilter,
    FrameCadencedDetector,
    LabelAllowListFilter,
    PersonOverlapVetoFilter,
    TemporalDetectionFilter,
    VehicleFurnitureOverlapVetoFilter,
)

ROOT = Path(__file__).resolve().parents[1]
MISSION_PATH = ROOT / "configs/missions/fire_patrol.demo.json"


class _FakeDetector:
    events: list[str] = []

    def __init__(self, config) -> None:
        self.config = config
        self.class_names = tuple(config.class_names)
        self.provider_names = ("FakeExecutionProvider",)
        self.component = Path(config.model_path).stem
        self.events.append(f"construct:{self.component}")

    def warmup(self, *, iterations: int = 1) -> None:
        self.events.append(f"warmup:{self.component}:{iterations}")

    def detect(self, _image) -> tuple:
        return ()

    def covers_labels(self, required_labels) -> bool:
        available = {label.strip().lower() for label in self.class_names}
        required = {label.strip().lower() for label in required_labels}
        return required.issubset(available)


class _FakeSemanticModel:
    close_count = 0

    def __init__(self, config, *, session=None) -> None:
        self.config = config
        self.session = session
        self.provider_names = ("FakeSemanticProvider",)

    def warmup(self) -> None:
        pass

    def close(self) -> None:
        type(self).close_count += 1


@pytest.fixture(autouse=True)
def _fake_executable_graphs(monkeypatch):
    _FakeDetector.events = []
    _FakeSemanticModel.close_count = 0
    monkeypatch.setattr(factory_module, "OnnxNx6Detector", _FakeDetector)
    monkeypatch.setattr(factory_module, "OnnxRawYoloDetector", _FakeDetector)
    monkeypatch.setattr(factory_module, "OnnxCategoricalSemanticContext", _FakeSemanticModel)


def _model_manifest(
    tmp_path: Path,
    component: str,
    *,
    class_names: tuple[str, ...],
    role: str,
    output_coordinates: str = "normalized_xyxy",
    native_output_format: str = "post_nms_N_x_6",
) -> tuple[Path, Path]:
    model = tmp_path / f"{component}.onnx"
    model.write_bytes(f"{component}-model-bytes".encode())
    document = create_candidate_model_manifest(
        model,
        model_id=f"{component}-model",
        model_version=f"{component}-v1",
        class_names=class_names,
        input_width=640,
        input_height=640,
        output_coordinates=output_coordinates,
        source_description=f"{component} test model",
        model_role=role,
        native_output_format=native_output_format,
    )
    manifest = write_candidate_model_manifest(
        tmp_path / f"{component}.manifest.json",
        document,
    )
    return model, manifest


def _semantic_manifest(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "semantic_context.onnx"
    model.write_bytes(b"semantic-context-model-bytes")
    manifest = write_candidate_model_manifest(
        tmp_path / "semantic_context.manifest.json",
        create_semantic_context_model_manifest(
            model,
            model_id="semantic-context-model",
            model_version="semantic-v1",
            class_names=CITYSEMSEGFORMER_LABELS,
            input_width=1820,
            input_height=1024,
            output_name="output",
            source_description="semantic context test model",
        ),
    )
    return model, manifest


def _live_args(*extra: str):
    return build_parser().parse_args(
        [
            "live-camera",
            str(MISSION_PATH),
            *extra,
        ]
    )


def _unwrap_types(detector) -> list[type]:
    types: list[type] = []
    current = detector
    while current is not None:
        types.append(type(current))
        current = getattr(current, "detector", None)
    return types


def test_factory_builds_full_role_bound_graph_and_stable_model_set_hash(tmp_path: Path) -> None:
    primary, primary_manifest = _model_manifest(
        tmp_path,
        "primary",
        class_names=("fire", "smoke"),
        role="fire_candidate",
    )
    verifier, verifier_manifest = _model_manifest(
        tmp_path,
        "rgb_verifier",
        class_names=("fire", "smoke"),
        role="fire_verifier",
    )
    safety, safety_manifest = _model_manifest(
        tmp_path,
        "safety",
        class_names=("person", "firefighter", "car", "chair"),
        role="safety_object_evidence",
    )
    priority, priority_manifest = _model_manifest(
        tmp_path,
        "priority",
        class_names=(
            "pedestrian",
            "people",
            "bicycle",
            "car",
            "van",
            "truck",
            "tricycle",
            "awning-tricycle",
            "bus",
            "motor",
        ),
        role="safety_object_evidence",
        output_coordinates="letterbox_xyxy_px",
        native_output_format="ultralytics_raw_xywh_class_scores",
    )
    environment, environment_manifest = _model_manifest(
        tmp_path,
        "environment",
        class_names=("power_line", "flammable_tank"),
        role="environment_risk_evidence",
    )
    semantic, semantic_manifest = _semantic_manifest(tmp_path)
    args = _live_args(
        "--onnx-model",
        str(primary),
        "--model-manifest",
        str(primary_manifest),
        "--output-coordinates",
        "normalized_xyxy",
        "--rgb-fire-verifier-model",
        str(verifier),
        "--rgb-fire-verifier-model-manifest",
        str(verifier_manifest),
        "--rgb-fire-verifier-output-coordinates",
        "normalized_xyxy",
        "--safety-onnx-model",
        str(safety),
        "--safety-model-manifest",
        str(safety_manifest),
        "--safety-class-names",
        "person,firefighter,car,chair",
        "--safety-output-coordinates",
        "normalized_xyxy",
        "--priority-onnx-model",
        str(priority),
        "--priority-model-manifest",
        str(priority_manifest),
        "--environment-onnx-model",
        str(environment),
        "--environment-model-manifest",
        str(environment_manifest),
        "--environment-output-coordinates",
        "normalized_xyxy",
        "--semantic-context-onnx-model",
        str(semantic),
        "--semantic-context-model-manifest",
        str(semantic_manifest),
        "--provider",
        "FakeExecutionProvider",
        "--warmup-iterations",
        "2",
    )

    bundle = build_production_perception(args, MissionConfig.from_json(MISSION_PATH))

    assert bundle.configured_model_components == (
        "primary",
        "rgb_fire_verifier",
        "safety",
        "priority",
        "environment",
        "semantic_context",
    )
    assert tuple(item.component for item in bundle.verified_artifacts) == (
        bundle.configured_model_components
    )
    assert tuple(item.artifact.model_role for item in bundle.verified_artifacts) == (
        "fire_candidate",
        "fire_verifier",
        "safety_object_evidence",
        "safety_object_evidence",
        "environment_risk_evidence",
        "semantic_scene_context",
    )
    assert all(
        item.artifact.artifact_sha256 == sha256_file(item.model_path)
        and item.manifest_sha256 == sha256_file(item.manifest_path)
        for item in bundle.verified_artifacts
    )
    assert bundle.all_configured_artifacts_verified is True
    assert bundle.all_configured_artifacts_production_approved is False
    assert bundle.synthetic_hil_components == ()
    assert bundle.model_set_sha256 is not None
    assert re.fullmatch(r"[0-9a-f]{64}", bundle.model_set_sha256)
    assert bundle.verified_artifact("priority") is not None
    assert bundle.verified_artifact("missing") is None
    assert bundle.rgb_fire_verifier is not None
    assert bundle.rgb_fire_corroborator is not None
    assert bundle.rgb_fire_verifier_evidence_qualified is False
    assert bundle.person_safety_model_coverage is True
    assert bundle.person_safety_evidence_qualified is True
    assert bundle.environment_model_coverage is True
    assert bundle.environment_evidence_qualified is True
    assert len(bundle.model_detectors) == 4
    assert bundle.provider_names == ("FakeExecutionProvider",) * 4

    wrapper_types = _unwrap_types(bundle.detector)
    assert wrapper_types[:6] == [
        TemporalDetectionFilter,
        PersonOverlapVetoFilter,
        BrightNeutralLightVetoFilter,
        LabelAllowListFilter,
        VehicleFurnitureOverlapVetoFilter,
        ClassConfidenceFilter,
    ]
    assert isinstance(bundle.primary_detector, FrameCadencedDetector)
    assert _FakeDetector.events.index("warmup:rgb_verifier:2") < _FakeDetector.events.index(
        "construct:safety"
    )

    second = build_production_perception(args, MissionConfig.from_json(MISSION_PATH))
    assert second.model_set_sha256 == bundle.model_set_sha256
    assert bundle.close() is True
    assert bundle.close() is True
    assert second.close() is True
    assert _FakeSemanticModel.close_count == 2


def test_factory_fails_manifest_role_gate_before_constructing_any_graph(tmp_path: Path) -> None:
    primary, primary_manifest = _model_manifest(
        tmp_path,
        "primary",
        class_names=("fire", "smoke"),
        role="fire_candidate",
    )
    safety = tmp_path / "safety.onnx"
    safety.write_bytes(primary.read_bytes())
    args = _live_args(
        "--onnx-model",
        str(primary),
        "--model-manifest",
        str(primary_manifest),
        "--output-coordinates",
        "normalized_xyxy",
        "--safety-onnx-model",
        str(safety),
        "--safety-model-manifest",
        str(primary_manifest),
        "--safety-class-names",
        "fire,smoke",
        "--safety-output-coordinates",
        "normalized_xyxy",
    )

    with pytest.raises(ModelManifestError, match="safety_object_evidence"):
        build_production_perception(args, MissionConfig.from_json(MISSION_PATH))

    assert _FakeDetector.events == []


def test_factory_preserves_unverified_onnx_mode_and_marks_model_set_unknown(tmp_path: Path) -> None:
    model = tmp_path / "unverified.onnx"
    model.write_bytes(b"unverified-placeholder")
    args = _live_args("--onnx-model", str(model))

    bundle = build_production_perception(args, MissionConfig.from_json(MISSION_PATH))

    assert bundle.configured_model_components == ("primary",)
    assert bundle.verified_artifacts == ()
    assert bundle.verified_fire_model is None
    assert bundle.all_configured_artifacts_verified is False
    assert bundle.all_configured_artifacts_production_approved is False
    assert bundle.model_set_sha256 is None
    assert bundle.close() is True
