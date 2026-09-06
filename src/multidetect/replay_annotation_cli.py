"""CLI for deployment replay annotation workspace creation and finalization."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .replay_annotation_workspace import (
    create_replay_annotation_workspace,
    finalize_replay_annotation_workspace,
    inspect_replay_annotation_workspace,
    verify_finalized_replay_annotations,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("intake", type=Path)
    create.add_argument("workspace", type=Path)
    create.add_argument("--class-names", default="flame,smoke")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("intake", type=Path)
    inspect.add_argument("workspace", type=Path)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("intake", type=Path)
    finalize.add_argument("workspace", type=Path)
    finalize.add_argument("output", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        classes = tuple(item.strip() for item in args.class_names.split(",") if item.strip())
        report = create_replay_annotation_workspace(
            args.intake, args.workspace, class_names=classes
        )
    elif args.command == "inspect":
        report = inspect_replay_annotation_workspace(args.intake, args.workspace)
    elif args.command == "finalize":
        report = finalize_replay_annotation_workspace(args.intake, args.workspace, args.output)
    else:
        report = verify_finalized_replay_annotations(args.output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 2 if args.command == "inspect" and not report["finalization_ready"] else 0


__all__ = ["build_parser", "main"]
