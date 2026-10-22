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
    # A world <plugin> would replace Gazebo's default systems (SITL: world never ready).
    assert world.findall("plugin") == []
    assert "multidetect-wind-gust amplitude_percent=" in output.read_text(encoding="utf-8")
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


def test_wind_startup_patch_adds_wind_effects_only_for_windy_worlds(tmp_path: Path) -> None:
    import subprocess

    from scripts import run_simulator_docker as docker_plan

    config = tmp_path / "server.config"
    plugins = "<server_config><plugins><plugin name='gz::sim::systems::Physics'/></plugins></server_config>"
    for windy in (True, False):
        config.write_text(plugins, encoding="utf-8")
        world = tmp_path / "world.sdf"
        body = (
            "<!-- multidetect-wind-gust amplitude_percent=0.5 period=4 --><wind><linear_velocity>6 0 0</linear_velocity></wind>"
            if windy
            else ""
        )
        world.write_text(f"<sdf><world name='default'>{body}</world></sdf>", encoding="utf-8")
        script = docker_plan._ENABLE_WIND_EFFECTS.replace(
            docker_plan.CONTAINER_WORLD, world.as_posix()
        ).replace(docker_plan.CONTAINER_SERVER_CONFIG, config.as_posix())
        subprocess.run(["sh", "-c", script], check=True)
        plugin = next(
            (
                p
                for p in ET.parse(config).getroot().iter("plugin")
                if "WindEffects" in p.get("name", "")
            ),
            None,
        )
        if windy:
            assert plugin is not None
            assert plugin.findtext("horizontal/magnitude/sin/amplitude_percent") == "0.5"
            assert plugin.findtext("horizontal/magnitude/sin/period") == "4"
        else:
            assert plugin is None


def _composition(path: str) -> tuple[list[tuple[str, str]], str]:
    model = ET.parse(path).getroot().find("model")
    assert model is not None
    parts = sorted(
        (child.tag, child.get("name") or child.findtext("uri") or "")
        for child in model
        if child.tag in {"link", "joint", "plugin", "include"}
    )
    body = "".join(
        ET.tostring(child, encoding="unicode")
        for child in model
        if child.tag in {"link", "joint", "plugin", "include"}
    )
    return parts, "".join(body.split())


@pytest.mark.parametrize(
    ("overlay", "base"),
    [
        (
            "simulation/gazebo/model-overlays/x500_mono_cam_wind/model.sdf",
            "simulation/gazebo/models/x500_mono_cam/model.sdf",
        ),
        (
            "simulation/gazebo/model-overlays/x500_mono_cam_seg_wind/model.sdf",
            "simulation/gazebo/model-overlays/x500_mono_cam_seg/model.sdf",
        ),
    ],
)
def test_wind_overlays_keep_the_shadowed_model_composition(overlay: str, base: str) -> None:
    # The overlay fully shadows the base model when mounted: it must keep every
    # link/joint/plugin (depth camera, odometry) and only add enable_wind.
    assert _composition(overlay) == _composition(base)


def test_wind_without_gust_still_marks_the_world_for_wind_effects(tmp_path: Path) -> None:
    source = Path("simulation/gazebo/worlds/gen_cnt2_w0000.sdf")
    output = tmp_path / "variant.sdf"
    transform(source, output, _args(gust_mps=None, actors=0, terrain=None, fog=False))
    text = output.read_text(encoding="utf-8")
    assert "multidetect-wind-gust amplitude_percent=0 period=4" in text


@pytest.mark.parametrize(
    "world",
    [
        "simulation/gazebo/worlds/urban_inspection_multirotor_001.sdf",
        "simulation/gazebo/worlds/river_rescue_vtol_001.sdf",
        "simulation/gazebo/worlds/coastal_patrol_fixed_wing_001.sdf",
    ],
)
def test_hand_made_worlds_with_wind_do_not_get_wind_effects(world: str, tmp_path: Path) -> None:
    import subprocess

    from scripts import run_simulator_docker as docker_plan

    assert "<wind>" in Path(world).read_text(encoding="utf-8")
    config = tmp_path / "server.config"
    config.write_text(
        "<server_config><plugins><plugin name='gz::sim::systems::Physics'/></plugins></server_config>",
        encoding="utf-8",
    )
    script = docker_plan._ENABLE_WIND_EFFECTS.replace(
        docker_plan.CONTAINER_WORLD, Path(world).resolve().as_posix()
    ).replace(docker_plan.CONTAINER_SERVER_CONFIG, config.as_posix())
    subprocess.run(["sh", "-c", script], check=True)
    assert "WindEffects" not in config.read_text(encoding="utf-8")


def test_committed_wind_variants_carry_the_wind_effects_marker() -> None:
    for path in Path("simulation/gazebo/worlds/variants").glob("*.sdf"):
        text = path.read_text(encoding="utf-8")
        if "<wind>" in text:
            assert "multidetect-wind-gust amplitude_percent=" in text, path
