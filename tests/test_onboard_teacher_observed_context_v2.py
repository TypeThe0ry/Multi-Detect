"""CPU-only source-bound v2 admission and retained-model boundary fixtures."""

from dataclasses import replace

import pytest
from test_onboard_continuation import _compile, _inputs
from test_onboard_teacher_observed_context import fixture as frozen_fixture
from test_onboard_teacher_observed_context import rewrite_row

from multidetect.autonomy.contracts import ContextSnapshot
from multidetect.autonomy.onboard_agent import (
    OnboardEventKind,
    OnboardModelInput,
    OnboardRuntimeEvent,
)
from scripts.onboard_teacher_observed_context_v2 import build_observed_teacher_context_v2
from scripts.onboard_teacher_telemetry_capture_v2 import TAG, encode_typed_value


@pytest.fixture
def fixture(tmp_path):
    return frozen_fixture.__wrapped__(tmp_path)


def test_v2_known_endurance_preserves_source_and_no_air_sensor_assumption(fixture):
    result = build_observed_teacher_context_v2(**fixture)
    context = result.context
    assert context.schema_version == 2
    assert ContextSnapshot.from_mapping(context.to_mapping()) == context
    assert context.airspeed_mps is None
    assert context.provenance.airspeed.kind == "unknown"
    assert context.provenance.endurance.kind == "autopilot_reported"
    assert context.provenance.wind.kind == "explicit_SIM_wind_model"
    assert context.estimated_flight_time_s > 1199
    assert not any(result.to_mapping()["claims"].values())
    assert result.context.flight_capability is None


def test_actual_unavailable_values_are_recorded_not_zero_or_permission(fixture):
    rewrite_row(fixture, "VFR_HUD", "airspeed", {TAG: encode_typed_value(float("nan"))})
    rewrite_row(fixture, "BATTERY_STATUS", "current_battery", -1)
    rewrite_row(fixture, "BATTERY_STATUS", "time_remaining", 0)
    result = build_observed_teacher_context_v2(**fixture)
    context = result.context
    assert context.airspeed_mps is None and context.estimated_flight_time_s is None
    assert context.provenance.endurance.kind == "unknown"
    assert result.qualification_details["planning_inputs_complete"] is False
    assert result.qualification_details["missing_values_filled_with_zero"] is False
    assert result.qualification_details["VFR_HUD_airspeed_finite"] is False


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("VFR_HUD", "airspeed", -1),
        ("VFR_HUD", "airspeed", {TAG: encode_typed_value(float("inf"))}),
        ("BATTERY_STATUS", "time_remaining", -1),
        ("BATTERY_STATUS", "current_battery", -2),
        ("HEARTBEAT", "system_status", 0),
        ("HEARTBEAT", "base_mode", 128),
        ("GPS_RAW_INT", "h_acc", 2001),
        ("SYS_STATUS", "errors_comm", 1),
    ],
)
def test_invalid_values_and_preflight_remain_rejected(fixture, kind, field, value):
    rewrite_row(fixture, kind, field, value)
    with pytest.raises(ValueError):
        build_observed_teacher_context_v2(**fixture)


def test_source_tamper_remains_rejected(fixture):
    path = fixture["artifact_root"] / fixture["resource_source"].path
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        build_observed_teacher_context_v2(**fixture)


def test_old_feature_model_rejects_v2_even_when_values_known(fixture):
    context = build_observed_teacher_context_v2(**fixture).context
    with pytest.raises(ValueError, match="38/40-feature"):
        OnboardModelInput(
            context, OnboardRuntimeEvent("fixture", OnboardEventKind.TARGET_LOST, 10.2)
        )


def test_old_continuation_cannot_bypass_new_capability_semantics(fixture):
    _, old, _, _, _ = _inputs()
    source = build_observed_teacher_context_v2(**fixture).context
    context = replace(
        old,
        schema_version=2,
        provenance=source.provenance,
        airspeed_mps=None,
    )
    with pytest.raises(ValueError, match="v2 capability/source"):
        _compile(current_context=context)
