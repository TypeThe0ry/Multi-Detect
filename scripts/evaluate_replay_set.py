#!/usr/bin/env python3
"""Evaluate a hash-bound six-stratum replay-set with the shared IoU evaluator.

This wrapper is intentionally usable from a clean checkout.  It emits a
machine-readable report and returns exit code 0 only after the caller supplies
an explicit independent-review acknowledgement and all configured thresholds
pass.  Use ``--report-only`` while collecting field predictions.
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

from multidetect.model_manifest import sha256_file
from multidetect.replay_set_evaluation import evaluate_replay_set


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="replay-set.manifest.json")
    parser.add_argument("--iou-threshold", type=float, default=0.5)
    parser.add_argument("--confidence-threshold", type=float, default=0.25)
    parser.add_argument("--minimum-precision", type=float)
    parser.add_argument("--minimum-recall", type=float)
    parser.add_argument("--maximum-false-alarm-frame-rate", type=float)
    parser.add_argument("--maximum-missed-detection-frame-rate", type=float)
    parser.add_argument("--maximum-latency-p95-ms", type=float)
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--reviewer")
    parser.add_argument(
        "--model-artifact",
        type=Path,
        help="optional ONNX/TensorRT artifact whose SHA-256 is bound to this report",
    )
    parser.add_argument("--model-id")
    parser.add_argument("--model-version")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--report-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        (args.model_id is not None or args.model_version is not None)
        and args.model_artifact is None
    ):
        raise ValueError("--model-artifact is required when model metadata is supplied")
    model_artifact_sha256 = None
    if args.model_artifact is not None:
        if not args.model_artifact.is_file():
            raise ValueError(f"model artifact does not exist: {args.model_artifact}")
        model_artifact_sha256 = sha256_file(args.model_artifact)
    document = evaluate_replay_set(
        args.manifest,
        iou_threshold=args.iou_threshold,
        confidence_threshold=args.confidence_threshold,
        minimum_precision=args.minimum_precision,
        minimum_recall=args.minimum_recall,
        maximum_false_alarm_frame_rate=args.maximum_false_alarm_frame_rate,
        maximum_missed_detection_frame_rate=args.maximum_missed_detection_frame_rate,
        maximum_latency_p95_ms=args.maximum_latency_p95_ms,
        reviewed=args.reviewed,
        reviewer=args.reviewer,
        model_artifact_sha256=model_artifact_sha256,
        model_id=args.model_id,
        model_version=args.model_version,
    )
    payload = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if document["accuracy_validated"] or args.report_only else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
