#!/usr/bin/env python3
"""Run a batch of operator-link SITL end-to-end missions.

The suite format intentionally mirrors ``mission_package.py`` packages while
allowing a case to provide only ``prompt``/``task_text`` and the mission
geometry.  ``--dry-run`` only renders the commands and never touches Docker or
the SITL lock.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ.get("MULTIDETECT_E2E_SCRATCH", r"D:\Projects\mdwt\_scratch\cx-e2e-multi"))
LOCK_DIR = Path(os.environ.get("MULTIDETECT_SITL_LOCK", r"D:\Projects\mdwt\_scratch\sitl.lock"))


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_suite(path: Path) -> list[dict[str, Any]]:
    """Load and validate a suite, accepting ``tasks`` or ``missions`` roots."""
    doc = _json(path)
    cases = (
        doc.get("tasks", doc.get("missions", doc.get("cases"))) if isinstance(doc, dict) else doc
    )
    if not isinstance(cases, list) or not cases:
        raise ValueError("suite must contain a non-empty tasks/missions/cases array")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict) or not case.get("id"):
            raise ValueError("each suite task requires an id")
        ident = str(case["id"])
        if ident in seen:
            raise ValueError(f"duplicate suite task id: {ident}")
        seen.add(ident)
        if (
            not case.get("package")
            and not case.get("mission_package")
            and not (case.get("prompt") or case.get("task_text"))
        ):
            raise ValueError(f"{ident}: provide package/mission_package or prompt/task_text")
        result.append(dict(case))
    return result


def _package_for(case: dict[str, Any], suite_path: Path, out: Path) -> Path:
    supplied = case.get("package") or case.get("mission_package")
    if isinstance(supplied, str):
        p = Path(supplied)
        return (suite_path.parent / p).resolve() if not p.is_absolute() else p
    package = (
        dict(supplied)
        if isinstance(supplied, dict)
        else {
            "mission_id": str(case["id"]),
            "task_text": case.get("prompt", case.get("task_text", "")),
            "risk_level": case.get("risk_level", "low"),
            "link_loss_policy": case.get(
                "link_loss_policy", {"action": "continue", "timeout_s": 0}
            ),
            "heartbeat_timeout_s": case.get("heartbeat_timeout_s", 5),
            "home": case.get("home", [0, 0]),
            "places": case.get("places", {}),
            "zones": case.get("zones", {}),
            "routes": case.get("routes", {}),
            "ref_labels": case.get("ref_labels", {}),
            "operating_area": case.get(
                "operating_area",
                {
                    "north_min_m": -100,
                    "north_max_m": 100,
                    "east_min_m": -100,
                    "east_max_m": 100,
                    "min_altitude_m": 5,
                    "max_altitude_m": 120,
                },
            ),
            "budget": case.get(
                "budget",
                {"time_s": 3600, "reserve_time_s": 180, "energy_wh": 600, "reserve_energy_wh": 60},
            ),
            "oversight_steps": case.get("oversight_steps", []),
        }
    )
    package.setdefault("mission_id", str(case["id"]))
    package.setdefault("task_text", case.get("prompt", case.get("task_text", "")))
    out.mkdir(parents=True, exist_ok=True)
    target = out / "package.json"
    target.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def build_commands(
    case: dict[str, Any], run_dir: Path, package: Path, *, python: str = sys.executable
) -> dict[str, list[str]]:
    """Return the three commands used by a case; kept pure for dry-run tests."""
    onboard = run_dir / "onboard"
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, str(run_dir.resolve())).hex[:8]
    safe_id = re.sub(r"[^a-z0-9-]+", "-", str(case["id"]).lower()).strip("-")
    container = f"multidetect-e2e-{safe_id}-{suffix}"
    session = [
        python,
        str(ROOT / "scripts/run_sim_session.py"),
        "--world",
        str(case.get("world", "gen_cnt2_w0000")),
        "--airframe",
        str(case.get("airframe", "x500_mono_cam")),
        "--gcs-port",
        str(case.get("gcs_port", 14560)),
        "--operator-link",
        "--exit-after-task",
        "--onboard-task",
        "operator link mission",
        "--onboard-out",
        str(onboard),
        "--session-log",
        str(run_dir / "session.jsonl"),
        "--container-name",
        container,
    ]
    gcs = [
        python,
        str(ROOT / "scripts/fake_gcs_console.py"),
        str(package),
        "--link",
        str(onboard / "operator-link.json"),
        "--auto-approve",
        "--max-seconds",
        str(case.get("gcs_timeout_s", 2700)),
        "--log",
        str(run_dir / "gcs-documents.jsonl"),
    ]
    evaluate = [
        python,
        str(ROOT / "scripts/run_e2e_operator_acceptance.py"),
        "--evaluate",
        str(run_dir),
        "--report",
        str(run_dir / "operator-acceptance"),
    ]
    return {"session": session, "gcs": gcs, "evaluate": evaluate, "container": [container]}


class SitlLock:
    def __init__(self, path: Path = LOCK_DIR, owner: str = "e2e-suite") -> None:
        self.path, self.owner = path, owner
        self.acquired = False

    def acquire(self, timeout: float = 0) -> None:
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            try:
                self.path.mkdir(parents=True)
                (self.path / "owner").write_text(
                    f"{self.owner} {os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n",
                    encoding="utf-8",
                )
                self.acquired = True
                return
            except FileExistsError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"SITL lock busy: {self.path}") from None
                time.sleep(min(5, max(0.2, (deadline - time.monotonic()) if deadline else 5)))

    def release(self) -> None:
        if self.acquired:
            owner_path = self.path / "owner"
            try:
                fields = owner_path.read_text(encoding="utf-8").split()
                owns_lock = len(fields) >= 2 and fields[1] == str(os.getpid())
            except OSError:
                owns_lock = False
            if owns_lock:
                shutil.rmtree(self.path, ignore_errors=True)
            self.acquired = False


def _terminate(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def cleanup_owned(processes: Iterable[subprocess.Popen[Any]], containers: Iterable[str]) -> None:
    """Terminate only recorded PIDs and explicitly recorded container names."""
    for proc in reversed(list(processes)):
        _terminate(proc)
    for name in set(containers):
        if re.fullmatch(r"multidetect-e2e-[a-z0-9-]+", name):
            subprocess.run(
                ["docker", "rm", "-f", name],
                cwd=ROOT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


# Small public aliases make the orchestration primitives convenient to exercise
# without starting SITL (and keep callers independent of the implementation
# helper names).
materialize_package = _package_for
cleanup_owned_resources = cleanup_owned


def render_dry_run(cases: list[dict[str, Any]], root: Path) -> str:
    lines = [f"suite: {len(cases)} task(s)", f"scratch: {root}"]
    for case in cases:
        run_dir = root / str(case["id"])
        package = (
            run_dir / "package.json"
            if not isinstance(case.get("package") or case.get("mission_package"), str)
            else Path(case.get("package") or case.get("mission_package"))
        )
        commands = build_commands(case, run_dir, package)
        lines += [
            f"[{case['id']}]",
            "  session: " + subprocess.list2cmdline(commands["session"]),
            "  gcs: " + subprocess.list2cmdline(commands["gcs"]),
            "  evaluate: " + subprocess.list2cmdline(commands["evaluate"]),
        ]
    return "\n".join(lines)


def run_suite(
    suite_path: Path, *, output_root: Path, dry_run: bool = False, lock_timeout: float = 0
) -> int:
    cases = load_suite(suite_path)
    if dry_run:
        print(render_dry_run(cases, output_root))
        return 0
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    lock = SitlLock(owner=f"e2e-suite {os.getpid()}")
    try:
        lock.acquire(lock_timeout)
        for case in cases:
            case_root = output_root / str(case["id"])
            run_dir = case_root
            if run_dir.exists():
                run_dir = case_root / time.strftime("%Y%m%d-%H%M%S")
                if run_dir.exists():
                    run_dir = case_root / (
                        time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
                    )
            run_dir.mkdir(parents=True)
            package = _package_for(case, suite_path, run_dir)
            commands = build_commands(case, run_dir, package)
            processes: list[subprocess.Popen[Any]] = []
            handles = []
            verdict = "ERROR"
            checks: dict[str, bool] = {}
            try:
                handles.append((run_dir / "session.out").open("w", encoding="utf-8"))
                session = subprocess.Popen(
                    commands["session"], cwd=ROOT, stdout=handles[-1], stderr=subprocess.STDOUT
                )
                processes.append(session)
                link = run_dir / "onboard" / "operator-link.json"
                deadline = time.monotonic() + float(case.get("link_timeout_s", 180))
                while not link.is_file() and session.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.25)
                if not link.is_file():
                    raise RuntimeError("operator-link.json was not produced")
                handles.append((run_dir / "gcs.out").open("w", encoding="utf-8"))
                gcs = subprocess.Popen(
                    commands["gcs"], cwd=ROOT, stdout=handles[-1], stderr=subprocess.STDOUT
                )
                processes.append(gcs)
                session.wait(timeout=float(case.get("session_timeout_s", 5400)))
                _terminate(gcs)
                verdict_proc = subprocess.run(
                    commands["evaluate"], cwd=ROOT, capture_output=True, text=True, check=False
                )
                (run_dir / "evaluation.stdout").write_text(
                    verdict_proc.stdout + verdict_proc.stderr, encoding="utf-8"
                )
                evaluation = _json(run_dir / "operator-acceptance.json")
                expect = case.get("expect") or {}
                criterion = expect.get("criterion")
                if criterion:
                    checks[str(criterion)] = (
                        evaluation.get("criteria", {}).get(str(criterion)) is True
                    )
                expected_skills = expect.get("skills")
                if expected_skills:
                    checks["expected_skills"] = (
                        evaluation.get("plan", {}).get("skills") == expected_skills
                    )
                verdict = (
                    "PASS" if verdict_proc.returncode == 0 and all(checks.values()) else "FAIL"
                )
            except Exception as exc:
                (run_dir / "error.txt").write_text(str(exc) + "\n", encoding="utf-8")
            finally:
                cleanup_owned(processes, commands["container"])
                for handle in handles:
                    handle.close()
            results.append(
                {
                    "id": case["id"],
                    "run_dir": str(run_dir),
                    "verdict": verdict,
                    "expected": case.get("expect", {}),
                    "checks": checks,
                }
            )
    finally:
        lock.release()
    (output_root / "suite-results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Operator E2E Suite",
        "",
        "| Task | Verdict | Checks | Run directory |",
        "|---|---|---|---|",
    ]
    lines += [
        f"| {r['id']} | {r['verdict']} | "
        f"`{json.dumps(r['checks'], ensure_ascii=False)}` | `{r['run_dir']}` |"
        for r in results
    ]
    (output_root / "suite-results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if all(r["verdict"] == "PASS" for r in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", type=Path)
    parser.add_argument("--output", type=Path, default=SCRATCH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock-timeout", type=float, default=0)
    args = parser.parse_args(argv)
    try:
        return run_suite(
            args.suite.resolve(),
            output_root=args.output.resolve(),
            dry_run=args.dry_run,
            lock_timeout=args.lock_timeout,
        )
    except (OSError, ValueError, TimeoutError) as exc:
        print(f"suite failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
