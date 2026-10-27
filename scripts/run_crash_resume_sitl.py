#!/usr/bin/env python3
"""Exercise onboard crash recovery against one disposable PX4/Gazebo SITL per case.

Run this command inside the repository SITL lock.  The onboard process is
always terminated by its Popen handle (the PID created by this script).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/evaluation/crash-resume-sitl-20261027"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.run_simulator_docker import DEFAULT_SCENARIO, build_docker_simulator_plan

CASES = ("valid", "corrupt", "digest", "expired", "rtl")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        path = path / "mission.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except ValueError:
            continue
    return rows


def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def launch_onboard(args, work: Path, log_path: Path) -> subprocess.Popen:
    command = [
        sys.executable, str(ROOT / "scripts/onboard_main.py"), "real",
        "--endpoint", f"udpin:127.0.0.1:{args.telemetry_port}",
        "--camera", args.camera, "--camera-backend", args.camera_backend,
        "--calibration", str(args.calibration), "--mission-package", str(args.mission_package),
        "--operator-key", str(args.operator_key), "--failsafe", "rtl",
        "--duration", str(args.duration), "--startup-timeout", str(args.startup_timeout),
        "--takeoff-timeout", str(args.takeoff_timeout), "--work-dir", str(work),
    ]
    if args.apply_px4_params:
        command.append("--apply-px4-params")
    stream = log_path.open("ab", buffering=0)
    proc = subprocess.Popen(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    proc._crash_resume_log_stream = stream
    return proc


def terminate_owned(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    # Popen.terminate targets this exact process handle/PID; never search by name.
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    finally:
        proc._crash_resume_log_stream.close()


def telemetry_probe(port: int, duration: float = 2.0) -> list[dict[str, Any]]:
    """Collect MAVLink telemetry evidence from the simulator's output stream."""
    try:
        from pymavlink import mavutil
    except ImportError:
        return []
    connection = mavutil.mavlink_connection(f"udpin:127.0.0.1:{port}", source_system=255)
    records = []
    deadline = time.monotonic() + duration
    try:
        while time.monotonic() < deadline:
            message = connection.recv_match(blocking=True, timeout=0.2)
            if message is None:
                continue
            kind = message.get_type()
            row: dict[str, Any] = {"type": kind}
            if kind == "HEARTBEAT":
                row["mode"] = mavutil.mode_string_v10(message)
                row["armed"] = bool(message.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            elif kind == "COMMAND_ACK":
                row["command"] = int(message.command)
                row["result"] = int(message.result)
            records.append(row)
    finally:
        connection.close()
    return records


def run_case(name: str, args) -> dict[str, Any]:
    case_dir = OUT / name
    case_dir.mkdir(parents=True, exist_ok=True)
    work = case_dir / "onboard-work"
    process_log = case_dir / "onboard-process.log"
    container = f"md-crash-resume-{name}"
    plan = build_docker_simulator_plan(
        args.scenario, repository_root=ROOT, job_dir=case_dir / "sim-job",
        container_name=container, control_port=args.control_port,
        telemetry_port=args.telemetry_port, dry_run=False,
    )
    docker = plan.docker_executable or "docker"
    result: dict[str, Any] = {"scenario": name, "container": container, "passed": False, "evidence": {}}
    first = second = None
    container_claimed = False
    try:
        if subprocess.run([docker, "container", "inspect", container], capture_output=True).returncode == 0:
            raise RuntimeError(f"refusing pre-existing container {container}")
        subprocess.run([docker, *plan.command[1:]], check=True, capture_output=True, text=True)
        container_claimed = True
        if not wait_for(lambda: subprocess.run([docker, "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True).stdout.strip() == "true", args.startup_timeout):
            raise TimeoutError("owned PX4 container did not become running")
        first = launch_onboard(args, work, process_log)
        result["first_pid"] = first.pid
        checkpoint = work / "crash_resume/checkpoint.json"
        if not wait_for(lambda: checkpoint.exists() and _sequence(checkpoint) >= args.crash_after_sequence, args.flight_timeout):
            raise TimeoutError("onboard did not write the requested airborne checkpoint")
        first_cp = json.loads(checkpoint.read_text(encoding="utf-8"))
        # Do not kill until armed-in-air telemetry has been observed.
        flight = telemetry_probe(args.telemetry_port, 1.0)
        if not any(record.get("armed") for record in flight if record.get("type") == "HEARTBEAT"):
            raise RuntimeError("PX4 airborne/armed telemetry was not observed before process stop")
        terminate_owned(first)
        if name == "corrupt":
            checkpoint.write_text('{"schema":"broken"', encoding="utf-8")
        elif name == "digest":
            changed = dict(first_cp)
            changed["package_digest"] = "0" * 64
            checkpoint.write_text(json.dumps(changed), encoding="utf-8")
        elif name == "expired":
            changed = dict(first_cp)
            changed["saved_at"] = time.time() - 3600
            checkpoint.write_text(json.dumps(changed), encoding="utf-8")
        elif name == "rtl":
            subprocess.run([docker, "exec", container, "/bin/sh", "-lc", "pxh> commander mode rtl"], check=False, capture_output=True)
        second = launch_onboard(args, work, process_log)
        result["second_pid"] = second.pid
        wait_for(lambda: "crash_resume" in process_log.read_text(encoding="utf-8", errors="replace"), args.decision_timeout)
        time.sleep(2)
        lines = process_log.read_text(encoding="utf-8", errors="replace").splitlines() if process_log.exists() else []
        mission_rows = read_jsonl(work / "mission_log")
        recovery_rows = [row for row in mission_rows if row.get("kind") == "crash_resume"]
        tail = mission_rows[-max(10, len(mission_rows) // 3):]
        commands = [row for row in tail if row.get("kind") in {"arm", "takeoff", "flight_handoff"}]
        result["evidence"] = {
            "recovery_rows": recovery_rows,
            "process_log_lines": [line for line in lines if "crash_resume" in line][-12:],
            "post_restart_step_id": next((row.get("step_id") for row in recovery_rows if row.get("step_id")), None),
            "checkpoint_before_crash": first_cp,
            "checkpoint_after_restart": _read_checkpoint(checkpoint),
            "post_restart_command_log_count": len(commands),
            "post_restart_command_log": commands,
            "telemetry": telemetry_probe(args.telemetry_port, 1.0),
        }
        row = recovery_rows[-1] if recovery_rows else {}
        expected_action = "resume" if name == "valid" else "monitor_failsafe" if name == "rtl" else "return_home"
        expected_reason = {"corrupt": "检查点", "digest": "任务包身份", "expired": "过期"}
        result["passed"] = (
            row.get("action") == expected_action
            and (name not in expected_reason or expected_reason[name] in str(row.get("reason_zh", "")))
            and (name != "valid" or result["evidence"]["post_restart_step_id"] == first_cp.get("step_id"))
            and not commands
        )
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for proc in (second, first):
            if proc is not None:
                terminate_owned(proc)
        if container_claimed:
            subprocess.run([docker, "rm", "-f", container], check=False, capture_output=True)
        result["container_absent_after_cleanup"] = subprocess.run([docker, "container", "inspect", container], capture_output=True).returncode != 0
    (case_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _sequence(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("sequence", 0))
    except (OSError, ValueError, TypeError):
        return 0


def _read_checkpoint(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-package", type=Path, required=True)
    parser.add_argument("--operator-key", type=Path, required=True)
    parser.add_argument("--camera", required=True, help="OpenCV camera index or GStreamer pipeline")
    parser.add_argument("--camera-backend", choices=("v4l2", "gstreamer"), default="gstreamer")
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--flight-timeout", type=float, default=600)
    parser.add_argument("--decision-timeout", type=float, default=90)
    parser.add_argument("--takeoff-timeout", type=float, default=60)
    parser.add_argument("--duration", type=float, default=7200)
    parser.add_argument("--crash-after-sequence", type=int, default=2)
    parser.add_argument("--apply-px4-params", action="store_true")
    args = parser.parse_args()
    for path in (args.mission_package, args.operator_key, args.calibration, args.scenario):
        if not path.is_file():
            parser.error(f"file not found: {path}")
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for name in args.cases:
        case = run_case(name, args)
        results.append(case)
        print(json.dumps({"scenario": name, "passed": case["passed"], "error": case.get("error")}, ensure_ascii=False), flush=True)
    summary = {"schema": "multidetect.crash-resume-sitl.v1", "generated_at": time.time(), "cases": results, "passed": all(row["passed"] and row.get("container_absent_after_cleanup") for row in results)}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(OUT / "summary.json"), "passed": summary["passed"]}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
