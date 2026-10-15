#!/usr/bin/env python3
"""Run one CUDA forward/backward memory preflight; never trains or saves weights."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--batch", type=int, default=2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    import torch
    from ultralytics import YOLO

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; preflight requires a CUDA-capable lead host")
    device = torch.device("cuda:0")
    torch.cuda.reset_peak_memory_stats(device)
    model = YOLO(str(args.weights)).model.to(device).train()
    dummy = torch.zeros((args.batch, 3, args.imgsz, args.imgsz), device=device)
    result = model(dummy)
    if isinstance(result, (list, tuple)):
        loss = sum(x.float().mean() for x in result if torch.is_tensor(x))
    else:
        loss = result.float().mean()
    loss.backward()
    report = {
        "status": "passed",
        "imgsz": args.imgsz,
        "batch": args.batch,
        "peak_gpu_memory_bytes": torch.cuda.max_memory_allocated(device),
        "recommended_1280_batch": args.batch,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
