#!/usr/bin/env python3
"""Convert a verified GNSS capture JSON report into replay telemetry JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.gnss_capture_sidecar import create_gnss_replay_sidecar


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "capture_report", type=Path, help="verified capture_gnss_samples JSON report"
    )
    parser.add_argument("output", type=Path, help="new RawReplayTelemetrySample JSONL sidecar")
    parser.add_argument(
        "--time-origin-monotonic-s",
        type=float,
        help="capture monotonic timestamp mapped to replay t=0; default: first raw GPS sample",
    )
    parser.add_argument(
        "--time-offset-s",
        type=float,
        default=0.0,
        help="finite replay-time shift after origin normalization; default: 0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = create_gnss_replay_sidecar(
            args.capture_report,
            args.output,
            time_origin_monotonic_s=args.time_origin_monotonic_s,
            time_offset_s=args.time_offset_s,
        )
    except (AutonomyContractError, FileExistsError, OSError, ValueError) as exc:
        raise SystemExit(f"GNSS replay sidecar conversion failed: {exc}") from exc
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
