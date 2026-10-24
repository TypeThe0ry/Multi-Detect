"""Run text-mission SIM regressions; --dry-run needs no simulator or model.

Relative suite/index/model paths are repository-relative. Each mission gets an
onboard config, campaign directory and report. Plan matching is ordered and exact;
suite rates include missing episodes as failures. Reusing --out resumes the existing
campaign, so use a fresh directory when changing tasks or model configuration.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
METADATA = {
    "id",
    "index",
    "world",
    "split",
    "expect",
    "steps",
    "max_episode_s",
    "episodes",
    # Execution selectors belong to the suite scheduler, not onboard config.
    "airframe",
    "takeoff_method",
    "not_found",
}
METRICS = {
    "alert_precision": "mission_alert_precision",
    "alert_recall": "mission_alert_recall",
    "expected_count": "expected_count",
    "reported_count": "reported_count",
    "matched_count": "matched_count",
    "false_findings": "false_findings",
    "count_exact": "count_exact",
    "inspect_match_fraction": "mission_inspect_match_fraction",
    "survey_coverage": "mission_survey_coverage",
    "steps": "steps",
    "confirmed": "confirmed",
    "safety_pass": "safety_pass",
    "min_relative_altitude_m": "min_relative_altitude_m",
    "ground_contact_detected": "ground_contact_detected",
    "return_home_distance_m": "return_home_distance_m",
    "moving_distance_mean_m": "moving_distance_mean_m",
    "moving_distance_max_m": "moving_distance_max_m",
    "moving_track_continuity": "moving_track_continuity",
    "moving_time_coverage": "moving_time_coverage",
    "follow_target_in_frame_fraction": "follow_target_in_frame_fraction",
    "follow_lost_count": "follow_lost_count",
    "follow_reacquire_max_s": "follow_reacquire_max_s",
    "follow_false_confirm_count": "follow_false_confirm_count",
    "follow_mean_horizontal_distance_m": "follow_mean_horizontal_distance_m",
    "follow_max_horizontal_distance_m": "follow_max_horizontal_distance_m",
    "follow_frame_target_fraction": "follow_frame_target_fraction",
}


def repo_path(path: str | Path) -> Path:
    path = Path(path)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def merge_config(defaults: dict[str, Any], mission: dict[str, Any]) -> dict[str, Any]:
    """Copy shared onboard options; suite scheduling/expectations never reach the model."""
    config = copy.deepcopy(defaults)
    config.setdefault("mission", "from_text")
    for key, value in mission.items():
        if key in METADATA:
            continue
        if key in {"refs", "ref_labels"}:
            config[key] = {**config.get(key, {}), **copy.deepcopy(value)}
        else:
            config[key] = copy.deepcopy(value)
    return config


def build_command(
    mission: dict[str, Any],
    config_path: Path,
    mission_out: Path,
    report_path: Path,
    episodes: int = 1,
    *,
    retry_failed_episodes: bool = False,
    min_free_disk_gb: float = 0,
    disk_check_every: int = 1,
    strict: bool = False,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/eval_onboard_loop_campaign.py"),
        "--index",
        str(repo_path(mission["index"])),
        "--split",
        str(mission.get("split", "test")),
        "--worlds",
        mission["world"],
        "--episodes",
        str(episodes),
        "--steps",
        str(mission["steps"]),
        "--max-episode-s",
        str(mission["max_episode_s"]),
        "--onboard-config",
        str(config_path),
        "--out",
        str(mission_out),
        "--report",
        str(report_path),
    ]
    if retry_failed_episodes:
        command.append("--retry-failed-episodes")
    if strict:
        command.append("--strict")
    command += [
        "--min-free-disk-gb",
        str(min_free_disk_gb),
        "--disk-check-every",
        str(disk_check_every),
    ]
    # Keep the historical x500 command byte-for-byte unchanged when a mission
    # does not opt into an airframe.  Fixed-wing/VTOL missions pass these
    # selectors through the campaign to the collector.
    if mission.get("airframe"):
        command += ["--airframe", str(mission["airframe"])]
    if mission.get("takeoff_method"):
        command += ["--takeoff-method", str(mission["takeoff_method"])]
    return command


def _expectations(row: dict[str, Any], expect: dict[str, Any]) -> dict[str, bool]:
    checks = {"skills": row["plan_match"]}
    for key, threshold in expect.items():
        if key in {"skills", "alternatives"}:
            continue
        if key == "supervisor":
            evidence = row.get("supervisor", {})
            for field, wanted in threshold.items():
                actual = evidence.get(field)
                if field.startswith("logged_"):
                    checks[f"supervisor.{field}"] = isinstance(actual, list) and all(
                        item in actual for item in wanted
                    )
                else:
                    checks[f"supervisor.{field}"] = actual is not None and actual == wanted
            continue
        metric = key.removeprefix("min_").removeprefix("max_")
        value = row.get(metric)
        if isinstance(value, dict):
            values = list(value.values())
            value = min(values) if values and all(v is not None for v in values) else None
        if value is None:
            checks[key] = False
        elif key.startswith("min_"):
            checks[key] = value >= threshold
        elif key.startswith("max_"):
            checks[key] = value <= threshold
        else:
            checks[key] = value == threshold
    return checks


def build_table(
    missions: list[dict[str, Any]], reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Keep per-episode values intact rather than averaging counts or skill lists."""
    rows = []
    for mission in missions:
        report = reports.get(mission["id"], {})
        episodes = [r for r in report.get("episodes", []) if r.get("world") == mission["world"]]
        expected_episodes = mission.get("episodes", 1)
        for number in range(max(expected_episodes, len(episodes))):
            episode = episodes[number] if number < len(episodes) else {}
            planned = episode.get("mission_plan_skills")
            expected = mission["expect"]["skills"]
            row = {
                "id": mission["id"],
                "world": mission["world"],
                "episode": number + 1,
                "status": episode.get("status", "missing"),
                "planned_skills": planned,
                "expected_skills": expected,
                # Equivalent plans the suite accepts are listed under expect.alternatives.
                "plan_match": planned == expected
                or planned in mission["expect"].get("alternatives", []),
                "mission_completed": bool(episode.get("mission_completed", False)),
                **{key: episode.get(source) for key, source in METRICS.items()},
                "step_outcomes": episode.get("mission_step_outcomes"),
            }
            # Keep legacy suite rows byte-for-byte compatible in shape.
            if "supervisor" in mission["expect"]:
                row["supervisor"] = episode.get("mission_supervisor") or {}
            row["expectation_checks"] = _expectations(row, mission["expect"])
            row["expectations_passed"] = all(row["expectation_checks"].values())
            rows.append(row)
    total = len(rows)
    return {
        "summary": {
            "missions": len(missions),
            "episodes": total,
            "plan_accuracy": sum(r["plan_match"] for r in rows) / total if total else None,
            "completion_rate": sum(r["mission_completed"] for r in rows) / total if total else None,
            "expectation_pass_rate": (
                sum(r["expectations_passed"] for r in rows) / total if total else None
            ),
        },
        "missions": rows,
    }


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


