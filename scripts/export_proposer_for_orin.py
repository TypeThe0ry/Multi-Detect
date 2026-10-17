#!/usr/bin/env python3
"""Export the onboard fixed-vocabulary YOLOE proposer offline on CPU."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def workspace_path(path: Path) -> Path:
    """Reject output paths outside this checkout, including symlink escapes."""
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError(f"Output must stay inside {ROOT}: {resolved}")
    return resolved


def local_asset(file: str | Path, weights_dir: Path) -> str:
    """Resolve a required asset without invoking Ultralytics download helpers."""
    path = Path(file)
    for candidate in (path, weights_dir / path.name):
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError(
        f"Offline asset missing: {file}; download size unknown (network disabled)"
    )


def no_network(*args, **kwargs):
    raise RuntimeError("Network access is disabled; no downloads or installs are allowed")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_predictions(actual, expected, class_count: int) -> dict:
    """Check box coordinates and scores separately at the proposal threshold scale."""
    import numpy as np

    if actual.shape != expected.shape or actual.ndim != 3 or actual.shape[1] != 4 + class_count:
        raise ValueError(f"Unexpected detection output shapes: {actual.shape}, {expected.shape}")
    np.testing.assert_allclose(actual[:, :4], expected[:, :4], rtol=1e-4, atol=0.01)
    np.testing.assert_allclose(actual[:, 4:], expected[:, 4:], rtol=1e-4, atol=1e-5)
    return {
        "provider": "CPUExecutionProvider",
        "input": "seed=0 uniform RGB float32 in [0,1]",
        "max_abs_box_error": float(np.max(np.abs(actual[:, :4] - expected[:, :4]))),
        "max_abs_score_error": float(np.max(np.abs(actual[:, 4:] - expected[:, 4:]))),
        "rtol": 1e-4,
        "box_atol": 0.01,
        "score_atol": 1e-5,
        "passed": True,
    }


def _frame_paths(frames_dir: Path, limit: int) -> list[Path]:
    if limit < 1:
        raise ValueError("comparison frame limit must be positive")
    paths = sorted(
        path
        for path in frames_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
    )
    if not paths:
        raise FileNotFoundError(f"no PNG/JPEG frames found under {frames_dir}")
    return paths[:limit]


def verify_frame_predictions(
    session,
    model,
    frames_dir: Path,
    *,
    image_size: int,
    class_count: int,
    limit: int,
) -> dict:
    """Compare raw proposal tensors on real recorded frames.

    The comparison is deliberately before NMS: each anchor is stable between
    PyTorch and ONNX, so its xywh box can be compared with IoU and its class
    score with an absolute error without introducing matching heuristics.
    """
    import numpy as np
    import torch
    from PIL import Image

    from scripts.orin_tensorrt_proposer import letterbox_rgb

    frame_metrics = []
    for path in _frame_paths(frames_dir, limit):
        rgb = np.asarray(Image.open(path).convert("RGB"))
        # letterbox_rgb follows the runtime BGR input contract.
        tensor, _scale, _pad = letterbox_rgb(rgb[:, :, ::-1], image_size)
        with torch.inference_mode():
            reference = model.model.float().eval()(torch.from_numpy(tensor))
        if isinstance(reference, tuple):
            reference = reference[0]
        expected = reference.detach().cpu().numpy()
        actual = session.run(None, {"images": tensor})[0]
        if actual.shape != expected.shape or actual.shape[1] != 4 + class_count:
            raise ValueError(f"unexpected frame output shape for {path}: {actual.shape}")
        expected_xyxy = _xywh_to_xyxy(expected[:, :4].transpose(0, 2, 1))
        actual_xyxy = _xywh_to_xyxy(actual[:, :4].transpose(0, 2, 1))
        expected_scores = expected[:, 4:].transpose(0, 2, 1)
        actual_scores = actual[:, 4:].transpose(0, 2, 1)
        iou = _box_iou(expected_xyxy, actual_xyxy)
        score_error = np.abs(expected_scores - actual_scores)
        frame_metrics.append(
            {
                "frame": str(path),
                "box_iou_mean": float(iou.mean()),
                "box_iou_min": float(iou.min()),
                "max_abs_score_error": float(score_error.max()),
                "mean_abs_score_error": float(score_error.mean()),
            }
        )
    return {
        "frame_count": len(frame_metrics),
        "frames": frame_metrics,
        "box_iou_mean": float(np.mean([row["box_iou_mean"] for row in frame_metrics])),
        "box_iou_min": float(np.min([row["box_iou_min"] for row in frame_metrics])),
        "max_abs_score_error": float(max(row["max_abs_score_error"] for row in frame_metrics)),
        "mean_abs_score_error": float(
            np.mean([row["mean_abs_score_error"] for row in frame_metrics])
        ),
        "iou_threshold": 0.995,
        "score_threshold": 1e-5,
        "passed": all(row["box_iou_min"] >= 0.995 for row in frame_metrics)
        and max(row["max_abs_score_error"] for row in frame_metrics) <= 1e-5,
    }


def _xywh_to_xyxy(values):
    import numpy as np

    result = np.empty_like(values)
    result[..., 0] = values[..., 0] - values[..., 2] / 2
    result[..., 1] = values[..., 1] - values[..., 3] / 2
    result[..., 2] = values[..., 0] + values[..., 2] / 2
    result[..., 3] = values[..., 1] + values[..., 3] / 2
    return result


def _box_iou(left, right):
    import numpy as np

    lt = np.maximum(left[..., :2], right[..., :2])
    rb = np.minimum(left[..., 2:], right[..., 2:])
    intersection = np.prod(np.maximum(rb - lt, 0), axis=-1)
    area_left = np.prod(np.maximum(left[..., 2:] - left[..., :2], 0), axis=-1)
    area_right = np.prod(np.maximum(right[..., 2:] - right[..., :2], 0), axis=-1)
    return intersection / np.maximum(area_left + area_right - intersection, 1e-12)


def export(args: argparse.Namespace) -> dict:
    """Bake the exact runtime vocabulary, export, and check the ONNX graph."""
    output_dir = workspace_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / ".cache"
    cache_dir.mkdir(exist_ok=True)
    (cache_dir / "ultralytics" / "Ultralytics").mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(cache_dir)
    for key, value in {
        "CUDA_VISIBLE_DEVICES": "-1",
        "YOLO_OFFLINE": "true",
        "YOLO_AUTOINSTALL": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "YOLO_CONFIG_DIR": str(cache_dir / "ultralytics"),
        "TORCH_HOME": str(cache_dir / "torch"),
        "HF_HOME": str(cache_dir / "huggingface"),
        "XDG_CACHE_HOME": str(cache_dir),
        "TEMP": str(cache_dir),
        "TMP": str(cache_dir),
    }.items():
        os.environ[key] = value

    required = ("torch", "ultralytics", "onnx", "onnxruntime", "clip")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            f"Missing offline dependencies: {', '.join(missing)}; "
            "download sizes unknown (no package index queried)"
        )
    if not args.weights.is_file():
        raise FileNotFoundError(f"Missing checkpoint: {args.weights}; download size unknown")
    text_encoder = Path(local_asset("mobileclip_blt.ts", args.weights_dir))

    with (
        patch("socket.socket.connect", no_network),
        patch("socket.create_connection", no_network),
    ):
        import onnx
        import torch
        from ultralytics import YOLOE
        from ultralytics.utils import SETTINGS, downloads

        from scripts.onboard_policy import VOCABULARY

        torch.set_num_threads(args.threads)
        for key in (
            "sync",
            "wandb",
            "comet",
            "clearml",
            "neptune",
            "mlflow",
            "hub",
            "openvino_msg",
        ):
            if key in SETTINGS:
                dict.__setitem__(SETTINGS, key, False)
        with (
            patch.object(
                downloads,
                "attempt_download_asset",
                lambda file, *a, **kw: local_asset(file, args.weights_dir),
            ),
            patch.object(downloads, "safe_download", no_network),
        ):
            model = YOLOE(str(args.weights.resolve())).to("cpu")
            head = model.model.model[-1]
            if hasattr(head, "lrpc"):
                raise ValueError(
                    "A prompt-free LRPC checkpoint does not match the onboard text-prompt proposer"
                )
            vocabulary = list(VOCABULARY)
            model.set_classes(vocabulary, model.get_text_pe(vocabulary))
            head_type, task = type(head).__name__, model.task
            if task != "detect":
                raise ValueError(
                    f"Expected the onboard detection-only checkpoint, found task={task}"
                )
            # Exporter derives the output path from pt_path, rather than output_dir.
            model.model.pt_path = str(output_dir / f"proposer-{args.imgsz}.pt")
            exported = Path(
                model.export(
                    format="onnx",
                    device="cpu",
                    imgsz=args.imgsz,
                    batch=1,
                    opset=args.opset,
                    dynamic=False,
                    half=False,
                    simplify=False,
                    nms=False,
                )
            ).resolve()
        if not exported.is_file() or not exported.is_relative_to(output_dir):
            raise RuntimeError(f"Exporter did not produce an ONNX file in {output_dir}: {exported}")
        graph = onnx.load(str(exported))
        onnx.checker.check_model(graph)
        inputs = {
            item.name: [dim.dim_value for dim in item.type.tensor_type.shape.dim]
            for item in graph.graph.input
        }
        if inputs != {"images": [1, 3, args.imgsz, args.imgsz]}:
            raise ValueError(f"Unexpected ONNX input shape: {inputs}")
        import numpy as np
        import onnxruntime as ort

        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = args.threads
        session = ort.InferenceSession(
            str(exported), sess_options=session_options, providers=["CPUExecutionProvider"]
        )
        image = np.random.default_rng(0).random((1, 3, args.imgsz, args.imgsz), dtype=np.float32)
        with torch.inference_mode():
            reference = model.model.float().eval()(torch.from_numpy(image))
        if isinstance(reference, tuple):
            reference = reference[0]
        if not isinstance(reference, torch.Tensor):
            raise ValueError(
                "Expected the onboard detection head to return a raw prediction tensor"
            )
        expected = reference.cpu().numpy()
        actual = session.run(None, {"images": image})[0]
        numerical_check = verify_predictions(actual, expected, len(vocabulary))
        frame_check = None
        if args.comparison_frames_dir is not None:
            frame_check = verify_frame_predictions(
                session,
                model,
                args.comparison_frames_dir,
                image_size=args.imgsz,
                class_count=len(vocabulary),
                limit=args.comparison_frame_limit,
            )
        report = {
            "status": "exported_and_checked",
            "device": "cpu",
            "weights": str(args.weights.resolve()),
            "weights_bytes": args.weights.stat().st_size,
            "weights_sha256": sha256_file(args.weights),
            "text_encoder_bytes": text_encoder.stat().st_size,
            "vocabulary": vocabulary,
            "head": head_type,
            "task": task,
            "prompt_mode": (
                "fixed onboard text embeddings fused into export; no runtime text encoder"
            ),
            "onnx": str(exported),
            "bytes": exported.stat().st_size,
            "sha256": sha256_file(exported),
            "opset": {entry.domain or "ai.onnx": entry.version for entry in graph.opset_import},
            "inputs": inputs,
            "outputs": {
                item.name: [
                    dim.dim_value or dim.dim_param for dim in item.type.tensor_type.shape.dim
                ]
                for item in graph.graph.output
            },
            "precision": "float32; build TensorRT with --fp16 on the target Orin",
            "nms": False,
            "cpu_numerical_check": numerical_check,
            "frame_consistency": frame_check,
            "versions": {
                name: importlib.metadata.version(name) for name in required if name != "clip"
            },
        }
        report_path = exported.with_suffix(".export.json")
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--weights",
        type=Path,
        default=ROOT / "artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt",
    )
    parser.add_argument(
        "--weights-dir",
        type=Path,
        default=ROOT / "weights",
        help="Local mobileclip_blt.ts directory",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/exports/orin-proposer")
    parser.add_argument("--imgsz", type=int, choices=(960, 1280), default=1280)
    parser.add_argument("--opset", type=int, default=17)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--comparison-frames-dir",
        type=Path,
        help="Optional recorded PNG/JPEG directory for real-frame PyTorch/ONNX comparison",
    )
    parser.add_argument(
        "--comparison-frame-limit",
        type=int,
        default=32,
        help="Number of real frames to compare (recommended 20-50)",
    )
    args = parser.parse_args(argv)
    if args.threads <= 0:
        parser.error("--threads must be positive")
    if args.comparison_frame_limit <= 0:
        parser.error("--comparison-frame-limit must be positive")
    return args


def main() -> int:
    try:
        print(json.dumps(export(parse_args()), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"Export stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
