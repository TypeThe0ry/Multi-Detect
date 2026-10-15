#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate deterministic procedural Gazebo worlds and scenario channels."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multidetect.simulator.world_gen import generate_fuel_worlds, generate_worlds  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--split-ratios", default="0.7,0.15,0.15")
    parser.add_argument("--out-worlds", type=Path, default=Path("simulation/gazebo/worlds"))
    parser.add_argument("--out-scenarios", type=Path, default=Path("configs/scenarios/generated"))
    parser.add_argument("--real-time-factor", type=float, default=1.0)
    parser.add_argument("--appearance", choices=("noon_clear", "morning_low_sun", "evening_low_sun", "overcast_flat", "dusk_dim", "haze"), default=None)
    parser.add_argument("--asset-mode", choices=("box", "fuel_v1"), default="box")
    parser.add_argument(
        "--segmentation-labels",
        action="store_true",
        help="emit Gazebo Harmonic Label plugins and segmentation_labels truth map (fuel_v1 only)",
    )
    parser.add_argument(
        "--unambiguous-tasks",
        action="store_true",
        help="fuel_v1: leave out distractors that the task text would also describe",
    )
    args = parser.parse_args()
    ratios = tuple(float(part) for part in args.split_ratios.split(","))
    fn = generate_fuel_worlds if args.asset_mode == "fuel_v1" else generate_worlds
    if args.asset_mode == "fuel_v1":
        document = fn(
            args.count,
            args.seed,
            ratios,
            args.out_worlds,
            args.out_scenarios,
            args.real_time_factor,
            appearance=args.appearance,
            segmentation_labels=args.segmentation_labels,
            unambiguous_tasks=args.unambiguous_tasks,
        )
    else:
        document = fn(
            args.count,
            args.seed,
            ratios,
            args.out_worlds,
            args.out_scenarios,
            args.real_time_factor,
            appearance=args.appearance,
        )
    print(f"generated {document['count']} worlds (seed={document['seed']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
