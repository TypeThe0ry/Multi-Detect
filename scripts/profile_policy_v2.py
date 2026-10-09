"""Profile a policy-v2 ONNX artifact on CUDA and CPU execution providers."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _parameter_count(checkpoint: Path | None) -> int | None:
    if checkpoint is None or not checkpoint.is_file():
        return None
    try:
        import torch

        payload = torch.load(checkpoint, map_location="cpu", weights_only=False)
        state = payload.get("state_dict", payload)
        return int(sum(value.numel() for value in state.values() if hasattr(value, "numel")))
    except Exception:
        return None


def profile(
    path: str | Path,
    *,
    iterations: int = 100,
    warmup: int = 10,
    checkpoint: Path | None = None,
) -> dict[str, Any]:
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("onnxruntime and numpy are required") from exc
    model_path = Path(path)
    available = ort.get_available_providers()
    providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in available]
    if not providers:
        raise RuntimeError(f"no CPU/CUDA execution provider available: {available}")
    rng = np.random.default_rng(0)
    feeds = {
        "state_history": rng.standard_normal((1, 16, 128), dtype=np.float32),
        "memory": rng.standard_normal((1, 5, 16, 16), dtype=np.float32),
        "action_mask": np.ones((1, 4), dtype=np.float32),
    }
    report: dict[str, Any] = {
        "schema": "multidetect.policy-v2-profile-v1",
        "model": str(model_path),
        "available_providers": list(available),
        "parameter_count": _parameter_count(checkpoint),
        "iterations": iterations,
        "warmup": warmup,
        "providers": {},
    }
    for provider in providers:
        session = ort.InferenceSession(str(model_path), providers=[provider])
        for _ in range(max(0, warmup)):
            session.run(None, feeds)
        samples = []
        for _ in range(max(1, iterations)):
            start = time.perf_counter_ns()
            session.run(None, feeds)
            samples.append((time.perf_counter_ns() - start) / 1e6)
        samples.sort()
        report["providers"][provider] = {
            "p50_ms": statistics.median(samples),
            "p95_ms": samples[min(len(samples) - 1, int(0.95 * (len(samples) - 1)))],
            "p99_ms": samples[min(len(samples) - 1, int(0.99 * (len(samples) - 1)))],
            "mean_ms": statistics.fmean(samples),
            "count": len(samples),
        }
    return report


profile_onnx = profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("onnx", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = profile(
        args.onnx,
        iterations=args.iterations,
        warmup=args.warmup,
        checkpoint=args.checkpoint,
    )
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

