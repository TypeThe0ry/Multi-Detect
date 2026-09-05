from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.candidate_detector_controls import (
    CandidateDetectorControlsConfig,
    CandidateDetectorControlsInputError,
    evaluate_candidate_detector_controls,
)
from multidetect.domain import BoundingBox, Detection
from multidetect.model_manifest import create_candidate_model_manifest, sha256_file

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_candidate_detector_controls.py"


@dataclass(frozen=True, slots=True)
class _Case:
    model: Path
    manifest: Path
    frame: Path
    annotation: Path
    config: CandidateDetectorControlsConfig


class _FakeDetector:
    provider_names = ("FakeExecutionProvider",)
    provider_selection = None

    def __init__(
        self,
        outputs: Sequence[tuple[Detection, ...]],
        observed_images: list[Any],
    ) -> None:
        self._outputs = tuple(outputs)
        self._observed_images = observed_images
        self._index = 0

    def detect(self, image_bgr: Any) -> tuple[Detection, ...]:
        self._observed_images.append(image_bgr.copy())
        output = self._outputs[self._index]
        self._index += 1
        return output


def _detection(
    *,
    label: str = "car",
    confidence: float = 0.90,
    bbox: BoundingBox | None = None,
) -> Detection:
    return Detection(
        label=label,
        confidence=confidence,
        bbox=bbox or BoundingBox(0.25, 0.25, 0.75, 0.75),
        model_version="fake-raw-candidate",
    )


def _factory(
    outputs: Sequence[tuple[Detection, ...]],
) -> tuple[Callable[[Any], _FakeDetector], list[Any], list[Any]]:
    observed_configs: list[Any] = []
    observed_images: list[Any] = []

    def build(config: Any) -> _FakeDetector:
        observed_configs.append(config)
        return _FakeDetector(outputs, observed_images)

    return build, observed_configs, observed_images


def _write_case(tmp_path: Path) -> _Case:
    model = tmp_path / "candidate.onnx"
    model.write_bytes(b"raw-yolo-candidate-placeholder")
    manifest_document = create_candidate_model_manifest(
        model,
        model_id="raw-candidate-test",
        model_version="v56-test",
        class_names=("person", "car"),
        input_width=640,
        input_height=640,
        output_coordinates="letterbox_xyxy_px",
        source_description="test-only quarantined raw ONNX candidate",
        model_role="safety_object_evidence",
        native_output_format="ultralytics_raw_xywh_class_scores",
    )
    manifest = tmp_path / "candidate.manifest.json"
    manifest.write_text(json.dumps(manifest_document), encoding="utf-8")

    frame = tmp_path / "sensor-frame-0001.bgr24"
    frame.write_bytes(np.full((4, 4, 3), 200, dtype=np.uint8).tobytes())
    annotation = tmp_path / "sensor-frame-0001.annotation.json"
    annotation.write_text(
        json.dumps(
            {
                "schema": "multidetect.candidate-detector-control-annotation",
                "schema_version": 1,
                "frame_id": "gazebo-frame-0001",
                "frame_sha256": sha256_file(frame),
                "normalized_bbox": [0.25, 0.25, 0.75, 0.75],
                "expected_label": "car",
                "independent_of_model": True,
                "source_kind": "software_test_fixture",
                "width": 4,
                "height": 4,
                "pixel_format": "BGR24",
            }
        ),
        encoding="utf-8",
    )
    config = CandidateDetectorControlsConfig(
        model_path=model,
        model_manifest_path=manifest,
        frame_path=frame,
        annotation_path=annotation,
        frame_width=4,
        frame_height=4,
        confidence_threshold=0.25,
        positive_min_confidence=0.50,
        positive_min_iou=0.50,
        providers=("FakeExecutionProvider",),
    )
    return _Case(model, manifest, frame, annotation, config)


