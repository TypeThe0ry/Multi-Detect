"""Exercise policy wiring with inert detector/slow-system constructors."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image

from multidetect.autonomy.ground_model import DemGround, GroundProjection
from multidetect.simulator.camera_projection import pixel_ground_point
from scripts import eval_slow_system, onboard_policy


@pytest.mark.parametrize("mission", [None, "find"])
def test_policy_loads_ground_model_and_applies_it_to_every_ground_callback(
    monkeypatch, tmp_path, mission
):
    monkeypatch.setattr(onboard_policy, "YoloeProposer", lambda *a, **k: lambda image: [])
    monkeypatch.setattr(eval_slow_system, "LlamaServerRunner", lambda *a, **k: SimpleNamespace())
    config = {
        "mission": mission,
        "ground_model": {
            "type": "dem",
            "path": "simulation/terrain/terrain_pixel_validation_001.dem.json",
            "use_agl": True,
        },
    }
    camera = {"fx": 270.0, "fy": 270.0, "cx": 320.0, "cy": 240.0}
    policy = onboard_policy.OnboardPolicy(
        task_text="Find a car",
        world_path=None,
        altitude_m=20,
        camera_info=camera,
        image_size=(640, 480),
        work_dir=tmp_path,
        config=config,
    )
    assert isinstance(policy.ground_projection.model, DemGround)
    seen = []

    def capture(detections, **kwargs):
        seen.append(kwargs["ground_point"](320, 400))
        return []

    if mission:

        def capture_mission(*args, **kwargs):
            capture([], **kwargs)
            return None, {}

        policy._act_mission = capture_mission
    else:
        monkeypatch.setattr(
            onboard_policy.OnboardLoop, "update_tracks", lambda self, *a, **k: capture(*a, **k)
        )

        def capture_action(current, **kwargs):
            capture([], **kwargs)
            return None, {}

        monkeypatch.setattr(
            onboard_policy.OnboardLoop, "next_action", lambda self, *a, **k: capture_action(*a, **k)
        )

    png = io.BytesIO()
    Image.new("RGB", (640, 480)).save(png, format="PNG")
    obs = {
        "rgb": png.getvalue(),
        "camera_info": camera,
        "pose": {"local_ned_m": [0, 0, -20]},
        "attitude": {"roll_deg": 0, "pitch_deg": 0, "yaw_deg": 0},
    }
    for measurement in (None, {"height_m": 12, "age_s": 0.2}):
        obs["ground_agl"] = measurement
        seen.clear()
        policy.act(obs, pose_ned=(0, 0, -20), yaw_deg=0)
        expected = pixel_ground_point(
            320,
            400,
            pose=obs,
            camera_info=camera,
            image_size=(640, 480),
            ground_model=policy.ground_projection.for_observation(obs),
        )
        assert expected is not None
        assert seen and all(point == expected for point in seen)


def test_unconfigured_policy_selects_original_projection():
    obs = {"ground_agl": {"height_m": 12, "age_s": 0.2}, "pose": {"local_ned_m": [0, 0, -20]}}
    assert GroundProjection().for_observation(obs) is None
