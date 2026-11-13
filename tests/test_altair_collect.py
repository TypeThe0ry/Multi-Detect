import pytest

from scripts.altair import collect_report as cr
from scripts.altair import eval_altair as ea

SUMMARY = {
    "tasks": 20, "valid_rate": 0.7, "exact_sequence_rate": 0.75, "same_skill_set_rate": 0.75,
    "repaired_rate": 0.15, "first_try_valid_rate": 0.55, "median_latency_s": 0.45,
    "category_metrics": {
        "fixed_wing_hover": {"count": 3, "accuracy": 1.0, "rejection_correct_rate": 1.0,
                             "intent_violation_rate": 0.0, "capability_violation_rate": 0.0},
        "fixed_wing_target_land": {"count": 0, "accuracy": 0.0},
        "query_grounding": {"count": 4, "accuracy": 0.5},
    },
}
IDENT = {"harness": "x", "rows": [], "metrics": {
    "identity_accuracy": 0.918, "decision_accuracy": 0.854, "distractor_rejection_rate": 1.0,
    "confirm_recall": 0.709, "json_validity": 1.0, "latency_s": {"p50": 1.0}}}


def test_collect_shapes():
    r = cr.collect(SUMMARY, IDENT)
    assert r["planning"]["valid_rate"] == 0.7 and "repaired_rate" not in r["planning"]
    assert r["fixed_wing"] == {
        "fixed_wing_hover_accuracy": 1.0,
        "fixed_wing_hover_intent_violation_rate": 0.0,
        "fixed_wing_hover_capability_violation_rate": 0.0,
    }
    assert "latency_s" not in r["identity"]


def test_baseline_vs_itself_passes_and_regression_fails():
    base = cr.collect(SUMMARY, IDENT)
    assert ea.evaluate(base, base)["pass"]
    worse = cr.collect({**SUMMARY, "valid_rate": 0.5}, IDENT)
    assert not ea.evaluate(base, worse)["pass"]
    viol = cr.collect({**SUMMARY, "category_metrics": {"fixed_wing_hover": {
        "count": 3, "accuracy": 1.0, "intent_violation_rate": 0.33, "capability_violation_rate": 0.0}}}, IDENT)
    assert not ea.evaluate(base, viol)["pass"]


def test_missing_fixed_wing_category_fails():
    base = cr.collect(SUMMARY, IDENT)
    cand = cr.collect({**SUMMARY, "category_metrics": {}}, IDENT)
    assert not ea.evaluate(base, cand)["pass"]


def test_bad_inputs():
    with pytest.raises(ValueError):
        cr.collect({}, IDENT)
    with pytest.raises(ValueError):
        cr.collect(SUMMARY, {"rows": []})