def _mutate_json(path: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def _write_frame_set(case: _Case, *, selected_frame_id: str = "gazebo-frame-0001") -> Path:
    frame_sha = sha256_file(case.frame)
    frame_bytes = case.frame.stat().st_size
    sensor_stamp_ns = 2_000_000_000
    provenance = {
        "frame_id": selected_frame_id,
        "sequence": 0,
        "captured_at_s": 123.25,
        "captured_at_clock": "host_monotonic",
        "sensor_stamp_ns": sensor_stamp_ns,
        "camera_info_stamp_ns": 1_950_000_000,
        "navsat_stamp_ns": 1_900_000_000,
        "odometry_stamp_ns": 1_850_000_000,
        "camera_info_source_skew_ns": 50_000_000,
        "navsat_source_skew_ns": 100_000_000,
        "odometry_source_skew_ns": 150_000_000,
        "record_sha256": "c" * 64,
        "payload_sha256": frame_sha,
        "width": 4,
        "height": 4,
        "stride": 12,
        "payload_bytes": frame_bytes,
        "flags": 15,
        "pixel_format": "BGR24",
    }
    frame_row = {
        "frame_id": selected_frame_id,
        "sequence": 0,
        "file": case.frame.name,
        "bytes": frame_bytes,
        "sha256": frame_sha,
        "wire_payload_sha256": frame_sha,
        "raw_bytes_sha256_matches_wire_payload": True,
        "wire_record_sha256": "c" * 64,
        "provenance": provenance,
    }
    document: dict[str, Any] = {
        "schema": "multidetect.gazebo-bgr24-frame-set",
        "schema_version": 1,
        "generated_at_utc": "2026-09-05T00:00:00Z",
        "scenario_id": "terrain_pixel_validation_001",
        "scenario_manifest_hash": "a" * 64,
        "sensor_binding_hash": "b" * 64,
        "wire_version": 3,
        "pixel_format": "BGR24",
        "frame_count": 1,
        "total_payload_bytes": frame_bytes,
        "frames": [frame_row],
        "source_attribution": {
            "pixels": "actual_gazebo_camera_sensor_output",
            "environment": "software_simulation",
            "wire": "MDGZSIM3",
            "truth_loaded_by_runtime": False,
            "source_row_order": "top_to_bottom",
            "wire_row_order": "top_to_bottom",
            "vertical_flip_applied": False,
            "repository_model_asset_file_count": 11,
            "repository_model_assets_sha256": "d" * 64,
        },
        "claims": {
            "actual_gazebo_pixels_persisted": True,
            "qgc_rendering": "not_claimed",
            "manual_visual_acceptance": "not_claimed",
            "hardware": "not_claimed",
            "detector_evaluation": "not_claimed",
            "production_model_approval": "not_claimed",
        },
    }
    document["manifest_sha256"] = canonical_sha256(document)
    path = case.frame.parent / "gazebo-bgr24-frame-set.manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_three_controls_pass_with_one_detector_session_and_truth_isolation(
    tmp_path: Path,
) -> None:
    case = _write_case(tmp_path)
    factory, configs, images = _factory(((_detection(),), (), ()))

    document = evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert document["gate_passed"] is True
    assert document["controls"]["present"]["passed"] is True
    assert document["controls"]["blank"]["passed"] is True
    assert document["controls"]["occluded"]["passed"] is True
    assert document["model_identity"]["candidate"] is True
    assert document["model_identity"]["raw_ultralytics_onnx"] is True
    assert document["model_identity"]["manifest_status"] == "quarantined"
    assert document["production_approved"] is False
    assert document["accuracy_validated"] is False
    assert document["qgc_visual_acceptance"] == "not_claimed"
    assert document["hardware_acceptance"] is False
    assert document["truth_isolation"]["annotation_supplied_to_detector"] is False
    assert len(configs) == 1
    assert len(images) == 3
    assert np.array_equal(images[0], np.full((4, 4, 3), 200, dtype=np.uint8))
    assert not images[1].any()
    assert np.all(images[2][1:3, 1:3] == 114)
    assert np.all(images[2][0, :] == 200)
    assert document["execution_provider"]["selected"] == ["FakeExecutionProvider"]
    assert document["provenance"]["actual_gazebo_pixels_persisted"] is False
    assert document["provenance"]["telemetry_causal_binding"] is False


@pytest.mark.parametrize(
    ("outputs", "failed_control"),
    [
        (((), (), ()), "present"),
        (((_detection(),), (_detection(label="person"),), ()), "blank"),
        (((_detection(),), (), (_detection(),)), "occluded"),
    ],
    ids=["positive-miss", "blank-false-positive", "occlusion-residual"],
)
def test_each_control_fails_the_gate_independently(
    tmp_path: Path,
    outputs: Sequence[tuple[Detection, ...]],
    failed_control: str,
) -> None:
    case = _write_case(tmp_path)
    factory, _, _ = _factory(outputs)

    document = evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert document["gate_passed"] is False
    assert document["controls"][failed_control]["passed"] is False


