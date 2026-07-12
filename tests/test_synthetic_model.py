from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.cli import main
from multidetect.model_manifest import ModelManifestError, verify_model_manifest
from multidetect.synthetic_model import create_synthetic_hil_model_bundle
from multidetect.vision import OnnxNx6Config, OnnxNx6Detector

onnx = pytest.importorskip("onnx")
np = pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
pytest.importorskip("cv2")

ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_hil_bundle_runs_real_onnxruntime_nx6_contract(tmp_path) -> None:
    model_path, manifest_path = create_synthetic_hil_model_bundle(tmp_path)
    onnx.checker.check_model(onnx.load(model_path))
    verified = verify_model_manifest(
        manifest_path,
        model_path,
        expected_class_names=("fire", "smoke"),
        expected_output_coordinates="normalized_xyxy",
    )
    detector = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=model_path,
            class_names=("fire", "smoke"),
            output_coordinates="normalized_xyxy",
            providers=("CPUExecutionProvider",),
            model_version=verified.model_version,
        )
    )

    (detection,) = detector.detect(np.zeros((640, 640, 3), dtype=np.uint8))

    assert verified.status == "synthetic_hil"
    assert verified.synthetic_hil_only is True
    assert verified.production_approved is False
    assert detection.label == "flame"
    assert detection.confidence == pytest.approx(0.95)
    assert detection.bbox.rounded() == (0.25, 0.25, 0.55, 0.55)


def test_synthetic_hil_manifest_cannot_pass_production_gate(tmp_path) -> None:
    model_path, manifest_path = create_synthetic_hil_model_bundle(tmp_path)

    with pytest.raises(ModelManifestError, match="not production approved"):
        verify_model_manifest(
            manifest_path,
            model_path,
            require_production_approved=True,
        )


def test_synthetic_hil_manifest_rejects_manual_production_approval(tmp_path) -> None:
    model_path, manifest_path = create_synthetic_hil_model_bundle(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["governance"]["production_approved"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ModelManifestError, match="can never be production approved"):
        verify_model_manifest(manifest_path, model_path)


def test_synthetic_model_init_cli_reports_explicit_nonproduction_status(tmp_path, capsys) -> None:
    assert main(["synthetic-model-init", "--out-dir", str(tmp_path)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["event"] == "synthetic_hil_model_created"
    assert result["synthetic_hil_only"] is True
    assert result["production_approved"] is False
    assert result["accuracy_validated"] is False
    assert result["hardware_control_enabled"] is False


def test_synthetic_model_bundle_requires_explicit_overwrite(tmp_path) -> None:
    create_synthetic_hil_model_bundle(tmp_path)

    with pytest.raises(FileExistsError, match="already exists"):
        create_synthetic_hil_model_bundle(tmp_path)


def test_live_camera_requires_explicit_synthetic_hil_opt_in(tmp_path, capsys) -> None:
    model_path, manifest_path = create_synthetic_hil_model_bundle(tmp_path)

    result = main(
        [
            "live-camera",
            str(ROOT / "configs/missions/fire_patrol.demo.json"),
            "--onnx-model",
            str(model_path),
            "--model-manifest",
            str(manifest_path),
            "--output-coordinates",
            "normalized_xyxy",
        ]
    )

    assert result == 1
    error = json.loads(capsys.readouterr().err)
    assert "explicit --allow-synthetic-hil-model" in error["message"]
