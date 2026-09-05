#!/usr/bin/env python3
"""Reproduce and verify Gazebo camera-to-WGS84 acceptance evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.gazebo_target_geolocation_acceptance import (  # noqa: E402
    verify_gazebo_target_geolocation_evidence,
    write_evidence_exclusive,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--allow-failed-gate",
        action="store_true",
        help="verify integrity/reproduction without requiring a positive acceptance gate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = verify_gazebo_target_geolocation_evidence(
        args.evidence,
        repository_root=args.repository_root,
        require_gate_passed=not args.allow_failed_gate,
    )
    write_evidence_exclusive(args.out, result)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["passed"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
