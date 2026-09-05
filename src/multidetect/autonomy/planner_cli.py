"""Compile a resolved task/context pair into a canonical platform-neutral plan."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .contracts import ContextSnapshot, ResolvedTaskSpec
from .planner import estimate_task, plan_task
from .serialization import strict_json_loads


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile a resolved Multi-Detect task and runtime context into a "
            "deterministic PlatformNeutralPlan."
        )
    )
    parser.add_argument("--task", type=Path, required=True, help="ResolvedTaskSpec JSON")
    parser.add_argument("--context", type=Path, required=True, help="ContextSnapshot JSON")
    parser.add_argument(
        "--vehicle-class",
        required=True,
        help="vtol, fixed-wing or multirotor (aliases accepted)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="plan JSON output (default: stdout)",
    )
    parser.add_argument(
        "--estimate-out",
        type=Path,
        default=None,
        help="optional planning estimate JSON output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        task = ResolvedTaskSpec.from_mapping(
            strict_json_loads(args.task.read_bytes(), name="ResolvedTaskSpec")
        )
        context = ContextSnapshot.from_mapping(
            strict_json_loads(args.context.read_bytes(), name="ContextSnapshot")
        )
        estimate = estimate_task(task, context, args.vehicle_class)
        plan = plan_task(task, context, args.vehicle_class)
        plan_payload = _json_payload(plan.to_mapping())
        if args.out is None:
            sys.stdout.write(plan_payload)
        else:
            _write_payload(args.out, plan_payload)
        if args.estimate_out is not None:
            _write_payload(args.estimate_out, _json_payload(estimate.to_mapping()))
    except (OSError, TypeError, ValueError) as exc:
        print(f"multidetect-plan: {exc}", file=sys.stderr)
        return 2
    return 0


def _json_payload(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _write_payload(path: Path, payload: str) -> None:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8", newline="\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
