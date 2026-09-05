"""Verify or bind deterministic three-airframe 3-D SIM control evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.simulator.three_d_control import (
    Simulator3DControlError,
    build_binding,
    verify_file,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "evidence",
        type=Path,
        nargs="+",
        help="one evidence JSON to verify, or three JSON files with --bind",
    )
    parser.add_argument(
        "--expected-release-label",
        default=None,
        help="reject evidence that was generated for another release label",
    )
    parser.add_argument(
        "--bind",
        action="store_true",
        help="create a hash-bound three-airframe binding instead of a single-file report",
    )
    parser.add_argument(
        "--manifest-directory",
        type=Path,
        default=Path.cwd(),
        help="base directory for relative paths in a binding (default: current directory)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the JSON report to this path as well as stdout",
    )
    return parser


def _failure(exc: Exception, *, binding: bool) -> dict[str, Any]:
    return {
        "event": (
            "simulator_3d_control_binding_failed"
            if binding
            else "simulator_3d_control_verification_failed"
        ),
        "schema_version": 1,
        "gate_passed": False,
        "checks": {"readable_json": False},
        "reasons": [f"{type(exc).__name__}: {exc}"],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    binding = bool(args.bind)
    try:
        if binding:
            if len(args.evidence) != 3:
                raise Simulator3DControlError("--bind requires exactly three evidence files")
            result = build_binding(
                args.evidence,
                manifest_directory=args.manifest_directory,
                expected_release_label=args.expected_release_label,
            )
            result = {"event": "simulator_3d_control_binding_verified", **result}
        else:
            if len(args.evidence) != 1:
                raise Simulator3DControlError(
                    "single-file verification accepts exactly one evidence file; "
                    "use --bind for three"
                )
            source = args.evidence[0].expanduser().resolve()
            result = verify_file(source, expected_release_label=args.expected_release_label)
            result = {"evidence_path": str(source), **result}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        result = _failure(exc, binding=binding)
    payload = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        output = args.out.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("gate_passed") is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
