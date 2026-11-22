from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import collect_sim_teacher_episodes as collector
from scripts.realworld import rtf_variants


def test_variant_rewrites_world_and_camera_without_touching_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><physics><max_step_size>0.004</max_step_size></physics><scene><shadows>true</shadows></scene><light name="sun"><cast_shadows>true</cast_shadows></light></world></sdf>')
    out = tmp_path / "out"
    result = rtf_variants.generate(source, out, variant="V4", airframe="rc_cessna_mono_cam")
    root = ET.parse(result["world"]).getroot()
    assert root.findtext("world/physics/max_step_size") == "0.008"
    assert root.findtext("world/physics/real_time_update_rate") == "125"
    assert root.findtext("world/scene/shadows") == "false"
    model = ET.parse(Path(result["overlay_root"]) / "mono_cam/model.sdf").getroot()
    sensor = model.find(".//sensor")
    assert sensor.findtext("camera/image/width") == "640"
    assert sensor.findtext("update_rate") == "10"
    assert sensor.find("camera/noise") is None
    assert "real_time_update_rate" not in source.read_text()


def test_v0_has_no_overlay() -> None:
    assert rtf_variants.VARIANTS["V0"]["camera_overlay"] is False


def _sensor_signature(path: Path) -> tuple[tuple, ...]:
    root = ET.parse(path).getroot()
    rows = []
    for sensor in root.iter("sensor"):
        rate = sensor.find("update_rate")
        if rate is not None:
            rate.text = "<rate>"

        def signature(node: ET.Element) -> tuple:
            return (node.tag, tuple(sorted(node.attrib.items())), (node.text or "").strip(),
                    tuple(signature(child) for child in node))

        rows.append(signature(sensor))
    return tuple(rows)


@pytest.mark.parametrize(("variant", "hz"), (("V6", "10"), ("V6_5", "5")))
def test_v6_rate_only_variants_preserve_camera_geometry(tmp_path: Path, variant: str, hz: str) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><physics/></world></sdf>')
    result = rtf_variants.generate(source, tmp_path / variant, variant=variant,
                                  airframe="rc_cessna_mono_cam")
    for source_model, output_model in (
        (rtf_variants.MONO_TEMPLATE / "model.sdf", Path(result["overlay_root"]) / "mono_cam/model.sdf"),
        (rtf_variants.MODEL_ROOT / "rc_cessna_mono_cam/model.sdf",
         Path(result["overlay_root"]) / "rc_cessna_mono_cam/model.sdf"),
    ):
        assert _sensor_signature(source_model) == _sensor_signature(output_model)
        sensors = ET.parse(output_model).getroot().iter("sensor")
        rates = [sensor.findtext("update_rate") for sensor in sensors
                 if sensor.get("type") in {"camera", "depth_camera"}]
        assert rates and set(rates) == {hz}
    assert result["camera"] == {"width": 1280, "height": 960, "update_rate_hz": int(hz)}


def test_v5_uses_budget_profile_and_keeps_terrain_collision(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><model name="rw_terrain">'
                      '<link name="terrain"><collision name="ground"/></link></model></world></sdf>')
    result = rtf_variants.generate(source, tmp_path / "V5", variant="V5",
                                  airframe="rc_cessna_mono_cam")
    assert result["terrain"]["budget_profile"] == "budget_6km"
    assert result["terrain"]["collision_preserved"] is True
    assert ET.parse(result["world"]).find(".//collision[@name='ground']") is not None


def test_v7_removes_building_collision_but_keeps_ground_collision(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><model name="rw_terrain">'
                      '<link name="terrain"><collision name="ground"/><collision name="buildings"/>'
                      '</link></model></world></sdf>')
    result = rtf_variants.generate(source, tmp_path / "V7", variant="V7",
                                  airframe="rc_cessna_mono_cam")
    root = ET.parse(result["world"]).getroot()
    assert root.find(".//collision[@name='ground']") is not None
    assert root.find(".//collision[@name='buildings']") is None
    assert result["collision_policy"]["near_field_terrain_precision_changed"] is False


def test_v8_combines_v6_camera_rate_and_v7_collision_policy(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><model name="rw_terrain">'
                      '<link name="terrain"><collision name="ground"/><collision name="buildings"/>'
                      '</link></model></world></sdf>')
    result = rtf_variants.generate(source, tmp_path / "V8", variant="V8",
                                  airframe="rc_cessna_mono_cam")
    world = ET.parse(result["world"]).getroot()
    assert world.find(".//collision[@name='ground']") is not None
    assert world.find(".//collision[@name='buildings']") is None
    assert result["collision_policy"]["removed"] == ["rw_terrain/buildings"]
    assert result["camera"] == {"width": 1280, "height": 960, "update_rate_hz": 10}
    for model_path in (Path(result["overlay_root"]) / "mono_cam/model.sdf",
                       Path(result["overlay_root"]) / "rc_cessna_mono_cam/model.sdf"):
        sensors = ET.parse(model_path).getroot().iter("sensor")
        camera_sensors = [sensor for sensor in sensors if sensor.get("type") in {"camera", "depth_camera"}]
        assert camera_sensors
        assert {sensor.findtext("update_rate") for sensor in camera_sensors} == {"10"}
        assert all(sensor.findtext("camera/image/width") == "1280" for sensor in camera_sensors)
        assert all(sensor.findtext("camera/image/height") == "960" for sensor in camera_sensors)
    assert rtf_variants.VARIANTS["V6"]["camera_hz"] == 10
    assert rtf_variants.VARIANTS["V7"]["remove_building_collision"] is True


