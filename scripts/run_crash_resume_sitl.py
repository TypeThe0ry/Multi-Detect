#!/usr/bin/env python3
"""Exercise onboard crash recovery against one disposable PX4/Gazebo SITL per case.

Run this command inside the repository SITL lock.  The onboard process is
always terminated by its Popen handle (the PID created by this script).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/evaluation/crash-resume-sitl-20261027"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from scripts.run_simulator_docker import DEFAULT_SCENARIO, build_docker_simulator_plan
from scripts.platform_paths import pythonpath_value
from scripts.sitl_lock import SitlLock, default_lock_path, sitl_lock_context
from multidetect.sim_network import sim_listen_host, sim_udpin_endpoint

CASES = ("valid", "corrupt", "digest", "expired", "rtl")


class Px4LoopbackRelay:
    """Bridge Docker's host UDP ports to onboard's loopback-only endpoint."""
    def __init__(self, px4_port: int, onboard_port: int, control_port: int):
        self.px4_port, self.onboard_port, self.control_port = px4_port, onboard_port, control_port
        self.stop = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((sim_listen_host(), px4_port))
        self.sock.settimeout(0.2)
        from pymavlink import mavutil
        self.mavutil = mavutil
        self.parser = mavutil.mavlink.MAVLink(None)
        self.telemetry: list[dict[str, Any]] = []
        self.telemetry_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()

    def _run(self):
        while not self.stop.is_set():
            try:
                packet, peer = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            if peer[1] == self.onboard_port:
                self.sock.sendto(packet, ("127.0.0.1", self.control_port))
            else:
                self._record_telemetry(packet)
                self.sock.sendto(packet, ("127.0.0.1", self.onboard_port))

    def _record_telemetry(self, packet: bytes):
        for byte in packet:
            message = self.parser.parse_char(bytes((byte,)))
            if message is not None and message.get_type() == "HEARTBEAT":
                row = {
                    "mode": self.mavutil.mode_string_v10(message),
                    "custom_mode": int(getattr(message, "custom_mode", 0)),
                    "base_mode": int(getattr(message, "base_mode", 0)),
                    "armed": bool(message.base_mode & self.mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
                    "time": time.time(),
                }
                with self.telemetry_lock:
                    self.telemetry.append(row)

    def send_to_px4(self, packet: bytes):
        self.sock.sendto(packet, ("127.0.0.1", self.control_port))


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
        "--endpoint", sim_udpin_endpoint(args.telemetry_port),
        "--camera", args.camera, "--camera-backend", args.camera_backend,
        "--calibration", str(args.calibration), "--mission-package", str(args.mission_package),
        "--operator-key", str(args.operator_key), "--failsafe", "rtl",
        "--operator-port", str(args.operator_port),
        "--duration", str(args.duration), "--startup-timeout", str(args.startup_timeout),
        "--takeoff-timeout", str(args.takeoff_timeout), "--work-dir", str(work),
    ]
    if args.apply_px4_params:
        command.append("--apply-px4-params")
    stream = log_path.open("ab", buffering=0)
    env = os.environ.copy()
    env.update({"PYTHONPATH": pythonpath_value(ROOT),
                "PYTHONUTF8": "1",
                "MULTIDETECT_ONBOARD_TEST_HOOK": "scripts.onboard_real_test_hook",
                "MULTIDETECT_CRASH_RESUME_TEST": "1",
                "MULTIDETECT_ONBOARD_MAVLINK_COMMAND_LOG": str(log_path.parent / "mavlink-commands.jsonl")})
    proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
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


def request_px4_rtl(endpoint: str) -> None:
    """Request RTL over the control socket before stopping the onboard sender."""
    from pymavlink import mavutil
    connection = mavutil.mavlink_connection(endpoint, source_system=254, source_component=190,
                                             autoreconnect=False)
    for _ in range(2):
        connection.mav.command_long_send(
            1, 1, mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH, 0,
            0, 0, 0, 0, 0, 0, 0,
        )
        time.sleep(0.05)
    connection.close()


def wait_for_px4_rtl(endpoint: str) -> list[dict[str, Any]]:
    """Observe RTL mode once the onboard process has stopped."""
    from pymavlink import mavutil
    from multidetect.onboard_runtime.fw_vtol_commands import PX4_AUTO_RTL_CUSTOM_MODE
    connection = mavutil.mavlink_connection(endpoint, source_system=254, source_component=190,
                                             autoreconnect=False)
    # The telemetry port is the one onboard binds after restart, so this
    # observer must release it on every path before onboard is relaunched.
    try:
        connection.wait_heartbeat(timeout=10)
        deadline = time.monotonic() + 8
        records = []
        while time.monotonic() < deadline:
            message = connection.recv_match(type="HEARTBEAT", blocking=True, timeout=0.5)
            if message is not None:
                records.append({"mode": mavutil.mode_string_v10(message),
                                "custom_mode": int(getattr(message, "custom_mode", 0)),
                                "base_mode": int(getattr(message, "base_mode", 0)),
                                "armed": bool(message.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
                                "time": time.time()})
            if any("RTL" in row["mode"] or row["custom_mode"] == PX4_AUTO_RTL_CUSTOM_MODE
                   for row in records):
                return records[-20:]
        raise TimeoutError(f"PX4 did not enter RTL: {records[-5:]}")
    finally:
        connection.close()


def run_case(name: str, args) -> dict[str, Any]:
    attempt = getattr(args, "attempt", 1)
    case_dir = OUT / name / f"attempt-{attempt}"
    case_dir.mkdir(parents=True, exist_ok=True)
    work = case_dir / "onboard-work"
    process_log = case_dir / "onboard-process.log"
    container = f"md-crash-resume-{name}-r{attempt}"
    plan = build_docker_simulator_plan(
        args.scenario, repository_root=ROOT, job_dir=case_dir / "sim-job",
        container_name=container, control_port=args.control_port,
        telemetry_port=args.telemetry_port, dry_run=False,
    )
    docker = plan.docker_executable or "docker"
    result: dict[str, Any] = {"scenario": name, "container": container, "passed": False, "evidence": {}}
    first = second = None
    container_claimed = False
    rtl_records: list[dict[str, Any]] = []
    relay = None
    try:
        if subprocess.run([docker, "container", "inspect", container], capture_output=True).returncode == 0:
            raise RuntimeError(f"refusing pre-existing container {container}")
        from scripts.run_simulator_docker import freeze_runtime_plan
        plan = freeze_runtime_plan(plan, docker=docker)
        subprocess.run([docker, *plan.command[1:]], check=True, capture_output=True, text=True)
        container_claimed = True
        if not wait_for(lambda: subprocess.run([docker, "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True).stdout.strip() == "true", args.startup_timeout):
            raise TimeoutError("owned PX4 container did not become running")
        first = launch_onboard(args, work, process_log)
        result["first_pid"] = first.pid
        checkpoint = work / "crash_resume/checkpoint.json"
        if not wait_for(lambda: checkpoint.exists() and _sequence(checkpoint) >= args.crash_after_sequence and _read_checkpoint(checkpoint).get("step_id") == args.crash_step_id, args.flight_timeout):
            raise TimeoutError("onboard did not reach the requested mission step and write its checkpoint")
        first_cp = json.loads(checkpoint.read_text(encoding="utf-8"))
        if name == "rtl":
            request_px4_rtl(f"udpout:127.0.0.1:{args.control_port}")
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
        if name == "rtl":
            # Confirm RTL on the telemetry port, release it, then relaunch onboard
            # right away so PX4 is still airborne in RTL when recovery classifies it.
            rtl_records = wait_for_px4_rtl(sim_udpin_endpoint(args.telemetry_port))
        second = launch_onboard(args, work, process_log)
        result["second_pid"] = second.pid
        wait_for(lambda: any(row.get("kind") == "crash_resume" for path in (work / "mission_log").rglob("mission.jsonl") for row in read_jsonl(path)), args.decision_timeout)
        time.sleep(2)
        lines = process_log.read_text(encoding="utf-8", errors="replace").splitlines() if process_log.exists() else []
        mission_rows = [row for mission_log in (work / "mission_log").rglob("mission.jsonl") for row in read_jsonl(mission_log)]
        recovery_rows = [row for row in mission_rows if row.get("kind") == "crash_resume"]
        command_log = case_dir / "mavlink-commands.jsonl"
        command_rows = read_jsonl(command_log)
        commands = [row for row in command_rows if row.get("pid") == second.pid and row.get("command") in {22, 400}]
        result["evidence"] = {
            "recovery_rows": recovery_rows,
            "process_log_lines": [line for line in lines if "crash_resume" in line][-12:],
            "post_restart_step_id": next((row.get("step_id") for row in recovery_rows if row.get("step_id")), None),
            "checkpoint_before_crash": first_cp,
            "checkpoint_after_restart": _read_checkpoint(checkpoint),
            "post_restart_command_log_count": len(commands),
            "post_restart_command_log": commands,
            "mavlink_commands": command_rows,
            "rtl_telemetry": rtl_records,
            "post_restart_checkpoint": _read_checkpoint(checkpoint),
        }
        row = max(recovery_rows, key=lambda item: item.get("timestamp", 0)) if recovery_rows else {}
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
        if relay is not None:
            relay.close()
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
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-package", type=Path, required=True)
    parser.add_argument("--operator-key", type=Path, required=True)
    parser.add_argument("--camera", required=True, help="OpenCV camera index or GStreamer pipeline")
    parser.add_argument("--camera-backend", choices=("v4l2", "gstreamer"), default="gstreamer")
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output-dir", type=Path, default=OUT,
                        help="write run evidence beneath this directory")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--control-port", type=int, default=14540)
    parser.add_argument("--telemetry-port", type=int, default=14541)
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--flight-timeout", type=float, default=600)
    parser.add_argument("--decision-timeout", type=float, default=90)
    parser.add_argument("--takeoff-timeout", type=float, default=60)
    parser.add_argument("--duration", type=float, default=7200)
    parser.add_argument("--operator-port", type=int, default=14680)
    parser.add_argument("--crash-after-sequence", type=int, default=2)
    parser.add_argument("--apply-px4-params", action="store_true")
    parser.add_argument("--crash-step-id", default="home")
    parser.add_argument("--sitl-lock-path", type=Path, default=default_lock_path())
    args = parser.parse_args()
    for path in (args.mission_package, args.operator_key, args.calibration, args.scenario):
        if not path.is_file():
            parser.error(f"file not found: {path}")
    OUT = args.output_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    with sitl_lock_context(SitlLock(args.sitl_lock_path, label="crash-resume", min_memory_gb=5.0)):
        for name in args.cases:
            args.attempt = 1
            case = run_case(name, args)
            if case.get("error") and any(term in case["error"].lower() for term in ("timeout", "docker", "memory", "container")):
                args.attempt = 2
                retry = run_case(name, args)
                retry["first_attempt_error"] = case["error"]
                case = retry
            results.append(case)
            print(json.dumps({"scenario": name, "passed": case["passed"], "error": case.get("error")}, ensure_ascii=False), flush=True)
    summary = {"schema": "multidetect.crash-resume-sitl.v1", "generated_at": time.time(), "cases": results, "passed": all(row["passed"] and row.get("container_absent_after_cleanup") for row in results)}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(OUT / "summary.json"), "passed": summary["passed"]}, ensure_ascii=False))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
