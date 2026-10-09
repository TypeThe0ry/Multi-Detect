import copy
import math

import pytest
from test_mission_dynamics_capture import message

from multidetect.simulation.roll_response import analyze_roll_response
from scripts.mission_dynamics_capture import MissionDynamicsCapture


def capture(rolls=(0, 0, 0, 30, 30, 30)):
    recorder = MissionDynamicsCapture()
    for i, roll in enumerate(rolls):
        half = math.radians(roll) / 2
        recorder.record(
            message(
                "ATTITUDE_TARGET",
                time_boot_ms=i * 250,
                type_mask=7,
                q=[math.cos(half), math.sin(half), 0, 0],
            ),
            i / 4,
        )
        recorder.record(message("ATTITUDE", time_boot_ms=i * 250, roll=0), i / 4 + 0.001)
    return recorder.snapshot()


def analyze(data, **kwargs):
    options = dict(
        lookback_ms=500,
        maximum_gap_ms=300,
        maximum_target_deviation_deg=2,
        quaternion_norm_tolerance=1e-5,
    )
    options.update(kwargs)
    return analyze_roll_response(data, **options)


def test_quiet_and_changing_history_no_future_leak():
    result = analyze(capture())
    assert [r["classification"] for r in result["samples"]] == [
        "insufficient_history",
        "insufficient_history",
        "sampled_near_current_target",
        "sampled_changing_target",
        "sampled_changing_target",
        "sampled_near_current_target",
    ]
    assert result["samples"][3]["roll_error_deg"] == pytest.approx(-30)
    assert result["continuous_target_hold_proven"] is False


def test_gap_and_ignored_target_are_not_stability():
    data = capture()
    assert all(
        r["classification"] == "insufficient_history"
        for r in analyze(data, maximum_gap_ms=200)["samples"]
    )
    data["streams"]["ATTITUDE_TARGET"]["samples"][1]["type_mask"] = 128
    assert analyze(data)["samples"][1]["classification"] == "insufficient_history"


def test_unmatched_targets_classified_without_fabricated_actuals():
    data = capture()
    for row in data["streams"]["ATTITUDE"]["samples"]:
        row["time_boot_ms"] += 1
    result = analyze(data)
    assert result["samples"] == []
    assert len(result["target_samples"]) == 6
    assert result["target_samples"][2]["classification"] == "sampled_near_current_target"
    assert all(not r["exact_actual_source_label_present"] for r in result["target_samples"])
    assert all(
        "actual_received_at_s" not in r and "roll_error_deg" not in r
        for r in result["target_samples"]
    )


def test_ignored_target_retained_as_unusable_in_coverage():
    data = capture()
    data["streams"]["ATTITUDE_TARGET"]["samples"][2]["type_mask"] = 128
    result = analyze(data)
    assert result["target_samples"][2]["classification"] == "unusable_target_attitude"
    assert len(result["samples"]) == 5


def test_wrapped_angles_are_compared_on_circle():
    result = analyze(capture((179.5, -179.5, 179.5)))
    assert result["samples"][-1]["classification"] == "sampled_near_current_target"
    assert result["samples"][-1]["maximum_observed_target_deviation_deg"] == pytest.approx(1)


@pytest.mark.parametrize(
    "option,value",
    [
        ("lookback_ms", True),
        ("lookback_ms", 0),
        ("maximum_gap_ms", -1),
        ("maximum_target_deviation_deg", math.nan),
        ("maximum_target_deviation_deg", 180),
    ],
)
def test_invalid_configuration(option, value):
    with pytest.raises(ValueError):
        analyze(capture(), **{option: value})


def test_incomplete_reset_and_unit_mismatch_rejected():
    base = capture()
    for kind in ("ATTITUDE", "ATTITUDE_TARGET"):
        data = copy.deepcopy(base)
        data["streams"][kind]["truncated"] = True
        with pytest.raises(ValueError):
            analyze(data)
        data = copy.deepcopy(base)
        data["streams"][kind]["samples"][1]["time_boot_ms"] = 0
        with pytest.raises(ValueError):
            analyze(data)
    base["units"]["ATTITUDE"]["roll"] = "deg"
    with pytest.raises(ValueError):
        analyze(base)
