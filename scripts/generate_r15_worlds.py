#!/usr/bin/env python3
"""Generate the r15 Fuel-appearance train/val world set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.simulator.world_gen import generate_fuel_worlds  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config", type=Path, default=ROOT / "configs/scenarios/generated-r15/r15-world-set.json"
    )
    p.add_argument("--out-worlds", type=Path, default=ROOT / "simulation/gazebo/worlds")
    p.add_argument("--out-scenarios", type=Path, default=ROOT / "configs/scenarios/generated-r15")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    if a.dry_run:
        print(
            json.dumps(
                {
                    "count": cfg["count"],
                    "seed": cfg["seed"],
                    "world_prefix": cfg["world_prefix"],
                    "splits": {
                        s: sum(w["split"] == s for w in cfg["worlds"])
                        for s in ("train", "val", "test")
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    targets = tuple(w["target"] for w in cfg["worlds"])
    conf = {}
    forced = {}
    for w in cfg["worlds"]:
        if w.get("confusers"):
            conf[w["target"]] = tuple(w["confusers"])
        forced[w["target"]] = tuple(
            dict.fromkeys((*forced.get(w["target"], ()), *w.get("forced_distractors", ())))
        )
    # Existing generated-r15 outputs are intentionally removed before regeneration;
    # only this prefix is written.
    doc = generate_fuel_worlds(
        count=cfg["count"],
        seed=cfg["seed"],
        split_ratios=tuple(cfg["split_ratios"]),
        out_worlds=a.out_worlds,
        out_scenarios=a.out_scenarios,
        catalog_path=ROOT / cfg["catalog"],
        resolved_root=ROOT / cfg["resolved_root"],
        segmentation_labels=cfg["segmentation_labels"],
        unambiguous_tasks=cfg["unambiguous_tasks"],
        world_prefix=cfg["world_prefix"],
        count_copies=cfg["count_copies"],
        count_slugs=targets,
        spread_m=tuple(cfg["spread_m"]),
        confusers=conf,
        confuser_copies=cfg["confuser_copies"],
        appearance=tuple(w["appearance"] for w in cfg["worlds"]),
        forced_distractors=forced,
    )
    # Preserve the human-authored world table and add a machine-generated summary beside it.
    summary = {
        "schema_version": 1,
        "config": a.config.name,
        "generated_index": "index.json",
        "count": doc["count"],
        "seed": doc["seed"],
        "world_prefix": cfg["world_prefix"],
        "split_counts": {
            s: sum(e["split"] == s for e in doc["worlds"]) for s in ("train", "val", "test")
        },
    }
    (a.out_scenarios / "generation-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
