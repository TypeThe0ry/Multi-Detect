"""Inspect a verified target-event replay bundle without exposing media bytes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.target_event_replay import (
    TargetEvent,
    load_target_event_replay_manifest,
)


def _event_summary(event: TargetEvent) -> dict[str, object]:
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "frame_id": event.frame_id,
        "target_id": event.target_id,
        "target_label": event.target_label,
        "captured_at_s": event.captured_at_s,
        "gps": event.gps.to_mapping(),
        # These are derived from the bound monotonic capture clock.  Exposing
        # them in the metadata-only inspector makes stale/unknown GPS evidence
        # visible without opening or copying any media bytes.
        "gps_age_s": event.gps_age_s,
        "gps_freshness": event.gps_freshness,
        "ranging": event.ranging.to_mapping(),
        "media": {
            "video": event.video.relative_path if event.video is not None else None,
            "depth": event.depth.relative_path if event.depth is not None else None,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and inspect a local Multi-Detect target-event replay bundle. "
            "Only metadata and relative media paths are printed."
        )
    )
    parser.add_argument("manifest", type=Path, help="path to the bundle manifest.json")
    parser.add_argument("--target-id", help="only include events for this target")
    parser.add_argument("--start-s", type=float, help="inclusive capture-time lower bound")
    parser.add_argument("--end-s", type=float, help="inclusive capture-time upper bound")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle = load_target_event_replay_manifest(args.manifest)
        events = [
            _event_summary(event)
            for event in bundle.iter_events(
                target_id=args.target_id,
                start_s=args.start_s,
                end_s=args.end_s,
            )
        ]
        print(
            json.dumps(
                {"summary": bundle.summary(), "events": events},
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
        )
        return 0
    except (AutonomyContractError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