def test_positive_requires_expected_label_confidence_and_iou(tmp_path: Path) -> None:
    case = _write_case(tmp_path)
    low_confidence = _detection(confidence=0.49)
    wrong_iou = _detection(bbox=BoundingBox(0.0, 0.0, 0.20, 0.20))
    factory, _, _ = _factory(((low_confidence, wrong_iou), (), ()))

    document = evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert document["gate_passed"] is False
    assert document["controls"]["present"]["qualifying_expected_match_count"] == 0


def test_frame_hash_mismatch_fails_before_detector_construction(tmp_path: Path) -> None:
    case = _write_case(tmp_path)
    _mutate_json(case.annotation, lambda raw: raw.__setitem__("frame_sha256", "f" * 64))
    factory, configs, _ = _factory(((), (), ()))

    with pytest.raises(CandidateDetectorControlsInputError, match="frame_sha256"):
        evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert configs == []


def test_model_derived_annotation_is_rejected_before_inference(tmp_path: Path) -> None:
    case = _write_case(tmp_path)
    _mutate_json(case.annotation, lambda raw: raw.__setitem__("derived_from_model", True))
    factory, configs, _ = _factory(((), (), ()))

    with pytest.raises(CandidateDetectorControlsInputError, match="derived_from_model"):
        evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert configs == []


@pytest.mark.parametrize(
    ("status", "approved", "message"),
    [
        ("approved", False, "status must be exactly 'quarantined'"),
        ("quarantined", True, "production_approved must be exactly false"),
    ],
)
def test_manifest_must_remain_quarantined_and_not_production_approved(
    tmp_path: Path,
    status: str,
    approved: bool,
    message: str,
) -> None:
    case = _write_case(tmp_path)

    def mutate(raw: dict[str, Any]) -> None:
        raw["status"] = status
        raw["governance"]["production_approved"] = approved

    _mutate_json(case.manifest, mutate)
    factory, configs, _ = _factory(((), (), ()))

    with pytest.raises(CandidateDetectorControlsInputError, match=message):
        evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert configs == []


def test_raw_bgr24_geometry_mismatch_fails_closed(tmp_path: Path) -> None:
    case = _write_case(tmp_path)
    case.frame.write_bytes(case.frame.read_bytes()[:-1])
    _mutate_json(
        case.annotation,
        lambda raw: raw.__setitem__("frame_sha256", sha256_file(case.frame)),
    )
    factory, configs, _ = _factory(((), (), ()))

    with pytest.raises(CandidateDetectorControlsInputError, match="byte length mismatch"):
        evaluate_candidate_detector_controls(case.config, detector_factory=factory)

    assert configs == []


def test_matching_frame_set_proves_gazebo_pixels_and_v2_causal_binding(tmp_path: Path) -> None:
    case = _write_case(tmp_path)
    frame_set = _write_frame_set(case)
    config = CandidateDetectorControlsConfig(
        **{
            name: getattr(case.config, name)
            for name in CandidateDetectorControlsConfig.__dataclass_fields__
            if name != "frame_set_manifest_path"
        },
        frame_set_manifest_path=frame_set,
    )
    factory, _, _ = _factory(((_detection(),), (), ()))

    document = evaluate_candidate_detector_controls(config, detector_factory=factory)

    assert document["gate_passed"] is True
    assert document["frame_identity"]["frame_set_manifest_verified"] is True
    assert document["provenance"]["actual_gazebo_pixels_persisted"] is True
    assert document["provenance"]["telemetry_causal_binding"] is True
    assert document["provenance"]["wire_version"] == 3
    assert document["provenance"]["maximum_source_skew_ns"] == 150_000_000
    assert document["provenance"]["source_row_order"] == "top_to_bottom"
    assert document["provenance"]["wire_row_order"] == "top_to_bottom"
    assert document["provenance"]["vertical_flip_applied"] is False
    assert document["truth_isolation"]["gazebo_runtime_truth_isolation_verified"] is True


