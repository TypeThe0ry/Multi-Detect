from __future__ import annotations

import json
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import (
    AirframeProfile,
    AirframeProfileError,
    airframe_profile_category,
    airframe_uses_multirotor_executor,
    resolve_airframe_profile,
)
from scripts.collect_sim_teacher_episodes import _airframe_profile
from scripts.run_sim_session import _canonical_airframe
from scripts.smoke_airframe_flight import profile_for

ROOT = Path(__file__).parents[1]
DEFAULTS = sorted((ROOT / "configs" / "airframes").glob("*.json"))


def test_all_default_profiles_load_and_round_trip(tmp_path: Path) -> None:
    assert len(DEFAULTS) == 11
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


@pytest.mark.parametrize(
    ("airframe", "category"),
    [
        ("x500_mono_cam", "multirotor"),
        ("x500_gimbal", "multirotor"),
        ("rc_cessna_runway", "fixed_wing"),
        ("standard_vtol", "vtol"),
    ],
)
def test_airframe_profile_category_resolves_existing_airframes(airframe: str, category: str) -> None:
    assert airframe_profile_category(airframe, base_dir=ROOT) == category


@pytest.mark.parametrize(
    ("airframe", "expected_profile", "expected_branch"),
    [
        ("x500", None, "multirotor"),
        ("x500_mono_cam", None, "multirotor"),
        ("multirotor", None, "multirotor"),
        ("fixed_wing", None, "fixed_wing"),
        ("fw", None, "fixed_wing"),
        ("vtol", None, "vtol"),
        ("x500_gimbal", "x500_gimbal", "multirotor"),
        ("rc_cessna_mono_cam", "rc_cessna_runway", "fixed_wing"),
        ("rc_cessna_runway", "rc_cessna_runway", "fixed_wing"),
        ("rc_cessna_hand", "rc_cessna_hand", "fixed_wing"),
        ("rc_cessna_catapult", "rc_cessna_catapult", "fixed_wing"),
        ("standard_vtol_mono_cam", "standard_vtol", "vtol"),
        ("standard_vtol", "standard_vtol", "vtol"),
        ("quadtailsitter_mono_cam", "quadtailsitter", "vtol"),
        ("quadtailsitter", "quadtailsitter", "vtol"),
        ("tiltrotor_mono_cam", "tiltrotor", "vtol"),
        ("tiltrotor", "tiltrotor", "vtol"),
        ("advanced_plane_mono_cam", "advanced_plane", "fixed_wing"),
        ("advanced_plane", "advanced_plane", "fixed_wing"),
    ],
)
def test_collector_airframe_ids_share_one_resolution(
    airframe: str, expected_profile: str | None, expected_branch: str
) -> None:
    takeoff = {
        "rc_cessna_hand": "hand",
        "rc_cessna_catapult": "catapult",
    }.get(airframe)
    profile = resolve_airframe_profile(airframe, takeoff, base_dir=ROOT)
    collector_profile = _airframe_profile(airframe, takeoff)
    assert (collector_profile is None) == (profile is None)
    if profile is not None and collector_profile is not None:
        assert collector_profile.id == profile.id
    if expected_profile is None:
        assert profile is None
    else:
        assert profile is not None and profile.id == expected_profile
    category = airframe_profile_category(airframe, takeoff, base_dir=ROOT)
    assert category == expected_branch
    expected_multirotor_branch = airframe in {"x500_mono_cam", "x500_gimbal"}
    assert airframe_uses_multirotor_executor(airframe, takeoff, base_dir=ROOT) == (
        expected_multirotor_branch
    )


@pytest.mark.parametrize(
    ("takeoff", "expected_profile"),
    [("hand", "rc_cessna_hand"), ("catapult", "rc_cessna_catapult")],
)
def test_rc_cessna_mono_cam_takeoff_aliases_share_resolution(
    takeoff: str, expected_profile: str
) -> None:
    profile = resolve_airframe_profile("rc_cessna_mono_cam", takeoff, base_dir=ROOT)
    assert profile is not None and profile.id == expected_profile
    assert airframe_profile_category("rc_cessna_mono_cam", takeoff, base_dir=ROOT) == "fixed_wing"


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("x500", "x500_mono_cam"),
        ("multirotor", "x500_mono_cam"),
        ("standard_vtol_mono_cam", "standard_vtol"),
        ("quadtailsitter_mono_cam", "quadtailsitter"),
        ("tiltrotor_mono_cam", "tiltrotor"),
        ("rc_cessna", "rc_cessna_mono_cam"),
        ("rc_cessna_runway", "rc_cessna_mono_cam"),
        ("rc_cessna_hand", "rc_cessna_mono_cam"),
        ("rc_cessna_catapult", "rc_cessna_mono_cam"),
    ],
)
def test_run_sim_aliases_resolve_to_collector_ids(alias: str, canonical: str) -> None:
    assert _canonical_airframe(alias) == canonical
    # Canonical ids are the values passed to the collector by run_sim_session.
    assert airframe_profile_category(canonical, base_dir=ROOT) in {
        "multirotor",
        "fixed_wing",
        "vtol",
    }


@pytest.mark.parametrize(
    ("airframe", "profile_id"),
    [
        ("rc_cessna_mono_cam", "rc_cessna_runway"),
        ("standard_vtol_mono_cam", "standard_vtol"),
        ("quadtailsitter_mono_cam", "quadtailsitter"),
        ("tiltrotor_mono_cam", "tiltrotor"),
        ("advanced_plane_mono_cam", "advanced_plane"),
    ],
)
def test_smoke_airframe_ids_use_the_same_profiles(airframe: str, profile_id: str) -> None:
    assert profile_for(airframe).id == profile_id


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
            profile.cruise_airspeed_mps, profile.max_airspeed_mps) == (10, 12, 15, 18)
    assert profile.transition.minimum_airspeed_mps == 15
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
