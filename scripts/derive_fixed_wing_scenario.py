#!/usr/bin/env python3
"""Re-target generated worlds at a runway fixed-wing (same world, truth and task)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.world_gen import derive_fixed_wing_scenario  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="+", type=Path, help="generated *.scenario.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--worlds-dir", type=Path, default=ROOT / "simulation/gazebo/worlds")
    parser.add_argument("--runway-heading-deg", type=float, default=None)
    parser.add_argument("--corridor-half-width-m", type=float, default=15.0)
    args = parser.parse_args(argv)
    for scenario in args.scenarios:
        entry = derive_fixed_wing_scenario(
            scenario,
            args.out,
            worlds_dir=args.worlds_dir,
            runway_heading_deg=args.runway_heading_deg,
            corridor_half_width_m=args.corridor_half_width_m,
        )
        print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
