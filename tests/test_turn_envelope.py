import hashlib
import json
import math
from pathlib import Path

import pytest

from multidetect.simulation.px4_parameter_snapshot import parse_parameter_observation
from multidetect.simulation.turn_envelope import coordinated_turn_envelope


def evaluate(speed=15.0, radius=30.0, bank=50.0):
    return coordinated_turn_envelope(true_airspeed_mps=speed, radius_m=radius, bank_limit_deg=bank)


def test_reference_radius_is_speed_dependent():
    cruise, maximum = evaluate(), evaluate(20)
    assert cruise["within_configured_bank_limit"] is True
    assert maximum["within_configured_bank_limit"] is False
    assert cruise["minimum_radius_at_bank_limit_m"] == pytest.approx(19.2518, abs=0.001)
    assert maximum["minimum_radius_at_bank_limit_m"] == pytest.approx(34.2255, abs=0.001)
    assert cruise["required_bank_deg"] < 38
    assert maximum["required_bank_deg"] > 53
    for report in (cruise, maximum):
        for key in (
            "aircraft_capability_proven",
            "ground_track_feasibility_proven",
            "flight_authorized",
        ):
            assert report[key] is False


@pytest.mark.parametrize("field", ["speed", "radius", "bank"])
@pytest.mark.parametrize("invalid", [True, False, None, "15", 0, -1, math.nan, math.inf, -math.inf])
def test_invalid_inputs(field, invalid):
    with pytest.raises(ValueError):
        evaluate(**{field: invalid})


@pytest.mark.parametrize("bank", [90, 100, 180])
def test_bank_domain(bank):
    with pytest.raises(ValueError):
        evaluate(bank=bank)


@pytest.mark.parametrize("speed", [1e308, 1e-300])
def test_unrepresentable_geometry(speed):
    with pytest.raises(ValueError, match="unrepresentable"):
        evaluate(speed)


def test_inverse_radius_and_speed_scaling():
    report = evaluate()
    limit = report["minimum_radius_at_bank_limit_m"]
    assert evaluate(radius=limit * (1 + 1e-9))["within_configured_bank_limit"]
    assert not evaluate(radius=limit * (1 - 1e-9))["within_configured_bank_limit"]
    assert evaluate(30)["minimum_radius_at_bank_limit_m"] == pytest.approx(4 * limit)
    assert evaluate(radius=120)["maximum_speed_at_radius_mps"] == pytest.approx(
        2 * report["maximum_speed_at_radius_mps"]
    )


def test_force_balance_at_reported_bank():
    report = evaluate(20)
    bank = math.radians(report["required_bank_deg"])
    lift_per_mass = report["required_load_factor"] * 9.80665
    assert lift_per_mass * math.cos(bank) == pytest.approx(9.80665)
    assert lift_per_mass * math.sin(bank) == pytest.approx(20**2 / 30)


def test_groundspeed_and_cas_are_not_silent_aliases():
    for name in ("groundspeed_mps", "calibrated_airspeed_mps"):
        with pytest.raises(TypeError):
            coordinated_turn_envelope(**{name: 15}, radius_m=30, bank_limit_deg=50)


def test_recorded_snapshot_comparison_is_reproducible():
    root = Path(__file__).resolve().parents[1]
    evidence = json.loads((root / "docs/evidence/turn-envelope-i-20260908.json").read_text())
    snapshot_path = root / evidence["source_path"].replace("\\", "/")
    assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == evidence["source_sha256"]
    snapshot = json.loads(snapshot_path.read_text())
    values = {
        name: parse_parameter_observation(name, observation)
        for name, observation in snapshot["raw_parameter_outputs"].items()
    }
    for row in evidence["reports"]:
        assert row["cas_to_tas_assumption"]
        report = coordinated_turn_envelope(
            true_airspeed_mps=values[row["reference_parameter"]],
            radius_m=evidence["reference_radius_m"],
            bank_limit_deg=values["FW_R_LIM"],
        )
        for key, value in report.items():
            if type(value) is float:
                assert row[key] == pytest.approx(value)
            else:
                assert row[key] == value
