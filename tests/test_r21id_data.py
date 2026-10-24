import json
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_r21id_worlds_are_split_and_holdout_clean():
    index = json.loads((ROOT / "configs/scenarios/generated-r21id/index.json").read_text())
    held = json.loads((ROOT / "configs/scenarios/generated-v2/index.json").read_text())["held_out_target_combos"]
    held_keys = {(x["slug"], x["category"], x.get("color")) for x in held}
    assert index["profile"] == "r21-identity"
    assert index["count"] == 24
    assert {s: sum(x["split"] == s for x in index["worlds"]) for s in ("train", "val", "test")} == {"train": 18, "val": 3, "test": 3}
    counts = {name: 0 for name in ("fire_hydrant", "backpack", "construction_cone")}
    for entry in index["worlds"]:
        runtime = json.loads(
            (ROOT / "configs/scenarios/generated-r21id" / f"{entry['world_id']}.runtime_input.json").read_text()
        )
        spawn = runtime["vehicle"]["spawn_pose"]
        assert spawn["z_up_m"] < 1.0
        assert spawn["pitch_rad"] == 0.0
        intent = json.loads(
            (ROOT / "configs/scenarios/generated-r21id" / f"{entry['world_id']}.teacher_intent.json").read_text()
        )["observation"]
        assert intent["camera_altitude_m"] > 1.0
        assert "camera_pitch_rad" in intent
        assert set(intent["observation_waypoint"]) >= {"x_east_m", "y_north_m", "z_up_m"}
        truth = json.loads((ROOT / "configs/scenarios/generated-r21id" / f"{entry['world_id']}.truth.json").read_text())
        manifest_path = ROOT / "configs/scenarios/generated-r21id" / f"{entry['world_id']}.scenario.json"
        manifest = json.loads(manifest_path.read_text())
        world_path = ROOT / "simulation/gazebo/worlds" / entry["world_file"]
        assert world_path.is_file()
        sdf = ET.parse(world_path).getroot()
        target = next(x for x in truth["targets"] if x.get("true_target"))
        assert abs(target["pose"]["x_east_m"]) <= 120
        assert abs(target["pose"]["y_north_m"]) <= 120
        assert sdf.find(f".//model[@name='{target['target_id']}']") is not None
        assert (target["asset_slug"], target["class"], target.get("color")) not in held_keys
        counts[target["asset_slug"]] += 1
        assert entry["hard_case_profile"] == "identity_multiview"
        assert entry["true_target_id"] == entry["view_geometry"]["target_id"]
        assert len(entry["view_geometry"]["identity_sequence"]) == 4
        assert len(intent.get("views", [])) == 4
        for view in intent["views"]:
            waypoint = view["observation_waypoint"]
            distance = ((target["pose"]["x_east_m"] - waypoint["x_east_m"]) ** 2 +
                        (target["pose"]["y_north_m"] - waypoint["y_north_m"]) ** 2) ** 0.5
            assert abs(distance - entry["view_geometry"]["ground_distance_m"]) <= 0.01
    assert counts == {"fire_hydrant": 8, "backpack": 8, "construction_cone": 8}


def test_r21id_takeoff_clearance_excludes_occluders_and_distractors():
    """Every non-target footprint stays outside the 15 m home clearance ring."""
    index = json.loads((ROOT / "configs/scenarios/generated-r21id/index.json").read_text())
    for entry in index["worlds"]:
        truth = json.loads(
            (ROOT / "configs/scenarios/generated-r21id" / f"{entry['world_id']}.truth.json").read_text()
        )
        target_id = next(row["target_id"] for row in truth["targets"] if row.get("true_target"))
        root = ET.parse(ROOT / "simulation/gazebo/worlds" / entry["world_file"]).getroot()
        for model in root.findall(".//world/model"):
            name = model.attrib.get("name", "")
            if name in {"ground_plane", "multirotor_home_pad", target_id}:
                continue
            pose = model.findtext("pose")
            if not pose:
                continue
            x, y = (float(value) for value in pose.split()[:2])
            collision_size = model.find("link/collision/geometry/box/size")
            if collision_size is None:
                continue
            sx, sy = (float(value) for value in collision_size.text.split()[:2])
            footprint_radius = (sx * sx + sy * sy) ** 0.5 / 2.0
            assert (x * x + y * y) ** 0.5 - footprint_radius >= 15.0
