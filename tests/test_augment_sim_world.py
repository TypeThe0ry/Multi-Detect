# ruff: noqa: E501

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scripts.augment_sim_world import transform


def _args(**overrides: object) -> Namespace:
    values = dict(
        wind_mps=6.0,
        gust_mps=3.0,
        gust_period=4.0,
        camera_noise=None,
        time_of_day="dusk",
        fog=True,
        actors=2,
        terrain="procedural",
    )
    values.update(overrides)
    return Namespace(**values)


def test_transform_adds_wind_gust_time_and_local_actors(tmp_path: Path) -> None:
    source = Path("simulation/gazebo/worlds/gen_cnt2_w0000.sdf")
    output = tmp_path / "variant.sdf"
    report = transform(source, output, _args())
    root = ET.parse(output).getroot()
    world = root.find("world")
    assert world is not None
    assert world.findtext("wind/linear_velocity") == "6 0 0"
    assert world.find("plugin[@name='gz::sim::systems::WindEffects']/gust_period") is not None
    assert len(world.findall("actor")) == 2
    assert (tmp_path / "actor_box.dae").exists()
    assert report["camera_sensors_noised"] == 0
    assert report["fog_note"]


def test_camera_noise_only_changes_explicit_world_sensors(tmp_path: Path) -> None:
    source = tmp_path / "camera.sdf"
    source.write_text(
        "<sdf version='1.9'><world name='w'><model name='m'><link name='l'><sensor name='c' type='camera'><camera/></sensor></link></model></world></sdf>",
        encoding="utf-8",
    )
    output = tmp_path / "camera_variant.sdf"
    report = transform(
        source,
        output,
        _args(wind_mps=None, gust_mps=None, actors=0, terrain=None, fog=False, camera_noise=0.02),
    )
    assert report["camera_sensors_noised"] == 1
    assert ET.parse(output).findtext(".//noise/stddev") == "0.02"


def test_pinned_mono_cam_overlay_contains_rgb_noise() -> None:
    path = Path("simulation/gazebo/model-overlays/mono_cam_noise/model.sdf")
    root = ET.parse(path).getroot()
    sensor = root.find(".//sensor[@name='camera']")
    assert sensor is not None
    assert sensor.findtext("camera/noise/type") == "gaussian"
    assert sensor.findtext("camera/noise/stddev") == "0.01"


@pytest.mark.parametrize("name", ["x500_mono_cam_wind", "x500_mono_cam_seg_wind"])
def test_wind_overlays_enable_wind_for_the_whole_vehicle(name) -> None:
    # Model-level enable_wind reaches the merged x500 links; redeclaring a merged
    # link such as base_link would collide with the included one.
    model = ET.parse(f"simulation/gazebo/model-overlays/{name}/model.sdf").getroot().find("model")
    assert model.findtext("enable_wind") == "true"
    assert model.find("link[@name='base_link']") is None


def test_wind_with_segmentation_mounts_one_x500_overlay(tmp_path) -> None:
    from scripts import collect_sim_teacher_episodes as collector

    stem = "configs/scenarios/variants/gen_cnt2_w0000__wind6_gust3p4"
    plan = collector.CollectionPlan(
        f"{stem}.scenario.json",
        f"{stem}.runtime_input.json",
        f"{stem}.truth.json",
        "explore",
        1,
        1,
        0,
        str(tmp_path),
        segmentation=True,
        wind_vehicle_overlay=True,
    )
    command = collector.docker_command(plan, name="md-test-wind")
    targets = [t for t in command if "target=/opt/multidetect-gazebo-models/x500_mono_cam," in t]
    assert len(targets) == 1 and "x500_mono_cam_seg_wind" in targets[0]
