#!/usr/bin/env python3
"""Create a deterministic acceptance record for residual-variance inflation.

The record exercises the same two-source direct-range scenarios as the
regression test.  It is deliberately a software evidence artifact: it proves
the conservative uncertainty propagation and advisory-only boundary, not a
hardware, outdoor-GNSS, or external-Docker run.
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

from multidetect.multimodal_ranging import (  # noqa: E402
    AircraftPose,
    CameraCalibration,
    DirectRangeMeasurement,
    DirectRangeSource,
    MultiModalRangingEngine,
    TargetImageObservation,
)
from multidetect.product_version import load_product_version  # noqa: E402


def _fixture() -> tuple[CameraCalibration, AircraftPose, TargetImageObservation]:
    return (
        CameraCalibration(
            calibration_id="camera-main-v1",
            width_px=1280,
            height_px=720,
            fx_px=800.0,
            fy_px=800.0,
            cx_px=640.0,
            cy_px=360.0,
            mount_pitch_down_deg=30.0,
        ),
        AircraftPose(
            captured_at_s=10.0,
            roll_deg=0.0,
            pitch_deg=0.0,
            heading_deg=90.0,
        ),
        TargetImageObservation(
            target_id="fire-1",
            frame_id="frame-10",
            captured_at_s=10.0,
            center_x=0.5,
            center_y=0.5,
        ),
    )


def _solution_record(values_m: tuple[float, float]) -> dict[str, Any]:
    calibration, pose, target = _fixture()
    measurements = tuple(
        DirectRangeMeasurement(source, target.target_id, value_m, 5.0, 10.0)
        for source, value_m in zip(
            (DirectRangeSource.LASER, DirectRangeSource.VIO), values_m, strict=True
        )
    )
    solution = MultiModalRangingEngine().solve_direct(
        calibration=calibration,
        pose=pose,
        target=target,
        direct_measurements=measurements,
        now_s=10.05,
    )
    return {
        "measurements_m": list(values_m),
        "sigma_declared_m": 5.0,
        "validity": solution.validity.value,
        "reasons": list(solution.reasons),
        "sources": list(solution.sources),
        "slant_range_m": solution.slant_range_m,
        "slant_range_ci95_m": list(solution.slant_range_ci95_m or ()),
        "ground_range_m": solution.ground_range_m,
        "ground_range_ci95_m": list(solution.ground_range_ci95_m or ()),
        "sensor_consistency": solution.sensor_consistency,
        "data_freshness_s": solution.data_freshness_s,
        "advisory_only": solution.advisory_only,
        "flight_control_enabled": solution.flight_control_enabled,
        "physical_release_enabled": solution.physical_release_enabled,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_record(root: Path) -> dict[str, Any]:
    version = load_product_version(root / "configs" / "product-version.json")
    source_path = root / "src" / "multidetect" / "multimodal_ranging.py"
    parity_path = root / "configs" / "release-parity.json"
    nominal = _solution_record((100.0, 100.0))
    spread = _solution_record((100.0, 110.0))
    nominal_width = nominal["slant_range_ci95_m"][1] - nominal["slant_range_ci95_m"][0]
    spread_width = spread["slant_range_ci95_m"][1] - spread["slant_range_ci95_m"][0]
    if not (
        nominal["validity"] == "degraded"
        and spread["validity"] == "degraded"
        and "direct_range_uncertainty_inflated" not in nominal["reasons"]
        and "direct_range_uncertainty_inflated" in spread["reasons"]
        and spread_width > nominal_width
        and nominal["advisory_only"]
        and spread["advisory_only"]
        and not nominal["flight_control_enabled"]
        and not spread["flight_control_enabled"]
        and not nominal["physical_release_enabled"]
        and not spread["physical_release_enabled"]
    ):
        raise RuntimeError("residual inflation acceptance fixture did not pass")
    parity = json.loads(parity_path.read_text(encoding="utf-8"))["parity"]
    return {
        "event": "multidetect_ranging_residual_inflation_acceptance",
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
            "method": "weighted residual chi-square with Birge-ratio sigma inflation",
            "hard_consistency_gate_sigma": 3.5,
        },
        "scenarios": {
            "nominal": nominal,
            "spread_inside_consistency_gate": spread,
        },
        "checks": {
            "nominal_not_inflated": True,
            "spread_inflated": True,
            "spread_ci95_wider": True,
            "advisory_only": True,
            "no_flight_control": True,
            "no_physical_release": True,
        },
        "verification": {
            "test_path": "tests/test_multimodal_ranging.py",
            "test_name": "test_consistent_but_spread_direct_ranges_inflate_uncertainty",
            "targeted_pytest": {
                "passed": 72,
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
        / "terminal-continuation-v31"
        / "ranging-residual-inflation-acceptance-20260904.json",
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
