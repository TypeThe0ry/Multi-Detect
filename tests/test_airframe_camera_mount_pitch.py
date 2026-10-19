"""Onboard projection uses the airframe's own camera mount, not the x500's."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from multidetect.autonomy.airframe_profile import AirframeProfile
from multidetect.autonomy.motion_intent import profile_mount_pitch_down_rad
from onboard_factories import make_policy

ROOT = Path(__file__).resolve().parents[1]
SIM_MODEL = {
    "x500_mono_cam": "x500_mono_cam",
    "rc_cessna_runway": "rc_cessna_mono_cam",
    "advanced_plane": "advanced_plane_mono_cam",
    "standard_vtol": "standard_vtol_mono_cam",
    "tiltrotor": "tiltrotor_mono_cam",
}


def _sdf_camera_pitch(model: str) -> float:
    root = ET.fromstring((ROOT / "simulation/gazebo/models" / model / "model.sdf").read_text("utf-8"))
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


def test_tailsitter_per_mode_mounts_fall_back_to_the_default() -> None:
    profile = AirframeProfile.from_json(ROOT / "configs/airframes/quadtailsitter.json")
    assert profile_mount_pitch_down_rad(profile, 0.25) == 0.25


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
