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
METADATA = {"id", "index", "world", "expect", "steps", "max_episode_s", "episodes"}
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
) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "scripts/eval_onboard_loop_campaign.py"),
        "--index",
        str(repo_path(mission["index"])),
        "--split",
        "test",
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


def _expectations(row: dict[str, Any], expect: dict[str, Any]) -> dict[str, bool]:
    checks = {"skills": row["plan_match"]}
    for key, threshold in expect.items():
        if key == "skills":
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
        "步数": "steps",
        "预期通过": "expectations_passed",
    }
    lines += ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"] * len(columns)) + " |"]
    for row in table["missions"]:
        lines.append("| " + " | ".join(_cell(row[key]) for key in columns.values()) + " |")
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
        "--suite", type=Path, default=ROOT / "configs/onboard/mission-suite-v1.json"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="+", help="mission ids, space or comma separated")
    parser.add_argument("--episodes", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    suite = json.loads(repo_path(args.suite).read_text(encoding="utf-8"))
    missions = suite["missions"]
    ids = [m["id"] for m in missions]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids):
        parser.error("mission ids must be unique, safe directory names")
    if args.episodes < 1:
        parser.error("--episodes must be positive")
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
            mission, config_path, directory / "campaign", report_path, args.episodes
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
    return int(bool(errors) or any(r["status"] == "missing" for r in table["missions"]))


if __name__ == "__main__":
    raise SystemExit(main())
