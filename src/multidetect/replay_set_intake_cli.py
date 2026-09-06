"""Installed command entry point for deployment replay-set intake."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replay_set import REPLAY_SET_STRATA
from .replay_set_intake import prepare_deployment_replay_set, verify_deployment_replay_intake


def _source(value: str) -> tuple[str, Path]:
    stratum, separator, path = value.partition("=")
    if not separator or stratum not in REPLAY_SET_STRATA or not path:
        raise argparse.ArgumentTypeError("SOURCE must be one of STRATUM=RECORDING_DIRECTORY")
    return stratum, Path(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage six deployment recordings for independent annotation and evaluation."
    )
    parser.add_argument("--output", type=Path, help="new staging directory; overwrite is refused")
    parser.add_argument("--dataset-id")
    parser.add_argument("--dataset-version")
    parser.add_argument("--source", action="append", type=_source, default=[])
    parser.add_argument("--verify", type=Path, help="verify an existing intake directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verify is not None:
        if args.output or args.source or args.dataset_id or args.dataset_version:
            parser.error("--verify cannot be combined with intake arguments")
        report = verify_deployment_replay_intake(args.verify)
    else:
        if not args.output or not args.dataset_id or not args.dataset_version:
            parser.error("--output, --dataset-id and --dataset-version are required")
        sources = dict(args.source)
        if len(sources) != len(args.source):
            parser.error("each --source stratum must appear exactly once")
        report = prepare_deployment_replay_set(
            sources,
            args.output,
            dataset_id=args.dataset_id,
            dataset_version=args.dataset_version,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = ["build_parser", "main"]
