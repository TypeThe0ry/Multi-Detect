from __future__ import annotations

import io
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from multidetect.autonomy.airframe_profile import AirframeProfile, CameraMountProfile
from multidetect.autonomy.fw_search import FixedWingSearch, camera_footprint_width_m
from multidetect.camera_mount import CameraMount, compose_camera_pose
from scripts.onboard_policy import OnboardPolicy

ROOT = Path(__file__).resolve().parents[1]
CAMERA = {"fx": 640.0, "fy": 640.0, "cx": 320.0, "cy": 240.0}
IMAGE_SIZE = (640, 480)


def camera_pose(yaw_deg=0.0, *, fresh=True):
    composed = compose_camera_pose(
        (0.0, 0.0, -30.0),
        vehicle_roll_rad=0.0,
        vehicle_pitch_rad=0.0,
        vehicle_yaw_rad=math.radians(yaw_deg),
        mount=CameraMount.fixed(pitch_rad=math.radians(60.0)),
    )
    return {
        "center_ned": composed.center_ned,
        "optical_to_world": composed.optical_to_world,
        "gimbal_feedback_fresh": fresh,
    }


def observation(pose):
    buffer = io.BytesIO()
    Image.new("RGB", IMAGE_SIZE).save(buffer, format="PNG")
    return {
        "rgb": buffer.getvalue(),
        "camera_info": dict(CAMERA),
        "pose": {"local_ned_m": (0.0, 0.0, -30.0)},
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
        "flight_state": {"vtol_state": 4},
        **({"camera_pose": pose} if pose is not None else {}),
    }


def make_policy(
    tmp_path, *, airframe, mission, gimballed=False, explicit_fixed=False, altitude_m=30.0
):
    profile = AirframeProfile.from_json(ROOT / "configs/airframes" / f"{airframe}.json")
    if gimballed or explicit_fixed:
        raw = profile.to_mapping()
        raw["camera_mount"] = (
            json.loads((ROOT / "configs/airframes/x500_gimbal.json").read_text("utf-8"))[
                "camera_mount"
            ]
            if gimballed
            else CameraMountProfile().to_mapping()
        )
        path = tmp_path / "airframe.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        airframe = str(path)
    return OnboardPolicy(
        task_text="Find a car",
        world_path=None,
        altitude_m=altitude_m,
        camera_info=CAMERA,
        image_size=IMAGE_SIZE,
        work_dir=tmp_path / "frames",
        config={
            "airframe": airframe,
            "mission": mission,
            **({"vtol_search_mode": "fw"} if profile.category == "vtol" else {}),
        },
        proposer=lambda image: [],
        slow_runner=SimpleNamespace(close=lambda: None),
    )


@pytest.mark.parametrize("airframe", ["rc_cessna_runway", "standard_vtol"])
@pytest.mark.parametrize("mission", [None, "find", "region"])
def test_onboard_search_uses_current_gimbal_pose_and_calibration(
    tmp_path, monkeypatch, airframe, mission
):
    subject = make_policy(
        tmp_path, airframe=airframe, mission="find" if mission == "region" else mission,
        gimballed=True,
    )
    if mission == "region":
        subject.executor._run.loop = subject.executor.loop_factory(
            "Find a car", region=(0.0, 0.0, 60.0)
        )
    seen = []
    original = FixedWingSearch.next_action

    def capture(search, **kwargs):
        seen.append((search, kwargs))
        return original(search, **kwargs)

    monkeypatch.setattr(FixedWingSearch, "next_action", capture)
    try:
        # The aircraft stays still: coverage must follow the camera's look
        # direction, and missing/stale feedback must not reuse a prior frame.
        frames = [None, camera_pose(), None, camera_pose(180.0, fresh=False), camera_pose(180.0)]
        grids = []
        for pose in frames:
            subject.act(observation(pose), pose_ned=(0.0, 0.0, -30.0), yaw_deg=0.0)
            assert len(seen) == len(grids) + 1
            search, kwargs = seen[-1]
            assert kwargs["camera_pose"] is pose
            assert search.camera_is_gimballed is True
            assert search.camera_intrinsics == CAMERA
            assert search.camera_image_size == IMAGE_SIZE
            if mission == "region":
                assert search is not subject._search_fw
                assert search._bounds == (-60.0, 60.0, -60.0, 60.0)
            grids.append(dict(search._grid))
        assert not any(grids[0].values())
        assert any(grids[1].values())
        assert grids[2] == grids[1]
        assert grids[3] == grids[1]
        assert sum(grids[4].values()) > sum(grids[1].values())
    finally:
        subject.close()


@pytest.mark.parametrize("airframe", ["rc_cessna_runway", "standard_vtol"])
@pytest.mark.parametrize("explicit_fixed", [False, True])
@pytest.mark.parametrize("mission", [None, "find"])
def test_fixed_camera_search_is_numerically_identical_to_legacy(
    tmp_path, airframe, explicit_fixed, mission
):
    subject = make_policy(
        tmp_path, airframe=airframe, mission=mission, explicit_fixed=explicit_fixed,
        altitude_m=10.0,
    )
    profile = subject.airframe_profile
    legacy = FixedWingSearch(
        home_ned=(0.0, 0.0, 0.0),
        radius_m=subject._search_mc.search_radius_m,
        footprint_width_m=camera_footprint_width_m(CAMERA, 10.0, subject.camera_pitch_down_rad),
        turn_radius_m=profile.min_turn_radius_m or profile.orbit_radius(),
        max_leg_m=profile.limits.max_step_distance_m,
        camera_pitch_rad=subject.camera_pitch_down_rad,
        max_search_calls=100,
        return_reserve_calls=40,
        coverage_enabled=True,
    )
    try:
        search = subject._search_fw
        assert search.camera_is_gimballed is False
        assert search.waypoints == legacy.waypoints
        for index, (position, yaw) in enumerate([
            ((0.0, 0.0, -10.0), 0.0),
            ((30.0, 10.0, -10.0), 35.0),
            ((-20.0, 40.0, -8.0), 90.0),
            ((-45.0, -30.0, -12.0), -170.0),
        ]):
            # Even an explicit composed pose on a fixed mount must retain
            # the old circular/swept coverage calculation.
            subject._search_camera_pose = camera_pose(index * 45.0)
            subject.motion_adapter.set_mode("FW")
            actual = subject.loop.search_action(position, yaw)
            assert actual == legacy.next_action(pose_ned=position, yaw_deg=yaw)
            assert search._grid == legacy._grid
            assert search.coverage_fraction == legacy.coverage_fraction
            assert search.search_outcome == legacy.search_outcome
            assert search.search_calls == legacy.search_calls
            assert search._last_pose == legacy._last_pose
    finally:
        subject.close()
