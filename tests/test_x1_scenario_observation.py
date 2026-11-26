from __future__ import annotations

import json

import pytest

from multidetect.autonomy.x1_contingency_state import EVENT_TTL_MS, GROUPS, X1StateBuilder
from multidetect.sim_teacher.x1_scenario_observation import (
    TARGET_FOUND_TTL_MS,
    project_target_found,
    validate_target_found,
)


@pytest.fixture
def values():
    return {
        "track_ref": "injected:lead-1",
        "class": "vehicle",
        "bearing_deg": -84,
        "elevation_deg": -12.5,
        "position_estimate": {"lat_deg": 47.4, "lon_deg": 8.55, "alt_amsl_m": 480, "error_bound_m": 18},
    }


def test_projection_preserves_bounded_estimate_and_injection_provenance(values):
    result = project_target_found(values, observed_ms=100, now_ms=140)
    assert result == {
        "source": "fault", "t_ms": 100, "age_ms": 40,
        "target_status": {"visible": True, "track_state": "tentative", "track_ref": "injected:lead-1"},
        "detection": values,
    }
    assert type(result["detection"]["position_estimate"]["alt_amsl_m"]) is float
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    result["detection"]["position_estimate"]["lat_deg"] = 0
    result["target_status"]["track_ref"] = "different"
    assert values["position_estimate"]["lat_deg"] == 47.4
    assert project_target_found(values, observed_ms=100, now_ms=141)["target_status"]["track_ref"] == "injected:lead-1"


@pytest.mark.parametrize("bearing,elevation", [(-180, -90), (180, 90)])
def test_accepts_body_angle_limits(values, bearing, elevation):
    result = validate_target_found({**values, "bearing_deg": bearing, "elevation_deg": elevation})
    assert (result["bearing_deg"], result["elevation_deg"]) == (bearing, elevation)


def test_projection_windows_respect_report_time_cutoff_and_target_ttl(values):
    start = 100
    assert TARGET_FOUND_TTL_MS == GROUPS["target"][0] == 2000
    last = start + TARGET_FOUND_TTL_MS
    assert project_target_found(values, observed_ms=start, now_ms=start - 1) is None
    assert project_target_found(values, observed_ms=start, now_ms=start)["age_ms"] == 0
    assert project_target_found(values, observed_ms=start, now_ms=last)["age_ms"] == TARGET_FOUND_TTL_MS
    assert project_target_found(values, observed_ms=start, now_ms=last + 1, end_ms=last + 100) is None
    assert project_target_found(values, observed_ms=start, now_ms=102, end_ms=103) is not None
    assert project_target_found(values, observed_ms=start, now_ms=103, end_ms=103) is None


def test_projection_maps_to_one_state_observation_and_event(values):
    builder = X1StateBuilder()
    source = project_target_found(values, observed_ms=100, now_ms=100)
    builder.observe(source["t_ms"], "target", source["target_status"], source=source["source"])
    builder.event(source["t_ms"], "target_found", source=source["source"], severity="info")
    snap = builder.snapshot(140, mission_id="evidence-follow", revision=1)
    assert snap["telemetry"]["target"] == {
        "visible": True, "track_state": "tentative", "track_ref": "injected:lead-1", "identity_confidence": None,
        "source": "fault", "age_ms": 40,
    }
    assert snap["events"] == [{"id": 1, "type": "target_found", "t_ms": 100, "source": "fault",
                               "confidence": None, "severity": "info"}]
    builder.event(141, "target_lost", source="tracker")
    expired = builder.snapshot(100 + TARGET_FOUND_TTL_MS + 1, mission_id="evidence-follow", revision=1)
    assert expired["telemetry"]["target"] is None
    assert [event["type"] for event in expired["events"]] == ["target_found", "target_lost"]
    assert builder.snapshot(141 + EVENT_TTL_MS + 1, mission_id="evidence-follow", revision=1)["events"] == []
    with pytest.raises(ValueError, match="unknown event kind or source"):
        builder.event(142 + EVENT_TTL_MS, "target_unknown", source="fault")


@pytest.mark.parametrize("field,bad", [
    ("track_ref", ""), ("track_ref", "lead\nnext"), ("class", "a" * 49), ("class", 42),
    ("bearing_deg", -181), ("bearing_deg", 181), ("bearing_deg", True),
    ("elevation_deg", -91), ("elevation_deg", 91), ("elevation_deg", float("nan")),
    ("position_estimate", []),
    ("position_estimate.lat_deg", "47.4"), ("position_estimate.lat_deg", 91),
    ("position_estimate.lon_deg", -181), ("position_estimate.lon_deg", float("inf")),
    ("position_estimate.alt_amsl_m", -501), ("position_estimate.alt_amsl_m", 10_001),
    ("position_estimate.alt_amsl_m", 10**400),
    ("position_estimate.error_bound_m", 0), ("position_estimate.error_bound_m", -1),
    ("position_estimate.error_bound_m", 1001), ("position_estimate.error_bound_m", False),
])
def test_rejects_invalid_field(values, field, bad):
    path = field.split(".")
    parent = values if len(path) == 1 else values[path[0]]
    parent[path[-1]] = bad
    with pytest.raises(ValueError, match=field.replace(".", r"\.")):
        validate_target_found(values)


def test_rejects_missing_fields_extra_metadata_and_truth_fields(values):
    with pytest.raises(ValueError, match="target_found.values"):
        validate_target_found({**values, "source": "tracker"})
    with pytest.raises(ValueError, match="target_found.values"):
        validate_target_found({key: value for key, value in values.items() if key != "class"})
    with pytest.raises(ValueError, match="target_found.position_estimate"):
        validate_target_found({**values, "position_estimate": {**values["position_estimate"], "truth_pose": [0, 0, 0]}})
    del values["position_estimate"]["error_bound_m"]
    with pytest.raises(ValueError, match="target_found.position_estimate"):
        project_target_found(values, observed_ms=100, now_ms=0)


@pytest.mark.parametrize("overrides,field", [
    ({"observed_ms": True}, "observed_ms"), ({"observed_ms": -1}, "observed_ms"),
    ({"now_ms": 100.0}, "now_ms"), ({"now_ms": -1}, "now_ms"),
    ({"end_ms": "102"}, "end_ms"), ({"end_ms": False}, "end_ms"), ({"end_ms": 100}, "end_ms"),
])
def test_rejects_invalid_clock(values, overrides, field):
    times = {"observed_ms": 100, "now_ms": 100, **overrides}
    with pytest.raises(ValueError, match=field):
        project_target_found(values, **times)
