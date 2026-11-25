#!/usr/bin/env python3
"""Replay an X1 episode or perform CPU-only observation fault overlay."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from multidetect.autonomy.x1_contingency_replay import load_faults, replay_states
from multidetect.autonomy.x1_contingency_state import compact_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mavlink", type=Path, help="recorded mavlink.jsonl or mavlink.jsonl.gz")
    parser.add_argument("--sidecar", type=Path, help="optional trusted sparse observation/event JSONL")
    parser.add_argument("--faults", type=Path, help="optional x1.contingency.faults.v1 JSON")
    parser.add_argument("--mission-id", default="x1-replay")
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument("--every-ms", type=int, default=1000)
    parser.add_argument("--until-ms", type=int)
    parser.add_argument("--vehicle-system", type=int, default=1)
    parser.add_argument("--vehicle-component", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        faults = load_faults(args.faults) if args.faults else ()
        for state in replay_states(args.mavlink, mission_id=args.mission_id, revision=args.revision,
                                   every_ms=args.every_ms, sidecar=args.sidecar, faults=faults,
                                   until_ms=args.until_ms, vehicle_system=args.vehicle_system,
                                   vehicle_component=args.vehicle_component):
            print(compact_json(state))
    except (ValueError, OSError) as exc:
        print(f"x1 replay failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
