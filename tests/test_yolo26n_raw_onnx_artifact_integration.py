from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "artifacts" / "models" / "coco-yolo26n-traditional" / "yolo26n-traditional.onnx"
PINNED_MODEL_SHA256 = "7ff7b97cad2fbb68d4fc6a74ed5dad67beb91f535e8cc25d5f0d97ae42d2e410"
pytestmark = pytest.mark.skipif(
    not MODEL.is_file(),
    reason="quarantined local ONNX candidate is not stored in Git",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@pytest.fixture(scope="module")
def ort_runtime() -> Iterator[tuple[Any, Any]]:
    np = pytest.importorskip("numpy", reason="real ONNX artifact test requires numpy")
    pytest.importorskip("cv2", reason="real detector preprocessing requires OpenCV")
    ort = pytest.importorskip(
        "onnxruntime",
        reason="real ONNX artifact test requires onnxruntime",
    )
    assert "CPUExecutionProvider" in ort.get_available_providers()
    session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"])
    try:
        yield np, session
    finally:
        del session


def test_yolo26n_raw_artifact_bytes_are_pinned() -> None:
    assert MODEL.is_file(), f"missing repository-pinned ONNX artifact: {MODEL}"
    assert MODEL.stat().st_size == 9_930_300
    assert _sha256(MODEL) == PINNED_MODEL_SHA256


def test_yolo26n_raw_artifact_exposes_expected_ort_io(
    ort_runtime: tuple[Any, Any],
) -> None:
    _, session = ort_runtime
    inputs = session.get_inputs()
    outputs = session.get_outputs()

    assert session.get_providers() == ["CPUExecutionProvider"]
    assert len(inputs) == 1
    assert inputs[0].name == "images"
    assert inputs[0].shape == [1, 3, 640, 640]
    assert inputs[0].type == "tensor(float)"
    assert len(outputs) == 1
    assert outputs[0].name == "output0"
    assert outputs[0].shape == [1, 84, 8400]
    assert outputs[0].type == "tensor(float)"


def test_yolo26n_raw_detector_accepts_blank_and_nx6_adapter_rejects_raw_output(
    ort_runtime: tuple[Any, Any],
) -> None:
    np, session = ort_runtime
    from multidetect.perception_factory import COCO80_CLASS_NAMES
    from multidetect.vision import (
        OnnxNx6Config,
        OnnxNx6Detector,
        OnnxOutputContractError,
        OnnxRawYoloConfig,
        OnnxRawYoloDetector,
    )

    blank_bgr = np.zeros((640, 640, 3), dtype=np.uint8)
    raw_detector = OnnxRawYoloDetector(
        OnnxRawYoloConfig(
            model_path=MODEL,
            class_names=COCO80_CLASS_NAMES,
            input_width=640,
            input_height=640,
            confidence_threshold=0.25,
            providers=("CPUExecutionProvider",),
            model_version="v56-quarantined-candidate",
        ),
        session=session,
    )

    raw_detector.warmup(iterations=1)
    assert raw_detector.provider_names == ("CPUExecutionProvider",)
    assert raw_detector.detect(blank_bgr) == ()

    nx6_detector = OnnxNx6Detector(
        OnnxNx6Config(
            model_path=MODEL,
            class_names=COCO80_CLASS_NAMES,
            input_width=640,
            input_height=640,
            confidence_threshold=0.25,
            providers=("CPUExecutionProvider",),
        ),
        session=session,
    )
    with pytest.raises(OnnxOutputContractError, match=r"expected post-NMS Nx6.*\(84, 8400\)"):
        nx6_detector.detect(blank_bgr)
