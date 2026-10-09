"""Derive a VTOL campaign with an explicitly supplied shared ranker.

Only the ranker block is replaced; task and runtime inputs remain byte-stable
JSON values from the source campaign.  This makes the cross-airframe policy
identity decision reproducible without hand-editing a large scenario file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from multidetect.autonomy.task_ranker import TaskRanker


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def derive(*, source: Path, ranker_path: Path, output: Path) -> None:
    if output.exists():
        raise ValueError(f"refusing to overwrite: {output}")
    campaign = _object(source.resolve(strict=True))
    ranker = _object(ranker_path.resolve(strict=True))
    TaskRanker.from_mapping(ranker)
    if campaign.get("schema_version") != 1 or not isinstance(campaign.get("tasks"), list):
        raise ValueError("source campaign schema is invalid")
    if any(not isinstance(item, dict) for item in campaign["tasks"]):
        raise ValueError("source campaign contains a non-object task")
    campaign["ranker"] = ranker
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(campaign, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ranker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        derive(source=args.source, ranker_path=args.ranker, output=args.output)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
