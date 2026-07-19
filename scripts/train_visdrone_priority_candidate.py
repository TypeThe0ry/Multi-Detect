#!/usr/bin/env python3
"""Fine-tune and export an aerial person/vehicle detector on VisDrone."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PINNED_PT_SHA256 = "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"
VISDRONE_RUNTIME_LABELS = (
    "person",
    "person",
    "bicycle",
    "car",
    "car",
    "truck",
    "motorcycle",
    "motorcycle",
    "bus",
    "motorcycle",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train a YOLO26n aerial priority-object candidate and export raw ONNX."
    )
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--data", default="VisDrone.yaml")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--name", default="yolo26n-visdrone-priority")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--imgsz", type=int, default=960)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--device", default="0")
    parser.add_argument("--fraction", type=float, default=1.0)
    parser.add_argument("--patience", type=int, default=10)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    checkpoint = args.checkpoint.resolve()
    project = args.project.resolve()
    _validate_args(args, checkpoint)
    try:
        import onnx
        import ultralytics
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError("install the repository model-training environment first") from exc

    project.mkdir(parents=True, exist_ok=True)
    run_directory = project / args.name
    launch = {
        "event": "visdrone_priority_training_started",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "source_checkpoint": str(checkpoint),
        "source_checkpoint_sha256": PINNED_PT_SHA256,
        "data": args.data,
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "fraction": args.fraction,
        "runtime_labels": list(VISDRONE_RUNTIME_LABELS),
        "flight_control_enabled": False,
    }
    (project / f"{args.name}.launch.json").write_text(
        json.dumps(launch, indent=2) + "\n", encoding="utf-8"
    )

    model_source = run_directory / "weights" / "last.pt" if args.resume else checkpoint
    if args.resume and not model_source.is_file():
        raise FileNotFoundError(f"resume checkpoint does not exist: {model_source}")
    model = YOLO(str(model_source))
    model.train(
        data=args.data,
        project=str(project),
        name=args.name,
        exist_ok=args.resume,
        resume=args.resume,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        workers=args.workers,
        device=args.device,
        fraction=args.fraction,
        patience=args.patience,
        close_mosaic=min(5, args.epochs),
        seed=0,
        deterministic=True,
        plots=True,
        save=True,
        save_period=1,
        val=True,
        verbose=True,
    )

    best = run_directory / "weights" / "best.pt"
    if not best.is_file():
        raise FileNotFoundError(f"training did not produce best checkpoint: {best}")
    best_model = YOLO(str(best))
    metrics = best_model.val(data=args.data, imgsz=args.imgsz, device=args.device, plots=True)
    # Validation fuses the loaded graph for inference; export from a clean reload so the
    # traditional one-to-many head is still available when end2end is disabled.
    export_model = YOLO(str(best))
    exported = Path(
        export_model.export(
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
    graph = onnx.load(str(exported))
    onnx.checker.check_model(graph)
    operators = {node.op_type for node in graph.graph.node}
    if operators.intersection({"TopK", "NonMaxSuppression"}):
        raise RuntimeError("export unexpectedly contains TopK or embedded NMS")
    summary = {
        "event": "visdrone_priority_training_completed",
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "ultralytics_version": ultralytics.__version__,
        "best_checkpoint": str(best),
        "best_checkpoint_sha256": _sha256(best),
        "raw_onnx": str(exported),
        "raw_onnx_sha256": _sha256(exported),
        "runtime_labels": list(VISDRONE_RUNTIME_LABELS),
        "metrics": _metrics_document(metrics),
        "production_approved": False,
        "flight_control_enabled": False,
    }
    summary_path = project / f"{args.name}.summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, separators=(",", ":")))
    return 0


def _validate_args(args: argparse.Namespace, checkpoint: Path) -> None:
    if not checkpoint.is_file():
        raise FileNotFoundError(f"checkpoint does not exist: {checkpoint}")
    if _sha256(checkpoint) != PINNED_PT_SHA256:
        raise ValueError("YOLO26n checkpoint hash does not match the pinned public artifact")
    if args.epochs <= 0 or args.imgsz <= 0 or args.workers < 0 or args.patience < 0:
        raise ValueError("epochs/imgsz must be positive and workers/patience non-negative")
    if args.batch == 0:
        raise ValueError("batch cannot be zero")
    if not 0.0 < args.fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")


def _metrics_document(metrics: Any) -> dict[str, float]:
    box = metrics.box
    return {
        "precision": float(box.mp),
        "recall": float(box.mr),
        "map50": float(box.map50),
        "map50_95": float(box.map),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
