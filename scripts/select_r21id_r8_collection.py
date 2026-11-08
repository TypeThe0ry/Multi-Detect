#!/usr/bin/env python3
"""Print a reproducible r8 world/episode selection without starting collection."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.generate_r15_worlds import r21id_r8_templates


ROOT = Path(__file__).resolve().parents[1]
WORLD_PREFIX = "gen_r21id_r8_w"
SEED = 20261122
EPISODES_PER_WORLD = 2


def _world_ids(index_path: Path, *, split: str | None = None) -> set[str]:
    if not index_path.is_file():
        return set()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return {
        row["world_id"]
        for row in index.get("worlds", [])
        if split is None or row.get("split") == split
    }


def build_plan(root: Path = ROOT) -> dict:
    r7_index = root / "configs/scenarios/generated-r21id-r7/index.json"
    if not r7_index.is_file():
        raise FileNotFoundError(f"required r7 index is missing: {r7_index}")
    reused = _world_ids(r7_index)
    held_out: set[str] = set()
    scenario_root = root / "configs/scenarios"
    for index_path in scenario_root.glob("generated-r21id*/index.json"):
        if index_path != r7_index:
            held_out.update(_world_ids(index_path, split="test"))
    held_out.update(_world_ids(scenario_root / "generated-v2/index.json", split="test"))

    worlds = []
    for i, template in enumerate(r21id_r8_templates()):
        world_id = f"{WORLD_PREFIX}{i:04d}"
        if world_id in reused or world_id in held_out:
            raise ValueError(f"r8 world id overlaps an existing/evaluation world: {world_id}")
        class_pair = template["r8_family"]
        worlds.append({
            "world_id": world_id,
            "seed": SEED + i,
            "split": "train",
            "class_pair": class_pair,
            "target": template["target"],
            "confuser": template["confusers"][0],
            "episodes": [f"{world_id}:0", f"{world_id}:1"],
        })

    episode_ids = [episode for world in worlds for episode in world["episodes"]]
    if len(episode_ids) != len(set(episode_ids)):
        raise ValueError("duplicate episode key in r8 selection")
    pair_counts = Counter(world["class_pair"] for world in worlds)
    return {
        "schema_version": 1,
        "round": "r8",
        "generator": "scripts/generate_r15_worlds.py --profile r21id-r8",
        "seed": SEED,
        "episodes_per_world": EPISODES_PER_WORLD,
        "worlds": worlds,
        "episodes": episode_ids,
        "expected_episode_counts_by_class_pair": {
            pair: count * EPISODES_PER_WORLD for pair, count in sorted(pair_counts.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON here; default is stdout")
    args = parser.parse_args(argv)
    rendered = json.dumps(build_plan(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
