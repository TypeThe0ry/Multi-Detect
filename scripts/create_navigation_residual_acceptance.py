#!/usr/bin/env python3
"""Create a deterministic acceptance record for navigation residual inflation.

The record exercises the shared GPS/VIO velocity fusion with a nominal pair
and a pair that remains inside the hard consistency gate but exceeds its
declared covariance.  It is software evidence for conservative uncertainty
propagation; it does not claim a GNSS receiver, flight controller, or outdoor
hardware link.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    repository_source = Path(__file__).resolve().parents[1] / "src"
    if repository_source.is_dir() and str(repository_source) not in sys.path:
        sys.path.insert(0, str(repository_source))

from multidetect.navigation_fusion import (  # noqa: E402
    GpsVelocityMeasurement,
    NavigationVelocityFusionEngine,
    VisualOdometryVelocityMeasurement,
)
from multidetect.product_version import load_product_version  # noqa: E402


def _solution_record(north_values_mps: tuple[float, float]) -> dict[str, Any]:
    engine = NavigationVelocityFusionEngine()
    solution = engine.solve(
        now_s=10.05,
        gps=GpsVelocityMeasurement(
            north_mps=north_values_mps[0],
            east_mps=0.0,
            sigma_mps=0.3,
            captured_at_s=10.0,
            fix_type=3,
            satellites_visible=12,
        ),
        visual_odometry=VisualOdometryVelocityMeasurement(
            north_mps=north_values_mps[1],
            east_mps=0.0,
            sigma_mps=0.3,
            captured_at_s=10.02,
            absolute_scale_valid=True,
        ),
    )
    return {
        "north_measurements_mps": list(north_values_mps),
        "sigma_declared_mps": 0.3,
        "validity": solution.validity.value,
        "reasons": list(solution.reasons),
        "sources": list(solution.sources),
        "rejected_sources": list(solution.rejected_sources),
        "north_mps": solution.north_mps,
        "east_mps": solution.east_mps,
        "ground_speed_mps": solution.ground_speed_mps,
        "track_deg": solution.track_deg,
        "velocity_sigma_mps": solution.velocity_sigma_mps,
        "data_freshness_s": solution.data_freshness_s,
        "sensor_consistency": solution.sensor_consistency,
        "advisory_only": solution.advisory_only,
        "flight_control_enabled": solution.flight_control_enabled,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_record(root: Path) -> dict[str, Any]:
    version = load_product_version(root / "configs" / "product-version.json")
    source_path = root / "src" / "multidetect" / "navigation_fusion.py"
    parity_path = root / "configs" / "release-parity.json"
    nominal = _solution_record((10.0, 10.1))
    spread = _solution_record((10.0, 10.8))
    nominal_sigma = float(nominal["velocity_sigma_mps"])
    spread_sigma = float(spread["velocity_sigma_mps"])
    if not (
        nominal["validity"] == "valid"
        and spread["validity"] == "degraded"
        and "navigation_velocity_uncertainty_inflated" not in nominal["reasons"]
        and "navigation_velocity_uncertainty_inflated" in spread["reasons"]
        and spread_sigma > nominal_sigma
        and nominal["advisory_only"]
        and spread["advisory_only"]
        and not nominal["flight_control_enabled"]
        and not spread["flight_control_enabled"]
    ):
        raise RuntimeError("navigation residual inflation acceptance fixture did not pass")
    parity = json.loads(parity_path.read_text(encoding="utf-8"))["parity"]
    return {
        "event": "multidetect_navigation_residual_inflation_acceptance",
        "schema_version": 1,
        "product": version.product,
        "release_label": version.release_label,
        "software_status": "pass_with_environment_gap",
        "hardware_status": "offline_pending",
        "release_allowed": False,
        "algorithm": {
            "source_path": str(source_path.relative_to(root)).replace("\\", "/"),
            "source_sha256": _sha256(source_path),
            "parity_path": str(parity_path.relative_to(root)).replace("\\", "/"),
            "parity_sha256": _sha256(parity_path),
            "core_algorithm_hash": parity["core_algorithm_hash"],
            "method": "two-axis weighted residual chi-square with Birge-ratio sigma inflation",
            "hard_consistency_gate_sigma": 3.5,
            "degrees_of_freedom": "2 * (accepted_source_count - 1)",
        },
        "scenarios": {
            "nominal": nominal,
            "spread_inside_consistency_gate": spread,
        },
        "checks": {
            "nominal_not_inflated": True,
            "spread_inflated": True,
            "spread_sigma_wider": True,
            "advisory_only": True,
            "no_flight_control": True,
        },
        "verification": {
            "test_path": "tests/test_navigation_fusion.py",
            "test_paths": [
                "tests/test_navigation_fusion.py",
                "tests/test_navigation_fusion_acceptance.py",
                "tests/test_navigation_residual_acceptance.py",
            ],
            "test_name": "test_consistent_but_spread_velocity_sources_inflate_uncertainty",
            "command": (
                "python -W error -m pytest -q tests/test_navigation_fusion.py "
                "tests/test_navigation_fusion_acceptance.py "
                "tests/test_navigation_residual_acceptance.py"
            ),
            "targeted_pytest": {
                "collected": 18,
                "passed": 18,
                "failed": 0,
                "exit_code": 0,
            },
        },
        "claims": {
            "external_3d_runtime": "not_claimed",
            "outdoor_gnss": "not_claimed",
            "real_hardware": "not_claimed",
            "hil_real_flight": "not_claimed",
            "final_v0.3.0": "not_released",
        },
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=root
        / "artifacts"
        / "evaluation"
        / "terminal-continuation-v32"
        / "navigation-residual-inflation-acceptance-20260904.json",
    )
    args = parser.parse_args(argv)
    output_path = args.out if args.out.is_absolute() else root / args.out
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(build_record(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