def test_frame_set_allows_static_camera_info_without_inflating_dynamic_skew(
    tmp_path: Path,
) -> None:
    case = _write_case(tmp_path)
    frame_set = _write_frame_set(case)
    raw = json.loads(frame_set.read_text(encoding="utf-8"))
    provenance = raw["frames"][0]["provenance"]
    provenance["camera_info_stamp_ns"] = 1
    provenance["camera_info_source_skew_ns"] = provenance["sensor_stamp_ns"] - 1
    raw.pop("manifest_sha256")
    raw["manifest_sha256"] = canonical_sha256(raw)
    frame_set.write_text(json.dumps(raw), encoding="utf-8")
    config = CandidateDetectorControlsConfig(
        **{
            name: getattr(case.config, name)
            for name in CandidateDetectorControlsConfig.__dataclass_fields__
            if name != "frame_set_manifest_path"
        },
        frame_set_manifest_path=frame_set,
    )
    factory, _, _ = _factory(((_detection(),), (), ()))

    document = evaluate_candidate_detector_controls(config, detector_factory=factory)

    assert document["gate_passed"] is True
    assert document["provenance"]["maximum_source_skew_ns"] == 150_000_000


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source_row_order", "bottom_to_top"),
        ("wire_row_order", "bottom_to_top"),
        ("vertical_flip_applied", True),
    ],
)
def test_frame_set_row_order_attribution_fails_closed_before_inference(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    case = _write_case(tmp_path)
    frame_set = _write_frame_set(case)
    raw = json.loads(frame_set.read_text(encoding="utf-8"))
    raw["source_attribution"][key] = value
    raw.pop("manifest_sha256")
    raw["manifest_sha256"] = canonical_sha256(raw)
    frame_set.write_text(json.dumps(raw), encoding="utf-8")
    config = CandidateDetectorControlsConfig(
        **{
            name: getattr(case.config, name)
            for name in CandidateDetectorControlsConfig.__dataclass_fields__
            if name != "frame_set_manifest_path"
        },
        frame_set_manifest_path=frame_set,
    )
    factory, configs, _ = _factory(((), (), ()))

    with pytest.raises(CandidateDetectorControlsInputError, match="row-order attribution"):
        evaluate_candidate_detector_controls(config, detector_factory=factory)

    assert configs == []


def test_frame_set_identity_mismatch_fails_before_inference(tmp_path: Path) -> None:
    case = _write_case(tmp_path)
    frame_set = _write_frame_set(case, selected_frame_id="different-frame")
    config = CandidateDetectorControlsConfig(
        **{
            name: getattr(case.config, name)
            for name in CandidateDetectorControlsConfig.__dataclass_fields__
            if name != "frame_set_manifest_path"
        },
        frame_set_manifest_path=frame_set,
    )
    factory, configs, _ = _factory(((), (), ()))

    with pytest.raises(CandidateDetectorControlsInputError, match="frame_id is absent"):
        evaluate_candidate_detector_controls(config, detector_factory=factory)

    assert configs == []


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("candidate_controls_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("gate_passed", "expected_exit"), [(True, 0), (False, 2)])
def test_cli_maps_gate_state_to_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate_passed: bool,
    expected_exit: int,
) -> None:
    module = _load_script_module()
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        module,
        "evaluate_candidate_detector_controls",
        lambda _config: {"gate_passed": gate_passed},
    )

    exit_code = module.main(
        [
            "--model",
            "model.onnx",
            "--manifest",
            "manifest.json",
            "--frame",
            "frame.bgr24",
            "--annotation",
            "annotation.json",
            "--width",
            "4",
            "--height",
            "4",
            "--out",
            str(output),
        ]
    )

    assert exit_code == expected_exit
    assert json.loads(output.read_text(encoding="utf-8"))["gate_passed"] is gate_passed


def test_cli_maps_input_contract_error_to_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module()
    output = tmp_path / "input-error.json"

    def fail(_config: Any) -> dict[str, Any]:
        raise CandidateDetectorControlsInputError("bad identity")

    monkeypatch.setattr(module, "evaluate_candidate_detector_controls", fail)

    exit_code = module.main(
        [
            "--model",
            "model.onnx",
            "--manifest",
            "manifest.json",
            "--frame",
            "frame.bgr24",
            "--annotation",
            "annotation.json",
            "--width",
            "4",
            "--height",
            "4",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 1
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["status"] == "input_contract_error"
    assert document["gate_passed"] is False
    assert document["production_approved"] is False


def test_cli_persists_runtime_failure_without_promoting_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module()
    output = tmp_path / "runtime-error.json"

    def fail(_config: Any) -> dict[str, Any]:
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(module, "evaluate_candidate_detector_controls", fail)

    exit_code = module.main(
        [
            "--model",
            "model.onnx",
            "--manifest",
            "manifest.json",
            "--frame",
            "frame.bgr24",
            "--annotation",
            "annotation.json",
            "--width",
            "4",
            "--height",
            "4",
            "--out",
            str(output),
        ]
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert document["status"] == "runtime_error"
    assert document["gate_passed"] is False
    assert document["model_identity"]["production_approved"] is False
