#!/usr/bin/env python3
"""Generate all-train Fuel worlds used for identity hard-negative episodes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.simulator.world_gen import generate_fuel_worlds  # noqa: E402

DEFAULT_CONFUSERS = ROOT / "configs/sim-assets/confusers-v1.json"
TARGETS = (
    "backpack",
    "electrical_box",
    "cardboard_box",
    "construction_cone",
    "fire_hydrant",
    "mailbox",
    "dumpster",
    "jersey_barrier",
)
DEFAULT_TABLE = {
    "backpack": ["electrical_box", "cardboard_box"],
    "electrical_box": ["backpack", "dumpster"],
    "cardboard_box": ["backpack", "electrical_box"],
    "construction_cone": ["fire_hydrant", "mailbox"],
    "fire_hydrant": ["construction_cone", "mailbox"],
    "mailbox": ["fire_hydrant", "electrical_box"],
    "dumpster": ["jersey_barrier", "cardboard_box"],
    "jersey_barrier": ["dumpster", "cardboard_box"],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20261016)
    parser.add_argument("--confusers", type=Path, default=DEFAULT_CONFUSERS)
    parser.add_argument("--out-worlds", type=Path, default=ROOT / "simulation/gazebo/worlds")
    parser.add_argument(
        "--out-scenarios", type=Path, default=ROOT / "configs/scenarios/generated-confusers"
    )
    parser.add_argument("--confuser-copies", type=int, choices=(1, 2), default=1)
    parser.add_argument("--world-prefix", default="gen_cfs_w")
    parser.add_argument("--targets", nargs="+", choices=TARGETS, default=list(TARGETS))
    # Counting worlds hold three copies of the target: more identity positives.
    parser.add_argument("--all-count", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.confusers.is_file():
        config = json.loads(args.confusers.read_text(encoding="utf-8"))
        table = config.get("confusers", config)
    else:
        table = DEFAULT_TABLE
    if not isinstance(table, dict):
        raise ValueError("confusers config must contain an object")
    if args.dry_run:
        print(
            json.dumps(
                {
                    "count": args.count,
                    "seed": args.seed,
                    "world_prefix": args.world_prefix,
                    "targets": list(args.targets),
                    "confuser_copies": args.confuser_copies,
                    "out_scenarios": str(args.out_scenarios),
                },
                sort_keys=True,
            )
        )
        return 0
    copy_counts = tuple(
        3 if args.all_count or i >= args.count // 2 else 1 for i in range(args.count)
    )
    document = generate_fuel_worlds(
        count=args.count,
        seed=args.seed,
        split_ratios=(1.0, 0.0, 0.0),
        out_worlds=args.out_worlds,
        out_scenarios=args.out_scenarios,
        catalog_path=ROOT / "configs/sim-assets/fuel-catalog.json",
        resolved_root=ROOT / "artifacts/cache/gz-fuel/resolved",
        segmentation_labels=True,
        unambiguous_tasks=True,
        world_prefix=args.world_prefix,
        count_copies=copy_counts,
        count_slugs=tuple(args.targets),
        spread_m=(10.0, 50.0),
        confusers=table,
        confuser_copies=args.confuser_copies,
    )
    print(f"generated {document['count']} worlds (seed={document['seed']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
