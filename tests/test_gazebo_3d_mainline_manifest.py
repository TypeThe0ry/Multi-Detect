from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.verify_gazebo_3d_mainline_manifest import verify_manifest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs" / "scenarios" / "gazebo-3d-mainline-v1.manifest.json"
SCHEMA = ROOT / "configs" / "schemas" / "gazebo-3d-mainline-manifest.schema.json"


def test_primary_gazebo_manifest_is_an_offline_three_airframe_3d_audit() -> None:
    result = verify_manifest(MANIFEST, repository_root=ROOT)

    assert result["event"] == "gazebo_3d_mainline_manifest_verified"
    assert result["deterministic_asset_audit"] is True
    assert result["scenario_count"] == 3
    assert {item["vehicle_class"] for item in result["scenarios"]} == {
        "multirotor",
        "vtol",
        "fixed-wing",
    }
    assert all(item["wgs84"] is True for item in result["scenarios"])
    assert all(item["world_physics"] == "ode" for item in result["scenarios"])
    assert all(
        item["target_model"]["uri"] == "model://multidetect_search_target_v1"
        for item in result["scenarios"]
    )
    assert {
        item["target_model"]["include_name"] for item in result["scenarios"]
    } == {
        "multidetect_search_target_urban_001",
        "multidetect_search_target_river_001",
        "multidetect_search_target_coastal_001",
    }
    assert all(
        set(item["model_dependencies"]) >= {"model://mono_cam"} for item in result["scenarios"]
    )
    assert result["external_gazebo_runtime"] == "not_claimed"
    assert result["hardware_acceptance"] == "not_claimed"
    assert result["hil_acceptance"] == "not_claimed"
    assert result["outdoor_gnss_acceptance"] == "not_claimed"
    assert result["final_release"] == "not_claimed"


def test_primary_manifest_explicitly_excludes_billboard_fixtures() -> None:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert document["asset_policy"]["billboard_perception_fixtures_excluded"] is True
    assert document["asset_policy"]["dynamic_px4_flight_dynamics"] is True
    serialized = json.dumps(document, ensure_ascii=False).lower()
    assert "billboard_perception_fixtures_excluded" in serialized
    assert "multidetect_coco_car_billboard" not in serialized


def test_primary_manifest_schema_is_local_and_versioned() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert schema["$id"].endswith("gazebo-3d-mainline-manifest.schema.json")
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["scenarios"]["minItems"] == 3
    scenario_schema = schema["$defs"]["scenario"]
    assert "target_model" in scenario_schema["required"]
    assert schema["$defs"]["targetModel"]["properties"]["uri"]["const"] == (
        "model://multidetect_search_target_v1"
    )


def test_neutral_target_model_is_structural_not_a_color_fixture() -> None:
    model = ROOT / "simulation/gazebo/models/multidetect_search_target_v1/model.sdf"
    root = ET.parse(model).getroot()
    target = root.find("model")
    assert target is not None
    assert target.attrib["name"] == "multidetect_search_target_v1"
    assert target.findtext("static") == "true"
    links = target.findall("link")
    assert {link.attrib["name"] for link in links} >= {
        "chassis_link",
        "cabin_link",
        "sensor_mast_link",
        "sensor_head_link",
        "wheel_left_front",
        "wheel_right_front",
    }
    assert all(link.find("collision") is not None for link in links)
    serialized = model.read_text(encoding="utf-8").lower()
    assert "billboard" not in serialized
    assert "red" not in serialized
    assert "green" not in serialized
    assert "ground_truth" not in serialized
