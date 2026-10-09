from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from multidetect.autonomy.serialization import canonical_sha256
from multidetect.runtime import RuntimeMode
from multidetect.simulation import load_runtime_scenario, load_scenario_manifest
from multidetect.simulator.stack import build_stack_plan

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ID = "gazebo_red_green_search_001"
SCENARIO_ROOT = ROOT / "configs/scenarios/validation"
MANIFEST = SCENARIO_ROOT / f"{SCENARIO_ID}.scenario.json"
RUNTIME = SCENARIO_ROOT / f"{SCENARIO_ID}.runtime.json"
TRUTH = SCENARIO_ROOT / f"{SCENARIO_ID}.truth.json"
EXPECTATIONS = SCENARIO_ROOT / f"{SCENARIO_ID}.expectations.json"
WORLD = ROOT / f"simulation/gazebo/worlds/{SCENARIO_ID}.sdf"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key).lower() for key in value} | {
            child for item in value.values() for child in _keys(item)
        }
    if isinstance(value, list):
        return {child for item in value for child in _keys(item)}
    return set()


def test_search_scene_channels_are_schema_valid_and_hash_bound() -> None:
    documents = {
        "manifest": _load(MANIFEST),
        "runtime_input": _load(RUNTIME),
        "truth": _load(TRUTH),
        "expectations": _load(EXPECTATIONS),
    }
    schema_names = {
        "manifest": "scenario-manifest.schema.json",
        "runtime_input": "scenario-runtime-input.schema.json",
        "truth": "scenario-truth.schema.json",
        "expectations": "scenario-expectations.schema.json",
    }
    for name, document in documents.items():
        schema = _load(ROOT / "configs/schemas" / schema_names[name])
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(document)
    for channel_name, channel in documents["manifest"]["channels"].items():
        assert canonical_sha256(documents[channel_name]) == channel["sha256"]
    manifest = load_scenario_manifest(MANIFEST)
    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    assert manifest.scenario_id == SCENARIO_ID
    assert runtime.scenario_id == SCENARIO_ID
    assert any(sensor.kind == "depth_camera" for sensor in runtime.sensors)


def test_search_scene_runtime_is_truth_free_and_world_has_staged_colors() -> None:
    runtime = _load(RUNTIME)
    forbidden = {
        "target",
        "targets",
        "target_id",
        "position",
        "bbox",
        "annotation",
        "ground_truth",
        "expected_label",
    }
    assert _keys(runtime).isdisjoint(forbidden)
    truth = _load(TRUTH)
    assert {target["behavior"] for target in truth["targets"]} == {
        "stationary_software_only_billboard_distractor",
        "stationary_software_only_billboard_target",
    }
    root = ET.parse(WORLD).getroot()
    world = root.find("world")
    assert world is not None
    green = world.find("model[@name='gazebo_green_car_search_distractor_billboard']")
    red = world.find("model[@name='gazebo_red_car_search_target_billboard']")
    assert green is not None and red is not None
    green_pose = [float(value) for value in (green.findtext("pose") or "").split()]
    red_pose = [float(value) for value in (red.findtext("pose") or "").split()]
    assert green_pose[:2] == [20.0, 0.0]
    assert red_pose[:2] == [5.0, 30.0]
    assert "green_car_search_distractor_visual" in WORLD.read_text(encoding="utf-8")
    assert "red_car_search_target_visual" in WORLD.read_text(encoding="utf-8")


def test_search_scene_stack_resolves_depth_capable_x500() -> None:
    plan = build_stack_plan(
        MANIFEST,
        repository_root=ROOT,
        tool_capabilities={"px4": False, "gazebo": False},
    )
    assert plan.scenario_id == SCENARIO_ID
    assert plan.world_path == WORLD
    assert plan.model_name == "x500_mono_cam"
    assert dict(plan.environment_mapping)["PX4_SIM_MODEL"] == "gz_x500_mono_cam"
