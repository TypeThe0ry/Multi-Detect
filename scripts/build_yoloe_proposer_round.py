#!/usr/bin/env python3
"""Prepare a YOLOE proposer round from r3-seg plus onboard-loop campaigns.

This is a thin, reproducible wrapper around ``build_sim_detector_dataset``.  It
keeps the old world split and segmentation box policy, adds the three DAgger
campaigns, and emits a compact old-vs-new category/size report.  It never
trains or imports torch/Ultralytics.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from build_sim_detector_dataset import _split_map, build_dataset  # noqa: E402

BUCKETS = ("<8", "8-16", "16-32", "32-64", ">=64")


def size_bucket(short_side: float) -> str:
    if short_side < 8:
        return "<8"
    if short_side < 16:
        return "8-16"
    if short_side < 32:
        return "16-32"
    if short_side < 64:
        return "32-64"
    return ">=64"


def manifest_histogram(manifest: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Return category x visible-box short-side counts from a builder manifest."""
    result: dict[str, dict[str, int]] = {}
    for category, values in manifest.get("counts", {}).get("category_size_histogram", {}).items():
        result[category] = {bucket: int(values.get(bucket, 0)) for bucket in BUCKETS}
    return result


def compare_histograms(
    old: dict[str, dict[str, int]], new: dict[str, dict[str, int]]
) -> dict[str, Any]:
    categories = sorted(set(old) | set(new))
    return {
        category: {
            bucket: {
                "old": old.get(category, {}).get(bucket, 0),
                "new": new.get(category, {}).get(bucket, 0),
            }
            for bucket in BUCKETS
        }
        for category in categories
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-campaign", type=Path, required=True)
    parser.add_argument("--campaign-root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--old-manifest", type=Path, required=True)
    parser.add_argument("--index", type=Path, action="append", required=True)
    parser.add_argument("--truth-root", action="append", type=Path, default=[])
    parser.add_argument("--min-box-px", type=float, default=2.0)
    parser.add_argument("--min-seg-area-px", type=int, default=1)
    parser.add_argument("--negative-ratio", type=float, default=0.15)
    parser.add_argument("--seed", type=int, default=20261015)
    parser.add_argument("--max-frames", type=int)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--preflight-weights", type=Path)
    parser.add_argument("--skip-cross-task-check", action="store_true")
    parser.add_argument(
        "--holdout-mode",
        choices=("historical-three", "generated-v2-six"),
        default="historical-three",
    )
    args = parser.parse_args(argv)
    from evaluate_yoloe_proposer_round import cross_task_split_check

    cross_task_split_check(_split_map(args.index), skip=args.skip_cross_task_check, required=True)
    if args.preflight:
        if args.preflight_weights is None:
            parser.error("--preflight requires --preflight-weights")
        from preflight_yoloe_proposer_round import main as preflight_main

        return preflight_main(
            [
                "--weights", str(args.preflight_weights), "--imgsz", "1280",
                "--batch", "2", "--out", str(args.output / "preflight.json")
            ]
        )
    holdout_assets = set()
    if args.holdout_mode == "generated-v2-six":
        holdout_assets = {
            "stop_sign",
            "suv",
            "walking_person",
            "pickup",
            "postbox",
            "standing_person",
        }
    campaigns = [args.old_campaign, *args.campaign_root]
    manifest = build_dataset(
        campaigns,
        args.output,
        index_path=args.index,
        min_box_px=args.min_box_px,
        min_seg_area_px=args.min_seg_area_px,
        negative_ratio=args.negative_ratio,
        seed=args.seed,
        label_source="segmentation",
        truth_roots=args.truth_root,
        max_frames=args.max_frames,
        holdout_assets=holdout_assets,
    )
    old = json.loads(args.old_manifest.read_text(encoding="utf-8"))
    report = {
        "output": str(args.output.resolve()),
        "policy": {
            "label_source": "segmentation",
            "min_box_px": args.min_box_px,
            "min_seg_area_px": args.min_seg_area_px,
            "holdout_mode": args.holdout_mode,
            "holdout_definition": (
                "historical r3 detector-class holdout (default; comparable with r3)"
                if args.holdout_mode == "historical-three"
                else "generated-v2 six asset-slug holdout from segmentation truth"
            ),
            "holdout_applied_at": (
                "trainer dataset view via --holdout-classes/--holdout-policy"
                if args.holdout_mode == "historical-three"
                else "builder image exclusion from segmentation asset slugs"
            ),
            "detector_holdout_classes": ["stop_sign", "postbox", "pickup"],
            "asset_holdout_slugs": sorted(holdout_assets),
        },
        "new_manifest": str((args.output / "manifest.json").resolve()),
        "old_manifest": str(args.old_manifest.resolve()),
        "old_counts": old.get("counts", {}),
        "new_counts": manifest.get("counts", {}),
        "dedupe": manifest.get("dedupe", {}),
        "heldout_asset_verification": {
            "remaining_train_val_instances": manifest.get("remaining_asset_instances", {}),
            "excluded_train_val_images": manifest.get("heldout_asset_images", {}),
        },
        "training_commands": {
            str(size): (
                f"python scripts/train_open_vocab_detector.py --data {args.output}/data.yaml "
                + ("--holdout-classes stop_sign,postbox,pickup --holdout-policy drop-images "
                   if args.holdout_mode == "historical-three" else "")
                + f"--out tmp/yoloe-v3-onboard-{size} --imgsz {size} --epochs 25 "
                + f"--batch {2 if size == 1280 else 4} --mode full --device 0 --workers 0"
            )
            for size in (1280, 960)
        },
        "category_size_histogram": compare_histograms(
            manifest_histogram(old), manifest_histogram(manifest)
        ),
    }
    (args.output / "round-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
