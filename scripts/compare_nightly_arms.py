"""Compare per-episode evidence across nightly output directories."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.nightly_trend import _read_json
from scripts.run_nightly_regression import mission_triage


FAILURE_CLASSES = ("px4_crashed", "clock_stalled_alive", "px4_alive_other_failure", "none")


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first_value(objects: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for obj in objects:
        for key in keys:
            if key in obj and obj[key] is not None:
                return obj[key]
    return None


def _propeller_counts(steps_path: Path) -> tuple[int | None, int | None]:
    if not steps_path.is_file():
        return None, None
    lines = [line for line in steps_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    frames = sum('"propeller_joint_state": {' in line for line in lines)
    return frames, len(lines) - frames


def _episode_record(episode_path: Path) -> dict[str, Any]:
    episode = _read_json(episode_path)
    summary_path = episode_path.parent / "mission_summary.json"
    summary = _read_json(summary_path) if summary_path.is_file() else {}
    report_path = episode_path.parents[4] / "report.json"
    report: dict[str, Any] = {}
    if report_path.is_file():
        report_doc = _read_json(report_path)
        entries = report_doc.get("episodes", [])
        report = next((e for e in entries if isinstance(e, dict) and
                       Path(str(e.get("episode", ""))).name == episode_path.parent.name), {})
    diag = episode.get("container_failure_diagnostics") or {}
    if not isinstance(diag, dict):
        diag = {}
    failure_state = diag.get("failure_state") or {}
    alive = failure_state.get("px4_process_alive") if isinstance(failure_state, dict) else None
    outcome = episode.get("status")
    finished = outcome in {"complete", "failed"} or episode.get("finished_wall_time") is not None
    # report.success is the single-target score, not the suite expectation; a
    # mission has an expected result only once suite-report.json exists.
    expected = None
    failure_class = diag.get("failure_class")
    if failure_class not in FAILURE_CLASSES:
        if outcome != "failed":
            failure_class = "none"
        elif alive is True:
            failure_class = "clock_stalled_alive" if "stall" in str(episode.get("failure", "")).lower() else "px4_alive_other_failure"
        else:
            failure_class = "px4_crashed"
    captures = list(episode_path.parent.glob("px4-clock-stall*.json"))
    duration = None
    start, end = _number(episode.get("created_wall_time")), _number(episode.get("finished_wall_time"))
    if start is not None and end is not None:
        duration = round(max(0.0, end - start), 3)
    metrics = [episode, report, summary]
    step_frames, step_missing = _propeller_counts(episode_path.parent / "steps.jsonl")
    return {
        "outcome": outcome if finished else "unfinished",
        "finished": finished,
        "expected_pass": expected,
        "failure_class": failure_class,
        "stall_capture_count": len(captures),
        "wall_clock_duration_s": duration,
        "propeller_frames": step_frames,
        "propeller_missing_or_stale": step_missing,
        "frame_rate": _number(_first_value(metrics, ("frame_rate", "frame_rate_hz", "fps"))),
    }


def load_arm(path: Path) -> dict[str, Any]:
    root = path.resolve()
    main = root / "main"
    missions: dict[str, dict[str, Any]] = {}
    total = 0
    if main.is_dir():
        mission_dirs = sorted(p for p in main.iterdir() if p.is_dir())
        total = sum(1 for p in mission_dirs if (p / "onboard.json").is_file()) or len(mission_dirs)
        triage_by_mission: dict[str, dict[str, Any]] = {}
        suite_report = main / "suite-report.json"
        if suite_report.is_file():
            triage = mission_triage(_read_json(suite_report), main)
            triage_by_mission = {str(row.get("id")): row for row in triage}
        for mission_dir in mission_dirs:
            episodes = sorted((mission_dir / "campaign").glob("*/episodes/episode-*/episode.json"))
            if not episodes:
                missions[mission_dir.name] = {"outcome": "unfinished", "finished": False,
                    "expected_pass": None, "failure_class": "none", "stall_capture_count": 0,
                    "wall_clock_duration_s": None, "propeller_frames": None,
                    "propeller_missing_or_stale": None, "frame_rate": None}
                continue
            records = [_episode_record(p) for p in episodes]
            record = records[-1]
            triage_row = triage_by_mission.get(mission_dir.name)
            if triage_row is not None and record["finished"]:
                record["expected_pass"] = triage_row.get("failure_category") is None
            missions[mission_dir.name] = record
    finished_count = sum(record["finished"] for record in missions.values())
    return {"name": path.name, "path": str(root), "finished": finished_count,
            "total": max(total, len(missions)), "missions": missions}


def build_comparison(paths: list[Path]) -> dict[str, Any]:
    arms = [load_arm(path) for path in paths]
    names = sorted({mission for arm in arms for mission in arm["missions"]})
    return {"arms": arms, "missions": [{"mission": name, "arms": {
        arm["name"]: arm["missions"].get(name, {"outcome": "unfinished", "finished": False,
            "expected_pass": None, "failure_class": "none", "stall_capture_count": 0,
            "wall_clock_duration_s": None, "propeller_frames": None,
            "propeller_missing_or_stale": None, "frame_rate": None}) for arm in arms}}
        for name in names]}


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_text(result: dict[str, Any]) -> str:
    lines = []
    for arm in result["arms"]:
        lines += [f"Arm: {arm['name']} ({arm['finished']}/{arm['total']} finished)",
                  "| Mission | Expected pass | Outcome | Failure class | Stall captures | Duration (s) | Propeller frames | Missing/stale | Frame rate |",
                  "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
        for mission, row in sorted(arm["missions"].items()):
            lines.append("| " + " | ".join([mission, *(_display(row[k]) for k in (
                "expected_pass", "outcome", "failure_class", "stall_capture_count",
                "wall_clock_duration_s", "propeller_frames", "propeller_missing_or_stale", "frame_rate"))]) + " |")
        lines.append("")
    lines += ["Per-mission comparison across arms", "| Mission | Arm | Finished | Expected pass | Outcome | Failure class | Stall captures | Duration (s) | Propeller frames | Missing/stale | Frame rate |",
              "| --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for entry in result["missions"]:
        for arm, row in entry["arms"].items():
            lines.append("| " + " | ".join([entry["mission"], arm, _display(row["finished"]), *(
                _display(row[k]) for k in ("expected_pass", "outcome", "failure_class", "stall_capture_count",
                    "wall_clock_duration_s", "propeller_frames", "propeller_missing_or_stale", "frame_rate"))]) + " |")
    return "\n".join(lines) + "\n"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text tables")
    parser.add_argument("directories", nargs="+", type=Path)
    args = parser.parse_args(argv)
    if len(args.directories) < 2:
        parser.error("provide at least two nightly directories")
    result = build_comparison(args.directories)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_text(result), end="\n" if args.json else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
