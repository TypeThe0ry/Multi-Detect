#!/usr/bin/env python3
"""Rebalance slow-system SFT identity positives by task category."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sft_task_category_balance import balance_task_categories  # noqa: E402


def _world_categories(repo_root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for index in sorted((repo_root / "configs" / "scenarios").glob("*/index.json")):
        try:
            document = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for world in document.get("worlds", []):
            combo = world.get("target_combo") or {}
            category = combo.get("category")
            if world.get("world_id") and category:
                result[str(world["world_id"])] = str(category)
    return result


def _text_category(row: dict) -> str | None:
    text = str(row.get("task_text") or "").casefold()
    for category in (
        "backpack", "traffic cone", "fire hydrant", "mailbox", "electrical box",
        "cardboard box", "ambulance", "bus", "car", "dumpster", "fire truck",
        "jersey barrier", "person", "pickup", "postbox", "stop sign", "suv",
    ):
        if category in text:
            return category.replace(" ", "_")
    return None


def category_for_row(row: dict, world_categories: dict[str, str]) -> str | None:
    return str(row["task_category"]) if row.get("task_category") else (
        world_categories.get(str(row.get("world"))) or _text_category(row)
    )


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def rebalance_dataset(
    input_dir: Path, output_dir: Path | None, *, min_ratio: float, max_upsample: int,
    seed: int, repo_root: Path, report_path: Path | None = None,
) -> dict:
    world_categories = _world_categories(repo_root)
    train = read_rows(input_dir / "train.jsonl")
    def category(row: dict) -> str | None:
        return category_for_row(row, world_categories)
    balanced, categories = balance_task_categories(
        train, min_ratio=min_ratio, max_upsample=max_upsample, seed=seed, category_for_row=category,
    )
    report = {
        "schema_version": 1,
        "input_dir": str(input_dir),
        "min_ratio": min_ratio,
        "max_upsample": max_upsample,
        "seed": seed,
        "rows_before": len(train),
        "rows_after": len(balanced),
        "categories": categories,
    }
    if output_dir is not None:
        if output_dir.exists():
            raise FileExistsError(f"Output already exists: {output_dir}")
        output_dir.mkdir(parents=True)
        (output_dir / "train.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in balanced),
            encoding="utf-8", newline="\n",
        )
        for split in ("val", "test"):
            shutil.copyfile(input_dir / f"{split}.jsonl", output_dir / f"{split}.jsonl")
        original_manifest = input_dir / "manifest.json"
        manifest = (
            json.loads(original_manifest.read_text(encoding="utf-8"))
            if original_manifest.is_file() else {}
        )
        manifest["task_category_balance"] = report
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="\n",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--balance-task-categories", "--min-ratio", type=float, required=True, dest="min_ratio",
    )
    parser.add_argument("--max-upsample", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--report", type=Path, help="write a JSON report")
    parser.add_argument(
        "--dry-run", action="store_true", help="report counts without creating output",
    )
    args = parser.parse_args(argv)
    if args.dry_run:
        args.output_dir = None
    elif args.output_dir is None:
        parser.error("--output-dir is required unless --dry-run is set")
    report = rebalance_dataset(
        args.input_dir, args.output_dir, min_ratio=args.min_ratio, max_upsample=args.max_upsample,
        seed=args.seed, repo_root=Path(__file__).resolve().parents[1], report_path=args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
