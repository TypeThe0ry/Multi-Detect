"""CPU checks for deterministic procedural SIM world generation."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from jsonschema import validate

from multidetect.simulator.world_gen import generate_worlds

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    name: json.loads(
        (ROOT / "configs" / "schemas" / f"scenario-{name}.schema.json").read_text(
            encoding="utf-8"
        )
    )
    for name in ("manifest", "runtime-input", "truth", "expectations")
}


def _generate(tmp_path: Path, count: int = 6, seed: int = 20261010, factor: float = 1.0):
    return generate_worlds(
        count,
        seed,
        out_worlds=tmp_path / "simulation/gazebo/worlds",
        out_scenarios=tmp_path / "configs/scenarios/generated",
        real_time_factor=factor,
    )


def test_same_seed_is_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _generate(first)
    _generate(second)
    for left in sorted((first / "simulation/gazebo/worlds").glob("gen_*.sdf")):
        right = second / "simulation/gazebo/worlds" / left.name
        assert left.read_bytes() == right.read_bytes()
    assert (first / "configs/scenarios/generated/index.json").read_bytes() == (
        second / "configs/scenarios/generated/index.json"
    ).read_bytes()


def test_channels_schema_valid_and_index_hashes(tmp_path: Path) -> None:
    _generate(tmp_path)
    directory = tmp_path / "configs/scenarios/generated"
    for suffix, schema_name in (
        ("scenario", "manifest"),
        ("runtime", "runtime-input"),
        ("truth", "truth"),
        ("expectations", "expectations"),
    ):
        for path in directory.glob(f"*.{suffix}.json"):
            validate(json.loads(path.read_text(encoding="utf-8")), SCHEMAS[schema_name])
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    assert len(index["worlds"]) == 6
    for entry in index["worlds"]:
        for item in entry["files"].values():
            world_root = tmp_path / "simulation/gazebo/worlds"
            path = (directory if item["path"].endswith(".json") else world_root) / item["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]


def test_splits_are_world_scoped_and_all_present_at_scale(tmp_path: Path) -> None:
    index = _generate(tmp_path, count=40, seed=9)
    counts = {
        split: sum(entry["split"] == split for entry in index["worlds"])
        for split in ("train", "val", "test")
    }
    assert all(counts.values())
    assert all(entry["split"] in ("train", "val", "test") for entry in index["worlds"])


def test_geometry_truth_poses_and_physics(tmp_path: Path) -> None:
    index = _generate(tmp_path, factor=2.5)
    worlds = tmp_path / "simulation/gazebo/worlds"
    scenarios = tmp_path / "configs/scenarios/generated"
    for entry in index["worlds"]:
        root = ET.fromstring((worlds / entry["world_file"]).read_text(encoding="utf-8"))
        world = root.find("world")
        assert world is not None
        assert float(world.findtext("physics/real_time_factor")) == pytest.approx(2.5)
        models = {model.attrib["name"]: model for model in world.findall("model")}
        truth = json.loads(
            (scenarios / f"{entry['world_id']}.truth.json").read_text(encoding="utf-8")
        )
        for target in truth["targets"]:
            pose = target["pose"]
            xml_pose = [
                float(value) for value in models[target["target_id"]].findtext("pose").split()
            ]
            assert xml_pose[:3] == pytest.approx(
                [pose["x_east_m"], pose["y_north_m"], pose["z_up_m"]]
            )
            assert xml_pose[5] == pytest.approx(pose["yaw_rad"])
            assert pose["x_east_m"] ** 2 + pose["y_north_m"] ** 2 <= 150.0**2
        pad = (0.0, 0.0, 5.0)
        placed = []
        for name, model in models.items():
            if name in {"ground_plane", "multirotor_home_pad"}:
                continue
            values = [float(value) for value in model.findtext("pose").split()]
            geometry = model.find("link/collision/geometry/box/size")
            assert geometry is not None
            sx, sy, _ = (float(value) for value in geometry.text.split())
            radius = (sx * sx + sy * sy) ** 0.5 / 2
            placed.append((values[0], values[1], radius))
            assert (values[0] ** 2 + values[1] ** 2) ** 0.5 > pad[2] + radius
        for index_a, (x_a, y_a, radius_a) in enumerate(placed):
            for x_b, y_b, radius_b in placed[index_a + 1 :]:
                assert ((x_a - x_b) ** 2 + (y_a - y_b) ** 2) ** 0.5 > radius_a + radius_b
