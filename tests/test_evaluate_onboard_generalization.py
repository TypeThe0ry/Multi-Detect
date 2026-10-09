from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multidetect.autonomy import OnboardOnnxModelRunner, OnboardTaskAction  # noqa: E402
from scripts.evaluate_onboard_generalization import (  # noqa: E402
    build_pressure_cases,
    evaluate_cases,
)


class _Input:
    name = "features"
    shape = [1, 40]


class _Output:
    name = "heads"


class _Session:
    def __init__(self, logits: np.ndarray) -> None:
        self.logits = logits

    def get_inputs(self):
        return [_Input()]

    def get_outputs(self):
        return [_Output()]

    def run(self, names, feeds):
        assert names == ["heads"]
        assert tuple(feeds["features"].shape) == (1, 40)
        return [self.logits]


def test_pressure_cases_are_deterministic_and_cover_required_groups() -> None:
    first = build_pressure_cases(20261005)
    second = build_pressure_cases(20261005)
    assert [case.case_id for case in first] == [case.case_id for case in second]
    assert len(first) >= 16
    groups = {case.group for case in first}
    assert {
        "target_matching",
        "phase_transitions",
        "health_faults",
        "multi_fault_conflicts",
        "navigation_bearing",
        "class_color_invariance",
    } <= groups
    assert {
        case.model_input.event.target_class for case in first if case.group == "target_matching"
    } >= {
        "vehicle",
        "person",
    }
    assert any(case.model_input.event.target_class_match is None for case in first)
    assert any(
        case.model_input.event.target_confidence is not None
        and case.model_input.event.target_confidence < 0.2
        for case in first
    )
    assert {
        case.model_input.event.target_bearing_deg
        for case in first
        if case.group == "navigation_bearing"
    } == {-60.0, 60.0}


def test_evaluation_reports_raw_heads_separately_from_guarded_masking() -> None:
    # Raw heads prefer TAKEOFF and RETURN_HOME.  In the SEARCH phase the
    # shared runner must mask both to SEARCH and MOVE_RELATIVE, respectively.
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 0] = 9.0  # raw TAKEOFF, phase mask rejects it
    logits[0, 1] = 2.0  # guarded SEARCH fallback
    logits[0, 9 + 5] = 9.0  # raw RETURN_HOME, task compatibility rejects it
    logits[0, 9 + 1] = 2.0  # guarded MOVE_RELATIVE fallback
    cases = tuple(
        case for case in build_pressure_cases(20261005) if case.case_id == "red-car-match"
    )
    result = evaluate_cases(OnboardOnnxModelRunner(_Session(logits)), cases)
    assert result["case_count"] == 1
    item = result["cases"][0]
    assert item["raw_model_heads"]["raw_task_action"] == OnboardTaskAction.TAKEOFF.value
    assert item["raw_model_heads"]["raw_flight_action"] == "return_home"
    assert item["guarded_result"]["task_action"] == OnboardTaskAction.SEARCH.value
    assert item["guarded_result"]["flight_action"] == "move_relative"
    assert item["raw_head_passed"] is False
    assert item["guarded_passed"] is True
    assert item["masked_success_not_model_learning"] is True


def test_evaluation_records_navigation_signs_ranges_and_never_promotes() -> None:
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 1] = 4.0
    logits[0, 9 + 1] = 4.0
    logits[0, 16] = 0.5
    logits[0, 17] = -0.5
    cases = tuple(
        case for case in build_pressure_cases(20261005) if case.group == "navigation_bearing"
    )
    result = evaluate_cases(OnboardOnnxModelRunner(_Session(logits)), cases)
    assert result["case_count"] == 2
    assert result["navigation_range_passed"] == 2
    assert result["navigation_direction_passed"] == 1
    assert result["guarded_passed"] == 1
    assert all("north_sign" in item["guarded_result"]["navigation"] for item in result["cases"])
    assert all("east_sign" in item["raw_model_heads"]["raw_navigation"] for item in result["cases"])


def test_evaluation_rejects_incompatible_raw_task_and_flight_pair() -> None:
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 5] = 8.0  # raw RETURN_HOME
    logits[0, 9 + 0] = 8.0  # raw HOLD_POSITION; incompatible with RETURN_HOME
    case = next(
        case for case in build_pressure_cases(20261005) if case.case_id == "phase-return_home"
    )
    result = evaluate_cases(OnboardOnnxModelRunner(_Session(logits)), (case,))
    item = result["cases"][0]
    assert item["raw_task_flight_compatible"] is False
    assert item["raw_head_passed"] is False


def test_class_and_color_strings_do_not_change_runner_heads() -> None:
    logits = np.zeros((1, 20), dtype=np.float32)
    logits[0, 1] = 4.0
    logits[0, 9 + 1] = 4.0
    cases = tuple(
        case for case in build_pressure_cases(20261005) if case.group == "class_color_invariance"
    )
    result = evaluate_cases(OnboardOnnxModelRunner(_Session(logits)), cases)
    assert result["class_color_invariance"] == {
        "case_count": 4,
        "raw_heads_identical": True,
        "guarded_results_identical": True,
        "passed": True,
    }
