"""Command-line interface for physical-field recording source attestations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .deployment_recording_attestation import (
    SOURCE_TYPES,
    derive_deployment_recording_time_window,
    verify_deployment_recording_attestation,
    write_deployment_recording_attestation,
)
from .replay_set import REPLAY_SET_STRATA


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording_root", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--stratum", choices=REPLAY_SET_STRATA)
    parser.add_argument("--captured-by")
    parser.add_argument("--capture-device-id")
    parser.add_argument("--capture-site")
    parser.add_argument("--source-type", choices=SOURCE_TYPES)
    parser.add_argument("--started-at-utc")
    parser.add_argument("--ended-at-utc")
    parser.add_argument(
        "--derive-times-from-recording",
        action="store_true",
        help=(
            "derive manifest start and minimum valid end; this does not infer or attest "
            "physical provenance"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.verify:
            if any(
                value is not None
                for value in (
                    args.captured_by,
                    args.capture_device_id,
                    args.capture_site,
                    args.source_type,
                    args.started_at_utc,
                    args.ended_at_utc,
                    True if args.derive_times_from_recording else None,
                )
            ):
                parser.error("--verify only accepts the optional --stratum constraint")
            result = verify_deployment_recording_attestation(
                args.recording_root, expected_stratum=args.stratum
            )
        else:
            if args.derive_times_from_recording:
                if args.started_at_utc is not None or args.ended_at_utc is not None:
                    parser.error(
                        "--derive-times-from-recording cannot be combined with explicit times"
                    )
                derived = derive_deployment_recording_time_window(args.recording_root)
                args.started_at_utc = derived["started_at_utc"]
                args.ended_at_utc = derived["minimum_ended_at_utc"]
            required = {
                "--stratum": args.stratum,
                "--captured-by": args.captured_by,
                "--capture-device-id": args.capture_device_id,
                "--capture-site": args.capture_site,
                "--source-type": args.source_type,
                "--started-at-utc": args.started_at_utc,
                "--ended-at-utc": args.ended_at_utc,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                parser.error("creation requires " + ", ".join(missing))
            result = write_deployment_recording_attestation(
                args.recording_root,
                stratum=args.stratum,
                captured_by=args.captured_by,
                capture_device_id=args.capture_device_id,
                capture_site=args.capture_site,
                source_type=args.source_type,
                started_at_utc=args.started_at_utc,
                ended_at_utc=args.ended_at_utc,
            )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"deployment recording attestation: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


__all__ = ["build_parser", "main"]
