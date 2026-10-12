"""CPU checks for opt-in Fuel world segmentation labels."""

from __future__ import annotations

import json
import subprocess
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

from multidetect.simulator.stack import _validate_world_sdf
from multidetect.simulator.world_gen import generate_fuel_worlds, generate_worlds


def _fixture_assets(root: Path) -> tuple[Path, Path]:
    assets = []
    resolved = root / "resolved"
    for slug, role in (
        ("red_car", "target"),
        ("blue_car", "target"),
        ("building", "scenery-obstacle"),
    ):
        asset = {
            "slug": slug,
            "role_eligibility": [role],
            "measured_size_m": [2, 2, 2],
            "bbox_min_m": [-1, -1, 0],
            "bbox_max_m": [1, 1, 2],
            "mesh_origin_offset_m": {"z_bottom": 0},
            "category": "car" if role == "target" else "building",
            "color": "red" if slug == "red_car" else "blue",
            "semantic_words": ["car"] if role == "target" else ["building"],
        }
        assets.append(asset)
        directory = resolved / slug
        directory.mkdir(parents=True)
        (directory / "model.sdf").write_text(
            '<sdf version="1.9"><model name="asset"><link name="body">'
            '<visual name="visual"><geometry><mesh><uri>meshes/model.dae</uri>'
            "</mesh></geometry></visual></link></model></sdf>",
            encoding="utf-8",
            newline="\n",
        )
    catalog = root / "catalog.json"
    catalog.write_text(json.dumps({"assets": assets}), encoding="utf-8", newline="\n")
    return catalog, resolved


def _generate(root: Path, catalog: Path, resolved: Path, enabled: bool = False):
    return generate_fuel_worlds(
        count=1,
        seed=20261012,
        out_worlds=root / "simulation/gazebo/worlds",
        out_scenarios=root / "configs/scenarios/generated-v2",
        catalog_path=catalog,
        resolved_root=resolved,
        segmentation_labels=enabled,
    )


def test_labels_are_unique_stable_and_map_all_object_models(tmp_path: Path) -> None:
    catalog, resolved = _fixture_assets(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    _generate(first, catalog, resolved, True)
    _generate(second, catalog, resolved, True)
    world = first / "simulation/gazebo/worlds/gen_v2_w0000.sdf"
    _validate_world_sdf(world, expected_name="default")
    assert world.read_bytes() == (second / "simulation/gazebo/worlds/gen_v2_w0000.sdf").read_bytes()
    truth = json.loads(
        (first / "configs/scenarios/generated-v2/gen_v2_w0000.truth.json").read_text()
    )
    labels = truth["segmentation_labels"]
    models = ET.parse(world).findall("world/model")
    objects = [model for model in models if model.attrib["name"] != "ground_plane"]
    assert len(labels) == len(objects)
    assert set(labels) == {str(index) for index in range(1, len(objects) + 1)}
    for model in objects:
        plugin = model.find("plugin")
        assert plugin is not None
        assert plugin.attrib == {
            "filename": "gz-sim-label-system",
            "name": "gz::sim::systems::Label",
        }
        label = labels[plugin.findtext("label")]
        assert 1 <= int(plugin.findtext("label")) <= 254
        for nested_model in model.findall(".//model"):
            assert nested_model.findtext("plugin/label") == plugin.findtext("label")
        assert label["target_id"] == model.attrib["name"]
        assert label["asset_slug"] in {"red_car", "blue_car", "building"}
    assert {row["target_id"] for row in truth["targets"]} <= {
        row["target_id"] for row in labels.values()
    }


def test_default_fuel_output_matches_pre_segmentation_generator(tmp_path: Path) -> None:
    catalog, resolved = _fixture_assets(tmp_path)
    original = subprocess.run(
        ["git", "show", "HEAD:src/multidetect/simulator/world_gen.py"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    module = types.ModuleType("original_world_gen")
    sys.modules[module.__name__] = module
    try:
        exec(compile(original, "original_world_gen.py", "exec"), module.__dict__)
        original_root = tmp_path / "original"
        module.generate_fuel_worlds(
            count=1,
            seed=20261012,
            out_worlds=original_root / "simulation/gazebo/worlds",
            out_scenarios=original_root / "configs/scenarios/generated-v2",
            catalog_path=catalog,
            resolved_root=resolved,
        )
        current_root = tmp_path / "current"
        _generate(current_root, catalog, resolved)
        for path in original_root.rglob("*"):
            if path.is_file():
                assert (
                    path.read_bytes()
                    == (current_root / path.relative_to(original_root)).read_bytes()
                )
        box_original, box_current = tmp_path / "box-original", tmp_path / "box-current"
        for root, generator in (
            (box_original, module.generate_worlds),
            (box_current, generate_worlds),
        ):
            generator(
                1,
                20261012,
                out_worlds=root / "simulation/gazebo/worlds",
                out_scenarios=root / "scenarios",
            )
        for path in box_original.rglob("*"):
            if path.is_file():
                assert (
                    path.read_bytes() == (box_current / path.relative_to(box_original)).read_bytes()
                )
    finally:
        del sys.modules[module.__name__]
