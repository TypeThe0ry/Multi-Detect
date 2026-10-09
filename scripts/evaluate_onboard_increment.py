"""Compare a development policy with its parent without selecting either model.

These are structured-state diagnostics, not rendered Gazebo flight evidence.
The sensor holdout uses the same scene teacher with a new seed, so it is not
an independently annotated visual dataset. Both models see identical cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import OnboardTaskAction  # noqa: E402
from scripts.evaluate_onboard_3d_sensor_holdout import evaluate as sensor_evaluate  # noqa: E402
from scripts.evaluate_onboard_generalization import evaluate_model  # noqa: E402
from scripts.evaluate_onboard_navigation_geometry import evaluate as geometry_evaluate  # noqa: E402


def summarize(parent: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    sensor_nonregression = all(
        candidate["sensor"][key] >= parent["sensor"][key]
        for key in ("task_accuracy", "flight_accuracy", "compatible_output_rate")
    )
    if not sensor_nonregression:
        reasons.append("structured sensor holdout regressed")
    generalization_nonregression = (
        all(
            candidate["generalization"][key] >= parent["generalization"][key]
            for key in ("raw_head_pass_rate", "guarded_pass_rate")
        )
        and not candidate["generalization"]["guarded_failures"]
    )
    if not generalization_nonregression:
        reasons.append("action or guarded generalization regressed or still has failures")
    geometry_passed = all(
        candidate["geometry"][phase]["thresholds"]["diagnostic_pass"]
        for phase in ("search", "inspect_target")
    )
    if not geometry_passed:
        reasons.append("navigation geometry did not pass in both task phases")
    reasons.append("independent rendered 3-D trajectory and Jetson evidence are unavailable")
    return {
        "structured_sensor_nonregression": sensor_nonregression,
        "generalization_nonregression": generalization_nonregression,
        "navigation_geometry_passed": geometry_passed,
        "development_diagnostics_passed": (
            sensor_nonregression and generalization_nonregression and geometry_passed
        ),
        "candidate_promoted": False,
        "current_reference_unchanged": True,
        "reason": reasons,
    }


def evaluate_pair(
    parent_path: Path,
    candidate_path: Path,
    manifest_path: Path,
    *,
    seed: int,
    sensor_count: int = 900,
    geometry_count: int = 240,
) -> dict[str, Any]:
    if sensor_count < 30 or geometry_count < 8:
        raise ValueError("sensor_count >= 30 and geometry_count >= 8 are required")
    results = {}
    for label, path in (("parent", parent_path), ("candidate", candidate_path)):
        results[label] = {
            "path": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "sensor": sensor_evaluate(path, manifest_path, count=sensor_count, seed=seed),
            "generalization": evaluate_model(path, seed=seed),
            "geometry": {
                phase.value: geometry_evaluate(
                    path,
                    count=geometry_count,
                    seed=seed,
                    task_phase=phase,
                    raw_head_semantics="full_observed_target_offset",
                )
                for phase in (OnboardTaskAction.SEARCH, OnboardTaskAction.INSPECT_TARGET)
            },
        }
    return {
        "schema_version": 1,
        "contract": "multidetect.onboard-increment-paired-diagnostics-v1",
        "evaluation_contract": {
            "seed": seed,
            "identical_cases_for_parent_and_candidate": True,
            "sensor_holdout_kind": "same_structured_scene_teacher_new_seed",
            "independent_visual_holdout": False,
            "raw_geometry_semantics": "full_observed_target_offset",
            "training_used": False,
            "pixel_level_rendered": False,
            "external_gazebo_runtime": False,
        },
        **results,
        "decision": summarize(results["parent"], results["candidate"]),
        "physical_performance_claimed": False,
        "jetson_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-model", type=Path, required=True)
    parser.add_argument("--candidate-model", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=(ROOT / "configs/scenarios/gazebo-3d-mainline-v1.manifest.json"),
    )
    parser.add_argument("--seed", type=int, default=20261008)
    parser.add_argument("--sensor-count", type=int, default=900)
    parser.add_argument("--geometry-count", type=int, default=240)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic evidence: {args.output}")
    report = evaluate_pair(
        args.parent_model.resolve(),
        args.candidate_model.resolve(),
        args.manifest.resolve(),
        seed=args.seed,
        sensor_count=args.sensor_count,
        geometry_count=args.geometry_count,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "decision": report["decision"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
