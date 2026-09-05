#!/usr/bin/env python3
"""Create a clearly labelled positive synthetic replay-set evaluator fixture.

The command copies an already verified six-stratum raw replay-set, writes
deterministic flame/smoke truth and matching predictions, and emits an
accuracy report.  The result remains ``synthetic_demo`` and therefore cannot
open the deployment-domain or final release gate.
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

from multidetect.product_version import load_product_version
from multidetect.replay_set_fixture import create_positive_replay_set


def build_parser() -> argparse.ArgumentParser:
    product = load_product_version()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source",
        type=Path,
        help="verified six-stratum replay-set root used as the raw recording source",
    )
    parser.add_argument(
        "output",
        type=Path,
        help="new output directory; it must not already exist",
    )
    parser.add_argument(
        "--dataset-id",
        default="multidetect-synthetic-positive-evaluator",
        help="dataset identity written to the derived manifest",
    )
    parser.add_argument(
        "--dataset-version",
        default=product.release_label,
        help=f"dataset version (default: {product.release_label})",
    )
    parser.add_argument(
        "--latency-base-ms",
        type=float,
        default=4.0,
        help="deterministic first-frame inference latency in milliseconds",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = create_positive_replay_set(
        args.source,
        args.output,
        dataset_id=args.dataset_id,
        dataset_version=args.dataset_version,
        latency_base_ms=args.latency_base_ms,
    )
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
