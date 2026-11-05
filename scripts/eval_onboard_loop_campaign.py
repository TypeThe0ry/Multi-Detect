"""Evaluate the truth-free onboard loop on the held-out realistic-mesh test worlds.

Runs ``collect_sim_teacher_episodes.py --teacher onboard`` once per (world,
episode), one container at a time, then scores every episode against truth with
``score_onboard_loop.py``. The privileged expert teacher's acquisition outcome
on the same worlds is reported as a reference, not as a competitor.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from multidetect.provenance import build_provenance  # noqa: E402
from scripts.model_round_guard import ensure_free_disk  # noqa: E402
from scripts.score_onboard_loop import score_episode  # noqa: E402


def _episode_metadata(episode: Path) -> dict[str, Any]:
    try:
        value = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_count_task(row: dict[str, Any]) -> bool:
    """Return whether generic count metrics apply to an episode.

    Count-site truth contains ``count_member`` targets for survey and monitor
    missions too. Their expected checks use coverage or alerts, so those truth
    members must not make the count summary look like a failed count task.
    Older episodes without mission labels retain the historical
    ``expected_count`` fallback.
    """
    skills = row.get("mission_plan_skills")
    if isinstance(skills, list):
        return "count" in skills
    return "expected_count" in row


def _count_summary(complete: list[dict[str, Any]]) -> dict[str, float | None]:
    count_rows = [row for row in complete if _is_count_task(row) and "expected_count" in row]
    return {
        "count_exact_rate": (
            sum(row.get("count_exact", False) for row in count_rows) / len(count_rows)
            if count_rows
            else None
        ),
        "mean_abs_count_error": (
            sum(abs((row.get("reported_count") or 0) - row["expected_count"]) for row in count_rows)
            / len(count_rows)
            if count_rows
            else None
        ),
    }


def episode_is_complete(episode: Path) -> bool:
    """Return whether an episode is safe to count as collected.

    A status marker alone is insufficient: interrupted writes can leave an empty
    or truncated steps file behind.  ``step_count`` was added to the recorder
    metadata, so when it is present it must agree with the parsed rows.
    """
    metadata = _episode_metadata(episode)
    if metadata.get("status") != "complete":
        return False
    steps = episode / "steps.jsonl"
    try:
        lines = steps.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    if not lines:
        return False
    parsed = []
    try:
        for line in lines:
            if not line.strip():
                return False
            parsed.append(json.loads(line))
    except (ValueError, TypeError):
        return False
    if not all(isinstance(row, dict) for row in parsed):
        return False
    expected = metadata.get("step_count")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        return False
    return expected is None or expected == len(parsed)


def _episode_index(episode: Path, metadata: dict[str, Any] | None = None) -> int | None:
    metadata = metadata or _episode_metadata(episode)
    for key in ("episode_index", "index"):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    match = re.match(r"episode-(\d+)(?:-|$)", episode.name)
    return int(match.group(1)) if match else None


def _episode_seed(episode: Path, metadata: dict[str, Any] | None = None) -> int | None:
    metadata = metadata or _episode_metadata(episode)
    for key in ("seed", "episode_seed", "collector_seed"):
        value = metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    # Older collectors did not persist seed.  Campaigns used seed + index with
    # a default seed of zero, so the episode number is the best reconstruction.
    return _episode_index(episode, metadata)


def retry_failed_episodes(out: Path) -> list[Path]:
    """Archive failed/incomplete recordings, leaving completed episodes untouched.

    The collector allocates UUID episode names. Keeping previous attempts outside
    ``episodes`` preserves diagnostics without counting attempts as extra episodes.
    """
    archived = []
    for episode in sorted((out / "episodes").glob("*")):
        if not episode.is_dir():
            continue
        if episode_is_complete(episode):
            continue
        destination = out / "retry-history" / f"{episode.name}-{uuid.uuid4().hex[:8]}"
        root = out.resolve()
        if not episode.resolve().is_relative_to(root) or not destination.resolve().is_relative_to(
            root
        ):
            raise RuntimeError(f"Retry archive path escapes campaign output: {episode}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(episode), str(destination))
        archived.append(destination)
    return archived


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
        "--airframe",
        default=None,
        help="collector airframe profile; omitted preserves the legacy x500 path",
    )
    parser.add_argument(
        "--takeoff-method",
        choices=("runway", "hand", "catapult", "vtol"),
        default=None,
        help="airframe launch method (passed through to collect_sim_teacher_episodes.py)",
    )
    parser.add_argument("--max-episode-s", type=int, default=2400)
    parser.add_argument("--retry-failed-episodes", action="store_true")
    parser.add_argument(
        "--keep-px4-ulog",
        action="store_true",
        help="copy PX4 .ulg flight logs into each collector job before cleanup",
    )
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--container-prefix", default="md-sim-teacher")
    parser.add_argument("--worker-index", type=int, default=0)
    parser.add_argument("--operator-port", type=int, default=14580)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on collector errors or incomplete scored rows (legacy default is permissive)",
    )
    parser.add_argument("--min-free-disk-gb", type=float, default=0)
    parser.add_argument("--disk-check-every", type=int, default=1)
    parser.add_argument(
        "--worlds", nargs="*", default=None, help="only these world ids (default: the whole split)"
    )
    args = parser.parse_args(argv)
    if args.episodes < 1 or args.disk_check_every < 1 or args.min_free_disk_gb < 0:
        parser.error("episodes and disk-check-every must be positive; disk GB must be nonnegative")
    mission_config = (
        json.loads(args.onboard_config.read_text(encoding="utf-8")) if args.onboard_config else None
    )
    index = json.loads(args.index.read_text(encoding="utf-8"))
    worlds = [
        w
        for w in index["worlds"]
        if w["split"] == args.split and (not args.worlds or w["world_id"] in args.worlds)
    ]
    scenarios = args.index.parent
    rows: list[dict[str, Any]] = []
    strict_failure = False
    for world in worlds:
        world_id = world["world_id"]
        out = args.out / world_id
        retry_targets: list[tuple[int | None, int | None]] = []
        if args.retry_failed_episodes and out.is_dir():
            failed = []
            for episode in sorted((out / "episodes").glob("*")):
                if episode.is_dir() and not episode_is_complete(episode):
                    metadata = _episode_metadata(episode)
                    failed.append(
                        (_episode_index(episode, metadata), _episode_seed(episode, metadata))
                    )
            retry_failed_episodes(out)
            retry_targets = failed
        complete_dirs = (
            [
                episode
                for episode in sorted((out / "episodes").glob("*"))
                if episode.is_dir() and episode_is_complete(episode)
            ]
            if out.is_dir()
            else []
        )
        used_seeds = {
            seed for episode in complete_dirs if (seed := _episode_seed(episode)) is not None
        }
        # Recollect each failed episode with its original seed.  A missing seed
        # from an old campaign is reconstructed from its episode index; if that
        # collides with a completed recording, advance to a fresh seed.
        for _failed_index, failed_seed in sorted(
            retry_targets,
            key=lambda item: (item[0] is None, item[0] if item[0] is not None else -1),
        ):
            seed = failed_seed
            if seed is None or seed in used_seeds:
                seed = 0 if seed is None else seed
                while seed in used_seeds:
                    seed += 1
            ensure_free_disk(args.out, args.min_free_disk_gb)
            command = [
                sys.executable,
                str(ROOT / "scripts/collect_sim_teacher_episodes.py"),
                "--scenario",
                str(scenarios / f"{world_id}.scenario.json"),
                "--teacher",
                "onboard",
                "--episodes",
                "1",
                "--seed",
                str(seed),
                "--steps-per-episode",
                str(args.steps),
                "--out",
                str(out),
                "--rgb-size",
                "1280x960",
                "--max-episode-s",
                str(args.max_episode_s),
                "--control-port", str(args.control_port),
                "--telemetry-port", str(args.telemetry_port),
                "--container-prefix", args.container_prefix,
                "--worker-index", str(args.worker_index),
                "--operator-port", str(args.operator_port),
            ]
            if args.segmentation:
                command.append("--segmentation")
            if mission_config and mission_config.get("camera_noise_overlay"):
                command.append("--camera-noise-overlay")
            if mission_config and mission_config.get("wind_vehicle_overlay"):
                command.append("--wind-vehicle-overlay")
            if args.onboard_config:
                command += ["--onboard-config", str(args.onboard_config)]
            if args.airframe:
                command += ["--airframe", str(args.airframe)]
            if args.takeoff_method:
                command += ["--takeoff-method", str(args.takeoff_method)]
            if args.keep_px4_ulog:
                command.append("--keep-px4-ulog")
            if args.min_free_disk_gb > 0:
                # The collector otherwise applies its own 15 GB guard and aborts
                # episodes that the campaign's lower --min-free-disk-gb allows.
                command += ["--min-free-gb", str(args.min_free_disk_gb)]
            log = args.out / f"{world_id}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                code = subprocess.run(
                    command, stdout=handle, stderr=subprocess.STDOUT, check=False
                ).returncode
            print(json.dumps({"world": world_id, "collector_exit": code}), flush=True)
            if code:
                strict_failure = True
                if args.strict:
                    raise RuntimeError(f"Collector failed for {world_id}: exit {code}; see {log}")
                break
            complete_after = [
                episode
                for episode in sorted((out / "episodes").glob("*"))
                if episode.is_dir() and episode_is_complete(episode)
            ]
            if len(complete_after) <= len(complete_dirs):
                print(json.dumps({"world": world_id, "status": "not filled: low disk"}), flush=True)
                strict_failure = True
                if args.strict:
                    raise RuntimeError(
                        f"Collector did not produce a complete episode for {world_id}: "
                        "not filled: low disk"
                    )
                break
            else:
                used_seeds.add(seed)
        complete_dirs = (
            [
                episode
                for episode in sorted((out / "episodes").glob("*"))
                if episode.is_dir() and episode_is_complete(episode)
            ]
            if out.is_dir()
            else []
        )
        missing = max(0, args.episodes - len(complete_dirs))
        seed_cursor = 0
        while missing:
            ensure_free_disk(args.out, args.min_free_disk_gb)
            batch = min(missing, args.disk_check_every)
            while seed_cursor in used_seeds:
                seed_cursor += 1
            command = [
                sys.executable,
                str(ROOT / "scripts/collect_sim_teacher_episodes.py"),
                "--scenario",
                str(scenarios / f"{world_id}.scenario.json"),
                "--teacher",
                "onboard",
                "--episodes",
                str(batch),
                "--seed",
                str(seed_cursor),
                "--steps-per-episode",
                str(args.steps),
                "--out",
                str(out),
                "--rgb-size",
                "1280x960",
                "--max-episode-s",
                str(args.max_episode_s),
                "--control-port", str(args.control_port),
                "--telemetry-port", str(args.telemetry_port),
                "--container-prefix", args.container_prefix,
                "--worker-index", str(args.worker_index),
                "--operator-port", str(args.operator_port),
            ]
            if args.segmentation:
                command.append("--segmentation")
            if mission_config and mission_config.get("camera_noise_overlay"):
                command.append("--camera-noise-overlay")
            if mission_config and mission_config.get("wind_vehicle_overlay"):
                command.append("--wind-vehicle-overlay")
            if args.onboard_config:
                command += ["--onboard-config", str(args.onboard_config)]
            if args.airframe:
                command += ["--airframe", str(args.airframe)]
            if args.takeoff_method:
                command += ["--takeoff-method", str(args.takeoff_method)]
            if args.keep_px4_ulog:
                command.append("--keep-px4-ulog")
            if args.min_free_disk_gb > 0:
                # The collector otherwise applies its own 15 GB guard and aborts
                # episodes that the campaign's lower --min-free-disk-gb allows.
                command += ["--min-free-gb", str(args.min_free_disk_gb)]
            log = args.out / f"{world_id}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                code = subprocess.run(
                    command, stdout=handle, stderr=subprocess.STDOUT, check=False
                ).returncode
            print(json.dumps({"world": world_id, "collector_exit": code}), flush=True)
            if code:
                strict_failure = True
                if args.strict:
                    raise RuntimeError(f"Collector failed for {world_id}: exit {code}; see {log}")
                break
            complete_after = [
                episode
                for episode in sorted((out / "episodes").glob("*"))
                if episode.is_dir() and episode_is_complete(episode)
            ]
            filled = len(complete_after) - len(complete_dirs)
            complete_dirs = complete_after
            if filled <= 0:
                print(json.dumps({"world": world_id, "status": "not filled: low disk"}), flush=True)
                strict_failure = True
                if args.strict:
                    raise RuntimeError(
                        f"Collector did not fill episodes for {world_id}: not filled: low disk"
                    )
                break
            missing = max(0, args.episodes - len(complete_dirs))
            used_seeds.update(range(seed_cursor, seed_cursor + batch))
            seed_cursor += batch
        truth = json.loads((scenarios / f"{world_id}.truth.json").read_text(encoding="utf-8"))
        for episode in sorted(out.glob("episodes/*/")):
            metadata = _episode_metadata(episode)
            if not metadata:
                continue
            # score_episode handles aborted_low_disk before opening steps.jsonl;
            # initial aborts may have no steps file and must still be reported.
            if metadata.get("status") != "aborted_low_disk" and not episode_is_complete(episode):
                continue
            scored = score_episode(
                episode,
                truth,
                radius_m=args.radius_m,
                mission_config=mission_config,
            )
            scored.update(
                world=world_id,
                task_text=world.get("task_text"),
                target=world.get("target_combo"),
            )
            rows.append(scored)
            print(
                json.dumps(
                    {
                        k: scored.get(k)
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
    count_summary = _count_summary(complete)
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
        **count_summary,
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
    mission_rows = [r for r in rows if "mission_completed" in r]
    if mission_rows:
        alert_precision = [
            r["mission_alert_precision"]
            for r in mission_rows
            if r.get("mission_alert_precision") is not None
        ]
        alert_recall = [
            r["mission_alert_recall"]
            for r in mission_rows
            if r.get("mission_alert_recall") is not None
        ]
        coverage = [
            value
            for r in mission_rows
            for value in (r.get("mission_survey_coverage") or {}).values()
            if value is not None
        ]
        summary.update(
            mission_mean_alert_precision=(
                sum(alert_precision) / len(alert_precision) if alert_precision else None
            ),
            mission_mean_alert_recall=(
                sum(alert_recall) / len(alert_recall) if alert_recall else None
            ),
            mission_completion_rate=sum(bool(r["mission_completed"]) for r in mission_rows)
            / len(mission_rows),
            mission_mean_coverage=sum(coverage) / len(coverage) if coverage else None,
        )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"summary": summary, "episodes": rows,
                    "provenance": build_provenance(root=ROOT, config_paths=[args.onboard_config] if args.onboard_config else [], command=sys.argv)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.strict:
        return int(
            strict_failure or len(rows) < len(worlds) * args.episodes or len(complete) != len(rows)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
