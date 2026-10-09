#!/usr/bin/env python3
"""Evaluate an OnboardAI artifact on a predeclared semantic holdout.

The holdout exercises the shared structured event contract only.  It does not
read Gazebo truth, issue MAVLink, arm a vehicle, or claim Jetson/HIL/REAL
performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import (  # noqa: E402
    ContextSnapshot,
    GeoPoint,
    OnboardEventKind,
    OnboardModelInput,
    OnboardOnnxModelRunner,
    OnboardRuntimeEvent,
    OnboardTaskAction,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _context(case_id: str, case: dict[str, object]) -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id=f"{case_id}-context",
        captured_at_s=10.0,
        captured_at_utc=datetime.fromtimestamp(10.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        aircraft_position=GeoPoint(1.300000, 103.800000, 30.0, "relative_home"),
        home_position=GeoPoint(1.300000, 103.800000),
        airframe_profile_id="px4_multirotor_gazebo_v1",
        airframe_profile_hash="b" * 64,
        battery_remaining_pct=float(case.get("battery_remaining_pct", 78.0)),
        estimated_flight_time_s=float(case.get("estimated_flight_time_s", 2_400.0)),
        ground_speed_mps=3.0,
        airspeed_mps=4.0,
        wind_north_mps=0.4,
        wind_east_mps=-0.3,
        gps_fix_type=int(case.get("gps_fix_type", 3)),
        gps_horizontal_accuracy_m=float(case.get("gps_horizontal_accuracy_m", 1.5)),
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _event(case_id: str, case: dict[str, object]) -> OnboardRuntimeEvent:
    phase = case.get("task_phase")
    return OnboardRuntimeEvent(
        event_id=case_id,
        kind=OnboardEventKind(str(case["event_kind"])),
        observed_at_s=10.0,
        severity=40 if str(case["event_kind"]) in {"gps_degraded", "obstacle_risk"} else 0,
        detail=f"independent holdout {case_id}",
        target_bearing_deg=-18.0,
        target_distance_m=45.0,
        target_confidence=float(case.get("target_confidence", 0.0)),
        target_match_score=float(case.get("target_match_score", 0.0)),
        target_class=(
            str(case["target_class"])
            if case.get("target_class") is not None
            else ("vehicle" if case.get("target_class_match") is not None else None)
        ),
        target_color=(
            str(case["target_color"])
            if case.get("target_color") is not None
            else ("red" if case.get("target_color_match") is not None else None)
        ),
        target_class_match=case.get("target_class_match"),
        target_color_match=case.get("target_color_match"),
        obstacle_distance_m=(
            float(case["obstacle_distance_m"]) if "obstacle_distance_m" in case else 5_000.0
        ),
        route_progress_fraction=0.42,
        task_phase=OnboardTaskAction(str(phase)) if phase is not None else None,
    )


def evaluate(model_path: Path, holdout_path: Path) -> dict[str, object]:
    document = json.loads(holdout_path.read_text(encoding="utf-8"))
    if document.get("training_used") is not False:
        raise ValueError("holdout must declare training_used=false")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("holdout requires cases")
    runner = OnboardOnnxModelRunner.from_path(model_path, providers=("CPUExecutionProvider",))
    results: list[dict[str, object]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            raise ValueError("holdout case must be an object")
        case_id = str(raw["case_id"])
        output = runner.infer(OnboardModelInput(_context(case_id, raw), _event(case_id, raw)))
        expected_task = str(raw["expected_task"])
        expected_flight = str(raw["expected_flight"])
        actual_task = None if output.task_action is None else output.task_action.value
        actual_flight = output.flight_plan.action.value
        results.append(
            {
                "case_id": case_id,
                "expected_task": expected_task,
                "expected_flight": expected_flight,
                "actual_task": actual_task,
                "actual_flight": actual_flight,
                "confidence": output.confidence,
                "reason": output.reason,
                "passed": actual_task == expected_task and actual_flight == expected_flight,
                "truth_free": True,
                "policy_applied_to_actuator": False,
            }
        )
    passed = sum(bool(item["passed"]) for item in results)
    return {
        "schema_version": 1,
        "contract": "multidetect.onboard-policy-independent-holdout-report",
        "model_path": str(model_path),
        "model_sha256": _sha256(model_path),
        "holdout_path": str(holdout_path),
        "holdout_sha256": _sha256(holdout_path),
        "case_count": len(results),
        "passed_cases": passed,
        "task_and_flight_accuracy": passed / len(results),
        "independent_holdout": True,
        "training_used": False,
        "results": results,
        "synthetic_structured_holdout": True,
        "physical_performance_claimed": False,
        "jetson_hardware_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "candidate_promoted": False,
        "final_release_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--holdout",
        type=Path,
        default=ROOT / "configs/training/onboard-policy-independent-holdout-v1.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.model.resolve(strict=True), args.holdout.resolve(strict=True))
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["passed_cases"] == result["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
