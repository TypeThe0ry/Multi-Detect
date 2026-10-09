"""Measure continuous NED navigation geometry for the shared OnboardAI model.

This evaluator is deliberately independent of the training teacher and of
the fixed 22-case pressure harness.  It samples a full bearing circle and a
continuous distance envelope, then runs the exact ONNX adapter used by SIM and
Jetson.  The result is a diagnostic model report, not Gazebo, HIL, or flight
evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
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

DEFAULT_MODEL = (
    ROOT / "artifacts/models/onboard-policy-r33-balanced-bearing-3d-dev/onboard_policy_r33.onnx"
)
DEFAULT_OUTPUT = ROOT / "artifacts/evaluation/onboard-navigation-geometry-diagnostic.json"
DEFAULT_STEP_HORIZON_M = 40.0
RAW_HEAD_SEMANTICS = ("full_observed_target_offset", "bounded_next_step")
DEFAULT_RAW_HEAD_SEMANTICS = "full_observed_target_offset"


def _context(index: int) -> ContextSnapshot:
    captured = 100.0 + index
    return ContextSnapshot(
        snapshot_id=f"navigation-geometry-{index}",
        captured_at_s=captured,
        captured_at_utc=(datetime(2026, 10, 6, tzinfo=timezone.utc) + timedelta(seconds=captured))
        .isoformat()
        .replace("+00:00", "Z"),
        aircraft_position=GeoPoint(1.3, 103.8, 40.0, "relative_home"),
        home_position=GeoPoint(1.3, 103.8, 0.0, "relative_home"),
        airframe_profile_id="px4_multirotor_gazebo_v1",
        airframe_profile_hash="b" * 64,
        battery_remaining_pct=82.0,
        estimated_flight_time_s=1_200.0,
        ground_speed_mps=2.0,
        airspeed_mps=2.0,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=2.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _vector_error(
    predicted_north: float,
    predicted_east: float,
    expected_north: float,
    expected_east: float,
) -> dict[str, float | None]:
    expected_range = math.hypot(expected_north, expected_east)
    predicted_range = math.hypot(predicted_north, predicted_east)
    endpoint_error = math.hypot(
        predicted_north - expected_north,
        predicted_east - expected_east,
    )
    if predicted_range <= 1.0e-6 or expected_range <= 1.0e-6:
        angle_error = None
    else:
        predicted_angle = math.atan2(predicted_east, predicted_north)
        expected_angle = math.atan2(expected_east, expected_north)
        delta = (predicted_angle - expected_angle + math.pi) % (2.0 * math.pi) - math.pi
        angle_error = abs(math.degrees(delta))
    return {
        "endpoint_error_m": endpoint_error,
        "predicted_range_m": predicted_range,
        "range_error_m": abs(predicted_range - expected_range),
        "angle_error_deg": angle_error,
    }


def evaluate(
    model_path: Path,
    *,
    count: int,
    seed: int,
    event_kind: OnboardEventKind = OnboardEventKind.TARGET_MOVED,
    task_phase: OnboardTaskAction = OnboardTaskAction.SEARCH,
    target_match_score: float = 0.95,
    target_class_match: bool = True,
    step_horizon_m: float = DEFAULT_STEP_HORIZON_M,
    raw_head_semantics: str = DEFAULT_RAW_HEAD_SEMANTICS,
) -> dict[str, Any]:
    if raw_head_semantics not in RAW_HEAD_SEMANTICS:
        raise ValueError(f"raw_head_semantics must be one of {RAW_HEAD_SEMANTICS}")
    if not math.isfinite(step_horizon_m) or step_horizon_m <= 0.0:
        raise ValueError("step_horizon_m must be a positive finite number")
    runner = OnboardOnnxModelRunner.from_path(model_path, providers=["CPUExecutionProvider"])
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        bearing_deg = rng.uniform(-179.0, 179.0)
        distance_m = rng.uniform(8.0, 420.0)
        expected_north = math.cos(math.radians(bearing_deg)) * distance_m
        expected_east = math.sin(math.radians(bearing_deg)) * distance_m
        context = _context(index)
        event = OnboardRuntimeEvent(
            event_id=f"navigation-geometry-{index}",
            kind=event_kind,
            observed_at_s=context.captured_at_s,
            target_bearing_deg=bearing_deg,
            target_geometry_frame="ned",
            target_distance_m=distance_m,
            target_confidence=0.95,
            target_match_score=target_match_score,
            target_class="vehicle",
            target_color_match=None,
            target_class_match=target_class_match,
            task_phase=task_phase,
        )
        model_input = OnboardModelInput(context, event)
        output = runner.infer(model_input)
        if output.flight_plan is None:
            predicted_north = 0.0
            predicted_east = 0.0
            action = None
        else:
            predicted_north = output.flight_plan.north_offset_m
            predicted_east = output.flight_plan.east_offset_m
            action = output.flight_plan.action.value
        metrics = _vector_error(
            predicted_north,
            predicted_east,
            expected_north,
            expected_east,
        )
        expected_step_distance = min(distance_m, step_horizon_m)
        expected_step_north = math.cos(math.radians(bearing_deg)) * expected_step_distance
        expected_step_east = math.sin(math.radians(bearing_deg)) * expected_step_distance
        step_metrics = _vector_error(
            predicted_north,
            predicted_east,
            expected_step_north,
            expected_step_east,
        )
        rows.append(
            {
                "case_id": f"navigation-geometry-{index}",
                "bearing_deg": bearing_deg,
                "distance_m": distance_m,
                "step_horizon_m": step_horizon_m,
                "expected_step_distance_m": expected_step_distance,
                "expected_north_m": expected_north,
                "expected_east_m": expected_east,
                "expected_step_north_m": expected_step_north,
                "expected_step_east_m": expected_step_east,
                "predicted_north_m": predicted_north,
                "predicted_east_m": predicted_east,
                "action": action,
                **metrics,
                "step_endpoint_error_m": step_metrics["endpoint_error_m"],
                "step_range_error_m": step_metrics["range_error_m"],
                "step_angle_error_deg": step_metrics["angle_error_deg"],
            }
        )
    angle_values = [
        float(row["angle_error_deg"]) for row in rows if row["angle_error_deg"] is not None
    ]
    endpoint_values = [float(row["endpoint_error_m"]) for row in rows]
    range_values = [float(row["range_error_m"]) for row in rows]
    step_angle_values = [
        float(row["step_angle_error_deg"])
        for row in rows
        if row["step_angle_error_deg"] is not None
    ]
    step_endpoint_values = [float(row["step_endpoint_error_m"]) for row in rows]
    step_range_values = [float(row["step_range_error_m"]) for row in rows]
    full_target_metrics = {
        "angle_mae_deg": sum(angle_values) / len(angle_values) if angle_values else None,
        "angle_p95_deg": sorted(angle_values)[max(0, math.ceil(len(angle_values) * 0.95) - 1)]
        if angle_values
        else None,
        "endpoint_mae_m": sum(endpoint_values) / len(endpoint_values),
        "endpoint_p95_m": sorted(endpoint_values)[
            max(0, math.ceil(len(endpoint_values) * 0.95) - 1)
        ],
        "range_mae_m": sum(range_values) / len(range_values),
    }
    bounded_step_metrics = {
        "angle_mae_deg": sum(step_angle_values) / len(step_angle_values)
        if step_angle_values
        else None,
        "angle_p95_deg": sorted(step_angle_values)[
            max(0, math.ceil(len(step_angle_values) * 0.95) - 1)
        ]
        if step_angle_values
        else None,
        "endpoint_mae_m": sum(step_endpoint_values) / len(step_endpoint_values),
        "endpoint_p95_m": sorted(step_endpoint_values)[
            max(0, math.ceil(len(step_endpoint_values) * 0.95) - 1)
        ],
        "range_mae_m": sum(step_range_values) / len(step_range_values),
    }
    # The current trainer and runtime use a complete measured NED offset.
    # A short receding-horizon step is an optional diagnostic, not an implicit
    # replacement for that contract.  Keep both views in every report.
    selected_metrics = (
        full_target_metrics
        if raw_head_semantics == "full_observed_target_offset"
        else bounded_step_metrics
    )
    selected_angles = (
        angle_values if raw_head_semantics == "full_observed_target_offset" else step_angle_values
    )
    direction_success = sum(value <= 20.0 for value in selected_angles)
    report = {
        "schema_version": 1,
        "contract": "multidetect.onboard-navigation-geometry-diagnostic",
        "model_path": str(model_path.resolve()),
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "seed": seed,
        "sample_count": count,
        "sampling": {
            "bearing_deg": [-179.0, 179.0],
            "distance_m": [8.0, 420.0],
            "step_horizon_m": step_horizon_m,
            "target_distance_semantics": "full_observed_target_range",
            "raw_head_semantics": raw_head_semantics,
            "calibrated_adapter_semantics": (
                "measured_ned_offsets_override_raw_head_for_follow_or_goto"
            ),
            "geometry_frame": "ned",
            "event_kind": event_kind.value,
            "task_phase": task_phase.value,
            "target_match_score": target_match_score,
            "target_class_match": target_class_match,
            # The default SEARCH/TARGET_MOVED run remains intentionally
            # independent from the teacher.  A matched INSPECT_TARGET run is
            # a separate semantic-control measurement: it tests the same
            # lifecycle state in which the teacher permits FOLLOW_TARGET.
            "independent_from_training_teacher": not (
                event_kind is OnboardEventKind.TARGET_MOVED
                and task_phase is OnboardTaskAction.INSPECT_TARGET
                and target_class_match
                and target_match_score >= 0.8
            ),
        },
        "metrics": {
            **selected_metrics,
            "direction_within_20deg": direction_success,
            "direction_within_20deg_rate": direction_success / len(selected_angles)
            if selected_angles
            else 0.0,
            "full_target_reference": full_target_metrics,
            "bounded_step_reference": bounded_step_metrics,
        },
        "thresholds": {
            "raw_head_semantics": raw_head_semantics,
            "angle_p95_deg": 20.0,
            "range_mae_m": 30.0,
            "diagnostic_pass": bool(
                selected_angles
                and selected_metrics["angle_p95_deg"] <= 20.0
                and selected_metrics["range_mae_m"] <= 30.0
            ),
        },
        "rows": rows,
        "physical_performance_claimed": False,
        "gazebo_runtime_evidence": False,
        "jetson_evidence": False,
        "hil_real_evidence": False,
        "outdoor_gnss_evidence": False,
        "final_release_allowed": False,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--count", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20261006)
    parser.add_argument(
        "--raw-head-semantics",
        choices=RAW_HEAD_SEMANTICS,
        default=DEFAULT_RAW_HEAD_SEMANTICS,
        help=(
            "navigation target used for threshold decisions; full observed target "
            "offset matches the current trainer/runtime, while bounded_next_step "
            "explicitly evaluates an experimental short-step policy"
        ),
    )
    parser.add_argument(
        "--step-horizon-m",
        type=float,
        default=DEFAULT_STEP_HORIZON_M,
        help=(
            "distance cap for the bounded-step reference; affects threshold "
            "decisions only with --raw-head-semantics bounded_next_step"
        ),
    )
    parser.add_argument(
        "--event-kind",
        choices=tuple(item.value for item in OnboardEventKind),
        default=OnboardEventKind.TARGET_MOVED.value,
        help="observed event supplied to the shared runtime contract",
    )
    parser.add_argument(
        "--task-phase",
        choices=tuple(item.value for item in OnboardTaskAction),
        default=OnboardTaskAction.SEARCH.value,
        help="observed task lifecycle phase supplied to the shared runtime contract",
    )
    parser.add_argument(
        "--target-match-score",
        type=float,
        default=0.95,
        help="generic detector/tracker match score; color is never used by this evaluator",
    )
    parser.add_argument(
        "--target-class-match",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="whether the neutral target satisfies the requested class",
    )
    args = parser.parse_args(argv)
    if args.count < 8:
        raise ValueError("count must be at least 8")
    report = evaluate(
        args.model.resolve(),
        count=args.count,
        seed=args.seed,
        event_kind=OnboardEventKind(args.event_kind),
        task_phase=OnboardTaskAction(args.task_phase),
        target_match_score=args.target_match_score,
        target_class_match=args.target_class_match,
        step_horizon_m=args.step_horizon_m,
        raw_head_semantics=args.raw_head_semantics,
    )
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["metrics"], sort_keys=True))
    print(f"diagnostic_pass={report['thresholds']['diagnostic_pass']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
