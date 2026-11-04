"""Onboard projection uses the airframe's own camera mount, not the x500's."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from onboard_factories import make_policy

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.motion_intent import profile_mount_pitch_down_rad

ROOT = Path(__file__).resolve().parents[1]
SIM_MODEL = {
    "x500_mono_cam": "x500_mono_cam",
    "rc_cessna_runway": "rc_cessna_mono_cam",
    "advanced_plane": "advanced_plane_mono_cam",
    "standard_vtol": "standard_vtol_mono_cam",
    "tiltrotor": "tiltrotor_mono_cam",
}


def _sdf_camera_pitch(model: str) -> float:
    root = ET.fromstring(
        (ROOT / "simulation/gazebo/models" / model / "model.sdf").read_text("utf-8")
    )
    for joint in root.iter("joint"):
        pose = joint.find("pose")
        if pose is not None and pose.get("relative_to") == "base_link":
            return float(re.split(r"\s+", pose.text.strip())[4])
    raise AssertionError(f"no camera joint pose in {model}")


@pytest.mark.parametrize("profile_id", sorted(SIM_MODEL))
def test_profile_mount_pitch_matches_the_simulated_camera(profile_id: str) -> None:
    profile = AirframeProfile.from_json(ROOT / "configs/airframes" / f"{profile_id}.json")
    assert profile_mount_pitch_down_rad(profile, 0.18) == pytest.approx(
        _sdf_camera_pitch(SIM_MODEL[profile_id]), abs=2e-3
    )


@pytest.mark.parametrize("profile_id", ["quadtailsitter", "delta_tailsitter"])
def test_tailsitter_per_mode_mounts_fall_back_to_the_default(profile_id: str) -> None:
    profile = AirframeProfile.from_json(ROOT / f"configs/airframes/{profile_id}.json")
    assert profile_mount_pitch_down_rad(profile, 0.25) == 0.25


@pytest.mark.parametrize("profile_id", ["quadtailsitter", "delta_tailsitter"])
def test_tailsitter_mc_mount_matches_the_simulated_camera_in_hover(profile_id: str) -> None:
    """The tailsitter model stands on its tail: model +X is the MC body x, model +Z
    the nose (MC -z), so the SDF pitch -20 deg about +Y lifts the axis 20 deg in hover.
    The pitch read from the MC matrix and the full-attitude ray must agree on it."""
    from multidetect.autonomy.motion_intent import camera_ray_from_profile

    profile = AirframeProfile.from_json(ROOT / f"configs/airframes/{profile_id}.json")
    if profile.camera.mount == "gimbal":
        root = ET.fromstring(
            (ROOT / "simulation/gazebo/models/delta_tailsitter/model.sdf").read_text("utf-8")
        )
        assert float(root.findtext("model/joint[@name='c11_pitch_joint']/axis/initial_position")) == pytest.approx(
            -math.radians(45), abs=1e-6
        )
        return
    pitch = profile_mount_pitch_down_rad(profile, 0.0, mode="mc")
    assert pitch == pytest.approx(_sdf_camera_pitch(f"{profile_id}_mono_cam"), abs=2e-3)
    ray = camera_ray_from_profile(
        320, 240, fx=270, fy=270, cx=320, cy=240, attitude_deg=(0, 0, 0), profile=profile
    )
    assert math.asin(ray[2]) == pytest.approx(pitch, abs=1e-9)
    # Same convention on a pitch-down mount (x500 values) through the same function.
    from types import SimpleNamespace

    x500 = AirframeProfile.from_json(ROOT / "configs/airframes/x500_mono_cam.json")
    shaped = SimpleNamespace(
        camera=SimpleNamespace(
            attitude_rotations={}, body_to_camera_rotation=x500.camera.body_to_camera_rotation
        )
    )
    down = camera_ray_from_profile(
        320, 240, fx=270, fy=270, cx=320, cy=240, attitude_deg=(0, 0, 0), profile=shaped
    )
    assert math.asin(down[2]) == pytest.approx(0.18, abs=2e-3)


def test_policy_projects_fixed_wing_frames_with_its_level_camera(tmp_path: Path) -> None:
    assert make_policy(work_dir=tmp_path / "x500").camera_pitch_down_rad == 0.18
    cessna = make_policy(work_dir=tmp_path / "fw", config={"airframe": "rc_cessna_runway"})
    assert cessna.camera_pitch_down_rad == 0.0
    forced = make_policy(
        work_dir=tmp_path / "forced",
        config={"airframe": "rc_cessna_runway", "camera_pitch_down_rad": 0.3},
    )
    assert forced.camera_pitch_down_rad == 0.3
    assert math.isfinite(cessna._search.footprint_width_m)


def test_scorer_projects_truth_with_the_recorded_airframe_camera(tmp_path: Path) -> None:
    import json

    from scripts.score_onboard_loop import episode_camera_pitch_rad

    cessna = AirframeProfile.from_json(ROOT / "configs/airframes/rc_cessna_runway.json")
    fixed_wing = tmp_path / "fw"
    fixed_wing.mkdir()
    (fixed_wing / "episode.json").write_text(
        json.dumps({"airframe_profile": cessna.to_mapping()}), encoding="utf-8"
    )
    assert episode_camera_pitch_rad(fixed_wing) == 0.0
    legacy = tmp_path / "x500"
    legacy.mkdir()
    (legacy / "episode.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    assert episode_camera_pitch_rad(legacy) == 0.18


def test_fixed_wing_policy_flies_passes_and_hands_the_return_to_the_autopilot(
    tmp_path: Path,
) -> None:
    cessna = make_policy(work_dir=tmp_path / "fw", config={"airframe": "rc_cessna_runway"})
    config = cessna.loop.config
    assert config.fixed_wing_pass and config.autopilot_return
    assert config.fw_min_turn_radius_m == pytest.approx(cessna.airframe_profile.loiter_radius_m())
    # Pass orbits are planned inside the final arbiter's fences.
    assert cessna.loop.pass_clear == cessna.safety_envelope.leg_allowed
    x500 = make_policy(work_dir=tmp_path / "x500").loop
    assert not x500.config.fixed_wing_pass and not x500.config.autopilot_return
    assert x500.pass_clear is None
