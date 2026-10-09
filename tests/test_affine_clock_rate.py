import hashlib
import json
from fractions import Fraction

import pytest

from scripts.audit_affine_clock_rate import assess_holdout, audit_outer, describe_affine_probes
from scripts.clock_offset_probe import describe_probes


def samples(points):
    return [
        {"container_ns": c, "host_before_ns": h, "host_after_ns": h + 1, "container_id": "a" * 64}
        for c, h in points
    ]


def fraction(value):
    return Fraction(value["numerator"], value["denominator"])


@pytest.mark.parametrize("rate", [1, 2, 3])
def test_known_rates_fit_without_qualifying_training(rate):
    result = describe_affine_probes(samples([(i * 100, i * 100 * rate + 7) for i in range(4)]))
    low, high = map(fraction, result["conditional_rate_interval"])
    assert low <= rate <= high
    assert result["constant_offset_compatible"] == (rate == 1)
    assert not result["training_label_eligible"]
    assert not result["extrapolation_allowed"]
    assert not result["source_clock_alignment_claimed"]


def test_three_non_affine_probes_reject_not_average():
    result = describe_affine_probes(samples([(0, 0), (100, 100), (200, 300)]))
    assert not result["affine_compatible"]
    assert result["conditional_rate_interval"] is None
    assert fraction(result["rate_constraints"]["lower"]) > fraction(
        result["rate_constraints"]["upper"]
    )


def test_large_origins_preserve_sub_float_rate_difference():
    origin, delta = 10**25, 10**18
    points = [(origin + i * delta, origin + i * (delta + 3)) for i in range(3)]
    result = describe_affine_probes(samples(points))
    low, high = map(fraction, result["conditional_rate_interval"])
    assert 1 < low <= Fraction(delta + 3, delta) <= high


def test_resolution_can_allow_open_zero_bound_but_never_zero_rate():
    rows = samples([(0, 0), (100, 2)])
    for row in rows:
        row["host_resolution_ns"] = 10
    result = describe_affine_probes(rows)
    assert result["affine_compatible"]
    assert fraction(result["rate_constraints"]["lower"]) == 0
    assert not result["rate_constraints"]["lower_inclusive"]


def test_mixed_container_rejected():
    rows = samples([(0, 0), (100, 100)])
    rows[1]["container_id"] = "b" * 64
    with pytest.raises(ValueError):
        describe_affine_probes(rows)


@pytest.mark.parametrize("shift,status", [(0, "compatible"), (40, "incompatible")])
def test_holdout_checks_consistency_without_refitting(shift, status):
    endpoints = samples([(c, c) for c in (0, 10, 20, 100, 110, 120)])
    held = samples([(c, c + shift) for c in (30, 40, 50)])
    original = describe_affine_probes(endpoints)
    result = assess_holdout(endpoints, held)
    assert result["status"] == status
    assert result["endpoint_conditional_rate_interval"] == original["conditional_rate_interval"]
    assert not result["endpoint_fit_modified"]
    assert not result["training_label_eligible"]


def test_holdout_must_be_interior_and_same_container():
    endpoints = samples([(c, c) for c in (0, 10, 20, 100, 110, 120)])
    with pytest.raises(ValueError, match="overlapping"):
        assess_holdout(endpoints, samples([(c, c) for c in (130, 140, 150)]))
    held = samples([(c, c) for c in (30, 40, 50)])
    held[0]["container_id"] = "b" * 64
    with pytest.raises(ValueError, match="mismatch"):
        assess_holdout(endpoints, held)
    with pytest.raises(ValueError, match="six endpoint"):
        assess_holdout(endpoints, held[:2])


def test_outer_hash_and_capture_binding(tmp_path):
    rows = samples([(0, 0), (100, 100)])
    capture = {
        "exit_code": 0,
        "container_id": "a" * 64,
        "host_container_probes": rows,
        "host_container_offset": describe_probes(rows),
    }
    outer = {"gate_passed": True, "gates": {"clock_capture": True}, "clock_capture": capture}
    path = tmp_path / "outer.json"
    path.write_text(json.dumps(outer), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert audit_outer(path, digest)["affine_compatible"]
    with pytest.raises(ValueError, match="hash"):
        audit_outer(path, "0" * 64)
    capture["container_id"] = "b" * 64
    path.write_text(json.dumps(outer), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        audit_outer(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_outer_holdout_declaration_and_binding(tmp_path):
    rows = samples([(c, c) for c in (0, 10, 20, 100, 110, 120)])
    held = samples([(c, c) for c in (30, 40, 50)])
    capture = {
        "exit_code": 0,
        "container_id": "a" * 64,
        "host_container_probes": rows,
        "host_container_offset": describe_probes(rows),
        "host_container_holdout": {
            "phase": "after_sensor_complete_before_mission_wait",
            "probes": held,
            "used_for_endpoint_fit": False,
            "mission_window_membership_verified": False,
        },
    }
    outer = {"gate_passed": True, "gates": {"clock_capture": True}, "clock_capture": capture}
    path = tmp_path / "outer.json"

    def audit():
        path.write_text(json.dumps(outer), encoding="utf-8")
        return audit_outer(path, hashlib.sha256(path.read_bytes()).hexdigest())

    assert audit()["holdout_validation"]["status"] == "compatible"
    capture["host_container_holdout"]["used_for_endpoint_fit"] = True
    with pytest.raises(ValueError, match="declaration"):
        audit()
    capture["host_container_holdout"]["used_for_endpoint_fit"] = False
    held[0]["container_id"] = "b" * 64
    with pytest.raises(ValueError, match="mismatch"):
        audit()
