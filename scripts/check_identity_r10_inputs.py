#!/usr/bin/env python3
"""Fail-closed CPU preflight for the r21id-r10 queue inputs.

This check validates only immutable inputs needed before a builder/mix/train
queue can be admitted. It never writes artifacts and never starts a model.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def _required_file(path: Path, *, nonempty: bool = True) -> Path:
    if not path.is_file():
        raise SystemExit(f"admission rejected: missing required file: {path}")
    if nonempty and path.stat().st_size == 0:
        raise SystemExit(f"admission rejected: required file is empty: {path}")
    return path


def _required_dir(path: Path) -> Path:
    if not path.is_dir():
        raise SystemExit(f"admission rejected: missing required directory: {path}")
    return path


def _jsonl_nonempty(path: Path) -> int:
    count = 0
    try:
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"admission rejected: invalid JSONL {path}:{line_no}: {exc}") from exc
                if not isinstance(value, dict):
                    raise SystemExit(f"admission rejected: JSONL row is not an object: {path}:{line_no}")
                count += 1
    except OSError as exc:
        raise SystemExit(f"admission rejected: cannot read {path}: {exc}") from exc
    if count == 0:
        raise SystemExit(f"admission rejected: required JSONL has no rows: {path}")
    return count


def validate(*, base_dataset: Path, index: Path, campaign: Path, proposals: Path) -> dict[str, object]:
    for split in ("train", "val", "test"):
        _required_file(base_dataset / f"{split}.jsonl")
    _required_dir(campaign)
    _required_file(proposals)
    proposal_rows = _jsonl_nonempty(proposals)

    _required_file(index)
    try:
        document = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"admission rejected: cannot parse index {index}: {exc}") from exc
    worlds = document.get("worlds")
    if not isinstance(worlds, list) or not worlds:
        raise SystemExit(f"admission rejected: index has no worlds: {index}")
    splits = Counter(str(row.get("split")) for row in worlds)
    directions = Counter((str(row.get("r9r_direction")), str(row.get("split"))) for row in worlds)
    expected_splits = Counter({"train": 36, "val": 12})
    expected_directions = Counter({
        ("car->mailbox", "train"): 18,
        ("suv->postbox", "train"): 18,
        ("car->mailbox", "val"): 6,
        ("suv->postbox", "val"): 6,
    })
    if splits != expected_splits:
        raise SystemExit(f"admission rejected: unexpected world split counts {dict(splits)}; expected {dict(expected_splits)}")
    if directions != expected_directions:
        raise SystemExit(f"admission rejected: unexpected direction/split counts {dict(directions)}; expected {dict(expected_directions)}")
    world_ids = [str(row.get("world_id")) for row in worlds]
    if any(value in {"", "None"} for value in world_ids) or len(set(world_ids)) != len(world_ids):
        raise SystemExit("admission rejected: world_id values must be present and unique")
    return {
        "base_train_rows": _jsonl_nonempty(base_dataset / "train.jsonl"),
        "base_val_rows": _jsonl_nonempty(base_dataset / "val.jsonl"),
        "base_test_rows": _jsonl_nonempty(base_dataset / "test.jsonl"),
        "campaign": str(campaign),
        "index": str(index),
        "index_worlds": len(worlds),
        "index_split_counts": dict(sorted(splits.items())),
        "index_direction_split_counts": {f"{a}/{b}": n for (a, b), n in sorted(directions.items())},
        "proposals": str(proposals),
        "proposal_rows": proposal_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dataset", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--proposals", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(base_dataset=args.base_dataset, index=args.index, campaign=args.campaign, proposals=args.proposals), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
