"""Build a chronological pass/fail matrix from nightly regression runs."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_nightly_regression import mission_triage


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _timestamp(value: Any, fallback: Path) -> tuple[datetime, str | None]:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed, value
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback.stat().st_mtime, timezone.utc), None


def _commit(report: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    code = report.get("provenance", {}).get("code", {})
    if not isinstance(code, dict):
        code = {}
    if not code and isinstance(fallback, dict):
        code = fallback
    return {"commit": code.get("commit"), "short_commit": code.get("short_commit")}


def _suite_rows(suite: dict[str, Any], run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    report_path = Path(str(suite.get("report", ""))) if suite.get("report") else run_dir / str(suite.get("name", "")) / "suite-report.json"
    if not report_path.is_file():
        report_path = run_dir / str(suite.get("name", "")) / "suite-report.json"
    if report_path.is_file():
        report = _read_json(report_path)
        return mission_triage(report, report_path.parent), _commit(report)
    # Older or partial aggregate reports may contain already-triaged rows.
    rows = suite.get("missions", [])
    return [row for row in rows if isinstance(row, dict)], _commit({}, suite.get("provenance"))


def discover_runs(run_glob: str | Path) -> list[dict[str, Any]]:
    """Read run directories matching *run_glob* without modifying them."""
    pattern = str(run_glob)
    candidates = sorted(Path().glob(pattern) if not Path(pattern).is_absolute() else Path("/").glob(pattern.lstrip("/")))
    runs: list[dict[str, Any]] = []
    for candidate in candidates:
        run_dir = candidate.parent if candidate.is_file() else candidate
        report_path = run_dir / "nightly-report.json"
        if not report_path.is_file() or any(r["path"] == str(run_dir.resolve()) for r in runs):
            continue
        try:
            aggregate = _read_json(report_path)
        except (OSError, ValueError):
            continue
        stamp, original_stamp = _timestamp(aggregate.get("generated_at"), run_dir)
        suites: list[dict[str, Any]] = []
        aggregate_prov = aggregate.get("provenance", {}).get("code", {})
        for suite in aggregate.get("suites", []):
            if not isinstance(suite, dict) or not suite.get("name"):
                continue
            rows, provenance = _suite_rows(suite, run_dir)
            if not provenance.get("commit") and isinstance(aggregate_prov, dict):
                provenance = _commit({}, aggregate_prov)
            suites.append({"name": suite["name"], "missions": rows, "provenance": provenance})
        runs.append({"name": run_dir.name, "path": str(run_dir.resolve()), "generated_at": original_stamp,
                     "sort_time": stamp, "suites": suites})
    runs.sort(key=lambda item: (item["sort_time"], item["name"]))
    for run in runs:
        run.pop("sort_time", None)
    return runs


def _cell(rows: list[dict[str, Any]], run: dict[str, Any]) -> dict[str, Any]:
    failures = [row for row in rows if row.get("failure_category") is not None]
    passed = not failures and all(row.get("failure_category") is None for row in rows)
    categories = [row.get("failure_category") for row in failures if row.get("failure_category")]
    return {"run": run["name"], "status": "pass" if passed else "fail", "failure_category": categories[0] if categories else None,
            "failure_categories": sorted(set(categories)), "attempts": len(rows),
            "commit": run.get("commit"), "short_commit": run.get("short_commit")}


def build_trend(runs: Iterable[dict[str, Any]]) -> dict[str, Any]:
    runs = list(runs)
    columns = [{"name": r["name"], "generated_at": r.get("generated_at"),
                "commits": {s["name"]: s.get("provenance", {}) for s in r.get("suites", [])}}
               for r in runs]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for run in runs:
        for suite in run.get("suites", []):
            by_mission: dict[str, list[dict[str, Any]]] = {}
            for row in suite.get("missions", []):
                if row.get("id") is not None:
                    by_mission.setdefault(str(row["id"]), []).append(row)
            commit = suite.get("provenance", {}).get("commit")
            short = suite.get("provenance", {}).get("short_commit")
            run_copy = dict(run, commit=commit, short_commit=short)
            for mission, rows in by_mission.items():
                grouped.setdefault((str(suite["name"]), mission), []).append(_cell(rows, run_copy))

    matrix = []
    regressions = []
    flaky = []
    for (suite, mission), cells in sorted(grouped.items()):
        by_run = {cell["run"]: cell for cell in cells}
        ordered = [by_run.get(run["name"], {"run": run["name"], "status": "missing", "failure_category": None,
                                              "failure_categories": [], "attempts": 0, "commit": None, "short_commit": None})
                   for run in runs]
        prior = None
        for cell in ordered:
            if cell["status"] == "missing":
                continue
            if prior and prior["status"] == "pass" and cell["status"] == "fail":
                regressions.append({"suite": suite, "mission": mission, "previous_run": prior["run"], "run": cell["run"],
                                    "failure_category": cell.get("failure_category"), "previous_commit": prior.get("short_commit"),
                                    "commit": cell.get("short_commit")})
            prior = cell
        statuses = {cell["status"] for cell in ordered if cell["status"] != "missing"}
        is_flaky = statuses == {"pass", "fail"}
        entry = {"suite": suite, "mission": mission, "runs": ordered, "flaky": is_flaky}
        matrix.append(entry)
        if is_flaky:
            flaky.append({"suite": suite, "mission": mission,
                          "runs": [cell["run"] for cell in ordered if cell["status"] != "missing"]})
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "runs": columns, "matrix": matrix,
            "regressions": regressions, "flaky": flaky}


def _md(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", "<br>")


def render_markdown(trend: dict[str, Any]) -> str:
    runs = trend["runs"]
    lines = ["# Nightly 趋势报告", "", "按生成时间排序；`P`=通过，`F`=失败，`—`=该运行未包含任务。", "",
             "| 套件 | 任务 | " + " | ".join(_md(run["name"]) for run in runs) + " | 状态 |", "| --- | --- | " + " | ".join("---" for _ in runs) + " | --- |"]
    for entry in trend["matrix"]:
        cells = entry["runs"]
        symbols = [("P" if c["status"] == "pass" else "F" if c["status"] == "fail" else "—") +
                   (f" ({c['failure_category']})" if c.get("failure_category") else "") for c in cells]
        lines.append("| " + " | ".join([_md(entry["suite"]), _md(entry["mission"]), *map(_md, symbols), "flaky" if entry["flaky"] else "—"]) + " |")
    lines += ["", "## 提交来源", "", "| 运行 | 套件提交 |", "| --- | --- |"]
    for run in runs:
        commits = ", ".join(f"{name}: {info.get('short_commit') or info.get('commit') or '—'}" for name, info in run["commits"].items()) or "—"
        lines.append(f"| {_md(run['name'])} | {_md(commits)} |")
    lines += ["", "## 回退", ""]
    lines += [f"- {x['suite']} / {x['mission']}: {x['previous_run']} → {x['run']} ({x.get('failure_category') or 'other'})" for x in trend["regressions"]] or ["- 未发现回退。"]
    lines += ["", "## Flaky 任务", ""]
    lines += [f"- {x['suite']} / {x['mission']}: {', '.join(x['runs'])}" for x in trend["flaky"]] or ["- 未发现 flaky 任务。"]
    return "\n".join(lines) + "\n"


def write_outputs(trend: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nightly-trend.json").write_text(json.dumps(trend, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "nightly-trend.md").write_text(render_markdown(trend), encoding="utf-8")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--glob", dest="run_glob", default="artifacts/benchmarks/nightly-linux-*")
    parser.add_argument("--out", type=Path, default=Path(".tmp/nightly-trend"))
    args = parser.parse_args(argv)
    trend = build_trend(discover_runs(args.run_glob))
    write_outputs(trend, args.out)
    print(f"wrote {args.out / 'nightly-trend.json'} and {args.out / 'nightly-trend.md'} ({len(trend['runs'])} runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
