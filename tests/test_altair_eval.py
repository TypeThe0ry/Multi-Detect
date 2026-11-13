from scripts.altair import eval_altair as ea

BASE = {"planning": {"plan_valid_rate": 0.95, "plan_invalid_rate": 0.02},
        "identity": {"multiview_consistency": 0.92, "distractor_false_accept": 0.02,
                     "recovery_rate": 0.9, "average_recovery_observations": 1.5,
                     "true_target_false_reject": 0.05, "fire_hydrant_false_reject": 0.1}}


def cand(**over):
    c = {t: dict(m) for t, m in BASE.items()}
    c["fixed_wing"] = {}
    for k, v in over.items():
        t, m = k.split("__")
        c.setdefault(t, {})[m] = v
    return c


GATES = {**ea.DEFAULT_GATES, "absolute": {**ea.DEFAULT_GATES["absolute"],
                                          "fixed_wing": {"cruise_hold_rate": {"min": 0.9}}}}
BASE["identity"] = {"distractor_rejection_rate": 1.0, "identity_accuracy": 0.9}


def test_pass():
    c = cand(fixed_wing__cruise_hold_rate=0.95)
    assert ea.evaluate(BASE, c, GATES)["pass"]


def test_regression_fails():
    r = ea.evaluate(BASE, cand(fixed_wing__cruise_hold_rate=0.95, planning__plan_valid_rate=0.90), GATES)
    assert not r["pass"] and any("plan_valid_rate" in x for x in r["reasons"])


def test_lower_is_better_direction():
    r = ea.evaluate(BASE, cand(fixed_wing__cruise_hold_rate=0.95, planning__plan_invalid_rate=0.01), GATES)
    assert r["pass"]
    r = ea.evaluate(BASE, cand(fixed_wing__cruise_hold_rate=0.95, planning__plan_invalid_rate=0.2), GATES)
    assert not r["pass"]


def test_fixed_wing_fails_closed():
    assert not ea.evaluate(BASE, cand(), GATES)["pass"]  # gated FW metric missing
    assert not ea.evaluate(BASE, {k: v for k, v in cand().items() if k != "fixed_wing"}, GATES)["pass"]


def test_identity_absolute_gate():
    r = ea.evaluate({}, cand(identity__distractor_rejection_rate=0.9, fixed_wing__cruise_hold_rate=0.95), GATES)
    assert any("distractor_rejection_rate" in x for x in r["reasons"])
