#!/usr/bin/env python3
"""Calibrate per-class confidence thresholds from a hash-bound replay-set.

This command is advisory: a non-zero exit code means that the replay-set is
not yet ready for promotion (for example, a SIM fixture is synthetic or an
independent review has not been recorded).  Use ``--report-only`` while
collecting field data.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.replay_set_calibration import (
    calibrate_replay_set_thresholds,
    parse_current_thresholds,
    parse_threshold_grid,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="hash-bound replay-set.manifest.json")
    parser.add_argument(
        "--thresholds",
        default="0.10:0.90:0.02",
        help="threshold grid as start:stop:step or comma-separated values",
    )
    parser.add_argument(
        "--current-thresholds",
        default="flame=0.72,smoke=0.60",
        help="existing per-class thresholds used for the comparison report",
    )
    parser.add_argument("--minimum-recall", type=float, default=0.70)
    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument(
        "--stability-tolerance",
        type=float,
        default=0.10,
        help="maximum per-class threshold range considered stable across strata",
    )
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--reviewer")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero while collecting pending or synthetic replay data",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = calibrate_replay_set_thresholds(
        args.manifest,
        thresholds=parse_threshold_grid(args.thresholds),
        current_thresholds=parse_current_thresholds(args.current_thresholds),
        iou_threshold=args.iou_threshold,
        beta=args.beta,
        minimum_recall=args.minimum_recall,
        stability_tolerance=args.stability_tolerance,
        reviewed=args.reviewed,
        reviewer=args.reviewer,
    )
    payload = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if document["promotion_ready"] or args.report_only else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