def test_v9_removes_only_gstreamer_and_optical_flow_plugins(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><physics/></world></sdf>')
    server_source = tmp_path / "base-server.config"
    server_source.write_text(
        '<server_config><plugins>'
        '<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics">'
        '<max_step_size>0.004</max_step_size></plugin>'
        '<plugin filename="libGstCameraSystem.so" name="GstCameraSystem"/>'
        '<plugin filename="libOpticalFlowSystem.so" name="OpticalFlowSystem"/>'
        '<plugin filename="gz-sim-magnetometer-system" name="gz::sim::systems::Magnetometer"/>'
        '<plugin filename="gz-sim-apply-link-wrench-system" name="gz::sim::systems::ApplyLinkWrench"/>'
        '</plugins></server_config>',
        encoding="utf-8",
    )

    result = rtf_variants.generate(source, tmp_path / "V9", variant="V9",
                                  airframe="rc_cessna_mono_cam",
                                  server_config_source=server_source)
    root = ET.parse(result["server_config"]).getroot()
    plugins = root.findall("plugins/plugin")
    identities = [(p.get("filename"), p.get("name")) for p in plugins]
    assert all("GstCameraSystem" not in " ".join(row) for row in identities)
    assert all("OpticalFlowSystem" not in " ".join(row) for row in identities)
    assert len(plugins) == 3
    assert plugins[0].attrib == {
        "filename": "gz-sim-physics-system",
        "name": "gz::sim::systems::Physics",
    }
    assert plugins[0].findtext("max_step_size") == "0.004"
    assert any("Magnetometer" in " ".join(row) for row in identities)
    assert any("ApplyLinkWrench" in " ".join(row) for row in identities)
    assert result["camera"]["update_rate_hz"] == 10
    assert Path(result["overlay_root"]).is_dir()


def test_v9_requires_both_target_plugins(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><physics/></world></sdf>')
    server_source = tmp_path / "incomplete.config"
    server_source.write_text('<server_config><plugins><plugin name="GstCameraSystem"/></plugins></server_config>')
    with pytest.raises(ValueError, match="OpticalFlowSystem"):
        rtf_variants.generate(source, tmp_path / "V9", variant="V9",
                              airframe="rc_cessna_mono_cam",
                              server_config_source=server_source)


def test_v9_cli_generates_standalone_without_image_config(tmp_path: Path) -> None:
    source = tmp_path / "source.sdf"
    source.write_text('<sdf version="1.9"><world name="default"><physics/></world></sdf>')
    out = tmp_path / "V9"

    assert rtf_variants.main([
        "--source-world", str(source), "--out", str(out), "--variant", "V9",
    ]) == 0
    plugins = ET.parse(out / "server.config").getroot().findall("plugins/plugin")
    filenames = {plugin.get("filename") for plugin in plugins}
    assert "gz-sim-sensors-system" in filenames
    assert "libGstCameraSystem.so" not in filenames
    assert "libOpticalFlowSystem.so" not in filenames
    assert (out / "models/mono_cam/model.sdf").is_file()


def test_scenario_world_path_uses_explicit_rtf_world_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    scenario_id = "rw_variant"
    world_root = tmp_path / "worlds"
    world_root.mkdir()
    world = world_root / f"{scenario_id}.sdf"
    world.write_text("<sdf/>", encoding="utf-8")
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"scenario_id":"rw_variant"}', encoding="utf-8")
    monkeypatch.setenv("MULTIDETECT_RTF_WORLD_ROOT", str(world_root))
    assert collector.scenario_world_path(runtime) == world


def test_wide_area_delta_overlay_only_changes_rgb_and_depth_rate(tmp_path):
    root = Path("simulation/gazebo")
    delta = "delta_tailsitter_gimbal_cam"
    output = tmp_path / "model.sdf"
    rtf_variants._rewrite_model(root / "models" / delta / "model.sdf", output,
                               width=1280, height=960, hz=10, preserve_camera_geometry=True)
    overlay = root / "models-wide-area" / delta / "model.sdf"
    assert overlay.read_bytes() == output.read_bytes()
    sensors = ET.parse(overlay).getroot().findall(".//sensor")
    assert {s.get("type"): s.findtext("update_rate") for s in sensors
            if s.get("type") in {"camera", "depth_camera"}} == {"camera": "10", "depth_camera": "10"}
