#!/usr/bin/env python3
"""Join an independent full telemetry JSONL sidecar into a raw replay.

The sidecar must contain one canonical ``kind=telemetry`` event per line with
the complete ``VehicleTelemetry`` mapping.  Samples are matched to frames by
latest-at-or-before timestamp and stale coverage is rejected.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.replay_telemetry_join import (
    DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
    join_raw_replay_telemetry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "recording_manifest",
        type=Path,
        help="verified source recording.manifest.json",
    )
    parser.add_argument(
        "telemetry_jsonl",
        type=Path,
        help="canonical telemetry-only JSONL sidecar",
    )
    parser.add_argument("output", type=Path, help="new joined recording directory")
    parser.add_argument(
        "--recording-id",
        help="new recording identity (defaults to source-id suffix)",
    )
    parser.add_argument(
        "--maximum-telemetry-age-s",
        type=float,
        default=DEFAULT_MAXIMUM_TELEMETRY_AGE_S,
        help="maximum frame-to-latest-telemetry age; default: 1.0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = join_raw_replay_telemetry(
            args.recording_manifest,
            args.telemetry_jsonl,
            args.output,
            recording_id=args.recording_id,
            maximum_telemetry_age_s=args.maximum_telemetry_age_s,
        )
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"raw replay telemetry join failed: {exc}") from exc
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
