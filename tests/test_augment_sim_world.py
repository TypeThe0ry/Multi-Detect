# ruff: noqa: E501

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from xml.etree import ElementTree as ET

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
