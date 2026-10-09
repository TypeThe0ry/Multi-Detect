"""Stress the deployed OnboardAI ONNX policy on fixed truth-free cases.

This is an evaluation-only harness.  It constructs structured context/events,
uses the same :class:`OnboardOnnxModelRunner` as the runtime, and records both
the unmasked ONNX heads and the runner's guarded result.  A guarded result that
improves a raw head is explicitly reported as masking help, never as evidence
that the model learned the expected action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from multidetect.autonomy import (  # noqa: E402
    ContextSnapshot,
    GeoPoint,
    OnboardEventKind,
    OnboardFlightAction,
    OnboardModelInput,
    OnboardOnnxModelRunner,
    OnboardRuntimeEvent,
    OnboardTaskAction,
    allowed_task_actions_for_event,
    is_task_flight_compatible,
)

DEFAULT_MODEL = (
    ROOT / "artifacts/models/onboard-policy-r17-teacher-consistent/onboard_policy_r17.onnx"
)
DEFAULT_OUTPUT = ROOT / "artifacts/evaluation/onboard-r17-generalization-20261005.json"
SEED = 20261005
TASK_ORDER = tuple(OnboardTaskAction)
FLIGHT_ORDER = tuple(OnboardFlightAction)


@dataclass(frozen=True)
class PressureCase:
    case_id: str
    group: str
    model_input: OnboardModelInput
    accepted_tasks: frozenset[str]
    accepted_flights: frozenset[str]
    expected_north_signs: frozenset[str] = frozenset({"zero", "positive", "negative"})
    expected_east_signs: frozenset[str] = frozenset({"zero", "positive", "negative"})


def _sign(value: float, *, epsilon: float = 1e-6) -> str:
    if abs(value) <= epsilon:
        return "zero"
    return "positive" if value > 0.0 else "negative"


def _context(rng: random.Random, case_id: str, **overrides: object) -> ContextSnapshot:
    captured_at = 100.0 + rng.random() * 500.0
    base: dict[str, object] = {
        "snapshot_id": f"generalization-{case_id}",
        "captured_at_s": captured_at,
        "captured_at_utc": (
            datetime(2026, 10, 5, tzinfo=timezone.utc) + timedelta(seconds=captured_at)
        )
        .isoformat()
        .replace("+00:00", "Z"),
        "aircraft_position": GeoPoint(1.3000, 103.8000, 40.0, "relative_home"),
        "home_position": GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "battery_remaining_pct": 72.0 + rng.random() * 18.0,
        "estimated_flight_time_s": 1_000.0 + rng.random() * 600.0,
        "ground_speed_mps": rng.random() * 12.0,
        "airspeed_mps": rng.random() * 12.0,
        "wind_north_mps": rng.uniform(-6.0, 6.0),
        "wind_east_mps": rng.uniform(-6.0, 6.0),
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 2.0 + rng.random() * 3.0,
        "geofence_valid": True,
        "camera_available": True,
        "perception_available": True,
        "map_cache_available": True,
        "dem_available": True,
        "airframe_profile_hash": "b" * 64,
    }
    base.update(overrides)
    return ContextSnapshot(**base)


def _event(
    case_id: str,
    kind: OnboardEventKind,
    *,
    at_s: float,
    task_phase: OnboardTaskAction | None = None,
    **overrides: object,
) -> OnboardRuntimeEvent:
    values: dict[str, object] = {
        "event_id": f"generalization-{case_id}",
        "kind": kind,
        "observed_at_s": at_s,
        "detail": f"fixed truth-free pressure case: {case_id}",
        "task_phase": task_phase,
    }
    values.update(overrides)
    return OnboardRuntimeEvent(**values)


def build_pressure_cases(seed: int = SEED) -> tuple[PressureCase, ...]:
    """Build deterministic context/event groups without a training oracle."""

    rng = random.Random(seed)
    cases: list[PressureCase] = []

    def add(
        case_id: str,
        group: str,
        context: ContextSnapshot,
        event: OnboardRuntimeEvent,
        tasks: Iterable[OnboardTaskAction | str],
        flights: Iterable[OnboardFlightAction | str],
        *,
        north: Iterable[str] = ("zero", "positive", "negative"),
        east: Iterable[str] = ("zero", "positive", "negative"),
    ) -> None:
        if isinstance(tasks, str):
            tasks = (tasks,)
        if isinstance(flights, str):
            flights = (flights,)
        cases.append(
            PressureCase(
                case_id,
                group,
                OnboardModelInput(context, event),
                frozenset(
                    item.value
                    if isinstance(item, (OnboardTaskAction, OnboardFlightAction))
                    else item
                    for item in tasks
                ),
                frozenset(
                    item.value
                    if isinstance(item, (OnboardTaskAction, OnboardFlightAction))
                    else item
                    for item in flights
                ),
                frozenset(north),
                frozenset(east),
            )
        )

    target_variants = (
        ("red-car-match", "vehicle", "red", True, True, 0.96, 0.94),
        ("green-unknown", "vehicle", "green", None, None, 0.55, 0.35),
        ("blue-class-mismatch", "person", "blue", False, True, 0.68, 0.24),
        ("yellow-color-mismatch", "vehicle", "yellow", True, False, 0.72, 0.42),
    )
    for (
        suffix,
        target_class,
        target_color,
        class_match,
        color_match,
        confidence,
        match_score,
    ) in target_variants:
        context = _context(rng, suffix)
        event = _event(
            suffix,
            OnboardEventKind.TARGET_MOVED,
            at_s=context.captured_at_s,
            task_phase=OnboardTaskAction.SEARCH,
            target_bearing_deg=rng.choice((-60.0, -30.0, 30.0, 60.0)),
            target_distance_m=40.0 + rng.random() * 180.0,
            target_confidence=confidence,
            target_match_score=match_score,
            target_class=target_class,
            target_color=target_color,
            target_class_match=class_match,
            target_color_match=color_match,
        )
        add(
            suffix,
            "target_matching",
            context,
            event,
            (
                OnboardTaskAction.SEARCH,
                OnboardTaskAction.INSPECT_TARGET,
                OnboardTaskAction.MATCH_TARGET,
            ),
            (
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            ),
        )

    # The model contract carries only resolved match booleans, not class/color
    # strings. Keep every numeric input identical so the report can check that
    # changing labels does not change either model head.
    invariant_context = _context(rng, "class-color-invariant-base")
    invariant_labels = (
        ("red", "vehicle"),
        ("blue", "truck"),
        ("yellow", "person"),
        ("white", "boat"),
    )
    for index, (target_color, target_class) in enumerate(invariant_labels):
        case_id = f"class-color-invariant-{index + 1}"
        add(
            case_id,
            "class_color_invariance",
            invariant_context,
            _event(
                case_id,
                OnboardEventKind.TARGET_MOVED,
                at_s=invariant_context.captured_at_s,
                task_phase=OnboardTaskAction.SEARCH,
                target_bearing_deg=30.0,
                target_distance_m=90.0,
                target_confidence=0.86,
                target_match_score=0.82,
                target_class=target_class,
                target_color=target_color,
                target_class_match=True,
                target_color_match=True,
            ),
            (
                OnboardTaskAction.SEARCH,
                OnboardTaskAction.INSPECT_TARGET,
                OnboardTaskAction.MATCH_TARGET,
            ),
            (
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            ),
        )

    low = "low-confidence-match"
    low_ctx = _context(rng, low)
    add(
        low,
        "target_matching",
        low_ctx,
        _event(
            low,
            OnboardEventKind.TARGET_MOVED,
            at_s=low_ctx.captured_at_s,
            task_phase=OnboardTaskAction.INSPECT_TARGET,
            target_bearing_deg=20.0,
            target_distance_m=220.0,
            target_confidence=0.12,
            target_match_score=0.18,
            target_class="vehicle",
            target_color="red",
            target_class_match=True,
            target_color_match=True,
        ),
        (
            OnboardTaskAction.SEARCH,
            OnboardTaskAction.INSPECT_TARGET,
            OnboardTaskAction.MATCH_TARGET,
        ),
        (
            OnboardFlightAction.MOVE_RELATIVE,
            OnboardFlightAction.GOTO_RELATIVE,
            OnboardFlightAction.SEARCH,
            OnboardFlightAction.FOLLOW_TARGET,
        ),
    )

    for phase, accepted_tasks, accepted_flights in (
        (
            OnboardTaskAction.TAKEOFF,
            (OnboardTaskAction.TAKEOFF, OnboardTaskAction.SEARCH),
            (OnboardFlightAction.GOTO_RELATIVE, OnboardFlightAction.MOVE_RELATIVE),
        ),
        (
            OnboardTaskAction.INSPECT_TARGET,
            (
                OnboardTaskAction.INSPECT_TARGET,
                OnboardTaskAction.MATCH_TARGET,
                OnboardTaskAction.SEARCH,
            ),
            (
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.FOLLOW_TARGET,
                OnboardFlightAction.SEARCH,
            ),
        ),
        (
            OnboardTaskAction.MATCH_TARGET,
            (OnboardTaskAction.MATCH_TARGET, OnboardTaskAction.RECORD_LOCATION),
            # RECORD_LOCATION is intentionally paired with RETURN_HOME by the
            # shared flight-action compatibility contract.
            (
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.FOLLOW_TARGET,
                OnboardFlightAction.RETURN_HOME,
            ),
        ),
        (
            OnboardTaskAction.RECORD_LOCATION,
            # A mission-complete receipt at RECORD_LOCATION is promoted by the
            # runtime contract to RETURN_HOME before it can reach PX4.
            (OnboardTaskAction.RECORD_LOCATION, OnboardTaskAction.RETURN_HOME),
            (OnboardFlightAction.RETURN_HOME,),
        ),
        (
            OnboardTaskAction.RETURN_HOME,
            (OnboardTaskAction.RETURN_HOME, OnboardTaskAction.REPORT),
            (OnboardFlightAction.RETURN_HOME, OnboardFlightAction.HOLD_POSITION),
        ),
        (
            OnboardTaskAction.REPORT,
            (OnboardTaskAction.REPORT,),
            (OnboardFlightAction.HOLD_POSITION, OnboardFlightAction.LAND),
        ),
    ):
        case_id = f"phase-{phase.value}"
        context = _context(rng, case_id)
        event_kind = (
            OnboardEventKind.MISSION_COMPLETE
            if phase in {OnboardTaskAction.RECORD_LOCATION, OnboardTaskAction.RETURN_HOME}
            else OnboardEventKind.TARGET_MOVED
        )
        add(
            case_id,
            "phase_transitions",
            context,
            _event(
                case_id,
                event_kind,
                at_s=context.captured_at_s,
                task_phase=phase,
                target_bearing_deg=35.0,
                target_distance_m=80.0,
                target_confidence=0.85,
                target_match_score=0.8,
                target_class="vehicle",
                target_color="red",
                target_class_match=True,
                target_color_match=True,
            ),
            accepted_tasks,
            accepted_flights,
        )

    health_cases = (
        (
            "gps-degraded",
            OnboardEventKind.GPS_DEGRADED,
            {"gps_fix_type": 1, "gps_horizontal_accuracy_m": 80.0},
            (OnboardTaskAction.HOLD, OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND),
            (
                OnboardFlightAction.HOLD_POSITION,
                OnboardFlightAction.RETURN_HOME,
                OnboardFlightAction.LAND,
            ),
        ),
        (
            "battery-low",
            OnboardEventKind.BATTERY_LOW,
            {"battery_remaining_pct": 9.0, "estimated_flight_time_s": 90.0},
            (OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND),
            (OnboardFlightAction.RETURN_HOME, OnboardFlightAction.LAND),
        ),
        (
            "obstacle-risk",
            OnboardEventKind.OBSTACLE_RISK,
            {},
            (OnboardTaskAction.HOLD, OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND),
            (
                OnboardFlightAction.HOLD_POSITION,
                OnboardFlightAction.RETURN_HOME,
                OnboardFlightAction.LAND,
            ),
        ),
        (
            "inference-fault",
            OnboardEventKind.INFERENCE_FAULT,
            {"perception_available": False},
            (OnboardTaskAction.HOLD, OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND),
            (
                OnboardFlightAction.HOLD_POSITION,
                OnboardFlightAction.RETURN_HOME,
                OnboardFlightAction.LAND,
            ),
        ),
    )
    for suffix, kind, context_overrides, tasks, flights in health_cases:
        context = _context(rng, suffix, **context_overrides)
        add(
            suffix,
            "health_faults",
            context,
            _event(
                suffix,
                kind,
                at_s=context.captured_at_s,
                severity=85,
                task_phase=OnboardTaskAction.SEARCH,
                obstacle_bearing_deg=-25.0,
                obstacle_distance_m=8.0,
            ),
            tasks,
            flights,
        )

    conflict = "multi-fault-conflict"
    conflict_ctx = _context(
        rng,
        conflict,
        battery_remaining_pct=7.0,
        estimated_flight_time_s=60.0,
        gps_fix_type=1,
        gps_horizontal_accuracy_m=90.0,
        perception_available=False,
    )
    add(
        conflict,
        "multi_fault_conflicts",
        conflict_ctx,
        _event(
            conflict,
            OnboardEventKind.BATTERY_LOW,
            at_s=conflict_ctx.captured_at_s,
            severity=100,
            task_phase=OnboardTaskAction.SEARCH,
            detail="battery_low + gps_degraded + obstacle_risk + inference_fault",
            obstacle_bearing_deg=25.0,
            obstacle_distance_m=4.0,
        ),
        (OnboardTaskAction.RETURN_HOME, OnboardTaskAction.LAND, OnboardTaskAction.HOLD),
        (
            OnboardFlightAction.RETURN_HOME,
            OnboardFlightAction.LAND,
            OnboardFlightAction.HOLD_POSITION,
        ),
    )

    for side, bearing, east_sign in (("left", -60.0, "negative"), ("right", 60.0, "positive")):
        case_id = f"bearing-{side}"
        context = _context(rng, case_id)
        add(
            case_id,
            "navigation_bearing",
            context,
            _event(
                case_id,
                OnboardEventKind.TARGET_MOVED,
                at_s=context.captured_at_s,
                task_phase=OnboardTaskAction.SEARCH,
                target_bearing_deg=bearing,
                target_distance_m=75.0,
                target_confidence=0.9,
                target_match_score=0.8,
                target_class="vehicle",
                target_color="red",
                target_class_match=True,
                target_color_match=True,
            ),
            (
                OnboardTaskAction.SEARCH,
                OnboardTaskAction.INSPECT_TARGET,
                OnboardTaskAction.MATCH_TARGET,
            ),
            (
                OnboardFlightAction.MOVE_RELATIVE,
                OnboardFlightAction.GOTO_RELATIVE,
                OnboardFlightAction.SEARCH,
                OnboardFlightAction.FOLLOW_TARGET,
            ),
            north=("positive", "zero"),
            east=(east_sign, "zero"),
        )

    return tuple(cases)


def _raw_heads(runner: OnboardOnnxModelRunner, model_input: OnboardModelInput) -> np.ndarray:
    features = model_input.feature_vector()
    if runner.input_feature_width == 38:
        features = model_input.legacy_feature_vector()
    vector = np.asarray([features], dtype=np.float32)
    raw = runner.session.run([runner.output_name], {runner.input_name: vector})
    if not isinstance(raw, (list, tuple)) or len(raw) != 1:
        raise ValueError("ONNX runner returned an invalid output list")
    logits = np.asarray(raw[0], dtype=np.float32)
    if logits.shape != (1, 20):
        raise ValueError(f"r14 generalization expects [1, 20] model heads, got {logits.shape}")
    if not np.isfinite(logits).all():
        raise ValueError("ONNX model heads contain non-finite values")
    return logits[0]


def _raw_record(logits: np.ndarray) -> dict[str, Any]:
    task_logits = logits[:9]
    flight_logits = logits[9:16]
    regression_logits = logits[16:20]
    north = float(np.tanh(regression_logits[0]) * 500.0)
    east = float(np.tanh(regression_logits[1]) * 500.0)
    # Keep this diagnostic decoder identical to the shared ONNX runtime.  A
    # previous 300 m scale made the report disagree with the actual 120 m
    # reviewed altitude envelope.
    altitude = min(120.0, float((1.0 / (1.0 + np.exp(-regression_logits[2]))) * 120.0))
    speed = float((1.0 / (1.0 + np.exp(-regression_logits[3]))) * 50.0)
    return {
        "task_logits": [float(v) for v in task_logits],
        "flight_logits": [float(v) for v in flight_logits],
        "regression_logits": [float(v) for v in regression_logits],
        "raw_task_action": TASK_ORDER[int(np.argmax(task_logits))].value,
        "raw_flight_action": FLIGHT_ORDER[int(np.argmax(flight_logits))].value,
        "raw_navigation": {
            "north_offset_m": north,
            "east_offset_m": east,
            "altitude_m": altitude,
            "speed_mps": speed,
            "north_sign": _sign(north),
            "east_sign": _sign(east),
            "range_valid": -500.0 <= north <= 500.0 and -500.0 <= east <= 500.0,
        },
    }


def evaluate_cases(runner: OnboardOnnxModelRunner, cases: Iterable[PressureCase]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    group_stats: dict[str, dict[str, int]] = {}
    for case in cases:
        raw = _raw_heads(runner, case.model_input)
        raw_record = _raw_record(raw)
        guarded = runner.infer(case.model_input)
        guarded_task = guarded.task_action.value if guarded.task_action is not None else None
        guarded_flight = (
            guarded.flight_plan.action.value if guarded.flight_plan is not None else None
        )
        guarded_north = (
            guarded.flight_plan.north_offset_m if guarded.flight_plan is not None else 0.0
        )
        guarded_east = guarded.flight_plan.east_offset_m if guarded.flight_plan is not None else 0.0
        raw_task_enum = OnboardTaskAction(raw_record["raw_task_action"])
        raw_flight_enum = OnboardFlightAction(raw_record["raw_flight_action"])
        allowed_raw_tasks = allowed_task_actions_for_event(
            case.model_input.event.kind,
            case.model_input.event.task_phase,
        )
        raw_phase_compatible = (
            allowed_raw_tasks is None or raw_task_enum in allowed_raw_tasks
        )
        raw_head_passed = (
            raw_record["raw_task_action"] in case.accepted_tasks
            and raw_record["raw_flight_action"] in case.accepted_flights
            and raw_phase_compatible
            and is_task_flight_compatible(raw_task_enum, raw_flight_enum)
        )
        direction_passed = (
            _sign(guarded_north) in case.expected_north_signs
            and _sign(guarded_east) in case.expected_east_signs
        )
        range_passed = -500.0 <= guarded_north <= 500.0 and -500.0 <= guarded_east <= 500.0
        guarded_compatibility_passed = (
            guarded_task in case.accepted_tasks
            and guarded_flight in case.accepted_flights
            and is_task_flight_compatible(
                guarded.task_action,
                guarded.flight_plan.action if guarded.flight_plan is not None else None,
            )
            and (
                (allowed_raw_tasks is None)
                or guarded.task_action in allowed_raw_tasks
            )
        )
        guarded_passed = (
            guarded_compatibility_passed
            and direction_passed
            and range_passed
        )
        masked_success = (not raw_head_passed) and guarded_passed
        stats = group_stats.setdefault(
            case.group,
            {
                "cases": 0,
                "raw_head_passed": 0,
                "guarded_passed": 0,
                "masked_success": 0,
                "direction_passed": 0,
                "range_passed": 0,
            },
        )
        stats["cases"] += 1
        stats["raw_head_passed"] += int(raw_head_passed)
        stats["guarded_passed"] += int(guarded_passed)
        stats["masked_success"] += int(masked_success)
        stats["direction_passed"] += int(direction_passed)
        stats["range_passed"] += int(range_passed)
        results.append(
            {
                "case_id": case.case_id,
                "group": case.group,
                "context": case.model_input.context.to_mapping(),
                "event": case.model_input.event.to_mapping(),
                "accepted_task_actions": sorted(case.accepted_tasks),
                "accepted_flight_actions": sorted(case.accepted_flights),
                "raw_model_heads": raw_record,
                "guarded_result": {
                    "task_action": guarded_task,
                    "flight_action": guarded_flight,
                    "response": guarded.response.value,
                    "reason": guarded.reason,
                    "confidence": guarded.confidence,
                    "navigation": {
                        "north_offset_m": guarded_north,
                        "east_offset_m": guarded_east,
                        "north_sign": _sign(guarded_north),
                        "east_sign": _sign(guarded_east),
                        "range_valid": range_passed,
                    },
                },
                "raw_head_passed": raw_head_passed,
                "raw_phase_compatible": raw_phase_compatible,
                "raw_task_flight_compatible": is_task_flight_compatible(
                    raw_task_enum, raw_flight_enum
                ),
                "guarded_compatibility_passed": guarded_compatibility_passed,
                "guarded_passed": guarded_passed,
                "masked_success_not_model_learning": masked_success,
                "navigation_direction_passed": direction_passed,
                "navigation_range_passed": range_passed,
                "truth_free": True,
            }
        )

    total = len(results)
    raw_count = sum(int(item["raw_head_passed"]) for item in results)
    guarded_count = sum(int(item["guarded_passed"]) for item in results)
    failures = [item["case_id"] for item in results if not item["guarded_passed"]]
    invariant_results = [item for item in results if item["group"] == "class_color_invariance"]
    invariant_raw = {
        json.dumps(item["raw_model_heads"], sort_keys=True) for item in invariant_results
    }
    invariant_guarded = {
        json.dumps(item["guarded_result"], sort_keys=True) for item in invariant_results
    }
    class_color_invariance = {
        "case_count": len(invariant_results),
        "raw_heads_identical": len(invariant_raw) == 1,
        "guarded_results_identical": len(invariant_guarded) == 1,
        "passed": bool(invariant_results)
        and len(invariant_raw) == 1
        and len(invariant_guarded) == 1,
    }
    return {
        "case_count": total,
        "raw_head_passed": raw_count,
        "guarded_passed": guarded_count,
        "guarded_failures": failures,
        "raw_head_pass_rate": raw_count / total if total else 0.0,
        "guarded_pass_rate": guarded_count / total if total else 0.0,
        "masked_success_count": sum(
            int(item["masked_success_not_model_learning"]) for item in results
        ),
        "navigation_direction_passed": sum(
            int(item["navigation_direction_passed"]) for item in results
        ),
        "navigation_range_passed": sum(int(item["navigation_range_passed"]) for item in results),
        "class_color_invariance": class_color_invariance,
        "group_statistics": group_stats,
        "cases": results,
    }


def evaluate_model(model_path: Path, *, seed: int = SEED) -> dict[str, Any]:
    model_path = model_path.resolve()
    runner = OnboardOnnxModelRunner.from_path(model_path, providers=("CPUExecutionProvider",))
    result = evaluate_cases(runner, build_pressure_cases(seed))
    result.update(
        {
            "schema_version": 1,
            "contract": "multidetect.onboard-policy-generalization-report",
            "generated_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "seed": seed,
            "model_path": str(model_path),
            "model_bytes": model_path.stat().st_size,
            "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
            "runner": "multidetect.autonomy.OnboardOnnxModelRunner",
            "training_used": False,
            "truth_free": True,
            "gazebo_truth_used": False,
            "candidate_promoted": False,
            "final_release_allowed": False,
            "status": "completed_with_failures"
            if result["guarded_failures"]
            else "completed_no_guarded_failures",
        }
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    try:
        report = evaluate_model(args.model, seed=args.seed)
        exit_code = 0
    except Exception as exc:  # preserve a diagnostic artifact on runtime failure
        report = {
            "schema_version": 1,
            "contract": "multidetect.onboard-policy-generalization-report",
            "generated_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "seed": args.seed,
            "model_path": str(args.model.resolve()),
            "status": "failed",
            "failure": f"{type(exc).__name__}: {exc}",
            "training_used": False,
            "truth_free": True,
            "gazebo_truth_used": False,
            "candidate_promoted": False,
            "final_release_allowed": False,
        }
        exit_code = 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "status": report["status"],
                "guarded_failures": report.get("guarded_failures", []),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
