"""Run all SIM mission suites sequentially and merge their reports."""
from __future__ import annotations
import argparse, json, os, re, shutil, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from multidetect.provenance import build_provenance
from scripts.sitl_lock import (
    SitlLock,
    available_memory_gb as _available_memory_gb,
    default_lock_path,
    sitl_lock_context,
)
DEFAULT_LOCK = default_lock_path()
DEFAULT_SUITES = {
    "main": ROOT / "configs/onboard/suite-main.json",
    "airframes": ROOT / "configs/onboard/suite-airframes.json",
    "follow": ROOT / "configs/onboard/suite-follow.json",
    "robustness": ROOT / "configs/onboard/suite-robustness.json",
    "e2e": ROOT / "configs/onboard/suite-e2e-operator.json",
}
MODEL_SET_SUITES = {
    "default": DEFAULT_SUITES,
    "r21idr3-r24plan": {
        "main": ROOT / "configs/onboard/suite-main-r21idr3-r24plan.json",
        "airframes": ROOT / "configs/onboard/suite-airframes-r21idr3-r24plan.json",
        "follow": ROOT / "configs/onboard/suite-follow-r21idr3-r24plan.json",
        "robustness": ROOT / "configs/onboard/suite-robustness-r21idr3-r24plan.json",
    },
}
SIM_ENV_KEYS = (
    "MULTIDETECT_SIM_GPU",
    "MULTIDETECT_SIM_IMAGE",
    "MULTIDETECT_SIM_GPU_DEVICES",
    "MULTIDETECT_SIM_GPU_AVOID",
    "MULTIDETECT_SIM_NETWORK",
    "MULTIDETECT_SITL_LOCK",
)
SIM_CONTROL_PORT = 14540
SIM_TELEMETRY_PORT = 14541
FAILURE_CATEGORIES = (
    "offboard_timeout", "land_timeout", "disarm_rejected", "telemetry_stall",
    "sensor_stream_eof", "preflight_health", "plan_refused", "other",
)


def pid_alive(pid: int) -> bool:
    """Compatibility probe used by older callers without sending a signal."""
    if pid <= 0:
        return False
    import psutil

    return psutil.pid_exists(pid)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1_000_000_000


def available_memory_gb() -> float:
    return _available_memory_gb()


def preflight(min_free_disk_gb: float, root: Path = ROOT) -> None:
    free = disk_free_gb(root)
    mem = available_memory_gb()
    if free < min_free_disk_gb:
        raise RuntimeError(f"free disk {free:.2f} GB < {min_free_disk_gb:.2f} GB")
    if mem < 5.0:
        raise RuntimeError(f"available memory {mem:.2f} GB < 5.00 GB")


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("alert_precision", "alert_recall", "count_exact", "false_findings", "safety_pass", "follow_target_in_frame_fraction", "follow_lost_count", "follow_reacquire_max_s", "follow_false_confirm_count")
    out = {}
    for field in fields:
        vals = [r.get(field) for r in rows if isinstance(r.get(field), (int, float)) and not isinstance(r.get(field), bool)]
        if vals: out[field] = sum(vals) / len(vals)
        elif any(field in r for r in rows): out[field] = None
    variants = {}
    for row in rows:
        variants.setdefault(row.get("variant", "base"), []).append(row.get("expectations_passed", False))
    out["variant_task_matrix"] = {k: {"tasks": len(v), "passed": sum(v), "pass_rate": sum(v)/len(v) if v else None} for k, v in variants.items()}
    return out


