#!/usr/bin/env python3
"""Prepare a hash-bound fire/smoke model review envelope.

The command never edits the candidate manifest and exits non-zero until the
candidate, model-check evidence, deployment accuracy evidence and all four
governance decisions are complete.  ``--report-only`` is useful while the
real deployment recordings are still being collected.
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

from multidetect.model_review import (  # noqa: E402
    MODEL_REVIEW_REQUIRED_REVIEWS,
    MODEL_REVIEW_STATUSES,
    prepare_model_review_bundle,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="quarantined fire/smoke model manifest")
    parser.add_argument("model_artifact", type=Path, help="ONNX/TensorRT artifact")
    parser.add_argument("--model-check-report", type=Path)
    parser.add_argument("--accuracy-report", type=Path)
    parser.add_argument("--reviewer")
    parser.add_argument("--reviewed-at-utc")
    for name in MODEL_REVIEW_REQUIRED_REVIEWS:
        parser.add_argument(
            f"--{name.replace('_', '-')}-review",
            choices=MODEL_REVIEW_STATUSES,
            default="pending",
        )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero while the review bundle is pending or blocked",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    decisions = {name: getattr(args, f"{name}_review") for name in MODEL_REVIEW_REQUIRED_REVIEWS}
    bundle = prepare_model_review_bundle(
        args.manifest,
        args.model_artifact,
        args.out,
        model_check_report_path=args.model_check_report,
        accuracy_report_path=args.accuracy_report,
        review_decisions=decisions,
        reviewer=args.reviewer,
        reviewed_at_utc=args.reviewed_at_utc,
        overwrite=args.force,
    )
    payload = json.dumps(bundle.document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    print(payload, end="")
    return 0 if bundle.production_approval_eligible or args.report_only else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
