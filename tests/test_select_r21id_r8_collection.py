import json
from pathlib import Path

from scripts.select_r21id_r8_collection import build_plan


ROOT = Path(__file__).resolve().parents[1]


def test_r8_selection_is_disjoint_and_fills_the_three_gaps():
    plan = build_plan(ROOT)
    worlds = plan["worlds"]
    r7_ids = {
        row["world_id"]
        for row in json.loads(
            (ROOT / "configs/scenarios/generated-r21id-r7/index.json").read_text()
        )["worlds"]
    }
    held_out_ids = {
        row["world_id"]
        for index_path in (ROOT / "configs/scenarios").glob("generated-r21id*/index.json")
        if index_path != ROOT / "configs/scenarios/generated-r21id-r7/index.json"
        for row in json.loads(index_path.read_text())["worlds"]
        if row.get("split") == "test"
    }
    assert len(worlds) == 3
    assert {row["class_pair"] for row in worlds} == {
        "person->jersey_barrier", "car->mailbox", "suv->postbox"
    }
    assert not {row["world_id"] for row in worlds} & r7_ids
    assert not {row["world_id"] for row in worlds} & held_out_ids
    assert all(row["split"] == "train" and len(row["episodes"]) == 2 for row in worlds)
    assert plan["expected_episode_counts_by_class_pair"] == {
        "car->mailbox": 2,
        "person->jersey_barrier": 2,
        "suv->postbox": 2,
    }
    assert plan["episodes"] == [episode for world in worlds for episode in world["episodes"]]
