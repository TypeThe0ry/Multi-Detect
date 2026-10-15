#!/usr/bin/env python3
"""Run one CUDA forward/backward memory preflight; never trains or saves weights."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def run_preflight(
    *, torch_module, model_factory, weights: Path, imgsz: int, requested_batch: int
) -> dict:
    """Run two optimizer steps, retrying batch one after a batch-two OOM.

    ``torch_module`` and ``model_factory`` are injectable so the control flow can
    be tested without importing CUDA or Ultralytics.
    """
    torch = torch_module
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; preflight requires a CUDA-capable lead host")
    device = torch.device("cuda:0")
    last_error = None
    for batch in (requested_batch, 1):
        try:
            torch.cuda.reset_peak_memory_stats(device)
            model = model_factory(str(weights)).model.to(device).train()
            optimizer = torch.optim.SGD(model.parameters(), lr=1e-4)
            for _ in range(2):
                optimizer.zero_grad(set_to_none=True)
                dummy = torch.zeros((batch, 3, imgsz, imgsz), device=device)
                result = model(dummy)
                tensors = result if isinstance(result, (list, tuple)) else [result]
                loss = sum(x.float().mean() for x in tensors if torch.is_tensor(x))
                loss.backward()
                optimizer.step()
            props = torch.cuda.get_device_properties(device)
            return {
                "status": "passed", "imgsz": imgsz, "batch": batch,
                "peak_gpu_memory_bytes": torch.cuda.max_memory_allocated(device),
                "peak_gpu_reserved_bytes": torch.cuda.max_memory_reserved(device),
                "device_total_memory_bytes": props.total_memory,
                "recommended_1280_batch": batch,
            }
        except RuntimeError as exc:
            last_error = exc
            if batch == 1 or "out of memory" not in str(exc).lower():
                raise
            torch.cuda.empty_cache()
    raise last_error  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    import torch
    from ultralytics import YOLO
    report = run_preflight(
        torch_module=torch, model_factory=YOLO, weights=args.weights,
        imgsz=args.imgsz, requested_batch=args.batch,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
