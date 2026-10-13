"""Evaluate the truth-free onboard loop on the held-out realistic-mesh test worlds.

Runs ``collect_sim_teacher_episodes.py --teacher onboard`` once per (world,
episode), one container at a time, then scores every episode against truth with
``score_onboard_loop.py``. The privileged expert teacher's acquisition outcome
on the same worlds is reported as a reference, not as a competitor.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.score_onboard_loop import score_episode  # noqa: E402


def _expert_reference(campaign: Path, world: str) -> dict[str, Any] | None:
    """Fraction of expert episodes on ``world`` whose teacher reached the hold state."""
    if not campaign.is_dir():
        return None
    episodes = sorted(campaign.glob(f"{world}/*/episodes/*/steps.jsonl"))
    if not episodes:
        return None
    reached = 0
    for steps in episodes:
        phases = {
            (json.loads(line).get("labels") or {}).get("phase")
            for line in steps.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        reached += bool(phases & {"hold", "done"})
    return {"episodes": len(episodes), "acquired": reached}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index", type=Path, default=ROOT / "configs/scenarios/generated-v2/index.json"
    )
    parser.add_argument("--split", default="test")
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--steps", type=int, default=80)
    parser.add_argument(
        "--out", type=Path, default=ROOT / "artifacts/datasets/onboard-loop/eval-v2-test"
    )
    parser.add_argument(
        "--report", type=Path, default=ROOT / "artifacts/benchmarks/onboard-loop-v2-test.json"
    )
    parser.add_argument(
        "--expert-campaign",
        type=Path,
        default=ROOT / "artifacts/datasets/sim-teacher/campaign-20261012-v2-r3-seg",
    )
    parser.add_argument("--segmentation", action="store_true", default=True)
    parser.add_argument("--radius-m", type=float, default=10.0)
    parser.add_argument("--onboard-config", type=Path, default=None)
    parser.add_argument(
        "--worlds", nargs="*", default=None, help="only these world ids (default: the whole split)"
    )
    args = parser.parse_args(argv)
    index = json.loads(args.index.read_text(encoding="utf-8"))
    worlds = [
        w
        for w in index["worlds"]
        if w["split"] == args.split and (not args.worlds or w["world_id"] in args.worlds)
    ]
    scenarios = args.index.parent
    rows: list[dict[str, Any]] = []
    for world in worlds:
        world_id = world["world_id"]
        out = args.out / world_id
        done = sorted(out.glob("episodes/*/episode.json")) if out.is_dir() else []
        missing = max(0, args.episodes - len(done))
        if missing:
            command = [
                sys.executable,
                str(ROOT / "scripts/collect_sim_teacher_episodes.py"),
                "--scenario",
                str(scenarios / f"{world_id}.scenario.json"),
                "--teacher",
                "onboard",
                "--episodes",
                str(missing),
                "--steps-per-episode",
                str(args.steps),
                "--out",
                str(out),
                "--rgb-size",
                "1280x960",
                "--max-episode-s",
                "2400",
            ]
            if args.segmentation:
                command.append("--segmentation")
            if args.onboard_config:
                command += ["--onboard-config", str(args.onboard_config)]
            log = args.out / f"{world_id}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                code = subprocess.run(
                    command, stdout=handle, stderr=subprocess.STDOUT, check=False
                ).returncode
            print(json.dumps({"world": world_id, "collector_exit": code}), flush=True)
        truth = json.loads((scenarios / f"{world_id}.truth.json").read_text(encoding="utf-8"))
        for episode in sorted(out.glob("episodes/*/")):
            if not (episode / "steps.jsonl").is_file():
                continue
            scored = score_episode(episode, truth, radius_m=args.radius_m)
            scored.update(
                world=world_id, task_text=world["task_text"], target=world["target_combo"]
            )
            rows.append(scored)
            print(
                json.dumps(
                    {
                        k: scored[k]
                        for k in (
                            "world",
                            "success",
                            "false_confirm",
                            "task_equivalent",
                            "confirmed",
                        )
                    }
                ),
                flush=True,
            )
    complete = [r for r in rows if r["status"] == "complete"]
    summary = {
        "worlds": [w["world_id"] for w in worlds],
        "episodes": len(rows),
        "complete": len(complete),
        "success_rate": sum(r["success"] for r in complete) / len(complete) if complete else None,
        "false_confirm_rate": sum(r["false_confirm"] for r in complete) / len(complete)
        if complete
        else None,
        "task_equivalent_confirm_rate": sum(r.get("task_equivalent", False) for r in complete)
        / len(complete)
        if complete
        else None,
        "success_rate_task_semantics": sum(
            r["success"] or r.get("task_equivalent", False) for r in complete
        )
        / len(complete)
        if complete
        else None,
        "no_confirm_rate": sum(not r["confirmed"] for r in complete) / len(complete)
        if complete
        else None,
        "count_exact_rate": (
            sum(r.get("count_exact", False) for r in complete) / len(complete)
            if complete and any("expected_count" in r for r in complete)
            else None
        ),
        "mean_abs_count_error": (
            sum(abs((r.get("reported_count") or 0) - r["expected_count"]) for r in complete)
            / len(complete)
            if complete and all("expected_count" in r for r in complete)
            else None
        ),
        "median_position_error_m": sorted(
            r["position_error_m"] for r in complete if r.get("position_error_m") is not None
        )[len([r for r in complete if r.get("position_error_m") is not None]) // 2]
        if any(r.get("position_error_m") is not None for r in complete)
        else None,
        # Successful episodes only: a false confirm's error is the distance to the
        # wrong object, not a localisation error.
        "median_position_error_success_m": (
            sorted(r["position_error_m"] for r in complete if r["success"])[
                sum(r["success"] for r in complete) // 2
            ]
            if any(r["success"] for r in complete)
            else None
        ),
        "expert_reference": {
            w["world_id"]: _expert_reference(args.expert_campaign, w["world_id"]) for w in worlds
        },
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"summary": summary, "episodes": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
