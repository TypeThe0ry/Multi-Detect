#!/usr/bin/env python3
"""Run the GPU-only r9p proposal cache, then the offline SFT builder.

This wrapper is intentionally read-only with respect to ``artifacts/r21id-r9pa``.
It writes a fresh output directory containing ``proposals.jsonl``, builder rows,
and ``run.json``.  Use the VLM environment and one CUDA device.  The r9pa pilot
contains roughly 900 RGB frames; at 1280 px a 20 GB RTX 3080 is ample (about
6 GB peak model/inference memory expected) and the proposal pass is normally
3–8 minutes, followed by a CPU builder pass of roughly 2–5 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _repo_root(path: Path) -> Path:
    resolved = path.resolve()
    for parent in (resolved, *resolved.parents):
        if (parent / ".git").exists():
            return parent
    return ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, default=Path("artifacts/r21id-r9pa/episodes"))
    parser.add_argument("--index", type=Path, default=Path("configs/scenarios/generated-r21id-r9p/index.json"))
    parser.add_argument("--output", type=Path, default=Path(".tmp/r9p-gpu-annotate"))
    parser.add_argument("--weights", type=Path, default=None)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--confidence", type=float, default=0.05)
    args = parser.parse_args(argv)

    campaign = args.campaign.resolve()
    index = args.index.resolve()
    output = args.output.resolve()
    if not campaign.is_dir():
        raise SystemExit(f"campaign directory does not exist: {campaign}")
    if not index.is_file():
        raise SystemExit(f"index does not exist: {index}")
    if output.exists():
        raise SystemExit(f"output already exists; choose a new directory: {output}")
    if output == campaign or campaign in output.parents:
        raise SystemExit("output must be outside the read-only r9pa campaign")

    source_root = _repo_root(campaign)
    output.mkdir(parents=True)
    proposals = output / "proposals.jsonl"
    sft_output = output / "sft"
    python = sys.executable
    env = os.environ.copy()
    env.update({"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", "PYTHONUTF8": "1", "TMPDIR": str(ROOT / ".tmp")})
    weights = args.weights or (ROOT / "artifacts/experiments/yoloe-v3-onboard-1280/train/weights/best.pt")
    cache_command = [
        python, str(ROOT / "scripts/cache_onboard_proposals.py"),
        "--campaign", str(campaign), "--output", str(proposals),
        "--weights", str(weights), "--device", args.device,
        "--imgsz", str(args.imgsz), "--confidence", str(args.confidence),
        "--source-root", str(source_root),
    ]
    builder_command = [
        python, str(ROOT / "scripts/build_slow_system_sft.py"),
        "--campaign", str(campaign), "--index", str(index), "--output", str(sft_output),
        "--proposals", str(proposals), "--confusable-negatives", "--task-text-variants",
        "--crop-policy", "none", "--min-visible-fraction", "0.3", "--confirm-min-side-px", "8",
    ]
    (output / "run.json").write_text(json.dumps({
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "campaign": str(campaign), "index": str(index), "source_root": str(source_root),
        "device": args.device, "imgsz": args.imgsz, "confidence": args.confidence,
        "expected_peak_gpu_memory_gb": 6,
        "expected_wall_time": "3-8 min proposal cache + 2-5 min CPU builder",
        "cache_command": cache_command, "builder_command": builder_command,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run(cache_command, cwd=ROOT, env=env, check=True)
    subprocess.run(builder_command, cwd=ROOT, env=env, check=True)
    print(json.dumps({"output": str(output), "proposals": str(proposals), "sft": str(sft_output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
