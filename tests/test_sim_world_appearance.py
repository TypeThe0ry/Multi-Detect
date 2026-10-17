"""Appearance profile, mirror-set and prefix acceptance checks."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from multidetect.simulator.world_gen import APPEARANCE_PRESETS, _appearance_values, generate_worlds
from scripts.collect_sim_teacher_episodes import auto_takeoff_altitude_m
from scripts.generate_appearance_test_set import generate_appearance_test_set


def test_all_appearance_presets_emit_valid_scene_and_light(tmp_path: Path) -> None:
    for preset in APPEARANCE_PRESETS:
        root = tmp_path / preset
        index = generate_worlds(
            1, 77, out_worlds=root / "worlds", out_scenarios=root / "scenarios", appearance=preset
        )
        sdf = ET.parse(root / "worlds" / index["worlds"][0]["world_file"]).getroot()
        world = sdf.find("world")
        assert world is not None
        assert world.find("scene/ambient") is not None
        assert world.find("scene/background") is not None
        assert world.find("light/direction") is not None
        assert world.find("light/diffuse") is not None
        assert world.find("light/specular") is not None
        expected = _appearance_values(preset, __import__("random").Random(77))
        assert world.findtext("scene/shadows") == str(expected["shadows"]).lower()
        assert float(world.findtext("light/intensity")) == expected["intensity"]
        direction = [float(v) for v in world.findtext("light/direction").split()]
        assert abs(sum(v * v for v in direction) ** 0.5 - 1.0) < 1e-3  # 4-decimal SDF output
        assert direction[2] < 0  # sun above the ground


def test_appearance_mirror_has_test_split_and_exact_truth(tmp_path: Path) -> None:
    source = tmp_path / "source"
    generate_appearance_test_set(
        source_worlds=Path("simulation/gazebo/worlds"),
        source_scenarios=Path("configs/scenarios/generated-v2"),
        out_worlds=source / "worlds",
        out_scenarios=source / "scenarios",
    )
    document = json.loads((source / "scenarios/index.json").read_text(encoding="utf-8"))
    assert len(document["worlds"]) == 6
    assert {item["split"] for item in document["worlds"]} == {"test"}
    for item in document["worlds"]:
        original = json.loads(
            (
                Path("configs/scenarios/generated-v2") / f"{item['source_world_id']}.truth.json"
            ).read_text(encoding="utf-8")
        )
        mirrored = json.loads(
            (source / "scenarios" / f"{item['world_id']}.truth.json").read_text(encoding="utf-8")
        )
        assert [target["pose"] for target in original["targets"]] == [
            target["pose"] for target in mirrored["targets"]
        ]
        ET.parse(source / "worlds" / item["world_file"])


def test_appearance_prefix_is_accepted_for_takeoff_altitude() -> None:
    path = Path("simulation/gazebo/worlds/gen_v2a_w0000.sdf")
    altitude, source = auto_takeoff_altitude_m(path)
    assert altitude >= 5.0
    assert source.startswith("auto_")