def compare_reports(current: dict[str, Any], previous: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not previous: return []
    regressions = []
    for suite in current.get("suites", []):
        old = next((x for x in previous.get("suites", []) if x.get("name") == suite.get("name")), None)
        if not old: continue
        cs, os_ = suite.get("summary", {}), old.get("summary", {})
        for key in ("expectation_pass_rate", "plan_accuracy"):
            if isinstance(cs.get(key), (int, float)) and isinstance(os_.get(key), (int, float)) and cs[key] < os_[key]:
                regressions.append({"suite": suite["name"], "metric": key, "previous": os_[key], "current": cs[key], "delta": cs[key]-os_[key]})
        cfalse = suite.get("metrics", {}).get("false_findings", 0) or 0
        ofalse = old.get("metrics", {}).get("false_findings", 0) or 0
        if cfalse > ofalse:
            regressions.append({"suite": suite["name"], "metric": "false_findings", "previous": ofalse, "current": cfalse, "delta": cfalse-ofalse})
    return regressions


def load_suite_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def final_campaign_exception(log: Path) -> tuple[str | None, str, str | None]:
    """Return the last traceback's exception and context, never a JSON event error."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, "", f"{type(exc).__name__}: {exc}"
    traces = list(re.finditer(r"^Traceback \(most recent call last\):[ \t]*$", text, re.MULTILINE))
    if not traces:
        return None, "", None
    # Unindented exception classes distinguish the terminal line from frames,
    # source code, chained causes and the collector's JSON diagnostics.
    context = text[traces[-1].end():]
    match = re.search(
        r"^(?:[\w]+\.)*[A-Z][\w]*(?::[^\r\n]*)?$", context, re.MULTILINE
    )
    if not match:
        return None, context, None
    message = [match.group()]
    for line in context[match.end():].splitlines():
        if not line.strip() and not message[1:]:
            continue
        if not line.strip() or line.startswith(("{", "Traceback ")):
            break
        message.append(line)
    return "\n".join(message), context[:match.end()], None


def failure_category(exception: str | None, context: str = "", *, row: dict[str, Any]) -> str:
    """Prefer the final exception; only use structured plan evidence if absent."""
    text = (exception or "").lower()
    timeout = any(word in text for word in ("timeout", "timed out", "deadline"))
    if "telemetry" in text and any(word in text for word in ("stall", "stale", "timeout", "timed out")):
        return "telemetry_stall"
    if "offboard" in text and timeout:
        return "offboard_timeout"
    if "disarm" in text and any(word in text for word in ("reject", "denied", "refused")):
        return "disarm_rejected"
    if re.search(r"\bland(?:ing)?\b|landing_timeout", text) and timeout and "disarm" not in text:
        return "land_timeout"
    sensor = any(word in text for word in ("sensor stream", "sensor_stream", "mdgz"))
    eof = any(word in text for word in ("eof", "end of file", "ended unexpectedly", "broken pipe"))
    if (sensor and eof) or (
        any(word in text for word in ("brokenpipeerror", "eoferror"))
        and "sim_teacher/streams.py" in context.replace("\\", "/")
        and ("_fresh_sensor_snapshot" in context or "request_stream.write" in context)
    ):
        return "sensor_stream_eof"
    if any(word in text for word in ("preflight", "pre-flight", "pre-arm health", "prearm health")):
        return "preflight_health"
    if "plan" in text and any(word in text for word in ("refus", "reject")):
        return "plan_refused"
    if not exception:
        if row.get("planned_skills") == ["refuse"] or any(
            step.get("reason") == "plan_refused" for step in (row.get("step_outcomes") or [])
        ):
            return "plan_refused"
    return "other"


def mission_triage(report: dict[str, Any], suite_out: Path) -> list[dict[str, Any]]:
    rows = []
    logs = {}
    for row in report.get("missions", []):
        log = suite_out / row["id"] / "campaign" / f"{row['world']}.log"
        if log not in logs:
            logs[log] = final_campaign_exception(log)
        exception, context, log_error = logs[log]
        failed = [name for name, passed in row.get("expectation_checks", {}).items() if not passed]
        complete = row.get("status") == "complete"
        is_failure = not complete or bool(failed) or row.get("expectations_passed") is False
        rows.append({
            "id": row["id"], "world": row["world"], "episode": row.get("episode"),
            "complete": complete, "mission_completed": bool(row.get("mission_completed", False)),
            "final_exception": exception, "failed_expectations": failed,
            "failure_category": failure_category(exception, context, row=row) if is_failure else None,
            "campaign_log": str(log), "log_error": log_error,
        })
    return rows


def _markdown_cell(value: Any) -> str:
    if value is None:
        return "—"
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;"
    ).replace("|", "&#124;").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def write_nightly_report(merged: dict[str, Any], out: Path) -> None:
    records = merged["suites"]
    histogram = {category: 0 for category in FAILURE_CATEGORIES}
    for record in records:
        for row in record.get("missions", []):
            if row["failure_category"] is not None:
                histogram[row["failure_category"]] += 1
    merged["failure_categories"] = histogram
    lines = ["# SIM 夜间回归合并报告", "", f"生成时间：{merged['generated_at']}", "", "| 套件 | 起止 | 退出码 | 预期通过率 | 计划准确率 | 关键指标 |", "| --- | --- | ---: | ---: | ---: | --- |"]
    for r in records:
        s = r["summary"]
        values = (r["name"], f"{r.get('started_at') or '—'} → {r.get('ended_at') or '—'}", r["exit_code"], s.get("expectation_pass_rate"), s.get("plan_accuracy"), json.dumps(r["metrics"], ensure_ascii=False))
        lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    lines += ["", "## 逐任务失败分诊", "",
              "complete 表示采集状态为 complete；任务完成对应 mission_completed，安全中止可能为 no。",
              "异常取该任务、该世界 campaign 日志中的最后一条 traceback；同一 campaign 的多架次共享此诊断，不代表逐架次异常定位。",
              "分类仅统计采集不完整或预期未通过的记录；无失败为 —。日志缺失会显式标注，无法归类的失败为 other。", "",
              "| 套件 | 任务 | 世界 | 架次 | complete | 任务完成 | 最终异常（类 + 消息） | 失败预期 | 失败分类 |",
              "| --- | --- | --- | ---: | --- | --- | --- | --- | --- |"]
    for r in records:
        for row in r.get("missions", []):
            exception = row["final_exception"]
            if row["log_error"]:
                exception = f"日志不可读：{row['log_error']}"
            values = (r["name"], row["id"], row["world"], row["episode"],
                      "yes" if row["complete"] else "no", "yes" if row["mission_completed"] else "no",
                      exception, ", ".join(row["failed_expectations"]) or None, row["failure_category"])
            lines.append("| " + " | ".join(_markdown_cell(value) for value in values) + " |")
    lines += ["", "## 失败分类直方图", "", "按失败记录计数（每任务 × 架次一条）；成功记录不计入。", "",
              "| 分类 | 数量 |", "| --- | ---: |"]
    lines += [f"| {category} | {count} |" for category, count in histogram.items()]
    lines += ["", "## 回退", ""]
    lines += [f"- {x['suite']} {x['metric']}: {x['previous']} → {x['current']} (Δ{x['delta']})" for x in merged["regressions"]] or ["- 未发现回退。"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "nightly-report.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "nightly-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(argv: list[str] | None = None, runner=subprocess.run) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-set", choices=list(MODEL_SET_SUITES), default=os.environ.get("MULTIDETECT_MODEL_SET", "default"))
    p.add_argument("--suites", nargs="+", default=None)
    p.add_argument("--episodes", type=int, default=1)
    p.add_argument("--suite-episodes", action="append", default=[], metavar="NAME=N")
    p.add_argument("--fire-hydrant-episodes", type=int, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--report-only", type=Path, nargs="+", metavar="SUITE_DIR",
                   help="read existing suite outputs without running collectors; requires a separate --out")
    p.add_argument("--previous-report", type=Path, default=None)
    p.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    p.add_argument("--min-free-disk-gb", type=float, default=0.0)
    p.add_argument("--disk-check-every", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--gpu-render", action=argparse.BooleanOptionalAction, default=None)
    p.add_argument("--sim-image", default=None, metavar="IMAGE")
    p.add_argument(
        "--keep-px4-ulog",
        action="store_true",
        help="copy PX4 .ulg flight logs into each collector job before cleanup",
    )
    args = p.parse_args(argv)
    if args.report_only:
        if args.out is None:
            p.error("--report-only requires --out outside the input suite directories")
        sources = [source.resolve() for source in args.report_only]
        out = args.out.resolve()
        if any(out == source or source in out.parents for source in sources):
            p.error("--out must be outside the input suite directories")
        if len({source.name for source in sources}) != len(sources):
            p.error("--report-only suite directory names must be unique")
        records = []
        for source in sources:
            report_path = source / "suite-report.json"
            report = load_suite_report(report_path)
            records.append({"name": source.name, "started_at": None, "ended_at": None,
                            "exit_code": None, "error": None, "summary": report.get("summary", {}),
                            "metrics": _metric_summary(report.get("missions", [])),
                            "missions": mission_triage(report, source), "report": str(report_path)})
        merged = {"generated_at": now_iso(), "suites": records,
                  "provenance": build_provenance(root=ROOT, command=sys.argv)}
        previous = load_suite_report(args.previous_report) if args.previous_report else None
        merged["regressions"] = compare_reports(merged, previous)
        write_nightly_report(merged, out)
        return 0
    if args.model_set not in MODEL_SET_SUITES:
        p.error(f"unknown --model-set: {args.model_set}")
    suite_paths = MODEL_SET_SUITES[args.model_set]
    suites = list(suite_paths) if args.suites is None else args.suites
    unknown_suites = sorted(set(suites) - set(suite_paths))
    if unknown_suites:
        p.error(f"unknown suites for model set {args.model_set}: {', '.join(unknown_suites)}")
    if args.episodes < 1: p.error("--episodes must be positive")
    counts = {name: args.episodes for name in suites}
    for item in args.suite_episodes:
        name, sep, value = item.partition("=")
        if name not in counts or not sep or int(value) < 1: p.error(f"invalid --suite-episodes: {item}")
        counts[name] = int(value)
    if args.fire_hydrant_episodes is not None:
        if "robustness" not in counts:
            p.error("--fire-hydrant-episodes requires the robustness suite")
        counts["robustness"] = args.fire_hydrant_episodes
    child_env = os.environ.copy()
    if args.gpu_render is not None:
        child_env["MULTIDETECT_SIM_GPU"] = "1" if args.gpu_render else "0"
    if args.sim_image is not None:
        child_env["MULTIDETECT_SIM_IMAGE"] = args.sim_image
    lock_path = args.lock_path.expanduser().resolve()
    child_env["MULTIDETECT_SITL_LOCK"] = str(lock_path)
    forwarded_sim_env = {key: child_env[key] for key in SIM_ENV_KEYS if key in child_env}
    out = (args.out or (ROOT.parent / "_scratch" / "cx-nightly" / ("nightly-" + datetime.now().strftime("%Y%m%d-%H%M%S")))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    previous_path = args.previous_report or (out.parent / "nightly-report.json")
    plans = []
    for name in suites:
        config = suite_paths[name]
        if name == "e2e" and not (config.exists() and (ROOT / "scripts/run_e2e_operator_suite.py").exists()):
            continue
        suite_out = out / name
        cmd = [sys.executable, str(ROOT / "scripts/run_mission_suite.py"), "--suite", str(config), "--out", str(suite_out), "--episodes", str(counts[name]), "--min-free-disk-gb", str(args.min_free_disk_gb), "--disk-check-every", str(args.disk_check_every)]
        if args.keep_px4_ulog:
            cmd.append("--keep-px4-ulog")
        if name == "e2e": cmd = [sys.executable, str(ROOT / "scripts/run_e2e_operator_suite.py"), "--suite", str(config), "--out", str(suite_out), "--episodes", str(counts[name])]
        plans.append((name, cmd, suite_out))
    if args.dry_run:
        print(json.dumps({
            "model_set": args.model_set,
            "lock_path": str(lock_path),
            "sim_control_port": SIM_CONTROL_PORT,
            "sim_telemetry_port": SIM_TELEMETRY_PORT,
            "sim_environment": forwarded_sim_env,
        }, ensure_ascii=False))
        for name, cmd, _ in plans: print(json.dumps({"suite": name, "episodes": counts[name], "command": cmd}, ensure_ascii=False))
        return 0
    records = []
    with sitl_lock_context(SitlLock(lock_path)):
        # Children inherit this lock; without the marker each collector waits on it forever.
        child_env["MULTIDETECT_SITL_LOCK_HELD"] = "1"
        for name, cmd, suite_out in plans:
            started = now_iso(); code = 0; error = None
            try:
                preflight(args.min_free_disk_gb); suite_out.mkdir(parents=True, exist_ok=True)
                completed = runner(cmd, cwd=ROOT, check=False, env=child_env)
                code = completed if isinstance(completed, int) else getattr(completed, "returncode", 0)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                code = getattr(exc, "returncode", 1) or 1; error = str(exc)
            finally:
                try: preflight(args.min_free_disk_gb)
                except Exception as exc: error = error or str(exc); code = code or 1
            report = {}
            rp = suite_out / "suite-report.json"
            if rp.exists(): report = load_suite_report(rp)
            records.append({"name": name, "started_at": started, "ended_at": now_iso(), "exit_code": code, "error": error, "summary": report.get("summary", {}), "metrics": _metric_summary(report.get("missions", [])), "missions": mission_triage(report, suite_out), "report": str(rp)})
    merged = {"generated_at": now_iso(), "suites": records,
              "provenance": build_provenance(root=ROOT, config_paths=[suite_paths[n] for n in suites if suite_paths[n].is_file()], command=sys.argv)}
    previous = load_suite_report(previous_path) if previous_path.exists() else None
    merged["regressions"] = compare_reports(merged, previous)
    write_nightly_report(merged, out)
    return int(any(r["exit_code"] for r in records))

if __name__ == "__main__": raise SystemExit(run())






