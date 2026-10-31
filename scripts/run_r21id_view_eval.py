#!/usr/bin/env python3
"""Run paired r18/r21id GGUF inference on r21id test views, then report identity gates."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.host_paths import executable_name
DATASET = ROOT / "artifacts/r21id-round/pipeline/r15-additions/test.jsonl"
OUTPUT = ROOT / "artifacts/evaluation/r21id-analysis-20261027/view-eval"
MODELS = {
    "r18": ROOT / "artifacts/models/slow-system-lora-v2-r18-multi-merged-gguf/slow-system-v2r18multi-merged-Q5_K_M.gguf",
    "r21id": ROOT / "artifacts/r21id-round/pipeline/merged-gguf/slow-system-Q5_K_M.gguf",
}
MMPROJ = ROOT / "artifacts/cache/hf-vlm-gguf/models--Qwen--Qwen3-VL-2B-Instruct-GGUF/snapshots/52d6c8ffea26cc873ac5ad116f8631268d7eb503/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf"
LLAMA_SERVER = ROOT / "artifacts/cache/llama-cpp/b11552" / executable_name("llama-server")


def commands() -> list[list[str]]:
    result = []
    for port, (name, model) in zip((9933, 9934), MODELS.items(), strict=True):
        result.append([
            sys.executable, "scripts/eval_slow_system.py",
            "--dataset", str(DATASET.parent), "--data-root", str(ROOT),
            "--backend", "gguf", "--identity-only", "--fast-identity",
            "--crop-policy", "candidate_zoom_v2", "--gguf-model", str(model),
            "--mmproj", str(MMPROJ), "--llama-server", str(LLAMA_SERVER),
            "--port", str(port), "--output", str(OUTPUT / f"{name}-predictions.json"),
        ])
    result.append([
        sys.executable, "scripts/eval_r21id_identity.py",
        "--dataset", str(DATASET),
        "--baseline", str(OUTPUT / "r18-predictions.json"),
        "--candidate", str(OUTPUT / "r21id-predictions.json"),
        "--output-json", str(OUTPUT / "identity-report.json"),
        "--output-md", str(OUTPUT / "identity-report.md"),
    ])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print commands without starting inference")
    parser.add_argument("--check", action="store_true", help="validate input files and command construction only")
    args = parser.parse_args()
    missing = [path for path in (DATASET, *MODELS.values(), MMPROJ, LLAMA_SERVER) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"missing required file: {path}", file=sys.stderr)
        return 2
    if args.check or args.dry_run:
        for command in commands():
            print(shlex.join(command))
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for command in commands():
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
