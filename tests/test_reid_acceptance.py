from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import multidetect.reid_acceptance as acceptance_module
from multidetect.appearance_reid import NVIDIA_TAO_REID_V1_2_SHA256
from multidetect.reid_acceptance import (
    ReIdModelAcceptanceConfig,
    ReIdTensorRtAcceptanceConfig,
    run_reid_model_acceptance,
    run_reid_tensorrt_acceptance,
)
from multidetect.unified_tracking import AppearanceEmbedding, TargetObservation
from multidetect.vehicle_reid import OPENVINO_VEHICLE_REID_0001_SHA384


class _Encoder:
    provider_names = ("CPUExecutionProvider",)

    def __init__(self, labels: frozenset[str], feature_size: int) -> None:
        self.labels = labels
        self.feature_size = feature_size
        self.warmup_batches: list[int] = []

    def warmup(self, *, batch_size: int) -> None:
        self.warmup_batches.append(batch_size)

    def encode_detections(self, _image, detections):
        values = (1.0,) + (0.0,) * (self.feature_size - 1)
        return tuple(
            TargetObservation.from_detection(
                detection,
                appearance=(
                    AppearanceEmbedding(values) if detection.label in self.labels else None
                ),
                appearance_reliable=detection.label in self.labels,
            )
            for detection in detections
        )


class _TensorEncoder(_Encoder):
    provider_names = ("TensorrtExecutionProvider",)


def test_reid_model_acceptance_keeps_identity_domains_disjoint(monkeypatch, tmp_path: Path) -> None:
    person_model = tmp_path / "person.onnx"
    vehicle_model = tmp_path / "vehicle.onnx"
    person_model.write_bytes(b"person")
    vehicle_model.write_bytes(b"vehicle")

    def _digest(_path: Path, algorithm: str) -> str:
        if algorithm == "sha256":
            return NVIDIA_TAO_REID_V1_2_SHA256
        return OPENVINO_VEHICLE_REID_0001_SHA384

    monkeypatch.setattr(acceptance_module, "_digest", _digest)
    person = _Encoder(frozenset({"person", "firefighter"}), 256)
    vehicle = _Encoder(frozenset({"vehicle", "car", "bus", "truck"}), 512)
    report = run_reid_model_acceptance(
        ReIdModelAcceptanceConfig(
            person_model_path=person_model,
            vehicle_model_path=vehicle_model,
            person_count=4,
            vehicle_count=4,
            iterations=2,
        ),
        person_encoder=person,
        vehicle_encoder=vehicle,
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert person.warmup_batches == [4]
    assert vehicle.warmup_batches == [4]
    assert report["person_embedding_count"] == 4
    assert report["vehicle_embedding_count"] == 4
    assert report["mixed_detection_count"] == 10
    assert report["identity_domains_disjoint"] is True
    assert report["maximum_person_repeat_cosine_distance"] == 0.0
    assert report["maximum_vehicle_repeat_cosine_distance"] == 0.0
    assert report["repeat_stability_validated"] is True
    assert report["deployment_domain_accuracy_validated"] is False
    assert report["target_tensorrt_runtime_validated"] is False
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


def test_reid_tensorrt_acceptance_binds_engines_and_closes_sessions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    person_model = tmp_path / "person.onnx"
    vehicle_model = tmp_path / "vehicle.onnx"
    person_engine = tmp_path / "person.engine"
    vehicle_engine = tmp_path / "vehicle.engine"
    for path in (person_model, vehicle_model, person_engine, vehicle_engine):
        path.write_bytes(path.name.encode("ascii"))

    def _digest(path: Path, algorithm: str) -> str:
        if path == person_model:
            return NVIDIA_TAO_REID_V1_2_SHA256
        if path == vehicle_model:
            return OPENVINO_VEHICLE_REID_0001_SHA384
        assert algorithm == "sha256"
        return "a" * 64

    sessions = []

    class _Session:
        def __init__(self, engine_path, **kwargs) -> None:
            self.engine_path = Path(engine_path)
            self.kwargs = kwargs
            self.closed = False
            sessions.append(self)

        def close(self) -> None:
            self.closed = True

    person = _TensorEncoder(frozenset({"person", "firefighter"}), 256)
    vehicle = _TensorEncoder(frozenset({"vehicle", "car", "bus", "truck"}), 512)
    monkeypatch.setattr(acceptance_module, "_digest", _digest)
    monkeypatch.setattr(acceptance_module, "TensorRtEmbeddingSession", _Session)
    monkeypatch.setattr(
        acceptance_module,
        "OnnxPersonReIdEncoder",
        lambda _config, *, session: person,
    )
    monkeypatch.setattr(
        acceptance_module,
        "OnnxVehicleReIdEncoder",
        lambda _config, *, session: vehicle,
    )

    report = run_reid_tensorrt_acceptance(
        ReIdTensorRtAcceptanceConfig(
            person_model_path=person_model,
            vehicle_model_path=vehicle_model,
            person_engine_path=person_engine,
            vehicle_engine_path=vehicle_engine,
            person_count=4,
            vehicle_count=4,
            iterations=2,
        ),
        image_bgr=np.zeros((720, 1280, 3), dtype=np.uint8),
    )

    assert [session.engine_path for session in sessions] == [person_engine, vehicle_engine]
    assert all(session.closed for session in sessions)
    assert report["runtime"] == "native_tensorrt"
    assert report["providers"] == ["TensorrtExecutionProvider"]
    assert report["target_tensorrt_runtime_validated"] is True
    assert report["person_engine_sha256"] == "a" * 64
    assert report["vehicle_engine_sha256"] == "a" * 64
    assert report["identity_domains_disjoint"] is True
    assert report["repeat_stability_validated"] is True
    assert report["flight_control_enabled"] is False
    assert report["physical_release_enabled"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("person_count", 0),
        ("vehicle_count", 11),
        ("iterations", 0),
        ("iterations", True),
        ("realtime_frame_budget_ms", float("nan")),
    ),
)
def test_reid_model_acceptance_config_rejects_invalid_values(
    field: str,
    value: object,
) -> None:
    values = {
        "person_model_path": Path("person.onnx"),
        "vehicle_model_path": Path("vehicle.onnx"),
        field: value,
    }
    with pytest.raises(ValueError):
        ReIdModelAcceptanceConfig(**values)


def test_reid_tensorrt_acceptance_rejects_batch_outside_vehicle_profile() -> None:
    with pytest.raises(ValueError, match="profile maximum"):
        ReIdTensorRtAcceptanceConfig(
            person_model_path=Path("person.onnx"),
            vehicle_model_path=Path("vehicle.onnx"),
            person_engine_path=Path("person.engine"),
            vehicle_engine_path=Path("vehicle.engine"),
            vehicle_count=9,
        )
