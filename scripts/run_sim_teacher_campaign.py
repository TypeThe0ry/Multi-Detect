#!/usr/bin/env python3
"""Run resumable SIM teacher collection campaigns one episode at a time.

The runner intentionally owns orchestration only. The collector and offline
auditor remain the source of truth for episode recording and validation.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "scripts" / "collect_sim_teacher_episodes.py"
AUDITOR = ROOT / "scripts" / "audit_sim_teacher_dataset.py"
DEFAULT_CONTROL_PORT = 14540
DEFAULT_TELEMETRY_PORT = 14541
CONTAINER_PREFIX = "md-sim-teacher-"
INFRASTRUCTURE_MARKERS = (
    "container",
    "docker",
    "ekf",
    "heartbeat",
    "timed out",
    "timeout",
    "mavlink",
    "connection refused",
    "daemon",
    "gazebo",
    "px4",
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CleanupRunner = Callable[[], Any]
Clock = Callable[[], float]


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _world_id(scenario: Path) -> str:
    name = scenario.name
    if name.endswith(".scenario.json"):
        return name[: -len(".scenario.json")]
    return scenario.stem


def resolve_scenarios(values: Sequence[str | Path]) -> list[Path]:
    """Expand globs and explicit paths while preserving input order."""
    found: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        text = os.fspath(value)
        # Permit a comma separated list as a convenience for shell wrappers.
        candidates = text.split(",") if "," in text and not Path(text).exists() else [text]
        for candidate in candidates:
            path = Path(candidate)
            matches = sorted(
                (Path(item) for item in glob.glob(candidate, recursive=True))
                if any(ch in candidate for ch in "*?[")
                else [path]
            )
            for match in matches:
                resolved = match.resolve()
                if not resolved.is_file() or not resolved.name.endswith(".scenario.json"):
                    continue
                if resolved not in seen:
                    found.append(resolved)
                    seen.add(resolved)
    if not found:
        raise ValueError("--scenarios did not match any .scenario.json files")
    return found


def load_splits(index_path: Path | None) -> dict[str, str]:
    if index_path is None:
        return {}
    document = json.loads(index_path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for world in document.get("worlds", []):
        if not isinstance(world, dict):
            continue
        world_id = world.get("world_id")
        split = world.get("split")
        if isinstance(world_id, str) and isinstance(split, str):
            result[world_id] = split
        files = world.get("files")
        if isinstance(files, dict):
            scenario = files.get("scenario", {})
            scenario_path = scenario.get("path") if isinstance(scenario, dict) else None
            if isinstance(scenario_path, str) and isinstance(split, str):
                result[Path(scenario_path).name.removesuffix(".scenario.json")] = split
    return result


def cleanup_leftover_containers(
    run: CommandRunner = subprocess.run,
    *,
    executable: str = "docker",
) -> list[str]:
    """Remove stale md-sim-teacher containers; Docker absence is harmless."""
    try:
        listed = run(
            [executable, "ps", "-aq", "--filter", f"name=^{CONTAINER_PREFIX}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    if ids:
        try:
            run([executable, "rm", "-f", *ids], capture_output=True, text=True, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    return ids


def is_infrastructure_failure(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in INFRASTRUCTURE_MARKERS)


def _audit_passed(path: Path) -> tuple[bool, dict[str, Any] | None]:
    report_path = path / "audit.json"
    if not report_path.is_file():
        return False, None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if not isinstance(report, dict):
        return False, None
    # An auditor report with a files map and step count is a successful report.
    return isinstance(report.get("files"), dict) and isinstance(
        report.get("step_count"), int
    ), report


def _audit_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report[key]
        for key in ("step_count", "success_rate", "sim_skew_ms", "frame_pose_skew_s")
        if key in report
    }


def _run_audit(
    dataset: Path,
    run: CommandRunner,
    executable: str,
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str]:
    command = [executable, str(AUDITOR), "--dataset", str(dataset), "--json"]
    try:
        result = run(command, capture_output=True, text=True, check=False, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if result.returncode:
        return None, result.stderr or result.stdout or "audit failed"
    try:
        report = json.loads(result.stdout)
    except ValueError:
        return None, "auditor returned invalid JSON"
    if not isinstance(report, dict) or not isinstance(report.get("step_count"), int):
        return None, "auditor returned an incomplete report"
    _json_dump(dataset / "audit.json", report)
    return report, ""


def _clear_incomplete_dataset(dataset: Path, campaign_root: Path) -> None:
    """Clear only this campaign episode's incomplete collector output."""
    root = campaign_root.resolve()
    target = dataset.resolve()
    if target == root or root not in target.parents:
        raise ValueError(f"episode output escapes campaign: {target}")
    for name in ("episodes", "jobs"):
        child = (target / name).resolve()
        if child.parent != target:
            raise ValueError(f"unsafe episode output: {child}")
        if child.is_dir():
            shutil.rmtree(child)
    (target / "audit.json").unlink(missing_ok=True)


