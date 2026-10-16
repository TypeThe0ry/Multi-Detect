#!/usr/bin/env python3
"""Run the real round trainer on a tiny synthetic dataset and measure CUDA memory."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def write_tiny_dataset(root: Path, count: int = 8) -> Path:
    """Create an isolated YOLO dataset; no campaign or truth artifact is read."""
    for split in ("train", "val"):
        (root / "images" / split).mkdir(parents=True, exist_ok=True)
        (root / "labels" / split).mkdir(parents=True, exist_ok=True)
        for index in range(count):
            image = np.zeros((64, 64, 3), dtype=np.uint8)
            cv2.rectangle(image, (8, 8), (24, 28), (255, 255, 255), -1)
            cv2.rectangle(image, (36, 34), (56, 56), (128, 128, 128), -1)
            name = f"synthetic-{index:03d}.jpg"
            cv2.imwrite(str(root / "images" / split / name), image)
            (root / "labels" / split / name.replace(".jpg", ".txt")).write_text(
                "0 0.25 0.28125 0.25 0.3125\n0 0.71875 0.703125 0.3125 0.34375\n",
                encoding="utf-8",
            )
    data = root / "data.yaml"
    data.write_text(
        f"path: {root.resolve()}\ntrain: images/train\nval: images/val\nnames:\n  0: object\n",
        encoding="utf-8",
    )
    return data


def run_training_preflight(
    *, weights: Path, imgsz: int, requested_batch: int, output_root: Path,
    holdout_classes: str = "stop_sign,postbox,pickup", run=subprocess.run,
) -> dict:
    """Run the production trainer and retry batch one only after CUDA OOM."""
    tiny_root = output_root / "tiny-dataset"
    data = write_tiny_dataset(tiny_root)
    for batch in (requested_batch, 1):
        run_dir = output_root / f"train-batch-{batch}"
        memory_path = run_dir / "cuda-memory.json"
        command = [
            sys.executable, str(ROOT / "scripts/train_open_vocab_detector.py"),
            "--data", str(data), "--holdout-classes", holdout_classes,
            "--holdout-policy", "drop-images", "--out", str(run_dir),
            "--weights", str(weights), "--epochs", "1", "--imgsz", str(imgsz),
            "--batch", str(batch), "--mode", "full", "--device", "0",
            "--workers", "0", "--preflight-memory-out", str(memory_path),
        ]
        try:
            completed = run(
                command, cwd=ROOT, env={**os.environ, "CUDA_VISIBLE_DEVICES": "0"},
                capture_output=True, text=True, check=True,
            )
        except subprocess.CalledProcessError as exc:
            error_text = f"{exc.stdout or ''}{exc.stderr or ''}".lower()
            if batch != 1 and "out of memory" in error_text:
                shutil.rmtree(run_dir, ignore_errors=True)
                continue
            raise RuntimeError(exc.stderr or exc.stdout) from exc
        metrics = json.loads(memory_path.read_text(encoding="utf-8"))
        shutil.rmtree(run_dir, ignore_errors=True)
        return {
            "status": "passed", "imgsz": imgsz, "batch": batch,
            "trainer": "scripts/train_open_vocab_detector.py", **metrics,
            "recommended_1280_batch": batch, "synthetic_dataset": str(tiny_root),
            "command": command, "stdout_tail": completed.stdout[-1000:],
        }
    raise RuntimeError("CUDA OOM at batch 2 and batch 1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--holdout-classes", default="stop_sign,postbox,pickup")
    args = parser.parse_args(argv)
    report = run_training_preflight(
        weights=args.weights, imgsz=args.imgsz, requested_batch=args.batch,
        output_root=args.out.parent / (args.out.stem + "-work"),
        holdout_classes=args.holdout_classes,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