def render_markdown(table: dict[str, Any]) -> str:
    summary = table["summary"]
    lines = [
        "# SIM 文本任务回归套件",
        "",
        f"任务数：{summary['missions']}；episode 数：{summary['episodes']}；"
        f"计划准确率：{_cell(summary['plan_accuracy'])}；"
        f"任务完成率：{_cell(summary['completion_rate'])}；"
        f"预期通过率：{_cell(summary['expectation_pass_rate'])}。",
        "",
        "技能按顺序精确匹配；缺失 episode 计为失败；— 表示未报告或不适用。",
        "覆盖率保留按步骤的值；min_survey_coverage 检查所有已报告步骤的最小值。",
        "",
    ]
    columns = {
        "任务": "id",
        "世界": "world",
        "Episode": "episode",
        "状态": "status",
        "计划技能": "planned_skills",
        "预期技能": "expected_skills",
        "匹配": "plan_match",
        "任务完成": "mission_completed",
        "告警精确率": "alert_precision",
        "告警召回率": "alert_recall",
        "预期数量": "expected_count",
        "报告数量": "reported_count",
        "匹配数量": "matched_count",
        "错误发现": "false_findings",
        "检查匹配率": "inspect_match_fraction",
        "测绘覆盖率": "survey_coverage",
        "跟随在框率": "follow_target_in_frame_fraction",
        "跟随平均距": "moving_distance_mean_m",
        "跟随最大距": "moving_distance_max_m",
        "跟随连续性": "moving_track_continuity",
        "跟随时间覆盖": "moving_time_coverage",
        "丢失次数": "follow_lost_count",
        "最大重获秒": "follow_reacquire_max_s",
        "误跟次数": "follow_false_confirm_count",
        "步数": "steps",
        "预期通过": "expectations_passed",
    }
    lines += ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for row in table["missions"]:
        lines.append("| " + " | ".join(_cell(row[key]) for key in columns.values()) + " |")
    supervised = [row for row in table["missions"] if "supervisor" in row]
    if supervised:
        lines += [
            "",
            "## Supervisor outcomes",
            "",
            "| Mission | Episode | Final decision | Reason | Return home started | "
            "Onboard log exists | Failed expectations |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in supervised:
            evidence = row["supervisor"]
            failed = [key for key, passed in row["expectation_checks"].items() if not passed]
            values = [
                row["id"],
                row["episode"],
                evidence.get("final_decision"),
                evidence.get("reason"),
                evidence.get("return_home_started"),
                evidence.get("onboard_log_exists"),
                failed,
            ]
            lines.append("| " + " | ".join(_cell(value) for value in values) + " |")
    if table.get("errors"):
        lines += ["", "运行错误：", ""] + [f"- {_cell(e)}" for e in table["errors"]]
    return "\n".join(lines) + "\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def main(argv: list[str] | None = None, *, runner: Callable[..., Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite", type=Path, default=ROOT / "configs/onboard/mission-suite-v2-r12id-r14plan.json"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="+", help="mission ids, space or comma separated")
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-failed-episodes", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return nonzero for collector errors, incomplete rows, or failed expectations",
    )
    parser.add_argument("--min-free-disk-gb", type=float, default=0)
    parser.add_argument("--disk-check-every", type=int, default=1)
    args = parser.parse_args(argv)
    suite = json.loads(repo_path(args.suite).read_text(encoding="utf-8"))
    missions = suite["missions"]
    ids = [m["id"] for m in missions]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
        parser.error("mission ids must be unique, safe directory names")
    if args.episodes < 1:
        parser.error("--episodes must be positive")
    if args.disk_check_every < 1 or args.min_free_disk_gb < 0:
        parser.error("disk-check-every must be positive; disk GB must be nonnegative")
    if args.only:
        selected = {i for value in args.only for i in value.split(",")}
        if unknown := selected - set(ids):
            parser.error(f"unknown mission ids: {', '.join(sorted(unknown))}")
        missions = [m for m in missions if m["id"] in selected]
    for mission in missions:
        for key in ("steps", "max_episode_s"):
            if not isinstance(mission[key], int) or mission[key] < 1:
                parser.error(f"{mission['id']}: {key} must be a positive integer")
        if not isinstance(mission["expect"]["skills"], list):
            parser.error(f"{mission['id']}: expect.skills must be a list")
        config = merge_config(suite["defaults"], mission)
        if "mission_package" in config or "sim_ground_events" in config:
            # Admission validates uploaded plans and references without starting a model.
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            if str(ROOT / "src") not in sys.path:
                sys.path.insert(0, str(ROOT / "src"))
            from multidetect.autonomy.mission_package import load_mission_package
            from scripts.onboard_policy import validate_sim_ground_events

            try:
                package = config.get("mission_package")
                if package is not None:
                    load_mission_package(
                        repo_path(package) if isinstance(package, str) else package
                    )
                validate_sim_ground_events(config)
            except (OSError, TypeError, ValueError) as exc:
                parser.error(f"{mission['id']}: {exc}")
    out = args.out.resolve()
    reports, errors = {}, []
    runner = runner or subprocess.run
    scheduled = []
    for mission in missions:
        mission = {**mission, "episodes": args.episodes}
        scheduled.append(mission)
        directory = out / mission["id"]
        config_path, report_path = directory / "onboard.json", directory / "report.json"
        config = merge_config(suite["defaults"], mission)
        write_json(config_path, config)
        command = build_command(
            mission,
            config_path,
            directory / "campaign",
            report_path,
            args.episodes,
            retry_failed_episodes=args.retry_failed_episodes,
            min_free_disk_gb=args.min_free_disk_gb,
            disk_check_every=args.disk_check_every,
            strict=args.strict,
        )
        if args.dry_run:
            print(
                json.dumps(
                    {"id": mission["id"], "command": command, "config": config},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            continue
        # Remove stale reports so a failed campaign cannot appear successful.
        report_path.unlink(missing_ok=True)
        try:
            runner(command, cwd=ROOT, check=True)
            reports[mission["id"]] = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            errors.append(f"{mission['id']}: {exc}")
    if args.dry_run:
        return 0
    table = build_table(scheduled, reports)
    table["errors"] = errors
    write_json(out / "suite-report.json", table)
    markdown = render_markdown(table)
    (out / "suite-report.md").write_text(markdown, encoding="utf-8", newline="\n")
    print(markdown)
    # Preserve the suite's historical command-line contract by default: a
    # missing row is the only scoring failure.  The model-round runner opts
    # into the stronger operational gate explicitly with --strict.
    if not args.strict:
        return int(any(r["status"] == "missing" for r in table["missions"]))
    return int(
        bool(errors)
        or any(r["status"] != "complete" for r in table["missions"])
        or any("supervisor" in r and not r["expectations_passed"] for r in table["missions"])
    )


if __name__ == "__main__":
    raise SystemExit(main())
