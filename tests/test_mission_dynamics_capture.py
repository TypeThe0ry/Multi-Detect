import json
import math
from types import SimpleNamespace

import pytest

from scripts.mission_dynamics_capture import MissionDynamicsCapture


def message(kind="ATTITUDE", source=(1, 1), **fields):
    return SimpleNamespace(
        get_type=lambda: kind,
        get_srcSystem=lambda: source[0],
        get_srcComponent=lambda: source[1],
        **fields,
    )


def test_raw_units_clocks_and_unproven_semantics():
    capture = MissionDynamicsCapture()
    capture.record(
        message(
            time_boot_ms=1234, roll=-0.5, pitch=0, yaw=1, rollspeed=0.1, pitchspeed=0, yawspeed=0
        ),
        5.123,
    )
    capture.record(message("VFR_HUD", airspeed=-0.02, groundspeed=20, alt=100, climb=-2), 5.124)
    result = capture.snapshot()
    attitude = result["streams"]["ATTITUDE"]["samples"][0]
    hud = result["streams"]["VFR_HUD"]["samples"][0]
    assert attitude["time_boot_ms"] == 1234
    assert attitude["roll"] == -0.5
    assert attitude["received_at_s"] == 5.123
    assert hud["airspeed"] == -0.02  # raw finite values, no clipping or TAS claim
    assert "time_boot_ms" not in hud
    assert not attitude["invalid_or_missing_fields"]
    assert not hud["invalid_or_missing_fields"]
    assert result["true_airspeed_semantics_proven"] is False
    assert result["affects_acceptance_gate"] is False


@pytest.mark.parametrize("value", [None, True, "1", math.nan, math.inf])
def test_invalid_float_is_null_and_json_safe(value):
    capture = MissionDynamicsCapture()
    capture.record(message(roll=value), 0)
    result = capture.snapshot()
    row = result["streams"]["ATTITUDE"]["samples"][0]
    assert row["roll"] is None
    assert "roll" in row["invalid_or_missing_fields"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("boot", [-1, 2**32, True, 1.5, None])
def test_invalid_source_clock(boot):
    capture = MissionDynamicsCapture()
    capture.record(message(time_boot_ms=boot), 0)
    row = capture.snapshot()["streams"]["ATTITUDE"]["samples"][0]
    assert row["time_boot_ms"] is None
    assert "time_boot_ms" in row["invalid_or_missing_fields"]


def test_independent_caps_snapshot_isolation_and_foreign_source():
    capture = MissionDynamicsCapture(1)
    for kind in ("ATTITUDE", "VFR_HUD", "NAV_CONTROLLER_OUTPUT", "ATTITUDE_TARGET"):
        for index in range(3):
            capture.record(message(kind, roll=index), index)
        for source in ((2, 1), (1, 2), (True, 1)):
            capture.record(message(kind, source=source), 4)
    result = capture.snapshot()
    for stream in result["streams"].values():
        assert stream["samples_seen"] == 3
        assert stream["samples_dropped"] == 2
        assert stream["truncated"] is True
        assert stream["rejected_source_count"] == 3
        stream["samples"][0]["invalid_or_missing_fields"].clear()
    assert capture.snapshot()["streams"]["ATTITUDE"]["samples"][0]["invalid_or_missing_fields"]


@pytest.mark.parametrize("capacity", [0, 32769, True, 1.5])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        MissionDynamicsCapture(capacity)


@pytest.mark.parametrize("receipt", [-1, True, math.nan, math.inf])
def test_invalid_receipt(receipt):
    with pytest.raises(ValueError):
        MissionDynamicsCapture().record(message(), receipt)


def test_other_message_ignored():
    capture = MissionDynamicsCapture()
    capture.record(message("HEARTBEAT"), 0)
    assert all(s["samples_seen"] == 0 for s in capture.snapshot()["streams"].values())


def test_navigation_outputs_keep_units_and_no_invented_source_clock():
    capture = MissionDynamicsCapture()
    capture.record(
        message(
            "NAV_CONTROLLER_OUTPUT",
            nav_roll=50,
            nav_pitch=-2,
            alt_error=-3,
            aspd_error=1.5,
            xtrack_error=-8,
        ),
        1.25,
    )
    result = capture.snapshot()
    row = result["streams"]["NAV_CONTROLLER_OUTPUT"]["samples"][0]
    assert row["nav_roll"] == 50
    assert row["xtrack_error"] == -8
    assert row["received_at_s"] == 1.25
    assert row["invalid_or_missing_fields"] == []
    assert "time_boot_ms" not in row
    assert result["units"]["NAV_CONTROLLER_OUTPUT"]["nav_roll"] == "deg"
    assert result["nav_controller_source_timestamp_available"] is False


def test_navigation_stream_is_bounded_and_invalid_data_not_zero_filled():
    capture = MissionDynamicsCapture(1)
    capture.record(message("NAV_CONTROLLER_OUTPUT", nav_roll=math.nan), 1)
    capture.record(message("NAV_CONTROLLER_OUTPUT"), 2)
    stream = capture.snapshot()["streams"]["NAV_CONTROLLER_OUTPUT"]
    assert stream["samples_dropped"] == 1
    assert stream["truncated"] is True
    assert stream["samples"][0]["nav_roll"] is None
    assert "nav_roll" in stream["samples"][0]["invalid_or_missing_fields"]


def test_attitude_target_keeps_source_time_quaternion_and_mask():
    capture = MissionDynamicsCapture()
    q = [1.0, 0.0, 0.0, 0.0]
    capture.record(
        message(
            "ATTITUDE_TARGET",
            q=q,
            time_boot_ms=123,
            type_mask=7,
            body_roll_rate=0,
            body_pitch_rate=0,
            body_yaw_rate=0,
            thrust=0.5,
        ),
        2,
    )
    q[0] = 2
    result = capture.snapshot()
    row = result["streams"]["ATTITUDE_TARGET"]["samples"][0]
    assert row["q"] == [1, 0, 0, 0]
    assert row["time_boot_ms"] == 123
    assert row["type_mask"] == 7
    assert row["invalid_or_missing_fields"] == []
    row["q"][0] = 3
    assert capture.snapshot()["streams"]["ATTITUDE_TARGET"]["samples"][0]["q"][0] == 1


@pytest.mark.parametrize(
    "quaternion", [None, [1, 0, 0], [True, 0, 0, 0], [math.nan, 0, 0, 0], "1000"]
)
def test_invalid_target_quaternion_is_explicit(quaternion):
    capture = MissionDynamicsCapture()
    capture.record(message("ATTITUDE_TARGET", q=quaternion, type_mask=True), 0)
    row = capture.snapshot()["streams"]["ATTITUDE_TARGET"]["samples"][0]
    assert row["q"] is None
    assert row["type_mask"] is None
    assert "q" in row["invalid_or_missing_fields"]


def test_finite_nonunit_target_is_preserved_not_repaired():
    capture = MissionDynamicsCapture()
    capture.record(message("ATTITUDE_TARGET", q=[2, 0, 0, 0]), 0)
    row = capture.snapshot()["streams"]["ATTITUDE_TARGET"]["samples"][0]
    assert row["q"] == [2, 0, 0, 0]
