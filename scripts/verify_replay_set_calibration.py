#!/usr/bin/env python3
"""Independently verify a hash-bound replay-set threshold calibration report.

The verifier reloads the referenced replay-set, checks both manifest digests,
and re-runs the exact calibration request encoded in the report.  It returns
zero only when the report is internally consistent with the current files;
the report's own ``promotion_ready`` value remains an independent advisory
result and is never upgraded by verification.
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

from multidetect.replay_set_calibration import verify_replay_set_calibration_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="threshold-calibration report JSON")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="optional replay-set manifest override when the report was copied",
    )
    parser.add_argument("--prediction-run-receipt", type=Path)
    parser.add_argument("--accuracy-report", type=Path)
    parser.add_argument("--out", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = verify_replay_set_calibration_report(
        args.report,
        manifest_path=args.manifest,
        prediction_run_receipt=args.prediction_run_receipt,
        accuracy_report_path=args.accuracy_report,
    )
    payload = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if document["gate_passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
