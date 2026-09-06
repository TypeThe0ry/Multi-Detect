"""Generate or verify fixed-model predictions for a reviewed deployment replay set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replay_prediction_run import run_replay_predictions, verify_replay_predictions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("reviewed", type=Path)
    run.add_argument("output", type=Path)
    run.add_argument("--model", type=Path, required=True)
    run.add_argument("--model-manifest", type=Path, required=True)
    run.add_argument(
        "--model-format",
        choices=("auto", "post_nms_nx6", "ultralytics_raw"),
        default="auto",
    )
    run.add_argument("--confidence-threshold", type=float, default=0.25)
    run.add_argument("--iou-threshold", type=float, default=0.45)
    run.add_argument("--maximum-detections", type=int, default=300)
    run.add_argument("--provider", action="append", default=[])
    run.add_argument("--warmup-iterations", type=int, default=1)
    verify = commands.add_parser("verify")
    verify.add_argument("output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        report = run_replay_predictions(
            args.reviewed,
            args.output,
            model_path=args.model,
            model_manifest_path=args.model_manifest,
            model_format=args.model_format,
            confidence_threshold=args.confidence_threshold,
            iou_threshold=args.iou_threshold,
            maximum_detections=args.maximum_detections,
            providers=tuple(args.provider),
            warmup_iterations=args.warmup_iterations,
        )
    else:
        report = verify_replay_predictions(args.output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = ["build_parser", "main"]
