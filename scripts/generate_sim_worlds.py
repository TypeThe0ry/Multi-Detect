#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate deterministic procedural Gazebo worlds and scenario channels."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.world_gen import generate_worlds  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--split-ratios", default="0.7,0.15,0.15")
    parser.add_argument("--out-worlds", type=Path, default=Path("simulation/gazebo/worlds"))
    parser.add_argument("--out-scenarios", type=Path, default=Path("configs/scenarios/generated"))
    parser.add_argument("--real-time-factor", type=float, default=1.0)
    args = parser.parse_args()
    ratios = tuple(float(part) for part in args.split_ratios.split(","))
    document = generate_worlds(args.count, args.seed, ratios, args.out_worlds, args.out_scenarios, args.real_time_factor)
    print(f"generated {document['count']} worlds (seed={document['seed']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
