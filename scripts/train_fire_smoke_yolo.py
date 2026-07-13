from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train a fire/smoke detector for interface HIL.")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--model", default="yolo26n.pt")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=float, default=0.70)
    parser.add_argument("--device", default="0")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cache", choices=("none", "disk", "ram"), default="none")
    parser.add_argument("--project", type=Path, default=Path("artifacts/training"))
    parser.add_argument("--name", default="fire-smoke-yolo26n")
    parser.add_argument("--fraction", type=float, default=1.0)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--export-onnx", action="store_true")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--lr0", type=float, default=0.01)
    parser.add_argument("--lrf", type=float, default=0.01)
    parser.add_argument("--optimizer", default="auto")
    parser.add_argument("--warmup-epochs", type=float, default=3.0)
    parser.add_argument("--freeze", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.data.is_file():
        raise FileNotFoundError(f"dataset YAML does not exist: {args.data}")
    if args.epochs <= 0 or args.imgsz <= 0 or args.workers < 0:
        raise ValueError("epochs/imgsz must be positive and workers cannot be negative")
    if not 0.0 < args.fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    if args.lr0 <= 0.0 or not 0.0 < args.lrf <= 1.0:
        raise ValueError("lr0 must be positive and lrf must be in (0, 1]")
    if args.warmup_epochs < 0.0 or args.freeze < 0:
        raise ValueError("warmup-epochs and freeze cannot be negative")
    if args.pilot:
        args.epochs = min(args.epochs, 3)
        args.fraction = min(args.fraction, 0.10)

    try:
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError(
            "training dependencies are missing; install CUDA PyTorch and ultralytics"
        ) from exc
    if str(args.device) != "cpu" and not torch.cuda.is_available():
        raise RuntimeError("CUDA training was requested but torch.cuda.is_available() is false")

    project = args.project.resolve()
    if args.resume is not None:
        if not args.resume.is_file():
            raise FileNotFoundError(f"resume checkpoint does not exist: {args.resume}")
        model = YOLO(str(args.resume.resolve()))
        result = model.train(resume=True)
    else:
        model = YOLO(args.model)
        result = model.train(
            data=str(args.data.resolve()),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device=args.device,
            workers=args.workers,
            cache=False if args.cache == "none" else args.cache,
            project=str(project),
            name=args.name,
            exist_ok=False,
            seed=42,
            deterministic=True,
            patience=max(10, min(25, args.epochs // 2)),
            save_period=5,
            fraction=args.fraction,
            lr0=args.lr0,
            lrf=args.lrf,
            optimizer=args.optimizer,
            warmup_epochs=args.warmup_epochs,
            freeze=args.freeze,
        )
    save_dir = Path(result.save_dir)
    best = save_dir / "weights" / "best.pt"
    if not best.is_file():
        raise RuntimeError("training finished without weights/best.pt")
    exported = None
    if args.export_onnx:
        exported = YOLO(str(best)).export(
            format="onnx",
            imgsz=args.imgsz,
            batch=1,
            dynamic=False,
            simplify=True,
        )
    print(
        json.dumps(
            {
                "event": "fire_smoke_training_finished",
                "save_dir": str(save_dir.resolve()),
                "best_checkpoint": str(best.resolve()),
                "onnx_model": str(Path(exported).resolve()) if exported else None,
                "pilot": args.pilot,
                "dataset_fraction": args.fraction,
                "accuracy_approved": False,
                "hardware_control_enabled": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
