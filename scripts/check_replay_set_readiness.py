#!/usr/bin/env python3
"""Check replay-set deployment and accuracy readiness and emit a readiness report.

The command is intentionally diagnostic: it re-verifies the hash-bound
manifest, counts real truth/prediction content and, when supplied, reruns the
encoded accuracy request.  It returns ``2`` for a pending/blocked gate unless
``--report-only`` is used.
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

from multidetect.replay_set_readiness import check_replay_set_readiness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="replay-set.manifest.json")
    parser.add_argument(
        "--accuracy-report",
        type=Path,
        help="optional hash-bound evaluate-replay-set report to re-compute and verify",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="return zero while the replay-set is pending or blocked",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    document = check_replay_set_readiness(
        args.manifest,
        accuracy_report_path=args.accuracy_report,
    )
    payload = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if document["gate_passed"] or args.report_only else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
