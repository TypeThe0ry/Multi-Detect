"""Generate a Chinese nightly review from one or more benchmark directories."""
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
from scripts.sensor_timeout_counts import count_sensor_timeouts_in_tree

FAILURE_CLASSES = (
    "px4_crashed",
    "clock_stalled_alive",
    "sensor_stream_stalled",
    "px4_alive_other_failure",
    "vehicle_lost_control",
    "climb_failed_suspected_loss_of_control",
    "none",
)
EVENT_FAILURES = (
    "vehicle_lost_control",
    "sensor_stream_stalled",
    "climb_failed_suspected_loss_of_control",
)


def _number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first_value(objects: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for obj in objects:
        for key in keys:
            if key in obj and obj[key] is not None:
                return obj[key]
    return None


def _json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _event_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        found = {value["event"]} if isinstance(value.get("event"), str) else set()
        for child in value.values():
            found.update(_event_names(child))
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found.update(_event_names(child))
        return found
    return set()


def _propeller_counts(steps_path: Path) -> tuple[int | None, int | None]:
    rows = _json_lines(steps_path)
    if not rows:
        return None, None
    frames = sum(
        isinstance(row.get("propeller_joint_state"), dict)
        or (isinstance(row.get("obs_before"), dict)
            and isinstance(row["obs_before"].get("propeller_joint_state"), dict))
        for row in rows
    )
    return frames, len(rows) - frames


def _steps_timing(steps_path: Path) -> tuple[float | None, float | None]:
    values: list[float] = []
    for row in _json_lines(steps_path):
        for side in (row.get("obs_before"), row.get("obs_after")):
            if not isinstance(side, dict):
                continue
            value = side.get("pose_time_boot_ms")
            if isinstance(value, (int, float)):
                values.append(float(value) / 1000.0)
            elif isinstance(side.get("pose_sim_stamp_ns"), (int, float)):
                values.append(float(side["pose_sim_stamp_ns"]) / 1e9)
    return (min(values), max(values)) if values else (None, None)


def _timeout_counts(mission_dir: Path) -> dict[str, int]:
    counts = count_sensor_timeouts_in_tree(mission_dir)
    return {"Accel": counts["accel"], "Gyro": counts["gyro"], "MAG": counts["mag"]}


def _episode_record(episode_path: Path, suite_row: dict[str, Any] | None = None) -> dict[str, Any]:
    episode = _read_json(episode_path)
    summary_path = episode_path.parent / "mission_summary.json"
    summary = _read_json(summary_path) if summary_path.is_file() else {}
    report_path = episode_path.parents[4] / "report.json"
    report: dict[str, Any] = {}
    if report_path.is_file():
        entries = _read_json(report_path).get("episodes", [])
        report = next((e for e in entries if isinstance(e, dict) and
                       Path(str(e.get("episode", ""))).name == episode_path.parent.name), {})
    diag = episode.get("container_failure_diagnostics") or {}
    if not isinstance(diag, dict):
        diag = {}
    failure_state = diag.get("failure_state") or {}
    alive = failure_state.get("px4_process_alive") if isinstance(failure_state, dict) else None
    outcome = episode.get("status")
    finished = outcome in {"complete", "failed"} or episode.get("finished_wall_time") is not None
    event_names = _event_names(episode)
    for log_path in (episode_path.parents[4] / "campaign").glob("*.log"):
        event_names.update(_event_names(_json_lines(log_path)))
    failure_class = next((event for event in EVENT_FAILURES if event in event_names), diag.get("failure_class"))
    if failure_class not in FAILURE_CLASSES or (failure_class == "none" and outcome == "failed"):
        if outcome != "failed":
            failure_class = "none"
        elif alive is True:
            failure_class = "clock_stalled_alive" if "stall" in str(episode.get("failure", "")).lower() else "px4_alive_other_failure"
        else:
            failure_class = "px4_crashed"
    captures = list(episode_path.parent.glob("px4-clock-stall*.json"))
    start, end = _number(episode.get("created_wall_time")), _number(episode.get("finished_wall_time"))
    duration = round(max(0.0, end - start), 3) if start is not None and end is not None else None
    sim_start, sim_end = _steps_timing(episode_path.parent / "steps.jsonl")
    rtf = round((sim_end - sim_start) / duration, 3) if duration and sim_start is not None and sim_end is not None else None
    metrics = [episode, report, summary]
    row = suite_row or {}
    expected = row.get("expected_count", _first_value(metrics, ("expected_count", "truth_count")))
    reported = row.get("reported_count", _first_value(metrics, ("reported_count",)))
    false_findings = row.get("false_findings", _first_value(metrics, ("false_findings",)))
    frames, missing = _propeller_counts(episode_path.parent / "steps.jsonl")
    return {"outcome": outcome if finished else "unfinished", "finished": finished,
            "expected_pass": None, "failure_class": failure_class,
            "stall_capture_count": len(captures), "wall_clock_duration_s": duration,
            "propeller_frames": frames, "propeller_missing_or_stale": missing,
            "frame_rate": _number(_first_value(metrics, ("frame_rate", "frame_rate_hz", "fps"))),
            "count_expected": expected, "count_reported": reported,
            "false_findings": false_findings, "rtf": rtf,
            "timeout_counts": _timeout_counts(episode_path.parents[4]),
            "failure_text": row.get("failure_text") or row.get("failure") or episode.get("failure"),
            "failure_events": sorted(event_names & set(EVENT_FAILURES))}


def _empty_record() -> dict[str, Any]:
    return {"outcome": "unfinished", "finished": False, "expected_pass": None,
            "failure_class": "none", "stall_capture_count": 0, "wall_clock_duration_s": None,
            "propeller_frames": None, "propeller_missing_or_stale": None, "frame_rate": None,
            "count_expected": None, "count_reported": None, "false_findings": None, "rtf": None,
            "timeout_counts": {"Accel": 0, "Gyro": 0, "MAG": 0}, "failure_text": None,
            "failure_events": []}


def load_arm(path: Path) -> dict[str, Any]:
    root = path.resolve()
    missions: dict[str, dict[str, Any]] = {}
    suites: dict[str, dict[str, Any]] = {}
    suite_dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "suite-report.json").is_file())
    if not suite_dirs and (root / "main").is_dir():
        suite_dirs = [root / "main"]
    for suite_dir in suite_dirs:
        suite_name = suite_dir.name
        suite_doc = _read_json(suite_dir / "suite-report.json") if (suite_dir / "suite-report.json").is_file() else {}
        report_rows = {str(row.get("id")): row for row in suite_doc.get("missions", []) if isinstance(row, dict)}
        triage_by_mission = {str(row.get("id")): row for row in mission_triage(suite_doc, suite_dir)} if suite_doc else {}
        mission_dirs = sorted(p for p in suite_dir.iterdir() if p.is_dir())
        suite_keys = []
        for mission_dir in mission_dirs:
            episodes = sorted((mission_dir / "campaign").glob("*/episodes/episode-*/episode.json"))
            record = _episode_record(episodes[-1], report_rows.get(mission_dir.name)) if episodes else _empty_record()
            triage_row = triage_by_mission.get(mission_dir.name)
            if triage_row and record["finished"]:
                record["expected_pass"] = triage_row.get("failure_category") is None
                reasons = [triage_row.get("final_exception"), *triage_row.get("failed_expectations", [])]
                record["failure_text"] = "; ".join(str(reason) for reason in reasons if reason) or record["failure_text"]
                exception_text = str(triage_row.get("final_exception") or "").lower()
                if "sensor stream" in exception_text or "sensor_stream" in exception_text:
                    record["failure_class"] = "sensor_stream_stalled"
                elif "vehicle_lost_control" in exception_text:
                    record["failure_class"] = "vehicle_lost_control"
                elif "climb_failed_suspected_loss_of_control" in exception_text:
                    record["failure_class"] = "climb_failed_suspected_loss_of_control"
            key = mission_dir.name if suite_name == "main" else f"{suite_name}/{mission_dir.name}"
            missions[key] = record
            suite_keys.append(key)
        suite_missions = [missions[key] for key in suite_keys]
        rate = suite_doc.get("summary", {}).get("expectation_pass_rate")
        expected_pass = (rate >= 1.0) if isinstance(rate, (int, float)) else bool(report_rows) and all(
            row.get("expectations_passed") is True for row in report_rows.values())
        suites[suite_name] = {"finished": sum(row["finished"] for row in suite_missions),
                              "missions": len(report_rows) or len(mission_dirs),
                              "expected_pass": expected_pass,
                              "failure_classes": sorted({row["failure_class"] for row in suite_missions
                                                          if row["failure_class"] != "none"})}
    return {"name": path.name, "path": str(root), "finished": sum(r["finished"] for r in missions.values()),
            "total": len(missions), "missions": missions, "suites": suites}


