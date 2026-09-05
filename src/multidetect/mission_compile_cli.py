"""Compile strict task/context/runtime JSON into a PX4 mission artifact.

This command is the file-backed entry point for the shared authoring seam.
It produces the same hash-bound ``PlannedMissionCompilation`` used by Python,
SIM, HIL and REAL tooling, while deliberately keeping transport and flight
control outside the command.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .autonomy import ContextSnapshot, ResolvedTaskSpec
from .autonomy.serialization import require_sha256, strict_json_loads
from .mission_planning import (
    compile_planned_task_mission_from_runtime,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile a strict task/context/runtime bundle into a hash-bound "
            "PlannedMissionCompilation JSON artifact."
        )
    )
    parser.add_argument("--task", type=Path, required=True, help="ResolvedTaskSpec JSON")
    parser.add_argument("--context", type=Path, required=True, help="ContextSnapshot JSON")
    parser.add_argument(
        "--runtime",
        type=Path,
        required=True,
        help="MissionCompileRuntime JSON (geometry plus safety policy)",
    )
    parser.add_argument(
        "--vehicle-class",
        required=True,
        help="vtol, fixed-wing or multirotor (aliases accepted)",
    )
    parser.add_argument(
        "--airframe-profile-hash",
        required=True,
        help="SHA-256 identity hash for the selected airframe profile",
    )
    parser.add_argument("--mission-id", default=None, help="optional stable mission identifier")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="compilation artifact output (default: stdout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        task = ResolvedTaskSpec.from_mapping(
            strict_json_loads(args.task.read_bytes(), name="ResolvedTaskSpec")
        )
        context = ContextSnapshot.from_mapping(
            strict_json_loads(args.context.read_bytes(), name="ContextSnapshot")
        )
        runtime = strict_json_loads(
            args.runtime.read_bytes(), name="MissionCompileRuntime"
        )
        compilation = compile_planned_task_mission_from_runtime(
            task,
            context,
            args.vehicle_class,
            runtime=runtime,
            airframe_profile_hash=require_sha256(
                args.airframe_profile_hash, "airframe_profile_hash"
            ),
            mission_id=args.mission_id,
        )
        payload = _json_payload(compilation.to_mapping())
        if args.out is None:
            sys.stdout.write(payload)
        else:
            _write_payload(args.out, payload)
    except (OSError, TypeError, ValueError) as exc:
        print(f"multidetect-mission-compile: {exc}", file=sys.stderr)
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
