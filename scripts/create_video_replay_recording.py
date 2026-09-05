#!/usr/bin/env python3
"""Create an integrity-bound raw replay recording from a video file.

The generated recording is camera-only and keeps navigation/GPS unknown.  It
is therefore suitable for replay/evaluator plumbing, but does not claim field
link, GNSS, geolocation, HIL or REAL acceptance.
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

from multidetect.video_replay_recording import (
    DEFAULT_AIRFRAME_PROFILE_HASH,
    DEFAULT_START_TIME_UTC,
    create_video_replay_recording,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path, help="recorded video readable by OpenCV")
    parser.add_argument("output", type=Path, help="new output directory")
    parser.add_argument("--recording-id")
    parser.add_argument(
        "--airframe-profile-hash",
        default=DEFAULT_AIRFRAME_PROFILE_HASH,
        help="64-hex profile identity (defaults to an explicit camera-only zero identity)",
    )
    parser.add_argument("--started-at-utc", default=DEFAULT_START_TIME_UTC)
    parser.add_argument(
        "--fps",
        type=float,
        help="override the video container FPS when metadata is missing or unreliable",
    )
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--frame-limit", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = create_video_replay_recording(
            args.video,
            args.output,
            recording_id=args.recording_id,
            airframe_profile_hash=args.airframe_profile_hash,
            started_at_utc=args.started_at_utc,
            fps=args.fps,
            frame_stride=args.frame_stride,
            frame_limit=args.frame_limit,
        )
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"video replay recording failed: {exc}") from exc
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
