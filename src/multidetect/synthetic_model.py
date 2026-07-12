from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .model_manifest import (
    create_candidate_model_manifest,
    write_candidate_model_manifest,
)


class SyntheticModelDependencyError(RuntimeError):
    """Raised when the optional ONNX model-construction dependency is unavailable."""


def _require_onnx() -> Any:
    try:
        import onnx
    except ImportError as exc:  # pragma: no cover - dependency-specific.
        raise SyntheticModelDependencyError(
            "Install synthetic HIL model tools with: pip install -e '.[model-tools]'"
        ) from exc
    return onnx


def create_synthetic_hil_model_bundle(
    directory: str | Path,
    *,
    input_width: int = 640,
    input_height: int = 640,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    """Create a constant flame candidate model for interface HIL, never for detection claims."""

    if input_width <= 0 or input_height <= 0:
        raise ValueError("synthetic model input dimensions must be positive")
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    model_path = destination / "synthetic-fire-nx6-hil.onnx"
    manifest_path = destination / "synthetic-fire-nx6-hil.manifest.json"
    if not overwrite and (model_path.exists() or manifest_path.exists()):
        raise FileExistsError("synthetic HIL model bundle already exists")

    onnx = _require_onnx()
    helper = onnx.helper
    tensor_proto = onnx.TensorProto
    input_info = helper.make_tensor_value_info(
        "images",
        tensor_proto.FLOAT,
        [1, 3, input_height, input_width],
    )
    output_info = helper.make_tensor_value_info(
        "detections",
        tensor_proto.FLOAT,
        [1, 6],
    )
    constant = helper.make_tensor(
        name="constant_detection",
        data_type=tensor_proto.FLOAT,
        dims=[1, 6],
        vals=[0.25, 0.25, 0.55, 0.55, 0.95, 0.0],
    )
    node = helper.make_node(
        "Constant",
        inputs=[],
        outputs=["detections"],
        value=constant,
    )
    graph = helper.make_graph(
        [node],
        "multi-detect-synthetic-hil",
        [input_info],
        [output_info],
    )
    model = helper.make_model(
        graph,
        producer_name="multi-detect-synthetic-hil",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    model.ir_version = min(model.ir_version, 10)
    onnx.checker.check_model(model)

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination,
        prefix=f".{model_path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        onnx.save_model(model, str(temporary))
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, model_path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    manifest = create_candidate_model_manifest(
        model_path,
        model_id="synthetic-fire-nx6-hil",
        model_version="constant-interface-test-v1",
        class_names=("fire", "smoke"),
        input_width=input_width,
        input_height=input_height,
        output_coordinates="normalized_xyxy",
        source_description="locally generated constant-output interface HIL model",
    )
    manifest["status"] = "synthetic_hil"
    manifest["synthetic_hil_only"] = True
    manifest["prohibited_uses"].extend(
        [
            "operational_fire_detection",
            "model_accuracy_claim",
            "field_deployment",
        ]
    )
    manifest["governance"]["notes"] = [
        "This model emits the same flame box for every input frame.",
        "It validates software interfaces only and can never be production approved.",
    ]
    write_candidate_model_manifest(manifest_path, manifest, overwrite=overwrite)
    return model_path, manifest_path


__all__ = [
    "SyntheticModelDependencyError",
    "create_synthetic_hil_model_bundle",
]
