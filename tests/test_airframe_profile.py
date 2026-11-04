from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile, AirframeProfileError

ROOT = Path(__file__).parents[1]
DEFAULTS = sorted((ROOT / "configs" / "airframes").glob("*.json"))


def test_all_default_profiles_load_and_round_trip(tmp_path: Path) -> None:
    assert len(DEFAULTS) == 10
    for source in DEFAULTS:
        profile = AirframeProfile.from_json(source)
        destination = tmp_path / source.name
        profile.to_json(destination)
        loaded = AirframeProfile.from_json(destination)
        assert loaded == profile
        assert json.loads(destination.read_text(encoding="utf-8")) == profile.to_mapping()


def test_x500_regression_baseline() -> None:
    profile = AirframeProfile.from_json(ROOT / "configs" / "airframes" / "x500_mono_cam.json")
    assert profile.endurance.hover_power_w == 360.0
    assert profile.endurance.cruise_power_w == 400.0
    assert profile.endurance.climb_power_w == 500.0
    assert profile.cruise_airspeed_mps == 5.0
    assert profile.can_stop_near_target()


def test_derived_behavior_and_radius() -> None:
    profile = AirframeProfile.from_json(ROOT / "configs" / "airframes" / "rc_cessna_hand.json")
    assert profile.thinking_behavior()["mode_choice"] == "loiter"
    assert not profile.can_stop_near_target()
    assert profile.orbit_radius(100.0) == 100.0
    assert profile.orbit_radius(1.0) == profile.loiter_radius_m()
    assert profile.min_turn_radius_m > profile.physical_turn_radius_m()


def test_fixed_wing_cannot_hover() -> None:
    raw = json.loads(
        (ROOT / "configs" / "airframes" / "rc_cessna_hand.json").read_text(encoding="utf-8")
    )
    raw["can_hover"] = True
    raw["hover_modes"] = ["multirotor"]
    with pytest.raises(AirframeProfileError):
        AirframeProfile.from_mapping(raw)


def test_hand_launch_requires_detection() -> None:
    raw = json.loads(
        (ROOT / "configs" / "airframes" / "rc_cessna_hand.json").read_text(encoding="utf-8")
    )
    raw["takeoff"].pop("launch_detection")
    with pytest.raises(AirframeProfileError):
        AirframeProfile.from_mapping(raw)


def test_turn_radius_physical_lower_bound() -> None:
    raw = json.loads(
        (ROOT / "configs" / "airframes" / "rc_cessna_hand.json").read_text(encoding="utf-8")
    )
    raw["min_turn_radius_m"] = 0.01
    with pytest.raises(AirframeProfileError):
        AirframeProfile.from_mapping(raw)


def test_tailsitter_requires_both_camera_attitudes() -> None:
    raw = json.loads(
        (ROOT / "configs" / "airframes" / "quadtailsitter.json").read_text(encoding="utf-8")
    )
    raw["camera"]["attitude_rotations"].pop("fw")
    with pytest.raises(AirframeProfileError):
        AirframeProfile.from_mapping(raw)


def test_delta_tailsitter_report_limits_and_energy() -> None:
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/delta_tailsitter.json")
    assert (profile.category, profile.subtype, profile.px4_autostart) == (
        "vtol", "tailsitter", 4050
    )
    assert (profile.stall_airspeed_mps, profile.min_airspeed_mps,
            profile.cruise_airspeed_mps, profile.max_airspeed_mps) == (10, 12, 15, 19)
    assert profile.transition.minimum_airspeed_mps == 13
    energy = profile.endurance
    assert energy.battery_wh == 14.8 * 5
    available = energy.battery_wh * (1 - energy.reserve_fraction)
    assert available / energy.hover_power_w * 60 == pytest.approx(7.104)
    assert available / energy.cruise_power_w * 60 == pytest.approx(22.2)
    assert "未经实飞验证" in " ".join(profile.notes)


def test_vtol_transition_command_values_are_fixed() -> None:
    raw = json.loads(
        (ROOT / "configs" / "airframes" / "standard_vtol.json").read_text(encoding="utf-8")
    )
    raw["transition"]["mc_to_fw_param1"] = 3.0
    with pytest.raises(AirframeProfileError):
        AirframeProfile.from_mapping(raw)


def test_unknown_parameter_must_be_marked_unverified() -> None:
    raw = json.loads(
        (ROOT / "configs" / "airframes" / "rc_cessna_runway.json").read_text(encoding="utf-8")
    )
    raw["takeoff"]["parameters"]["unknown"] = {
        "value": 1.0,
        "px4_name": "FW_NOT_IN_REFERENCE",
        "unverified": False,
    }
    with pytest.raises(AirframeProfileError):
        AirframeProfile.from_mapping(raw)
    raw["takeoff"]["parameters"]["unknown"]["unverified"] = True
    assert AirframeProfile.from_mapping(raw).takeoff.parameters["unknown"].unverified
