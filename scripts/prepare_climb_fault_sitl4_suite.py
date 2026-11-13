#!/usr/bin/env python3
"""Prepare the single-mission suite used by the climb-fault SITL v4 job."""

from __future__ import annotations

import copy
import json
from pathlib import Path


def build_suite(source_path: Path, output_path: Path) -> None:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    mission = next(item for item in source["missions"] if item["id"] == "operator-abort")
    mission = copy.deepcopy(mission)
    mission["sim_ground_events"] = [{"t_s": 0, "event": "abort"}]
    mission["sim_climb_timeout_injection"] = True
    mission["sim_pre_abort_descent_altitude_m"] = 1.5
    mission["max_episode_s"] = max(2400, int(mission["max_episode_s"]))
    # The base abort expectation is incompatible with the injected safety
    # escalation and would report the intended land_now as a harness failure.
    mission["expect"] = copy.deepcopy(mission["expect"])
    mission["expect"]["supervisor"] = {
        **mission["expect"]["supervisor"],
        "final_decision": "land_now",
        "reason": "climb_failed_suspected_loss_of_control",
    }
    source["missions"] = [mission]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_suite(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
