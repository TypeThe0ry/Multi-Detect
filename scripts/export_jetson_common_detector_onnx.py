#!/usr/bin/env python3
"""Export the pinned COCO80 YOLO26n head without embedded TopK/NMS nodes."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

PINNED_PT_SHA256 = "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, default=Path("yolo26n.pt"))
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("artifacts/models/coco-yolo26n-traditional/yolo26n-traditional.onnx"),
    )
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    checkpoint = args.checkpoint.resolve()
    destination = args.out.resolve()
    if _sha256(checkpoint) != PINNED_PT_SHA256:
        raise ValueError("YOLO26n checkpoint hash does not match the pinned public artifact")
    if destination.exists() and not args.force:
        raise FileExistsError(f"output already exists: {destination}; pass --force to replace it")
    if args.imgsz <= 0:
        raise ValueError("imgsz must be positive")

    try:
        import onnx
        import ultralytics
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError("install the repository model-training environment first") from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    staged_checkpoint = destination.with_suffix(".pt")
    shutil.copy2(checkpoint, staged_checkpoint)
    exported = Path(
        YOLO(str(staged_checkpoint)).export(
            format="onnx",
            imgsz=args.imgsz,
            opset=17,
            simplify=True,
            dynamic=False,
            nms=False,
            end2end=False,
            batch=1,
        )
    ).resolve()
    if exported != destination:
        if destination.exists():
            destination.unlink()
        exported.replace(destination)

    graph = onnx.load(str(destination))
    onnx.checker.check_model(graph)
    operators = {node.op_type for node in graph.graph.node}
    if operators.intersection({"TopK", "NonMaxSuppression"}):
        raise RuntimeError("traditional export unexpectedly contains TopK or embedded NMS")
    opsets = {item.domain: item.version for item in graph.opset_import}
    if opsets.get("") != 17:
        raise RuntimeError(f"unexpected ONNX opset: {opsets}")
    output_shape = [
        dimension.dim_value for dimension in graph.graph.output[0].type.tensor_type.shape.dim
    ]
    if output_shape != [1, 84, 8400]:
        raise RuntimeError(f"unexpected YOLO26n raw output shape: {output_shape}")

    artifact_sha256 = _sha256(destination)
    metadata = {
        "schema_version": 1,
        "source_checkpoint": checkpoint.name,
        "source_sha256": PINNED_PT_SHA256,
        "artifact": destination.name,
        "artifact_sha256": artifact_sha256,
        "ultralytics_version": ultralytics.__version__,
        "onnx_version": onnx.__version__,
        "opset": 17,
        "input_shape": [1, 3, args.imgsz, args.imgsz],
        "output_shape": output_shape,
        "end2end": False,
        "embedded_nms": False,
        "topk_nodes": 0,
    }
    sidecar = destination.with_suffix(destination.suffix + ".export.json")
    sidecar.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{artifact_sha256}  {destination.name}\n",
        encoding="ascii",
    )
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
