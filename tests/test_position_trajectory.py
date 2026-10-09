from copy import deepcopy

import pytest

from multidetect.simulation.position_trajectory import (
    bracket_position_receipt,
    summarize_position_capture,
)


def capture():
    return dict(
        schema_version=1,
        message_type="GLOBAL_POSITION_INT",
        clock="observer_relative_host_receive_time",
        truncated=False,
        samples_seen=3,
        samples_dropped=0,
        samples=[
            dict(
                time_boot_ms=1000 * i,
                lat=13000000,
                lon=1038000000,
                relative_alt=10000 - 1000 * i,
                vx=0,
                vy=0,
                vz=100,
                received_at_s=2 * i + 0.1,
                source_system_id=1,
                source_component_id=1,
                missing_integer_fields=[],
            )
            for i in range(3)
        ],
    )


def test_source_time_not_receive_time_used_for_velocity_integral():
    result = summarize_position_capture(capture())
    assert result["source_boot_span_s"] == 2
    assert result["receive_span_s"] == pytest.approx(4)
    assert result["observed_net_down_m"] == 2
    assert result["source_time_trapezoidal_vz_integral_m"] == 2
    assert result["net_down_minus_vz_integral_m"] == 0
    assert result["training_label_eligible"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("truncated", True),
        ("samples_dropped", 1),
        ("samples_seen", 4),
        ("clock", "unix"),
    ],
)
def test_invalid_capture_metadata(field, value):
    data = capture()
    data[field] = value
    with pytest.raises(ValueError):
        summarize_position_capture(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("time_boot_ms", 0),
        ("received_at_s", -1),
        ("vz", None),
        ("relative_alt", float("nan")),
        ("source_system_id", 2),
        ("source_system_id", True),
        ("missing_integer_fields", ["vz"]),
    ],
)
def test_malformed_or_unordered_sample(field, value):
    data = capture()
    data["samples"][1][field] = value
    with pytest.raises(ValueError):
        summarize_position_capture(data)


def test_input_is_not_modified_and_sparse_data_is_not_continuous_proof():
    data = capture()
    data["samples"][2]["time_boot_ms"] = 12000
    before = deepcopy(data)
    result = summarize_position_capture(data)
    assert data == before
    assert result["maximum_source_gap_s"] == 11
    assert result["continuous_coverage_proven"] is False


def test_receipt_brackets_do_not_interpolate_exact_ties():
    data = capture()
    result = bracket_position_receipt(data, data["samples"][1]["received_at_s"])
    assert result["before"]["time_boot_ms"] == 0
    assert result["after"]["time_boot_ms"] == 2000
    assert result["equal_receipt_sample_count"] == 1
    assert result["source_event_position_bounds_proven"] is False


@pytest.mark.parametrize("at", [0, 0.1, 4.1, 5])
def test_outside_or_endpoint_events_not_extrapolated(at):
    result = bracket_position_receipt(capture(), at)
    assert not result["strictly_bracketed"]
    assert result["receive_gap_s"] is None


@pytest.mark.parametrize("at", [True, -1, float("inf"), float("nan")])
def test_invalid_bracket_event(at):
    with pytest.raises(ValueError):
        bracket_position_receipt(capture(), at)
