#!/usr/bin/env python3
"""Create a strict six-stratum detector replay-set manifest.

Expected layout below ``ROOT``::

    day/recording.manifest.json
    day/ground-truth.jsonl
    ... (night, haze, reflection, smoke_only, no_fire)

``predictions.jsonl`` is optional per stratum.  Missing strata can be recorded
while a field collection is in progress with ``--allow-incomplete``; the
resulting manifest keeps ``gate_passed=false`` until every required stratum is
present and explicitly marked as deployment-domain/reviewed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Keep the repository wrapper runnable from a clean checkout.  When this file
# is invoked as ``python scripts/create_replay_set_manifest.py`` Python starts
# with ``scripts/`` on ``sys.path`` and the sibling ``src/`` tree is otherwise
# invisible.  Add only this repository's source directory for direct script
# execution; installed-package imports retain their normal resolution.
if __package__ in {None, ""}:
    _repository_source = Path(__file__).resolve().parents[1] / "src"
    if _repository_source.is_dir() and str(_repository_source) not in sys.path:
        sys.path.insert(0, str(_repository_source))

from multidetect.replay_set import (
    REPLAY_SET_SOURCE_KINDS,
    REPLAY_SET_STRATA,
    build_replay_set_manifest,
    replay_set_validation_document,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory containing one subdirectory per stratum")
    parser.add_argument(
        "--out", type=Path, required=True, help="manifest path directly inside ROOT"
    )
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--class-names", default="flame,smoke")
    parser.add_argument(
        "--source-kind",
        choices=REPLAY_SET_SOURCE_KINDS,
        default="lab_recording",
    )
    parser.add_argument(
        "--annotations-reviewed",
        action="store_true",
        help="mark every present entry as reviewed; use only after an independent review",
    )
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="write a pending manifest while one or more strata are still absent",
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    class_names = tuple(item.strip() for item in args.class_names.split(",") if item.strip())
    manifest = build_replay_set_manifest(
        args.root,
        args.out,
        dataset_id=args.dataset_id,
        dataset_version=args.dataset_version,
        class_names=class_names,
        required_strata=REPLAY_SET_STRATA,
        source_kind=args.source_kind,
        annotations_reviewed=args.annotations_reviewed,
        allow_incomplete=args.allow_incomplete,
        overwrite=args.force,
    )
    print(
        json.dumps(
            replay_set_validation_document(manifest, manifest_path=args.out),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if manifest.is_complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
