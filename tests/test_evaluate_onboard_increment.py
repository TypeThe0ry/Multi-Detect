from __future__ import annotations

import copy

from scripts.evaluate_onboard_increment import summarize


def _result():
    return {
        "sensor": {
            "task_accuracy": 0.9,
            "flight_accuracy": 0.9,
            "compatible_output_rate": 1.0,
        },
        "generalization": {
            "raw_head_pass_rate": 1.0,
            "guarded_pass_rate": 1.0,
            "guarded_failures": [],
        },
        "geometry": {
            "search": {"thresholds": {"diagnostic_pass": True}},
            "inspect_target": {"thresholds": {"diagnostic_pass": True}},
        },
    }


def test_passing_structured_diagnostics_never_promotes_model():
    result = summarize(_result(), _result())
    assert result["development_diagnostics_passed"] is True
    assert result["candidate_promoted"] is False
    assert result["current_reference_unchanged"] is True
    assert any("rendered 3-D" in reason for reason in result["reason"])


def test_geometry_failure_is_not_hidden_by_better_sensor_accuracy():
    parent = _result()
    candidate = copy.deepcopy(parent)
    candidate["sensor"]["task_accuracy"] = 1.0
    candidate["geometry"]["search"]["thresholds"]["diagnostic_pass"] = False
    result = summarize(parent, candidate)
    assert result["structured_sensor_nonregression"] is True
    assert result["navigation_geometry_passed"] is False
    assert result["development_diagnostics_passed"] is False


def test_action_regression_cannot_be_hidden_by_guard_masking():
    candidate = _result()
    candidate["generalization"]["raw_head_pass_rate"] = 0.5
    result = summarize(_result(), candidate)
    assert result["generalization_nonregression"] is False
    assert result["development_diagnostics_passed"] is False
