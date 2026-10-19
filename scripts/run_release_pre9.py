#!/usr/bin/env python3
"""Resumable, fail-closed release-candidate orchestrator for 0.3.0-pre.9.

This file is the scheduling layer only.  Runtime stages write immutable logs and
fresh evidence below ``artifacts/release-pre9-runs``; dry-run never creates that
directory and never edits a release input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET_VERSION = "0.3.0-pre.9"
TARGET_PYTHON_VERSION = "0.3.0rc9"
STAGE_NAMES = (
    "preflight",
    "version_bump",
    "parity",
    "static_checks",
    "sim_safety_acceptance",
    "docker_sim_acceptance",
    # The combined acceptance launches the portable GCS this build produces.
    "windows_gcs_build",
    "combined_px4_gazebo_qgc_acceptance",
    "release_gate_manifest",
    "current_progress_rebind",
    "evidence_checks",
)


@dataclass(frozen=True)
class Stage:
    name: str
    estimate: str
    disk: str
    command: tuple[str, ...]
    owner_required: bool = False


def _python() -> str:
    return sys.executable


def stage_definitions(root: Path = ROOT) -> tuple[Stage, ...]:
    run_root = "artifacts/release-pre9-runs/<run-id>"
    return (
        Stage(
            "preflight",
            "1-3 min",
            "read-only",
            (_python(), "scripts/run_release_pre9.py", "--only-stage", "preflight"),
        ),
        Stage(
            "version_bump",
            "<1 min",
            "<1 MB",
            (_python(), "scripts/run_release_pre9.py", "--only-stage", "version_bump"),
        ),
        Stage(
            "parity",
            "<1 min",
            "<5 MB",
            (
                _python(),
                "scripts/create_release_parity.py",
                "--root",
                ".",
                "--out",
                "configs/release-parity.json",
                "--version-manifest",
                "configs/product-version.json",
                "&&",
                _python(),
                "scripts/verify_current_progress_index.py",
                "--root",
                ".",
                "--check-sources-only",
            ),
        ),
        Stage(
            "static_checks",
            "5-20 min",
            "<2 GB",
            (
                _python(),
                "-m",
                "ruff",
                "check",
                ".",
                "&&",
                _python(),
                "scripts/run_onboard_gate.py",
                "-q",
                "-p",
                "no:warnings",
                "&&",
                _python(),
                "-m",
                "pytest",
                "-q",
                # Source-only checks; the progress index and release gate are
                # bound to the new version/parity only by current_progress_rebind,
                # so their tests run in evidence_checks at the end.
                "tests/test_release_parity.py",
                "tests/test_product_version.py",
            ),
        ),
        Stage(
            "sim_safety_acceptance",
            "2-10 min",
            "<1 GB",
            (
                _python(),
                "scripts/run_simulator_safety_acceptance.py",
                "--out",
                f"{run_root}/simulator-safety-acceptance.json",
            ),
        ),
        Stage(
            "docker_sim_acceptance",
            "10-30 min",
            "5-15 GB",
            (
                _python(),
                "scripts/run_simulator_docker_acceptance.py",
                "--scenario-manifest",
                "configs/scenarios/river_rescue_vtol_001.scenario.json",
                "--evidence",
                f"{run_root}/simulator-docker-acceptance.json",
                "--execute",
                "--timeout",
                "1200",
            ),
        ),
        Stage(
            "windows_gcs_build",
            "20-60 min",
            "10-30 GB",
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "scripts/build_multidetect_windows.ps1",
                "-Configuration",
                "Release",
                "-BuildSelfTests",
                "-InstallPortable",
                "-Parallel",
                "4",
                "-Version",
                TARGET_VERSION,
                # -BuildSelfTests also builds and runs the Ed25519 mission-security
                # self-test inside the MSVC/Qt shell (a bare cmake --build here had
                # no MSVC include path).
            ),
        ),
        Stage(
            "combined_px4_gazebo_qgc_acceptance",
            "15-45 min",
            "5-20 GB",
            (
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                "scripts/run_px4_gazebo_qgc_combined_sim_acceptance.ps1",
                "-EvidencePath",
                f"{run_root}/combined-px4-gazebo-qgc-evidence.json",
            ),
        ),
        Stage(
            "release_gate_manifest",
            "1-3 min",
            "<50 MB",
            (
                _python(),
                "scripts/create_release_gate_manifest.py",
                "--out",
                f"{run_root}/release-gate-pre9.json",
                "--parity-json",
                "configs/release-parity.json",
                "--replay-evidence",
                f"{run_root}/replay-owner-required.json",
                "--sim-evidence",
                f"{run_root}/sim-owner-required.json",
                "--hil-evidence",
                f"{run_root}/hil-owner-required.json",
                "--real-evidence",
                f"{run_root}/real-owner-required.json",
                "--simulator-safety-evidence",
                f"{run_root}/simulator-safety-acceptance.json",
                "&&",
                _python(),
                "scripts/check_release_gate.py",
                f"{run_root}/release-gate-pre9.json",
                "--version-manifest",
                "configs/product-version.json",
                "--out",
                f"{run_root}/release-gate-pre9-report.json",
                "--report-only",
            ),
        ),
        Stage(
            "current_progress_rebind",
            "1-3 min",
            "<50 MB",
            (_python(), "scripts/rebind_current_release_parity_index.py"),
            owner_required=True,
        ),
        Stage(
            "evidence_checks",
            "2-5 min",
            "<100 MB",
            (
                _python(),
                "-m",
                "pytest",
                "-q",
                "tests/test_current_progress_index.py",
                "tests/test_release_gate.py",
                "tests/test_release_gate_manifest.py",
                "tests/test_current_release_gate_wrapper.py",
            ),
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_command(command: Sequence[str], root: Path, run_dir: Path | None = None) -> list[str]:
    # The displayed command uses ``&&`` as a readable separator.  Runtime
    # execution handles the two commands independently so failure stops cleanly.
    run_text = str(run_dir) if run_dir is not None else "artifacts/release-pre9-runs/<run-id>"
    resolved: list[str] = []
    for value in command:
        value = value.replace("artifacts/release-pre9-runs/<run-id>", run_text)
        resolved.append(str(root / value) if value.startswith("scripts/") else value)
    return resolved


def _run(
    command: Sequence[str], root: Path, log: Path, *, allow_exit_codes: set[int] | None = None
) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("$ " + subprocess.list2cmdline(list(command)) + "\n")
        handle.flush()
        result = subprocess.run(
            list(command),
            cwd=root,
            env=os.environ.copy(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    elapsed = time.monotonic() - started
    with log.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"\nexit_code={result.returncode}\nelapsed_seconds={elapsed:.3f}\n")
    if result.returncode and result.returncode not in (allow_exit_codes or set()):
        raise RuntimeError(
            f"command failed ({result.returncode}): {subprocess.list2cmdline(list(command))}"
        )
    return result.returncode


def _tool(name: str) -> str | None:
    return shutil.which(name)


def preflight_report(root: Path = ROOT) -> dict[str, object]:
    checks: dict[str, object] = {}
    for drive in ("C:\\", "D:\\"):
        try:
            usage = shutil.disk_usage(drive)
            checks[f"disk_{drive[0].lower()}_free_bytes"] = usage.free
            checks[f"disk_{drive[0].lower()}_free_gib"] = round(usage.free / 1024**3, 2)
        except OSError as exc:
            checks[f"disk_{drive[0].lower()}_error"] = str(exc)
    required_tools = (
        "python",
        "ruff",
        "cmake",
        "git",
        "patch",
        "docker",
        "qmake",
        "gst-inspect-1.0",
    )
    checks["tools"] = {name: _tool(name) for name in required_tools}
    checks["missing_tools"] = [name for name in required_tools if _tool(name) is None]
    checks["msvc_vswhere"] = _tool("vswhere") or (
        "C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe"
        if Path("C:\\Program Files (x86)\\Microsoft Visual Studio\\Installer\\vswhere.exe").exists()
        else None
    )
    vswhere = checks["msvc_vswhere"]
    checks["msvc_installation"] = None
    if vswhere:
        try:
            probe = subprocess.run(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                capture_output=True,
                timeout=10,
                text=True,
            )
            checks["msvc_installation"] = probe.stdout.strip() or None
        except (OSError, subprocess.TimeoutExpired):
            checks["msvc_installation"] = None
    checks["temp_expected"] = os.environ.get("TEMP") == str(root / ".tmp")
    checks["temp_exists"] = (root / ".tmp").exists()
    checks["docker_daemon"] = False
    docker = _tool("docker")
    if docker:
        try:
            checks["docker_daemon"] = (
                subprocess.run(
                    [docker, "info", "--format", "{{.ServerVersion}}"],
                    cwd=root,
                    capture_output=True,
                    timeout=10,
                    text=True,
                ).returncode
                == 0
            )
        except (OSError, subprocess.TimeoutExpired):
            checks["docker_daemon"] = False
    checks["msvc_required"] = bool(checks["msvc_installation"])
    checks["ed25519_self_test_target"] = (
        root / "ground-control/MultiDetectGCS/custom/tests/MultiDetectMissionSecuritySelfTest.cc"
    ).exists()
    return checks


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _version_bump(root: Path) -> None:
    path = root / "configs/product-version.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(
        {
            "release_label": TARGET_VERSION,
            "numeric": "0.3.0",
            "python": TARGET_PYTHON_VERSION,
            "channel": "prerelease",
        }
    )
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _owner_evidence(run_dir: Path) -> None:
    for mode in ("replay", "sim", "hil", "real"):
        _write_json(
            run_dir / f"{mode}-owner-required.json",
            {
                "schema_version": 1,
                "event": f"pre9-{mode}-owner-required",
                "runtime_mode": mode,
                "gate_passed": False,
                "owner_required": True,
                "claims": [],
                "notes": "fresh diagnostic placeholder; no acceptance claimed",
            },
        )


def _artifact_hashes(run_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(run_dir).as_posix(): _sha256(path)
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and not path.name.endswith(".status.json")
    }


def _internal_log(run_dir: Path, stage: Stage, detail: object) -> None:
    _write_json(
        run_dir / f"{stage.name}.log.json",
        {"command": list(stage.command), "stage": stage.name, "result": detail},
    )


def _execute_stage(stage: Stage, root: Path, run_dir: Path) -> None:
    if stage.name == "preflight":
        report = preflight_report(root)
        _write_json(run_dir / "preflight.json", report)
        missing = list(report["missing_tools"])
        if not report.get("msvc_required"):
            missing.append("Visual Studio MSVC C++ workload")
        if not report["docker_daemon"]:
            missing.append("Docker Desktop daemon")
        if not report["temp_exists"]:
            missing.append(".tmp writable directory")
        _internal_log(run_dir, stage, {"preflight": report, "missing": missing})
        if missing:
            raise RuntimeError("preflight missing: " + ", ".join(missing))
        return
    if stage.name == "version_bump":
        _version_bump(root)
        _internal_log(run_dir, stage, {"release_label": TARGET_VERSION})
        return
    if stage.name == "release_gate_manifest":
        _owner_evidence(run_dir)
        command = list(stage.command)
        # Shell separators in the declarative command are executed in two
        # fail-closed subprocesses; writer exit 2 is expected for pending modes.
        split = command.index("&&")
        _run(
            _resolve_command(command[:split], root, run_dir),
            root,
            run_dir / "release_gate_manifest.log",
            allow_exit_codes={2},
        )
        _run(
            _resolve_command(command[split + 1 :], root, run_dir),
            root,
            run_dir / "check_release_gate.log",
            allow_exit_codes={0, 1, 2},
        )
        report_path = run_dir / "release-gate-pre9-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("release_allowed") is not False:
            raise RuntimeError("pre-release gate report must remain release_allowed=false")
        return
    # Every "&&" separates a fail-closed subprocess (static_checks has three).
    for index, part in enumerate(_split_commands(stage.command)):
        _run(
            _resolve_command(part, root, run_dir),
            root,
            run_dir / (f"{stage.name}.log" if index == 0 else f"{stage.name}-{index + 1}.log"),
        )


def _split_commands(command: Sequence[str]) -> list[list[str]]:
    parts: list[list[str]] = [[]]
    for token in command:
        if token == "&&":
            parts.append([])
        else:
            parts[-1].append(token)
    if any(not part for part in parts):
        raise ValueError(f"empty command around '&&': {list(command)}")
    return parts


def _print_plan(stages: Sequence[Stage]) -> None:
    print("release-pre9 dry-run (no files will be written)")
    for index, stage in enumerate(stages, 1):
        print(f"[{index:02d}] {stage.name}: estimate={stage.estimate}; disk={stage.disk}")
        print("     " + subprocess.list2cmdline(list(stage.command)))
    print(
        "owner-required: HIL, REAL, hardware, and manual QGC visual acceptance "
        "remain false/not_claimed"
    )
    print("release_allowed: false (prerelease and incomplete owner evidence)")


def run(args: argparse.Namespace) -> int:
    stages = stage_definitions(ROOT)
    selected = list(stages)
    if args.only_stage:
        selected = [stage for stage in stages if stage.name == args.only_stage]
        if not selected:
            raise SystemExit(f"unknown stage: {args.only_stage}")
    elif args.from_stage:
        names = [stage.name for stage in stages]
        if args.from_stage not in names:
            raise SystemExit(f"unknown stage: {args.from_stage}")
        selected = list(stages[names.index(args.from_stage) :])
    if args.dry_run:
        _print_plan(selected)
        print(json.dumps(preflight_report(ROOT), ensure_ascii=False, indent=2))
        return 0
    run_dir = (
        Path(args.run_dir)
        if args.run_dir
        else ROOT
        / "artifacts"
        / "release-pre9-runs"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    run_dir = run_dir if run_dir.is_absolute() else ROOT / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    for stage in selected:
        marker = run_dir / f"{stage.name}.status.json"
        if (
            marker.exists()
            and json.loads(marker.read_text(encoding="utf-8")).get("status") == "passed"
        ):
            print(f"SKIP {stage.name} (already passed in {run_dir})")
            continue
        started = time.monotonic()
        record = {
            "stage": stage.name,
            "status": "running",
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(marker, record)
        try:
            _execute_stage(stage, ROOT, run_dir)
        except Exception as exc:
            record.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "artifact_sha256": _artifact_hashes(run_dir),
                }
            )
            _write_json(marker, record)
            raise
        record.update(
            {
                "status": "passed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "artifact_sha256": _artifact_hashes(run_dir),
            }
        )
        _write_json(marker, record)
        print(f"PASS {stage.name} ({record['elapsed_seconds']}s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--from-stage", choices=STAGE_NAMES)
    parser.add_argument("--only-stage", choices=STAGE_NAMES)
    parser.add_argument("--run-dir", type=Path)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