def build_comparison(paths: list[Path]) -> dict[str, Any]:
    arms = [load_arm(path) for path in paths]
    names = sorted({mission for arm in arms for mission in arm["missions"]})
    return {"arms": arms, "missions": [{"mission": name, "arms": {
        arm["name"]: arm["missions"].get(name, _empty_record()) for arm in arms}}
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
    lines += ["Per-mission comparison across arms"]
    return "\n".join(lines) + "\n"


def _failure_text(row: dict[str, Any]) -> str:
    values = list(row.get("failure_events") or [])
    if row.get("failure_text"):
        values.append(str(row["failure_text"]))
    return "; ".join(dict.fromkeys(values)) or "—"


def render_markdown(result: dict[str, Any]) -> str:
    lines = [f"# 夜间回归复核：{result['arms'][-1]['name']}", "",
             "输入：" + "、".join(arm["name"] for arm in result["arms"]), ""]
    for arm in result["arms"]:
        lines += [f"## {arm['name']}", "", f"完成任务：{arm['finished']}/{arm['total']}", "",
                  "| 套件 | 完成 | 任务数 | 预期通过 | 失败类 |", "| --- | ---: | ---: | --- | --- |"]
        for suite, summary in sorted(arm["suites"].items()):
            lines.append(f"| {suite} | {summary['finished']} | {summary['missions']} | {_display(summary['expected_pass'])} | {', '.join(summary['failure_classes']) or '无'} |")
        lines += ["", "| 套件/任务 | 预期通过 | 失败类 | 失败文本/事件 | expected/reported | false findings | 时长(s) | RTF | Accel/Gyro/MAG TIMEOUT | stall capture |", "| --- | --- | --- | --- | --- | ---: | ---: | ---: | --- | ---: |"]
        for name, row in sorted(arm["missions"].items()):
            timeout = row.get("timeout_counts", {})
            lines.append("| " + " | ".join([name, _display(row.get("expected_pass")), _display(row.get("failure_class")),
                _failure_text(row), f"{_display(row.get('count_expected'))}/{_display(row.get('count_reported'))}",
                _display(row.get("false_findings")), _display(row.get("wall_clock_duration_s")), _display(row.get("rtf")),
                "/".join(str(timeout.get(x, 0)) for x in ("Accel", "Gyro", "MAG")), str(row.get("stall_capture_count", 0))]) + " |")
        lines.append("")
    if len(result["arms"]) >= 2:
        previous, current = result["arms"][-2], result["arms"][-1]
        lines += ["## 与上一架次对比", "", "| 任务 | 预期通过变化 | 原因 |", "| --- | --- | --- |"]
        changed = False
        for name in sorted(set(previous["missions"]) | set(current["missions"])):
            old, new = previous["missions"].get(name, _empty_record()), current["missions"].get(name, _empty_record())
            if old.get("expected_pass") != new.get("expected_pass"):
                changed = True
                lines.append(f"| {name} | {_display(old.get('expected_pass'))} → {_display(new.get('expected_pass'))} | {_failure_text(new)} |")
        if not changed:
            lines.append("| — | 无变化 | — |")
    return "\n".join(lines) + "\n"


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    parser.add_argument("--output", type=Path, help="write output to a file")
    parser.add_argument("directories", nargs="+", type=Path)
    args = parser.parse_args(argv)
    result = build_comparison(args.directories)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_markdown(result)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
