"""Run all SIM mission suites sequentially and merge their reports."""
from __future__ import annotations
import argparse, json, os, shutil, signal, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = Path(r"D:\Projects\mdwt\_scratch\sitl.lock")
DEFAULT_SUITES = {
    "main": ROOT / "configs/onboard/suite-main.json",
    "airframes": ROOT / "configs/onboard/suite-airframes.json",
    "follow": ROOT / "configs/onboard/suite-follow.json",
    "robustness": ROOT / "configs/onboard/suite-robustness.json",
    "e2e": ROOT / "configs/onboard/suite-e2e-operator.json",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0) here: on Windows signal 0 is CTRL_C_EVENT, which
    # interrupts the holder's console group instead of probing it.  The shell
    # lock (sitl_lock.sh) records the holder's Windows PID, so psutil can see it.
    if pid <= 0 or pid == os.getpid():
        return pid == os.getpid()
    import psutil

    return psutil.pid_exists(pid)


class SitlLock:
    def __init__(self, path: Path = DEFAULT_LOCK, label: str = "nightly-regression", retry_s: int = 60, stale_s: int = 120):
        self.path, self.label, self.retry_s, self.stale_s = Path(path), label, retry_s, stale_s
        self.owner = self.path / "owner"
        self.acquired = False
        self.owner_text = ""

    def acquire(self, wait: bool = True) -> None:
        while True:
            if self.owner.exists():
                try:
                    fields = self.owner.read_text(encoding="utf-8").split()
                    pid = int(fields[1]) if len(fields) > 1 else -1
                    age = time.time() - self.owner.stat().st_mtime
                    if age > self.stale_s and not pid_alive(pid):
                        shutil.rmtree(self.path, ignore_errors=True)
                except (OSError, ValueError):
                    pass
            try:
                self.path.mkdir(parents=False)
            except FileExistsError:
                if not wait:
                    raise RuntimeError(f"SITL lock busy: {self.path}")
                time.sleep(self.retry_s)
                continue
            self.owner_text = f"{self.label} {os.getpid()} {now_iso()}"
            self.owner.write_text(self.owner_text + "\n", encoding="utf-8")
            self.acquired = True
            return

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = self.owner.read_text(encoding="utf-8").strip()
            if current == self.owner_text:
                self.owner.unlink(missing_ok=True)
                self.path.rmdir()
        except OSError:
            pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1_000_000_000


def available_memory_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().available / 1_000_000_000
    except ImportError:
        if os.name == "nt":
            out = subprocess.check_output(["powershell", "-NoProfile", "-Command", "[int](Get-Counter '\\Memory\\Available MBytes').CounterSamples[0].CookedValue"], text=True)
            return float(out.strip()) / 1024
        return float(os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / 1_000_000_000


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


def run(argv: list[str] | None = None, runner=subprocess.run) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--suites", nargs="+", default=list(DEFAULT_SUITES), choices=list(DEFAULT_SUITES))
    p.add_argument("--episodes", type=int, default=1)
    p.add_argument("--suite-episodes", action="append", default=[], metavar="NAME=N")
    p.add_argument("--fire-hydrant-episodes", type=int, default=None)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--previous-report", type=Path, default=None)
    p.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    p.add_argument("--min-free-disk-gb", type=float, default=0.0)
    p.add_argument("--disk-check-every", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.episodes < 1: p.error("--episodes must be positive")
    counts = {name: args.episodes for name in args.suites}
    for item in args.suite_episodes:
        name, sep, value = item.partition("=")
        if name not in counts or not sep or int(value) < 1: p.error(f"invalid --suite-episodes: {item}")
        counts[name] = int(value)
    if args.fire_hydrant_episodes is not None: counts["robustness"] = args.fire_hydrant_episodes
    out = (args.out or (ROOT.parent / "_scratch" / "cx-nightly" / ("nightly-" + datetime.now().strftime("%Y%m%d-%H%M%S")))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    previous_path = args.previous_report or (out.parent / "nightly-report.json")
    plans = []
    for name in args.suites:
        config = DEFAULT_SUITES[name]
        if name == "e2e" and not (config.exists() and (ROOT / "scripts/run_e2e_operator_suite.py").exists()):
            continue
        suite_out = out / name
        cmd = [sys.executable, str(ROOT / "scripts/run_mission_suite.py"), "--suite", str(config), "--out", str(suite_out), "--episodes", str(counts[name]), "--min-free-disk-gb", str(args.min_free_disk_gb), "--disk-check-every", str(args.disk_check_every)]
        if name == "e2e": cmd = [sys.executable, str(ROOT / "scripts/run_e2e_operator_suite.py"), "--suite", str(config), "--out", str(suite_out), "--episodes", str(counts[name])]
        plans.append((name, cmd, suite_out))
    if args.dry_run:
        for name, cmd, _ in plans: print(json.dumps({"suite": name, "episodes": counts[name], "command": cmd}, ensure_ascii=False))
        return 0
    records = []
    with SitlLock(args.lock_path):
        for name, cmd, suite_out in plans:
            started = now_iso(); code = 0; error = None
            try:
                preflight(args.min_free_disk_gb); suite_out.mkdir(parents=True, exist_ok=True)
                completed = runner(cmd, cwd=ROOT, check=False)
                code = completed if isinstance(completed, int) else getattr(completed, "returncode", 0)
            except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
                code = getattr(exc, "returncode", 1) or 1; error = str(exc)
            finally:
                try: preflight(args.min_free_disk_gb)
                except Exception as exc: error = error or str(exc); code = code or 1
            report = {}
            rp = suite_out / "suite-report.json"
            if rp.exists(): report = load_suite_report(rp)
            records.append({"name": name, "started_at": started, "ended_at": now_iso(), "exit_code": code, "error": error, "summary": report.get("summary", {}), "metrics": _metric_summary(report.get("missions", [])), "report": str(rp)})
    merged = {"generated_at": now_iso(), "suites": records}
    previous = load_suite_report(previous_path) if previous_path.exists() else None
    merged["regressions"] = compare_reports(merged, previous)
    (out / "nightly-report.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# SIM 夜间回归合并报告", "", f"生成时间：{merged['generated_at']}", "", "| 套件 | 起止 | 退出码 | 预期通过率 | 计划准确率 | 关键指标 |", "| --- | --- | ---: | ---: | ---: | --- |"]
    for r in records:
        s=r["summary"]; lines.append(f"| {r['name']} | {r['started_at']} → {r['ended_at']} | {r['exit_code']} | {s.get('expectation_pass_rate','—')} | {s.get('plan_accuracy','—')} | {json.dumps(r['metrics'],ensure_ascii=False)} |")
    lines += ["", "## 回退", ""]
    lines += [f"- {x['suite']} {x['metric']}: {x['previous']} → {x['current']} (Δ{x['delta']})" for x in merged["regressions"]] or ["- 未发现回退。"]
    (out / "nightly-report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return int(any(r["exit_code"] for r in records))

if __name__ == "__main__": raise SystemExit(run())






