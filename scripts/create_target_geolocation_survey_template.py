#!/usr/bin/env python3
"""Create a fill-in field-truth worksheet from a verified target-event replay.

The generated worksheet contains only fresh/qualified estimated GPS events.
Ground-truth latitude and longitude remain empty until an independent
surveyor/RTK receiver fills them.  The worksheet is not accuracy evidence and
never changes runtime, calibration or flight-control state.
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

from multidetect.autonomy.serialization import AutonomyContractError  # noqa: E402
from multidetect.geolocation_survey_template import (  # noqa: E402
    build_geolocation_survey_template,
    write_geolocation_survey_template,
    write_template_summary,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "replay_manifest",
        type=Path,
        help="verified target-event replay manifest.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="worksheet output (.csv, .json or .jsonl)",
    )
    parser.add_argument("--target-id", help="only include events for one target_id")
    parser.add_argument("--start-s", type=float, help="inclusive capture-time lower bound")
    parser.add_argument("--end-s", type=float, help="inclusive capture-time upper bound")
    parser.add_argument(
        "--summary-output",
        type=Path,
        help="optional JSON selection summary sidecar",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing worksheet/summary files explicitly",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_geolocation_survey_template(
            args.replay_manifest,
            target_id=args.target_id,
            start_s=args.start_s,
            end_s=args.end_s,
        )
        output_path = write_geolocation_survey_template(
            args.output,
            result,
            overwrite=args.overwrite,
        )
        summary_path = None
        if args.summary_output is not None:
            summary_path = write_template_summary(
                args.summary_output,
                result,
                output_path=output_path,
                overwrite=args.overwrite,
            )
    except (AutonomyContractError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"target geolocation survey template failed: {exc}", file=sys.stderr)
        return 2

    summary = result.summary()
    summary["output"] = str(output_path)
    summary["output_format"] = output_path.suffix.lower().lstrip(".")
    if summary_path is not None:
        summary["summary_output"] = str(summary_path)
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True))
    # An empty worksheet is useful diagnostic output, but it is not a usable
    # field handoff.  Keep the command fail-closed so CI cannot mistake it for
    # a ready-to-survey capture.
    return 0 if result.eligible_event_count > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