def _labels_stats(dataset: Path) -> tuple[int, int]:
    total = acquired = 0
    for steps in dataset.glob("episodes/*/steps.jsonl"):
        try:
            rows = steps.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in rows:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            labels = row.get("labels")
            if not isinstance(labels, dict) or not isinstance(labels.get("target_in_fov"), bool):
                continue
            total += 1
            if labels.get("target_in_fov") is True:
                acquired += 1
    return total, acquired


def _status_row(
    *,
    world: str,
    seed: int,
    split: str,
    teacher: str,
    dataset: Path,
    status: str,
    duration_s: float,
    attempts: int,
    audit: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "world": world,
        "seed": seed,
        "split": split,
        "teacher": teacher,
        "dataset": str(dataset),
        "status": status,
        "attempts": attempts,
        "duration_s": round(duration_s, 3),
    }
    if audit is not None:
        row["audit"] = audit
    if error:
        row["error"] = error
    return row


def run_campaign(
    scenarios: Sequence[str | Path],
    *,
    teacher: str,
    episodes_per_scenario: int,
    steps_per_episode: int,
    seed_base: int,
    out: Path,
    split_from_index: Path | None = None,
    max_episode_s: float = 300.0,
    max_retries: int = 1,
    command_runner: CommandRunner = subprocess.run,
    cleanup: CleanupRunner | None = None,
    clock: Clock = time.monotonic,
    python_executable: str | None = None,
) -> dict[str, Any]:
    if teacher not in {"expert_search", "explore", "mixed"}:
        raise ValueError("teacher must be expert_search, explore, or mixed")
    if episodes_per_scenario < 1 or steps_per_episode < 1:
        raise ValueError("episode and step counts must be positive")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    scenario_paths = resolve_scenarios(scenarios)
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    splits = load_splits(split_from_index)
    campaign_path = out / "campaign.json"
    existing: dict[str, Any] = {}
    if campaign_path.is_file():
        try:
            loaded = json.loads(campaign_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    episodes: list[dict[str, Any]] = [
        x for x in existing.get("episodes", []) if isinstance(x, dict)
    ]
    wanted = {
        f"{_world_id(scenario)}:{seed_base + scenario_number * episodes_per_scenario + index}"
        for scenario_number, scenario in enumerate(scenario_paths)
        for index in range(episodes_per_scenario)
    }
    by_key = {
        f"{x.get('world')}:{x.get('seed')}": x
        for x in episodes
        if f"{x.get('world')}:{x.get('seed')}" in wanted
    }
    run = command_runner
    cleanup_fn = cleanup or (lambda: cleanup_leftover_containers(run))
    executable = python_executable or sys.executable

    for scenario_number, scenario in enumerate(scenario_paths):
        world = _world_id(scenario)
        split = splits.get(world, "unspecified")
        for episode_index in range(episodes_per_scenario):
            seed = seed_base + scenario_number * episodes_per_scenario + episode_index
            key = f"{world}:{seed}"
            dataset = out / world / str(seed)
            previous = by_key.get(key)
            passed, report = _audit_passed(dataset)
            # A cached audit.json can be stale after files are changed.  The
            # auditor must validate the dataset again before a resume skip.
            if passed or (dataset / "episodes").is_dir():
                report, _ = _run_audit(dataset, run, executable, max_episode_s)
                passed = report is not None
            if passed:
                row = _status_row(
                    world=world,
                    seed=seed,
                    split=split,
                    teacher=(
                        "expert_search"
                        if teacher == "mixed" and episode_index % 2 == 0
                        else "explore"
                        if teacher == "mixed"
                        else teacher
                    ),
                    dataset=dataset,
                    status="skipped",
                    duration_s=float((previous or {}).get("duration_s", 0)),
                    attempts=int((previous or {}).get("attempts", 0)),
                    audit=_audit_summary(report),
                )
                by_key[key] = row
                continue
            selected_teacher = (
                "expert_search"
                if teacher == "mixed" and episode_index % 2 == 0
                else "explore"
                if teacher == "mixed"
                else teacher
            )
            started = clock()
            last_error = ""
            attempts = 0
            audit_report: dict[str, Any] | None = None
            status = "failed"
            while attempts <= max_retries:
                attempts += 1
                cleanup_fn()
                _clear_incomplete_dataset(dataset, out)
                command = [
                    executable,
                    str(COLLECTOR),
                    "--scenario",
                    str(scenario),
                    "--teacher",
                    selected_teacher,
                    "--episodes",
                    "1",
                    "--steps-per-episode",
                    str(steps_per_episode),
                    "--seed",
                    str(seed),
                    "--out",
                    str(dataset),
                    "--max-episode-s",
                    str(max_episode_s),
                    "--timeout-s",
                    str(max_episode_s),
                ]
                try:
                    result = run(
                        command,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=max_episode_s + 60.0,
                    )
                    output = f"{result.stdout}\n{result.stderr}"
                    if result.returncode == 0:
                        audit_report, last_error = _run_audit(
                            dataset, run, executable, max_episode_s
                        )
                        if audit_report is not None:
                            status = "success"
                            break
                        retryable = is_infrastructure_failure(last_error)
                    else:
                        last_error = output.strip()[-4000:]
                        retryable = is_infrastructure_failure(last_error)
                    if not retryable:
                        break
                except subprocess.TimeoutExpired as exc:
                    last_error = f"timeout: {exc}"
                except (OSError, subprocess.SubprocessError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                if attempts <= max_retries:
                    cleanup_fn()
            duration = clock() - started
            row = _status_row(
                world=world,
                seed=seed,
                split=split,
                teacher=selected_teacher,
                dataset=dataset,
                status=status,
                duration_s=duration,
                attempts=attempts,
                audit=_audit_summary(audit_report) if audit_report is not None else None,
                error=None if status == "success" else last_error,
            )
            by_key[key] = row
            episodes = list(by_key.values())
            _write_campaign(
                campaign_path,
                episodes,
                teacher,
                episodes_per_scenario,
                steps_per_episode,
                seed_base,
                out,
                max_episode_s,
                max_retries,
            )
    episodes = list(by_key.values())
    result = _write_campaign(
        campaign_path,
        episodes,
        teacher,
        episodes_per_scenario,
        steps_per_episode,
        seed_base,
        out,
        max_episode_s,
        max_retries,
    )
    cleanup_fn()
    return result


def _write_campaign(
    path: Path,
    episodes: list[dict[str, Any]],
    teacher: str,
    episodes_per_scenario: int,
    steps: int,
    seed_base: int,
    out: Path,
    max_episode_s: float,
    max_retries: int,
) -> dict[str, Any]:
    counts = Counter(str(e.get("status")) for e in episodes)
    by_split: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_world: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_transitions = 0
    expert_labels = acquired = 0
    for row in episodes:
        split = str(row.get("split", "unspecified"))
        world = str(row.get("world", "unknown"))
        status = str(row.get("status"))
        by_split[split][status] += 1
        by_world[world][status] += 1
        audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
        total_transitions += int(audit.get("step_count", 0) or 0)
        labels, hits = (
            _labels_stats(Path(str(row.get("dataset", ""))))
            if status in {"success", "skipped"} and row.get("teacher") == "expert_search"
            else (0, 0)
        )
        expert_labels += labels
        acquired += hits
    total = len(episodes)
    summary = {
        "counts": dict(counts),
        "counts_by_split": {k: dict(v) for k, v in by_split.items()},
        "counts_by_world": {k: dict(v) for k, v in by_world.items()},
        "success_rate": (counts.get("success", 0) + counts.get("skipped", 0)) / total
        if total
        else 0.0,
        "acquisition_rate": acquired / expert_labels if expert_labels else 0.0,
        "expert_label_count": expert_labels,
        "acquired_label_count": acquired,
        "total_transitions": total_transitions,
    }
    document = {
        "schema_version": 1,
        "teacher": teacher,
        "episodes_per_scenario": episodes_per_scenario,
        "steps_per_episode": steps,
        "seed_base": seed_base,
        "out": str(out),
        "max_episode_s": max_episode_s,
        "max_retries": max_retries,
        "control_port": DEFAULT_CONTROL_PORT,
        "telemetry_port": DEFAULT_TELEMETRY_PORT,
        "episodes": sorted(episodes, key=lambda x: (str(x.get("world")), int(x.get("seed", 0)))),
        "summary": summary,
    }
    _json_dump(path, document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", nargs="+", required=True)
    parser.add_argument("--teacher", choices=("expert_search", "explore", "mixed"), required=True)
    parser.add_argument("--episodes-per-scenario", type=int, required=True)
    parser.add_argument("--steps-per-episode", type=int, required=True)
    parser.add_argument("--seed-base", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split-from-index", type=Path)
    parser.add_argument("--max-episode-s", type=float, default=300.0)
    parser.add_argument("--max-retries", type=int, default=1)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    result = run_campaign(
        args.scenarios,
        teacher=args.teacher,
        episodes_per_scenario=args.episodes_per_scenario,
        steps_per_episode=args.steps_per_episode,
        seed_base=args.seed_base,
        out=args.out,
        split_from_index=args.split_from_index,
        max_episode_s=args.max_episode_s,
        max_retries=args.max_retries,
    )
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 1 if result["summary"]["counts"].get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
