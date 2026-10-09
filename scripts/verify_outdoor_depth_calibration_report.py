#!/usr/bin/env python3
"""Verify that an outdoor depth validation report authorizes one calibration file.

The validator intentionally does not fit or mutate calibration data.  This
small verifier is the deployment-side companion: it binds a report to the
exact calibration document bytes and, when requested, requires the report's
independent-holdout quality gate to be explicitly passed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from multidetect.autonomy.serialization import (  # noqa: E402
    AutonomyContractError,
    strict_json_loads,
)
from multidetect.depth_calibration import load_calibration_document  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--require-quality-gate",
        action="store_true",
        help="fail unless the report explicitly records a passed strict holdout gate",
    )
    return parser


def verify_report(
    *,
    calibration_path: Path,
    report_path: Path,
    require_quality_gate: bool = False,
) -> dict[str, object]:
    profile = load_calibration_document(calibration_path)
    try:
        decoded = strict_json_loads(report_path.read_bytes(), name="depth validation report")
    except (OSError, AutonomyContractError, TypeError) as exc:
        raise ValueError(f"depth validation report is not valid strict JSON: {exc}") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError("depth validation report root must be an object")

    calibration = decoded.get("calibration")
    if not isinstance(calibration, Mapping):
        raise ValueError("depth validation report is missing calibration metadata")
    report_sha = calibration.get("document_sha256")
    if report_sha != profile.document_sha256:
        raise ValueError(
            "depth validation report calibration hash does not match the deployed document"
        )
    if decoded.get("automatic_calibration_update") is not False:
        raise ValueError("depth validation report must disable automatic calibration updates")

    quality_gate = decoded.get("quality_gate")
    if not isinstance(quality_gate, Mapping):
        raise ValueError("depth validation report is missing quality_gate")
    status = quality_gate.get("status")
    passed = quality_gate.get("passed")
    required = quality_gate.get("required")
    if require_quality_gate and not (
        required is True and status == "passed" and passed is True
    ):
        raise ValueError("depth validation report quality gate is not explicitly passed")

    return {
        "calibration_document_sha256": profile.document_sha256,
        "quality_gate_status": status,
        "quality_gate_required": required,
        "quality_gate_passed": passed,
        "automatic_calibration_update": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = verify_report(
            calibration_path=args.calibration,
            report_path=args.report,
            require_quality_gate=args.require_quality_gate,
        )
    except (OSError, ValueError) as exc:
        print(f"calibration_report_verification_failed={exc}", file=sys.stderr)
        return 2
    print("calibration_report_verified=true")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
