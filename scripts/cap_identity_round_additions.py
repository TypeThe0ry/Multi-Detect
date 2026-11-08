#!/usr/bin/env python3
"""Cap round additions to a target share of the replay base, stratified by source."""
from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stratum(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(row.get("world") or "unknown"),
        str(row.get("confusable_pair_type") or row.get("task_category") or "unknown"),
        str(row.get("view") or "unknown"),
        str(row.get("label_type") or "unknown"),
        str((row.get("assistant_target") or {}).get("decision", "unknown")),
    )


def cap_rows(rows: list[dict[str, Any]], limit: int, seed: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[stratum(row)].append(row)
    keys = sorted(groups)
    exact = {key: limit * len(groups[key]) / len(rows) for key in keys}
    quota = {key: math.floor(exact[key]) for key in keys}
    remainder = sorted(keys, key=lambda key: (-(exact[key] - quota[key]), key))
    for key in remainder[: limit - sum(quota.values())]:
        quota[key] += 1
    rng = random.Random(seed)
    selected = []
    for key in keys:
        pool = sorted(groups[key], key=lambda row: str(row.get("sample_id", "")))
        rng.shuffle(pool)
        selected.extend(pool[:quota[key]])
    return sorted(selected, key=lambda row: str(row.get("sample_id", "")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-train", type=Path, required=True)
    parser.add_argument("--addition-fraction", type=float, default=0.2,
                        help="target share of additions in base plus additions")
    parser.add_argument("--seed", type=int, default=20261107)
    args = parser.parse_args()
    if not 0 < args.addition_fraction < 1:
        parser.error("--addition-fraction must be between 0 and 1")
    if args.output.exists():
        parser.error(f"output already exists: {args.output}")
    base_count = len(load_rows(args.base_train))
    source = load_rows(args.input / "train.jsonl")
    limit = math.floor(base_count * args.addition_fraction / (1 - args.addition_fraction))
    selected = cap_rows(source, limit, args.seed)
    args.output.mkdir(parents=True)
    counts = {}
    for split in ("train", "val", "test"):
        rows = selected if split == "train" else load_rows(args.input / f"{split}.jsonl")
        (args.output / f"{split}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        counts[split] = len(rows)
    print(json.dumps({"base_train_rows": base_count, "input_addition_train_rows": len(source),
                      "train_addition_limit": limit, "output_rows": counts,
                      "addition_fraction_target": args.addition_fraction}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
