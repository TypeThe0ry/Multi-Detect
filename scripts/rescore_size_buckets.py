#!/usr/bin/env python3
"""离线重算慢模型候选框尺寸分层指标；绝不调用模型。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.eval_slow_system import size_bucket_metrics


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--eval", nargs="+", metavar="NAME=JSON", required=True)
    args = parser.parse_args(argv)
    dataset = read_jsonl(args.dataset)
    # test.jsonl stores paths relative to the Multi-Detect artifact root.
    artifact_root = args.dataset.resolve().parents[1]
    for row in dataset:
        image = row.get("image_path")
        if image and not Path(str(image)).is_absolute():
            row["image_path"] = str(artifact_root / str(image))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    combined: dict[str, Any] = {"dataset": str(args.dataset), "evaluations": {}}
    for item in args.eval:
        if "=" not in item:
            parser.error(f"--eval must be NAME=JSON: {item}")
        name, path_text = item.split("=", 1)
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
        rows = payload.get("rows")
        if not isinstance(rows, list):
            parser.error(f"evaluation has no rows: {path_text}")
        combined["evaluations"][name] = {
            "source": path_text,
            "size_buckets": size_bucket_metrics(rows, dataset),
        }
    output = args.output_dir / "size-buckets.json"
    output.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
