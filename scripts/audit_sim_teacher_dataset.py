#!/usr/bin/env python3
"""Offline integrity and coverage audit for SIM teacher episodes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.sim_teacher.recording import DatasetAuditError, audit_dataset  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--max-sim-skew-ms",
        type=float,
        default=250.0,
        help="maximum absolute live frame/PX4 simulation-clock skew (default: 250 ms)",
    )
    parser.add_argument(
        "--pose-motion-threshold-m",
        type=float,
        default=1.0,
        help="pose movement that requires a newer live frame stamp (default: 1 m)",
    )
    args = parser.parse_args(argv)
    try:
        report = audit_dataset(
            args.dataset.resolve(),
            max_sim_skew_ms=args.max_sim_skew_ms,
            pose_motion_threshold_m=args.pose_motion_threshold_m,
        )
    except (DatasetAuditError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
