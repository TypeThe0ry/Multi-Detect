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
    p.add_argument("--world-prefix", help="override the generated world id prefix")
    p.add_argument("--seed", type=int, help="override the configured generation seed")
    p.add_argument("--count", type=int, help="override the configured world count")
    p.add_argument("--catalog", type=Path, help="override asset catalog path")
    p.add_argument("--resolved-root", type=Path, help="override resolved asset root")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--profile", choices=("default", "r20"), default="default",
                   help="optional hard-case profile; default preserves the r15 config")
    a = p.parse_args(argv)
    cfg = json.loads(a.config.read_text(encoding="utf-8"))
    if a.profile == "r20":
        # Keep r20 isolated and deterministic: paired confusers are explicit in
        # the world table, while the generator still owns placement/truth output.
        cfg.update({"world_prefix": "gen_r20_w", "seed": 20261024,
                    "count": 3, "split_ratios": [1.0, 0.0, 0.0],
                    "spread_m": [80.0, 120.0], "count_copies": 1,
                    "confuser_copies": 1, "segmentation_labels": True,
                    "unambiguous_tasks": True})
        cfg["worlds"] = [
            {"split": "train", "target": "pickup", "confusers": ["suv"],
             "forced_distractors": ["jersey_barrier"], "appearance": "noon_clear"},
            {"split": "train", "target": "construction_cone", "confusers": ["jersey_barrier"],
             "forced_distractors": ["pickup"], "appearance": "overcast_flat"},
            {"split": "train", "target": "backpack", "confusers": ["cardboard_box"],
             "forced_distractors": ["fire_hydrant"], "appearance": "haze",
             "view_profile": "small_oblique_10_13px"},
        ]
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
    if a.world_prefix:
        cfg["world_prefix"] = a.world_prefix
    if a.seed is not None:
        cfg["seed"] = a.seed
    if a.count is not None:
        cfg["count"] = a.count
        cfg["worlds"] = [
            dict(
                cfg["worlds"][i % len(cfg["worlds"])],
                world_id=f"{cfg['world_prefix']}{i:04d}",
            )
            for i in range(a.count)
        ]
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
        catalog_path=(a.catalog or (ROOT / cfg["catalog"])),
        resolved_root=(a.resolved_root or (ROOT / cfg["resolved_root"])),
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
    if a.profile == "r20":
        doc["profile"] = "r20"
        for entry, world in zip(doc["worlds"], cfg["worlds"]):
            entry["hard_case_profile"] = world.get("view_profile", "paired_identity")
            entry["paired_confusers"] = list(world.get("confusers", ()))
            entry["view_geometry"] = {
                "fixed_wing_oblique": True,
                "slope_deg": [20, 40],
                "slant_range_m": [80, 200],
                "short_side_px": [20, 40],
            } if entry["hard_case_profile"] == "paired_identity" else {
                "fixed_wing_oblique": False, "slant_range_m": [25, 35],
                "short_side_px": [10, 13], "altitude_m": [18, 20],
            }
        (a.out_scenarios / "index.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
