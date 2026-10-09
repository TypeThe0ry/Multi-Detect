import copy
import math

import pytest
from test_roll_response import capture

from multidetect.simulation.roll_response import inspect_attitude_neighbors


def test_exact_and_missing_boundaries_not_filled():
    data = capture()
    assert all(r["status"] == "exact_label" for r in inspect_attitude_neighbors(data)["samples"])
    for row in data["streams"]["ATTITUDE"]["samples"]:
        row["time_boot_ms"] += 1
    original = copy.deepcopy(data)
    result = inspect_attitude_neighbors(data)
    assert result["samples"][0]["status"] == "unbracketed"
    row = result["samples"][1]
    assert row["status"] == "bracketed_labels"
    assert [e["source_offset_ms"] for e in row["endpoints"]] == [-249, 1]
    assert row["bracket_width_ms"] == 250
    assert "interpolated_roll_deg" not in row
    assert result["intermediate_error_bound_proven"] is False
    assert data == original


def test_endpoint_wrap_is_not_a_full_rotation():
    data = capture()
    rows = data["streams"]["ATTITUDE"]["samples"]
    for i, row in enumerate(rows):
        row["time_boot_ms"] += 1
        row["roll"] = math.radians(179 if i == 0 else -179)
    assert inspect_attitude_neighbors(data)["samples"][1]["observed_endpoint_roll_change_deg"] == 2


@pytest.mark.parametrize("mutation", ["units", "nan", "reset", "truncated"])
def test_invalid_capture_rejected(mutation):
    data = capture()
    stream = data["streams"]["ATTITUDE"]
    if mutation == "units":
        data["units"]["ATTITUDE"]["time_boot_ms"] = "host_ms"
    elif mutation == "nan":
        stream["samples"][0]["roll"] = math.nan
    elif mutation == "reset":
        stream["samples"][1]["time_boot_ms"] = 0
    else:
        stream["truncated"] = True
    with pytest.raises(ValueError):
        inspect_attitude_neighbors(data)
