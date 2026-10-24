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
    assert counts == {"fire_hydrant": 8, "backpack": 8, "construction_cone": 8}
