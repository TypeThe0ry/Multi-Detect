#!/usr/bin/env python3
"""Fine tune YOLO26n on the SIM teacher dataset and quarantine an ONNX candidate.

The script deliberately keeps model promotion out of the training path.  The
export is the raw Ultralytics head (host side NMS), which is the contract used
by :class:`multidetect.vision.OnnxRawYoloDetector`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

SOURCE_SHA256 = "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="dataset data.yaml")
    parser.add_argument("--tag", required=True, help="candidate tag used in run and manifest names")
    parser.add_argument("--model", type=Path, default=Path("yolo26n.pt"))
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--device", default="0")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--project", type=Path, default=Path("artifacts/models"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--patience", type=int, default=20)
    parser.add_argument("--fraction", type=float, default=1.0)
    parser.add_argument("--cache", choices=("none", "disk", "ram"), default="none")
    parser.add_argument("--lr0", type=float, default=0.01)
    parser.add_argument("--lrf", type=float, default=0.01)
    parser.add_argument("--optimizer", default="auto")
    parser.add_argument("--freeze", type=int, default=0)
    parser.add_argument(
        "--manifest", type=Path, help="dataset manifest (defaults to sibling manifest.json)"
    )
    parser.add_argument("--manifest-output-dir", type=Path, default=Path("configs/models"))
    parser.add_argument("--allow-source-hash-mismatch", action="store_true")
    parser.add_argument(
        "--skip-export", action="store_true", help="only useful for mocked/unit runs"
    )
    return parser


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency is part of ultralytics.
        raise RuntimeError("PyYAML is required to read the Ultralytics data file") from exc
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"dataset YAML must contain a mapping: {path}")
    return value


def _class_names(data: dict[str, Any]) -> list[str]:
    names = data.get("names")
    if isinstance(names, dict):
        try:
            return [str(names[key]) for key in sorted(names, key=lambda key: int(key))]
        except (TypeError, ValueError):
            return [str(value) for _, value in sorted(names.items(), key=lambda item: str(item[0]))]
    if isinstance(names, list) and names and all(isinstance(item, str) for item in names):
        return list(names)
    raise ValueError("dataset YAML names must be a non-empty list or integer-keyed mapping")


def _dataset_manifest(path: Path, explicit: Path | None) -> tuple[Path | None, str | None]:
    candidate = explicit or path.with_name("manifest.json")
    if not candidate.is_file():
        candidates = sorted(path.parent.glob("*manifest*.json"))
        candidate = candidates[0] if candidates else candidate
    if not candidate.is_file():
        return None, None
    return candidate, sha256_file(candidate)


def _validate_args(args: argparse.Namespace) -> None:
    if not args.data.is_file():
        raise FileNotFoundError(f"dataset YAML does not exist: {args.data}")
    if not args.model.is_file():
        raise FileNotFoundError(f"source checkpoint does not exist: {args.model}")
    if args.epochs <= 0 or args.imgsz <= 0 or args.workers < 0 or args.freeze < 0:
        raise ValueError("epochs/imgsz must be positive; workers/freeze cannot be negative")
    if args.batch <= 0 or not 0.0 < args.fraction <= 1.0:
        raise ValueError("batch must be positive and fraction must be in (0, 1]")
    actual = sha256_file(args.model)
    if actual != SOURCE_SHA256 and not args.allow_source_hash_mismatch:
        raise ValueError(
            f"source checkpoint sha256 mismatch for {args.model}: {actual}; "
            f"expected {SOURCE_SHA256} (use --allow-source-hash-mismatch only for tests)"
        )


def _train(args: argparse.Namespace) -> tuple[Path, Path | None, dict[str, Any]]:
    try:
        import torch
        from ultralytics import YOLO
    except ImportError as exc:  # pragma: no cover - exercised only without training deps.
        raise RuntimeError("training requires CUDA PyTorch and ultralytics") from exc
    if str(args.device).lower() not in {"cpu", "-1"} and not torch.cuda.is_available():
        raise RuntimeError("CUDA training was requested but torch.cuda.is_available() is false")
    model = YOLO(str(args.model.resolve()))
    cache: bool | str = False if args.cache == "none" else args.cache
    # A campaign can legitimately contain only worlds assigned to ``train``
    # by the authoritative generated-world index.  Ultralytics attempts to
    # validate by default and emits misleading empty-dataset errors in that
    # case; keep validation enabled whenever the builder produced val images.
    val_images = args.data.parent / "images" / "val"
    has_val = any(
        path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
        for path in val_images.glob("*")
    ) if val_images.is_dir() else False
    train_data = str(args.data.resolve())
    if not has_val:
        # Ultralytics still constructs a validation loader during training.
        # Point it at train only for this explicitly diagnostic no-val smoke;
        # production runs should always provide an index-backed val split.
        no_val_data = _load_yaml(args.data)
        no_val_data["val"] = no_val_data.get("train")
        fallback_data = args.data.with_name(f"{args.data.stem}.trainval.yaml")
        lines = [f"path: {no_val_data.get('path', args.data.parent.resolve().as_posix())}"]
        lines.extend(f"{key}: {value}" for key, value in no_val_data.items() if key != "path")
        fallback_data.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        train_data = str(fallback_data.resolve())
    result = model.train(
        data=train_data,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=int(args.batch),
        device=args.device,
        workers=args.workers,
        cache=cache,
        project=str(args.project.resolve()),
        name=f"sim-detector-{args.tag}",
        exist_ok=False,
        seed=args.seed,
        deterministic=True,
        patience=args.patience,
        fraction=args.fraction,
        lr0=args.lr0,
        lrf=args.lrf,
        optimizer=args.optimizer,
        freeze=args.freeze,
        val=has_val,
        verbose=True,
    )
    save_dir = Path(result.save_dir)
    best = save_dir / "weights" / "best.pt"
    if not best.is_file():
        raise RuntimeError(f"training finished without weights/best.pt: {best}")
    exported: Path | None = None
    if not args.skip_export:
        export_result = YOLO(str(best)).export(
            format="onnx",
            imgsz=args.imgsz,
            batch=1,
            device=args.device,
            dynamic=False,
            simplify=True,
            opset=17,
            nms=False,
            end2end=False,
        )
        exported = Path(export_result)
        if not exported.is_file():
            raise RuntimeError(f"Ultralytics export did not produce an ONNX file: {exported}")
    return best, exported, {"save_dir": str(save_dir.resolve())}


def _write_manifest(
    args: argparse.Namespace,
    *,
    data: dict[str, Any],
    best: Path,
    onnx: Path | None,
    dataset_manifest: Path | None,
    dataset_sha: str | None,
) -> Path:
    classes = _class_names(data)
    output_dir = args.manifest_output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"sim_yolo26n_{args.tag}_candidate.json"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "model_id": f"sim-yolo26n-{args.tag}",
        "model_version": f"ultralytics-8.4.92-sim-{args.tag}",
        "status": "quarantined",
        "production_approved": False,
        "model_role": "sim_teacher_object_evidence",
        "source_checkpoint": str(args.model.resolve()),
        "source_checkpoint_sha256": sha256_file(args.model),
        "classes": [
            {"id": index, "source_name": name, "canonical_label": name}
            for index, name in enumerate(classes)
        ],
        "dataset": {
            "yaml": str(args.data.resolve()),
            "manifest": str(dataset_manifest.resolve()) if dataset_manifest else None,
            "manifest_sha256": dataset_sha,
        },
        "training": {
            "epochs": args.epochs,
            "image_size": args.imgsz,
            "batch": args.batch,
            "device": str(args.device),
            "seed": args.seed,
            "deterministic": True,
            "best_checkpoint": str(best.resolve()),
        },
        "input": {
            "sensor": "rgb",
            "color_space": "RGB",
            "tensor_layout": "NCHW",
            "dtype": "float32",
            "shape": [1, 3, args.imgsz, args.imgsz],
            "resize": "letterbox",
        },
        "output": {
            "native_export": {
                "format": "ultralytics_raw_xywh_class_scores",
                "nms_embedded": False,
            },
            "adapter_contract": {
                "format": "N_x_6",
                "fields": ["x1", "y1", "x2", "y2", "confidence", "class_id"],
                "box_format": "letterbox_xyxy_px",
            },
        },
        "export": {
            "artifact_format": "onnx",
            "artifact_path": str(onnx.resolve()) if onnx else None,
            "artifact_sha256": sha256_file(onnx) if onnx else None,
            "tool_versions": {"ultralytics": "8.4.92", "opset": 17},
        },
        "governance": {"production_approved": False, "reviewer": None, "reviewed_at_utc": None},
    }
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_args(args)
    data = _load_yaml(args.data)
    dataset_manifest, dataset_sha = _dataset_manifest(args.data, args.manifest)
    best, exported, details = _train(args)
    manifest_path = _write_manifest(
        args,
        data=data,
        best=best,
        onnx=exported,
        dataset_manifest=dataset_manifest,
        dataset_sha=dataset_sha,
    )
    print(
        json.dumps(
            {
                "event": "sim_detector_training_finished",
                "run": details,
                "best_checkpoint": str(best.resolve()),
                "onnx_model": str(exported.resolve()) if exported else None,
                "manifest": str(manifest_path.resolve()),
                "status": "quarantined",
                "production_approved": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
